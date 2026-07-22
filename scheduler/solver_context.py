"""
solver_context.py — Scheduling Context Layer
=============================================

Holds the lightweight data-transfer objects (CourseObj, RoomObj) and the
SchedulingContext + builder that every phase of the solver shares.

Responsibilities:
  - Define lightweight __slots__ wrappers for DB query results (CourseObj, RoomObj)
  - Build and cache the per-timeslot overlap map (_get_overlap_map)
  - Build the SchedulingContext (build_scheduling_context) — the single object
    computed once per generate_timetable() call and threaded to every phase:
      greedy engine, CP-SAT model builder, gap compactor.
  - Expand DB courses into solver scheduling units (expand_courses_for_scheduling)
  - Pre-flight capacity check (check_scheduling_capacity)

Design pattern: Builder + Value Object.
No Django ORM mutations happen here — this module is read-only with respect to
the database.
"""

from collections import defaultdict
from .solver_rules import CustomRuleSet


# ──────────────────────────────────────────────────────────────────────────────
# Overlap Map In-Memory Cache (self-invalidating based on TimeSlot content)
# ──────────────────────────────────────────────────────────────────────────────

_OVERLAP_MAP_CACHE: dict = {}


def _get_overlap_map(university_id, timeslots):
    """
    Return (and cache) a dict[int, list[int]] where overlap_map[i] is the
    list of slot indices that overlap with slot i on the same day.

    The cache key encodes the full set of timeslot properties so it
    automatically invalidates whenever slots are added/changed/removed.
    Cache is bounded at 500 entries to prevent unbounded memory growth.
    """
    ts_key = (university_id, tuple(
        (ts.id, ts.day_of_week, ts.start_time, ts.end_time)
        for ts in timeslots
    ))
    if ts_key not in _OVERLAP_MAP_CACHE:
        overlap_map: dict = defaultdict(list)
        for i, ts1 in enumerate(timeslots):
            for j, ts2 in enumerate(timeslots):
                if ts1.day_of_week == ts2.day_of_week:
                    if max(ts1.start_time, ts2.start_time) < min(ts1.end_time, ts2.end_time):
                        overlap_map[i].append(j)
        if len(_OVERLAP_MAP_CACHE) > 500:
            _OVERLAP_MAP_CACHE.clear()
        _OVERLAP_MAP_CACHE[ts_key] = overlap_map
    return _OVERLAP_MAP_CACHE[ts_key]


# ──────────────────────────────────────────────────────────────────────────────
# Lightweight Value Objects (avoid Django model overhead in hot loops)
# ──────────────────────────────────────────────────────────────────────────────

class CourseObj:
    """Lightweight proxy for a Course DB row; populated from a .values() dict."""
    __slots__ = (
        'id', 'code', 'name', 'duration_slots', 'required_room_type',
        'lecturer_id', 'student_group_id', 'campus_id', 'group_size',
        'orig_duration', 'orig_course_id', 'session_index',
    )

    def __init__(self, d: dict):
        self.id                = d['id']
        self.code              = d['code']
        self.name              = d['name']
        self.duration_slots    = d['duration_slots']
        self.required_room_type = d['required_room_type']
        self.lecturer_id       = d['lecturer_id']
        self.student_group_id  = d['student_group_id']
        self.campus_id         = d['program__department__faculty__campus_id']
        self.group_size        = d['student_group__size']
        self.orig_course_id    = d.get('orig_course_id', d['id'])
        self.session_index     = d.get('session_index', 0)
        self.orig_duration     = d.get('orig_duration', d['duration_slots'])


class RoomObj:
    """Lightweight proxy for a Room DB row; populated from a .values() dict."""
    __slots__ = ('id', 'name', 'capacity', 'room_type', 'campus_id', 'is_virtual')

    def __init__(self, d: dict):
        self.id         = d['id']
        self.name       = d['name']
        self.capacity   = d['capacity']
        self.room_type  = d['room_type']
        self.campus_id  = d['campus_id']
        self.is_virtual = d.get('is_virtual', False)


# ──────────────────────────────────────────────────────────────────────────────
# Virtual ID counter (module-level so it can be reset before each solve)
# ──────────────────────────────────────────────────────────────────────────────

_VIRTUAL_ID_COUNTER = [10_000_000]


# ──────────────────────────────────────────────────────────────────────────────
# SchedulingContext — computed ONCE per generate_timetable() call
# ──────────────────────────────────────────────────────────────────────────────

