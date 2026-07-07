from django.test import TestCase
from django.contrib.auth.models import User
from django.db.models.signals import post_save, post_delete
from django.urls import reverse
from unittest.mock import patch, MagicMock
import datetime
import json

from scheduler.models import (
    University, Semester, Timetable, Course, Lecturer, Room, TimeSlot, StudentGroup, ScheduleSlot,
    Campus, Faculty, Department, Program
)
from accounts.models import UserProfile, GoogleCalendarToken
from scheduler import google_calendar_service
from scheduler.signals import sync_schedule_slot_to_google, delete_schedule_slot_from_google

class GoogleCalendarServiceTestCase(TestCase):
    def setUp(self):
        # Disconnect signals during setup to avoid unexpected event triggers
        post_save.disconnect(sync_schedule_slot_to_google, sender=ScheduleSlot, dispatch_uid="sync_slot_to_google")
        post_delete.disconnect(delete_schedule_slot_from_google, sender=ScheduleSlot, dispatch_uid="delete_slot_from_google")

        # Setup basic data
        self.university = University.objects.create(name="Test Uni")
        self.campus = Campus.objects.create(university=self.university, name="Main Campus")
        self.faculty = Faculty.objects.create(campus=self.campus, name="Science Faculty")
        self.department = Department.objects.create(faculty=self.faculty, name="CS Dept")
        self.program = Program.objects.create(department=self.department, name="BSc CS")

        self.semester = Semester.objects.create(
            university=self.university,
            name="Fall 2026",
            start_date=datetime.date(2026, 9, 1),
            end_date=datetime.date(2026, 12, 20)
        )
        self.timetable = Timetable.objects.create(semester=self.semester, name="Timetable v1")
        self.course = Course.objects.create(code="CS101", name="Intro to CS", program=self.program)
        self.lecturer = Lecturer.objects.create(name="Dr. Smith", email="smith@test.com", department=self.department)
        self.room = Room.objects.create(name="Room 101", room_type="lecture", capacity=50, campus=self.campus)
        self.time_slot = TimeSlot.objects.create(
            university=self.university, day_of_week=1, slot_number=1,
            start_time=datetime.time(8, 30), end_time=datetime.time(10, 0)
        )
        self.student_group = StudentGroup.objects.create(name="CS Year 1", program=self.program, size=30)
        
        self.slot = ScheduleSlot.objects.create(
            timetable=self.timetable,
            course=self.course,
            lecturer=self.lecturer,
            room=self.room,
            time_slot=self.time_slot,
            student_group=self.student_group
        )

        # Create user account and profile linked to Lecturer
        self.user = User.objects.create_user(username="smith", password="password")
        self.profile = UserProfile.objects.create(
            user=self.user,
            role="lecturer",
            university=self.university,
            lecturer=self.lecturer
        )
        
        # Token details - set expiry to a far future date to avoid refresh attempts
        self.token_data = {
            "token": "fake-access-token",
            "refresh_token": "fake-refresh-token",
            "client_id": "fake-client-id",
            "client_secret": "fake-client-secret",
            "scopes": ["https://www.googleapis.com/auth/calendar.events"],
            "expiry": "2035-01-01T00:00:00Z"
        }
        self.google_token = GoogleCalendarToken.objects.create(
            user=self.user,
            token=json.dumps(self.token_data),
            email="smith@gmail.com"
        )

    def tearDown(self):
        # Reconnect signals post test
        post_save.connect(sync_schedule_slot_to_google, sender=ScheduleSlot, dispatch_uid="sync_slot_to_google")
        post_delete.connect(delete_schedule_slot_from_google, sender=ScheduleSlot, dispatch_uid="delete_slot_from_google")

    @patch('scheduler.google_calendar_service.build')
    def test_get_calendar_service(self, mock_build):
        mock_service = MagicMock()
        mock_build.return_value = mock_service
        
        service = google_calendar_service.get_calendar_service(self.user)
        self.assertIsNotNone(service)
        mock_build.assert_called_once()

    @patch('scheduler.google_calendar_service.get_calendar_service')
    def test_sync_slot_to_google_insert(self, mock_get_service):
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service
        
        # Mock API response for insert by setting return_values directly
        mock_service.events.return_value.insert.return_value.execute.return_value = {'id': 'google-event-12345'}
        
        success = google_calendar_service.sync_slot_to_google(self.slot.id)
        self.assertTrue(success)
        
        # Verify slot got Google Event ID saved
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.google_event_id, 'google-event-12345')
        
        mock_service.events.return_value.insert.assert_called_once()

    @patch('scheduler.google_calendar_service.get_calendar_service')
    def test_sync_slot_to_google_update(self, mock_get_service):
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service
        
        # Set existing event ID
        self.slot.google_event_id = 'google-event-12345'
        self.slot.save()
        
        # Mock API response for update by setting return_values directly
        mock_service.events.return_value.update.return_value.execute.return_value = {'id': 'google-event-12345'}
        
        success = google_calendar_service.sync_slot_to_google(self.slot.id)
        self.assertTrue(success)
        
        mock_service.events.return_value.update.assert_called_once()

    @patch('scheduler.google_calendar_service.build')
    def test_delete_slot_from_google(self, mock_build):
        mock_service = MagicMock()
        mock_build.return_value = mock_service
        
        # Mock API response for delete by setting return_values directly
        mock_service.events.return_value.delete.return_value.execute.return_value = {}
        
        success = google_calendar_service.delete_slot_from_google('google-event-12345', json.dumps(self.token_data))
        self.assertTrue(success)
        mock_service.events.return_value.delete.assert_called_once_with(calendarId='primary', eventId='google-event-12345')


