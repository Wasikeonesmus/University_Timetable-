"""
solver_compactor.py — Post-Generation Schedule Gap Compactor
=============================================================

Runs after slots have been written to the database and eliminates
schedule gaps for students and lecturers via local search.

Strategy (2-phase local search):
  Phase 1 — Student-group gaps:
    For each (group, day) pair that has gaps between the first and last
    class of the day, try to move later classes forward into the gap.
    Repeat for up to 5 passes or until no improvement is found.
  Phase 2 — Lecturer gaps:
    Same logic applied across (lecturer, day) pairs.

A move is only accepted when:
  - The target timeslot is free for the room, lecturer, and all affected groups
  - No campus/building travel-time constraint would be violated
  - The lecturer's consecutive-slot and daily-workload limits are respected
  - Required room features are still present

All accepted moves are flushed to the DB in a topological order that
avoids transient unique-constraint violations (earlier slot freed before
new slot claimed).

Design pattern: Strategy (the gap-fixing logic is a reusable post-processor
pluggable after either the greedy or the CP-SAT solving phase).
"""

import logging
from collections import defaultdict
from django.db import transaction

logger = logging.getLogger(__name__)


def compact_schedule_gaps(timetable, ctx=None):
    """
    Post-processing pass that eliminates student/lecturer schedule gaps.

    Args:
        timetable: the Timetable object being compacted.
        ctx:       optional SchedulingContext (build_scheduling_context()).
                   When provided all feature/building/preference/overlap maps
                   are reused instead of being re-queried from scratch.

    Returns: int — number of gaps fixed.
    """
    from .models import ScheduleSlot, TimeSlot

    uni = timetable.semester.university

    slots = list(
        ScheduleSlot.objects.filter(timetable=timetable)
        .select_related('time_slot', 'room')
        .order_by('student_group_id', 'time_slot__day_of_week', 'time_slot__slot_number')
    )

    if not slots:
        return 0

    if len(slots) > 20_000:
        logger.info(
            f"[Compaction] Timetable has {len(slots)} slots (>20000) — skipping."
        )
        return 0

    if ctx is None:
        from .solver_context import build_scheduling_context
        all_ts = list(
            TimeSlot.objects.filter(university=uni).order_by('day_of_week', 'slot_number')
        )
        ctx = build_scheduling_context(uni, all_ts)

    # Unpack context
    ts_by_day_pos               = ctx.ts_by_day_pos
    ts_to_idx                   = ctx.ts_to_idx
    idx_to_ts                   = ctx.idx_to_ts
    overlap_map                 = ctx.overlap_map
    timeslots_by_day            = ctx.timeslots_by_day
    group_parent_map            = ctx.group_parent_map
    group_children_map          = ctx.group_children_map
    group_conflict_sets         = ctx.group_conflict_sets
    lecturer_max_consec         = ctx.lecturer_max_consec
    room_features_map           = ctx.room_features_map
    course_required_features_map = ctx.course_required_features_map
    course_additional_groups_map = ctx.course_additional_groups_map
    room_building_map           = ctx.room_building_map
    building_distances          = ctx.building_distances
    lecturer_max_slots_per_day  = ctx.lecturer_max_slots_per_day

    # ---- In-memory occupation maps ----
    room_ts_map = {(s.room_id, s.time_slot_id): s for s in slots}
    lec_ts_map  = {(s.lecturer_id, s.time_slot_id): s for s in slots}

    slot_occupied_groups: dict = {}
    for s in slots:
        groups = {s.student_group_id}
        for g_add in course_additional_groups_map.get(s.course_id, set()):
            groups.add(g_add)
        expanded: set = set()
        for g in groups:
            expanded.update(group_conflict_sets.get(g, {g}))
        slot_occupied_groups[s.id] = expanded

    grp_ts_map: dict = {}
    for s in slots:
        for g in slot_occupied_groups[s.id]:
            grp_ts_map[(g, s.time_slot_id)] = s

    # ── Move-validity checker ───────────────────────────────────────────────

    def is_move_valid(slot, target_ts) -> bool:
        target_ts_id  = target_ts.id
        target_ts_idx = ts_to_idx[target_ts_id]
        target_slot_num = target_ts.slot_number
        day    = target_ts.day_of_week
        l_id   = slot.lecturer_id
        r_id   = slot.room_id
        campus_id = slot.room.campus_id

        # Required room features
        req_feats = course_required_features_map.get(slot.course_id, set())
        if req_feats and not req_feats.issubset(room_features_map.get(r_id, set())):
            return False

        # Overlap conflict check
        for ou_idx in overlap_map[target_ts_idx]:
            ou_ts_id = idx_to_ts[ou_idx].id

            existing = room_ts_map.get((r_id, ou_ts_id))
            if existing and existing.id != slot.id:
                return False

            if l_id:
                existing = lec_ts_map.get((l_id, ou_ts_id))
                if existing and existing.id != slot.id:
                    return False

            for rg in slot_occupied_groups[slot.id]:
                existing = grp_ts_map.get((rg, ou_ts_id))
                if existing and existing.id != slot.id:
                    return False

        prev_ts = ts_by_day_pos.get((day, target_slot_num - 1))
        next_ts = ts_by_day_pos.get((day, target_slot_num + 1))

        # Campus travel — lecturer
        if l_id:
            for adj_ts in (prev_ts, next_ts):
                if adj_ts:
                    existing = lec_ts_map.get((l_id, adj_ts.id))
                    if existing and existing.id != slot.id:
                        if not existing.room.is_virtual and not slot.room.is_virtual:
                            if existing.room.campus_id != campus_id:
                                return False

        # Building travel — lecturer
        if l_id and r_id in room_building_map:
            cur_b = room_building_map[r_id]
            for adj_ts in (prev_ts, next_ts):
                if adj_ts:
                    existing = lec_ts_map.get((l_id, adj_ts.id))
                    if existing and existing.id != slot.id:
                        if not existing.room.is_virtual and not slot.room.is_virtual:
                            if existing.room_id in room_building_map:
                                other_b = room_building_map[existing.room_id]
                                if other_b != cur_b:
                                    walk = max(
                                        building_distances.get((cur_b, other_b), 0),
                                        building_distances.get((other_b, cur_b), 0),
                                    )
                                    if walk > 15:
                                        return False

        # Building travel — student groups
        for rg in slot_occupied_groups[slot.id]:
            for adj_ts in (prev_ts, next_ts):
                if adj_ts:
                    existing = grp_ts_map.get((rg, adj_ts.id))
                    if existing and existing.id != slot.id:
                        if not existing.room.is_virtual and not slot.room.is_virtual:
                            if existing.room_id in room_building_map and r_id in room_building_map:
                                other_b = room_building_map[existing.room_id]
                                cur_b   = room_building_map[r_id]
                                if other_b != cur_b:
                                    walk = max(
                                        building_distances.get((cur_b, other_b), 0),
                                        building_distances.get((other_b, cur_b), 0),
                                    )
                                    if walk > 15:
                                        return False

        # Consecutive-slot limit — lecturer
        if l_id and l_id in lecturer_max_consec:
            max_consec = lecturer_max_consec[l_id]
            day_slots: set = {target_slot_num}
            for ts in timeslots_by_day[day]:
                existing = lec_ts_map.get((l_id, ts.id))
                if existing and existing.id != slot.id:
                    day_slots.add(ts.slot_number)
            sorted_day = sorted(day_slots)
            cur_run = max_run = 1
            for k in range(1, len(sorted_day)):
                if sorted_day[k] - sorted_day[k - 1] == 1:
                    cur_run += 1
                    max_run = max(max_run, cur_run)
                else:
                    cur_run = 1
            if max_run > max_consec:
                return False

        # Daily workload — lecturer
        if l_id:
            max_slots   = lecturer_max_slots_per_day.get(l_id, 4)
            day_occupied = sum(
                1 for ts in timeslots_by_day[day]
                if (existing := lec_ts_map.get((l_id, ts.id))) and existing.id != slot.id
            )
            if day_occupied + 1 > max_slots:
                return False

        return True

    # ── Helper: apply a move in-memory ─────────────────────────────────────

    def apply_move(slot, target_ts, old_ts_id, old_time_slot_ids, modified_slots):
        if slot.id not in old_time_slot_ids:
            old_time_slot_ids[slot.id] = old_ts_id

        room_ts_map.pop((slot.room_id, old_ts_id), None)
        if slot.lecturer_id:
            lec_ts_map.pop((slot.lecturer_id, old_ts_id), None)
        for g in slot_occupied_groups[slot.id]:
            grp_ts_map.pop((g, old_ts_id), None)

        slot.time_slot    = target_ts
        slot.time_slot_id = target_ts.id
        modified_slots[slot.id] = slot

        room_ts_map[(slot.room_id, target_ts.id)] = slot
        if slot.lecturer_id:
            lec_ts_map[(slot.lecturer_id, target_ts.id)] = slot
        for g in slot_occupied_groups[slot.id]:
            grp_ts_map[(g, target_ts.id)] = slot

    # ── Phase 1: Student-group gap compaction ───────────────────────────────

    fixed           = 0
    max_passes      = 5
    modified_slots: dict  = {}
    old_time_slot_ids: dict = {}

    group_day_slots: dict = defaultdict(list)
    for s in slots:
        group_day_slots[(s.student_group_id, s.time_slot.day_of_week)].append(s)

    for _ in range(max_passes):
        improved = False
        for (gid, day), day_slots in group_day_slots.items():
            day_slots.sort(key=lambda s: s.time_slot.slot_number)
            slot_nums = [s.time_slot.slot_number for s in day_slots]
            if len(slot_nums) < 2:
                continue
            first, last = slot_nums[0], slot_nums[-1]
            gaps = set(range(first, last + 1)) - set(slot_nums)
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
                    apply_move(slot_to_move, target_ts, old_ts_id, old_time_slot_ids, modified_slots)
                    day_slots = [s for s in day_slots if s.id != slot_to_move.id]
                    day_slots.append(slot_to_move)
                    day_slots.sort(key=lambda s: s.time_slot.slot_number)
                    group_day_slots[(gid, day)] = day_slots
                    fixed   += 1
                    improved = True
        if not improved:
            break

    # ── Phase 2: Lecturer gap compaction ───────────────────────────────────

    lecturer_day_slots: dict = defaultdict(list)
    for s in slots:
        if s.lecturer_id:
            lecturer_day_slots[(s.lecturer_id, s.time_slot.day_of_week)].append(s)

    for _ in range(max_passes):
        improved = False
        for (lid, day), day_slots in lecturer_day_slots.items():
            day_slots.sort(key=lambda s: s.time_slot.slot_number)
            slot_nums = [s.time_slot.slot_number for s in day_slots]
            if len(slot_nums) < 2:
                continue
            first, last = slot_nums[0], slot_nums[-1]
            gaps = set(range(first, last + 1)) - set(slot_nums)
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
                    apply_move(slot_to_move, target_ts, old_ts_id, old_time_slot_ids, modified_slots)
                    day_slots = [s for s in day_slots if s.id != slot_to_move.id]
                    day_slots.append(slot_to_move)
                    day_slots.sort(key=lambda s: s.time_slot.slot_number)
                    lecturer_day_slots[(lid, day)] = day_slots
                    fixed   += 1
                    improved = True
        if not improved:
            break

    # ── Flush to DB in safe topological order ──────────────────────────────

    if modified_slots:
        with transaction.atomic():
            remaining = list(modified_slots.values())
            while remaining:
                safe_slot = None
                for s in remaining:
                    blocked = any(
                        other.id != s.id
                        and old_time_slot_ids.get(other.id) == s.time_slot_id
                        for other in remaining
                    )
                    if not blocked:
                        safe_slot = s
                        break
                if safe_slot is None:
                    logger.warning(
                        "[Compaction] Dependency cycle detected — falling back to first slot."
                    )
                    safe_slot = remaining[0]
                safe_slot.save(update_fields=['time_slot'])
                remaining.remove(safe_slot)

    logger.info(f"[Compaction] Fixed {fixed} gaps across student groups and lecturers.")
    return fixed