class SchedulingContext:
    """
    Immutable bag of pre-computed, per-university scheduling data.

    Computed once by build_scheduling_context() and passed to every phase
    of the solver pipeline (greedy, CP-SAT, gap compactor) so that
    expensive DB queries and O(T²) overlap-map construction are never
    duplicated within a single generation run.
    """
    __slots__ = (
        'overlap_map', 'ts_to_idx', 'idx_to_ts', 'ts_id_by_idx', 'ts_day_by_idx',
        'ts_is_evening_by_idx', 'ts_pos_in_day', 'ts_by_day_pos', 'timeslots_by_day',
        'room_features_map', 'course_required_features_map', 'course_additional_groups_map',
        'room_building_map', 'building_distances',
        'lecturer_preferences_prefer', 'lecturer_preferences_dislike',
        'lecturer_max_slots_per_day', 'lecturer_max_consec',
        'group_parent_map', 'group_children_map', 'group_conflict_sets',
        'db_constraints', 'group_sizes',
        # Mirrored from rule_set for external compatibility
        'forbidden_lecturer_rooms', 'forbidden_course_rooms', 'forbidden_group_rooms',
        'forbidden_lecturer_times', 'forbidden_course_times', 'forbidden_group_times',
        'required_lecturer_rooms', 'required_course_rooms', 'required_group_rooms',
        'required_lecturer_times', 'required_course_times', 'required_group_times',
        'rule_set',
    )


