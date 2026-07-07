"""
scheduler/api/urls.py
---------------------
REST API URL routing using DRF DefaultRouter.
All API endpoints are under /api/v1/
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'v1/universities', views.UniversityViewSet, basename='university')
router.register(r'v1/lecturers', views.LecturerViewSet, basename='lecturer')
router.register(r'v1/student-groups', views.StudentGroupViewSet, basename='studentgroup')
router.register(r'v1/rooms', views.RoomViewSet, basename='room')
router.register(r'v1/timeslots', views.TimeSlotViewSet, basename='timeslot')
router.register(r'v1/courses', views.CourseViewSet, basename='course')
router.register(r'v1/timetables', views.TimetableViewSet, basename='timetable')

urlpatterns = [
    path('', include(router.urls)),
    path('auth/', include('rest_framework.urls', namespace='rest_framework')),
    path('v1/integrations/sis/sync/', views.SISIntegrationView.as_view(), name='sis-sync'),
    path('v1/integrations/lms/export/', views.LMSIntegrationView.as_view(), name='lms-export'),
    path('v1/integrations/hr/sync/', views.HRIntegrationView.as_view(), name='hr-sync'),
    path('v1/integrations/sso/settings/', views.SSOSettingsView.as_view(), name='sso-settings'),
]
