"""
solver_sanitizer.py — Pre-Write Conflict Sanitizer
====================================================

Last-line-of-defence pass that walks a proposed set of
(course_id, room_id, t_idx) assignments immediately before they are
persisted to the database and removes any that would create a hard conflict.

Both the greedy heuristic and the CP-SAT model are *supposed* to guarantee
no double-bookings, but production GenerationLog history has occasionally
shown conflicts surviving to the saved schedule (e.g. from the relaxed-cap
second-pass retry).  Rather than silently writing a conflicting schedule —
or, now that ScheduleSlot has DB-level UniqueConstraints, crashing the whole
bulk_create with an IntegrityError — this function walks the assignments in a
stable deterministic order, accepts the first claimant on every slot, and drops
later ones that clash, logging exactly what was dropped.

Design pattern: Pipeline Filter (Chain of Responsibility variant).
"""

import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


def sanitize_assignments(
    assignment_triples,
    course_by_id: dict,
    course_durations: dict,
    course_lecturers: dict,
    course_groups: dict,
    overlap_map: dict,
    ts_id_by_idx: dict,
    timetable_id=None,
):
    """
    Walk *assignment_triples* in a stable order and drop any triple whose
    course, lecturer, or student-group would conflict with a previously
    accepted triple.

    Args:
        assignment_triples: iterable of (course_id, room_id, t_idx) tuples.
        course_by_id:      dict[int, CourseObj]
        course_durations:  dict[int, int]
        course_lecturers:  dict[int, int | None]
        course_groups:     dict[int, int]
        overlap_map:       dict[int, list[int]]  (slot index → overlapping indices)
        ts_id_by_idx:      dict[int, int]         (slot index → DB timeslot id)
        timetable_id:      optional, used only for log messages.

    Returns:
        (clean_triples: list, dropped: list[str])
        where *dropped* is a list of human-readable conflict descriptions.
    """
    room_occ:  set = set()   # (room_id,      overlap_unit) already taken
    lec_occ:   set = set()   # (lecturer_id,  overlap_unit) already taken
    group_occ: set = set()   # (group_id,     overlap_unit) already taken

    clean:   list = []
    dropped: list = []

    # Stable ordering: sort by (t_idx, room_id, course_id) so results are
    # deterministic across runs and independent of dict iteration order.
    for (c_id, r_id, t_idx) in sorted(
        assignment_triples, key=lambda k: (k[2], k[1], k[0])
    ):
        course = course_by_id.get(c_id)
        if course is None:
            continue

        duration = course_durations.get(c_id, course.duration_slots)
        lec_id   = course_lecturers.get(c_id, course.lecturer_id)
        grp_id   = course_groups.get(c_id, course.student_group_id)

        span_units     = range(t_idx, t_idx + duration)
        conflict_reason = None

        for u in span_units:
            for ou in overlap_map.get(u, (u,)):
                if (r_id, ou) in room_occ:
                    conflict_reason = f"room {r_id} already booked at timeslot unit {ou}"
                    break
                if lec_id and (lec_id, ou) in lec_occ:
                    conflict_reason = f"lecturer {lec_id} already booked at timeslot unit {ou}"
                    break
                if grp_id and (grp_id, ou) in group_occ:
                    conflict_reason = f"student group {grp_id} already booked at timeslot unit {ou}"
                    break
            if conflict_reason:
                break

        if conflict_reason:
            msg = (
                f"[Solver] DROPPED conflicting assignment "
                f"course={course.code} (id={c_id}) room={r_id} t_idx={t_idx} "
                f"timetable={timetable_id}: {conflict_reason}"
            )
            logger.error(msg)
            dropped.append(msg)
            continue

        # Accept: mark all overlapping units as occupied
        for u in span_units:
            for ou in overlap_map.get(u, (u,)):
                room_occ.add((r_id, ou))
                if lec_id:
                    lec_occ.add((lec_id, ou))
                if grp_id:
                    group_occ.add((grp_id, ou))

        clean.append((c_id, r_id, t_idx))

    if dropped:
        logger.error(
            f"[Solver] Pre-write sanitizer dropped {len(dropped)} conflicting "
            f"assignment(s) for timetable {timetable_id} — see preceding log lines."
        )

    return clean, dropped
