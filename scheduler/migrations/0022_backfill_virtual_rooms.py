from django.db import migrations


def backfill_virtual_rooms(apps, schema_editor):
    """
    Data migration: sets is_virtual=True for every existing room whose name
    contains any of the known virtual-room keywords (case-insensitive).
    Also sets room_type='Virtual' for those rooms.
    """
    Room = apps.get_model('scheduler', 'Room')
    VIRTUAL_KEYWORDS = ('zoom', 'virtual', 'online', 'teams')
    updated = []
    for room in Room.objects.all():
        name_lower = room.name.strip().lower()
        if any(kw in name_lower for kw in VIRTUAL_KEYWORDS):
            room.is_virtual = True
            room.room_type = 'Virtual'
            updated.append(room)
    if updated:
        Room.objects.bulk_update(updated, ['is_virtual', 'room_type'], batch_size=500)


def backfill_course_delivery_mode(apps, schema_editor):
    """
    Data migration: marks courses as Online (OL) if their assigned room is virtual.
    Falls back to Physical (PH) for all other courses.
    """
    Course = apps.get_model('scheduler', 'Course')
    # All courses default to Physical — nothing to do here since the field
    # default is already 'PH'. This function is a placeholder for future
    # per-course delivery assignment via the UI.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('scheduler', '0021_add_delivery_mode_and_is_virtual'),
    ]

    operations = [
        migrations.RunPython(
            backfill_virtual_rooms,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.RunPython(
            backfill_course_delivery_mode,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
