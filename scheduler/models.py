import uuid
from django.db import models
from django.contrib.auth.models import User

class University(models.Model):
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name_plural = "Universities"

class Campus(models.Model):
    university = models.ForeignKey(University, on_delete=models.CASCADE, related_name='campuses')
    name = models.CharField(max_length=255)

    def __str__(self):
        return f"{self.name} ({self.university.code})"

    class Meta:
        verbose_name_plural = "Campuses"

class Faculty(models.Model):
    campus = models.ForeignKey(Campus, on_delete=models.CASCADE, related_name='faculties')
    name = models.CharField(max_length=255)

    def __str__(self):
        return f"{self.name} - {self.campus.name}"

    class Meta:
        verbose_name_plural = "Faculties"

class Department(models.Model):
    faculty = models.ForeignKey(Faculty, on_delete=models.CASCADE, related_name='departments')
    name = models.CharField(max_length=255)

    def __str__(self):
        return f"{self.name} ({self.faculty.name})"

class Program(models.Model):
    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name='programs')
    name = models.CharField(max_length=255)

    def __str__(self):
        return f"{self.name} - {self.department.name}"

class Semester(models.Model):
    university = models.ForeignKey(University, on_delete=models.CASCADE, related_name='semesters')
    name = models.CharField(max_length=100)
    start_date = models.DateField()
    end_date = models.DateField()
    is_active = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.name} - {self.university.name}"

class Course(models.Model):
    ROOM_TYPE_CHOICES = [
        ('Lecture', 'Lecture Hall'),
        ('Lab', 'Laboratory'),
        ('Seminar', 'Seminar Room')
    ]
    # Delivery mode — how this course is taught
    DELIVERY_PHYSICAL = 'PH'
    DELIVERY_ONLINE   = 'OL'
    DELIVERY_HYBRID   = 'HY'
    DELIVERY_MODE_CHOICES = [
        (DELIVERY_PHYSICAL, 'Physical'),
        (DELIVERY_ONLINE,   'Online'),
        (DELIVERY_HYBRID,   'Hybrid'),
    ]
    program = models.ForeignKey(Program, on_delete=models.CASCADE, related_name='courses')
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=255)
    duration_slots = models.PositiveIntegerField(default=1)  # Number of consecutive timeslots per session
    sessions_per_week = models.PositiveIntegerField(
        default=1,
        help_text="How many times per week this course meets (e.g. 2 lectures + 1 lab = separate courses)."
    )
    delivery_mode = models.CharField(
        max_length=2,
        choices=DELIVERY_MODE_CHOICES,
        default=DELIVERY_PHYSICAL,
        help_text="How this course is delivered: Physical (in-person), Online (virtual/Zoom), or Hybrid."
    )
    required_room_type = models.CharField(max_length=50, choices=ROOM_TYPE_CHOICES, default='Lecture')
    lecturer = models.ForeignKey('Lecturer', on_delete=models.SET_NULL, null=True, blank=True, related_name='courses')
    student_group = models.ForeignKey('StudentGroup', on_delete=models.SET_NULL, null=True, blank=True, related_name='courses')
    required_features = models.ManyToManyField('RoomFeature', blank=True, related_name='required_by_courses')
    additional_student_groups = models.ManyToManyField('StudentGroup', blank=True, related_name='shared_courses')

    @property
    def is_online(self):
        return self.delivery_mode in (self.DELIVERY_ONLINE, self.DELIVERY_HYBRID)

    def __str__(self):
        return f"{self.code}: {self.name} ({self.lecturer} - {self.student_group})"

class Lecturer(models.Model):
    FULL_TIME = 'FT'
    PART_TIME = 'PT'
    LECTURER_TYPE_CHOICES = [
        (FULL_TIME, 'Full-Time'),
        (PART_TIME, 'Part-Time'),
    ]

    user = models.OneToOneField(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='lecturer_profile',
        help_text="Link this lecturer to a Django user account for login access."
    )
    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name='lecturers')
    staff_id = models.CharField(
        max_length=50, blank=True, null=True, unique=True,
        help_text="Official staff/employee ID (e.g. EMP/2024/001). Auto-displayed as STAFF-{pk} if blank."
    )
    name = models.CharField(max_length=255)
    email = models.EmailField(blank=True, null=True)
    lecturer_type = models.CharField(
        max_length=2,
        choices=LECTURER_TYPE_CHOICES,
        default=FULL_TIME,
        help_text="Employment contract type: Full-Time (permanent/tenured) or Part-Time (adjunct/sessional)."
    )
    max_hours_per_week = models.PositiveIntegerField(default=20)
    max_slots_per_day = models.PositiveIntegerField(default=4)
    is_active = models.BooleanField(default=True, help_text="Designates whether this lecturer is currently active in the timetabling system.")
    is_verified = models.BooleanField(default=False, help_text="Designates whether this lecturer's information has been verified against institutional records.")
    calendar_token = models.UUIDField(default=uuid.uuid4, null=True, blank=True, unique=True)
    profile_picture = models.ImageField(
        upload_to='lecturers/avatars/', null=True, blank=True,
        help_text="Upload a profile picture/avatar for the lecturer."
    )

    @property
    def is_full_time(self):
        return self.lecturer_type == self.FULL_TIME

    def __str__(self):
        return self.name

