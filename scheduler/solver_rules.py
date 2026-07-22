"""
Constraint layer for the timetable solver.

Extracted from solver.py, where an identical ~35-line closure named
`is_combination_forbidden` was duplicated in two places (the greedy
assigner and the CP-SAT model builder), each hand-threading the same
dozen forbidden_*/required_* lookup dicts as free variables.

This module makes that logic:
  - defined once, instead of copy-pasted
  - independently unit-testable without spinning up Django/a DB
  - explicit about its inputs (via CustomRuleSet), instead of relying on
    closure capture over a dozen loose local variables

Behavior is unchanged from the original two closures — see
scheduler/tests_solver_rules.py for an equivalence check against the
original logic.
"""

from collections import defaultdict


class CustomRuleSet:
    """
    Holds the forbidden/required room & time lookups derived from
    CUSTOM_RULE Constraint rows, for lecturers, courses, and student groups.

    This is pure data — no Django imports, no DB access — so it can be
    constructed once (e.g. as part of SchedulingContext) and passed to
    both the greedy assigner and the CP-SAT builder, and can be built by
    hand in tests without a database.
    """

    __slots__ = (
        'forbidden_lecturer_rooms', 'forbidden_course_rooms', 'forbidden_group_rooms',
        'forbidden_lecturer_times', 'forbidden_course_times', 'forbidden_group_times',
        'required_lecturer_rooms', 'required_course_rooms', 'required_group_rooms',
        'required_lecturer_times', 'required_course_times', 'required_group_times',
    )

    def __init__(self):
        self.forbidden_lecturer_rooms = defaultdict(set)
        self.forbidden_course_rooms = defaultdict(set)
        self.forbidden_group_rooms = defaultdict(set)
        self.forbidden_lecturer_times = defaultdict(set)
        self.forbidden_course_times = defaultdict(set)
        self.forbidden_group_times = defaultdict(set)
        self.required_lecturer_rooms = {}
        self.required_course_rooms = {}
        self.required_group_rooms = {}
        self.required_lecturer_times = {}
        self.required_course_times = {}
        self.required_group_times = {}

    @classmethod
    def from_constraints(cls, db_constraints):
        """
        Build a CustomRuleSet from an iterable of Constraint objects (any
        constraint_type — this filters for 'CUSTOM_RULE' itself, matching
        the original inline filtering behavior, so callers can pass the
        full db_constraints list without pre-filtering).

        Each CUSTOM_RULE's `.parameters` dict is expected to have the shape
        {target_type, target_id, rule_type, rule_value}.
        """
        rs = cls()
        for rule in db_constraints:
            if rule.constraint_type != 'CUSTOM_RULE':
                continue
            p = rule.parameters
            target_type = p.get('target_type')
            target_id = p.get('target_id')
            rule_type = p.get('rule_type')
            rule_value = p.get('rule_value')

            if not target_id or not rule_value:
                continue

            val_set = set()
            if isinstance(rule_value, list):
                val_set.update(int(v) for v in rule_value)
            else:
                try:
                    val_set.add(int(rule_value))
                except (ValueError, TypeError):
                    continue

            try:
                target_id = int(target_id)
            except (ValueError, TypeError):
                continue

            if rule_type == 'FORBID_ROOM':
                if target_type == 'LECTURER':
                    rs.forbidden_lecturer_rooms[target_id].update(val_set)
                elif target_type == 'COURSE':
                    rs.forbidden_course_rooms[target_id].update(val_set)
                elif target_type == 'STUDENT_GROUP':
                    rs.forbidden_group_rooms[target_id].update(val_set)
            elif rule_type == 'FORBID_TIME':
                if target_type == 'LECTURER':
                    rs.forbidden_lecturer_times[target_id].update(val_set)
                elif target_type == 'COURSE':
                    rs.forbidden_course_times[target_id].update(val_set)
                elif target_type == 'STUDENT_GROUP':
                    rs.forbidden_group_times[target_id].update(val_set)
            elif rule_type == 'REQUIRE_ROOM':
                if target_type == 'LECTURER':
                    rs.required_lecturer_rooms[target_id] = val_set
                elif target_type == 'COURSE':
                    rs.required_course_rooms[target_id] = val_set
                elif target_type == 'STUDENT_GROUP':
                    rs.required_group_rooms[target_id] = val_set
            elif rule_type == 'REQUIRE_TIME':
                if target_type == 'LECTURER':
                    rs.required_lecturer_times[target_id] = val_set
                elif target_type == 'COURSE':
                    rs.required_course_times[target_id] = val_set
                elif target_type == 'STUDENT_GROUP':
                    rs.required_group_times[target_id] = val_set
        return rs

    def is_forbidden(self, orig_c_id, c_id, r_id, t_idx, duration, ts_ids,
                     lec_id, group_id):
        """
        Returns True if placing course c_id (whose original/parent id is
        orig_c_id, taught by lec_id, attended by group_id) in room r_id at
        timeslot start t_idx (spanning `duration` slots, whose timeslot ids
        are precomputed in ts_ids) would violate a custom rule.

        ts_ids is passed in rather than recomputed here (PERF FIX A from
        the earlier optimization pass): it depends only on (t_idx, duration),
        not on r_id, and callers iterate multiple rooms per timeslot — so
        computing it once per timeslot and reusing it here avoids the
        redundant rebuild that existed in the original duplicated closures.
        """
        # Lecturer checks
        if lec_id:
            if r_id in self.forbidden_lecturer_rooms[lec_id]:
                return True
            if lec_id in self.required_lecturer_rooms and r_id not in self.required_lecturer_rooms[lec_id]:
                return True
            if any(tid in self.forbidden_lecturer_times[lec_id] for tid in ts_ids):
                return True
            if lec_id in self.required_lecturer_times and any(
                tid not in self.required_lecturer_times[lec_id] for tid in ts_ids
            ):
                return True

        # Course checks (using orig_c_id — the parent course for split/virtual sessions)
        if r_id in self.forbidden_course_rooms[orig_c_id]:
            return True
        if orig_c_id in self.required_course_rooms and r_id not in self.required_course_rooms[orig_c_id]:
            return True
        if any(tid in self.forbidden_course_times[orig_c_id] for tid in ts_ids):
            return True
        if orig_c_id in self.required_course_times and any(
            tid not in self.required_course_times[orig_c_id] for tid in ts_ids
        ):
            return True

        # Student group checks (including parent-child and shared electives —
        # callers are expected to call this once per relevant group_id if a
        # course has expanded/related groups; this method checks exactly one)
        if r_id in self.forbidden_group_rooms[group_id]:
            return True
        if group_id in self.required_group_rooms and r_id not in self.required_group_rooms[group_id]:
            return True
        if any(tid in self.forbidden_group_times[group_id] for tid in ts_ids):
            return True
        if group_id in self.required_group_times and any(
            tid not in self.required_group_times[group_id] for tid in ts_ids
        ):
            return True

        return False


