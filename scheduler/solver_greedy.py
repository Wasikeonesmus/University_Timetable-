"""
solver_greedy.py — Heuristic Greedy Scheduling Engine
======================================================

Implements the Phase-1 greedy pre-assignment that schedules courses
one-by-one in O(C × R × T) time, sorted by most-constrained-first.

The greedy solver:
  1. Sorts courses by fewest valid timeslot starts (most constrained first).
  2. For each course, scores all candidate timeslots (lecturer preferences,
     day-balancing, weekend penalty).
  3. Tries rooms in capacity order; for each (timeslot, room) pair checks:
       - Bitmask occupancy (OccupancyTracker, O(1))
       - Custom forbidden/required rules (CustomRuleSet)
       - Building travel time constraints
       - Campus travel constraints
       - Lecturer consecutive-slot and daily-workload limits
  4. On success, marks the slot as occupied and moves on.
  5. Falls back to a second pass for split "virtual" courses that couldn't
     be placed on a fresh day.

Returns a dict {(course_id, room_id, t_idx): 1} for every placed course.
Unplaced courses are left for the CP-SAT phase (or the relaxed-cap retry).

Design pattern: Strategy — the greedy engine is a concrete strategy for the
scheduling Pipeline; it can be swapped or extended without touching the
orchestrator.
"""

import logging
from collections import defaultdict
from .solver_rules import CustomRuleSet, OccupancyTracker

logger = logging.getLogger(__name__)


