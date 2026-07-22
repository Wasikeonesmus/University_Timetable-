"""
Standalone (no-Django) equivalence test for CustomRuleSet.is_forbidden
against the original inline is_combination_forbidden closures it replaces.

Run directly with: python3 tests_solver_rules.py
(Deliberately avoids Django's test runner / DB so this can run in CI or
locally without a configured settings module — the class under test has
no Django dependency.)
"""
import random
from collections import defaultdict

try:
    from .solver_rules import CustomRuleSet
except ImportError:
    from solver_rules import CustomRuleSet


def original_is_combination_forbidden(
    orig_c_id, c_id, r_id, t_idx, duration, ts_ids, lec_id, g_id,
    forbidden_lecturer_rooms, forbidden_course_rooms, forbidden_group_rooms,
    forbidden_lecturer_times, forbidden_course_times, forbidden_group_times,
    required_lecturer_rooms, required_course_rooms, required_group_rooms,
    required_lecturer_times, required_course_times, required_group_times,
):
    """Verbatim port of the original closure body, for comparison."""
    if lec_id:
        if r_id in forbidden_lecturer_rooms[lec_id]:
            return True
        if lec_id in required_lecturer_rooms and r_id not in required_lecturer_rooms[lec_id]:
            return True
        if any(tid in forbidden_lecturer_times[lec_id] for tid in ts_ids):
            return True
        if lec_id in required_lecturer_times and any(tid not in required_lecturer_times[lec_id] for tid in ts_ids):
            return True

    if r_id in forbidden_course_rooms[orig_c_id]:
        return True
    if orig_c_id in required_course_rooms and r_id not in required_course_rooms[orig_c_id]:
        return True
    if any(tid in forbidden_course_times[orig_c_id] for tid in ts_ids):
        return True
    if orig_c_id in required_course_times and any(tid not in required_course_times[orig_c_id] for tid in ts_ids):
        return True

    if r_id in forbidden_group_rooms[g_id]:
        return True
    if g_id in required_group_rooms and r_id not in required_group_rooms[g_id]:
        return True
    if any(tid in forbidden_group_times[g_id] for tid in ts_ids):
        return True
    if g_id in required_group_times and any(tid not in required_group_times[g_id] for tid in ts_ids):
        return True

    return False


def run():
    random.seed(7)

    NUM_LEC, NUM_ROOM, NUM_TS, NUM_COURSE, NUM_GROUP = 6, 6, 25, 12, 6

    forbidden_lecturer_rooms = defaultdict(set, {0: {1, 2}, 3: {5}})
    forbidden_course_rooms = defaultdict(set, {2: {0}, 7: {1, 2, 3}})
    forbidden_group_rooms = defaultdict(set, {1: {4}})
    forbidden_lecturer_times = defaultdict(set, {2: {30, 40, 50}})
    forbidden_course_times = defaultdict(set, {4: {10}})
    forbidden_group_times = defaultdict(set, {0: {20}})
    required_lecturer_rooms = {1: {2, 3}}
    required_course_rooms = {}
    required_group_rooms = {3: {0, 1}}
    required_lecturer_times = {}
    required_course_times = {5: {0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130, 140, 150, 160, 170, 180, 190, 200, 210, 220, 230, 240}}
    required_group_times = {}

    rule_set = CustomRuleSet()
    rule_set.forbidden_lecturer_rooms = forbidden_lecturer_rooms
    rule_set.forbidden_course_rooms = forbidden_course_rooms
    rule_set.forbidden_group_rooms = forbidden_group_rooms
    rule_set.forbidden_lecturer_times = forbidden_lecturer_times
    rule_set.forbidden_course_times = forbidden_course_times
    rule_set.forbidden_group_times = forbidden_group_times
    rule_set.required_lecturer_rooms = required_lecturer_rooms
    rule_set.required_course_rooms = required_course_rooms
    rule_set.required_group_rooms = required_group_rooms
    rule_set.required_lecturer_times = required_lecturer_times
    rule_set.required_course_times = required_course_times
    rule_set.required_group_times = required_group_times

    ts_id_by_idx = {i: i * 10 for i in range(NUM_TS)}

    trials = 0
    mismatches = 0
    for _ in range(50000):
        orig_c_id = random.randint(0, NUM_COURSE - 1)
        c_id = orig_c_id
        r_id = random.randint(0, NUM_ROOM - 1)
        duration = random.randint(1, 3)
        t_idx = random.randint(0, NUM_TS - duration - 1)
        ts_ids = [ts_id_by_idx[t_idx + off] for off in range(duration)]
        lec_id = random.choice([None] + list(range(NUM_LEC)))
        g_id = random.randint(0, NUM_GROUP - 1)

        expected = original_is_combination_forbidden(
            orig_c_id, c_id, r_id, t_idx, duration, ts_ids, lec_id, g_id,
            forbidden_lecturer_rooms, forbidden_course_rooms, forbidden_group_rooms,
            forbidden_lecturer_times, forbidden_course_times, forbidden_group_times,
            required_lecturer_rooms, required_course_rooms, required_group_rooms,
            required_lecturer_times, required_course_times, required_group_times,
        )
        actual = rule_set.is_forbidden(orig_c_id, c_id, r_id, t_idx, duration, ts_ids, lec_id, g_id)

        trials += 1
        if expected != actual:
            mismatches += 1
            print(f"MISMATCH: orig_c_id={orig_c_id} r_id={r_id} t_idx={t_idx} dur={duration} "
                  f"lec_id={lec_id} g_id={g_id} expected={expected} actual={actual}")

    print(f"{trials} trials, {mismatches} mismatches")
    assert mismatches == 0, "CustomRuleSet.is_forbidden diverges from original logic!"
    print("PASS: CustomRuleSet.is_forbidden is behaviorally identical to the original closures")


if __name__ == '__main__':
    run()