class OccupancyTracker:
    """
    Tracks and checks occupancy of rooms, lecturers, and student groups.
    Encapsulates the bitmask occupancy representation and helper checks.
    """
    def __init__(self, num_ts, overlap_map):
        self.room_occupied = defaultdict(int)
        self.lecturer_occupied = defaultdict(int)
        self.group_occupied = defaultdict(int)
        
        # Precompute overlap bitmasks for timeslots (Tier 2 bitmask check)
        self.overlap_mask = [0] * num_ts
        for u in range(num_ts):
            mask = 0
            for ou in overlap_map[u]:
                mask |= (1 << ou)
            self.overlap_mask[u] = mask

    def get_combined_mask(self, t_idx, duration):
        mask = 0
        for off in range(duration):
            mask |= self.overlap_mask[t_idx + off]
        return mask

    def is_room_free(self, r_id, combined_mask):
        return (self.room_occupied[r_id] & combined_mask) == 0

    def is_lecturer_free(self, lec_id, combined_mask):
        if not lec_id:
            return True
        return (self.lecturer_occupied[lec_id] & combined_mask) == 0

    def is_group_free(self, group_id, combined_mask):
        return (self.group_occupied[group_id] & combined_mask) == 0

    def reserve(self, r_id, lec_id, expanded_groups, combined_mask):
        self.room_occupied[r_id] |= combined_mask
        if lec_id:
            self.lecturer_occupied[lec_id] |= combined_mask
        for g_id in expanded_groups:
            self.group_occupied[g_id] |= combined_mask