class StudentGroup(models.Model):
    YEAR_CHOICES = [
        (1, 'Year 1'),
        (2, 'Year 2'),
        (3, 'Year 3'),
        (4, 'Year 4'),
    ]
    program = models.ForeignKey(Program, on_delete=models.CASCADE, related_name='student_groups')
    name = models.CharField(max_length=255)
    size = models.PositiveIntegerField()
    year = models.PositiveIntegerField(choices=YEAR_CHOICES, default=1, null=True, blank=True, help_text="Academic year (e.g. Year 1, 2, 3)")
    calendar_token = models.UUIDField(default=uuid.uuid4, null=True, blank=True, unique=True)
    parent_group = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='sub_groups',
        help_text="Link this group to a parent merged group if they attend classes together."
    )

    def __str__(self):
        return f"{self.name} (Size: {self.size})"

class Room(models.Model):
    ROOM_TYPE_CHOICES = [
        ('Lecture', 'Lecture Hall'),
        ('Lab', 'Laboratory'),
        ('Seminar', 'Seminar Room'),
        ('Virtual', 'Virtual / Online Room'),
    ]
    campus = models.ForeignKey(Campus, on_delete=models.CASCADE, related_name='rooms')
    name = models.CharField(max_length=255)
    capacity = models.PositiveIntegerField()
    room_type = models.CharField(max_length=50, choices=ROOM_TYPE_CHOICES, default='Lecture')
    is_virtual = models.BooleanField(
        default=False,
        help_text="Tick if this is a virtual/online room (Zoom, Teams, Google Meet, etc.). "
                  "Virtual rooms have no physical location — campus travel rules are skipped."
    )
    building = models.ForeignKey('Building', on_delete=models.SET_NULL, null=True, blank=True, related_name='rooms')
    features = models.ManyToManyField('RoomFeature', blank=True, related_name='rooms')

    def __str__(self):
        tag = ' [Virtual]' if self.is_virtual else ''
        return f"{self.name} ({self.room_type}, Cap: {self.capacity}){tag}"

class TimeSlot(models.Model):
    DAY_CHOICES = [
        (1, 'Monday'),
        (2, 'Tuesday'),
        (3, 'Wednesday'),
        (4, 'Thursday'),
        (5, 'Friday'),
        (6, 'Saturday'),
        (7, 'Sunday')
    ]
    university = models.ForeignKey(University, on_delete=models.CASCADE, related_name='time_slots')
    day_of_week = models.PositiveIntegerField(choices=DAY_CHOICES)
    start_time = models.TimeField()
    end_time = models.TimeField()
    slot_number = models.PositiveIntegerField()
    is_evening = models.BooleanField(default=False)

    class Meta:
        ordering = ['day_of_week', 'start_time', 'slot_number']
        unique_together = ('university', 'day_of_week', 'slot_number')

    def __str__(self):
        def _fmt(t):
            if not t:
                return ""
            if hasattr(t, 'strftime'):
                return t.strftime('%H:%M')
            return str(t)[:5]
        return f"{self.get_day_of_week_display()} Slot {self.slot_number} ({_fmt(self.start_time)}-{_fmt(self.end_time)})"


