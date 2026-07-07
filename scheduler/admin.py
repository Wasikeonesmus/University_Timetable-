from django.contrib import admin
from .models import (
    University, Campus, Faculty, Department, Program, Semester, 
    Course, Lecturer, StudentGroup, Room, TimeSlot, Constraint, Timetable, ScheduleSlot
)

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
    list_display = ('code', 'name', 'program', 'required_room_type', 'duration_slots', 'sessions_per_week', 'lecturer', 'student_group')
    search_fields = ('code', 'name')
    list_filter = ('program__department__faculty__campus__university', 'required_room_type')

@admin.register(Lecturer)
class LecturerAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'department')
    search_fields = ('name', 'email')
    list_filter = ('department__faculty__campus__university',)

@admin.register(StudentGroup)
class StudentGroupAdmin(admin.ModelAdmin):
    list_display = ('name', 'year', 'program', 'size')
    search_fields = ('name',)
    list_filter = ('year', 'program__department__faculty__campus__university')

@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ('name', 'campus', 'capacity', 'room_type')
    list_filter = ('campus__university', 'room_type')

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