def build_scheduling_context(university, timeslots, db_constraints=None) -> SchedulingContext:
    """
    Build a SchedulingContext for *university* given *timeslots* and
    *db_constraints*.

    When db_constraints is None, they are fetched from the DB here.
    When supplied by the caller (as in generate_timetable which loads them
    early for capacity checks) the query is skipped.

    All data that was previously re-queried independently in _greedy_assign(),
    the CP-SAT builder, and compact_schedule_gaps() is now computed once here
    and shared via the returned context object.
    """
    from .models import (
        Constraint, StudentGroup, Room, Course, BuildingDistance,
        Lecturer, LecturerTimeSlotPreference,
    )

    ctx = SchedulingContext()

    # ---- Constraints ----
    if db_constraints is None:
        db_constraints = list(Constraint.objects.filter(university=university))
    ctx.db_constraints = db_constraints

    # ---- Overlap map: the O(T²) pass — built exactly once ----
    ctx.overlap_map = _get_overlap_map(university.id, timeslots)

    # ---- Timeslot index maps ----
    ctx.ts_to_idx          = {ts.id:  idx for idx, ts in enumerate(timeslots)}
    ctx.idx_to_ts          = {idx:    ts  for idx, ts in enumerate(timeslots)}
    ctx.ts_id_by_idx       = {idx:    ts.id              for idx, ts in enumerate(timeslots)}
    ctx.ts_day_by_idx      = {idx:    ts.day_of_week     for idx, ts in enumerate(timeslots)}
    ctx.ts_is_evening_by_idx = {idx:  ts.is_evening      for idx, ts in enumerate(timeslots)}

    timeslots_by_day: dict = defaultdict(list)
    for ts in timeslots:
        timeslots_by_day[ts.day_of_week].append(ts)
    for day in timeslots_by_day:
        timeslots_by_day[day] = sorted(timeslots_by_day[day], key=lambda x: x.slot_number)
    ctx.timeslots_by_day = timeslots_by_day

    ts_pos_in_day: dict = {}
    ts_by_day_pos: dict = {}
    for day, day_slots in timeslots_by_day.items():
        for pos, ts in enumerate(day_slots):
            ts_pos_in_day[ts.id] = pos
            ts_by_day_pos[(day, ts.slot_number)] = ts
    ctx.ts_pos_in_day = ts_pos_in_day
    ctx.ts_by_day_pos = ts_by_day_pos

    # ---- Feature / building / distance / preference maps: ONE set of queries ----
    room_features_map: dict = defaultdict(set)
    for room_id, feature_id in Room.features.through.objects.filter(
        room__campus__university_id=university.id
    ).values_list('room_id', 'roomfeature_id'):
        room_features_map[room_id].add(feature_id)
    ctx.room_features_map = room_features_map

    course_required_features_map: dict = defaultdict(set)
    for course_id, feature_id in Course.required_features.through.objects.filter(
        course__program__department__faculty__campus__university_id=university.id
    ).values_list('course_id', 'roomfeature_id'):
        course_required_features_map[course_id].add(feature_id)
    ctx.course_required_features_map = course_required_features_map

    course_additional_groups_map: dict = defaultdict(set)
    for course_id, group_id in Course.additional_student_groups.through.objects.filter(
        course__program__department__faculty__campus__university_id=university.id
    ).values_list('course_id', 'studentgroup_id'):
        course_additional_groups_map[course_id].add(group_id)
    ctx.course_additional_groups_map = course_additional_groups_map

    ctx.room_building_map = {
        r_id: b_id for r_id, b_id in Room.objects.filter(
            campus__university_id=university.id
        ).values_list('id', 'building_id')
    }

    building_distances: dict = {}
    for b1, b2, time_min in BuildingDistance.objects.filter(
        from_building__campus__university_id=university.id
    ).values_list('from_building_id', 'to_building_id', 'walking_time_minutes'):
        building_distances[(b1, b2)] = time_min
    ctx.building_distances = building_distances

    lecturer_preferences_prefer:  dict = defaultdict(set)
    lecturer_preferences_dislike: dict = defaultdict(set)
    for l_id, ts_id, pref in LecturerTimeSlotPreference.objects.filter(
        lecturer__department__faculty__campus__university_id=university.id
    ).values_list('lecturer_id', 'time_slot_id', 'preference_level'):
        if pref == 'prefer':
            lecturer_preferences_prefer[l_id].add(ts_id)
        elif pref == 'dislike':
            lecturer_preferences_dislike[l_id].add(ts_id)
    ctx.lecturer_preferences_prefer  = lecturer_preferences_prefer
    ctx.lecturer_preferences_dislike = lecturer_preferences_dislike

    ctx.lecturer_max_slots_per_day = {
        l_id: max_slots for l_id, max_slots in Lecturer.objects.filter(
            department__faculty__campus__university_id=university.id
        ).values_list('id', 'max_slots_per_day')
    }

    lecturer_max_consec: dict = {}
    for c in db_constraints:
        if c.constraint_type == 'LECTURER_MAX_CONSECUTIVE_SLOTS' and c.is_hard:
            l_id  = c.parameters.get('lecturer_id')
            p_max = c.parameters.get('max_consecutive')
            if l_id and p_max is not None:
                lecturer_max_consec[int(l_id)] = int(p_max)
    ctx.lecturer_max_consec = lecturer_max_consec

    # ---- Group hierarchy ----
    group_parent_map:   dict = {}
    group_children_map: dict = defaultdict(list)
    group_conflict_sets: dict = defaultdict(set)
    group_sizes: dict = {}
    for g in StudentGroup.objects.filter(
        program__department__faculty__campus__university=university
    ).values('id', 'parent_group_id', 'size'):
        g_id      = g['id']
        parent_id = g['parent_group_id']
        group_sizes[g_id]   = g['size']
        group_parent_map[g_id] = parent_id
        if parent_id:
            group_children_map[parent_id].append(g_id)
        group_conflict_sets[g_id].add(g_id)
        if parent_id:
            group_conflict_sets[g_id].add(parent_id)
            group_conflict_sets[parent_id].add(g_id)
    ctx.group_parent_map    = group_parent_map
    ctx.group_children_map  = group_children_map
    ctx.group_conflict_sets = group_conflict_sets
    ctx.group_sizes         = group_sizes

    # ---- Custom constraint rules ----
    rule_set = CustomRuleSet.from_constraints(db_constraints)

    # ---- Mode of Study Constraints (FT, PT, WKD) ----
    group_names_map = {
        g['id']: g['name'] for g in StudentGroup.objects.filter(
            program__department__faculty__campus__university=university
        ).values('id', 'name')
    }
    for g_id, g_name in group_names_map.items():
        g_name_upper = g_name.upper()
        if ' FT' in g_name_upper or '-FT' in g_name_upper or ' FT-' in g_name_upper:
            # Full-Time: forbidden in evening slots (is_evening) and weekends (Days 6 & 7)
            for ts in timeslots:
                if ts.is_evening or ts.day_of_week in (6, 7):
                    rule_set.forbidden_group_times[g_id].add(ts.id)
        elif ' PT' in g_name_upper or '-PT' in g_name_upper or ' PT-' in g_name_upper:
            # Part-Time: forbidden in weekday daytime slots (Days 1..5, not is_evening)
            for ts in timeslots:
                if ts.day_of_week in (1, 2, 3, 4, 5) and not ts.is_evening:
                    rule_set.forbidden_group_times[g_id].add(ts.id)
        elif ' WKD' in g_name_upper or 'WEEKEND' in g_name_upper or '-WKD' in g_name_upper:
            # Weekend: forbidden on weekdays (Days 1..5)
            for ts in timeslots:
                if ts.day_of_week in (1, 2, 3, 4, 5):
                    rule_set.forbidden_group_times[g_id].add(ts.id)

    ctx.rule_set = rule_set

    # Mirror rule_set attributes onto ctx for external compatibility
    ctx.forbidden_lecturer_rooms = rule_set.forbidden_lecturer_rooms
    ctx.forbidden_course_rooms   = rule_set.forbidden_course_rooms
    ctx.forbidden_group_rooms    = rule_set.forbidden_group_rooms
    ctx.forbidden_lecturer_times = rule_set.forbidden_lecturer_times
    ctx.forbidden_course_times   = rule_set.forbidden_course_times
    ctx.forbidden_group_times    = rule_set.forbidden_group_times
    ctx.required_lecturer_rooms  = rule_set.required_lecturer_rooms
    ctx.required_course_rooms    = rule_set.required_course_rooms
    ctx.required_group_rooms     = rule_set.required_group_rooms
    ctx.required_lecturer_times  = rule_set.required_lecturer_times
    ctx.required_course_times    = rule_set.required_course_times
    ctx.required_group_times     = rule_set.required_group_times

    return ctx


