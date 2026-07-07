"""
scheduler/conflict_cache.py
---------------------------
Module-level in-process cache for the conflicts_json AJAX endpoint.

Extracted into its own module so both views.py and scheduling_service.py
can import it without creating a circular dependency
(views.py → scheduling_service.py means scheduling_service cannot import
from views.py directly).

Cache key: the tuple returned by _get_timetable_conflict_fingerprint()
  (timetable_id, tuple(slots_fp), tuple(constraints_fp), tuple(avail_fp))

Invalidation:
  - Call clear_conflicts_cache(timetable_id) after any operation that
    writes ScheduleSlot rows for that timetable (generation, autofix,
    drag-drop slot_update).
  - The cache also self-heals: the fingerprint is recomputed from the DB
    on every request, so a stale entry is simply never matched after slots
    change — the eviction just frees memory sooner.

Multi-process note:
  This is a per-process dict.  Each gunicorn/celery worker maintains its
  own copy.  That is intentional: the cache is cheap to rebuild and not
  easily serialisable across processes.
"""

_CONFLICTS_JSON_CACHE: dict = {}

# Maximum number of entries before the whole cache is wiped.
# Each entry is keyed by a fingerprint tuple (cheap), value is a small
# JSON-serialisable dict.  500 entries ≈ a few MB at most.
_CACHE_MAX_SIZE = 500


def clear_conflicts_cache(timetable_id: int) -> None:
    """
    Remove all cached results for a given timetable.
    Call this whenever ScheduleSlot rows for that timetable are modified.
    """
    keys_to_del = [k for k in _CONFLICTS_JSON_CACHE if k[0] == timetable_id]
    for k in keys_to_del:
        _CONFLICTS_JSON_CACHE.pop(k, None)
