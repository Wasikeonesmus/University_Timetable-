"""
scheduler/tasks.py
------------------
Async task functions for django-q2 task queue.
These are called via async_task() from views and run in background workers.
"""
import logging
from collections import defaultdict
from django.core.mail import send_mail
from django.conf import settings
from django.template.loader import render_to_string

from .scheduling_service import run_scheduling_pipeline
from .models import Timetable, ScheduleSlot

logger = logging.getLogger(__name__)


def generate_timetable_async(timetable_id: int, time_limit: int = 60):
    """
    Background task: run the full scheduling pipeline for a timetable.
    Called via django_q async_task() from the generate view.

    On completion, sends email notifications to all affected lecturers.
    """
    logger.info(f"[Task] Starting async generation for timetable ID={timetable_id}")
    result = run_scheduling_pipeline(timetable_id, time_limit_seconds=time_limit)
    logger.info(f"[Task] Generation done — status={result.status}, courses={result.courses_scheduled}")

    # Send email notifications if generation succeeded
    if result.status in ('OPTIMAL', 'FEASIBLE'):
        try:
            _notify_lecturers_on_publish(timetable_id)
        except Exception as e:
            logger.error(f"[Task] Email notification failed: {e}")

    return {
        'status':            result.status,
        'message':           result.message,
        'courses_scheduled': result.courses_scheduled,
        'log_id':            result.log_id,
        'hard_conflicts':    len(result.hard_conflicts),
        'soft_conflicts':    len(result.soft_conflicts),
    }


def _notify_lecturers_on_publish(timetable_id: int):
    """
    Sends email notifications to all lecturers whose schedule was just generated.
    Includes personalized .ics calendar attachments.

    FIX G6: Batch-fetches all slots upfront and groups by lecturer in Python.
    Eliminates the previous pattern of 1 DB query per lecturer.
    """
    try:
        timetable = Timetable.objects.select_related('semester', 'semester__university').get(pk=timetable_id)
    except Timetable.DoesNotExist:
        return

    # FIX G6: Single query — fetch ALL slots with related data up-front.
    # Old code ran one extra query per lecturer inside the loop.
    all_slots = list(
        ScheduleSlot.objects.filter(timetable=timetable)
        .select_related(
            'lecturer', 'lecturer__user',
            'course', 'room', 'time_slot',
        )
    )

    if not all_slots:
        return

    # Group slots by lecturer in Python (O(N) — no extra DB hits)
    slots_by_lecturer = defaultdict(list)
    for slot in all_slots:
        if slot.lecturer_id:
            slots_by_lecturer[slot.lecturer_id].append(slot)

    from django.core.mail import EmailMessage
    import uuid

    notified = 0
    for lecturer_id, lec_slots in slots_by_lecturer.items():
        lecturer = lec_slots[0].lecturer
        if not lecturer:
            continue

        # Get email: from User account if linked, else from Lecturer record
        email = None
        if lecturer.user and lecturer.user.email:
            email = lecturer.user.email
        elif lecturer.email:
            email = lecturer.email

        if not email:
            continue

        # Ensure lecturer has a calendar token
        if not lecturer.calendar_token:
            lecturer.calendar_token = uuid.uuid4()
            lecturer.save(update_fields=['calendar_token'])

        subject = f"Your Schedule is Ready – {timetable.name}"
        message_lines = [
            f"Dear {lecturer.name},",
            "",
            f"Your timetable for '{timetable.name}' ({timetable.semester.name}) has been generated.",
            "",
            "Your assigned classes:",
        ]
        for s in lec_slots:
            message_lines.append(
                f"  • {s.course.code}: {s.course.name} | {s.time_slot} | Room: {s.room.name}"
            )
        message_lines += [
            "",
            "Please log in to the Timetable System to view your full schedule.",
            "",
            "Best regards,",
            "University Timetable System",
        ]

        try:
            email_msg = EmailMessage(
                subject=subject,
                body='\n'.join(message_lines),
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[email],
            )

            email_msg.send(fail_silently=False)
            notified += 1
            logger.info(f"[Task] Notified lecturer {lecturer.name} at {email}")

            # Check if lecturer has Google Calendar integration active and sync
            if lecturer.user:
                try:
                    from accounts.models import GoogleCalendarToken
                    if GoogleCalendarToken.objects.filter(user=lecturer.user).exists():
                        import sys
                        if 'test' in sys.argv:
                            from .google_tasks import sync_lecturer_timetable_google
                            sync_lecturer_timetable_google(lecturer.id, timetable.id)
                        else:
                            from django_q.tasks import async_task
                            async_task('scheduler.google_tasks.sync_lecturer_timetable_google', lecturer.id, timetable.id)
                        logger.info(f"[Task] Triggered Google Calendar sync for lecturer {lecturer.name}")
                except Exception as g_err:
                    logger.warning(f"[Task] Failed to trigger Google sync for {lecturer.name}: {g_err}")

        except Exception as e:
            logger.warning(f"[Task] Could not email {lecturer.name} ({email}): {e}")

    logger.info(
        f"[Task] Sent {notified} lecturer notification(s) "
        f"for timetable {timetable_id}"
    )