class Constraint(models.Model):
    CONSTRAINT_TYPE_CHOICES = [
        ('LECTURER_AVAILABILITY', 'Lecturer Availability'),
        ('ROOM_CAPACITY', 'Room Capacity Check'),
        ('MAX_CLASSES_PER_DAY', 'Lecturer Max Classes Per Day'),
        ('NO_EVENING_CLASSES', 'Avoid Evening Classes'),
        ('ROOM_PREFERENCE', 'Preferred Rooms for Course'),
        ('LAB_ONLY_COURSE', 'Lab-Only Courses'),
        ('STUDENT_MAX_CLASSES_PER_DAY', 'Student Group Max Classes Per Day'),
        ('LECTURER_MAX_CONSECUTIVE_SLOTS', 'Lecturer Max Consecutive Slots'),
        ('LECTURER_MAX_DAYS_PER_WEEK', 'Lecturer Max Teaching Days Per Week'),
        ('CUSTOM_RULE', 'Custom Rule Configuration'),
    ]
    university = models.ForeignKey(University, on_delete=models.CASCADE, related_name='constraints')
    name = models.CharField(max_length=255)
    constraint_type = models.CharField(max_length=50, choices=CONSTRAINT_TYPE_CHOICES)
    is_hard = models.BooleanField(default=True)
    weight = models.PositiveIntegerField(default=10, help_text="Priority/weight for soft constraints (ignored for hard constraints)")
    parameters = models.JSONField(default=dict, blank=True, help_text="JSON payload with rule params")

    @property
    def description(self):
        p = self.parameters or {}
        ctype = self.constraint_type

        # Helper to fetch lecturer name
        def get_lecturer_name(l_id):
            if not l_id:
                return "All Lecturers (Global Default)"
            try:
                from .models import Lecturer
                return Lecturer.objects.get(id=l_id).name
            except Exception:
                return f"Lecturer #{l_id}"

        # Helper to fetch course name
        def get_course_name(c_id):
            if not c_id:
                return "Any Course"
            try:
                from .models import Course
                c = Course.objects.get(id=c_id)
                return f"{c.code}: {c.name}"
            except Exception:
                return f"Course #{c_id}"

        # Helper to fetch room name
        def get_room_name(r_id):
            try:
                from .models import Room
                return Room.objects.get(id=r_id).name
            except Exception:
                return f"Room #{r_id}"

        # Helper to fetch student group name
        def get_group_name(g_id):
            try:
                from .models import StudentGroup
                return StudentGroup.objects.get(id=g_id).name
            except Exception:
                return f"Group #{g_id}"

        # Helper to fetch timeslot name
        def get_slot_name(ts_id):
            try:
                from .models import TimeSlot
                ts = TimeSlot.objects.get(id=ts_id)
                return f"{ts.get_day_of_week_display()} {ts.start_time.strftime('%H:%M')}-{ts.end_time.strftime('%H:%M')}"
            except Exception:
                return f"Slot #{ts_id}"

        if ctype == 'LECTURER_AVAILABILITY':
            l_name = get_lecturer_name(p.get('lecturer_id'))
            slots = p.get('unavailable_slots', [])
            slots_str = ", ".join(get_slot_name(sid) for sid in slots) if slots else "None"
            return f"Lecturer '{l_name}' unavailable slots: {slots_str}"

        elif ctype == 'ROOM_CAPACITY':
            return "Student group size must fit room capacity."

        elif ctype == 'MAX_CLASSES_PER_DAY':
            l_name = get_lecturer_name(p.get('lecturer_id'))
            val = p.get('max_classes', 4)
            return f"Max {val} classes per day for: {l_name}"

        elif ctype == 'NO_EVENING_CLASSES':
            return "Avoid scheduling classes during evening slots."

        elif ctype == 'ROOM_PREFERENCE':
            c_name = get_course_name(p.get('course_id'))
            rms = p.get('preferred_rooms', [])
            rms_str = ", ".join(get_room_name(rid) for rid in rms) if rms else "None"
            return f"Course '{c_name}' preferred rooms: {rms_str}"

        elif ctype == 'LAB_ONLY_COURSE':
            c_name = get_course_name(p.get('course_id'))
            return f"Course '{c_name}' must be scheduled in a Laboratory space"

        elif ctype == 'STUDENT_MAX_CLASSES_PER_DAY':
            g_name = get_group_name(p.get('student_group_id'))
            val = p.get('max_classes', 4)
            return f"Max {val} classes per day for student group: {g_name}"

        elif ctype == 'LECTURER_MAX_CONSECUTIVE_SLOTS':
            l_name = get_lecturer_name(p.get('lecturer_id'))
            val = p.get('max_consecutive', 3)
            return f"Max {val} consecutive teaching slots for: {l_name}"

        elif ctype == 'LECTURER_MAX_DAYS_PER_WEEK':
            l_name = get_lecturer_name(p.get('lecturer_id'))
            val = p.get('max_days', 5)
            return f"Max {val} teaching days per week for: {l_name}"

        elif ctype == 'CUSTOM_RULE':
            t_type = p.get('target_type', 'LECTURER')
            t_id = p.get('target_id')
            r_type = p.get('rule_type', 'FORBID_ROOM')
            val = p.get('rule_value', [])
            val_list = val if isinstance(val, list) else [val] if val else []

            # Target label
            if t_type == 'LECTURER':
                t_label = f"Lecturer '{get_lecturer_name(t_id)}'"
            elif t_type == 'COURSE':
                t_label = f"Course '{get_course_name(t_id)}'"
            else:
                t_label = f"Student Group '{get_group_name(t_id)}'"

            # Rule label
            if 'ROOM' in r_type:
                vals_str = ", ".join(get_room_name(vid) for vid in val_list)
                verb = "forbidden from rooms" if "FORBID" in r_type else "restricted strictly to rooms"
            else:
                vals_str = ", ".join(get_slot_name(vid) for vid in val_list)
                verb = "forbidden from slots" if "FORBID" in r_type else "restricted strictly to slots"

            return f"{t_label} is {verb}: [{vals_str}]"

        return str(p)

    def __str__(self):
        prefix = "[HARD]" if self.is_hard else f"[SOFT: W={self.weight}]"
        return f"{prefix} {self.name} ({self.get_constraint_type_display()})"

