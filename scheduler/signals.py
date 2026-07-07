import logging
import threading
from django.db import transaction
from django.db.models.signals import post_save, post_delete

from .models import (
    Course, Lecturer, StudentGroup, Room, TimeSlot, Constraint,
    LecturerAvailability, Timetable, GenerationLog
)

logger = logging.getLogger(__name__)


# ── Auto-provision new universities with defaults ─────────────────────────────
def auto_provision_university(sender, instance, created, **kwargs):
    """
    Whenever a new University is created, automatically provision:
      - A default Fall semester
      - 25 time slots (Mon–Fri, 5 slots/day: 08:30–17:00)
      - A default Campus so imports work immediately
    """
    if not created:
        return

    import sys
    if 'test' in sys.argv or 'pytest' in sys.argv or any('pytest' in arg for arg in sys.argv):
        return

    import datetime
    from .models import Semester, TimeSlot, Campus

    # 1. Default semester
    if not Semester.objects.filter(university=instance).exists():
        Semester.objects.create(
            university=instance,
            name='Fall 2026',
            start_date=datetime.date(2026, 9, 1),
            end_date=datetime.date(2026, 12, 20),
            is_active=True,
        )
        logger.info(f"[AutoProvision] Created default semester for '{instance.name}'")

    # 2. Default time slots (Mon–Fri, 5 slots/day)
    if not TimeSlot.objects.filter(university=instance).exists():
        slot_config = [
            (1, datetime.time(8, 30),  datetime.time(10, 0)),
            (2, datetime.time(10, 15), datetime.time(11, 45)),
            (3, datetime.time(12, 0),  datetime.time(13, 30)),
            (4, datetime.time(13, 45), datetime.time(15, 15)),
            (5, datetime.time(15, 30), datetime.time(17, 0)),
        ]
        slots = [
            TimeSlot(
                university=instance,
                day_of_week=day,
                slot_number=slot_num,
                start_time=start,
                end_time=end,
                is_evening=False,
            )
            for day in range(1, 6)
            for slot_num, start, end in slot_config
        ]
        TimeSlot.objects.bulk_create(slots)
        logger.info(f"[AutoProvision] Created 25 time slots for '{instance.name}'")

    # 3. Default campus (needed for imports to work)
    if not Campus.objects.filter(university=instance).exists():
        Campus.objects.create(university=instance, name='Default Campus')
        logger.info(f"[AutoProvision] Created default campus for '{instance.name}'")



# Thread-local storage to track which timetables are already queued for regeneration
# in the current database transaction. This prevents queueing multiple tasks.
_local = threading.local()

def queue_auto_generation(timetable):
    if not hasattr(_local, 'pending_timetables'):
        _local.pending_timetables = set()

    if timetable.id in _local.pending_timetables:
        logger.debug(f"[Signals] Timetable {timetable.id} already marked as pending in this thread.")
        return

    _local.pending_timetables.add(timetable.id)

    def do_queue():
        # Clear the pending flag for this timetable
        if hasattr(_local, 'pending_timetables'):
            _local.pending_timetables.discard(timetable.id)

        # 1. Clear any stale PENDING logs (from interrupted previous runs)
        GenerationLog.objects.filter(timetable=timetable, status='PENDING').delete()

        # 2. Determine solver time limit based on course count
        try:
            course_count = Course.objects.filter(
                program__department__faculty__campus__university=timetable.semester.university,
                lecturer__isnull=False,
                student_group__isnull=False
            ).count()
        except Exception as e:
            logger.warning(f"[Signals] Could not resolve course count for timetable {timetable.id} (likely deleted): {e}")
            return

        if course_count <= 50:
            time_limit = 30
        elif course_count <= 150:
            time_limit = 60
        elif course_count <= 500:
            time_limit = 120
        else:
            time_limit = 180

        # 3. Create a PENDING GenerationLog to track background task status
        GenerationLog.objects.create(
            timetable=timetable,
            status='PENDING',
            message='Automatic generation queued in the background worker queue.'
        )

        # 4. Trigger background thread execution
        import sys
        if 'test' in sys.argv or 'pytest' in sys.argv or any('pytest' in arg for arg in sys.argv):
            try:
                from django_q.tasks import async_task
                task_id = async_task(
                    'scheduler.tasks.generate_timetable_async',
                    timetable.id,
                    time_limit,
                    task_name=f'generate-timetable-{timetable.id}',
                    group=f'timetable-{timetable.id}',
                )
                logger.info(f"[Signals] Automatically queued generation task in test mode (Task ID: {task_id})")
            except Exception as e:
                logger.error(f"[Signals] Failed to queue async task in test mode: {e}")
        else:
            try:
                import threading
                from django.db import close_old_connections
                from .tasks import generate_timetable_async

                def run_async():
                    close_old_connections()
                    try:
                        generate_timetable_async(timetable.id, time_limit)
                    except Exception as thread_err:
                        logger.error(f"[Signals Thread] Background generation failed: {thread_err}")
                        try:
                            pending_log = GenerationLog.objects.filter(timetable=timetable, status='PENDING').order_by('-created_at').first()
                            if pending_log:
                                pending_log.status = 'ERROR'
                                pending_log.message = f'Generation thread crashed: {thread_err}'
                                pending_log.save(update_fields=['status', 'message'])
                        except Exception:
                            pass
                    finally:
                        close_old_connections()

                t_thread = threading.Thread(target=run_async, name=f"AutoGenerate-{timetable.id}")
                t_thread.daemon = False  # Non-daemon so it survives dev server reloads
                t_thread.start()
                logger.info(f"[Signals] Automatically started background thread for timetable {timetable.id}")
            except Exception as e:
                logger.error(f"[Signals] Failed to start background thread for timetable {timetable.id}: {e}")

    transaction.on_commit(do_queue)