def greedy_assign(
    courses: list,
    rooms: list,
    timeslots: list,
    course_valid_start_indices: dict,
    course_orig_valid_indices_count: dict,
    course_durations: dict,
    course_lecturers: dict,
    course_groups: dict,
    course_campuses: dict,
    course_group_sizes: dict,
    course_room_types: dict,
    rooms_by_campus_and_type: dict,
    rooms_by_campus: dict,
    lab_only_course_ids: set,
    course_hard_pref_rooms: dict,
    lecturer_hard_unavailables: dict,
    ts_id_by_idx: dict,
    room_limit: int,
    ctx=None,
) -> dict:
    """
    Fast greedy scheduler.

    Returns: dict {(course_id, room_id, t_idx): 1}

    Args:
        ctx: optional SchedulingContext (from build_scheduling_context).
             When provided, overlap_map and all feature/building/preference
             maps are reused instead of being rebuilt/re-queried here.
    """
    # ── Resolve context ────────────────────────────────────────────────────
    rule_set: CustomRuleSet | None = None
    uni_id = timeslots[0].university_id if timeslots else None

    if ctx is not None:
        ts_day_by_idx               = ctx.ts_day_by_idx
        overlap_map                 = ctx.overlap_map
        lecturer_max_consec         = ctx.lecturer_max_consec
        group_parent_map            = ctx.group_parent_map
        group_children_map          = ctx.group_children_map
        room_features_map           = ctx.room_features_map
        course_required_features_map = ctx.course_required_features_map
        course_additional_groups_map = ctx.course_additional_groups_map
        room_building_map           = ctx.room_building_map
        building_distances          = ctx.building_distances
        lecturer_preferences_prefer  = ctx.lecturer_preferences_prefer
        lecturer_preferences_dislike = ctx.lecturer_preferences_dislike
        lecturer_max_slots_per_day  = ctx.lecturer_max_slots_per_day
        rule_set                    = ctx.rule_set
    else:
        # Standalone call — build everything locally
        ts_day_by_idx = {idx: ts.day_of_week for idx, ts in enumerate(timeslots)}
        from .solver_context import _get_overlap_map
        overlap_map = _get_overlap_map(uni_id, timeslots)

        from .models import (
            Constraint, StudentGroup, Room, Course, BuildingDistance,
            LecturerTimeSlotPreference, Lecturer,
        )

        lecturer_max_consec: dict = {}
        if uni_id:
            for cfg in Constraint.objects.filter(
                university_id=uni_id,
                constraint_type='LECTURER_MAX_CONSECUTIVE_SLOTS',
                is_hard=True,
            ):
                l_id  = cfg.parameters.get('lecturer_id')
                p_max = cfg.parameters.get('max_consecutive')
                if l_id and p_max is not None:
                    lecturer_max_consec[int(l_id)] = int(p_max)

        group_parent_map:   dict = {}
        group_children_map: dict = defaultdict(list)
        if uni_id:
            for g in StudentGroup.objects.filter(
                program__department__faculty__campus__university_id=uni_id
            ).values('id', 'parent_group_id'):
                group_parent_map[g['id']] = g['parent_group_id']
                if g['parent_group_id']:
                    group_children_map[g['parent_group_id']].append(g['id'])

        room_features_map:            dict = defaultdict(set)
        course_required_features_map: dict = defaultdict(set)
        course_additional_groups_map: dict = defaultdict(set)
        room_building_map:            dict = {}
        building_distances:           dict = {}
        lecturer_preferences_prefer:  dict = defaultdict(set)
        lecturer_preferences_dislike: dict = defaultdict(set)
        lecturer_max_slots_per_day:   dict = {}

        if uni_id:
            for rid, fid in Room.features.through.objects.filter(
                room__campus__university_id=uni_id
            ).values_list('room_id', 'roomfeature_id'):
                room_features_map[rid].add(fid)
            for cid, fid in Course.required_features.through.objects.filter(
                course__program__department__faculty__campus__university_id=uni_id
            ).values_list('course_id', 'roomfeature_id'):
                course_required_features_map[cid].add(fid)
            for cid, gid in Course.additional_student_groups.through.objects.filter(
                course__program__department__faculty__campus__university_id=uni_id
            ).values_list('course_id', 'studentgroup_id'):
                course_additional_groups_map[cid].add(gid)
            room_building_map = {
                rid: bid for rid, bid in Room.objects.filter(
                    campus__university_id=uni_id
                ).values_list('id', 'building_id')
            }
            for b1, b2, t in BuildingDistance.objects.filter(
                from_building__campus__university_id=uni_id
            ).values_list('from_building_id', 'to_building_id', 'walking_time_minutes'):
                building_distances[(b1, b2)] = t
            for lid, tsid, pref in LecturerTimeSlotPreference.objects.filter(
                lecturer__department__faculty__campus__university_id=uni_id
            ).values_list('lecturer_id', 'time_slot_id', 'preference_level'):
                if pref == 'prefer':
                    lecturer_preferences_prefer[lid].add(tsid)
                elif pref == 'dislike':
                    lecturer_preferences_dislike[lid].add(tsid)
            lecturer_max_slots_per_day = {
                lid: ms for lid, ms in Lecturer.objects.filter(
                    department__faculty__campus__university_id=uni_id
                ).values_list('id', 'max_slots_per_day')
            }
            custom_constraints = list(Constraint.objects.filter(
                university_id=uni_id, constraint_type='CUSTOM_RULE'
            ))
            rule_set = CustomRuleSet.from_constraints(custom_constraints)
        else:
            rule_set = CustomRuleSet()

    # ── State tracking ─────────────────────────────────────────────────────
    course_scheduled_days:    set  = set()   # (orig_c_id, day_of_week)
    course_assigned_room:     dict = {}      # orig_c_id → r_id (split-virtual same-room)
    assignment:               dict = {}      # (c_id, r_id, t_idx) → 1
    day_class_counts:         dict = defaultdict(int)
    lecturer_scheduled_slots: dict = defaultdict(dict)  # l_id → {u → campus_id}
    lecturer_scheduled_rooms: dict = defaultdict(dict)  # l_id → {u → r_id}
    group_scheduled_rooms:    dict = defaultdict(dict)  # g_id → {u → r_id}
    lecturer_teaching_days:   dict = defaultdict(set)
    lecturer_day_count:       dict = defaultdict(lambda: defaultdict(int))

    tracker = OccupancyTracker(len(timeslots), overlap_map)

    virtual_room_ids:       set  = {r.id for r in rooms if r.is_virtual}
    lecturer_slot_is_virtual: dict = defaultdict(dict)  # l_id → {u → bool}

    # Most-constrained first
    sorted_courses = sorted(
        courses,
        key=lambda c: course_orig_valid_indices_count.get(c.id, 0),
    )

    # ── Main placement loop ────────────────────────────────────────────────

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

        # Eligible rooms (pre-sorted by capacity, waterfall fallback)
        candidate_rooms = rooms_by_campus_and_type.get((campus_id, req_type), [])
        eligible_rooms  = [r for r in candidate_rooms if r.capacity >= group_size]
        if not eligible_rooms:
            all_campus = rooms_by_campus.get(campus_id, [])
            eligible_rooms = [r for r in all_campus if r.capacity >= group_size]
        if not eligible_rooms:
            eligible_rooms = [
                r for r in rooms
                if r.room_type == req_type and r.capacity >= group_size
            ]
            if not eligible_rooms:
                eligible_rooms = [r for r in rooms if r.capacity >= group_size]
        if orig_c_id in lab_only_course_ids:
            eligible_rooms = [r for r in eligible_rooms if r.room_type == 'Lab']
        if orig_c_id in course_hard_pref_rooms:
            pref_set = course_hard_pref_rooms[orig_c_id]
            eligible_rooms = [r for r in eligible_rooms if r.id in pref_set]
        req_feats = course_required_features_map.get(orig_c_id, set())
        if req_feats:
            eligible_rooms = [
                r for r in eligible_rooms
                if req_feats.issubset(room_features_map.get(r.id, set()))
            ]
        if is_virtual and orig_c_id in course_assigned_room:
            assigned_r_id = course_assigned_room[orig_c_id]
            eligible_rooms = [r for r in eligible_rooms if r.id == assigned_r_id]

        num_eligible = len(eligible_rooms)
        if num_eligible > room_limit:
            window_size = num_eligible
            offset      = c_id % max(1, window_size - room_limit + 1)
            eligible_rooms = eligible_rooms[offset: offset + room_limit]

        # Score timeslots
        prefer_set  = lecturer_preferences_prefer.get(lec_id, set())  if lec_id else set()
        dislike_set = lecturer_preferences_dislike.get(lec_id, set()) if lec_id else set()
        slot_scores: list = []
        for t_idx in valid_t_indices:
            day   = ts_day_by_idx[t_idx]
            score = day_class_counts[day] * 100
            if lec_id:
                if day in lecturer_teaching_days[lec_id]:
                    score -= 2000
                if day == 6:
                    score += 3000   # Saturday preferred weekend day
                elif day == 7:
                    score += 10000  # Sunday fallback (used only when Saturday is full)
                ts_ids_tmp = [ts_id_by_idx[t_idx + off] for off in range(duration)]
                if any(tid in dislike_set for tid in ts_ids_tmp):
                    score += 5000
                elif any(tid in prefer_set for tid in ts_ids_tmp):
                    score -= 50
                    if day in (6, 7):
                        score -= 6000
            slot_scores.append((score, t_idx))
        slot_scores.sort()
        sorted_t_indices = [t for _, t in slot_scores]

        # Expand student groups
        related_groups = {group_id}
        for g_add in course_additional_groups_map.get(orig_c_id, set()):
            related_groups.add(g_add)
        expanded_groups: set = set()
        for rg in related_groups:
            expanded_groups.add(rg)
            p_id = group_parent_map.get(rg)
            if p_id:
                expanded_groups.add(p_id)
            for child_id in group_children_map.get(rg, []):
                expanded_groups.add(child_id)

        # ── Placement validity checker (inner) ─────────────────────────────

        def is_placement_possible(t_idx, r_id, combined_mask, ts_ids):
            if rule_set.is_forbidden(orig_c_id, c_id, r_id, t_idx, duration, ts_ids, lec_id, group_id):
                return False
            if not tracker.is_room_free(r_id, combined_mask):
                return False

            # Building travel — lecturer
            if lec_id and r_id in room_building_map:
                lec_b = room_building_map[r_id]
                for adj_offset, same_day_check in ((-1, True), (duration, True)):
                    adj_idx = t_idx + adj_offset
                    if adj_idx in lecturer_scheduled_slots[lec_id]:
                        if ts_day_by_idx.get(adj_idx) == ts_day_by_idx[t_idx]:
                            prev_r = lecturer_scheduled_rooms[lec_id].get(adj_idx)
                            if prev_r and prev_r in room_building_map:
                                other_b = room_building_map[prev_r]
                                if other_b != lec_b:
                                    walk = max(
                                        building_distances.get((other_b, lec_b), 0),
                                        building_distances.get((lec_b, other_b), 0),
                                    )
                                    if walk > 15:
                                        return False

            # Building travel — student groups
            for rg_id in expanded_groups:
                for adj_offset in (-1, duration):
                    adj_idx = t_idx + adj_offset
                    if adj_idx in group_scheduled_rooms[rg_id]:
                        if ts_day_by_idx.get(adj_idx) == ts_day_by_idx[t_idx]:
                            prev_r = group_scheduled_rooms[rg_id].get(adj_idx)
                            if prev_r and prev_r in room_building_map and r_id in room_building_map:
                                prev_b = room_building_map[prev_r]
                                cur_b  = room_building_map[r_id]
                                if prev_b != cur_b:
                                    walk = max(
                                        building_distances.get((prev_b, cur_b), 0),
                                        building_distances.get((cur_b, prev_b), 0),
                                    )
                                    if walk > 15:
                                        return False
            return True

        # ── Try pass 1: fresh day for this orig course ─────────────────────

        placed = False
        course_all_virtual = all(r.id in virtual_room_ids for r in eligible_rooms)

        for t_idx in sorted_t_indices:
            day = ts_day_by_idx[t_idx]
            if (orig_c_id, day) in course_scheduled_days:
                continue

            combined_mask = tracker.get_combined_mask(t_idx, duration)

            if not tracker.is_lecturer_free(lec_id, combined_mask):
                continue
            if any(not tracker.is_group_free(rg_id, combined_mask) for rg_id in expanded_groups):
                continue

            # Campus travel — lecturer
            if lec_id and not course_all_virtual:
                campus_conflict = False
                for adj_offset in (-1, duration):
                    adj_idx = t_idx + adj_offset
                    if adj_idx in lecturer_scheduled_slots[lec_id]:
                        if ts_day_by_idx.get(adj_idx) == day:
                            adj_virtual = lecturer_slot_is_virtual[lec_id].get(adj_idx, False)
                            if not adj_virtual and lecturer_scheduled_slots[lec_id][adj_idx] != campus_id:
                                campus_conflict = True
                                break
                if campus_conflict:
                    continue

            # Max consecutive slots
            span = range(t_idx, t_idx + duration)
            if lec_id and lec_id in lecturer_max_consec:
                max_consec = lecturer_max_consec[lec_id]
                lec_day_set = {
                    u for u in lecturer_scheduled_slots[lec_id]
                    if ts_day_by_idx[u] == day
                }
                lec_day_set.update(span)
                sorted_day = sorted(lec_day_set)
                max_run = cur_run = 1
                for k in range(1, len(sorted_day)):
                    if sorted_day[k] - sorted_day[k - 1] == 1:
                        cur_run += 1
                        max_run = max(max_run, cur_run)
                    else:
                        cur_run = 1
                if max_run > max_consec:
                    continue

            # Daily workload
            if lec_id:
                max_slots = lecturer_max_slots_per_day.get(lec_id, 2)
                if lecturer_day_count[lec_id][day] + duration > max_slots:
                    continue

            ts_ids_for_t_idx = [ts_id_by_idx[t_idx + off] for off in range(duration)]

            for room in eligible_rooms:
                r_id = room.id
                if not is_placement_possible(t_idx, r_id, combined_mask, ts_ids_for_t_idx):
                    continue

                # ── Place the course ───────────────────────────────────────
                assignment[(c_id, r_id, t_idx)] = 1
                tracker.reserve(r_id, lec_id, expanded_groups, combined_mask)
                for u in span:
                    if lec_id:
                        lecturer_scheduled_slots[lec_id][u] = campus_id
                        lecturer_scheduled_rooms[lec_id][u] = r_id
                        lecturer_slot_is_virtual[lec_id][u] = (r_id in virtual_room_ids)
                    for rg_id in expanded_groups:
                        group_scheduled_rooms[rg_id][u] = r_id
                course_scheduled_days.add((orig_c_id, day))
                if is_virtual:
                    course_assigned_room[orig_c_id] = r_id
                day_class_counts[day] += 1
                if lec_id:
                    lecturer_day_count[lec_id][day] += duration
                    lecturer_teaching_days[lec_id].add(day)
                placed = True
                break
            if placed:
                break

        # ── Pass 2: allow any day (fallback for virtual split courses) ─────

        if not placed and is_virtual:
            for t_idx in sorted_t_indices:
                day          = ts_day_by_idx[t_idx]
                span         = range(t_idx, t_idx + duration)
                combined_mask = tracker.get_combined_mask(t_idx, duration)

                if not tracker.is_lecturer_free(lec_id, combined_mask):
                    continue
                if any(not tracker.is_group_free(rg, combined_mask) for rg in expanded_groups):
                    continue

                course_all_virtual2 = all(r.id in virtual_room_ids for r in eligible_rooms)
                if lec_id and not course_all_virtual2:
                    skip = False
                    for adj_offset in (-1, duration):
                        adj_idx = t_idx + adj_offset
                        if adj_idx in lecturer_scheduled_slots[lec_id]:
                            if ts_day_by_idx.get(adj_idx) == day:
                                adj_virtual = lecturer_slot_is_virtual[lec_id].get(adj_idx, False)
                                if not adj_virtual and lecturer_scheduled_slots[lec_id][adj_idx] != campus_id:
                                    skip = True
                                    break
                    if skip:
                        continue

                if lec_id and lec_id in lecturer_max_consec:
                    max_consec = lecturer_max_consec[lec_id]
                    lec_day_set = {
                        u for u in lecturer_scheduled_slots[lec_id]
                        if ts_day_by_idx[u] == day
                    }
                    lec_day_set.update(span)
                    sorted_day = sorted(lec_day_set)
                    max_run = cur_run = 1
                    for k in range(1, len(sorted_day)):
                        if sorted_day[k] - sorted_day[k - 1] == 1:
                            cur_run += 1
                            max_run = max(max_run, cur_run)
                        else:
                            cur_run = 1
                    if max_run > max_consec:
                        continue

                if lec_id:
                    max_slots = lecturer_max_slots_per_day.get(lec_id, 2)
                    if lecturer_day_count[lec_id][day] + duration > max_slots:
                        continue

                ts_ids_for_t_idx = [ts_id_by_idx[t_idx + off] for off in range(duration)]

                for room in eligible_rooms:
                    r_id = room.id
                    if not is_placement_possible(t_idx, r_id, combined_mask, ts_ids_for_t_idx):
                        continue

                    assignment[(c_id, r_id, t_idx)] = 1
                    tracker.reserve(r_id, lec_id, expanded_groups, combined_mask)
                    for u in span:
                        if lec_id:
                            lecturer_scheduled_slots[lec_id][u] = campus_id
                            lecturer_scheduled_rooms[lec_id][u] = r_id
                            lecturer_slot_is_virtual[lec_id][u] = (r_id in virtual_room_ids)
                        for rg_id in expanded_groups:
                            group_scheduled_rooms[rg_id][u] = r_id
                    course_scheduled_days.add((orig_c_id, day))
                    course_assigned_room[orig_c_id] = r_id
                    day_class_counts[day] += 1
                    if lec_id:
                        lecturer_day_count[lec_id][day] += duration
                        lecturer_teaching_days[lec_id].add(day)
                    placed = True
                    break
                if placed:
                    break

        if not placed:
            logger.debug(f"[Greedy] Could not place course {c_id} — left to CP-SAT")

    return assignment
