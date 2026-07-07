import datetime

def escape_text(text):
    """
    Escape text characters for iCalendar compliance.
    """
    if not text:
        return ""
    return str(text).replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")

def get_first_occurrence(start_date, day_of_week):
    """
    Find the first date on or after start_date that matches day_of_week.
    day_of_week in DB is 1 (Monday) to 7 (Sunday).
    Python's weekday() is 0 (Monday) to 6 (Sunday).
    """
    target_weekday = day_of_week - 1
    start_weekday = start_date.weekday()
    days_ahead = target_weekday - start_weekday
    if days_ahead < 0:
        days_ahead += 7
    return start_date + datetime.timedelta(days=days_ahead)

def _generate_empty_ics(name):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//Multi University Timetable System//{name}//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{name}",
        "END:VCALENDAR"
    ]
    return "\r\n".join(lines)

def _generate_ics_from_slots(slots, semester, name):
    start_date = semester.start_date
    end_date = semester.end_date
    until_str = end_date.strftime("%Y%m%d") + "T235959Z"
    dtstamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//Multi University Timetable System//{name}//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{name}",
        "X-WR-TIMEZONE:Africa/Nairobi",
    ]

    for slot in slots:
        ts = slot.time_slot
        first_date = get_first_occurrence(start_date, ts.day_of_week)

        # Build local start/end times
        dtstart = datetime.datetime.combine(first_date, ts.start_time).strftime("%Y%m%dT%H%M%S")
        dtend = datetime.datetime.combine(first_date, ts.end_time).strftime("%Y%m%dT%H%M%S")

        summary = f"{slot.course.code}: {slot.course.name}"
        location = f"{slot.room.name} ({slot.room.get_room_type_display()}, Cap: {slot.room.capacity})"
        description = f"Lecturer: {slot.lecturer.name}\\nStudent Group: {slot.student_group.name}\\nUniversity: {semester.university.name}"

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:slot_{slot.id}_{dtstamp}@timetable_system",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART;TZID=Africa/Nairobi:{dtstart}",
            f"DTEND;TZID=Africa/Nairobi:{dtend}",
            f"RRULE:FREQ=WEEKLY;UNTIL={until_str}",
            f"SUMMARY:{escape_text(summary)}",
            f"LOCATION:{escape_text(location)}",
            f"DESCRIPTION:{escape_text(description)}",
            "END:VEVENT"
        ])

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


def generate_ics_content(timetable):
    """
    Generate standard iCalendar (.ics) string for all slots in a timetable.
    """
    slots = timetable.slots.select_related('course', 'lecturer', 'room', 'time_slot', 'student_group').all()
    return _generate_ics_from_slots(slots, timetable.semester, f"Timetable {timetable.name}")

def generate_lecturer_ics(lecturer, timetable=None):
    """
    Generate standard iCalendar (.ics) string for a lecturer's schedule.
    """
    from scheduler.models import Timetable, ScheduleSlot
    if not timetable:
        university = lecturer.department.faculty.campus.university
        timetable = Timetable.objects.filter(semester__university=university, is_active=True).first()
        if not timetable:
            timetable = Timetable.objects.filter(semester__university=university).order_by('-created_at').first()

    if not timetable:
        return _generate_empty_ics(f"Lecturer {lecturer.name}")

    slots = ScheduleSlot.objects.filter(timetable=timetable, lecturer=lecturer).select_related(
        'course', 'lecturer', 'room', 'time_slot', 'student_group'
    )
    return _generate_ics_from_slots(slots, timetable.semester, f"Lecturer {lecturer.name}")

def generate_student_group_ics(student_group, timetable=None):
    """
    Generate standard iCalendar (.ics) string for a student group's schedule.
    """
    from scheduler.models import Timetable, ScheduleSlot
    if not timetable:
        university = student_group.program.department.faculty.campus.university
        timetable = Timetable.objects.filter(semester__university=university, is_active=True).first()
        if not timetable:
            timetable = Timetable.objects.filter(semester__university=university).order_by('-created_at').first()

    if not timetable:
        return _generate_empty_ics(f"Group {student_group.name}")

    slots = ScheduleSlot.objects.filter(timetable=timetable, student_group=student_group).select_related(
        'course', 'lecturer', 'room', 'time_slot', 'student_group'
    )
    return _generate_ics_from_slots(slots, timetable.semester, f"Group {student_group.name}")
