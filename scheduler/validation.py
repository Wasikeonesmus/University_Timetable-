from .models import Course, Room, TimeSlot, Lecturer, StudentGroup, Timetable

def validate_timetable_inputs(timetable):
    """
    Validates all input data for the timetable before running the solver.
    Returns: (is_valid, errors, warnings)
      - is_valid: Boolean (False if there are blocking errors)
      - errors: List of strings (blocking issues)
      - warnings: List of strings (non-blocking suggestions)
    """
    errors   = []
    warnings = []

    university = timetable.semester.university

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
    lecturer_hours = {}
    lecturer_map   = {}
    for course in courses:
        if course.lecturer:
            lecturer_map[course.lecturer.id] = course.lecturer
            hours = course.duration_slots * course.sessions_per_week * 1.5
            lecturer_hours[course.lecturer.id] = lecturer_hours.get(course.lecturer.id, 0.0) + hours

    for lec_id, hours in lecturer_hours.items():
        lecturer = lecturer_map.get(lec_id)
        if lecturer and hours > lecturer.max_hours_per_week:
            warnings.append(
                f"Lecturer '{lecturer.name}' is over-allocated: assigned {hours} hours of classes, "
                f"exceeding their maximum limit of {lecturer.max_hours_per_week} hours/week."
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

    is_valid = len(errors) == 0
    return is_valid, errors, warnings
