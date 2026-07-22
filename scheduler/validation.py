from .models import Course, Room, TimeSlot, Lecturer, StudentGroup, Timetable

def validate_university_data(university):
    """
    Validates all input data for the university.
    Returns: (is_valid, errors, warnings)
      - is_valid: Boolean (False if there are blocking errors)
      - errors: List of strings (blocking issues)
      - warnings: List of strings (non-blocking suggestions)
    """
    errors   = []
    warnings = []

    # 1. Fetch scoped resources
    courses   = list(Course.objects.filter(
        program__department__faculty__campus__university=university
    ).select_related('lecturer', 'student_group'))

    rooms     = list(Room.objects.filter(campus__university=university))
    timeslots = list(TimeSlot.objects.filter(university=university))

    # 2. Structural Checks
    if not courses:
        errors.append("No courses found for this university. Please define courses in the admin tab.")
    if not rooms:
        errors.append("No rooms found for this university. Please define classrooms or labs.")
    if not timeslots:
        errors.append("No time slots defined for this university. Please define teaching hours.")

    if errors:
        return False, errors, warnings

    # 3. Course-specific checks
    max_room_capacity = max(r.capacity for r in rooms)

    # Pre-group typed rooms once to avoid repeated list comprehensions
    typed_rooms_map = {}
    for r in rooms:
        typed_rooms_map.setdefault(r.room_type, []).append(r)
    typed_max_capacity_map = {
        rtype: max(r.capacity for r in rlist)
        for rtype, rlist in typed_rooms_map.items()
    }

    for course in courses:
        if not course.lecturer:
            errors.append(f"Course '{course.code}: {course.name}' does not have an assigned Lecturer.")
        if not course.student_group:
            errors.append(f"Course '{course.code}: {course.name}' does not have an assigned Student Group.")
            continue

        # Check if the course's campus has any classrooms defined
        campus = course.program.department.faculty.campus
        campus_rooms = [r for r in rooms if r.campus_id == campus.id]
        if not campus_rooms:
            warnings.append(
                f"NO_CAMPUS_ROOMS: Course '{course.code}' is scheduled on Campus '{campus.name}' "
                f"which has no rooms. Solver will fall back to university-wide rooms."
            )
            campus_rooms = rooms

        # Check if there is any room of the required type on this campus (or university if fallback)
        req_type = course.required_room_type
        campus_typed_rooms = [r for r in campus_rooms if r.room_type == req_type]
        if not campus_typed_rooms:
            warnings.append(
                f"NO_CAMPUS_TYPED_ROOMS: Course '{course.code}' requires a '{req_type}' room on Campus '{campus.name}', "
                f"but none exist. Solver will fall back to any room type on the campus/university."
            )
            campus_typed_rooms = campus_rooms

        group_size = course.student_group.size
        if group_size > max_room_capacity:
            errors.append(
                f"Course '{course.code}' has student group '{course.student_group.name}' of size {group_size}, "
                f"which exceeds the capacity of the largest room in the university ({max_room_capacity} seats)."
            )

        # Soft Capacity check for required room type
        req_type = course.required_room_type
        if req_type in typed_max_capacity_map:
            max_typed = typed_max_capacity_map[req_type]
            if group_size > max_typed:
                warnings.append(
                    f"ROOM_TYPE_MISMATCH: Course '{course.code}' needs a '{req_type}' room for "
                    f"'{course.student_group.name}' ({group_size} students), but the largest "
                    f"'{req_type}' room only fits {max_typed} students. "
                    f"Fix: increase a '{req_type}' room capacity to at least {group_size} in Resources → Rooms, then regenerate."
                )
        else:
            warnings.append(
                f"NO_LAB_ROOM: Course '{course.code}' requires room type '{req_type}', "
                f"but no '{req_type}' rooms are defined for this university. "
                f"Fix: add a '{req_type}' room in Resources → Rooms, then regenerate."
            )

    # 4. Global Capacity Check
    total_requested_slots = sum(c.duration_slots * c.sessions_per_week for c in courses)
    total_available_slots = len(rooms) * len(timeslots)
    if total_requested_slots > total_available_slots:
        errors.append(
            f"Insufficient timetable capacity. Total courses require {total_requested_slots} slots, "
            f"but you only have {total_available_slots} available slots "
            f"(Rooms ({len(rooms)}) × Time Slots ({len(timeslots)}))."
        )

    # 5. Lecturer Hours limit checks
    #
    # This check used to only add a *warning*, letting generation proceed.
    # That's misleading: total weekly hours is a property of course
    # assignments, not of the timetable arrangement — no matter how the
    # solver shuffles rooms/times, it cannot reduce a lecturer's total
    # course load. An over-allocated lecturer will show up as an
    # unavoidable hard conflict after "successful" generation. We now
    # block generation for this case (with a small tolerance buffer) so the
    # fix happens at the data layer, before wasting a solver run.
    OVER_ALLOCATION_TOLERANCE = 1.10  # allow 10% buffer before hard-blocking

    # Calculate average timeslot duration dynamically
    if timeslots:
        avg_duration = sum(
            ((ts.end_time.hour * 60 + ts.end_time.minute) - (ts.start_time.hour * 60 + ts.start_time.minute)) / 60.0
            for ts in timeslots
        ) / len(timeslots)
    else:
        avg_duration = 1.5

    lecturer_hours = {}
    lecturer_map   = {}
    for course in courses:
        if course.lecturer:
            lecturer_map[course.lecturer.id] = course.lecturer
            if avg_duration >= 2.5 and course.duration_slots >= 2:
                # When timeslots are 3 hours long, duration_slots=3 represents total contact hours (3h = 1 slot)
                session_hours = float(course.duration_slots)
            else:
                session_hours = course.duration_slots * avg_duration
            hours = session_hours * course.sessions_per_week
            lecturer_hours[course.lecturer.id] = lecturer_hours.get(course.lecturer.id, 0.0) + hours

    for lec_id, hours in lecturer_hours.items():
        lecturer = lecturer_map.get(lec_id)
        if not lecturer:
            continue
        if hours > lecturer.max_hours_per_week * OVER_ALLOCATION_TOLERANCE:
            errors.append(
                f"Lecturer '{lecturer.name}' is over-allocated: assigned {hours} hours of classes, "
                f"exceeding their maximum limit of {lecturer.max_hours_per_week} hours/week. "
                f"No timetable arrangement can fix this — either raise this lecturer's "
                f"max_hours_per_week, reassign some of their courses to another lecturer, "
                f"or reduce sessions_per_week/duration_slots for their courses, then regenerate."
            )
        elif hours > lecturer.max_hours_per_week:
            warnings.append(
                f"Lecturer '{lecturer.name}' is over-allocated: assigned {hours} hours of classes, "
                f"exceeding their maximum limit of {lecturer.max_hours_per_week} hours/week "
                f"(within {int((OVER_ALLOCATION_TOLERANCE - 1) * 100)}% tolerance — generation will proceed, "
                f"but consider rebalancing this lecturer's course load)."
            )

    # 6. FIX G3: Unused lecturers — use a DB-level exclude instead of loading all objects
    # Cap at 10 warnings to avoid overwhelming the UI with spammy output.
    assigned_lecturer_ids = {c.lecturer_id for c in courses if c.lecturer_id}
    unused_names = list(
        Lecturer.objects
        .filter(department__faculty__campus__university=university)
        .exclude(id__in=assigned_lecturer_ids)
        .values_list('name', flat=True)[:10]
    )
    for name in unused_names:
        warnings.append(f"Lecturer '{name}' is registered but has no assigned courses.")
    # Note count if truncated
    if len(unused_names) == 10:
        warnings.append(
            "…and more unassigned lecturers (only first 10 shown). "
            "Assign courses to lecturers via Resources → Courses."
        )

    # 7. Pre-flight: per-student-group timeslot feasibility (HARD ERROR)
    #
    # A student group can only be in one place at a time, so its total course
    # load (own courses + courses where it's a combined/shared attendee via
    # additional_student_groups) can never need more *distinct* timeslot-units
    # than actually exist in the week. Unlike room-type mismatches, this is
    # not something the solver can route around by trying another room or
    # campus — if the demand is impossible, it's impossible in every
    # arrangement. We check it here, before a solver run is wasted on it.
    total_timeslot_units = len(timeslots)
    group_demand = {}   # group_id -> total slot-units required
    group_map    = {}   # group_id -> StudentGroup instance

    for course in courses:
        if not course.student_group:
            continue
        demand = course.duration_slots * course.sessions_per_week
        g = course.student_group
        group_map[g.id] = g
        group_demand[g.id] = group_demand.get(g.id, 0) + demand

        # Combined/shared courses: every additional group attending also has
        # that slot occupied (they sit in the same session), so it counts
        # against their own weekly capacity too.
        for extra_group in course.additional_student_groups.all():
            group_map[extra_group.id] = extra_group
            group_demand[extra_group.id] = group_demand.get(extra_group.id, 0) + demand

    for g_id, demand in group_demand.items():
        group = group_map.get(g_id)
        if group and demand > total_timeslot_units:
            errors.append(
                f"Student group '{group.name}' requires {demand} class slot-units per week, "
                f"but only {total_timeslot_units} timeslots exist in total. No timetable "
                f"arrangement can fit this — a group can't attend two classes at once. "
                f"Fix: reduce this group's course load (fewer sessions_per_week or "
                f"duration_slots), split the group, or add more timeslots, then regenerate."
            )

    # 8. Pre-flight: per-campus room-capacity feasibility (HARD ERROR)
    #
    # The existing global capacity check (#4) only catches university-wide
    # shortfalls. A university can look fine in aggregate while one campus
    # is individually oversubscribed — courses can't "borrow" room-time from
    # a different campus's rooms in most setups, so we check each campus on
    # its own too.
    rooms_by_campus_id = {}
    for r in rooms:
        rooms_by_campus_id.setdefault(r.campus_id, []).append(r)

    campus_demand = {}  # campus_id -> total slot-units required
    campus_obj_by_id = {}
    for course in courses:
        campus = course.program.department.faculty.campus
        campus_obj_by_id[campus.id] = campus
        campus_demand[campus.id] = campus_demand.get(campus.id, 0) + (
            course.duration_slots * course.sessions_per_week
        )

    for campus_id, demand in campus_demand.items():
        campus_rooms_here = rooms_by_campus_id.get(campus_id, [])
        if not campus_rooms_here:
            # Already flagged as NO_CAMPUS_ROOMS above (warning) — the solver
            # falls back to university-wide rooms in that case, so it's not
            # a guaranteed infeasibility here.
            continue
        supply = len(campus_rooms_here) * total_timeslot_units
        if demand > supply:
            campus_obj = campus_obj_by_id.get(campus_id)
            campus_name = campus_obj.name if campus_obj else str(campus_id)
            errors.append(
                f"Campus '{campus_name}' requires {demand} room slot-units per week, but its "
                f"{len(campus_rooms_here)} room(s) × {total_timeslot_units} timeslots only supply "
                f"{supply}. No arrangement fits this on this campus alone. "
                f"Fix: add more rooms/timeslots to '{campus_name}', or move some courses to "
                f"another campus, then regenerate."
            )

    is_valid = len(errors) == 0
    return is_valid, errors, warnings

def validate_timetable_inputs(timetable):
    """
    Validates all input data for the timetable before running the solver.
    Returns: (is_valid, errors, warnings)
      - is_valid: Boolean (False if there are blocking errors)
      - errors: List of strings (blocking issues)
      - warnings: List of strings (non-blocking suggestions)
    """
    return validate_university_data(timetable.semester.university)