class CalendarExportViewsTestCase(TestCase):
    def setUp(self):
        # Setup basic data
        self.university = University.objects.create(name="Test Uni")
        self.campus = Campus.objects.create(university=self.university, name="Main Campus")
        self.faculty = Faculty.objects.create(campus=self.campus, name="Science Faculty")
        self.department = Department.objects.create(faculty=self.faculty, name="CS Dept")
        self.program = Program.objects.create(department=self.department, name="BSc CS")

        self.semester = Semester.objects.create(
            university=self.university,
            name="Fall 2026",
            start_date=datetime.date(2026, 9, 1),
            end_date=datetime.date(2026, 12, 20)
        )
        self.timetable = Timetable.objects.create(semester=self.semester, name="Timetable v1")
        self.course = Course.objects.create(code="CS101", name="Intro to CS", program=self.program)
        self.lecturer = Lecturer.objects.create(name="Dr. Smith", email="smith@test.com", department=self.department)
        self.room = Room.objects.create(name="Room 101", room_type="lecture", capacity=50, campus=self.campus)
        self.time_slot = TimeSlot.objects.create(
            university=self.university, day_of_week=1, slot_number=1,
            start_time=datetime.time(8, 30), end_time=datetime.time(10, 0)
        )
        self.student_group = StudentGroup.objects.create(name="CS Year 1", program=self.program, size=30)
        
        self.slot = ScheduleSlot.objects.create(
            timetable=self.timetable,
            course=self.course,
            lecturer=self.lecturer,
            room=self.room,
            time_slot=self.time_slot,
            student_group=self.student_group
        )

        self.user = User.objects.create_user(username="testuser", password="password")
        self.profile = UserProfile.objects.create(
            user=self.user,
            role="admin",
            university=self.university
        )
        
        # Explicitly set active_university_id in session
        session = self.client.session
        session['active_university_id'] = self.university.id
        session['active_role'] = 'admin'
        session.save()
        
        self.client.login(username="testuser", password="password")

    def test_export_timetable_ics_unfiltered(self):
        url = reverse('scheduler:export_timetable_ics', args=[self.timetable.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/calendar')
        self.assertIn('BEGIN:VCALENDAR', response.content.decode('utf-8'))
        self.assertIn('CS101', response.content.decode('utf-8'))

    def test_export_timetable_ics_filtered_lecturer(self):
        url = reverse('scheduler:export_timetable_ics', args=[self.timetable.pk])
        response = self.client.get(url, {'filter_type': 'lecturer', 'filter_id': self.lecturer.id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/calendar')
        self.assertIn('BEGIN:VCALENDAR', response.content.decode('utf-8'))
        self.assertIn('CS101', response.content.decode('utf-8'))

    def test_export_timetable_ics_filtered_group(self):
        url = reverse('scheduler:export_timetable_ics', args=[self.timetable.pk])
        response = self.client.get(url, {'filter_type': 'group', 'filter_id': self.student_group.id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/calendar')
        self.assertIn('BEGIN:VCALENDAR', response.content.decode('utf-8'))
        self.assertIn('CS101', response.content.decode('utf-8'))

    def test_export_timetable_ics_filtered_room(self):
        url = reverse('scheduler:export_timetable_ics', args=[self.timetable.pk])
        response = self.client.get(url, {'filter_type': 'room', 'filter_id': self.room.id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/calendar')
        self.assertIn('BEGIN:VCALENDAR', response.content.decode('utf-8'))
        self.assertIn('CS101', response.content.decode('utf-8'))



    @patch('scheduler.google_tasks.sync_lecturer_timetable_google')
    def test_sync_to_google_calendar_success(self, mock_sync_task):
        # Setup lecturer profile link
        self.profile.role = 'lecturer'
        self.profile.lecturer = self.lecturer
        self.profile.save()
        
        self.lecturer.user = self.user
        self.lecturer.save()
        
        # Setup google token
        self.token_data = {
            "token": "fake-access-token",
            "refresh_token": "fake-refresh-token",
            "client_id": "fake-client-id",
            "client_secret": "fake-client-secret",
            "scopes": ["https://www.googleapis.com/auth/calendar.events"],
            "expiry": "2035-01-01T00:00:00Z"
        }
        GoogleCalendarToken.objects.create(
            user=self.user,
            token=json.dumps(self.token_data),
            email="smith@gmail.com"
        )
        
        url = reverse('scheduler:sync_to_google_calendar')
        response = self.client.get(url)
        
        self.assertRedirects(response, reverse('scheduler:lecturer_my_schedule'), fetch_redirect_response=False)

        mock_sync_task.assert_called_once_with(self.lecturer.id, self.timetable.id)

    def test_sync_to_google_calendar_not_lecturer(self):
        # user role is 'admin' (default in setUp) and not linked to lecturer
        url = reverse('scheduler:sync_to_google_calendar')
        response = self.client.get(url)
        self.assertRedirects(response, reverse('accounts:profile'))

    def test_sync_to_google_calendar_no_token(self):
        # setup lecturer role but no google token
        self.profile.role = 'lecturer'
        self.profile.lecturer = self.lecturer
        self.profile.save()
        
        self.lecturer.user = self.user
        self.lecturer.save()
        
        url = reverse('scheduler:sync_to_google_calendar')
        response = self.client.get(url)
        self.assertRedirects(response, reverse('accounts:profile'))
