"""
scheduler/api/views.py
-----------------------
Django REST Framework ViewSets for the Timetable API.
Read-only endpoints are public; write endpoints require authentication.
"""
from rest_framework import viewsets, permissions, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from scheduler.models import (
    University, Lecturer, StudentGroup, Room, TimeSlot,
    Course, Timetable, ScheduleSlot, GenerationLog
)
from .serializers import (
    UniversitySerializer, LecturerSerializer, StudentGroupSerializer,
    RoomSerializer, TimeSlotSerializer, CourseSerializer,
    TimetableSerializer, ScheduleSlotSerializer, GenerationLogSerializer
)


class UniversityViewSet(viewsets.ReadOnlyModelViewSet):
    """List all universities. GET /api/v1/universities/"""
    queryset = University.objects.all()
    serializer_class = UniversitySerializer
    permission_classes = [permissions.AllowAny]


class LecturerViewSet(viewsets.ReadOnlyModelViewSet):
    """List all lecturers, filterable by university. GET /api/v1/lecturers/"""
    serializer_class = LecturerSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = ['name', 'email', 'department__name']

    def get_queryset(self):
        qs = Lecturer.objects.select_related('department', 'department__faculty__campus__university')
        uni_id = self.request.query_params.get('university')
        if uni_id:
            qs = qs.filter(department__faculty__campus__university_id=uni_id)
        return qs


class StudentGroupViewSet(viewsets.ReadOnlyModelViewSet):
    """List all student groups. GET /api/v1/student-groups/"""
    serializer_class = StudentGroupSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = StudentGroup.objects.select_related('program')
        uni_id = self.request.query_params.get('university')
        if uni_id:
            qs = qs.filter(program__department__faculty__campus__university_id=uni_id)
        return qs


class RoomViewSet(viewsets.ReadOnlyModelViewSet):
    """List all rooms. GET /api/v1/rooms/"""
    serializer_class = RoomSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = Room.objects.select_related('campus')
        uni_id = self.request.query_params.get('university')
        room_type = self.request.query_params.get('type')
        if uni_id:
            qs = qs.filter(campus__university_id=uni_id)
        if room_type:
            qs = qs.filter(room_type=room_type)
        return qs


class TimeSlotViewSet(viewsets.ReadOnlyModelViewSet):
    """List all time slots. GET /api/v1/timeslots/"""
    serializer_class = TimeSlotSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = TimeSlot.objects.all()
        uni_id = self.request.query_params.get('university')
        if uni_id:
            qs = qs.filter(university_id=uni_id)
        return qs


class CourseViewSet(viewsets.ReadOnlyModelViewSet):
    """List all courses. GET /api/v1/courses/"""
    serializer_class = CourseSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = ['code', 'name']

    def get_queryset(self):
        qs = Course.objects.select_related('lecturer', 'student_group', 'program')
        uni_id = self.request.query_params.get('university')
        if uni_id:
            qs = qs.filter(program__department__faculty__campus__university_id=uni_id)
        return qs


class TimetableViewSet(viewsets.ReadOnlyModelViewSet):
    """
    List timetables. GET /api/v1/timetables/
    Get slots for a timetable: GET /api/v1/timetables/{id}/slots/
    """
    serializer_class = TimetableSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = Timetable.objects.select_related('semester', 'semester__university')
        uni_id = self.request.query_params.get('university')
        if uni_id:
            qs = qs.filter(semester__university_id=uni_id)
        return qs

    @action(detail=True, methods=['get'], url_path='slots')
    def slots(self, request, pk=None):
        """GET /api/v1/timetables/{id}/slots/ — returns all schedule slots."""
        timetable = self.get_object()
        slots_qs = ScheduleSlot.objects.filter(timetable=timetable).select_related(
            'course', 'lecturer', 'room', 'time_slot', 'student_group'
        )
        # Filter by group, lecturer, or room
        group_id = request.query_params.get('group')
        lecturer_id = request.query_params.get('lecturer')
        room_id = request.query_params.get('room')
        if group_id:
            slots_qs = slots_qs.filter(student_group_id=group_id)
        if lecturer_id:
            slots_qs = slots_qs.filter(lecturer_id=lecturer_id)
        if room_id:
            slots_qs = slots_qs.filter(room_id=room_id)

        serializer = ScheduleSlotSerializer(slots_qs, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'], url_path='logs')
    def logs(self, request, pk=None):
        """GET /api/v1/timetables/{id}/logs/ — generation audit logs."""
        timetable = self.get_object()
        logs = GenerationLog.objects.filter(timetable=timetable)
        serializer = GenerationLogSerializer(logs, many=True)
        return Response(serializer.data)


class SISIntegrationView(APIView):
    """
    Mock endpoint for SIS (Student Information System) sync.
    POST /api/v1/integrations/sis/sync/
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        data = request.data
        return Response({
            "status": "success",
            "message": "SIS sync mock successful.",
            "synced_records_count": len(data.get("students", [])) if isinstance(data.get("students"), list) else 150
        }, status=status.HTTP_200_OK)


class LMSIntegrationView(APIView):
    """
    Mock endpoint for LMS (Learning Management System) export.
    POST /api/v1/integrations/lms/export/
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        timetable_id = request.data.get("timetable_id")
        return Response({
            "status": "success",
            "message": f"Successfully pushed timetable {timetable_id} events to Moodle/Canvas LMS mock.",
            "events_exported": 1240
        }, status=status.HTTP_200_OK)


class HRIntegrationView(APIView):
    """
    Mock endpoint for HR (Human Resources) sync.
    POST /api/v1/integrations/hr/sync/
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        return Response({
            "status": "success",
            "message": "HR sync mock successful.",
            "synced_lecturers": 45
        }, status=status.HTTP_200_OK)


class SSOSettingsView(APIView):
    """
    Mock endpoint for Single Sign-On configuration settings.
    GET/POST /api/v1/integrations/sso/settings/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        return Response({
            "provider": "SAML2 / OpenID Connect",
            "entrypoint": "https://sso.university.edu/adfs/ls/",
            "issuer": "timetable-platform-sp",
            "enabled": True
        })

    def post(self, request, *args, **kwargs):
        return Response({
            "status": "success",
            "message": "SSO configuration parameters updated."
        }, status=status.HTTP_200_OK)
