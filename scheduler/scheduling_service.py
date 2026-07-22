"""
scheduling_service.py
---------------------
The clean API layer for Phase 3's Scheduling Engine.

This module acts as the single entry point for all timetable generation.
It orchestrates the full pipeline:
  1. Pre-solve validation (validation.py)
  2. Solver execution (solver.py)
  3. Post-solve conflict detection (conflicts.py)
  4. Audit log creation (GenerationLog model)

Views and tests should call run_scheduling_pipeline() instead of
calling generate_timetable() directly.
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Optional

from .models import Timetable, GenerationLog
from .validation import validate_timetable_inputs
from .solver import generate_timetable
from .conflicts import check_conflicts_for_timetable
from .firebase_service import update_generation_status, update_timetable_conflicts
from .conflict_cache import clear_conflicts_cache

logger = logging.getLogger(__name__)


@dataclass
class SchedulingResult:
    """
    Structured result object returned by run_scheduling_pipeline().

    Attributes:
        status:              'OPTIMAL', 'FEASIBLE', 'INFEASIBLE', 'VALIDATION_ERROR', or 'ERROR'
        message:             Human-readable description of the outcome
        log_id:              PK of the GenerationLog record created for this run
        solve_time_seconds:  Wall-clock time the solver took (None if validation failed)
        solver_score:        Objective function value (lower = better; None if no solution)
        courses_scheduled:   Number of ScheduleSlots written to the DB
        hard_conflicts:      List of hard-conflict dicts found after solving
        soft_conflicts:      List of soft-conflict (warning) dicts found after solving
        validation_errors:   Blocking validation errors (empty if validation passed)
        validation_warnings: Non-blocking validation warnings
    """
    status: str
    message: str
    log_id: Optional[int] = None
    solve_time_seconds: Optional[float] = None
    solver_score: Optional[int] = None
    courses_scheduled: int = 0
    hard_conflicts: list = field(default_factory=list)
    soft_conflicts: list = field(default_factory=list)
    validation_errors: list = field(default_factory=list)
    validation_warnings: list = field(default_factory=list)


def run_scheduling_pipeline(timetable_id: int, time_limit_seconds: int = 60) -> SchedulingResult:
    """
    Executes the full scheduling pipeline for a timetable:

      Step 1 → Validate inputs (capacity, lecturer hours, structural checks)
      Step 2 → Run OR-Tools CP-SAT solver
      Step 3 → Detect post-solve conflicts
      Step 4 → Persist a GenerationLog audit record
      Step 5 → Return a SchedulingResult

    Args:
        timetable_id:        Primary key of the Timetable to generate.
        time_limit_seconds:  Maximum solver wall-clock time (default 60s).

    Returns:
        SchedulingResult with full outcome details.
    """
    # ── Fetch timetable ──────────────────────────────────────────────────────
    try:
        timetable = Timetable.objects.select_related('semester', 'semester__university').get(pk=timetable_id)
    except Timetable.DoesNotExist:
        logger.error(f"[SchedulingService] Timetable ID {timetable_id} not found.")
        return SchedulingResult(
            status='ERROR',
            message=f"Timetable with ID {timetable_id} does not exist.",
        )

    logger.info(f"[SchedulingService] Starting pipeline for timetable '{timetable.name}' (ID={timetable_id})")

    # Update status to VALIDATING in Firebase
    update_generation_status(timetable_id, {
        'status': 'VALIDATING',
        'message': 'Validating input constraints...',
        'courses_scheduled': 0,
        'hard_conflicts': 0,
        'soft_conflicts': 0,
    })

    # ── Step 1: Pre-solve Validation ─────────────────────────────────────────
    is_valid, val_errors, val_warnings = validate_timetable_inputs(timetable)

    if not is_valid:
        logger.warning(f"[SchedulingService] Validation failed with {len(val_errors)} error(s).")
        log = GenerationLog.objects.create(
            timetable=timetable,
            status='ERROR',
            message=f"Validation failed: {'; '.join(val_errors[:3])}{'...' if len(val_errors) > 3 else ''}",
            validation_errors=val_errors,
            validation_warnings=val_warnings,
        )
        
        # Update Firebase with validation error status
        update_generation_status(timetable_id, {
            'status': 'VALIDATION_ERROR',
            'message': "Timetable generation aborted due to validation errors.",
            'courses_scheduled': 0,
            'hard_conflicts': len(val_errors),
            'soft_conflicts': len(val_warnings),
            'validation_errors': val_errors,
            'validation_warnings': val_warnings,
        })
        # Notify managers of validation failure (non-blocking — runs on a daemon thread)
        try:
            from .notifications import notify_university_managers_async
            from django.urls import reverse
            link = reverse('scheduler:timetable_detail', kwargs={'pk': timetable.pk})
            notify_university_managers_async(
                university=timetable.semester.university,
                title="Timetable validation failed",
                message=f"Timetable '{timetable.name}' could not be generated due to {len(val_errors)} validation errors.",
                link=link,
                level='danger'
            )
        except Exception as e:
            logger.error(f"Failed to send manager notifications for validation failure: {e}")
        
        return SchedulingResult(
            status='VALIDATION_ERROR',
            message="Timetable generation aborted due to validation errors.",
            log_id=log.pk,
            validation_errors=val_errors,
            validation_warnings=val_warnings,
        )

    # ── Step 2: Solve ────────────────────────────────────────────────────────
    # Update status to SOLVING in Firebase
    update_generation_status(timetable_id, {
        'status': 'SOLVING',
        'message': 'Running optimization solver...',
        'courses_scheduled': 0,
        'hard_conflicts': 0,
        'soft_conflicts': 0,
    })

    wall_start = time.perf_counter()
    solver_status, solver_message, solver_score = generate_timetable(timetable_id, time_limit_seconds)
    wall_elapsed = round(time.perf_counter() - wall_start, 3)

    logger.info(f"[SchedulingService] Solver finished in {wall_elapsed}s — status={solver_status}, score={solver_score}")

    # ── Step 3: Post-solve Conflict Detection ────────────────────────────────
    # Update status to CHECKING_CONFLICTS in Firebase
    update_generation_status(timetable_id, {
        'status': 'CHECKING_CONFLICTS',
        'message': 'Checking for schedule conflicts...',
        'courses_scheduled': 0,
        'hard_conflicts': 0,
        'soft_conflicts': 0,
    })

    hard_conflicts = []
    soft_conflicts = []
    courses_scheduled = 0

    if solver_status in ('OPTIMAL', 'FEASIBLE'):
        all_conflicts = check_conflicts_for_timetable(timetable)
        hard_conflicts = [c for c in all_conflicts if c['severity'] == 'error']
        soft_conflicts = [c for c in all_conflicts if c['severity'] == 'warning']
        courses_scheduled = timetable.slots.values('course_id').distinct().count()

        # Eagerly clear any stale conflict-JSON cache for this timetable so the
        # next poll always sees the freshly generated schedule.
        clear_conflicts_cache(timetable_id)

        logger.info(
            f"[SchedulingService] Post-solve: {courses_scheduled} courses, "
            f"{len(hard_conflicts)} errors, {len(soft_conflicts)} warnings"
        )

    # ── Step 4: Persist GenerationLog ────────────────────────────────────────
    log_message = solver_message
    if solver_status in ('OPTIMAL', 'FEASIBLE') and hard_conflicts:
        log_message += f" | {len(hard_conflicts)} hard conflict(s) detected after solving."

    # Try to find an existing PENDING log for this timetable to update (e.g. from background queue)
    log = GenerationLog.objects.filter(timetable=timetable, status='PENDING').order_by('-created_at').first()
    if log:
        log.status = solver_status
        log.message = log_message
        log.solver_score = solver_score
        log.solve_time_seconds = wall_elapsed
        log.courses_scheduled = courses_scheduled
        log.hard_conflicts_found = len(hard_conflicts)
        log.soft_conflicts_found = len(soft_conflicts)
        log.validation_errors = val_errors
        log.validation_warnings = val_warnings
        log.save()
        logger.info(f"[SchedulingService] GenerationLog updated (ID={log.pk})")
    else:
        log = GenerationLog.objects.create(
            timetable=timetable,
            status=solver_status,
            message=log_message,
            solver_score=solver_score,
            solve_time_seconds=wall_elapsed,
            courses_scheduled=courses_scheduled,
            hard_conflicts_found=len(hard_conflicts),
            soft_conflicts_found=len(soft_conflicts),
            validation_errors=val_errors,
            validation_warnings=val_warnings,
        )
        logger.info(f"[SchedulingService] GenerationLog created (ID={log.pk})")

    # ── Step 5: Update Firebase Final Status and Conflicts ────────────────────
    fb_errors = [{'type': c['constraint_type'], 'message': c['message']} for c in hard_conflicts]
    fb_warnings = [{'type': c['constraint_type'], 'message': c['message']} for c in soft_conflicts]
    
    update_timetable_conflicts(timetable_id, {
        'hard_count': len(fb_errors),
        'soft_count': len(fb_warnings),
        'total': len(hard_conflicts) + len(soft_conflicts),
        'errors': fb_errors,
        'warnings': fb_warnings,
    })
    
    update_generation_status(timetable_id, {
        'status': solver_status,
        'message': solver_message,
        'solve_time': wall_elapsed,
        'solver_score': solver_score,
        'courses_scheduled': courses_scheduled,
        'hard_conflicts': len(fb_errors),
        'soft_conflicts': len(fb_warnings),
    })

    # ── Step 6: Notify University Managers (non-blocking — runs on a daemon thread) ──
    try:
        from .notifications import notify_university_managers_async
        title = f"Timetable generation completed: {solver_status}"
        msg = (
            f"Timetable '{timetable.name}' generation run is completed.\n"
            f"Status: {solver_status}\n"
            f"Courses scheduled: {courses_scheduled}\n"
            f"Hard conflicts: {len(hard_conflicts)}\n"
            f"Soft conflicts: {len(soft_conflicts)}\n"
            f"Elapsed time: {wall_elapsed} seconds."
        )
        from django.urls import reverse
        link = reverse('scheduler:timetable_detail', kwargs={'pk': timetable.pk})
        notify_university_managers_async(
            university=timetable.semester.university,
            title=title,
            message=msg,
            link=link,
            # FIX BUG 11: Use proper severity levels instead of collapsing all failures to 'warning'.
            # 'danger' for ERROR/INFEASIBLE, 'warning' for success-with-conflicts, 'success' otherwise.
            level=(
                'success' if solver_status in ('OPTIMAL', 'FEASIBLE') and len(hard_conflicts) == 0
                else 'warning' if solver_status in ('OPTIMAL', 'FEASIBLE')
                else 'danger'
            )
        )
    except Exception as e:
        logger.error(f"Failed to send manager notifications: {e}")

    # ── Step 7: Return result ─────────────────────────────────────────────────
    return SchedulingResult(
        status=solver_status,
        message=log_message,
        log_id=log.pk,
        solve_time_seconds=wall_elapsed,
        solver_score=solver_score,
        courses_scheduled=courses_scheduled,
        hard_conflicts=hard_conflicts,
        soft_conflicts=soft_conflicts,
        validation_errors=val_errors,
        validation_warnings=val_warnings,
    )
