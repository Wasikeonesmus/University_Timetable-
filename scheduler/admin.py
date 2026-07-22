from django.contrib import admin
from .models import (
    University, Campus, Faculty, Department, Program, Semester, 
    Course, Lecturer, StudentGroup, Room, TimeSlot, Constraint, Timetable, ScheduleSlot,
    ImportAuditLog, GenerationLog, LecturerAvailability, RoomFeature, Building,
    BuildingDistance, LecturerTimeSlotPreference, Notification, Announcement,
    AttendanceSession, AttendanceRecord, ApprovalLog, FieldMapping
)

@admin.register(ImportAuditLog)
class ImportAuditLogAdmin(admin.ModelAdmin):
    list_display = ('file_name', 'university', 'imported_by', 'import_type', 'imported_at')
    list_filter = ('university', 'import_type', 'imported_at')
    search_fields = ('file_name', 'imported_by__username')
    readonly_fields = ('imported_at',)

@admin.register(University)
class UniversityAdmin(admin.ModelAdmin):
    list_display = ('name', 'code')
    search_fields = ('name', 'code')

@admin.register(Campus)
class CampusAdmin(admin.ModelAdmin):
    list_display = ('name', 'university')
    list_filter = ('university',)

@admin.register(Faculty)
class FacultyAdmin(admin.ModelAdmin):
    list_display = ('name', 'campus')
    list_filter = ('campus__university',)

@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ('name', 'faculty')
    list_filter = ('faculty__campus__university',)

@admin.register(Program)
class ProgramAdmin(admin.ModelAdmin):
    list_display = ('name', 'department')
    list_filter = ('department__faculty__campus__university',)

@admin.register(Semester)
class SemesterAdmin(admin.ModelAdmin):
    list_display = ('name', 'university', 'is_active')
    list_filter = ('university', 'is_active')

@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'program', 'delivery_mode', 'required_room_type', 'duration_slots', 'sessions_per_week', 'lecturer', 'student_group')
    search_fields = ('code', 'name')
    list_filter = ('program__department__faculty__campus__university', 'delivery_mode', 'required_room_type')

@admin.register(Lecturer)
class LecturerAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'department', 'lecturer_type', 'max_hours_per_week', 'max_slots_per_day')
    search_fields = ('name', 'email')
    list_filter = ('department__faculty__campus__university', 'lecturer_type')

@admin.register(StudentGroup)
class StudentGroupAdmin(admin.ModelAdmin):
    list_display = ('name', 'year', 'program', 'size')
    search_fields = ('name',)
    list_filter = ('year', 'program__department__faculty__campus__university')

@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ('name', 'campus', 'capacity', 'room_type', 'is_virtual')
    list_filter = ('campus__university', 'room_type', 'is_virtual')

@admin.register(TimeSlot)
class TimeSlotAdmin(admin.ModelAdmin):
    list_display = ('university', 'day_of_week', 'slot_number', 'start_time', 'end_time', 'is_evening')
    list_filter = ('university', 'day_of_week', 'is_evening')

@admin.register(Constraint)
class ConstraintAdmin(admin.ModelAdmin):
    list_display = ('name', 'university', 'constraint_type', 'is_hard', 'weight')
    list_filter = ('university', 'constraint_type', 'is_hard')

@admin.register(Timetable)
class TimetableAdmin(admin.ModelAdmin):
    list_display = ('name', 'semester', 'is_active')
    list_filter = ('semester__university', 'is_active')

@admin.register(ScheduleSlot)
class ScheduleSlotAdmin(admin.ModelAdmin):
    list_display = ('timetable', 'course', 'lecturer', 'room', 'time_slot', 'student_group')
    list_filter = ('timetable', 'room', 'time_slot')

@admin.register(GenerationLog)
class GenerationLogAdmin(admin.ModelAdmin):
    list_display = ('timetable', 'status', 'solver_score', 'solve_time_seconds', 'courses_scheduled', 'created_at')
    list_filter = ('status', 'timetable__semester__university')
    readonly_fields = ('created_at',)

@admin.register(LecturerAvailability)
class LecturerAvailabilityAdmin(admin.ModelAdmin):
    list_display = ('lecturer', 'time_slot', 'is_available')
    list_filter = ('is_available', 'lecturer__department__faculty__campus__university')

@admin.register(RoomFeature)
class RoomFeatureAdmin(admin.ModelAdmin):
    list_display = ('name',)
    search_fields = ('name',)

@admin.register(Building)
class BuildingAdmin(admin.ModelAdmin):
    list_display = ('name', 'campus')
    list_filter = ('campus',)

@admin.register(BuildingDistance)
class BuildingDistanceAdmin(admin.ModelAdmin):
    list_display = ('from_building', 'to_building', 'walking_time_minutes')

@admin.register(LecturerTimeSlotPreference)
class LecturerTimeSlotPreferenceAdmin(admin.ModelAdmin):
    list_display = ('lecturer', 'time_slot', 'preference_level')
    list_filter = ('preference_level',)

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('user', 'title', 'is_read', 'created_at')
    list_filter = ('is_read', 'created_at')
    search_fields = ('title', 'message')

@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ('title', 'university', 'created_by', 'created_at')
    list_filter = ('university', 'created_at')
    search_fields = ('title', 'content')

@admin.register(AttendanceSession)
class AttendanceSessionAdmin(admin.ModelAdmin):
    list_display = ('schedule_slot', 'date', 'token', 'is_active')
    list_filter = ('is_active', 'date')

@admin.register(AttendanceRecord)
class AttendanceRecordAdmin(admin.ModelAdmin):
    list_display = ('session', 'student_name', 'is_present', 'marked_at')
    list_filter = ('is_present', 'marked_at')

@admin.register(ApprovalLog)
class ApprovalLogAdmin(admin.ModelAdmin):
    list_display = ('timetable', 'actor', 'action', 'created_at')
    list_filter = ('action', 'created_at')

@admin.register(FieldMapping)
class FieldMappingAdmin(admin.ModelAdmin):
    list_display = ('system_name', 'local_model', 'local_field')
    list_filter = ('system_name', 'local_model')