def get_university_from_instance(instance):
    try:
        if isinstance(instance, Course):
            from .models import Program
            return Program.objects.select_related('department__faculty__campus__university').get(pk=instance.program_id).department.faculty.campus.university
        elif isinstance(instance, Lecturer):
            from .models import Department
            return Department.objects.select_related('faculty__campus__university').get(pk=instance.department_id).faculty.campus.university
        elif isinstance(instance, StudentGroup):
            from .models import Program
            return Program.objects.select_related('department__faculty__campus__university').get(pk=instance.program_id).department.faculty.campus.university
        elif isinstance(instance, Room):
            from .models import Campus
            return Campus.objects.select_related('university').get(pk=instance.campus_id).university
        elif isinstance(instance, TimeSlot):
            return instance.university
        elif isinstance(instance, Constraint):
            return instance.university
        elif isinstance(instance, LecturerAvailability):
            from .models import Lecturer
            return Lecturer.objects.select_related('department__faculty__campus__university').get(pk=instance.lecturer_id).department.faculty.campus.university
    except Exception:
        pass
    return None


def trigger_regeneration_for_university_models(sender, instance, **kwargs):
    # Skip during migrations or bulk loading of fixtures
    if kwargs.get('raw', False):
        return

    # Determine the university for the changed instance
    university = get_university_from_instance(instance)
    if not university:
        try:
            instance_str = repr(instance)
        except Exception:
            instance_str = f'<{sender.__name__} pk={getattr(instance, "pk", "?")}>'
        logger.debug(f"[Signals] Could not resolve university for instance {instance_str} of model {sender.__name__}")
        return

    # Find active timetables for the university.
    # Falls back to the most recent timetable if none is marked is_active=True,
    # so auto-generation works even before a timetable has been explicitly activated.
    active_timetables = list(
        Timetable.objects.filter(semester__university=university, is_active=True)
    )
    if not active_timetables:
        latest = (
            Timetable.objects.filter(semester__university=university)
            .order_by('-created_at')
            .first()
        )
        if latest:
            active_timetables = [latest]

    for timetable in active_timetables:
        queue_auto_generation(timetable)



# Register post_save and post_delete signals for the input models
MODELS_TO_WATCH = [
    Course, Lecturer, StudentGroup, Room, TimeSlot, Constraint, LecturerAvailability
]

for model in MODELS_TO_WATCH:
    post_save.connect(trigger_regeneration_for_university_models, sender=model, dispatch_uid=f"auto_gen_save_{model.__name__.lower()}")
    post_delete.connect(trigger_regeneration_for_university_models, sender=model, dispatch_uid=f"auto_gen_delete_{model.__name__.lower()}")

# Auto-provision semester + timeslots + campus when a new university is created
from .models import University
post_save.connect(auto_provision_university, sender=University, dispatch_uid="auto_provision_university")


# ── Google Calendar Sync Signals ─────────────────────────────────────────────
from .models import ScheduleSlot

def sync_schedule_slot_to_google(sender, instance, created, **kwargs):
    """
    Triggers background Google Calendar sync when a single ScheduleSlot is saved.
    (Note: bulk_create during solver generation does not trigger this signal,
    which is handled separately in tasks.py).
    """
    if kwargs.get('raw', False):
        return

    lecturer = instance.lecturer
    if not lecturer:
        return

    try:
        profile = lecturer.user_profile
        user = profile.user
    except Exception:
        return

    from accounts.models import GoogleCalendarToken
    if not GoogleCalendarToken.objects.filter(user=user).exists():
        return

    import sys
    if 'test' in sys.argv:
        from .google_tasks import sync_slot_async
        sync_slot_async(instance.id)
    else:
        from django_q.tasks import async_task
        async_task('scheduler.google_tasks.sync_slot_async', instance.id)

def delete_schedule_slot_from_google(sender, instance, **kwargs):
    """
    Triggers Google Calendar event deletion when a ScheduleSlot is deleted.
    """
    if not instance.google_event_id:
        return

    try:
        lecturer = instance.lecturer
    except Exception:
        return

    if not lecturer:
        return

    try:
        profile = lecturer.user_profile
        user = profile.user
        token_record = user.google_token
        token_json = token_record.token
    except Exception:
        return

    import sys
    if 'test' in sys.argv:
        from .google_tasks import delete_event_async
        delete_event_async(instance.google_event_id, token_json)
    else:
        from django_q.tasks import async_task
        async_task('scheduler.google_tasks.delete_event_async', instance.google_event_id, token_json)

post_save.connect(sync_schedule_slot_to_google, sender=ScheduleSlot, dispatch_uid="sync_slot_to_google")
post_delete.connect(delete_schedule_slot_from_google, sender=ScheduleSlot, dispatch_uid="delete_slot_from_google")


