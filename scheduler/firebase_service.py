import logging
import time
import threading
from django.conf import settings

logger = logging.getLogger(__name__)

# Initialize Firebase admin SDK safely
firebase_app = None
is_enabled = False

credentials_json = getattr(settings, 'FIREBASE_CREDENTIALS_JSON', None)
database_url = getattr(settings, 'FIREBASE_DATABASE_URL', None)

import os

if credentials_json and database_url and os.path.exists(credentials_json):
    try:
        import firebase_admin
        from firebase_admin import credentials

        # Prevent double initialization in multi-worker environments
        if not firebase_admin._apps:
            cred = credentials.Certificate(credentials_json)
            firebase_app = firebase_admin.initialize_app(cred, {
                'databaseURL': database_url
            })
        else:
            firebase_app = firebase_admin.get_app()
        is_enabled = True
        logger.info("[Firebase] Successfully initialized Firebase Admin app.")
    except Exception as e:
        logger.warning(f"[Firebase] Initialization failed: {e}. Falling back to standard polling.")
        is_enabled = False
else:
    logger.info("[Firebase] Unconfigured. Real-time features disabled; falling back to polling.")


def _fire_and_forget(fn, *args, **kwargs):
    """
    Runs *fn* on a daemon thread so Firebase HTTPS calls can never block
    the scheduling pipeline, regardless of network latency or reachability.
    """
    t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
    t.start()


def update_generation_status(timetable_id: int, data: dict):
    """
    Pushes generation status data to Firebase under:
    /timetables/{timetable_id}/status
    """
    if not is_enabled:
        return

    def _do():
        try:
            from firebase_admin import db
            ref = db.reference(f'timetables/{timetable_id}/status')
            ref.set(data)
            logger.debug(f"[Firebase] Updated status for timetable {timetable_id}")
        except Exception as e:
            logger.warning(f"[Firebase] Failed to write status for timetable {timetable_id}: {e}")

    _fire_and_forget(_do)


def update_timetable_conflicts(timetable_id: int, data: dict):
    """
    Pushes conflict list data to Firebase under:
    /timetables/{timetable_id}/conflicts
    """
    if not is_enabled:
        return

    def _do():
        try:
            from firebase_admin import db
            ref = db.reference(f'timetables/{timetable_id}/conflicts')
            ref.set(data)
            logger.debug(f"[Firebase] Updated conflicts for timetable {timetable_id}")
        except Exception as e:
            logger.warning(f"[Firebase] Failed to write conflicts for timetable {timetable_id}: {e}")

    _fire_and_forget(_do)


def trigger_timetable_refresh(timetable_id: int):
    """
    Updates the /timetables/{timetable_id}/refresh timestamp to signal clients
    that they should reload the schedule if another user updated a slot.
    """
    if not is_enabled:
        return

    def _do():
        try:
            from firebase_admin import db
            ref = db.reference(f'timetables/{timetable_id}/refresh')
            ref.set({
                'updated_at': int(time.time() * 1000)
            })
            logger.debug(f"[Firebase] Triggered refresh timestamp for timetable {timetable_id}")
        except Exception as e:
            logger.warning(f"[Firebase] Failed to write refresh trigger for timetable {timetable_id}: {e}")

    _fire_and_forget(_do)
