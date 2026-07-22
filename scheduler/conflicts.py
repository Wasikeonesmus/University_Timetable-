from collections import defaultdict
from django.db.models import Q
from .models import ScheduleSlot, TimeSlot, Constraint, LecturerAvailability, Lecturer, StudentGroup

def check_conflicts_for_timetable(timetable):
    """
    Wrapper that fetches all slots for a timetable and runs conflict detection.
    """
    slots = list(timetable.slots.select_related(
        'course', 'lecturer', 'room', 'room__campus', 'time_slot', 'student_group', 'time_slot__university'
    ).all())
    return detect_conflicts(slots, timetable.semester.university)

def detect_conflicts(slots, university):
    """
    Analyzes schedule slots and returns a list of conflicts (hard/soft).
    Each conflict is a dictionary with:
    - severity: 'error' (hard) or 'warning' (soft)
    - constraint_type: name of the constraint rule
    - message: user-friendly description
    - entities: dict with DB ids of involved objects
    """
    conflicts = []

    # Map day labels for display
    day_labels = {
        1: 'Monday', 2: 'Tuesday', 3: 'Wednesday', 4: 'Thursday', 
        5: 'Friday', 6: 'Saturday', 7: 'Sunday'
    }

    # Pre-build mappings to avoid O(N) linear search
    from .models import Course, Room, BuildingDistance, LecturerTimeSlotPreference
    course_required_features = defaultdict(set)
    for course_id, feature_name in Course.required_features.through.objects.filter(course__program__department__faculty__campus__university=university).values_list('course_id', 'roomfeature__name'):
        course_required_features[course_id].add(feature_name)

    room_features = defaultdict(set)
    for room_id, feature_name in Room.features.through.objects.filter(room__campus__university=university).values_list('room_id', 'roomfeature__name'):
        room_features[room_id].add(feature_name)

    course_additional_groups = defaultdict(set)
    for course_id, group_id in Course.additional_student_groups.through.objects.filter(course__program__department__faculty__campus__university=university).values_list('course_id', 'studentgroup_id'):
        course_additional_groups[course_id].add(group_id)

    room_building_map = {r_id: b_id for r_id, b_id in Room.objects.filter(campus__university=university).values_list('id', 'building_id')}
    room_building_name_map = {r_id: b_name for r_id, b_name in Room.objects.filter(campus__university=university).values_list('id', 'building__name')}
    
    building_distances = {}
    for b1, b2, time_min in BuildingDistance.objects.filter(from_building__campus__university=university).values_list('from_building_id', 'to_building_id', 'walking_time_minutes'):
        building_distances[(b1, b2)] = time_min

    lecturer_preferences_dislike = defaultdict(set)
    for l_id, ts_id in LecturerTimeSlotPreference.objects.filter(lecturer__department__faculty__campus__university=university, preference_level='dislike').values_list('lecturer_id', 'time_slot_id'):
        lecturer_preferences_dislike[l_id].add(ts_id)

    lecturer_map = {}
    student_group_map = {}
    
    # Helper maps for double bookings
    lecturer_occupancy = defaultdict(list)     # (lecturer_id, day, slot_number) -> slots
    room_occupancy = defaultdict(list)         # (room_id, day, slot_number) -> slots
    student_group_occupancy = defaultdict(list)# (student_group_id, day, slot_number) -> slots
    
    # Track student group daily course starts for max classes per day check
    student_group_daily_courses = defaultdict(lambda: defaultdict(set))
    # Track lecturer daily course starts for max classes per day check
    lecturer_daily_courses = defaultdict(lambda: defaultdict(set))
    # Lecturer daily slot detail — built in the main slot loop (single pass)
    lecturer_daily_slots_detail = defaultdict(lambda: defaultdict(list))

    # Load constraints at the beginning to avoid variable scoping errors
    lecturer_avail_constraints = defaultdict(list)
    room_preference_constraints = defaultdict(list)
    lab_only_constraints = defaultdict(list)
    student_max_classes_constraints = defaultdict(list)
    lecturer_max_classes_constraints = defaultdict(list)
    lecturer_max_consecutive_constraints = defaultdict(list)

    configs = Constraint.objects.filter(university=university)
    for config in configs:
        if config.constraint_type == 'LECTURER_AVAILABILITY':
            p_lecturer_id = config.parameters.get('lecturer_id')
            p_unavail_slots = config.parameters.get('unavailable_slots', [])
            if p_lecturer_id and p_unavail_slots:
                lecturer_avail_constraints[p_lecturer_id].append((config, set(p_unavail_slots)))
        elif config.constraint_type == 'LECTURER_MAX_CONSECUTIVE_SLOTS':
            p_lecturer_id = config.parameters.get('lecturer_id')
            p_max = config.parameters.get('max_consecutive')
            if p_lecturer_id and p_max is not None:
                lecturer_max_consecutive_constraints[p_lecturer_id].append((config, p_max))
        elif config.constraint_type == 'ROOM_PREFERENCE':
            p_course_id = config.parameters.get('course_id')
            p_pref_rooms = config.parameters.get('preferred_rooms', [])
            if p_course_id and p_pref_rooms:
                room_preference_constraints[p_course_id].append((config, set(p_pref_rooms)))
        elif config.constraint_type == 'LAB_ONLY_COURSE':
            p_course_id = config.parameters.get('course_id')
            if p_course_id:
                lab_only_constraints[p_course_id].append(config)
        elif config.constraint_type == 'STUDENT_MAX_CLASSES_PER_DAY':
            p_group_id = config.parameters.get('student_group_id')
            p_max = config.parameters.get('max_classes')
            if p_group_id and p_max is not None:
                student_max_classes_constraints[p_group_id].append((config, p_max))
        elif config.constraint_type == 'MAX_CLASSES_PER_DAY':
            p_lecturer_id = config.parameters.get('lecturer_id')
            p_max = config.parameters.get('max_classes')
            if p_lecturer_id and p_max is not None:
                lecturer_max_classes_constraints[p_lecturer_id].append((config, p_max))

    # Group slots by lecturer / student / room per day & slot_number
    for slot in slots:
        ts = slot.time_slot
        day = ts.day_of_week
        num = ts.slot_number

        if slot.lecturer_id:
            lecturer_map[slot.lecturer_id] = slot.lecturer
            lecturer_daily_courses[slot.lecturer_id][day].add(slot.course_id)
            lecturer_daily_slots_detail[slot.lecturer_id][day].append(slot)
        if slot.student_group_id:
            student_group_map[slot.student_group_id] = slot.student_group
            student_group_daily_courses[slot.student_group_id][day].add(slot.course_id)

        lecturer_occupancy[(slot.lecturer_id, day, num)].append(slot)
        room_occupancy[(slot.room_id, day, num)].append(slot)
        student_group_occupancy[(slot.student_group_id, day, num)].append(slot)

        # Room Capacity check
        if slot.student_group.size > slot.room.capacity:
            conflicts.append({
                'severity': 'error',
                'constraint_type': 'ROOM_CAPACITY',
                'message': f"Room '{slot.room.name}' capacity ({slot.room.capacity}) is smaller than Student Group '{slot.student_group.name}' size ({slot.student_group.size}) for Course '{slot.course.code}'.",
                'entities': {
                    'room_id': slot.room_id,
                    'student_group_id': slot.student_group_id,
                    'course_id': slot.course_id,
                    'slot_id': slot.id
                }
            })

        # Room type preference/requirement check (Soft warning if mismatched)
        if slot.course.required_room_type != slot.room.room_type:
            conflicts.append({
                'severity': 'warning',
                'constraint_type': 'ROOM_TYPE_MISMATCH',
                'message': f"Course '{slot.course.code}' requires room type '{slot.course.get_required_room_type_display()}' but was scheduled in room '{slot.room.name}' ({slot.room.get_room_type_display()}).",
                'entities': {
                    'room_id': slot.room_id,
                    'course_id': slot.course_id,
                    'slot_id': slot.id
                }
            })

        # Avoid Evening Classes check
        if ts.is_evening:
            conflicts.append({
                'severity': 'warning',
                'constraint_type': 'NO_EVENING_CLASSES',
                'message': f"Course '{slot.course.code}' is scheduled in the evening at {ts}.",
                'entities': {
                    'course_id': slot.course_id,
                    'time_slot_id': ts.id,
                    'slot_id': slot.id
                }
            })

        # Room Feature Tagging check
        req_feats = course_required_features.get(slot.course_id, set())
        if req_feats:
            room_feats = room_features.get(slot.room_id, set())
            if not req_feats.issubset(room_feats):
                missing = req_feats - room_feats
                conflicts.append({
                    'severity': 'error',
                    'constraint_type': 'ROOM_MISSING_REQUIRED_FEATURES',
                    'message': f"Room '{slot.room.name}' lacks required features for Course '{slot.course.code}': {', '.join(missing)}.",
                    'entities': {
                        'room_id': slot.room_id,
                        'course_id': slot.course_id,
                        'slot_id': slot.id
                    }
                })

        # Lecturer disliked slot warning
        if slot.lecturer_id and slot.time_slot_id in lecturer_preferences_dislike[slot.lecturer_id]:
            conflicts.append({
                'severity': 'warning',
                'constraint_type': 'LECTURER_DISLIKED_SLOT',
                'message': f"Lecturer {slot.lecturer.name} dislikes teaching in slot {slot.time_slot}.",
                'entities': {
                    'lecturer_id': slot.lecturer_id,
                    'time_slot_id': slot.time_slot_id,
                    'slot_id': slot.id
                }
            })

    # ── Lecturer double-booking: O(N log N) interval-sweep instead of O(N²) ──
    lecturer_day_slots = defaultdict(list)
    for slot in slots:
        if slot.lecturer_id:
            lecturer_day_slots[(slot.lecturer_id, slot.time_slot.day_of_week)].append(slot)

    seen_lecturer_conflict_pairs = set()
    for (lecturer_id, day), day_slots in lecturer_day_slots.items():
        # Sort by start time so we only compare each slot against its immediate neighbours
        day_slots.sort(key=lambda s: s.time_slot.start_time)
        for i in range(len(day_slots) - 1):
            s1 = day_slots[i]
            # Walk forward while s2 still overlaps s1 (once s2.start >= s1.end, so do all later)
            for j in range(i + 1, len(day_slots)):
                s2 = day_slots[j]
                if s2.time_slot.start_time >= s1.time_slot.end_time:
                    break  # no further overlap possible
                pair_key = (s1.id, s2.id) if s1.id < s2.id else (s2.id, s1.id)
                if pair_key not in seen_lecturer_conflict_pairs:
                    seen_lecturer_conflict_pairs.add(pair_key)
                    day_name = day_labels.get(day, f"Day {day}")
                    conflicts.append({
                        'severity': 'error',
                        'constraint_type': 'LECTURER_DOUBLE_BOOKING',
                        'message': (
                            f"Lecturer {s1.lecturer.name} is double-booked on {day_name}: "
                            f"'{s1.course.code}' ({s1.time_slot.start_time.strftime('%H:%M')}-"
                            f"{s1.time_slot.end_time.strftime('%H:%M')} in {s1.room.name}) "
                            f"overlaps with '{s2.course.code}' ({s2.time_slot.start_time.strftime('%H:%M')}-"
                            f"{s2.time_slot.end_time.strftime('%H:%M')} in {s2.room.name})."
                        ),
                        'entities': {
                            'lecturer_id': lecturer_id,
                            'day': day,
                            'slot_ids': [s1.id, s2.id]
                        }
                    })

    # ── Room double-booking: O(N log N) interval-sweep ───────────────────────
    room_day_slots = defaultdict(list)
    for slot in slots:
        if slot.room_id:
            room_day_slots[(slot.room_id, slot.time_slot.day_of_week)].append(slot)

    seen_room_conflict_pairs = set()
    for (room_id, day), day_slots in room_day_slots.items():
        day_slots.sort(key=lambda s: s.time_slot.start_time)
        for i in range(len(day_slots) - 1):
            s1 = day_slots[i]
            for j in range(i + 1, len(day_slots)):
                s2 = day_slots[j]
                if s2.time_slot.start_time >= s1.time_slot.end_time:
                    break
                pair_key = (s1.id, s2.id) if s1.id < s2.id else (s2.id, s1.id)
                if pair_key not in seen_room_conflict_pairs:
                    seen_room_conflict_pairs.add(pair_key)
                    day_name = day_labels.get(day, f"Day {day}")
                    conflicts.append({
                        'severity': 'error',
                        'constraint_type': 'ROOM_DOUBLE_BOOKING',
                        'message': (
                            f"Room {s1.room.name} is double-booked on {day_name}: "
                            f"'{s1.course.code}' ({s1.time_slot.start_time.strftime('%H:%M')}-"
                            f"{s1.time_slot.end_time.strftime('%H:%M')}) "
                            f"overlaps with '{s2.course.code}' ({s2.time_slot.start_time.strftime('%H:%M')}-"
                            f"{s2.time_slot.end_time.strftime('%H:%M')})."
                        ),
                        'entities': {
                            'room_id': room_id,
                            'day': day,
                            'slot_ids': [s1.id, s2.id]
                        }
                    })


    # Build student-group hierarchy maps — values_list only (no full ORM objects needed)
    all_student_groups = {}   # id -> StudentGroup ORM object (already in slot.student_group)
    group_conflict_sets = defaultdict(set)
    for g_id, parent_id in StudentGroup.objects.filter(
        program__department__faculty__campus__university=university
    ).values_list('id', 'parent_group_id'):
        group_conflict_sets[g_id].add(g_id)
        if parent_id:
            group_conflict_sets[g_id].add(parent_id)
            group_conflict_sets[parent_id].add(g_id)
    # Populate all_student_groups from already-fetched slot data (no extra query)
    for slot in slots:
        if slot.student_group_id and slot.student_group_id not in all_student_groups:
            all_student_groups[slot.student_group_id] = slot.student_group

    # Cache occupied groups for each slot to avoid building sets repeatedly
    occupied_groups_by_slot = {}
    for slot in slots:
        groups = {slot.student_group_id}
        for g_add in course_additional_groups.get(slot.course_id, set()):
            groups.add(g_add)
        expanded = set()
        for g in groups:
            expanded.update(group_conflict_sets.get(g, {g}))
        occupied_groups_by_slot[slot.id] = expanded

    # ── Student Group double-booking: O(N log N) interval-sweep per group per day ──
    slots_by_day = defaultdict(list)
    for slot in slots:
        slots_by_day[slot.time_slot.day_of_week].append(slot)

    seen_student_conflict_pairs = set()
    for day, day_slots in slots_by_day.items():
        group_to_slots = defaultdict(list)
        for s in day_slots:
            for g in occupied_groups_by_slot[s.id]:
                group_to_slots[g].append(s)

        for g, g_slots in group_to_slots.items():
            if len(g_slots) < 2:
                continue
            # Sort by start_time for sweep
            g_slots.sort(key=lambda s: s.time_slot.start_time)
            for i in range(len(g_slots) - 1):
                s1 = g_slots[i]
                for j in range(i + 1, len(g_slots)):
                    s2 = g_slots[j]
                    if s2.time_slot.start_time >= s1.time_slot.end_time:
                        break  # sorted — no further overlap
                    pair_key = (s1.id, s2.id) if s1.id < s2.id else (s2.id, s1.id)
                    if pair_key not in seen_student_conflict_pairs:
                        seen_student_conflict_pairs.add(pair_key)
                        day_name = day_labels.get(day, f"Day {day}")
                        # Collect all groups from the union of both slots' expanded sets
                        intersect = occupied_groups_by_slot[s1.id] & occupied_groups_by_slot[s2.id]
                        group_names = []
                        for gid in sorted(intersect):
                            g_obj = all_student_groups.get(gid) or student_group_map.get(gid)
                            group_names.append(g_obj.name if g_obj else f"Group {gid}")
                        conflicts.append({
                            'severity': 'error',
                            'constraint_type': 'STUDENT_GROUP_DOUBLE_BOOKING',
                            'message': (
                                f"Student Group Double Booking: Overlapping groups "
                                f"(such as {', '.join(group_names)}) are scheduled on {day_name}: "
                                f"'{s1.course.code}' ({s1.time_slot.start_time.strftime('%H:%M')}-"
                                f"{s1.time_slot.end_time.strftime('%H:%M')}) overlaps with "
                                f"'{s2.course.code}' ({s2.time_slot.start_time.strftime('%H:%M')}-"
                                f"{s2.time_slot.end_time.strftime('%H:%M')})."
                            ),
                            'entities': {
                                'student_group_ids': sorted(list(intersect)),
                                'day': day,
                                'slot_ids': [s1.id, s2.id]
                            }
                        })

    # (lecturer_daily_slots_detail already built in the main slot loop above)

    # Check Lecturer Gaps, Campus Travel, and Consecutive slots
    for lecturer_id, days in lecturer_daily_slots_detail.items():
        lecturer = lecturer_map.get(lecturer_id)
        lecturer_name = lecturer.name if lecturer else f"Lecturer {lecturer_id}"

        for day, day_slots in days.items():
            # Sort slots by slot_number
            day_slots = sorted(day_slots, key=lambda s: s.time_slot.slot_number)
            slot_nums = [s.time_slot.slot_number for s in day_slots]
            day_name = day_labels.get(day, f"Day {day}")

            # 1. Gap detection
            if len(slot_nums) > 1:
                gaps = []
                for i in range(len(slot_nums) - 1):
                    diff = slot_nums[i+1] - slot_nums[i]
                    if diff > 1:
                        gaps.extend(range(slot_nums[i] + 1, slot_nums[i+1]))
                if gaps:
                    conflicts.append({
                        'severity': 'warning',
                        'constraint_type': 'LECTURER_GAP',
                        'message': f"Lecturer {lecturer_name} has a gap in their schedule on {day_name} between slots {slot_nums[0]} and {slot_nums[-1]} (empty slots: {', '.join(map(str, gaps))}).",
                        'entities': {
                            'lecturer_id': lecturer_id,
                            'day': day,
                            'gaps': gaps
                        }
                    })

            # 2. Campus Travel and Building Travel Check
            if len(day_slots) > 1:
                for i in range(len(day_slots) - 1):
                    s1 = day_slots[i]
                    s2 = day_slots[i+1]
                    if s2.time_slot.slot_number - s1.time_slot.slot_number == 1:
                        # Virtual/online rooms have no physical location.
                        # Use the explicit is_virtual flag (set on the Room model)
                        # instead of guessing from the room name.
                        r1_virtual = s1.room.is_virtual
                        r2_virtual = s2.room.is_virtual
                        if r1_virtual or r2_virtual:
                            pass  # no travel penalty for virtual rooms
                        elif s1.room.campus_id != s2.room.campus_id:
                            conflicts.append({
                                'severity': 'error',
                                'constraint_type': 'LECTURER_CAMPUS_TRAVEL_VIOLATION',
                                'message': f"Lecturer {lecturer_name} has consecutive classes on different campuses on {day_name}: "
                                           f"Slot {s1.time_slot.slot_number} in Room '{s1.room.name}' ({s1.room.campus.name}) "
                                           f"and Slot {s2.time_slot.slot_number} in Room '{s2.room.name}' ({s2.room.campus.name}).",
                                'entities': {
                                    'lecturer_id': lecturer_id,
                                    'day': day,
                                    'slot_numbers': [s1.time_slot.slot_number, s2.time_slot.slot_number],
                                    'slot_ids': [s1.id, s2.id]
                                }
                            })
                        elif s1.room_id in room_building_map and s2.room_id in room_building_map:
                            b1 = room_building_map[s1.room_id]
                            b2 = room_building_map[s2.room_id]
                            if b1 and b2 and b1 != b2:
                                walk_time = max(building_distances.get((b1, b2), 0), building_distances.get((b2, b1), 0))
                                if walk_time > 15:
                                    conflicts.append({
                                        'severity': 'error',
                                        'constraint_type': 'INSUFFICIENT_TRAVEL_TIME',
                                        'message': f"Lecturer {lecturer_name} has consecutive classes in different buildings with insufficient walking time ({walk_time} mins, exceeds 15 mins break) on {day_name}: "
                                                   f"Slot {s1.time_slot.slot_number} in Room '{s1.room.name}' ({room_building_name_map.get(s1.room_id)}) "
                                                   f"and Slot {s2.time_slot.slot_number} in Room '{s2.room.name}' ({room_building_name_map.get(s2.room_id)}).",
                                        'entities': {
                                            'lecturer_id': lecturer_id,
                                            'day': day,
                                            'slot_numbers': [s1.time_slot.slot_number, s2.time_slot.slot_number],
                                            'slot_ids': [s1.id, s2.id]
                                        }
                                    })


            # 3. Consecutive Slots limit check
            if lecturer_id in lecturer_max_consecutive_constraints and len(slot_nums) > 0:
                for config, max_consec in lecturer_max_consecutive_constraints[lecturer_id]:
                    current_run = []
                    runs = []
                    for s_num in slot_nums:
                        if not current_run or s_num - current_run[-1] == 1:
                            current_run.append(s_num)
                        else:
                            runs.append(current_run)
                            current_run = [s_num]
                    if current_run:
                        runs.append(current_run)

                    for run in runs:
                        if len(run) > max_consec:
                            conflicts.append({
                                'severity': 'error' if config.is_hard else 'warning',
                                'constraint_type': 'LECTURER_CONSECUTIVE_SLOTS_VIOLATION',
                                'message': f"Lecturer {lecturer_name} has {len(run)} consecutive classes on {day_name} (slots {', '.join(map(str, run))}), exceeding limit of {max_consec}.",
                                'entities': {
                                    'lecturer_id': lecturer_id,
                                    'day': day,
                                    'consecutive_run': run,
                                    'max_consecutive': max_consec
                                }
                            })

    # Student Group gaps and Building Travel checks
    student_daily_slots_detail = defaultdict(lambda: defaultdict(list))
    for slot in slots:
        student_daily_slots_detail[slot.student_group_id][slot.time_slot.day_of_week].append(slot)
        for g_add_id in course_additional_groups.get(slot.course_id, set()):
            student_daily_slots_detail[g_add_id][slot.time_slot.day_of_week].append(slot)

    for group_id, days in student_daily_slots_detail.items():
        group = all_student_groups.get(group_id) or student_group_map.get(group_id)
        group_name = group.name if group else f"Group {group_id}"
        
        for day, day_slots in days.items():
            # De-duplicate slots by slot_number (in case of overlaps which are already handled by double booking checks)
            seen_slots = {}
            for s in day_slots:
                seen_slots[s.time_slot.slot_number] = s
            day_slots = sorted(seen_slots.values(), key=lambda s: s.time_slot.slot_number)
            slot_nums = [s.time_slot.slot_number for s in day_slots]
            day_name = day_labels.get(day, f"Day {day}")
            
            if len(slot_nums) > 1:
                # 1. Gap detection
                gaps = []
                for i in range(len(slot_nums) - 1):
                    diff = slot_nums[i+1] - slot_nums[i]
                    if diff > 1:
                        gaps.extend(range(slot_nums[i] + 1, slot_nums[i+1]))
                if gaps:
                    conflicts.append({
                        'warning_type': 'STUDENT_GAP', # For compatibility
                        'severity': 'warning',
                        'constraint_type': 'STUDENT_GAP',
                        'message': f"Student Group {group_name} has a gap in their schedule on {day_name} between slots {slot_nums[0]} and {slot_nums[-1]} (empty slots: {', '.join(map(str, gaps))}).",
                        'entities': {
                            'student_group_id': group_id,
                            'day': day,
                            'gaps': gaps
                        }
                    })

                # 2. Building Travel Check
                for i in range(len(day_slots) - 1):
                    s1 = day_slots[i]
                    s2 = day_slots[i+1]
                    if s2.time_slot.slot_number - s1.time_slot.slot_number == 1:
                        if s1.room.is_virtual or s2.room.is_virtual:
                            continue
                        if s1.room_id in room_building_map and s2.room_id in room_building_map:
                            b1 = room_building_map[s1.room_id]
                            b2 = room_building_map[s2.room_id]
                            if b1 and b2 and b1 != b2:
                                walk_time = max(building_distances.get((b1, b2), 0), building_distances.get((b2, b1), 0))
                                if walk_time > 15:
                                    conflicts.append({
                                        'severity': 'error',
                                        'constraint_type': 'INSUFFICIENT_TRAVEL_TIME',
                                        'message': f"Student Group '{group_name}' has consecutive classes in different buildings with insufficient walking time ({walk_time} mins, exceeds 15 mins break) on {day_name}: "
                                                   f"Slot {s1.time_slot.slot_number} in Room '{s1.room.name}' ({room_building_name_map.get(s1.room_id)}) "
                                                   f"and Slot {s2.time_slot.slot_number} in Room '{s2.room.name}' ({room_building_name_map.get(s2.room_id)}).",
                                        'entities': {
                                            'student_group_id': group_id,
                                            'day': day,
                                            'slot_numbers': [s1.time_slot.slot_number, s2.time_slot.slot_number],
                                            'slot_ids': [s1.id, s2.id]
                                        }
                                    })



    # Load Lecturer self-service availability (where is_available=False)
    lecturer_self_unavail = defaultdict(set)
    unavail_records = LecturerAvailability.objects.filter(
        lecturer__department__faculty__campus__university=university,
        is_available=False
    )
    for record in unavail_records:
        lecturer_self_unavail[record.lecturer_id].add(record.time_slot_id)

    # Evaluate slot-specific constraints
    for slot in slots:
        # LECTURER_AVAILABILITY
        if slot.lecturer_id in lecturer_avail_constraints:
            for config, unavail_slots in lecturer_avail_constraints[slot.lecturer_id]:
                if slot.time_slot_id in unavail_slots:
                    conflicts.append({
                        'severity': 'error' if config.is_hard else 'warning',
                        'constraint_type': 'LECTURER_AVAILABILITY_VIOLATION',
                        'message': f"Lecturer availability constraint '{config.name}' violated. {slot.lecturer.name} scheduled in unavailable slot: {slot.time_slot}.",
                        'entities': {
                            'lecturer_id': slot.lecturer_id,
                            'time_slot_id': slot.time_slot_id,
                            'slot_id': slot.id
                        }
                    })

        # LECTURER_AVAILABILITY (Self-Service)
        if slot.lecturer_id in lecturer_self_unavail:
            if slot.time_slot_id in lecturer_self_unavail[slot.lecturer_id]:
                conflicts.append({
                    'severity': 'error',
                    'constraint_type': 'LECTURER_SELF_SERVICE_AVAILABILITY_VIOLATION',
                    'message': f"Lecturer self-service availability violated. {slot.lecturer.name} scheduled in unavailable slot: {slot.time_slot}.",
                    'entities': {
                        'lecturer_id': slot.lecturer_id,
                        'time_slot_id': slot.time_slot_id,
                        'slot_id': slot.id
                    }
                })

        # ROOM_PREFERENCE
        if slot.course_id in room_preference_constraints:
            for config, pref_rooms in room_preference_constraints[slot.course_id]:
                if slot.room_id not in pref_rooms:
                    conflicts.append({
                        'severity': 'error' if config.is_hard else 'warning',
                        'constraint_type': 'ROOM_PREFERENCE_VIOLATION',
                        'message': f"Room preference constraint '{config.name}' violated. Course {slot.course.code} is scheduled in room '{slot.room.name}' which is not in the preferred rooms list.",
                        'entities': {
                            'course_id': slot.course_id,
                            'room_id': slot.room_id,
                            'slot_id': slot.id
                        }
                    })

        # LAB_ONLY_COURSE
        if slot.course_id in lab_only_constraints:
            for config in lab_only_constraints[slot.course_id]:
                if slot.room.room_type != 'Lab':
                    conflicts.append({
                        'severity': 'error' if config.is_hard else 'warning',
                        'constraint_type': 'LAB_ONLY_VIOLATION',
                        'message': (
                            f"Lab-only constraint '{config.name}' violated. "
                            f"Course '{slot.course.code}' is placed in '{slot.room.name}' "
                            f"(type: {slot.room.get_room_type_display()}) instead of a Lab."
                        ),
                        'entities': {
                            'course_id': slot.course_id,
                            'room_id': slot.room_id,
                            'slot_id': slot.id
                        }
                    })

    # Evaluate STUDENT_MAX_CLASSES_PER_DAY constraints
    for group_id, constraints in student_max_classes_constraints.items():
        for config, max_classes in constraints:
            for day, course_ids in student_group_daily_courses[group_id].items():
                if len(course_ids) > max_classes:
                    group = all_student_groups.get(group_id) or student_group_map.get(group_id)
                    group_name = group.name if group else f"Group {group_id}"
                    day_label = day_labels.get(day, f"Day {day}")
                    conflicts.append({
                        'severity': 'error' if config.is_hard else 'warning',
                        'constraint_type': 'STUDENT_MAX_CLASSES_PER_DAY_VIOLATION',
                        'message': (
                            f"Student group '{group_name}' has {len(course_ids)} classes on {day_label}, "
                            f"exceeding the maximum of {max_classes} per day."
                        ),
                        'entities': {
                            'student_group_id': group_id,
                            'day': day,
                            'courses_count': len(course_ids)
                        }
                    })

    # Evaluate LECTURER_MAX_CLASSES_PER_DAY constraints
    for lecturer_id, constraints in lecturer_max_classes_constraints.items():
        for config, max_classes in constraints:
            for day, course_ids in lecturer_daily_courses[lecturer_id].items():
                if len(course_ids) > max_classes:
                    lecturer = lecturer_map.get(lecturer_id)
                    lecturer_name = lecturer.name if lecturer else f"Lecturer {lecturer_id}"
                    day_label = day_labels.get(day, f"Day {day}")
                    conflicts.append({
                        'severity': 'error' if config.is_hard else 'warning',
                        'constraint_type': 'LECTURER_MAX_CLASSES_PER_DAY_VIOLATION',
                        'message': (
                            f"Lecturer '{lecturer_name}' has {len(course_ids)} classes on {day_label}, "
                            f"exceeding the maximum of {max_classes} per day."
                        ),
                        'entities': {
                            'lecturer_id': lecturer_id,
                            'day': day,
                            'courses_count': len(course_ids)
                        }
                    })

    # Evaluate LECTURER_WEEKLY_HOURS constraints
    lecturer_weekly_slots = defaultdict(list)
    for slot in slots:
        if slot.lecturer_id:
            lecturer_weekly_slots[slot.lecturer_id].append(slot)

    for lecturer_id, lecturer_slots in lecturer_weekly_slots.items():
        lecturer = lecturer_map.get(lecturer_id)
        if lecturer:
            total_hours = 0.0
            for slot in lecturer_slots:
                ts = slot.time_slot
                duration_mins = (ts.end_time.hour * 60 + ts.end_time.minute) - (ts.start_time.hour * 60 + ts.start_time.minute)
                total_hours += duration_mins / 60.0
            if total_hours > lecturer.max_hours_per_week:
                conflicts.append({
                    'severity': 'warning',
                    'constraint_type': 'LECTURER_WEEKLY_HOURS_VIOLATION',
                    'message': f"Lecturer {lecturer.name} is scheduled for {total_hours} hours, exceeding their weekly maximum of {lecturer.max_hours_per_week} hours.",
                    'entities': {
                        'lecturer_id': lecturer_id,
                        'total_hours': total_hours,
                        'max_hours': lecturer.max_hours_per_week
                    }
                })

    # Evaluate LECTURER_DAILY_LIMIT_EXCEEDED
    for lecturer_id, days in lecturer_daily_slots_detail.items():
        lecturer = lecturer_map.get(lecturer_id)
        if lecturer:
            max_slots = lecturer.max_slots_per_day
            for day, day_slots in days.items():
                if len(day_slots) > max_slots:
                    day_label = day_labels.get(day, f"Day {day}")
                    conflicts.append({
                        'severity': 'error',
                        'constraint_type': 'LECTURER_DAILY_LIMIT_EXCEEDED',
                        'message': f"Lecturer {lecturer.name} is scheduled for {len(day_slots)} slots on {day_label}, exceeding their daily workload limit of {max_slots} slots.",
                        'entities': {
                            'lecturer_id': lecturer_id,
                            'day': day,
                            'slots_count': len(day_slots),
                            'max_slots': max_slots
                        }
                    })

    # Evaluate LECTURER_MAX_DAYS_PER_WEEK constraints
    for db_const in configs:
        if db_const.constraint_type == 'LECTURER_MAX_DAYS_PER_WEEK':
            l_id     = db_const.parameters.get('lecturer_id')
            max_days = db_const.parameters.get('max_days')
            if l_id and max_days is not None:
                try:
                    l_id = int(l_id)
                    max_days = int(max_days)
                except (ValueError, TypeError):
                    continue
                
                # Count actual active days for this lecturer in the timetable
                active_days = set()
                for slot in slots:
                    if slot.lecturer_id == l_id:
                        active_days.add(slot.time_slot.day_of_week)
                
                if len(active_days) > max_days:
                    lec_obj = lecturer_map.get(l_id)
                    lec_name = lec_obj.name if lec_obj else f"Lecturer #{l_id}"
                    conflicts.append({
                        'severity': 'error' if db_const.is_hard else 'warning',
                        'constraint_type': 'LECTURER_MAX_DAYS_PER_WEEK_VIOLATION',
                        'message': f"Lecturer '{lec_name}' taught on {len(active_days)} days, exceeding the maximum limit of {max_days} days per week.",
                        'entities': {
                            'lecturer_id': l_id,
                            'active_days_count': len(active_days),
                            'max_days': max_days
                        }
                    })

    return conflicts
