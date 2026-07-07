import time
import logging
import multiprocessing
from collections import defaultdict
from ortools.sat.python import cp_model
from django.db import transaction
from .models import Timetable, ScheduleSlot, Course, Lecturer, StudentGroup, Room, TimeSlot, Constraint, LecturerAvailability

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Overlap Map In-Memory Cache (Self-Invalidating based on TimeSlots' content)
# ──────────────────────────────────────────────────────────────────────────────
_OVERLAP_MAP_CACHE = {}

def _get_overlap_map(university_id, timeslots):
    ts_key = (university_id, tuple((ts.id, ts.day_of_week, ts.start_time, ts.end_time) for ts in timeslots))
    if ts_key not in _OVERLAP_MAP_CACHE:
        overlap_map = defaultdict(list)
        for i, ts1 in enumerate(timeslots):
            for j, ts2 in enumerate(timeslots):
                if ts1.day_of_week == ts2.day_of_week:
                    if max(ts1.start_time, ts2.start_time) < min(ts1.end_time, ts2.end_time):
                        overlap_map[i].append(j)
        # Limit cache size to prevent memory growth
        if len(_OVERLAP_MAP_CACHE) > 500:
            _OVERLAP_MAP_CACHE.clear()
        _OVERLAP_MAP_CACHE[ts_key] = overlap_map
    return _OVERLAP_MAP_CACHE[ts_key]



# ──────────────────────────────────────────────────────────────────────────────
# Lightweight Objects for Memory and Speed Optimization
# ──────────────────────────────────────────────────────────────────────────────

class CourseObj:
    __slots__ = ('id', 'code', 'name', 'duration_slots', 'required_room_type',
                 'lecturer_id', 'student_group_id', 'campus_id', 'group_size',
                 'orig_duration', 'orig_course_id', 'session_index')
    def __init__(self, d):
        self.id = d['id']
        self.code = d['code']
        self.name = d['name']
        self.duration_slots = d['duration_slots']
        self.required_room_type = d['required_room_type']
        self.lecturer_id = d['lecturer_id']
        self.student_group_id = d['student_group_id']
        self.campus_id = d['program__department__faculty__campus_id']
        self.group_size = d['student_group__size']
        self.orig_course_id = d.get('orig_course_id', d['id'])
        self.session_index = d.get('session_index', 0)
        self.orig_duration = d.get('orig_duration', d['duration_slots'])

class RoomObj:
    __slots__ = ('id', 'name', 'capacity', 'room_type', 'campus_id')
    def __init__(self, d):
        self.id = d['id']
        self.name = d['name']
        self.capacity = d['capacity']
        self.room_type = d['room_type']
        self.campus_id = d['campus_id']


_VIRTUAL_ID_COUNTER = [10_000_000]


# ──────────────────────────────────────────────────────────────────────────────
# Shared Scheduling Context — computed ONCE per generate_timetable() call and
# reused by _greedy_assign(), the CP-SAT phase, and compact_schedule_gaps().
# Avoids rebuilding overlap_map (O(T^2)) and re-querying feature/building/
# preference data three separate times per generation run.
# ──────────────────────────────────────────────────────────────────────────────

class SchedulingContext:
    __slots__ = (
        'overlap_map', 'ts_to_idx', 'idx_to_ts', 'ts_id_by_idx', 'ts_day_by_idx',
        'ts_is_evening_by_idx', 'ts_pos_in_day', 'ts_by_day_pos', 'timeslots_by_day',
        'room_features_map', 'course_required_features_map', 'course_additional_groups_map',
        'room_building_map', 'building_distances',
        'lecturer_preferences_prefer', 'lecturer_preferences_dislike',
        'lecturer_max_slots_per_day', 'lecturer_max_consec',
        'group_parent_map', 'group_children_map', 'group_conflict_sets',
        'db_constraints',
    )


def build_scheduling_context(university, timeslots, db_constraints=None):
    """
    Computes every piece of per-university/per-timetable data that
    _greedy_assign(), the CP-SAT model builder, and compact_schedule_gaps()
    would otherwise each compute independently. Build once, pass everywhere.
    """
    from .models import (
        Constraint, StudentGroup, Room, Course, BuildingDistance,
        Lecturer, LecturerTimeSlotPreference,
    )

    ctx = SchedulingContext()

    if db_constraints is None:
        db_constraints = list(Constraint.objects.filter(university=university))
    ctx.db_constraints = db_constraints

    # ---- overlap_map: the O(T^2) pass — built exactly once ----
    ctx.overlap_map = _get_overlap_map(university.id, timeslots)

    ctx.ts_to_idx = {ts.id: idx for idx, ts in enumerate(timeslots)}
    ctx.idx_to_ts = {idx: ts for idx, ts in enumerate(timeslots)}
    ctx.ts_id_by_idx = {idx: ts.id for idx, ts in enumerate(timeslots)}
    ctx.ts_day_by_idx = {idx: ts.day_of_week for idx, ts in enumerate(timeslots)}
    ctx.ts_is_evening_by_idx = {idx: ts.is_evening for idx, ts in enumerate(timeslots)}

    timeslots_by_day = defaultdict(list)
    for ts in timeslots:
        timeslots_by_day[ts.day_of_week].append(ts)
    for day in timeslots_by_day:
        timeslots_by_day[day] = sorted(timeslots_by_day[day], key=lambda x: x.slot_number)
    ctx.timeslots_by_day = timeslots_by_day

    ts_pos_in_day = {}
    ts_by_day_pos = {}
    for day, day_slots in timeslots_by_day.items():
        for pos, ts in enumerate(day_slots):
            ts_pos_in_day[ts.id] = pos
            ts_by_day_pos[(day, ts.slot_number)] = ts
    ctx.ts_pos_in_day = ts_pos_in_day
    ctx.ts_by_day_pos = ts_by_day_pos

    # ---- feature / building / distance / preference maps: ONE set of queries ----
    room_features_map = defaultdict(set)
    for room_id, feature_id in Room.features.through.objects.filter(
        room__campus__university_id=university.id
    ).values_list('room_id', 'roomfeature_id'):
        room_features_map[room_id].add(feature_id)
    ctx.room_features_map = room_features_map

    course_required_features_map = defaultdict(set)
    for course_id, feature_id in Course.required_features.through.objects.filter(
        course__program__department__faculty__campus__university_id=university.id
    ).values_list('course_id', 'roomfeature_id'):
        course_required_features_map[course_id].add(feature_id)
    ctx.course_required_features_map = course_required_features_map

    course_additional_groups_map = defaultdict(set)
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

    building_distances = {}
    for b1, b2, time_min in BuildingDistance.objects.filter(
        from_building__campus__university_id=university.id
    ).values_list('from_building_id', 'to_building_id', 'walking_time_minutes'):
        building_distances[(b1, b2)] = time_min
    ctx.building_distances = building_distances

    lecturer_preferences_prefer = defaultdict(set)
    lecturer_preferences_dislike = defaultdict(set)
    for l_id, ts_id, pref in LecturerTimeSlotPreference.objects.filter(
        lecturer__department__faculty__campus__university_id=university.id
    ).values_list('lecturer_id', 'time_slot_id', 'preference_level'):
        if pref == 'prefer':
            lecturer_preferences_prefer[l_id].add(ts_id)
        elif pref == 'dislike':
            lecturer_preferences_dislike[l_id].add(ts_id)
    ctx.lecturer_preferences_prefer = lecturer_preferences_prefer
    ctx.lecturer_preferences_dislike = lecturer_preferences_dislike

    ctx.lecturer_max_slots_per_day = {
        l_id: max_slots for l_id, max_slots in Lecturer.objects.filter(
            department__faculty__campus__university_id=university.id
        ).values_list('id', 'max_slots_per_day')
    }

    lecturer_max_consec = {}
    for c in db_constraints:
        if c.constraint_type == 'LECTURER_MAX_CONSECUTIVE_SLOTS' and c.is_hard:
            l_id = c.parameters.get('lecturer_id')
            p_max = c.parameters.get('max_consecutive')
            if l_id and p_max is not None:
                lecturer_max_consec[int(l_id)] = int(p_max)
    ctx.lecturer_max_consec = lecturer_max_consec

    group_parent_map = {}
    group_children_map = defaultdict(list)
    group_conflict_sets = defaultdict(set)
    for g in StudentGroup.objects.filter(
        program__department__faculty__campus__university=university
    ).values('id', 'parent_group_id'):
        g_id = g['id']
        parent_id = g['parent_group_id']
        group_parent_map[g_id] = parent_id
        if parent_id:
            group_children_map[parent_id].append(g_id)
        group_conflict_sets[g_id].add(g_id)
        if parent_id:
            group_conflict_sets[g_id].add(parent_id)
            group_conflict_sets[parent_id].add(g_id)
    ctx.group_parent_map = group_parent_map
    ctx.group_children_map = group_children_map
    ctx.group_conflict_sets = group_conflict_sets

    return ctx


def expand_courses_for_scheduling(courses_raw):
    """
    Expand DB courses into solver scheduling units:
      - sessions_per_week: each session is scheduled independently
      - duration_slots > 1 for Lecture/Seminar: split into consecutive 1-slot pieces
    """
    expanded = []
    for c in courses_raw:
        base_id = c['id']
        sessions = max(1, c.get('sessions_per_week') or 1)
        duration = c['duration_slots']
        room_type = c['required_room_type']

        for session_idx in range(sessions):
            if room_type in ('Lecture', 'Seminar') and duration > 1:
                for _sub_idx in range(duration):
                    piece = c.copy()
                    piece['orig_course_id'] = base_id
                    piece['session_index'] = session_idx
                    piece['duration_slots'] = 1
                    piece['orig_duration'] = duration
                    piece['id'] = _VIRTUAL_ID_COUNTER[0]
                    _VIRTUAL_ID_COUNTER[0] += 1
                    expanded.append(piece)
            else:
                piece = c.copy()
                piece['orig_course_id'] = base_id
                piece['session_index'] = session_idx
                piece['orig_duration'] = duration
                if sessions > 1:
                    piece['id'] = _VIRTUAL_ID_COUNTER[0]
                    _VIRTUAL_ID_COUNTER[0] += 1
                expanded.append(piece)
    return expanded


# ──────────────────────────────────────────────────────────────────────────────
# Post-Generation Gap Compaction
# ──────────────────────────────────────────────────────────────────────────────

def compact_schedule_gaps(timetable, ctx=None):
    """
    Post-processing pass that eliminates student schedule gaps.

    Strategy (local search):
      For each student group, on each day that has gaps:
        - Find the 'gapped' course (one that has empty slots before it)
        - Try to move it to the earliest free slot on that day
        - A move is valid if: room is free, lecturer is free at new slot
      Repeat until no improvements found (max 5 passes).

    Args:
        timetable: the Timetable being compacted.
        ctx: optional SchedulingContext (from build_scheduling_context()).
             When provided, all feature/building/preference/overlap maps are
             reused instead of being re-queried and rebuilt from scratch.

    Returns: number of gaps fixed.
    """
    from collections import defaultdict
    from .models import Constraint, StudentGroup, Room, Course, RoomFeature, Building, BuildingDistance, Lecturer, TimeSlot

    # Skip gap compaction for very large timetables to avoid memory and CPU bottlenecks
    uni = timetable.semester.university
    course_count = Course.objects.filter(
        program__department__faculty__campus__university=uni,
        lecturer__isnull=False,
        lecturer__is_active=True,
        student_group__isnull=False
    ).count()
    if course_count > 5000:
        logger.info(f"[Compaction] Timetable has {course_count} courses (>5000) — skipping compaction.")
        return 0

    slots = list(
        ScheduleSlot.objects.filter(timetable=timetable)
        .select_related('time_slot', 'room', 'lecturer', 'student_group', 'course')
        .order_by('student_group_id', 'time_slot__day_of_week', 'time_slot__slot_number')
    )

    if not slots:
        return 0

    if ctx is None:
        # Standalone call (no shared context passed in) — build everything
        # locally, same as before. Keeps compact_schedule_gaps() usable on its own.
        all_ts = list(TimeSlot.objects.filter(
            university=uni
        ).order_by('day_of_week', 'slot_number'))
        ctx = build_scheduling_context(uni, all_ts)

    ts_by_day_pos = ctx.ts_by_day_pos
    ts_to_idx = ctx.ts_to_idx
    idx_to_ts = ctx.idx_to_ts
    overlap_map = ctx.overlap_map
    timeslots_by_day = ctx.timeslots_by_day
    group_parent_map = ctx.group_parent_map
    group_children_map = ctx.group_children_map
    group_conflict_sets = ctx.group_conflict_sets
    lecturer_max_consec = ctx.lecturer_max_consec
    room_features_map = ctx.room_features_map
    course_required_features_map = ctx.course_required_features_map
    course_additional_groups_map = ctx.course_additional_groups_map
    room_building_map = ctx.room_building_map
    building_distances = ctx.building_distances
    lecturer_max_slots_per_day = ctx.lecturer_max_slots_per_day

    # Pre-build occupation sets for fast conflict checking
    # (room_id, ts_id) -> slot
    room_ts_map = {(s.room_id, s.time_slot_id): s for s in slots}
    # (lecturer_id, ts_id) -> slot
    lec_ts_map  = {(s.lecturer_id, s.time_slot_id): s for s in slots}

    # Map slot ID -> set of occupied student groups
    slot_occupied_groups = {}
    for s in slots:
        groups = {s.student_group_id}
        for g_add in course_additional_groups_map.get(s.course_id, set()):
            groups.add(g_add)
        expanded = set()
        for g in groups:
            expanded.update(group_conflict_sets.get(g, {g}))
        slot_occupied_groups[s.id] = expanded

    # (group_id, ts_id) -> slot
    grp_ts_map = {}
    for s in slots:
        for g in slot_occupied_groups[s.id]:
            grp_ts_map[(g, s.time_slot_id)] = s

    def is_move_valid(slot, target_ts):
        target_ts_id = target_ts.id
        target_ts_idx = ts_to_idx[target_ts_id]
        target_slot_num = target_ts.slot_number
        day = target_ts.day_of_week
        l_id = slot.lecturer_id
        r_id = slot.room_id
        campus_id = slot.room.campus_id

        # Check Room Features
        req_feats = course_required_features_map.get(slot.course_id, set())
        if req_feats:
            if not req_feats.issubset(room_features_map.get(r_id, set())):
                return False

        # Real-time overlap check for all overlapping timeslots at target position
        for ou_idx in overlap_map[target_ts_idx]:
            ou_ts = idx_to_ts[ou_idx]
            ou_ts_id = ou_ts.id

            # Room free?
            if (r_id, ou_ts_id) in room_ts_map:
                s = room_ts_map[(r_id, ou_ts_id)]
                if s.id != slot.id:
                    return False

            # Lecturer free?
            if l_id and (l_id, ou_ts_id) in lec_ts_map:
                s = lec_ts_map[(l_id, ou_ts_id)]
                if s.id != slot.id:
                    return False
            
            # Student group free (including parent-child and shared electives)?
            for rg in slot_occupied_groups[slot.id]:
                if (rg, ou_ts_id) in grp_ts_map:
                    s = grp_ts_map[(rg, ou_ts_id)]
                    if s.id != slot.id:
                        return False

        prev_ts = ts_by_day_pos.get((day, target_slot_num - 1))
        next_ts = ts_by_day_pos.get((day, target_slot_num + 1))

        # Campus Travel check for Lecturer
        if l_id:
            for adj_ts in (prev_ts, next_ts):
                if adj_ts and (l_id, adj_ts.id) in lec_ts_map:
                    s = lec_ts_map[(l_id, adj_ts.id)]
                    if s.id != slot.id and s.room.campus_id != campus_id:
                        return False

        # Building Travel check for Lecturer
        if l_id and r_id in room_building_map:
            cur_b = room_building_map[r_id]
            for adj_ts in (prev_ts, next_ts):
                if adj_ts and (l_id, adj_ts.id) in lec_ts_map:
                    s = lec_ts_map[(l_id, adj_ts.id)]
                    if s.id != slot.id and s.room_id in room_building_map:
                        other_b = room_building_map[s.room_id]
                        if other_b != cur_b:
                            walk_time = max(building_distances.get((cur_b, other_b), 0), building_distances.get((other_b, cur_b), 0))
                            if walk_time > 15:
                                return False

        # Building Travel check for Student Groups (primary and expanded)
        for rg in slot_occupied_groups[slot.id]:
            for adj_ts in (prev_ts, next_ts):
                if adj_ts and (rg, adj_ts.id) in grp_ts_map:
                    s = grp_ts_map[(rg, adj_ts.id)]
                    if s.id != slot.id and s.room_id in room_building_map and r_id in room_building_map:
                        other_b = room_building_map[s.room_id]
                        cur_b = room_building_map[r_id]
                        if other_b != cur_b:
                            walk_time = max(building_distances.get((cur_b, other_b), 0), building_distances.get((other_b, cur_b), 0))
                            if walk_time > 15:
                                return False

        # Lecturer Consecutive Slots check
        if l_id and l_id in lecturer_max_consec:
            max_consec = lecturer_max_consec[l_id]
            day_slots = {target_slot_num}
            for ts in timeslots_by_day[day]:
                if (l_id, ts.id) in lec_ts_map:
                    s = lec_ts_map[(l_id, ts.id)]
                    if s.id != slot.id:
                        day_slots.add(ts.slot_number)
            
            day_slots_sorted = sorted(list(day_slots))
            current_run = []
            runs = []
            for snum in day_slots_sorted:
                if not current_run or snum - current_run[-1] == 1:
                    current_run.append(snum)
                else:
                    runs.append(current_run)
                    current_run = [snum]
            if current_run:
                runs.append(current_run)
            if any(len(run) > max_consec for run in runs):
                return False

        # Lecturer Daily Workload check
        if l_id:
            max_slots = lecturer_max_slots_per_day.get(l_id, 4)
            day_occupied = 0
            for ts in timeslots_by_day[day]:
                if (l_id, ts.id) in lec_ts_map:
                    s = lec_ts_map[(l_id, ts.id)]
                    if s.id != slot.id:
                        day_occupied += 1
            if day_occupied + 1 > max_slots:
                return False

        return True

    fixed = 0
    max_passes = 5
    modified_slots = {}  # id -> ScheduleSlot

    # Pass 1: Compact Student Group schedule gaps
    for _ in range(max_passes):
        improved = False

        # Group slots by (group_id, day)
        group_day_slots = defaultdict(list)
        for s in slots:
            group_day_slots[(s.student_group_id, s.time_slot.day_of_week)].append(s)

        for (gid, day), day_slots in group_day_slots.items():
            # Sort by slot_number
            day_slots.sort(key=lambda s: s.time_slot.slot_number)
            slot_nums = [s.time_slot.slot_number for s in day_slots]

            if len(slot_nums) < 2:
                continue

            first, last = slot_nums[0], slot_nums[-1]
            all_expected = set(range(first, last + 1))
            present = set(slot_nums)
            gaps = all_expected - present
            if not gaps:
                continue

            for target_pos in sorted(gaps):
                target_ts = ts_by_day_pos.get((day, target_pos))
                if not target_ts:
                    continue

                candidates = [s for s in day_slots if s.time_slot.slot_number > target_pos]
                if not candidates:
                    continue

                slot_to_move = candidates[0]

                if is_move_valid(slot_to_move, target_ts):
                    old_ts_id = slot_to_move.time_slot_id

                    # Remove old occupation
                    room_ts_map.pop((slot_to_move.room_id, old_ts_id), None)
                    if slot_to_move.lecturer_id:
                        lec_ts_map.pop((slot_to_move.lecturer_id, old_ts_id), None)
                    for g in slot_occupied_groups[slot_to_move.id]:
                        grp_ts_map.pop((g, old_ts_id), None)

                    # Apply new time slot (in-memory)
                    slot_to_move.time_slot = target_ts
                    slot_to_move.time_slot_id = target_ts.id
                    modified_slots[slot_to_move.id] = slot_to_move

                    # Register new occupation
                    room_ts_map[(slot_to_move.room_id, target_ts.id)]  = slot_to_move
                    if slot_to_move.lecturer_id:
                        lec_ts_map[(slot_to_move.lecturer_id, target_ts.id)] = slot_to_move
                    for g in slot_occupied_groups[slot_to_move.id]:
                        grp_ts_map[(g, target_ts.id)] = slot_to_move

                    # Update local list
                    day_slots = [s for s in day_slots if s.id != slot_to_move.id]
                    day_slots.append(slot_to_move)
                    day_slots.sort(key=lambda s: s.time_slot.slot_number)
                    slot_nums = [s.time_slot.slot_number for s in day_slots]
                    group_day_slots[(gid, day)] = day_slots

                    # Refresh full slots list


                    fixed += 1
                    improved = True

        if not improved:
            break

    # Pass 2: Compact Lecturer schedule gaps
    for _ in range(max_passes):
        improved = False

        # Group slots by (lecturer_id, day)
        lecturer_day_slots = defaultdict(list)
        for s in slots:
            if s.lecturer_id:
                lecturer_day_slots[(s.lecturer_id, s.time_slot.day_of_week)].append(s)

        for (lid, day), day_slots in lecturer_day_slots.items():
            day_slots.sort(key=lambda s: s.time_slot.slot_number)
            slot_nums = [s.time_slot.slot_number for s in day_slots]

            if len(slot_nums) < 2:
                continue

            first, last = slot_nums[0], slot_nums[-1]
            all_expected = set(range(first, last + 1))
            present = set(slot_nums)
            gaps = all_expected - present
            if not gaps:
                continue

            for target_pos in sorted(gaps):
                target_ts = ts_by_day_pos.get((day, target_pos))
                if not target_ts:
                    continue

                candidates = [s for s in day_slots if s.time_slot.slot_number > target_pos]
                if not candidates:
                    continue

                slot_to_move = candidates[0]

                if is_move_valid(slot_to_move, target_ts):
                    old_ts_id = slot_to_move.time_slot_id

                    # Remove old occupation
                    room_ts_map.pop((slot_to_move.room_id, old_ts_id), None)
                    lec_ts_map.pop((slot_to_move.lecturer_id, old_ts_id), None)
                    for g in slot_occupied_groups[slot_to_move.id]:
                        grp_ts_map.pop((g, old_ts_id), None)

                    # Apply new time slot (in-memory)
                    slot_to_move.time_slot = target_ts
                    slot_to_move.time_slot_id = target_ts.id
                    modified_slots[slot_to_move.id] = slot_to_move

                    # Register new occupation
                    room_ts_map[(slot_to_move.room_id, target_ts.id)]  = slot_to_move
                    lec_ts_map[(slot_to_move.lecturer_id, target_ts.id)] = slot_to_move
                    for g in slot_occupied_groups[slot_to_move.id]:
                        grp_ts_map[(g, target_ts.id)] = slot_to_move

                    # Update local list
                    day_slots = [s for s in day_slots if s.id != slot_to_move.id]
                    day_slots.append(slot_to_move)
                    day_slots.sort(key=lambda s: s.time_slot.slot_number)
                    slot_nums = [s.time_slot.slot_number for s in day_slots]
                    lecturer_day_slots[(lid, day)] = day_slots

                    # Refresh full slots list


                    fixed += 1
                    improved = True

        if not improved:
            break

    # Perform a single bulk update to save all optimized timeslots at once
    if modified_slots:
        with transaction.atomic():
            ScheduleSlot.objects.bulk_update(modified_slots.values(), ['time_slot'])

    logger.info(f"[Compaction] Fixed {fixed} gaps across student groups and lecturers.")
    return fixed


# ──────────────────────────────────────────────────────────────────────────────
# Greedy Pre-Assignment
# ──────────────────────────────────────────────────────────────────────────────

def _greedy_assign(courses, rooms, timeslots,
                   course_valid_start_indices,
                   course_orig_valid_indices_count,
                   course_durations, course_lecturers, course_groups,
                   course_campuses, course_group_sizes, course_room_types,
                   rooms_by_campus_and_type, rooms_by_campus,
                   lab_only_course_ids, course_hard_pref_rooms,
                   lecturer_hard_unavailables, ts_id_by_idx,
                   room_limit, ctx=None):
    """
    Fast greedy scheduler that assigns courses one-by-one in O(C * R * T) time.
    Returns a dict: {(c_id, r_id, t_idx): 1} for each assigned course.

    Strategy:
      - Sort courses by most-constrained first (fewest valid slots).
      - For each course try rooms in capacity order, timeslots in order.
      - Track occupation sets for rooms, lecturers, student-groups per slot.

    Args:
        ctx: optional SchedulingContext (from build_scheduling_context()).
             When provided, overlap_map and all feature/building/preference
             maps are reused instead of being rebuilt/re-queried here.
    """
    # Occupation sets: (entity_id, timeslot_idx) -> True if occupied
    room_occupied    = set()   # (r_id, u_idx)
    lecturer_occupied = set()  # (l_id, u_idx)
    group_occupied   = set()   # (g_id, u_idx)

    # For split virtual courses, track day of week: (orig_c_id, day_of_week) -> True
    course_scheduled_days = set()
    course_assigned_room = {}  # orig_c_id -> r_id (forces same room for split virtual courses)

    assignment = {}  # (c_id, r_id, t_idx) -> 1

    # Track class counts per day
    day_class_counts = defaultdict(int)
    num_courses = len(courses)

    uni_id = timeslots[0].university_id if timeslots else None

    if ctx is not None:
        # Reuse precomputed context — no re-querying, no O(T^2) rebuild.
        ts_day_by_idx = ctx.ts_day_by_idx
        overlap_map = ctx.overlap_map
        lecturer_max_consec = ctx.lecturer_max_consec
        group_parent_map = ctx.group_parent_map
        group_children_map = ctx.group_children_map
        room_features_map = ctx.room_features_map
        course_required_features_map = ctx.course_required_features_map
        course_additional_groups_map = ctx.course_additional_groups_map
        room_building_map = ctx.room_building_map
        building_distances = ctx.building_distances
        lecturer_preferences_prefer = ctx.lecturer_preferences_prefer
        lecturer_preferences_dislike = ctx.lecturer_preferences_dislike
        lecturer_max_slots_per_day = ctx.lecturer_max_slots_per_day
    else:
        # Standalone call — build everything locally, same as before.
        ts_day_by_idx = {idx: ts.day_of_week for idx, ts in enumerate(timeslots)}
        overlap_map = _get_overlap_map(uni_id, timeslots)

        from .models import Constraint, StudentGroup, Room, Course, RoomFeature, Building, BuildingDistance, LecturerTimeSlotPreference, Lecturer

        lecturer_max_consec = {}
        if uni_id:
            configs = Constraint.objects.filter(university_id=uni_id, constraint_type='LECTURER_MAX_CONSECUTIVE_SLOTS', is_hard=True)
            for config in configs:
                l_id = config.parameters.get('lecturer_id')
                p_max = config.parameters.get('max_consecutive')
                if l_id and p_max is not None:
                    lecturer_max_consec[int(l_id)] = int(p_max)

        group_parent_map = {}
        group_children_map = defaultdict(list)
        if uni_id:
            for g in StudentGroup.objects.filter(program__department__faculty__campus__university_id=uni_id).values('id', 'parent_group_id'):
                g_id = g['id']
                p_id = g['parent_group_id']
                group_parent_map[g_id] = p_id
                if p_id:
                    group_children_map[p_id].append(g_id)

        room_features_map = defaultdict(set)
        course_required_features_map = defaultdict(set)
        course_additional_groups_map = defaultdict(set)
        room_building_map = {}
        building_distances = {}
        lecturer_preferences_prefer = defaultdict(set)
        lecturer_preferences_dislike = defaultdict(set)
        lecturer_max_slots_per_day = {}

        if uni_id:
            for room_id, feature_id in Room.features.through.objects.filter(room__campus__university_id=uni_id).values_list('room_id', 'roomfeature_id'):
                room_features_map[room_id].add(feature_id)
            for course_id, feature_id in Course.required_features.through.objects.filter(course__program__department__faculty__campus__university_id=uni_id).values_list('course_id', 'roomfeature_id'):
                course_required_features_map[course_id].add(feature_id)
            for course_id, group_id in Course.additional_student_groups.through.objects.filter(course__program__department__faculty__campus__university_id=uni_id).values_list('course_id', 'studentgroup_id'):
                course_additional_groups_map[course_id].add(group_id)
            room_building_map = {r_id: b_id for r_id, b_id in Room.objects.filter(campus__university_id=uni_id).values_list('id', 'building_id')}
            for b1, b2, time_min in BuildingDistance.objects.filter(from_building__campus__university_id=uni_id).values_list('from_building_id', 'to_building_id', 'walking_time_minutes'):
                building_distances[(b1, b2)] = time_min
            for l_id, ts_id, pref in LecturerTimeSlotPreference.objects.filter(lecturer__department__faculty__campus__university_id=uni_id).values_list('lecturer_id', 'time_slot_id', 'preference_level'):
                if pref == 'prefer':
                    lecturer_preferences_prefer[l_id].add(ts_id)
                elif pref == 'dislike':
                    lecturer_preferences_dislike[l_id].add(ts_id)
            lecturer_max_slots_per_day = {l_id: max_slots for l_id, max_slots in Lecturer.objects.filter(department__faculty__campus__university_id=uni_id).values_list('id', 'max_slots_per_day')}

    # Track lecturer scheduled slots, rooms, and group rooms
    lecturer_scheduled_slots = defaultdict(dict)
    lecturer_scheduled_rooms = defaultdict(dict)
    group_scheduled_rooms = defaultdict(dict)
    # O(1) daily workload counters — replaces O(T) linear scan per candidate slot
    lecturer_day_count = defaultdict(lambda: defaultdict(int))  # [l_id][day] -> slot count

    # Sort: most constrained (fewest valid start slots for original duration) first
    sorted_courses = sorted(
        courses,
        key=lambda c: course_orig_valid_indices_count.get(c.id, 0)
    )

    for course in sorted_courses:
        c_id      = course.id
        duration  = course_durations[c_id]
        lec_id    = course_lecturers[c_id]
        group_id  = course_groups[c_id]
        campus_id = course_campuses[c_id]
        group_size = course_group_sizes[c_id]
        req_type  = course_room_types[c_id]

        orig_c_id = course.orig_course_id
        is_virtual = (course.orig_duration > course.duration_slots)

        valid_t_indices = course_valid_start_indices.get(c_id, [])

        # Filter by lecturer hard unavailability
        if lec_id and lec_id in lecturer_hard_unavailables:
            unavail_set = lecturer_hard_unavailables[lec_id]
            valid_t_indices = [
                t for t in valid_t_indices
                if not any(ts_id_by_idx.get(t + off) in unavail_set for off in range(duration))
            ]

        # Build candidate room list — NOTE: pre-sorted rooms, so no sort needed here!
        candidate_rooms = rooms_by_campus_and_type.get((campus_id, req_type), [])
        eligible_rooms  = [r for r in candidate_rooms if r.capacity >= group_size]
        if not eligible_rooms:
            all_campus = rooms_by_campus.get(campus_id, [])
            eligible_rooms = [r for r in all_campus if r.capacity >= group_size]
        if not eligible_rooms:
            eligible_rooms = [r for r in rooms if r.room_type == req_type and r.capacity >= group_size]
            if not eligible_rooms:
                eligible_rooms = [r for r in rooms if r.capacity >= group_size]
        if orig_c_id in lab_only_course_ids:
            eligible_rooms = [r for r in eligible_rooms if r.room_type == 'Lab']
        if orig_c_id in course_hard_pref_rooms:
            pref_set = course_hard_pref_rooms[orig_c_id]
            eligible_rooms = [r for r in eligible_rooms if r.id in pref_set]
            
        req_feats = course_required_features_map.get(orig_c_id, set())
        if req_feats:
            eligible_rooms = [r for r in eligible_rooms if req_feats.issubset(room_features_map.get(r.id, set()))]

        # Force same room for split virtual courses
        if is_virtual and orig_c_id in course_assigned_room:
            assigned_r_id = course_assigned_room[orig_c_id]
            eligible_rooms = [r for r in eligible_rooms if r.id == assigned_r_id]

        num_eligible = len(eligible_rooms)
        if num_eligible > room_limit:
            window_size  = num_eligible
            offset       = c_id % max(1, window_size - room_limit + 1)
            eligible_rooms = eligible_rooms[offset: offset + room_limit]

        # First pass: try to schedule on a day where this course is not yet scheduled
        placed = False

        # Pre-score all candidate timeslots once (avoids re-evaluating the closure per comparison)
        prefer_set  = lecturer_preferences_prefer.get(lec_id, set())  if lec_id else set()
        dislike_set = lecturer_preferences_dislike.get(lec_id, set()) if lec_id else set()
        slot_scores = []
        for t_idx in valid_t_indices:
            day = ts_day_by_idx[t_idx]
            score = day_class_counts[day] * 100
            if lec_id:
                ts_ids = [ts_id_by_idx[t_idx + off] for off in range(duration)]
                if any(tid in dislike_set for tid in ts_ids):
                    score += 5000
                elif any(tid in prefer_set for tid in ts_ids):
                    score -= 50
            slot_scores.append((score, t_idx))
        slot_scores.sort()
        sorted_t_indices = [t for _, t in slot_scores]

        # Pre-expand student groups
        related_groups = {group_id}
        for g_add in course_additional_groups_map.get(orig_c_id, set()):
            related_groups.add(g_add)
        expanded_groups = set()
        for rg in related_groups:
            expanded_groups.add(rg)
            p_id = group_parent_map.get(rg)
            if p_id:
                expanded_groups.add(p_id)
            for child_id in group_children_map.get(rg, []):
                expanded_groups.add(child_id)

        # Define a helper closure to check room-specific placement constraints (room occupancy, building travel times)
        def is_placement_possible(t_idx, r_id):
            span = range(t_idx, t_idx + duration)
            if any((r_id, ou) in room_occupied for u in span for ou in overlap_map[u]):
                return False
                
            # Check Building Travel Times for lecturer
            if lec_id and r_id in room_building_map:
                lec_b = room_building_map[r_id]
                if t_idx - 1 in lecturer_scheduled_slots[lec_id]:
                    if ts_day_by_idx[t_idx - 1] == ts_day_by_idx[t_idx]:
                        prev_r_id = lecturer_scheduled_rooms[lec_id].get(t_idx - 1)
                        if prev_r_id and prev_r_id in room_building_map:
                            prev_b = room_building_map[prev_r_id]
                            if prev_b != lec_b:
                                walk_time = max(building_distances.get((prev_b, lec_b), 0), building_distances.get((lec_b, prev_b), 0))
                                if walk_time > 15:
                                    return False
                if t_idx + duration in lecturer_scheduled_slots[lec_id]:
                    if ts_day_by_idx[t_idx + duration] == ts_day_by_idx[t_idx]:
                        next_r_id = lecturer_scheduled_rooms[lec_id].get(t_idx + duration)
                        if next_r_id and next_r_id in room_building_map:
                            next_b = room_building_map[next_r_id]
                            if next_b != lec_b:
                                walk_time = max(building_distances.get((lec_b, next_b), 0), building_distances.get((next_b, lec_b), 0))
                                if walk_time > 15:
                                    return False

            # Check Building Travel Times for student groups (primary and expanded)
            for rg_id in expanded_groups:
                if t_idx - 1 in group_scheduled_rooms[rg_id]:
                    if ts_day_by_idx[t_idx - 1] == ts_day_by_idx[t_idx]:
                        prev_r_id = group_scheduled_rooms[rg_id].get(t_idx - 1)
                        if prev_r_id and prev_r_id in room_building_map and r_id in room_building_map:
                            prev_b = room_building_map[prev_r_id]
                            cur_b = room_building_map[r_id]
                            if prev_b != cur_b:
                                walk_time = max(building_distances.get((prev_b, cur_b), 0), building_distances.get((cur_b, prev_b), 0))
                                if walk_time > 15:
                                    return False
                if t_idx + duration in group_scheduled_rooms[rg_id]:
                    if ts_day_by_idx[t_idx + duration] == ts_day_by_idx[t_idx]:
                        next_r_id = group_scheduled_rooms[rg_id].get(t_idx + duration)
                        if next_r_id and next_r_id in room_building_map and r_id in room_building_map:
                            next_b = room_building_map[next_r_id]
                            cur_b = room_building_map[r_id]
                            if next_b != cur_b:
                                walk_time = max(building_distances.get((cur_b, next_b), 0), building_distances.get((next_b, cur_b), 0))
                                if walk_time > 15:
                                    return False
            return True

        for t_idx in sorted_t_indices:
            day = ts_day_by_idx[t_idx]
            if (orig_c_id, day) in course_scheduled_days:
                continue

            # Check lecturer & group availability across all duration slots
            span = range(t_idx, t_idx + duration)
            if lec_id and any((lec_id, ou) in lecturer_occupied for u in span for ou in overlap_map[u]):
                continue
            
            # Check student group double booking (including parent-child and shared electives)
            if any((rg_id, ou) in group_occupied for rg_id in expanded_groups for u in span for ou in overlap_map[u]):
                continue

            # Check Campus Travel Times for lecturer (independent of room)
            if lec_id:
                if t_idx - 1 in lecturer_scheduled_slots[lec_id]:
                    if ts_day_by_idx[t_idx - 1] == ts_day_by_idx[t_idx]:
                        if lecturer_scheduled_slots[lec_id][t_idx - 1] != campus_id:
                            continue
                if t_idx + duration in lecturer_scheduled_slots[lec_id]:
                    if ts_day_by_idx[t_idx + duration] == ts_day_by_idx[t_idx]:
                        if lecturer_scheduled_slots[lec_id][t_idx + duration] != campus_id:
                            continue

            # Check Max Consecutive Slots for lecturer (independent of room)
            if lec_id and lec_id in lecturer_max_consec:
                max_consec = lecturer_max_consec[lec_id]
                # O(1) check: use pre-maintained run-length tracker instead of rebuild+sort
                lec_day_slots_set = {u for u in lecturer_scheduled_slots[lec_id] if ts_day_by_idx[u] == day}
                lec_day_slots_set.update(span)
                sorted_day = sorted(lec_day_slots_set)
                max_run = 1
                cur_run = 1
                for k in range(1, len(sorted_day)):
                    if sorted_day[k] - sorted_day[k-1] == 1:
                        cur_run += 1
                        if cur_run > max_run:
                            max_run = cur_run
                    else:
                        cur_run = 1
                if max_run > max_consec:
                    continue

            # Check Lecturer Daily Workload Limit — O(1) using pre-maintained counter
            if lec_id:
                max_slots = lecturer_max_slots_per_day.get(lec_id, 4)
                if lecturer_day_count[lec_id][day] + duration > max_slots:
                    continue

            for room in eligible_rooms:
                r_id = room.id
                if not is_placement_possible(t_idx, r_id):
                    continue

                # Place the course
                assignment[(c_id, r_id, t_idx)] = 1
                for u in span:
                    for ou in overlap_map[u]:
                        room_occupied.add((r_id, ou))
                        if lec_id:
                            lecturer_occupied.add((lec_id, ou))
                        for rg_id in expanded_groups:
                            group_occupied.add((rg_id, ou))

                    if lec_id:
                        lecturer_scheduled_slots[lec_id][u] = campus_id
                        lecturer_scheduled_rooms[lec_id][u] = r_id
                    for rg_id in expanded_groups:
                        group_scheduled_rooms[rg_id][u] = r_id
                course_scheduled_days.add((orig_c_id, day))
                if is_virtual:
                    course_assigned_room[orig_c_id] = r_id
                day_class_counts[day] += 1
                if lec_id:
                    lecturer_day_count[lec_id][day] += duration  # keep O(1) workload counter in sync
                placed = True
                break
            if placed:
                break

        # Second pass (fallback): if not placed, allow scheduling on any day
        if not placed and is_virtual:
            sorted_fallback_indices = sorted_t_indices  # already scored and sorted above

            for t_idx in sorted_fallback_indices:
                day = ts_day_by_idx[t_idx]
                span = range(t_idx, t_idx + duration)
                if lec_id and any((lec_id, ou) in lecturer_occupied for u in span for ou in overlap_map[u]):
                    continue

                # Check student group double booking (including parent-child and shared electives)
                if any((rg_id, ou) in group_occupied for rg_id in expanded_groups for u in span for ou in overlap_map[u]):
                    continue

                # Check Campus Travel Times for lecturer (independent of room)
                if lec_id:
                    if t_idx - 1 in lecturer_scheduled_slots[lec_id]:
                        if ts_day_by_idx[t_idx - 1] == ts_day_by_idx[t_idx]:
                            if lecturer_scheduled_slots[lec_id][t_idx - 1] != campus_id:
                                continue
                    if t_idx + duration in lecturer_scheduled_slots[lec_id]:
                        if ts_day_by_idx[t_idx + duration] == ts_day_by_idx[t_idx]:
                            if lecturer_scheduled_slots[lec_id][t_idx + duration] != campus_id:
                                continue

                # Check Max Consecutive Slots — O(1) tracker
                if lec_id and lec_id in lecturer_max_consec:
                    max_consec = lecturer_max_consec[lec_id]
                    lec_day_slots_set = {u for u in lecturer_scheduled_slots[lec_id] if ts_day_by_idx[u] == day}
                    lec_day_slots_set.update(span)
                    sorted_day = sorted(lec_day_slots_set)
                    max_run = 1
                    cur_run = 1
                    for k in range(1, len(sorted_day)):
                        if sorted_day[k] - sorted_day[k-1] == 1:
                            cur_run += 1
                            if cur_run > max_run:
                                max_run = cur_run
                        else:
                            cur_run = 1
                    if max_run > max_consec:
                        continue

                # Check Lecturer Daily Workload Limit — O(1) counter
                if lec_id:
                    max_slots = lecturer_max_slots_per_day.get(lec_id, 4)
                    if lecturer_day_count[lec_id][day] + duration > max_slots:
                        continue

                for room in eligible_rooms:
                    r_id = room.id
                    if not is_placement_possible(t_idx, r_id):
                        continue

                    # Place the course
                    assignment[(c_id, r_id, t_idx)] = 1
                    for u in span:
                        for ou in overlap_map[u]:
                            room_occupied.add((r_id, ou))
                            if lec_id:
                                lecturer_occupied.add((lec_id, ou))
                            for rg_id in expanded_groups:
                                group_occupied.add((rg_id, ou))

                        if lec_id:
                            lecturer_scheduled_slots[lec_id][u] = campus_id
                            lecturer_scheduled_rooms[lec_id][u] = r_id
                        for rg_id in expanded_groups:
                            group_scheduled_rooms[rg_id][u] = r_id
                    course_scheduled_days.add((orig_c_id, day))
                    course_assigned_room[orig_c_id] = r_id
                    day_class_counts[day] += 1
                    if lec_id:
                        lecturer_day_count[lec_id][day] += duration
                    placed = True
                    break
                if placed:
                    break

        if not placed:
            logger.debug(f"[Greedy] Could not place course {c_id} — left to CP-SAT")

    return assignment


class FirebaseProgressCallback(cp_model.CpSolverSolutionCallback):
    """
    CP-SAT Solver callback that pushes progress reports (intermediate solutions,
    objective scores, and placed courses counts) to Firebase Realtime Database.
    """
    def __init__(self, timetable_id, variables_dict):
        cp_model.CpSolverSolutionCallback.__init__(self)
        self.timetable_id = timetable_id
        self.variables_dict = variables_dict
        self.solution_count = 0
        self.last_update_time = time.time()

    def on_solution_callback(self):
        self.solution_count += 1
        current_time = time.time()
        # Throttle updates to Firebase (at most once every 1.5 seconds)
        if current_time - self.last_update_time >= 1.5:
            self.last_update_time = current_time
            # Count scheduled courses (unique course_ids)
            scheduled_course_ids = set()
            for (c_id, r_id, t_idx), var in self.variables_dict.items():
                if self.Value(var) == 1:
                    scheduled_course_ids.add(c_id)
            scheduled_count = len(scheduled_course_ids)
            
            try:
                from .firebase_service import update_generation_status
                update_generation_status(self.timetable_id, {
                    'status': 'SOLVING',
                    'message': f'Solving... Found {self.solution_count} feasible solution(s).',
                    'courses_scheduled': scheduled_count,
                    'solver_score': int(self.ObjectiveValue()),
                    'hard_conflicts': 0,
                    'soft_conflicts': 0
                })
            except Exception:
                pass


# ──────────────────────────────────────────────────────────────────────────────
# Main Solver Entry Point
# ──────────────────────────────────────────────────────────────────────────────

def generate_timetable(timetable_id, time_limit_seconds=30):
    """
    Generates a timetable using a two-phase approach:
      Phase 1 — Greedy pre-assignment (fast, O(C*R*T))
      Phase 2 — Google OR-Tools CP-SAT polishing (warm-started from greedy hints)

    Returns: (status, message, objective_value)
      - status:           'OPTIMAL', 'FEASIBLE', 'INFEASIBLE', or 'ERROR'
      - message:          Human-readable outcome description
      - objective_value:  Integer solver score (lower = better); None if no solution found
    """
    try:
        timetable = Timetable.objects.select_related('semester', 'semester__university').get(pk=timetable_id)
    except Timetable.DoesNotExist:
        return 'ERROR', f"Timetable with ID {timetable_id} does not exist.", None

    university = timetable.semester.university

    # ── Load university data using optimized values() queries ────────────────────────
    courses_raw = list(Course.objects.filter(
        program__department__faculty__campus__university=university,
        lecturer__isnull=False,
        lecturer__is_active=True,
        student_group__isnull=False
    ).values(
        'id', 'code', 'name', 'duration_slots', 'sessions_per_week', 'required_room_type',
        'lecturer_id', 'student_group_id', 'program__department__faculty__campus_id',
        'student_group__size'
    ))

    _VIRTUAL_ID_COUNTER[0] = 10_000_000
    courses_raw = expand_courses_for_scheduling(courses_raw)

    courses = [CourseObj(c) for c in courses_raw]

    rooms_raw = list(Room.objects.filter(campus__university=university).values(
        'id', 'name', 'capacity', 'room_type', 'campus_id'
    ))
    rooms = [RoomObj(r) for r in rooms_raw]

    timeslots  = list(TimeSlot.objects.filter(university=university).order_by('day_of_week', 'slot_number'))

    # ── Load DB Constraints (needed by build_scheduling_context too) ─────────
    db_constraints = list(Constraint.objects.filter(university=university))

    # ── Build the shared scheduling context ONCE ──────────────────────────────
    # This replaces three separate O(T^2) overlap_map rebuilds and ~8 duplicate
    # DB queries (features, building distances, group hierarchy, lecturer
    # preferences) that used to happen independently in generate_timetable(),
    # _greedy_assign(), and compact_schedule_gaps().
    ctx = build_scheduling_context(university, timeslots, db_constraints)

    overlap_map = ctx.overlap_map
    ts_to_idx = ctx.ts_to_idx
    idx_to_ts = ctx.idx_to_ts
    ts_id_by_idx = ctx.ts_id_by_idx
    ts_day_by_idx = ctx.ts_day_by_idx
    ts_is_evening_by_idx = ctx.ts_is_evening_by_idx
    ts_pos_in_day = ctx.ts_pos_in_day
    timeslots_by_day = ctx.timeslots_by_day
    room_features_map = ctx.room_features_map
    course_required_features_map = ctx.course_required_features_map
    course_additional_groups_map = ctx.course_additional_groups_map
    room_building_map = ctx.room_building_map
    building_distances = ctx.building_distances
    lecturer_preferences_prefer = ctx.lecturer_preferences_prefer
    lecturer_preferences_dislike = ctx.lecturer_preferences_dislike
    lecturer_max_slots_per_day = ctx.lecturer_max_slots_per_day

    num_courses = len(courses)
    if num_courses <= 2000:
        lecturers  = list(Lecturer.objects.filter(department__faculty__campus__university=university))
        student_groups = list(StudentGroup.objects.filter(
            program__department__faculty__campus__university=university
        ))
    else:
        lecturers = []
        student_groups = []

    if not courses:
        return 'ERROR', "No courses with assigned lecturers and student groups found.", None
    if not rooms:
        return 'ERROR', "No rooms found in the university campuses.", None
    if not timeslots:
        return 'ERROR', "No time slots found.", None

    # NOTE: timeslots_by_day, ts_to_idx/idx_to_ts, ts_pos_in_day, ts_id_by_idx,
    # ts_day_by_idx, ts_is_evening_by_idx, and db_constraints are already
    # available from ctx / the earlier db_constraints load above — no need to
    # rebuild them here (previously this was a second full pass).

    hard_no_evening = any(
        c.constraint_type == 'NO_EVENING_CLASSES' and c.is_hard
        for c in db_constraints
    )

    lab_only_course_ids = set()
    for c in db_constraints:
        if c.constraint_type == 'LAB_ONLY_COURSE' and c.is_hard:
            cid = c.parameters.get('course_id')
            if cid:
                lab_only_course_ids.add(int(cid))

    student_max_per_day = {}
    for c in db_constraints:
        if c.constraint_type == 'STUDENT_MAX_CLASSES_PER_DAY' and c.is_hard:
            gid     = c.parameters.get('student_group_id')
            max_cls = c.parameters.get('max_classes')
            if gid and max_cls is not None:
                student_max_per_day[int(gid)] = int(max_cls)

    # ── Pre-build constraint mappings ─────────────────────────────────────────
    lecturer_hard_unavailables = defaultdict(set)
    lecturer_soft_unavailables = defaultdict(list)
    course_hard_pref_rooms     = {}
    course_soft_pref_rooms     = {}

    for db_const in db_constraints:
        if db_const.constraint_type == 'LECTURER_AVAILABILITY':
            l_id         = db_const.parameters.get('lecturer_id')
            unavail_slots = db_const.parameters.get('unavailable_slots', [])
            if l_id and unavail_slots:
                if db_const.is_hard:
                    lecturer_hard_unavailables[l_id].update(unavail_slots)
                else:
                    lecturer_soft_unavailables[l_id].append((db_const.weight, set(unavail_slots)))
        elif db_const.constraint_type == 'ROOM_PREFERENCE':
            c_id      = db_const.parameters.get('course_id')
            pref_rooms = db_const.parameters.get('preferred_rooms', [])
            if c_id and pref_rooms:
                if db_const.is_hard:
                    course_hard_pref_rooms[c_id] = set(pref_rooms)
                else:
                    course_soft_pref_rooms[c_id] = (db_const.weight, set(pref_rooms))

    # Add Lecturer self-service availability as hard constraints
    unavail_records = LecturerAvailability.objects.filter(
        lecturer__department__faculty__campus__university=university,
        is_available=False
    )
    for record in unavail_records:
        lecturer_hard_unavailables[record.lecturer_id].add(record.time_slot_id)

    # NOTE: room_features_map, course_required_features_map,
    # course_additional_groups_map, room_building_map, building_distances,
    # lecturer_preferences_prefer/dislike, and lecturer_max_slots_per_day are
    # already sourced from ctx above — no re-querying needed here.

    # ── Pre-compute Course attributes ─────────────────────────────────────────
    course_durations   = {c.id: c.duration_slots                          for c in courses}
    course_lecturers   = {c.id: c.lecturer_id                             for c in courses}
    course_groups      = {c.id: c.student_group_id                        for c in courses}
    course_campuses    = {c.id: c.campus_id                               for c in courses}
    course_group_sizes = {c.id: c.group_size                              for c in courses}
    course_room_types  = {c.id: c.required_room_type                      for c in courses}

    # Pre-group rooms by campus and type, and pre-sort by capacity
    rooms_by_campus_and_type = {}
    for r in rooms:
        rooms_by_campus_and_type.setdefault((r.campus_id, r.room_type), []).append(r)
    for key in rooms_by_campus_and_type:
        rooms_by_campus_and_type[key].sort(key=lambda x: x.capacity)

    rooms_by_campus = {}
    for r in rooms:
        rooms_by_campus.setdefault(r.campus_id, []).append(r)
    for key in rooms_by_campus:
        rooms_by_campus[key].sort(key=lambda x: x.capacity)

    course_by_id = {c.id: c for c in courses}
    room_by_id   = {r.id: r for r in rooms}

    # Dynamically scale room candidate limit based on dataset size
    if num_courses <= 50:
        room_limit = 20
    elif num_courses <= 150:
        room_limit = 12
    elif num_courses <= 2000:
        room_limit = 6
    else:
        room_limit = 4

    # ── FIX G1: Compute valid start indices using O(1) pos lookup ─────────────
    course_valid_start_indices = {}
    course_orig_valid_indices_count = {}
    for course in courses:
        c_id      = course.id
        duration  = course_durations[c_id]
        orig_dur  = course.orig_duration
        valid_indices = []
        orig_valid_count = 0
        for ts in timeslots:
            if hard_no_evening and ts.is_evening:
                continue
            ts_idx  = ts_to_idx[ts.id]
            day_slots = timeslots_by_day[ts.day_of_week]
            pos = ts_pos_in_day[ts.id]          # O(1) — was O(N) list.index()
            if pos + duration <= len(day_slots):
                spanned = day_slots[pos:pos + duration]
                # Ensure the timeslots are strictly chronological and non-overlapping
                if not any(spanned[idx].end_time > spanned[idx + 1].start_time for idx in range(len(spanned) - 1)):
                    if not (hard_no_evening and any(s.is_evening for s in spanned)):
                        valid_indices.append(ts_idx)
            if pos + orig_dur <= len(day_slots):
                spanned_orig = day_slots[pos:pos + orig_dur]
                # Ensure the timeslots are strictly chronological and non-overlapping
                if not any(spanned_orig[idx].end_time > spanned_orig[idx + 1].start_time for idx in range(len(spanned_orig) - 1)):
                    if not (hard_no_evening and any(s.is_evening for s in spanned_orig)):
                        orig_valid_count += 1
        course_valid_start_indices[c_id] = valid_indices
        course_orig_valid_indices_count[c_id] = orig_valid_count

    # ── FIX G2: Phase 1 — Greedy Pre-Assignment ───────────────────────────────
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

    greedy_assigned = len(greedy_result)
    t_greedy_elapsed = round(time.perf_counter() - t_greedy_start, 3)
    logger.info(
        f"[Solver] Greedy assigned {greedy_assigned}/{num_courses} courses "
        f"in {t_greedy_elapsed}s"
    )

    # ── Fast-path: Greedy solved everything OR dataset is too large or time_limit is low — skip CP-SAT entirely ───────────
    # We bypass CP-SAT if:
    # 1. Greedy solver scheduled 100% of courses.
    # 2. Dataset is very large (>1000 courses).
    # 3. Dataset is moderately large (>300 courses) and the time limit is <= 120 seconds,
    #    guaranteeing instant generation (<5 seconds) instead of waiting for a CP-SAT timeout.
    should_bypass_cpsat = (
        greedy_assigned == num_courses or 
        num_courses > 1000 or
        (num_courses > 300 and time_limit_seconds <= 120)
    )

    if should_bypass_cpsat:
        if num_courses > 1000 or (num_courses > 300 and time_limit_seconds <= 120):
            logger.info(f"[Solver] Large dataset ({num_courses} courses) or low time limit ({time_limit_seconds}s) — skipping CP-SAT, saving greedy results directly.")
        else:
            logger.info("[Solver] Greedy solved 100% — skipping CP-SAT, saving directly.")
        
        slots_to_create = []
        with transaction.atomic():
            ScheduleSlot.objects.filter(timetable_id=timetable_id).delete()
            for (c_id, r_id, t_idx) in greedy_result:
                course   = course_by_id[c_id]
                duration = course.duration_slots
                for i in range(duration):
                    ts = idx_to_ts[t_idx + i]
                    slots_to_create.append(
                        ScheduleSlot(
                            timetable_id=timetable_id,
                            course_id=course_by_id[c_id].orig_course_id,
                            lecturer_id=course.lecturer_id,
                            room_id=r_id,
                            time_slot_id=ts.id,
                            student_group_id=course.student_group_id,
                        )
                    )
            # Use batch_size to avoid SQLITE_MAX_VARIABLE_NUMBER exceptions on large datasets
            ScheduleSlot.objects.bulk_create(slots_to_create, batch_size=2000)
            
        # Compact gaps after saving
        gaps_fixed = compact_schedule_gaps(timetable, ctx=ctx)
        gap_note = f" Compacted {gaps_fixed} schedule gaps." if gaps_fixed else ""
        
        pct = round(greedy_assigned / num_courses * 100)
        status_outcome = 'FEASIBLE' if greedy_assigned < num_courses else 'OPTIMAL'
        return (
            status_outcome,
            f"Timetable generated instantly via greedy solver ({greedy_assigned}/{num_courses} courses, {pct}%). "
            f"{len(slots_to_create)} slots assigned in {t_greedy_elapsed}s.{gap_note}",
            0,
        )

    # ── Phase 2 — CP-SAT Model (only runs if greedy missed some courses) ──────
    model = cp_model.CpModel()

    x = {}
    vars_by_room_and_slot          = defaultdict(list)
    vars_by_lecturer_and_slot      = defaultdict(list)
    vars_by_student_group_and_slot = defaultdict(list)
    vars_by_course                 = defaultdict(list)
    start_vars_by_group_and_day    = defaultdict(list)
    start_vars_by_lecturer_and_day = defaultdict(list)

    obj_terms = []
    evening_penalty_weight  = 5
    room_mismatch_penalty   = 15
    avail_penalty_weight    = 20

    # Reuse group parent-child relationships from the shared context (already queried once)
    group_parent_map  = ctx.group_parent_map
    group_children_map = ctx.group_children_map



    for course in courses:
        c_id       = course.id
        duration   = course_durations[c_id]
        valid_t_indices = course_valid_start_indices[c_id]
        lec_id     = course_lecturers[c_id]
        group_id   = course_groups[c_id]
        course_campus_id = course_campuses[c_id]
        group_size = course_group_sizes[c_id]
        req_room_type = course_room_types[c_id]
        orig_c_id  = course.orig_course_id

        # Precompute expanded groups ONCE per course (not per room×timeslot)
        _related = {group_id}
        for _g_add in course_additional_groups_map.get(orig_c_id, set()):
            _related.add(_g_add)
        course_expanded_groups = set()
        for _rg in _related:
            course_expanded_groups.add(_rg)
            _p = group_parent_map.get(_rg)
            if _p:
                course_expanded_groups.add(_p)
            for _ch in group_children_map.get(_rg, []):
                course_expanded_groups.add(_ch)

        # Find eligible rooms
        candidate_rooms = rooms_by_campus_and_type.get((course_campus_id, req_room_type), [])
        eligible_rooms  = [r for r in candidate_rooms if r.capacity >= group_size]
        if not eligible_rooms:
            all_campus_rooms = rooms_by_campus.get(course_campus_id, [])
            eligible_rooms   = [r for r in all_campus_rooms if r.capacity >= group_size]
        if not eligible_rooms:
            eligible_rooms = [r for r in rooms if r.room_type == req_room_type and r.capacity >= group_size]
            if not eligible_rooms:
                eligible_rooms = [r for r in rooms if r.capacity >= group_size]
        if orig_c_id in lab_only_course_ids:
            eligible_rooms = [r for r in eligible_rooms if r.room_type == 'Lab']
        if orig_c_id in course_hard_pref_rooms:
            pref_set = course_hard_pref_rooms[orig_c_id]
            eligible_rooms = [r for r in eligible_rooms if r.id in pref_set]
        
        req_feats = course_required_features_map.get(orig_c_id, set())
        if req_feats:
            eligible_rooms = [r for r in eligible_rooms if req_feats.issubset(room_features_map.get(r.id, set()))]

        eligible_rooms = sorted(eligible_rooms, key=lambda r: r.capacity)
        num_eligible   = len(eligible_rooms)
        if num_eligible <= room_limit:
            selected_rooms = eligible_rooms
        else:
            window_size  = num_eligible
            offset       = c_id % max(1, window_size - room_limit + 1)
            selected_rooms = eligible_rooms[offset: offset + room_limit]

        # Filter timeslots by lecturer hard availability
        if lec_id and lec_id in lecturer_hard_unavailables:
            unavail_set = lecturer_hard_unavailables[lec_id]
            filtered_t_indices = [
                t for t in valid_t_indices
                if not any(ts_id_by_idx[t + off] in unavail_set for off in range(duration))
            ]
        else:
            filtered_t_indices = valid_t_indices

        # Pre-calculate timeslot penalties for this course
        ts_penalties = {}
        for t_idx in filtered_t_indices:
            penalty = 0
            evening_count = sum(1 for i in range(duration) if ts_is_evening_by_idx[t_idx + i])
            if evening_count > 0:
                penalty += evening_penalty_weight * evening_count
            if lec_id and lec_id in lecturer_soft_unavailables:
                for weight, unavail_set in lecturer_soft_unavailables[lec_id]:
                    if any(ts_id_by_idx[t_idx + off] in unavail_set for off in range(duration)):
                        penalty += avail_penalty_weight
            
            # Lecturer soft timeslot preference
            if lec_id:
                prefer_set = lecturer_preferences_prefer.get(lec_id, set())
                dislike_set = lecturer_preferences_dislike.get(lec_id, set())
                if any(ts_id_by_idx[t_idx + off] in prefer_set for off in range(duration)):
                    penalty -= 15
                if any(ts_id_by_idx[t_idx + off] in dislike_set for off in range(duration)):
                    penalty += 15
            ts_penalties[t_idx] = penalty

        soft_pref_weight, soft_pref_rooms = course_soft_pref_rooms.get(orig_c_id, (0, None))

        for room in selected_rooms:
            r_id = room.id
            room_mismatch_penalty_val = room_mismatch_penalty if req_room_type != room.room_type else 0
            pref_weight = -soft_pref_weight if (soft_pref_rooms and r_id in soft_pref_rooms) else 0

            for t_idx in filtered_t_indices:
                var = model.NewBoolVar(f"x_c{c_id}_r{r_id}_t{t_idx}")
                x[(c_id, r_id, t_idx)] = var
                vars_by_course[c_id].append(var)

                day = ts_day_by_idx[t_idx]

                for rg_id in course_expanded_groups:
                    start_vars_by_group_and_day[(rg_id, day)].append(var)
                if lec_id:
                    start_vars_by_lecturer_and_day[(lec_id, day)].append(var)

                for offset in range(duration):
                    u_idx = t_idx + offset
                    for ou_idx in overlap_map[u_idx]:
                        vars_by_room_and_slot[(r_id, ou_idx)].append(var)
                        if lec_id:
                            vars_by_lecturer_and_slot[(lec_id, ou_idx)].append(var)
                        for rg_id in course_expanded_groups:
                            vars_by_student_group_and_slot[(rg_id, ou_idx)].append(var)

                total_penalty = ts_penalties[t_idx] + room_mismatch_penalty_val + pref_weight
                if total_penalty != 0:
                    obj_terms.append(var * total_penalty)

    # ── Hard Constraint 1: Each course scheduled exactly once ────────────────
    for course in courses:
        c_id = course.id
        course_vars = vars_by_course.get(c_id, [])
        if not course_vars:
            return 'INFEASIBLE', f"Course '{course.code}' cannot be scheduled (check room capacities or timeslots).", None
        model.AddExactlyOne(course_vars)

    # ── Hard Constraint 2: Room cannot host two classes at the same time ──────
    for (r_id, u_idx), overlapping in vars_by_room_and_slot.items():
        if len(overlapping) > 1:
            model.AddAtMostOne(overlapping)

    # ── Hard Constraint 3: Lecturer cannot teach two classes simultaneously ───
    for (l_id, u_idx), overlapping in vars_by_lecturer_and_slot.items():
        if len(overlapping) > 1:
            model.AddAtMostOne(overlapping)

    # ── Hard Constraint 4: Student group cannot attend two classes at once ────
    for (g_id, u_idx), overlapping in vars_by_student_group_and_slot.items():
        if len(overlapping) > 1:
            model.AddAtMostOne(overlapping)

    vars_by_lecturer_and_day = defaultdict(list)
    for (c_id, r_id, t_idx), var in x.items():
        l_id = course_lecturers[c_id]
        if l_id:
            day = ts_day_by_idx[t_idx]
            vars_by_lecturer_and_day[(l_id, day)].append(var * course_durations[c_id])

    # ── Hard Constraint: Lecturer Daily Workload Limits ──
    for lec in lecturers:
        l_id = lec.id
        max_slots = lecturer_max_slots_per_day.get(l_id, 4)
        for day in timeslots_by_day.keys():
            day_vars = vars_by_lecturer_and_day.get((l_id, day), [])
            if day_vars:
                model.Add(sum(day_vars) <= max_slots)

    # ── Hard Constraint: Building Travel Time Constraints (Lecturers & Groups) ──
    lec_building_vars = defaultdict(list)
    group_building_vars = defaultdict(list)
    for (c_id, r_id, t_idx), var in x.items():
        lec_id = course_lecturers[c_id]
        group_id = course_groups[c_id]
        b_id = room_building_map.get(r_id)
        if not b_id:
            continue
        duration = course_durations[c_id]
        for offset in range(duration):
            ts = idx_to_ts[t_idx + offset]
            day = ts.day_of_week
            snum = ts.slot_number
            if lec_id:
                lec_building_vars[(lec_id, day, snum, b_id)].append(var)
            related = {group_id}
            for g_add in course_additional_groups_map.get(course_by_id[c_id].orig_course_id, set()):
                related.add(g_add)
            expanded = set()
            for rg in related:
                expanded.add(rg)
                p_id = group_parent_map.get(rg)
                if p_id:
                    expanded.add(p_id)
                for child_id in group_children_map.get(rg, []):
                    expanded.add(child_id)
            for rg_id in expanded:
                group_building_vars[(rg_id, day, snum, b_id)].append(var)

    # Enforce travel limits for lecturers
    for (lec_id, day, snum, b_id), vars_curr in lec_building_vars.items():
        snum_next = snum + 1
        for b_next, dist_min in building_distances.items():
            if b_next[0] == b_id and dist_min > 15:
                vars_next = lec_building_vars.get((lec_id, day, snum_next, b_next[1]))
                if vars_next:
                     model.Add(sum(vars_curr) + sum(vars_next) <= 1)

    # Enforce travel limits for student groups
    for (rg_id, day, snum, b_id), vars_curr in group_building_vars.items():
        snum_next = snum + 1
        for b_next, dist_min in building_distances.items():
            if b_next[0] == b_id and dist_min > 15:
                vars_next = group_building_vars.get((rg_id, day, snum_next, b_next[1]))
                if vars_next:
                     model.Add(sum(vars_curr) + sum(vars_next) <= 1)

    # ── Soft Constraint: For split virtual courses, minimize scheduling same course on the same day ──
    # And Hard Constraint: Ensure all split virtual courses use the same room
    units_by_original = defaultdict(list)
    for course in courses:
        units_by_original[course.orig_course_id].append(course.id)

    vars_by_course_and_day = defaultdict(list)
    vars_by_course_and_room = defaultdict(list)
    for (c_id, r_id, t_idx), var in x.items():
        day = ts_day_by_idx[t_idx]
        vars_by_course_and_day[(c_id, day)].append(var)
        vars_by_course_and_room[(c_id, r_id)].append(var)

    for orig_id, v_ids in units_by_original.items():
        if len(v_ids) <= 1:
            continue
        # 1. Day-splitting soft penalty
        for day in timeslots_by_day.keys():
            day_vars = []
            for v_id in v_ids:
                day_vars.extend(vars_by_course_and_day.get((v_id, day), []))
            
            if len(day_vars) > 1:
                multi_slots_active = model.NewBoolVar(f"multi_slots_{orig_id}_d{day}")
                model.Add(sum(day_vars) <= 1).OnlyEnforceIf(multi_slots_active.Not())
                obj_terms.append(multi_slots_active * 100)

        # 2. Same-room hard constraint
        # Collect all room IDs used by variables of these virtual pieces
        course_rooms = set(r_id for (c_id, r_id, t_idx) in x.keys() if c_id in v_ids)
        room_used_vars = []
        for r_id in course_rooms:
            y_room = model.NewBoolVar(f"room_used_orig{orig_id}_r{r_id}")
            room_used_vars.append(y_room)
            for v_id in v_ids:
                v_room_vars = vars_by_course_and_room.get((v_id, r_id), [])
                if v_room_vars:
                    model.Add(sum(v_room_vars) <= y_room)
        
        if room_used_vars:
            model.AddAtMostOne(room_used_vars)

    # ── Hard Constraint 5: STUDENT_MAX_CLASSES_PER_DAY ───────────────────────
    for group in student_groups:
        g_id = group.id
        if g_id not in student_max_per_day:
            continue
        max_cls = student_max_per_day[g_id]
        for day in timeslots_by_day.keys():
            day_start_vars = start_vars_by_group_and_day.get((g_id, day), [])
            if day_start_vars:
                model.Add(sum(day_start_vars) <= max_cls)

    # ── DB-configured MAX_CLASSES_PER_DAY for lecturers ──────────────────────
    for db_const in db_constraints:
        if db_const.constraint_type == 'MAX_CLASSES_PER_DAY':
            l_id        = db_const.parameters.get('lecturer_id')
            max_classes = db_const.parameters.get('max_classes')
            if l_id and max_classes is not None and db_const.is_hard:
                for day in timeslots_by_day.keys():
                    day_vars = start_vars_by_lecturer_and_day.get((l_id, day), [])
                    if day_vars:
                        model.Add(sum(day_vars) <= max_classes)

    # ── Hard Constraint: Campus Travel Time for Lecturers ────────────────────
    # For each lecturer, if they have classes at consecutive slots, they must be at the same campus
    room_campus_map = {r.id: r.campus_id for r in rooms}
    vars_by_lec_slot_campus = defaultdict(list)
    for (c_id, r_id, t_idx), var in x.items():
        course = course_by_id[c_id]
        l_id = course.lecturer_id
        if not l_id:
            continue
        ts = idx_to_ts[t_idx]
        duration = course.duration_slots
        camp_id = room_campus_map[r_id]
        for offset in range(duration):
            slot_ts = idx_to_ts[t_idx + offset]
            vars_by_lec_slot_campus[(l_id, slot_ts.day_of_week, slot_ts.slot_number, camp_id)].append(var)

    campuses_by_lec_day_slot = defaultdict(set)
    for (l_id, day, slot, camp_id) in vars_by_lec_slot_campus.keys():
        campuses_by_lec_day_slot[(l_id, day, slot)].add(camp_id)

    lecturer_ids = {c.lecturer_id for c in courses if c.lecturer_id}
    for l_id in lecturer_ids:
        for day, day_ts_list in timeslots_by_day.items():
            k = len(day_ts_list)
            if k <= 1:
                continue
            sorted_slots = sorted([ts.slot_number for ts in day_ts_list])
            for i in range(len(sorted_slots) - 1):
                s1 = sorted_slots[i]
                s2 = sorted_slots[i+1]
                if s2 - s1 == 1:
                    campuses_s1 = campuses_by_lec_day_slot.get((l_id, day, s1), set())
                    campuses_s2 = campuses_by_lec_day_slot.get((l_id, day, s2), set())
                    for c1 in campuses_s1:
                        for c2 in campuses_s2:
                            if c1 != c2:
                                s1_vars = vars_by_lec_slot_campus[(l_id, day, s1, c1)]
                                s2_vars = vars_by_lec_slot_campus[(l_id, day, s2, c2)]
                                model.Add(sum(s1_vars) + sum(s2_vars) <= 1)

    # ── DB-configured LECTURER_MAX_CONSECUTIVE_SLOTS ──────────────────────────
    lecturer_max_consec = {}
    for db_const in db_constraints:
        if db_const.constraint_type == 'LECTURER_MAX_CONSECUTIVE_SLOTS':
            l_id = db_const.parameters.get('lecturer_id')
            p_max = db_const.parameters.get('max_consecutive')
            if l_id and p_max is not None and db_const.is_hard:
                lecturer_max_consec[int(l_id)] = int(p_max)

    for l_id, max_consec in lecturer_max_consec.items():
        for day, day_ts_list in timeslots_by_day.items():
            k = len(day_ts_list)
            if k <= max_consec:
                continue
            sorted_ts = sorted(day_ts_list, key=lambda ts: ts.slot_number)
            active_vars_by_slot = {}
            for ts in sorted_ts:
                u_idx = ts_to_idx[ts.id]
                overlapping_vars = vars_by_lecturer_and_slot.get((l_id, u_idx), [])
                if overlapping_vars:
                    slot_active_var = model.NewBoolVar(f"consec_active_L{l_id}_d{day}_s{ts.slot_number}")
                    model.Add(slot_active_var == sum(overlapping_vars))
                    active_vars_by_slot[ts.slot_number] = slot_active_var

            slot_numbers = [ts.slot_number for ts in sorted_ts]
            for i in range(len(slot_numbers) - max_consec):
                window_slots = slot_numbers[i:i + max_consec + 1]
                if window_slots[-1] - window_slots[0] == max_consec:
                    window_vars = [active_vars_by_slot[snum] for snum in window_slots if snum in active_vars_by_slot]
                    if len(window_vars) > max_consec:
                        model.Add(sum(window_vars) <= max_consec)

    # Soft: Minimize gaps (only for small datasets — expensive constraint)
    if num_courses <= 30:
        lecturer_gap_weight = 4
        for lecturer in lecturers:
            l_id = lecturer.id
            for day, day_ts_list in timeslots_by_day.items():
                k = len(day_ts_list)
                if k <= 1:
                    continue
                A_L_d_j_list = []
                for j_idx, ts in enumerate(day_ts_list):
                    u_idx  = ts_to_idx[ts.id]
                    A_vars = vars_by_lecturer_and_slot.get((l_id, u_idx), [])
                    active_var = model.NewBoolVar(f"act_L_{l_id}_d{day}_s{j_idx}")
                    if A_vars:
                        model.Add(active_var == sum(A_vars))
                    else:
                        model.Add(active_var == 0)
                    A_L_d_j_list.append(active_var)

                is_active_day = model.NewBoolVar(f"active_L_{l_id}_d{day}")
                model.AddMaxEquality(is_active_day, A_L_d_j_list)

                first_slot = model.NewIntVar(0, k, f"first_L_{l_id}_d{day}")
                last_slot  = model.NewIntVar(0, k, f"last_L_{l_id}_d{day}")
                model.Add(first_slot == 0).OnlyEnforceIf(is_active_day.Not())
                model.Add(last_slot  == 0).OnlyEnforceIf(is_active_day.Not())

                for j_idx, active_var in enumerate(A_L_d_j_list):
                    model.Add(first_slot <= j_idx + 1).OnlyEnforceIf(active_var)
                    model.Add(last_slot  >= j_idx + 1).OnlyEnforceIf(active_var)

                obj_terms.append(
                    (last_slot - first_slot + is_active_day - sum(A_L_d_j_list)) * lecturer_gap_weight
                )

        student_gap_weight = 4
        for group in student_groups:
            g_id = group.id
            for day, day_ts_list in timeslots_by_day.items():
                k = len(day_ts_list)
                if k <= 1:
                    continue
                A_G_d_j_list = []
                for j_idx, ts in enumerate(day_ts_list):
                    u_idx  = ts_to_idx[ts.id]
                    A_vars = vars_by_student_group_and_slot.get((g_id, u_idx), [])
                    active_var = model.NewBoolVar(f"act_G_{g_id}_d{day}_s{j_idx}")
                    if A_vars:
                        model.Add(active_var == sum(A_vars))
                    else:
                        model.Add(active_var == 0)
                    A_G_d_j_list.append(active_var)

                is_active_day = model.NewBoolVar(f"active_G_{g_id}_d{day}")
                model.AddMaxEquality(is_active_day, A_G_d_j_list)

                first_slot = model.NewIntVar(0, k, f"first_G_{g_id}_d{day}")
                last_slot  = model.NewIntVar(0, k, f"last_G_{g_id}_d{day}")
                model.Add(first_slot == 0).OnlyEnforceIf(is_active_day.Not())
                model.Add(last_slot  == 0).OnlyEnforceIf(is_active_day.Not())

                for j_idx, active_var in enumerate(A_G_d_j_list):
                    model.Add(first_slot <= j_idx + 1).OnlyEnforceIf(active_var)
                    model.Add(last_slot  >= j_idx + 1).OnlyEnforceIf(active_var)

                obj_terms.append(
                    (last_slot - first_slot + is_active_day - sum(A_G_d_j_list)) * student_gap_weight
                )

    if obj_terms:
        model.Minimize(sum(obj_terms))

    # ── FIX G2/G4: Seed solver with greedy hints ──────────────────────────────
    hints_added = 0
    for (c_id, r_id, t_idx), var in x.items():
        greedy_val = greedy_result.get((c_id, r_id, t_idx), 0)
        model.AddHint(var, greedy_val)
        hints_added += 1
    logger.info(f"[Solver] Added {hints_added} CP-SAT hints from greedy solution")

    # ── Configure and run solver ──────────────────────────────────────────────
    # For very large problems skip CP-SAT entirely — the greedy result is already
    # good and CP-SAT will time out without improving it at this scale.
    if num_courses > 300 and greedy_assigned > 0:
        pct = round(greedy_assigned / num_courses * 100)
        logger.info(
            f"[Solver] Skipping CP-SAT for large dataset ({num_courses} courses). "
            f"Saving greedy result ({greedy_assigned}/{num_courses} = {pct}%) directly."
        )
        slots_to_create = []
        with transaction.atomic():
            ScheduleSlot.objects.filter(timetable_id=timetable_id).delete()
            for (c_id, r_id, t_idx) in greedy_result:
                course   = course_by_id[c_id]
                duration = course.duration_slots
                for i in range(duration):
                    ts = idx_to_ts[t_idx + i]
                    slots_to_create.append(
                        ScheduleSlot(
                            timetable_id=timetable_id,
                            course_id=course_by_id[c_id].orig_course_id,
                            lecturer_id=course.lecturer_id,
                            room_id=r_id,
                            time_slot_id=ts.id,
                            student_group_id=course.student_group_id,
                        )
                    )
            ScheduleSlot.objects.bulk_create(slots_to_create, batch_size=2000)
        missed = num_courses - greedy_assigned
        gaps_fixed = compact_schedule_gaps(timetable, ctx=ctx)
        return (
            'FEASIBLE',
            f"Greedy solver placed {greedy_assigned}/{num_courses} courses ({pct}%). "
            f"{missed} courses could not be scheduled. Compacted {gaps_fixed} gaps.",
            0,
        )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    # On Windows, PORTFOLIO_SEARCH with multiple workers can ignore the time limit.
    # Use AUTOMATIC with a single worker so the time limit is reliably enforced.
    solver.parameters.search_branching    = cp_model.AUTOMATIC_SEARCH
    solver.parameters.num_search_workers  = 1

    # Stop at first feasible solution for large datasets
    solver.parameters.stop_after_first_solution = (num_courses > 100)

    logger.info(
        f"[Solver] Phase 2: CP-SAT solving {len(x)} variables, "
        f"time_limit={time_limit_seconds}s, "
        f"workers={solver.parameters.num_search_workers}"
    )
    # Instantiate progress callback and run solve
    callback = FirebaseProgressCallback(timetable_id, x)
    status = solver.Solve(model, callback)

    # ── Extract objective value ───────────────────────────────────────────────
    objective_value = None
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        try:
            objective_value = int(solver.ObjectiveValue())
        except Exception:
            objective_value = None

    # ── Save schedule to DB ───────────────────────────────────────────────────
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        slots_to_create = []
        with transaction.atomic():
            ScheduleSlot.objects.filter(timetable_id=timetable_id).delete()
            for (c_id, r_id, t_idx), var in x.items():
                if solver.Value(var) == 1:
                    course = course_by_id[c_id]
                    duration = course.duration_slots
                    for i in range(duration):
                        ts = idx_to_ts[t_idx + i]
                        slots_to_create.append(
                            ScheduleSlot(
                                timetable_id=timetable_id,
                                course_id=course_by_id[c_id].orig_course_id,
                                lecturer_id=course.lecturer_id,
                                room_id=r_id,
                                time_slot_id=ts.id,
                                student_group_id=course.student_group_id,
                            )
                        )
            ScheduleSlot.objects.bulk_create(slots_to_create, batch_size=2000)

        gaps_fixed = compact_schedule_gaps(timetable, ctx=ctx)
        status_str = 'OPTIMAL' if status == cp_model.OPTIMAL else 'FEASIBLE'
        return (
            status_str,
            f"Timetable scheduled successfully. {len(slots_to_create)} slots assigned. "
            f"Objective score: {objective_value}. Compacted {gaps_fixed} gaps. "
            f"(Greedy: {greedy_assigned}/{num_courses} pre-assigned in {t_greedy_elapsed}s)",
            objective_value,
        )

    elif status == cp_model.INFEASIBLE:
        return 'INFEASIBLE', "The scheduling problem is infeasible. Check constraints, rooms, and timeslot capacity.", None
    else:
        # CP-SAT timed out or returned UNKNOWN.
        # ALWAYS fall back to greedy result — even partial is better than nothing.
        if greedy_result:
            pct = round(greedy_assigned / num_courses * 100)
            logger.warning(
                f"[Solver] CP-SAT returned {status} — saving greedy result "
                f"({greedy_assigned}/{num_courses} courses = {pct}%)"
            )
            slots_to_create = []
            with transaction.atomic():
                ScheduleSlot.objects.filter(timetable_id=timetable_id).delete()
                for (c_id, r_id, t_idx) in greedy_result:
                    course   = course_by_id[c_id]
                    duration = course.duration_slots
                    for i in range(duration):
                        ts = idx_to_ts[t_idx + i]
                        slots_to_create.append(
                            ScheduleSlot(
                                timetable_id=timetable_id,
                                course_id=course_by_id[c_id].orig_course_id,
                                lecturer_id=course.lecturer_id,
                                room_id=r_id,
                                time_slot_id=ts.id,
                                student_group_id=course.student_group_id,
                            )
                        )
                ScheduleSlot.objects.bulk_create(slots_to_create, batch_size=2000)
            missed = num_courses - greedy_assigned
            return (
                'FEASIBLE',
                f"Greedy solver placed {greedy_assigned}/{num_courses} courses ({pct}%). "
                f"{missed} courses could not be scheduled (check constraints or data).",
                0,
            )
        return 'ERROR', f"Solver returned {status} and greedy placed 0 courses. Check data integrity.", None
