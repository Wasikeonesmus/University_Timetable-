"""
solver.py — Timetable Solver Orchestrator (Facade)
===================================================

This module is the single public entry point for timetable generation.
All heavy lifting has been extracted into focused, independently-testable
modules:

    solver_context.py   — Data objects, context builder, course expansion,
                          overlap map, capacity check
    solver_rules.py     — CustomRuleSet (forbidden/required rules),
                          OccupancyTracker (bitmask occupancy)
    solver_greedy.py    — Phase-1 heuristic greedy engine
    solver_cpsat.py     — Phase-2 CP-SAT model builder + solver runner,
                          FirebaseProgressCallback
    solver_sanitizer.py — Pre-write conflict sanitizer
    solver_compactor.py — Post-write schedule gap compactor

Public API (unchanged):
    generate_timetable(timetable_id, time_limit_seconds=30)
        → (status: str, message: str, objective_value: int | None)

Design patterns used:
    Facade        — this file hides the multi-module pipeline behind one call
    Strategy      — greedy and CP-SAT are interchangeable solving strategies
    Builder       — build_scheduling_context() assembles the shared context
    Pipeline      — data flows: load → context → greedy → [cpsat] → sanitize → save → compact
    Value Object  — CourseObj, RoomObj, SchedulingContext carry data without mutation
    Filter        — sanitize_assignments() is a pipeline filter step
"""

import time
import logging
from collections import defaultdict
from django.db import transaction
from ortools.sat.python import cp_model

# ── Pipeline module imports ───────────────────────────────────────────────────
from .solver_context import (
    CourseObj, RoomObj, SchedulingContext,
    build_scheduling_context, expand_courses_for_scheduling,
    check_scheduling_capacity, _VIRTUAL_ID_COUNTER,
)
from .solver_rules import CustomRuleSet, OccupancyTracker
from .solver_greedy import greedy_assign as _greedy_assign
from .solver_cpsat import build_cpsat_model, run_cpsat_solver, FirebaseProgressCallback
from .solver_sanitizer import sanitize_assignments as _sanitize_assignments
from .solver_compactor import compact_schedule_gaps