class Timetable(models.Model):
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('HOD_REVIEW', 'Head of Department Review'),
        ('DEAN_REVIEW', 'Dean Review'),
        ('REGISTRAR_REVIEW', 'Registrar Review'),
        ('DVC_REVIEW', 'DVC Academic Review'),
        ('PUBLISHED', 'Published'),
    ]

    semester = models.ForeignKey(Semester, on_delete=models.CASCADE, related_name='timetables')
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=False)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='DRAFT')

    def __str__(self):
        return f"{self.name} ({self.semester.name}) [{self.get_status_display()}]"

class ScheduleSlot(models.Model):
    timetable = models.ForeignKey(Timetable, on_delete=models.CASCADE, related_name='slots')
    course = models.ForeignKey(Course, on_delete=models.CASCADE)
    lecturer = models.ForeignKey(Lecturer, on_delete=models.CASCADE)
    room = models.ForeignKey(Room, on_delete=models.CASCADE)
    time_slot = models.ForeignKey(TimeSlot, on_delete=models.CASCADE)
    student_group = models.ForeignKey(StudentGroup, on_delete=models.CASCADE)
    google_event_id = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        # Structural double-booking guard: even if a bug in the solver's Python
        # logic ever produces an overlapping assignment, the database itself
        # will refuse to persist it (IntegrityError) instead of silently
        # writing a conflicting timetable. This does NOT replace the solver's
        # own conflict avoidance — it's a last-line-of-defense safety net.
        constraints = [
            models.UniqueConstraint(
                fields=['timetable', 'room', 'time_slot'],
                name='uniq_room_per_slot',
            ),
            models.UniqueConstraint(
                fields=['timetable', 'lecturer', 'time_slot'],
                name='uniq_lecturer_per_slot',
            ),
            models.UniqueConstraint(
                fields=['timetable', 'student_group', 'time_slot'],
                name='uniq_group_per_slot',
            ),
        ]
        indexes = [
            models.Index(fields=['timetable', 'time_slot']),
        ]

    def __str__(self):
        return f"{self.course.code} in {self.room.name} by {self.lecturer.name} at {self.time_slot}"


class GenerationLog(models.Model):
    """
    Audit trail for every timetable generation run.
    Records solver outcome, performance stats, and validation issues.
    """
    STATUS_CHOICES = [
        ('PENDING',   'Pending'),     # FIX BUG 3: Added PENDING — used throughout signals/views as sentinel
        ('OPTIMAL',   'Optimal'),
        ('FEASIBLE',  'Feasible'),
        ('INFEASIBLE','Infeasible'),
        ('ERROR',     'Error'),
    ]

    timetable = models.ForeignKey(Timetable, on_delete=models.CASCADE, related_name='generation_logs')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    message = models.TextField()
    solver_score = models.IntegerField(null=True, blank=True, help_text="Objective value returned by the solver (lower is better)")
    solve_time_seconds = models.FloatField(null=True, blank=True)
    courses_scheduled = models.IntegerField(default=0)
    hard_conflicts_found = models.IntegerField(default=0)
    soft_conflicts_found = models.IntegerField(default=0)
    validation_errors = models.JSONField(default=list, blank=True)
    validation_warnings = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.status}] {self.timetable.name} @ {self.created_at.strftime('%Y-%m-%d %H:%M')}"


