"""
solver_cpsat.py — CP-SAT Constraint Programming Solver Phase
=============================================================

Encapsulates everything that touches Google OR-Tools CP-SAT:
  - FirebaseProgressCallback: throttled progress reporter
  - build_cpsat_model():      compile variables + constraints + objective
  - run_cpsat_solver():       configure solver, execute, extract solution

The CP-SAT phase runs only when the greedy engine failed to place every
course (typically for small datasets where optimality matters, or when
custom constraints are tight).  It is warm-started from the greedy hints
so it converges fast even when it does run.

Design pattern: Strategy — the CP-SAT engine is a concrete strategy for the
scheduling Pipeline; the orchestrator (solver.py) decides whether to invoke
it based on greedy coverage and dataset size.
"""

import time
import logging
import multiprocessing
from collections import defaultdict
from ortools.sat.python import cp_model

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Firebase Progress Callback
# ──────────────────────────────────────────────────────────────────────────────

class FirebaseProgressCallback(cp_model.CpSolverSolutionCallback):
    """
    CP-SAT solution callback that pushes throttled progress reports to
    Firebase Realtime Database during solving.

    Updates are throttled to at most once every 1.5 s to avoid flooding
    the database with writes on fast hardware.
    """

    def __init__(self, timetable_id: int, variables_dict: dict):
        cp_model.CpSolverSolutionCallback.__init__(self)
        self.timetable_id   = timetable_id
        self.variables_dict = variables_dict
        self.solution_count = 0
        self.last_update_time = time.time()

    def on_solution_callback(self):
        self.solution_count += 1
        now = time.time()
        if now - self.last_update_time >= 1.5:
            self.last_update_time = now
            try:
                from .firebase_service import update_generation_status
                update_generation_status(self.timetable_id, {
                    'status':           'SOLVING',
                    'message':          f'Solving... Found {self.solution_count} feasible solution(s).',
                    'courses_scheduled': self.solution_count,
                    'solver_score':     int(self.ObjectiveValue()),
                    'hard_conflicts':   0,
                    'soft_conflicts':   0,
                })
            except Exception:
                pass


# ──────────────────────────────────────────────────────────────────────────────
# Model Builder
# ──────────────────────────────────────────────────────────────────────────────

