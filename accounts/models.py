from django.db import models
from django.contrib.auth.models import User
from scheduler.models import University, Lecturer, StudentGroup


class UserProfile(models.Model):
    """
    Extends Django's built-in User model with role and university context.
    One profile per user.
    """
    ROLE_CHOICES = [
        ('admin', 'Super Admin'),
        ('institution_admin', 'Institution Admin'),
        ('registrar', 'Registrar'),
        ('dvc_academic', 'DVC Academic'),
        ('dean', 'Dean'),
        ('hod', 'Head of Department'),
        ('timetable_officer', 'Timetable Officer'),
        ('scheduler', 'Scheduler'),
        ('lecturer', 'Lecturer'),
        ('student', 'Student'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='student')
    university = models.ForeignKey(
        University, on_delete=models.SET_NULL, null=True, blank=True,
        help_text="The university this user belongs to."
    )
    # For lecturers: link to their Lecturer record
    lecturer = models.OneToOneField(
        Lecturer, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='user_profile'
    )
    # For students: link to their StudentGroup record
    student_group = models.ForeignKey(
        StudentGroup, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='student_profiles'
    )
    bio = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.username} [{self.get_role_display()}]"

    @property
    def is_admin(self):
        return self.role == 'admin'

    @property
    def is_institution_admin(self):
        return self.role == 'institution_admin'

    @property
    def is_registrar(self):
        return self.role == 'registrar'

    @property
    def is_dvc(self):
        return self.role == 'dvc_academic'

    @property
    def is_dean(self):
        return self.role == 'dean'

    @property
    def is_hod(self):
        return self.role == 'hod'

    @property
    def is_timetable_officer(self):
        return self.role == 'timetable_officer'

    @property
    def is_scheduler(self):
        return self.role in ('scheduler', 'timetable_officer')

    @property
    def is_lecturer(self):
        return self.role == 'lecturer'

    @property
    def is_student(self):
        return self.role == 'student'

    @property
    def can_manage(self):
        """Admins and schedulers can create/edit timetables."""
        return self.role in ('admin', 'institution_admin', 'scheduler', 'timetable_officer', 'registrar')


class GoogleCalendarToken(models.Model):
    """
    Stores the OAuth2 credentials for users to sync schedules to Google Calendar.
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='google_token')
    token = models.TextField()  # JSON-serialized credentials (access/refresh tokens)
    email = models.EmailField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Google Calendar Token for {self.user.username} ({self.email or 'No email'})"