class LecturerAvailability(models.Model):
    """
    Tracks which time slots a lecturer is available to teach.
    Lecturers set this themselves via the self-service portal.
    The solver reads these records as hard constraints.
    """
    lecturer = models.ForeignKey(
        Lecturer, on_delete=models.CASCADE, related_name='availability_slots'
    )
    time_slot = models.ForeignKey(
        TimeSlot, on_delete=models.CASCADE, related_name='lecturer_availability'
    )
    is_available = models.BooleanField(
        default=True,
        help_text="True = lecturer can teach in this slot; False = unavailable."
    )
    note = models.CharField(
        max_length=255, blank=True,
        help_text="Optional note (e.g. 'Hospital appointment')"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('lecturer', 'time_slot')
        ordering = ['time_slot__day_of_week', 'time_slot__slot_number']
        verbose_name_plural = 'Lecturer Availabilities'

    def __str__(self):
        status = 'Available' if self.is_available else 'Unavailable'
        try:
            lecturer_name = self.lecturer.name
        except Exception:
            lecturer_name = f'Lecturer#{self.lecturer_id}'
        try:
            ts_str = str(self.time_slot)
        except Exception:
            ts_str = f'TimeSlot#{self.time_slot_id}'
        return f"{lecturer_name} — {ts_str} [{status}]"


class RoomFeature(models.Model):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name


class Building(models.Model):
    campus = models.ForeignKey(Campus, on_delete=models.CASCADE, related_name='buildings')
    name = models.CharField(max_length=255)

    def __str__(self):
        return f"{self.name} ({self.campus.name})"


class BuildingDistance(models.Model):
    from_building = models.ForeignKey(Building, on_delete=models.CASCADE, related_name='distances_from')
    to_building = models.ForeignKey(Building, on_delete=models.CASCADE, related_name='distances_to')
    walking_time_minutes = models.PositiveIntegerField()

    class Meta:
        unique_together = ('from_building', 'to_building')

    def __str__(self):
        return f"{self.from_building.name} -> {self.to_building.name} ({self.walking_time_minutes} min)"


class LecturerTimeSlotPreference(models.Model):
    PREFERENCE_CHOICES = [
        ('prefer', 'Prefer'),
        ('dislike', 'Dislike')
    ]
    lecturer = models.ForeignKey(Lecturer, on_delete=models.CASCADE, related_name='slot_preferences')
    time_slot = models.ForeignKey(TimeSlot, on_delete=models.CASCADE)
    preference_level = models.CharField(max_length=10, choices=PREFERENCE_CHOICES, default='prefer')

    class Meta:
        unique_together = ('lecturer', 'time_slot')

    def __str__(self):
        return f"{self.lecturer.name} - {self.time_slot} [{self.preference_level}]"


class Subscription(models.Model):
    TIER_CHOICES = [
        ('free', 'Free Tier'),
        ('growth', 'Growth Tier'),
        ('enterprise', 'Enterprise Tier'),
    ]
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('past_due', 'Past Due'),
        ('canceled', 'Canceled'),
    ]
    university = models.OneToOneField(University, on_delete=models.CASCADE, related_name='subscription')
    tier = models.CharField(max_length=20, choices=TIER_CHOICES, default='free')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    max_rooms = models.PositiveIntegerField(default=10)
    max_courses = models.PositiveIntegerField(default=50)
    start_date = models.DateField(auto_now_add=True)
    end_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.university.name} - {self.get_tier_display()} ({self.get_status_display()})"


class Notification(models.Model):
    LEVEL_CHOICES = [
        ('info', 'Info'),
        ('success', 'Success'),
        ('warning', 'Warning'),
        ('danger', 'Danger'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=255)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    link = models.CharField(max_length=255, blank=True, null=True)
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, default='info')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Notification for {self.user.username}: {self.title} (Read: {self.is_read})"


class Announcement(models.Model):
    university = models.ForeignKey(
        University, on_delete=models.CASCADE, related_name='announcements',
        null=True, blank=True,
        help_text="If null, this announcement is system-wide for all users."
    )
    title = models.CharField(max_length=255)
    content = models.TextField()
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='announcements_created')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        scope = self.university.name if self.university else "System-wide"
        return f"{self.title} ({scope}) @ {self.created_at.strftime('%Y-%m-%d')}"


