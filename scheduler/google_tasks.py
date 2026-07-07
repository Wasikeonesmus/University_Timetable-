import logging
from scheduler.models import ScheduleSlot, Lecturer, Timetable
from scheduler import google_calendar_service

logger = logging.getLogger(__name__)

def sync_slot_async(slot_id):
    """
    Background task wrapper to sync a single slot to Google Calendar.
    """
    logger.info(f"[Google Tasks] Running async sync for slot {slot_id}...")
    success = google_calendar_service.sync_slot_to_google(slot_id)
    if success:
        logger.info(f"[Google Tasks] Async sync for slot {slot_id} completed successfully.")
    else:
        logger.warning(f"[Google Tasks] Async sync for slot {slot_id} failed or was skipped.")

def delete_event_async(google_event_id, token_json):
    """
    Background task wrapper to delete a single event from Google Calendar.
    """
    logger.info(f"[Google Tasks] Running async delete for event {google_event_id}...")
    success = google_calendar_service.delete_slot_from_google(google_event_id, token_json)
    if success:
        logger.info(f"[Google Tasks] Async delete for event {google_event_id} completed successfully.")
    else:
        logger.warning(f"[Google Tasks] Async delete for event {google_event_id} failed or was skipped.")

def sync_lecturer_timetable_google(lecturer_id, timetable_id):
    """
    Syncs all schedule slots for a specific lecturer in a timetable to Google Calendar.
    """
    logger.info(f"[Google Tasks] Syncing whole timetable {timetable_id} for lecturer {lecturer_id}...")
    try:
        lecturer = Lecturer.objects.get(pk=lecturer_id)
        timetable = Timetable.objects.get(pk=timetable_id)
    except (Lecturer.DoesNotExist, Timetable.DoesNotExist) as e:
        logger.error(f"[Google Tasks] Sync aborted. Lecturer or Timetable not found: {e}")
        return

    # Find slots
    slots = ScheduleSlot.objects.filter(timetable=timetable, lecturer=lecturer)
    logger.info(f"[Google Tasks] Found {slots.count()} slots to sync for lecturer {lecturer.name}.")
    
    for slot in slots:
        sync_slot_async(slot.id)
