import os
import json
import logging
from django.conf import settings
from django.urls import reverse
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

from accounts.models import GoogleCalendarToken
from scheduler.models import ScheduleSlot

logger = logging.getLogger(__name__)

CLIENT_SECRET_FILE = os.path.join(settings.BASE_DIR, 'client_secret.json')
SCOPES = [
    'https://www.googleapis.com/auth/calendar.events',
    'https://www.googleapis.com/auth/userinfo.email',
    'openid'
]

def get_auth_flow(request=None):
    """
    Returns a configured Flow object for Google OAuth2.
    """
    if request:
        redirect_uri = request.build_absolute_uri(reverse('accounts:google_calendar_callback'))
    else:
        redirect_uri = 'http://127.0.0.1:8000/accounts/google-calendar/callback/'

    flow = Flow.from_client_secrets_file(
        CLIENT_SECRET_FILE,
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )
    return flow

def get_calendar_service(user):
    """
    Returns an authenticated Google Calendar API service instance for the given User.
    Automatically refreshes expired access tokens and saves them back to the database.
    """
    try:
        gtoken = user.google_token
    except GoogleCalendarToken.DoesNotExist:
        logger.warning(f"GoogleCalendarToken does not exist for user {user.username}")
        return None

    try:
        token_data = json.loads(gtoken.token)
        credentials = Credentials.from_authorized_user_info(token_data, SCOPES)
    except Exception as e:
        logger.error(f"Failed to load user token for Google calendar: {e}")
        return None

    if credentials.expired and credentials.refresh_token:
        try:
            logger.info(f"Refreshing Google Calendar credentials for user {user.username}...")
            credentials.refresh(Request())
            gtoken.token = credentials.to_json()
            gtoken.save()
            logger.info(f"Successfully refreshed and saved credentials for user {user.username}.")
        except Exception as e:
            logger.error(f"Failed to refresh Google credentials for user {user.username}: {e}")
            return None

    try:
        service = build('calendar', 'v3', credentials=credentials)
        return service
    except Exception as e:
        logger.error(f"Failed to build Google Calendar service for user {user.username}: {e}")
        return None

def sync_slot_to_google(slot_id):
    """
    Syncs a single ScheduleSlot to the lecturer's Google Calendar.
    Creates the event if it doesn't exist, or updates it if slot.google_event_id is set.
    """
    try:
        slot = ScheduleSlot.objects.select_related(
            'course', 'lecturer', 'room', 'time_slot', 'student_group', 'timetable__semester'
        ).get(pk=slot_id)
    except ScheduleSlot.DoesNotExist:
        logger.error(f"ScheduleSlot {slot_id} does not exist for Google sync.")
        return False

    lecturer = slot.lecturer
    try:
        profile = lecturer.user_profile
        user = profile.user
    except Exception:
        logger.debug(f"Lecturer {lecturer.name} has no linked UserProfile/User. Skipping Google sync.")
        return False

    service = get_calendar_service(user)
    if not service:
        logger.debug(f"Google Calendar API service not available for user {user.username}. Skipping sync.")
        return False

    semester = slot.timetable.semester
    start_date = semester.start_date
    end_date = semester.end_date
    ts = slot.time_slot

    # Find the first date matching slot's day_of_week on or after start_date
    from scheduler.calendar_exporter import get_first_occurrence
    first_date = get_first_occurrence(start_date, ts.day_of_week)

    import datetime
    start_dt = datetime.datetime.combine(first_date, ts.start_time)
    end_dt = datetime.datetime.combine(first_date, ts.end_time)

    # Convert to ISO format and set timezone parameter
    start_str = start_dt.isoformat()
    end_str = end_dt.isoformat()

    # Recurrence rule
    until_str = end_date.strftime("%Y%m%d") + "T235959Z"
    recurrence_rrule = f"RRULE:FREQ=WEEKLY;UNTIL={until_str}"

    summary = f"{slot.course.code}: {slot.course.name}"
    location = f"{slot.room.name} ({slot.room.get_room_type_display()}, Cap: {slot.room.capacity})"
    description = (
        f"Student Group: {slot.student_group.name}\n"
        f"Timetable: {slot.timetable.name}\n"
        f"University: {semester.university.name}"
    )

    event_body = {
        'summary': summary,
        'location': location,
        'description': description,
        'start': {
            'dateTime': start_str,
            'timeZone': 'Africa/Nairobi',
        },
        'end': {
            'dateTime': end_str,
            'timeZone': 'Africa/Nairobi',
        },
        'recurrence': [recurrence_rrule],
        'reminders': {
            'useDefault': True,
        }
    }


    try:
        if slot.google_event_id:
            logger.info(f"Updating Google Calendar event {slot.google_event_id} for slot {slot_id}...")
            event = service.events().update(
                calendarId='primary',
                eventId=slot.google_event_id,
                body=event_body
            ).execute()
        else:
            logger.info(f"Creating new Google Calendar event for slot {slot_id}...")
            event = service.events().insert(
                calendarId='primary',
                body=event_body
            ).execute()
            slot.google_event_id = event['id']
            slot.save(update_fields=['google_event_id'])
        
        logger.info(f"Successfully synced slot {slot_id} to Google Calendar. Event ID: {event['id']}")
        return True
    except Exception as e:
        logger.error(f"Error syncing slot {slot_id} to Google Calendar: {e}")
        return False

def delete_slot_from_google(google_event_id, token_json):
    """
    Deletes an event from Google Calendar using a stored token.
    Runs completely standalone (doesn't need slot instance, useful post-delete).
    """
    if not google_event_id or not token_json:
        return False

    try:
        token_data = json.loads(token_json)
        credentials = Credentials.from_authorized_user_info(token_data, SCOPES)
        service = build('calendar', 'v3', credentials=credentials)

        logger.info(f"Deleting Google Calendar event {google_event_id}...")
        service.events().delete(calendarId='primary', eventId=google_event_id).execute()
        logger.info(f"Successfully deleted Google Calendar event {google_event_id}.")
        return True
    except Exception as e:
        logger.error(f"Failed to delete Google Calendar event {google_event_id}: {e}")
        return False