class AttendanceSession(models.Model):
    """
    A single attendance-taking session opened by a lecturer for a given
    ScheduleSlot on a specific date. The lecturer manually marks students
    present or absent from the student group linked to that slot.
    """
    schedule_slot = models.ForeignKey(
        ScheduleSlot, on_delete=models.CASCADE, related_name='attendance_sessions'
    )
    date = models.DateField(help_text="The actual calendar date of this class session.")
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    is_active = models.BooleanField(
        default=True,
        help_text="True while the lecturer has the session open; False after it is closed."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-date', '-created_at']
        unique_together = ('schedule_slot', 'date')

    def __str__(self):
        return (
            f"Attendance: {self.schedule_slot.course.code} "
            f"on {self.date} ({'Open' if self.is_active else 'Closed'})"
        )

    @property
    def attendance_rate(self):
        total = self.records.count()
        if total == 0:
            return 0
        present = self.records.filter(is_present=True).count()
        return round((present / total) * 100)


class AttendanceRecord(models.Model):
    """
    One row per student per AttendanceSession. The lecturer checks each
    student off manually; default is absent (is_present=False) so unmarked
    students are automatically recorded as absent when session closes.
    """
    session = models.ForeignKey(
        AttendanceSession, on_delete=models.CASCADE, related_name='records'
    )
    student_name = models.CharField(max_length=255)
    student_id = models.CharField(
        max_length=50, blank=True,
        help_text="Optional student registration number for reference."
    )
    is_present = models.BooleanField(default=False)
    marked_at = models.DateTimeField(auto_now=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['student_name']
        unique_together = ('session', 'student_name')

    def __str__(self):
        status = 'Present' if self.is_present else 'Absent'
        return f"{self.student_name} — {self.session.schedule_slot.course.code} [{status}]"


class ApprovalLog(models.Model):
    timetable = models.ForeignKey(Timetable, on_delete=models.CASCADE, related_name='approval_logs')
    stage = models.CharField(max_length=50) # e.g. HOD_REVIEW, DEAN_REVIEW, etc.
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=20) # e.g. SUBMIT, APPROVE, REJECT
    comments = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        actor_name = self.actor.username if self.actor else "System"
        return f"{self.timetable.name} - {self.stage} [{self.action}] by {actor_name} at {self.created_at.strftime('%Y-%m-%d %H:%M')}"


class FieldMapping(models.Model):
    system_name = models.CharField(max_length=100) # e.g., 'BANNER_SIS', 'WORKDAY_HR'
    local_model = models.CharField(max_length=100) # e.g., 'Course', 'Lecturer'
    external_field = models.CharField(max_length=100) # e.g., 'crn_code', 'employee_id'
    local_field = models.CharField(max_length=100) # e.g., 'code', 'staff_id'

    def __str__(self):
        return f"{self.system_name}: {self.local_model}.{self.local_field} -> {self.external_field}"


class ImportAuditLog(models.Model):
    """
    Persists a record of every smart-import run: who imported which file,
    when, what changed, and any near-duplicate warnings.
    """
    IMPORT_TYPE_CHOICES = [
        ('smart', 'Smart Import'),
        ('manual', 'Manual Import'),
    ]

    university   = models.ForeignKey(University, on_delete=models.CASCADE, related_name='import_logs')
    imported_by  = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='import_logs')
    file_name    = models.CharField(max_length=255)
    import_type  = models.CharField(max_length=20, choices=IMPORT_TYPE_CHOICES, default='smart')
    # Per-entity counts
    created_count  = models.JSONField(default=dict, help_text="{'lecturers': 5, 'rooms': 3, ...}")
    updated_count  = models.JSONField(default=dict)
    # Near-duplicate flags from fuzzy matching
    flagged_dupes  = models.JSONField(default=list,
        help_text="List of {entity_type, incoming, existing, similarity} dicts")
    # Warnings from post-import validation
    warnings       = models.JSONField(default=list)
    imported_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-imported_at']
        verbose_name = 'Import Audit Log'
        verbose_name_plural = 'Import Audit Logs'

    def __str__(self):
        actor = self.imported_by.username if self.imported_by else 'System'
        ts = self.imported_at.strftime('%Y-%m-%d %H:%M') if self.imported_at else '—'
        return f"Import by {actor} — {self.file_name} @ {ts}"