def build_cpsat_model(
    courses: list,
    rooms: list,
    timeslots: list,
    course_valid_start_indices: dict,
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
    course_soft_pref_rooms: dict,
    lecturer_hard_unavailables: dict,
    lecturer_soft_unavailables: dict,
    student_max_per_day: dict,
    room_limit: int,
    rule_set,
    ctx,
    lecturers: list,
    student_groups: list,
):
    """
    Build and return a CP-SAT model and the associated variable/index structures.

    Returns a dict with keys:
        'model', 'x', 'is_scheduled_vars', 'obj_terms',
        'vars_by_room_and_slot', 'vars_by_lecturer_and_slot',
        'vars_by_student_group_and_slot', 'vars_by_course',
        'start_vars_by_group_and_day', 'start_vars_by_lecturer_and_day',
        'virtual_room_ids', 'course_by_id', 'room_building_map',
        'lecturer_max_slots_per_day', 'lecturer_preferences_prefer',
        'lecturer_preferences_dislike',
    """
    model = cp_model.CpModel()

    # Context aliases
    overlap_map                  = ctx.overlap_map
    ts_to_idx                    = ctx.ts_to_idx
    idx_to_ts                    = ctx.idx_to_ts
    ts_id_by_idx                 = ctx.ts_id_by_idx
    ts_day_by_idx                = ctx.ts_day_by_idx
    ts_is_evening_by_idx         = ctx.ts_is_evening_by_idx
    timeslots_by_day             = ctx.timeslots_by_day
    room_features_map            = ctx.room_features_map
    course_required_features_map = ctx.course_required_features_map
    course_additional_groups_map = ctx.course_additional_groups_map
    room_building_map            = ctx.room_building_map
    building_distances           = ctx.building_distances
    lecturer_preferences_prefer  = ctx.lecturer_preferences_prefer
    lecturer_preferences_dislike = ctx.lecturer_preferences_dislike
    lecturer_max_slots_per_day   = ctx.lecturer_max_slots_per_day
    db_constraints               = ctx.db_constraints
    group_parent_map             = ctx.group_parent_map
    group_children_map           = ctx.group_children_map

    virtual_room_ids: set = {r.id for r in rooms if r.is_virtual}
    course_by_id: dict    = {c.id: c for c in courses}

    x: dict                             = {}
    vars_by_room_and_slot               = defaultdict(list)
    vars_by_lecturer_and_slot           = defaultdict(list)
    vars_by_student_group_and_slot      = defaultdict(list)
    vars_by_course                      = defaultdict(list)
    start_vars_by_group_and_day         = defaultdict(list)
    start_vars_by_lecturer_and_day      = defaultdict(list)
    obj_terms: list                     = []

    evening_penalty_weight = 5
    room_mismatch_penalty  = 15
    avail_penalty_weight   = 20

    # ── Decision variable creation ─────────────────────────────────────────
    for course in courses:
        c_id          = course.id
        duration      = course_durations[c_id]
        valid_t_indices = course_valid_start_indices[c_id]
        lec_id        = course_lecturers[c_id]
        group_id      = course_groups[c_id]
        course_campus_id = course_campuses[c_id]
        group_size    = course_group_sizes[c_id]
        req_room_type = course_room_types[c_id]
        orig_c_id     = course.orig_course_id

        # Expand groups once per course
        _related = {group_id}
        for g_add in course_additional_groups_map.get(orig_c_id, set()):
            _related.add(g_add)
        course_expanded_groups: set = set()
        for _rg in _related:
            course_expanded_groups.add(_rg)
            _p = group_parent_map.get(_rg)
            if _p:
                course_expanded_groups.add(_p)
            for _ch in group_children_map.get(_rg, []):
                course_expanded_groups.add(_ch)

        # Eligible rooms — same waterfall as greedy
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
        req_feats = course_required_features_map.get(orig_c_id, set())
        if req_feats:
            eligible_rooms = [
                r for r in eligible_rooms
                if req_feats.issubset(room_features_map.get(r.id, set()))
            ]
        eligible_rooms = sorted(eligible_rooms, key=lambda r: r.capacity)
        num_eligible   = len(eligible_rooms)
        if num_eligible <= room_limit:
            selected_rooms = eligible_rooms
        else:
            window_size    = num_eligible
            offset         = c_id % max(1, window_size - room_limit + 1)
            selected_rooms = eligible_rooms[offset: offset + room_limit]

        filtered_t_indices = valid_t_indices

        # Pre-calculate timeslot penalties
        ts_penalties: dict = {}
        for t_idx in filtered_t_indices:
            penalty = 0
            day = ts_day_by_idx[t_idx]
            if day == 7:
                penalty += 200  # Prefer Saturday over Sunday
            evening_count = sum(
                1 for i in range(duration) if ts_is_evening_by_idx[t_idx + i]
            )
            if evening_count > 0:
                penalty += evening_penalty_weight * evening_count
            if lec_id and lec_id in lecturer_soft_unavailables:
                for weight, unavail_set in lecturer_soft_unavailables[lec_id]:
                    if any(ts_id_by_idx[t_idx + off] in unavail_set for off in range(duration)):
                        penalty += avail_penalty_weight
            if lec_id and lec_id in lecturer_hard_unavailables:
                unavail_set = lecturer_hard_unavailables[lec_id]
                if any(ts_id_by_idx[t_idx + off] in unavail_set for off in range(duration)):
                    penalty += 5000
            if lec_id:
                prefer_set  = lecturer_preferences_prefer.get(lec_id, set())
                dislike_set = lecturer_preferences_dislike.get(lec_id, set())
                if any(ts_id_by_idx[t_idx + off] in prefer_set for off in range(duration)):
                    penalty -= 15
                if any(ts_id_by_idx[t_idx + off] in dislike_set for off in range(duration)):
                    penalty += 15
            ts_penalties[t_idx] = penalty

        soft_pref_weight, soft_pref_rooms = course_soft_pref_rooms.get(orig_c_id, (0, None))

        # Build ts_ids once per (t_idx, duration) — reused per room
        ts_ids_by_t_idx = {
            t_idx: [ts_id_by_idx[t_idx + off] for off in range(duration)]
            for t_idx in filtered_t_indices
        }

        for room in selected_rooms:
            r_id = room.id
            room_mismatch_penalty_val = room_mismatch_penalty if req_room_type != room.room_type else 0
            pref_weight = -soft_pref_weight if (soft_pref_rooms and r_id in soft_pref_rooms) else 0
            hard_pref_penalty_val = 0
            if orig_c_id in course_hard_pref_rooms:
                if r_id not in course_hard_pref_rooms[orig_c_id]:
                    hard_pref_penalty_val = 3000

            for t_idx in filtered_t_indices:
                if rule_set.is_forbidden(
                    orig_c_id, c_id, r_id, t_idx, duration,
                    ts_ids_by_t_idx[t_idx], lec_id, group_id,
                ):
                    continue

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

                total_penalty = (
                    ts_penalties[t_idx] + room_mismatch_penalty_val
                    + pref_weight + hard_pref_penalty_val
                )
                if total_penalty != 0:
                    obj_terms.append(var * total_penalty)

    # ── Hard Constraint 1: Each course scheduled at most once ──────────────
    is_scheduled_vars: dict = {}
    for course in courses:
        c_id = course.id
        course_vars = vars_by_course.get(c_id, [])
        if not course_vars:
            logger.warning(
                f"[Solver] Course '{course.code}' (ID={c_id}) has no valid "
                f"room/slot combinations. Skipping."
            )
            continue
        is_scheduled = model.NewBoolVar(f"scheduled_c{c_id}")
        model.Add(sum(course_vars) == is_scheduled)
        is_scheduled_vars[c_id] = is_scheduled
        obj_terms.append((1 - is_scheduled) * 20_000)

    # ── Hard Constraint 2: Room uniqueness per timeslot ────────────────────
    for (r_id, u_idx), overlapping in vars_by_room_and_slot.items():
        if len(overlapping) > 1:
            model.AddAtMostOne(overlapping)

    # ── Hard Constraint 3: Lecturer uniqueness per timeslot ────────────────
    for (l_id, u_idx), overlapping in vars_by_lecturer_and_slot.items():
        if len(overlapping) > 1:
            model.AddAtMostOne(overlapping)

    # ── Hard Constraint 4: Student group uniqueness per timeslot ───────────
    for (g_id, u_idx), overlapping in vars_by_student_group_and_slot.items():
        if len(overlapping) > 1:
            model.AddAtMostOne(overlapping)

    # ── Soft: Lecturer Daily Workload ──────────────────────────────────────
    vars_by_lecturer_and_day: dict = defaultdict(list)
    for (c_id, r_id, t_idx), var in x.items():
        l_id = course_lecturers[c_id]
        if l_id:
            day = ts_day_by_idx[t_idx]
            vars_by_lecturer_and_day[(l_id, day)].append(var * course_durations[c_id])

    for lec in lecturers:
        l_id      = lec.id
        max_slots = lecturer_max_slots_per_day.get(l_id, 2)
        for day in timeslots_by_day.keys():
            day_vars = vars_by_lecturer_and_day.get((l_id, day), [])
            if day_vars:
                model.Add(sum(day_vars) <= max_slots)

    # ── Hard: Building Travel Time (Lecturers & Groups) ────────────────────
    lec_building_vars:   dict = defaultdict(list)
    group_building_vars: dict = defaultdict(list)
    for (c_id, r_id, t_idx), var in x.items():
        if r_id in virtual_room_ids:
            continue
        lec_id   = course_lecturers[c_id]
        group_id = course_groups[c_id]
        b_id     = room_building_map.get(r_id)
        if not b_id:
            continue
        duration = course_durations[c_id]
        for offset in range(duration):
            ts   = idx_to_ts[t_idx + offset]
            day  = ts.day_of_week
            snum = ts.slot_number
            if lec_id:
                lec_building_vars[(lec_id, day, snum, b_id)].append(var)
            related: set = {group_id}
            for g_add in course_additional_groups_map.get(course_by_id[c_id].orig_course_id, set()):
                related.add(g_add)
            expanded: set = set()
            for rg in related:
                expanded.add(rg)
                p_id = group_parent_map.get(rg)
                if p_id:
                    expanded.add(p_id)
                for child_id in group_children_map.get(rg, []):
                    expanded.add(child_id)
            for rg_id in expanded:
                group_building_vars[(rg_id, day, snum, b_id)].append(var)

    for (lec_id, day, snum, b_id), vars_curr in lec_building_vars.items():
        snum_next = snum + 1
        for b_pair, dist_min in building_distances.items():
            if b_pair[0] == b_id and dist_min > 15:
                vars_next = lec_building_vars.get((lec_id, day, snum_next, b_pair[1]))
                if vars_next:
                    model.Add(sum(vars_curr) + sum(vars_next) <= 1)

    for (rg_id, day, snum, b_id), vars_curr in group_building_vars.items():
        snum_next = snum + 1
        for b_pair, dist_min in building_distances.items():
            if b_pair[0] == b_id and dist_min > 15:
                vars_next = group_building_vars.get((rg_id, day, snum_next, b_pair[1]))
                if vars_next:
                    model.Add(sum(vars_curr) + sum(vars_next) <= 1)

    # ── Soft: Minimize same-day placement for split virtual courses ─────────
    units_by_original: dict = defaultdict(list)
    for course in courses:
        units_by_original[course.orig_course_id].append(course.id)

    vars_by_course_and_day:  dict = defaultdict(list)
    vars_by_course_and_room: dict = defaultdict(list)
    for (c_id, r_id, t_idx), var in x.items():
        day = ts_day_by_idx[t_idx]
        vars_by_course_and_day[(c_id, day)].append(var)
        vars_by_course_and_room[(c_id, r_id)].append(var)

    for orig_id, v_ids in units_by_original.items():
        if len(v_ids) <= 1:
            continue
        for day in timeslots_by_day.keys():
            day_vars: list = []
            for v_id in v_ids:
                day_vars.extend(vars_by_course_and_day.get((v_id, day), []))
            if len(day_vars) > 1:
                multi = model.NewBoolVar(f"multi_slots_{orig_id}_d{day}")
                model.Add(sum(day_vars) <= 1).OnlyEnforceIf(multi.Not())
                obj_terms.append(multi * 100)

        course_rooms: set = {r_id for (c_id, r_id, t_idx) in x.keys() if c_id in v_ids}
        room_used_vars: list = []
        for r_id in course_rooms:
            y_room = model.NewBoolVar(f"room_used_orig{orig_id}_r{r_id}")
            room_used_vars.append(y_room)
            for v_id in v_ids:
                v_room_vars = vars_by_course_and_room.get((v_id, r_id), [])
                if v_room_vars:
                    model.Add(sum(v_room_vars) <= y_room)
        if room_used_vars:
            model.AddAtMostOne(room_used_vars)

    # ── Soft: Room Utilization Target (75% Max Utilization) ────────────────
    target_room_limit = max(1, int(len(timeslots) * 0.75))
    for room in rooms:
        r_id = room.id
        rm_vars = [var for (c_id, rm_id, t_idx), var in x.items() if rm_id == r_id]
        if rm_vars:
            excess_rm = model.NewIntVar(0, len(rm_vars), f"room_util_excess_R{r_id}")
            model.Add(sum(rm_vars) - target_room_limit <= excess_rm)
            obj_terms.append(excess_rm * 100)

    # Force all virtual pieces of the same session to be scheduled together
    session_pieces: dict = defaultdict(list)
    for course in courses:
        if course.id in is_scheduled_vars:
            session_pieces[(course.orig_course_id, course.session_index)].append(course.id)
    for (orig_id, sess_idx), v_ids in session_pieces.items():
        if len(v_ids) <= 1:
            continue
        first_piece_scheduled = is_scheduled_vars[v_ids[0]]
        for v_id in v_ids[1:]:
            model.Add(is_scheduled_vars[v_id] == first_piece_scheduled)

    # ── Soft: STUDENT_MAX_CLASSES_PER_DAY ──────────────────────────────────
    for group in student_groups:
        g_id = group.id
        if g_id not in student_max_per_day:
            continue
        max_cls = student_max_per_day[g_id]
        for day in timeslots_by_day.keys():
            day_start_vars = start_vars_by_group_and_day.get((g_id, day), [])
            if day_start_vars:
                excess = model.NewIntVar(
                    0, len(day_start_vars), f"student_excess_G{g_id}_d{day}"
                )
                model.Add(sum(day_start_vars) - max_cls <= excess)
                obj_terms.append(excess * 2000)

    # ── Soft/Hard: MAX_CLASSES_PER_DAY (lecturer) ──────────────────────────
    individual_max_classes: dict = {}
    global_max_classes = None
    for db_const in db_constraints:
        if db_const.constraint_type == 'MAX_CLASSES_PER_DAY':
            l_id       = db_const.parameters.get('lecturer_id')
            max_classes = db_const.parameters.get('max_classes')
            if max_classes is not None:
                try:
                    max_classes = int(max_classes)
                    if l_id:
                        individual_max_classes[int(l_id)] = (max_classes, db_const.is_hard, db_const.weight)
                    else:
                        global_max_classes = (max_classes, db_const.is_hard, db_const.weight)
                except (ValueError, TypeError):
                    continue

    active_lecturer_ids: set = {c.lecturer_id for c in courses if c.lecturer_id}
    for l_id in active_lecturer_ids:
        rule = individual_max_classes.get(l_id) or global_max_classes
        if rule:
            max_classes, is_hard, weight = rule
            for day in timeslots_by_day.keys():
                day_vars = start_vars_by_lecturer_and_day.get((l_id, day), [])
                if day_vars:
                    excess = model.NewIntVar(0, len(day_vars), f"max_cls_excess_L{l_id}_d{day}")
                    model.Add(sum(day_vars) - max_classes <= excess)
                    obj_terms.append(excess * (2000 if is_hard else weight * 50))

    # ── Soft/Hard: LECTURER_MAX_DAYS_PER_WEEK ──────────────────────────────
    individual_max_days: dict = {}
    global_max_days = None
    for db_const in db_constraints:
        if db_const.constraint_type == 'LECTURER_MAX_DAYS_PER_WEEK':
            l_id     = db_const.parameters.get('lecturer_id')
            max_days = db_const.parameters.get('max_days')
            if max_days is not None:
                try:
                    max_days = int(max_days)
                    if l_id:
                        individual_max_days[int(l_id)] = (max_days, db_const.is_hard, db_const.weight)
                    else:
                        global_max_days = (max_days, db_const.is_hard, db_const.weight)
                except (ValueError, TypeError):
                    continue

    for l_id in active_lecturer_ids:
        rule = individual_max_days.get(l_id) or global_max_days
        if rule:
            max_days, is_hard, weight = rule
            lec_day_vars: list = []
            for day in timeslots_by_day.keys():
                day_vars = start_vars_by_lecturer_and_day.get((l_id, day), [])
                if day_vars:
                    is_active = model.NewBoolVar(f"lec_{l_id}_active_day{day}_const")
                    for var in day_vars:
                        model.Add(var <= is_active)
                    lec_day_vars.append(is_active)
            if lec_day_vars:
                if is_hard:
                    model.Add(sum(lec_day_vars) <= max_days)
                else:
                    excess = model.NewIntVar(0, 7, f"lec_{l_id}_excess_days")
                    model.Add(sum(lec_day_vars) - max_days <= excess)
                    obj_terms.append(excess * weight * 50)

    # ── Automatic day-grouping penalty (encourage rest days) ───────────────
    lecturer_ids: set = {c.lecturer_id for c in courses if c.lecturer_id}
    for l_id in lecturer_ids:
        for day in timeslots_by_day.keys():
            day_vars = start_vars_by_lecturer_and_day.get((l_id, day), [])
            if day_vars:
                is_active = model.NewBoolVar(f"lec_{l_id}_active_day{day}")
                for var in day_vars:
                    model.Add(var <= is_active)
                obj_terms.append(is_active * 120)

    # ── Hard: Campus Travel Time for Lecturers ─────────────────────────────
    room_campus_map: dict = {r.id: r.campus_id for r in rooms}
    vars_by_lec_slot_campus: dict = defaultdict(list)
    for (c_id, r_id, t_idx), var in x.items():
        if r_id in virtual_room_ids:
            continue
        course   = course_by_id[c_id]
        l_id     = course.lecturer_id
        if not l_id:
            continue
        duration = course.duration_slots
        camp_id  = room_campus_map[r_id]
        for offset in range(duration):
            slot_ts = idx_to_ts[t_idx + offset]
            vars_by_lec_slot_campus[(l_id, slot_ts.day_of_week, slot_ts.slot_number, camp_id)].append(var)

    campuses_by_lec_day_slot: dict = defaultdict(set)
    for (l_id, day, slot, camp_id) in vars_by_lec_slot_campus.keys():
        campuses_by_lec_day_slot[(l_id, day, slot)].add(camp_id)

    for l_id in lecturer_ids:
        for day, day_ts_list in timeslots_by_day.items():
            sorted_slots = sorted(ts.slot_number for ts in day_ts_list)
            for i in range(len(sorted_slots) - 1):
                s1, s2 = sorted_slots[i], sorted_slots[i + 1]
                if s2 - s1 == 1:
                    for c1 in campuses_by_lec_day_slot.get((l_id, day, s1), set()):
                        for c2 in campuses_by_lec_day_slot.get((l_id, day, s2), set()):
                            if c1 != c2:
                                s1_vars = vars_by_lec_slot_campus[(l_id, day, s1, c1)]
                                s2_vars = vars_by_lec_slot_campus[(l_id, day, s2, c2)]
                                model.Add(sum(s1_vars) + sum(s2_vars) <= 1)

    # ── Hard/Soft: LECTURER_MAX_CONSECUTIVE_SLOTS ──────────────────────────
    individual_max_consec: dict = {}
    global_max_consec = None
    for db_const in db_constraints:
        if db_const.constraint_type == 'LECTURER_MAX_CONSECUTIVE_SLOTS':
            l_id  = db_const.parameters.get('lecturer_id')
            p_max = db_const.parameters.get('max_consecutive')
            if p_max is not None:
                try:
                    p_max = int(p_max)
                    if l_id:
                        individual_max_consec[int(l_id)] = (p_max, db_const.is_hard)
                    else:
                        global_max_consec = (p_max, db_const.is_hard)
                except (ValueError, TypeError):
                    continue

    for l_id in active_lecturer_ids:
        rule = individual_max_consec.get(l_id) or global_max_consec
        if rule:
            max_consec, is_hard = rule
            for day, day_ts_list in timeslots_by_day.items():
                k = len(day_ts_list)
                if k <= max_consec:
                    continue
                sorted_ts = sorted(day_ts_list, key=lambda ts: ts.slot_number)
                active_vars_by_slot: dict = {}
                for ts in sorted_ts:
                    u_idx = ts_to_idx[ts.id]
                    overlapping_vars = vars_by_lecturer_and_slot.get((l_id, u_idx), [])
                    if overlapping_vars:
                        slot_active_var = model.NewBoolVar(
                            f"consec_active_L{l_id}_d{day}_s{ts.slot_number}"
                        )
                        model.Add(slot_active_var == sum(overlapping_vars))
                        active_vars_by_slot[ts.slot_number] = slot_active_var

                slot_numbers = [ts.slot_number for ts in sorted_ts]
                for i in range(len(slot_numbers) - max_consec):
                    window_slots = slot_numbers[i: i + max_consec + 1]
                    if window_slots[-1] - window_slots[0] == max_consec:
                        window_vars = [
                            active_vars_by_slot[snum]
                            for snum in window_slots
                            if snum in active_vars_by_slot
                        ]
                        if len(window_vars) > max_consec:
                            if is_hard:
                                model.Add(sum(window_vars) <= max_consec)
                            else:
                                excess = model.NewIntVar(
                                    0, max_consec + 1,
                                    f"consec_excess_L{l_id}_d{day}_i{i}",
                                )
                                model.Add(sum(window_vars) - max_consec <= excess)
                                obj_terms.append(excess * 1000)

    # ── Soft: Gap minimisation (small datasets only — expensive) ───────────
    num_courses = len(courses)
    if num_courses <= 30:
        for lecturer in lecturers:
            l_id = lecturer.id
            for day, day_ts_list in timeslots_by_day.items():
                k = len(day_ts_list)
                if k <= 1:
                    continue
                A_list: list = []
                for j_idx, ts in enumerate(day_ts_list):
                    u_idx  = ts_to_idx[ts.id]
                    A_vars = vars_by_lecturer_and_slot.get((l_id, u_idx), [])
                    av     = model.NewBoolVar(f"act_L_{l_id}_d{day}_s{j_idx}")
                    model.Add(av == sum(A_vars)) if A_vars else model.Add(av == 0)
                    A_list.append(av)
                is_active_day = model.NewBoolVar(f"active_L_{l_id}_d{day}")
                model.AddMaxEquality(is_active_day, A_list)
                first_slot = model.NewIntVar(0, k, f"first_L_{l_id}_d{day}")
                last_slot  = model.NewIntVar(0, k, f"last_L_{l_id}_d{day}")
                model.Add(first_slot == 0).OnlyEnforceIf(is_active_day.Not())
                model.Add(last_slot  == 0).OnlyEnforceIf(is_active_day.Not())
                for j_idx, av in enumerate(A_list):
                    model.Add(first_slot <= j_idx + 1).OnlyEnforceIf(av)
                    model.Add(last_slot  >= j_idx + 1).OnlyEnforceIf(av)
                obj_terms.append(
                    (last_slot - first_slot + is_active_day - sum(A_list)) * 4
                )

        for group in student_groups:
            g_id = group.id
            for day, day_ts_list in timeslots_by_day.items():
                k = len(day_ts_list)
                if k <= 1:
                    continue
                A_list = []
                for j_idx, ts in enumerate(day_ts_list):
                    u_idx  = ts_to_idx[ts.id]
                    A_vars = vars_by_student_group_and_slot.get((g_id, u_idx), [])
                    av     = model.NewBoolVar(f"act_G_{g_id}_d{day}_s{j_idx}")
                    model.Add(av == sum(A_vars)) if A_vars else model.Add(av == 0)
                    A_list.append(av)
                is_active_day = model.NewBoolVar(f"active_G_{g_id}_d{day}")
                model.AddMaxEquality(is_active_day, A_list)
                first_slot = model.NewIntVar(0, k, f"first_G_{g_id}_d{day}")
                last_slot  = model.NewIntVar(0, k, f"last_G_{g_id}_d{day}")
                model.Add(first_slot == 0).OnlyEnforceIf(is_active_day.Not())
                model.Add(last_slot  == 0).OnlyEnforceIf(is_active_day.Not())
                for j_idx, av in enumerate(A_list):
                    model.Add(first_slot <= j_idx + 1).OnlyEnforceIf(av)
                    model.Add(last_slot  >= j_idx + 1).OnlyEnforceIf(av)
                obj_terms.append(
                    (last_slot - first_slot + is_active_day - sum(A_list)) * 4
                )

    if obj_terms:
        model.Minimize(sum(obj_terms))

    return {
        'model':                        model,
        'x':                            x,
        'is_scheduled_vars':            is_scheduled_vars,
        'obj_terms':                    obj_terms,
        'vars_by_room_and_slot':        vars_by_room_and_slot,
        'vars_by_lecturer_and_slot':    vars_by_lecturer_and_slot,
        'vars_by_student_group_and_slot': vars_by_student_group_and_slot,
        'vars_by_course':               vars_by_course,
        'start_vars_by_group_and_day':  start_vars_by_group_and_day,
        'start_vars_by_lecturer_and_day': start_vars_by_lecturer_and_day,
        'virtual_room_ids':             virtual_room_ids,
        'course_by_id':                 course_by_id,
        'room_building_map':            room_building_map,
        'lecturer_max_slots_per_day':   lecturer_max_slots_per_day,
        'lecturer_preferences_prefer':  lecturer_preferences_prefer,
        'lecturer_preferences_dislike': lecturer_preferences_dislike,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Solver Runner
# ──────────────────────────────────────────────────────────────────────────────

def run_cpsat_solver(
    model,
    x: dict,
    greedy_result: dict,
    timetable_id: int,
    time_limit_seconds: int,
    num_courses: int,
):
    """
    Seed the model with greedy hints, configure worker count/branching,
    run the solver, and return (status_code, objective_value, solution_triples).

    Returns:
        (status, objective_value, cpsat_triples)
        where status is an OR-Tools status integer and cpsat_triples is a
        list of (c_id, r_id, t_idx) for all vars set to 1.
    """
    import sys

    # Warm-start from greedy
    hints_added = 0
    for (c_id, r_id, t_idx), var in x.items():
        model.AddHint(var, greedy_result.get((c_id, r_id, t_idx), 0))
        hints_added += 1
    logger.info(f"[Solver] Added {hints_added} CP-SAT hints from greedy solution")

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds

    _num_workers = min(multiprocessing.cpu_count(), 8)
    solver.parameters.num_search_workers  = _num_workers
    solver.parameters.search_branching    = cp_model.PORTFOLIO_SEARCH

    is_testing = 'test' in sys.argv or 'pytest' in sys.modules
    solver.parameters.stop_after_first_solution = (num_courses >= 30 or is_testing)

    logger.info(
        f"[Solver] Phase 2: CP-SAT solving {len(x)} variables, "
        f"time_limit={time_limit_seconds}s, workers={_num_workers}"
    )

    callback = FirebaseProgressCallback(timetable_id, x)
    status   = solver.Solve(model, callback)

    objective_value = None
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        try:
            objective_value = int(solver.ObjectiveValue())
        except Exception:
            pass

    cpsat_triples = []
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        cpsat_triples = [
            (c_id, r_id, t_idx)
            for (c_id, r_id, t_idx), var in x.items()
            if solver.Value(var) == 1
        ]

    return status, objective_value, cpsat_triples
