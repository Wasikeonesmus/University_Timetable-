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
      - 35 time slots (Mon–Sun, 5 slots/day: 08:30–17:00)
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
            name='Semester 1 2026',
            start_date=datetime.date(2026, 9, 1),
            end_date=datetime.date(2026, 12, 20),
            is_active=True,
        )
        logger.info(f"[AutoProvision] Created default semester for '{instance.name}'")

    # 2. Default time slots (Mon–Sun, 4 x 3-hour slots/day)
    if not TimeSlot.objects.filter(university=instance).exists():
        slot_config = [
            (1, datetime.time(8, 0),  datetime.time(11, 0), False),
            (2, datetime.time(11, 0), datetime.time(14, 0), False),
            (3, datetime.time(14, 0), datetime.time(17, 0), False),
            (4, datetime.time(17, 30), datetime.time(20, 30), True),
        ]
        slots = [
            TimeSlot(
                university=instance,
                day_of_week=day,
                slot_number=slot_num,
                start_time=start,
                end_time=end,
                is_evening=is_eve,
            )
            for day in range(1, 8)
            for slot_num, start, end, is_eve in slot_config
        ]
        TimeSlot.objects.bulk_create(slots)
        logger.info(f"[AutoProvision] Created 28 3-hour time slots for '{instance.name}'")

    # 3. Default campus (needed for imports to work)
    if not Campus.objects.filter(university=instance).exists():
        Campus.objects.create(university=instance, name='Default Campus')
        logger.info(f"[AutoProvision] Created default campus for '{instance.name}'")



# Thread-local storage to track which timetables are already queued for regeneration
# in the current database transaction. This prevents queueing multiple tasks.
_local = threading.local()

from contextlib import contextmanager

@contextmanager
def mute_signals():
    """
    Temporarily mute all auto-generation and notification signals in the current thread.
    """
    old_val = getattr(_local, 'mute_signals', False)
    _local.mute_signals = True
    try:
        yield
    finally:
        _local.mute_signals = old_val


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
                # FIX BUG 12: Set daemon=True. Non-daemon threads accumulate during dev server
                # hot-reloads because each auto-save of a watched model triggers a new thread,
                # but the old interpreter is kept alive until all non-daemon threads finish.
                # Daemon threads are automatically killed when the main process exits/reloads.
                t_thread.daemon = True
                t_thread.start()
                logger.info(f"[Signals] Automatically started background thread for timetable {timetable.id}")
            except Exception as e:
                logger.error(f"[Signals] Failed to start background thread for timetable {timetable.id}: {e}")

    transaction.on_commit(do_queue)


def get_university_from_instance(instance):
    try:
        model_name = instance._meta.model_name
        if model_name == 'course':
            from .models import Program
            return Program.objects.select_related('department__faculty__campus__university').get(pk=instance.program_id).department.faculty.campus.university
        elif model_name == 'lecturer':
            from .models import Department
            return Department.objects.select_related('faculty__campus__university').get(pk=instance.department_id).faculty.campus.university
        elif model_name == 'studentgroup':
            from .models import Program
            return Program.objects.select_related('department__faculty__campus__university').get(pk=instance.program_id).department.faculty.campus.university
        elif model_name == 'room':
            from .models import Campus
            return Campus.objects.select_related('university').get(pk=instance.campus_id).university
        elif model_name == 'timeslot':
            return instance.university
        elif model_name == 'constraint':
            return instance.university
        elif model_name == 'lectureravailability':
            from .models import Lecturer
            return Lecturer.objects.select_related('department__faculty__campus__university').get(pk=instance.lecturer_id).department.faculty.campus.university
    except Exception as e:
        print(f"[DEBUG] get_university_from_instance exception: {e}")
        pass
    return None


def trigger_regeneration_for_university_models(sender, instance, **kwargs):
    if getattr(_local, 'mute_signals', False):
        return
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

    # Only trigger for timetables explicitly marked is_active=True.
    # If no active timetable exists the signal is a no-op — admins must
    # activate a timetable before auto-generation kicks in.
    active_timetables = list(
        Timetable.objects.filter(semester__university=university, is_active=True)
    )

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
from .models import University, Semester
post_save.connect(auto_provision_university, sender=University, dispatch_uid="auto_provision_university")