# ──────────────────────────────────────────────────────────────────────────────
# Course Expansion
# ──────────────────────────────────────────────────────────────────────────────

def expand_courses_for_scheduling(courses_raw: list) -> list:
    """
    Expand DB courses into solver scheduling units.

    Two expansion rules apply:
      1. sessions_per_week > 1 → each session is scheduled as an independent unit.
      2. Lecture/Seminar with duration_slots > 1 → split into consecutive 1-slot pieces
         (so the solver can pack them on the same day, same room).

    All virtual/expanded units get a fresh ID from _VIRTUAL_ID_COUNTER and
    carry their parent's `orig_course_id` so the final save step can write
    the correct FK to the original Course row.
    """
    expanded = []
    for c in courses_raw:
        base_id   = c['id']
        sessions  = max(1, c.get('sessions_per_week') or 1)
        duration  = c['duration_slots']
        room_type = c['required_room_type']

        for session_idx in range(sessions):
            if room_type in ('Lecture', 'Seminar') and duration > 1:
                for _sub_idx in range(duration):
                    piece = c.copy()
                    piece['orig_course_id'] = base_id
                    piece['session_index']  = session_idx
                    piece['duration_slots'] = 1
                    piece['orig_duration']  = duration
                    piece['id']             = _VIRTUAL_ID_COUNTER[0]
                    _VIRTUAL_ID_COUNTER[0] += 1
                    expanded.append(piece)
            else:
                piece = c.copy()
                piece['orig_course_id'] = base_id
                piece['session_index']  = session_idx
                piece['orig_duration']  = duration
                if sessions > 1:
                    piece['id'] = _VIRTUAL_ID_COUNTER[0]
                    _VIRTUAL_ID_COUNTER[0] += 1
                expanded.append(piece)
    return expanded


# ──────────────────────────────────────────────────────────────────────────────
# Pre-flight Capacity Check
# ──────────────────────────────────────────────────────────────────────────────

def check_scheduling_capacity(courses: list, rooms: list, timeslots: list) -> dict:
    """
    Pre-flight sanity check that catches configurations which are
    mathematically impossible to fully schedule.

    Returns a dict with keys:
      'ok'       → bool (False if any hard error was found)
      'warnings' → list of warning strings
      'errors'   → list of error strings
    """
    warnings_list = []
    errors_list   = []

    total_timeslots        = len(timeslots)
    total_rooms            = len(rooms)
    total_room_slot_capacity = total_timeslots * total_rooms
    total_demand_slot_units  = sum(c.duration_slots for c in courses)

    if total_rooms == 0:
        errors_list.append("No rooms are configured.")
    elif total_timeslots == 0:
        errors_list.append("No time slots are configured.")
    elif total_demand_slot_units > total_room_slot_capacity:
        errors_list.append(
            f"Total course demand ({total_demand_slot_units} slot-units) "
            f"exceeds total room-slot capacity ({total_room_slot_capacity})."
        )
    elif total_demand_slot_units > total_room_slot_capacity * 0.85:
        warnings_list.append("Room utilisation will be very high (>85%).")

    # Per-group overload check
    group_demand:        dict = defaultdict(int)
    group_course_counts: dict = defaultdict(int)
    for c in courses:
        group_demand[c.student_group_id]        += c.duration_slots * getattr(c, 'sessions_per_week', 1)
        group_course_counts[c.student_group_id] += 1
    overloaded_groups = [
        (gid, d, group_course_counts[gid])
        for gid, d in group_demand.items() if d > total_timeslots
    ]
    if overloaded_groups:
        errors_list.append(
            f"{len(overloaded_groups)} student group(s) have more weekly "
            f"course demand than time slots exist."
        )

    # Per-lecturer overload check
    lecturer_demand: dict = defaultdict(int)
    for c in courses:
        if c.lecturer_id:
            lecturer_demand[c.lecturer_id] += c.duration_slots * getattr(c, 'sessions_per_week', 1)
    overloaded_lecturers = [
        (lid, d) for lid, d in lecturer_demand.items() if d > total_timeslots
    ]
    if overloaded_lecturers:
        errors_list.append(
            f"{len(overloaded_lecturers)} lecturer(s) are assigned more "
            f"weekly teaching load than time slots exist."
        )

    return {
        'ok':       len(errors_list) == 0,
        'warnings': warnings_list,
        'errors':   errors_list,
    }