# ── Django model imports ───────────────────────────────────────────────────────
from .models import (
    Timetable, ScheduleSlot, Course, Lecturer, StudentGroup,
    Room, TimeSlot, Constraint, LecturerAvailability,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Public Entry Point
# ──────────────────────────────────────────────────────────────────────────────

def generate_timetable(timetable_id: int, time_limit_seconds: int = 30):
    """
    Generate a timetable using a two-phase approach:
      Phase 1 — Greedy pre-assignment  (fast, O(C×R×T))
      Phase 2 — CP-SAT polishing       (warm-started from greedy hints;
                                        skipped for large datasets or when
                                        greedy achieves 100% coverage)

    Returns: (status, message, objective_value)
      status:          'OPTIMAL', 'FEASIBLE', 'INFEASIBLE', or 'ERROR'
      message:         Human-readable outcome description
      objective_value: Integer solver score (lower = better); None if no solution
    """
    from .signals import mute_signals
    with mute_signals():
        return _generate_timetable_impl(timetable_id, time_limit_seconds)


# ──────────────────────────────────────────────────────────────────────────────
# Internal Orchestrator
# ──────────────────────────────────────────────────────────────────────────────

def _generate_timetable_impl(timetable_id: int, time_limit_seconds: int = 30):
    try:
        timetable = Timetable.objects.select_related(
            'semester', 'semester__university'
        ).get(pk=timetable_id)
    except Timetable.DoesNotExist:
        return 'ERROR', f"Timetable with ID {timetable_id} does not exist.", None

    university = timetable.semester.university

    # ── 1. Load raw data ───────────────────────────────────────────────────
    courses_raw = list(Course.objects.filter(
        program__department__faculty__campus__university=university,
        lecturer__isnull=False,
        lecturer__is_active=True,
        student_group__isnull=False,
    ).values(
        'id', 'code', 'name', 'duration_slots', 'sessions_per_week',
        'required_room_type', 'lecturer_id', 'student_group_id',
        'program__department__faculty__campus_id', 'student_group__size',
    ))

    _VIRTUAL_ID_COUNTER[0] = 10_000_000
    courses_raw = expand_courses_for_scheduling(courses_raw)
    courses     = [CourseObj(c) for c in courses_raw]

    rooms_raw = list(Room.objects.filter(campus__university=university).values(
        'id', 'name', 'capacity', 'room_type', 'campus_id', 'is_virtual',
    ))
    rooms = [RoomObj(r) for r in rooms_raw]

    timeslots = list(
        TimeSlot.objects.filter(university=university)
        .order_by('day_of_week', 'slot_number')
    )

    # ── 2. Pre-flight capacity check ───────────────────────────────────────
    capacity_check = check_scheduling_capacity(courses, rooms, timeslots)
    for w in capacity_check['warnings']:
        logger.warning(f"[Capacity Check] {w}")
    if not capacity_check['ok']:
        for e in capacity_check['errors']:
            logger.error(f"[Capacity Check] {e}")
        return 'INFEASIBLE', (
            "Cannot generate timetable — course load exceeds available capacity: "
            + " | ".join(capacity_check['errors'])
        ), None

    # ── 3. Load constraints + build shared context ─────────────────────────
    db_constraints = list(Constraint.objects.filter(university=university))
    ctx = build_scheduling_context(university, timeslots, db_constraints)

    # Unpack frequently-used context fields
    overlap_map          = ctx.overlap_map
    ts_to_idx            = ctx.ts_to_idx
    idx_to_ts            = ctx.idx_to_ts
    ts_id_by_idx         = ctx.ts_id_by_idx
    ts_day_by_idx        = ctx.ts_day_by_idx
    ts_is_evening_by_idx = ctx.ts_is_evening_by_idx
    ts_pos_in_day        = ctx.ts_pos_in_day
    timeslots_by_day     = ctx.timeslots_by_day
    room_features_map    = ctx.room_features_map
    course_required_features_map  = ctx.course_required_features_map
    course_additional_groups_map  = ctx.course_additional_groups_map
    room_building_map    = ctx.room_building_map
    building_distances   = ctx.building_distances
    lecturer_preferences_prefer   = ctx.lecturer_preferences_prefer
    lecturer_preferences_dislike  = ctx.lecturer_preferences_dislike
    lecturer_max_slots_per_day    = ctx.lecturer_max_slots_per_day
    rule_set             = ctx.rule_set

    if not courses:
        return 'ERROR', "No courses with assigned lecturers and student groups found.", None
    if not rooms:
        return 'ERROR', "No rooms found in the university campuses.", None
    if not timeslots:
        return 'ERROR', "No time slots found.", None

    num_courses = len(courses)
    if num_courses <= 2000:
        lecturers      = list(Lecturer.objects.filter(
            department__faculty__campus__university=university
        ))
        student_groups = list(StudentGroup.objects.filter(
            program__department__faculty__campus__university=university
        ))
    else:
        lecturers      = []
        student_groups = []

    # ── 4. Pre-compute course attributes ──────────────────────────────────
    course_durations   = {c.id: c.duration_slots   for c in courses}
    course_lecturers   = {c.id: c.lecturer_id       for c in courses}
    course_groups      = {c.id: c.student_group_id  for c in courses}
    course_campuses    = {c.id: c.campus_id         for c in courses}
    course_room_types  = {c.id: c.required_room_type for c in courses}

    course_group_sizes: dict = {}
    for c in courses:
        total_size = c.group_size
        for add_g_id in course_additional_groups_map.get(c.orig_course_id, set()):
            total_size += ctx.group_sizes.get(add_g_id, 0)
        course_group_sizes[c.id] = total_size

    # Pre-group rooms
    rooms_by_campus_and_type: dict = {}
    for r in rooms:
        rooms_by_campus_and_type.setdefault((r.campus_id, r.room_type), []).append(r)
    for key in rooms_by_campus_and_type:
        rooms_by_campus_and_type[key].sort(key=lambda x: x.capacity)

    rooms_by_campus: dict = {}
    for r in rooms:
        rooms_by_campus.setdefault(r.campus_id, []).append(r)
    for key in rooms_by_campus:
        rooms_by_campus[key].sort(key=lambda x: x.capacity)

    course_by_id = {c.id: c for c in courses}
    room_by_id   = {r.id: r for r in rooms}

    # Dynamic room candidate limit
    if num_courses <= 50:
        room_limit = 20
    elif num_courses <= 150:
        room_limit = 12
    elif num_courses <= 2000:
        room_limit = 6
    else:
        room_limit = 4

    # ── 5. Pre-compute valid start indices ────────────────────────────────
    hard_no_evening = any(
        c.constraint_type == 'NO_EVENING_CLASSES' and c.is_hard
        for c in db_constraints
    )

    course_valid_start_indices:      dict = {}
    course_orig_valid_indices_count: dict = {}
    for course in courses:
        c_id     = course.id
        duration = course_durations[c_id]
        orig_dur = course.orig_duration
        valid_indices: list  = []
        orig_valid_count = 0
        for ts in timeslots:
            if hard_no_evening and ts.is_evening:
                continue
            ts_idx    = ts_to_idx[ts.id]
            day_slots = timeslots_by_day[ts.day_of_week]
            pos       = ts_pos_in_day[ts.id]
            if pos + duration <= len(day_slots):
                spanned = day_slots[pos: pos + duration]
                if not any(spanned[i].end_time > spanned[i + 1].start_time for i in range(len(spanned) - 1)):
                    if not (hard_no_evening and any(s.is_evening for s in spanned)):
                        valid_indices.append(ts_idx)
            if pos + orig_dur <= len(day_slots):
                spanned_orig = day_slots[pos: pos + orig_dur]
                if not any(spanned_orig[i].end_time > spanned_orig[i + 1].start_time for i in range(len(spanned_orig) - 1)):
                    if not (hard_no_evening and any(s.is_evening for s in spanned_orig)):
                        orig_valid_count += 1
        course_valid_start_indices[c_id]      = valid_indices
        course_orig_valid_indices_count[c_id] = orig_valid_count

    # ── 6. Parse constraint mappings ──────────────────────────────────────
    lab_only_course_ids: set = set()
    for c in db_constraints:
        if c.constraint_type == 'LAB_ONLY_COURSE' and c.is_hard:
            cid = c.parameters.get('course_id')
            if cid:
                lab_only_course_ids.add(int(cid))

    student_max_per_day: dict = {}
    for c in db_constraints:
        if c.constraint_type == 'STUDENT_MAX_CLASSES_PER_DAY' and c.is_hard:
            gid     = c.parameters.get('student_group_id')
            max_cls = c.parameters.get('max_classes')
            if gid and max_cls is not None:
                student_max_per_day[int(gid)] = int(max_cls)

    lecturer_hard_unavailables: dict = defaultdict(set)
    lecturer_soft_unavailables: dict = defaultdict(list)
    course_hard_pref_rooms:     dict = {}
    course_soft_pref_rooms:     dict = {}

    for db_const in db_constraints:
        if db_const.constraint_type == 'LECTURER_AVAILABILITY':
            l_id          = db_const.parameters.get('lecturer_id')
            unavail_slots = db_const.parameters.get('unavailable_slots', [])
            if l_id and unavail_slots:
                if db_const.is_hard:
                    lecturer_hard_unavailables[l_id].update(unavail_slots)
                else:
                    lecturer_soft_unavailables[l_id].append(
                        (db_const.weight, set(unavail_slots))
                    )
        elif db_const.constraint_type == 'ROOM_PREFERENCE':
            c_id       = db_const.parameters.get('course_id')
            pref_rooms = db_const.parameters.get('preferred_rooms', [])
            if c_id and pref_rooms:
                if db_const.is_hard:
                    course_hard_pref_rooms[c_id] = set(pref_rooms)
                else:
                    course_soft_pref_rooms[c_id] = (db_const.weight, set(pref_rooms))

    # Add self-service lecturer unavailability
    for record in LecturerAvailability.objects.filter(
        lecturer__department__faculty__campus__university=university,
        is_available=False,
    ):
        lecturer_hard_unavailables[record.lecturer_id].add(record.time_slot_id)

    # ── 7. Phase 1 — Greedy pre-assignment ────────────────────────────────
    logger.info(f"[Solver] Phase 1: Greedy pre-assignment for {num_courses} courses…")
    t_greedy_start = time.perf_counter()

    greedy_result = _greedy_assign(
        courses, rooms, timeslots,
        course_valid_start_indices,
        course_orig_valid_indices_count,
        course_durations, course_lecturers, course_groups,
        course_campuses, course_group_sizes, course_room_types,
        rooms_by_campus_and_type, rooms_by_campus,
        lab_only_course_ids, course_hard_pref_rooms,
        lecturer_hard_unavailables, ts_id_by_idx,
        room_limit, ctx=ctx,
    )

    greedy_assigned   = len(greedy_result)
    t_greedy_elapsed  = round(time.perf_counter() - t_greedy_start, 3)
    logger.info(
        f"[Solver] Greedy assigned {greedy_assigned}/{num_courses} courses "
        f"in {t_greedy_elapsed}s"
    )

    # ── 8. Fast-path: bypass CP-SAT for 100% greedy or large datasets ─────
    should_bypass_cpsat = (greedy_assigned == num_courses or num_courses > 300)

    if should_bypass_cpsat:
        if num_courses > 300:
            logger.info(
                f"[Solver] Large dataset ({num_courses} courses) — "
                f"skipping CP-SAT, saving greedy results directly."
            )
        else:
            logger.info("[Solver] Greedy solved 100% — skipping CP-SAT.")

        # Second-pass retry: relax daily cap, keep hard conflict rules
        greedy_scheduled_ids = {c_id for (c_id, _r, _t) in greedy_result}
        unscheduled_pass2    = [c for c in courses if c.id not in greedy_scheduled_ids]

        if unscheduled_pass2:
            logger.info(
                f"[Solver] Second-pass retry: {len(unscheduled_pass2)} unscheduled "
                f"courses (relaxed daily cap, hard conflicts still enforced)…"
            )
            room_occ2  = set()
            lec_occ2   = set()
            group_occ2 = set()
            for (gc_id, gr_id, gt_idx) in greedy_result:
                gc_dur = course_durations[gc_id]
                gl_id  = course_lecturers[gc_id]
                gg_id  = course_groups[gc_id]
                for u in range(gt_idx, gt_idx + gc_dur):
                    for ou in overlap_map[u]:
                        room_occ2.add((gr_id, ou))
                        if gl_id:
                            lec_occ2.add((gl_id, ou))
                        group_occ2.add((gg_id, ou))

            retry_placed = 0
            for course2 in unscheduled_pass2:
                c2_id      = course2.id
                dur2       = course_durations[c2_id]
                lec2_id    = course_lecturers[c2_id]
                grp2_id    = course_groups[c2_id]
                campus2_id = course_campuses[c2_id]
                grp2_size  = course_group_sizes[c2_id]
                rtype2     = course_room_types[c2_id]

                elig2 = rooms_by_campus_and_type.get((campus2_id, rtype2), [])
                if not elig2:
                    elig2 = rooms_by_campus.get(campus2_id, [])
                if not elig2:
                    elig2 = [r for r in rooms if r.capacity >= grp2_size]
                elig2 = [r for r in elig2 if r.capacity >= grp2_size]

                placed2 = False
                for t2_idx in course_valid_start_indices[c2_id]:
                    if placed2:
                        break
                    span2 = range(t2_idx, t2_idx + dur2)

                    if lec2_id and lec2_id in lecturer_hard_unavailables:
                        if any(
                            ts_id_by_idx[t2_idx + off] in lecturer_hard_unavailables[lec2_id]
                            for off in range(dur2)
                        ):
                            continue

                    if lec2_id and any(
                        (lec2_id, ou) in lec_occ2
                        for u in span2 for ou in overlap_map[u]
                    ):
                        continue

                    if any(
                        (grp2_id, ou) in group_occ2
                        for u in span2 for ou in overlap_map[u]
                    ):
                        continue

                    for room2 in elig2[:room_limit]:
                        r2_id = room2.id
                        if any(
                            (r2_id, ou) in room_occ2
                            for u in span2 for ou in overlap_map[u]
                        ):
                            continue
                        greedy_result[(c2_id, r2_id, t2_idx)] = 1
                        for u in span2:
                            for ou in overlap_map[u]:
                                room_occ2.add((r2_id, ou))
                                if lec2_id:
                                    lec_occ2.add((lec2_id, ou))
                                group_occ2.add((grp2_id, ou))
                        placed2 = True
                        retry_placed += 1
                        break

            greedy_assigned += retry_placed
            logger.info(
                f"[Solver] Second-pass placed {retry_placed} additional courses. "
                f"Total: {greedy_assigned}/{num_courses}"
            )

        # Sanitize and save
        slots_to_create: list = []
        clean_result, dropped_conflicts = _sanitize_assignments(
            greedy_result.keys(), course_by_id, course_durations,
            course_lecturers, course_groups, overlap_map, ts_id_by_idx,
            timetable_id=timetable_id,
        )
        if dropped_conflicts:
            greedy_assigned -= len(dropped_conflicts)

        with transaction.atomic():
            ScheduleSlot.objects.filter(timetable_id=timetable_id).delete()
            for (c_id, r_id, t_idx) in clean_result:
                course   = course_by_id[c_id]
                duration = course.duration_slots
                for i in range(duration):
                    ts = idx_to_ts[t_idx + i]
                    slots_to_create.append(ScheduleSlot(
                        timetable_id=timetable_id,
                        course_id=course_by_id[c_id].orig_course_id,
                        lecturer_id=course.lecturer_id,
                        room_id=r_id,
                        time_slot_id=ts.id,
                        student_group_id=course.student_group_id,
                    ))
            ScheduleSlot.objects.bulk_create(slots_to_create, batch_size=2000)

        gaps_fixed     = compact_schedule_gaps(timetable, ctx=ctx)
        gap_note       = f" Compacted {gaps_fixed} schedule gaps." if gaps_fixed else ""
        conflict_note  = (
            f" [{len(dropped_conflicts)} unsafe assignment(s) were caught and removed "
            f"before saving — check server logs for details.]"
            if dropped_conflicts else ""
        )
        pct            = round(greedy_assigned / num_courses * 100)
        status_outcome = 'FEASIBLE' if greedy_assigned < num_courses else 'OPTIMAL'
        return (
            status_outcome,
            f"Timetable generated via greedy solver + second-pass retry "
            f"({greedy_assigned}/{num_courses} courses, {pct}%). "
            f"{len(slots_to_create)} slots assigned in {t_greedy_elapsed}s."
            f"{gap_note}{conflict_note}",
            0,
        )

    # ── 9. Phase 2 — CP-SAT (small datasets, greedy missed some courses) ──
    model_data = build_cpsat_model(
        courses=courses,
        rooms=rooms,
        timeslots=timeslots,
        course_valid_start_indices=course_valid_start_indices,
        course_durations=course_durations,
        course_lecturers=course_lecturers,
        course_groups=course_groups,
        course_campuses=course_campuses,
        course_group_sizes=course_group_sizes,
        course_room_types=course_room_types,
        rooms_by_campus_and_type=rooms_by_campus_and_type,
        rooms_by_campus=rooms_by_campus,
        lab_only_course_ids=lab_only_course_ids,
        course_hard_pref_rooms=course_hard_pref_rooms,
        course_soft_pref_rooms=course_soft_pref_rooms,
        lecturer_hard_unavailables=lecturer_hard_unavailables,
        lecturer_soft_unavailables=lecturer_soft_unavailables,
        student_max_per_day=student_max_per_day,
        room_limit=room_limit,
        rule_set=rule_set,
        ctx=ctx,
        lecturers=lecturers,
        student_groups=student_groups,
    )

    model  = model_data['model']
    x      = model_data['x']

    # Defensive fallback (should be unreachable in normal operation — see bypass above)
    if num_courses > 300 and greedy_assigned > 0:
        pct = round(greedy_assigned / num_courses * 100)
        logger.info(
            f"[Solver] Skipping CP-SAT for large dataset ({num_courses} courses). "
            f"Saving greedy result ({greedy_assigned}/{num_courses} = {pct}%) directly."
        )
        return _save_and_compact(
            greedy_result.keys(), course_by_id, course_durations,
            course_lecturers, course_groups, overlap_map, ts_id_by_idx,
            idx_to_ts, timetable_id, timetable, ctx,
            greedy_assigned, num_courses, t_greedy_elapsed, 0,
            prefix="Greedy solver placed",
        )

    status, objective_value, cpsat_triples = run_cpsat_solver(
        model, x, greedy_result, timetable_id, time_limit_seconds, num_courses,
    )

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        clean_result, dropped_conflicts = _sanitize_assignments(
            cpsat_triples, course_by_id, course_durations,
            course_lecturers, course_groups, overlap_map, ts_id_by_idx,
            timetable_id=timetable_id,
        )
        slots_to_create: list = []
        with transaction.atomic():
            ScheduleSlot.objects.filter(timetable_id=timetable_id).delete()
            for (c_id, r_id, t_idx) in clean_result:
                course   = course_by_id[c_id]
                duration = course.duration_slots
                for i in range(duration):
                    ts = idx_to_ts[t_idx + i]
                    slots_to_create.append(ScheduleSlot(
                        timetable_id=timetable_id,
                        course_id=course_by_id[c_id].orig_course_id,
                        lecturer_id=course.lecturer_id,
                        room_id=r_id,
                        time_slot_id=ts.id,
                        student_group_id=course.student_group_id,
                    ))
            ScheduleSlot.objects.bulk_create(slots_to_create, batch_size=2000)

        gaps_fixed = compact_schedule_gaps(timetable, ctx=ctx)
        status_str = 'OPTIMAL' if status == cp_model.OPTIMAL else 'FEASIBLE'

        scheduled_orig_ids  = {s.course_id for s in slots_to_create}
        all_orig_ids        = {c.orig_course_id for c in courses}
        unscheduled_orig_ids = all_orig_ids - scheduled_orig_ids
        unscheduled_note    = ""
        if unscheduled_orig_ids:
            codes = sorted({c.code for c in courses if c.orig_course_id in unscheduled_orig_ids})
            unscheduled_note = (
                f" (Unscheduled: {', '.join(codes[:5])}"
                f"{'...' if len(codes) > 5 else ''})"
            )
        conflict_note = (
            f" [{len(dropped_conflicts)} unsafe assignment(s) caught and removed "
            f"— CP-SAT produced a conflict, investigate.] " if dropped_conflicts else ""
        )
        return (
            status_str,
            f"Timetable scheduled successfully. {len(slots_to_create)} slots assigned."
            f"{unscheduled_note} Objective score: {objective_value}. "
            f"Compacted {gaps_fixed} gaps. "
            f"(Greedy: {greedy_assigned}/{num_courses} pre-assigned in {t_greedy_elapsed}s)"
            f"{conflict_note}",
            objective_value,
        )

    # CP-SAT failed — fall back to greedy
    if greedy_result:
        pct = round(greedy_assigned / num_courses * 100)
        logger.warning(
            f"[Solver] CP-SAT returned {status} — saving greedy result "
            f"({greedy_assigned}/{num_courses} courses = {pct}%)"
        )
        clean_result, dropped_conflicts = _sanitize_assignments(
            greedy_result.keys(), course_by_id, course_durations,
            course_lecturers, course_groups, overlap_map, ts_id_by_idx,
            timetable_id=timetable_id,
        )
        if dropped_conflicts:
            greedy_assigned -= len(dropped_conflicts)
            pct = round(greedy_assigned / num_courses * 100)

        slots_to_create: list = []
        with transaction.atomic():
            ScheduleSlot.objects.filter(timetable_id=timetable_id).delete()
            for (c_id, r_id, t_idx) in clean_result:
                course   = course_by_id[c_id]
                duration = course.duration_slots
                for i in range(duration):
                    ts = idx_to_ts[t_idx + i]
                    slots_to_create.append(ScheduleSlot(
                        timetable_id=timetable_id,
                        course_id=course_by_id[c_id].orig_course_id,
                        lecturer_id=course.lecturer_id,
                        room_id=r_id,
                        time_slot_id=ts.id,
                        student_group_id=course.student_group_id,
                    ))
            ScheduleSlot.objects.bulk_create(slots_to_create, batch_size=2000)

        scheduled_orig_ids   = {s.course_id for s in slots_to_create}
        all_orig_ids         = {c.orig_course_id for c in courses}
        unscheduled_orig_ids = all_orig_ids - scheduled_orig_ids
        unscheduled_note     = ""
        if unscheduled_orig_ids:
            codes = sorted({c.code for c in courses if c.orig_course_id in unscheduled_orig_ids})
            unscheduled_note = (
                f" (Unscheduled: {', '.join(codes[:5])}"
                f"{'...' if len(codes) > 5 else ''})"
            )
        conflict_note = (
            f" [{len(dropped_conflicts)} unsafe assignment(s) caught and removed.]"
            if dropped_conflicts else ""
        )
        gaps_fixed = compact_schedule_gaps(timetable, ctx=ctx)
        return (
            'FEASIBLE',
            f"CP-SAT solver bypassed/failed ({status}). "
            f"Generated via greedy solver + second-pass retry "
            f"({greedy_assigned}/{num_courses} courses, {pct}%).{unscheduled_note} "
            f"{len(slots_to_create)} slots assigned in {t_greedy_elapsed}s."
            f"{conflict_note}",
            0,
        )

    return 'ERROR', f"Solver returned {status} and greedy placed 0 courses. Check data integrity.", None