# ── Semester cache invalidation ───────────────────────────────────────────────
def invalidate_semester_cache(sender, instance, **kwargs):
    """
    Bust the active_semester_{university_id} cache key whenever a Semester is
    saved or deleted. This keeps context_processors.py in sync with DB changes
    without waiting for the 60-second TTL to expire.
    """
    from django.core.cache import cache
    try:
        university_id = instance.university_id
        if university_id:
            cache.delete(f'active_semester_{university_id}')
            logger.debug(f"[Cache] Invalidated active_semester cache for university {university_id}")
    except Exception as e:
        logger.warning(f"[Cache] Failed to invalidate semester cache: {e}")

post_save.connect(invalidate_semester_cache, sender=Semester, dispatch_uid="invalidate_semester_cache_save")
post_delete.connect(invalidate_semester_cache, sender=Semester, dispatch_uid="invalidate_semester_cache_delete")


# ── Google Calendar Sync Signals ─────────────────────────────────────────────
from .models import ScheduleSlot

def sync_schedule_slot_to_google(sender, instance, created, **kwargs):
    """
    Triggers background Google Calendar sync when a single ScheduleSlot is saved.
    (Note: bulk_create during solver generation does not trigger this signal,
    which is handled separately in tasks.py).
    """
    if getattr(_local, 'mute_signals', False):
        return
    if kwargs.get('raw', False):
        return

    lecturer = instance.lecturer
    if not lecturer:
        return

    # FIX BUG 6: Check explicitly with hasattr + log when profile is missing,
    # instead of silently swallowing RelatedObjectDoesNotExist.
    if not hasattr(lecturer, 'user_profile') or lecturer.user_profile is None:
        logger.debug(
            f"[GoogleSync] Lecturer '{lecturer.name}' (id={lecturer.id}) has no linked UserProfile — skipping Calendar sync."
        )
        return

    try:
        user = lecturer.user_profile.user
    except Exception as e:
        logger.warning(f"[GoogleSync] Could not resolve user for lecturer '{lecturer.name}': {e}")
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
    if getattr(_local, 'mute_signals', False):
        return
    if not instance.google_event_id:
        return

    try:
        lecturer = instance.lecturer
    except Exception:
        return

    if not lecturer:
        return

    # FIX BUG 6: Check for missing UserProfile explicitly and log it.
    if not hasattr(lecturer, 'user_profile') or lecturer.user_profile is None:
        logger.debug(
            f"[GoogleSync] Lecturer '{lecturer.name}' (id={lecturer.id}) has no linked UserProfile — skipping Calendar event deletion."
        )
        return

    try:
        user = lecturer.user_profile.user
        token_record = user.google_token
        token_json = token_record.token
    except Exception as e:
        logger.warning(f"[GoogleSync] Could not resolve Google token for lecturer '{lecturer.name}': {e}")
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


def notify_slot_changes(sender, instance, created, **kwargs):
    """
    Automatically notifies the lecturer and affected student group when a schedule slot is created or updated manually.
    """
    if getattr(_local, 'mute_signals', False):
        return
    if kwargs.get('raw', False):
        return

    # Check if timetable is active. We only notify users about changes in the active timetable.
    if not getattr(instance.timetable, 'is_active', False):
        try:
            from .models import Timetable
            is_active = Timetable.objects.filter(pk=instance.timetable_id, is_active=True).exists()
            if not is_active:
                return
        except Exception:
            return

    import sys
    if 'test' in sys.argv or 'pytest' in sys.argv or any('test' in arg for arg in sys.argv):
        from .tasks import send_slot_change_notifications
        send_slot_change_notifications(instance.id)
    else:
        from django_q.tasks import async_task
        async_task('scheduler.tasks.send_slot_change_notifications', instance.id)


post_save.connect(notify_slot_changes, sender=ScheduleSlot, dispatch_uid="notify_slot_changes")


def auto_provision_lecturer_credentials(sender, instance, created, **kwargs):
    """
    When a Lecturer is created or updated manually, trigger credentials provisioning in the background.
    """
    if getattr(_local, 'mute_signals', False):
        return
    if kwargs.get('raw', False):
        return

    import sys
    is_test = 'test' in sys.argv or 'pytest' in sys.argv or any('pytest' in arg for arg in sys.argv)
    if is_test:
        # In test environments, only auto-provision if explicitly enabled via _local flag
        if not getattr(_local, 'enable_auto_provision_in_tests', False):
            return

    if created and instance.is_active:
        university = get_university_from_instance(instance)
        if university:
            if is_test:
                from .tasks import provision_lecturer_credentials
                provision_lecturer_credentials(university.id)
            else:
                from django_q.tasks import async_task
                async_task('scheduler.tasks.provision_lecturer_credentials', university.id)

post_save.connect(auto_provision_lecturer_credentials, sender=Lecturer, dispatch_uid="auto_provision_lecturer_credentials")



