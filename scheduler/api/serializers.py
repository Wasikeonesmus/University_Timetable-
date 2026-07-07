"""
scheduler/api/serializers.py
-----------------------------
Django REST Framework serializers for the Timetable Scheduler API.
"""
from rest_framework import serializers
from scheduler.models import (
    University, Campus, Faculty, Department, Program,
    Semester, Course, Lecturer, StudentGroup, Room,
    TimeSlot, Timetable, ScheduleSlot, Constraint, GenerationLog
)


class UniversitySerializer(serializers.ModelSerializer):
    class Meta:
        model = University
        fields = ['id', 'name', 'code']


class CampusSerializer(serializers.ModelSerializer):
    university_name = serializers.CharField(source='university.name', read_only=True)

    class Meta:
        model = Campus
        fields = ['id', 'name', 'university', 'university_name']


class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = ['id', 'name', 'faculty']


class LecturerSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source='department.name', read_only=True)

    class Meta:
        model = Lecturer
        fields = ['id', 'name', 'email', 'department', 'department_name', 'max_hours_per_week', 'profile_picture']


class StudentGroupSerializer(serializers.ModelSerializer):
    program_name = serializers.CharField(source='program.name', read_only=True)

    class Meta:
        model = StudentGroup
        fields = ['id', 'name', 'size', 'program', 'program_name']


class RoomSerializer(serializers.ModelSerializer):
    campus_name = serializers.CharField(source='campus.name', read_only=True)

    class Meta:
        model = Room
        fields = ['id', 'name', 'capacity', 'room_type', 'campus', 'campus_name']


class TimeSlotSerializer(serializers.ModelSerializer):
    day_label = serializers.CharField(source='get_day_of_week_display', read_only=True)

    class Meta:
        model = TimeSlot
        fields = ['id', 'day_of_week', 'day_label', 'start_time', 'end_time', 'slot_number', 'is_evening']


class CourseSerializer(serializers.ModelSerializer):
    lecturer_name = serializers.CharField(source='lecturer.name', read_only=True)
    student_group_name = serializers.CharField(source='student_group.name', read_only=True)

    class Meta:
        model = Course
        fields = [
            'id', 'code', 'name', 'program', 'duration_slots', 'sessions_per_week',
            'required_room_type', 'lecturer', 'lecturer_name',
            'student_group', 'student_group_name'
        ]


class ScheduleSlotSerializer(serializers.ModelSerializer):
    course_code = serializers.CharField(source='course.code', read_only=True)
    course_name = serializers.CharField(source='course.name', read_only=True)
    lecturer_name = serializers.CharField(source='lecturer.name', read_only=True)
    room_name = serializers.CharField(source='room.name', read_only=True)
    student_group_name = serializers.CharField(source='student_group.name', read_only=True)
    day = serializers.CharField(source='time_slot.get_day_of_week_display', read_only=True)
    start_time = serializers.TimeField(source='time_slot.start_time', read_only=True)
    end_time = serializers.TimeField(source='time_slot.end_time', read_only=True)

    class Meta:
        model = ScheduleSlot
        fields = [
            'id', 'timetable', 'course', 'course_code', 'course_name',
            'lecturer', 'lecturer_name', 'room', 'room_name',
            'student_group', 'student_group_name',
            'time_slot', 'day', 'start_time', 'end_time'
        ]


class TimetableSerializer(serializers.ModelSerializer):
    semester_name = serializers.CharField(source='semester.name', read_only=True)
    university = serializers.CharField(source='semester.university.name', read_only=True)
    slot_count = serializers.SerializerMethodField()

    def get_slot_count(self, obj):
        return obj.slots.count()

    class Meta:
        model = Timetable
        fields = ['id', 'name', 'semester', 'semester_name', 'university', 'is_active', 'created_at', 'slot_count']


class GenerationLogSerializer(serializers.ModelSerializer):
    timetable_name = serializers.CharField(source='timetable.name', read_only=True)

    class Meta:
        model = GenerationLog
        fields = [
            'id', 'timetable', 'timetable_name', 'status', 'message',
            'solver_score', 'solve_time_seconds', 'courses_scheduled',
            'hard_conflicts_found', 'soft_conflicts_found', 'created_at'
        ]
