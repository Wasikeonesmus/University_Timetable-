# Generated manually — adds DB-level double-booking guards to ScheduleSlot.
#
# These constraints are a safety net: they don't change solver behaviour,
# they just make it impossible for a bug (or a manual edit) to silently
# persist an overlapping room/lecturer/student-group assignment. If the
# solver ever tries to write a genuine double-booking, this will raise an
# IntegrityError instead of the conflict only being discovered later by
# the post-solve conflict checker.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduler', '0018_alter_constraint_constraint_type'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='scheduleslot',
            index=models.Index(fields=['timetable', 'time_slot'], name='scheduler_ss_tt_ts_idx'),
        ),
        migrations.AddConstraint(
            model_name='scheduleslot',
            constraint=models.UniqueConstraint(
                fields=['timetable', 'room', 'time_slot'],
                name='uniq_room_per_slot',
            ),
        ),
        migrations.AddConstraint(
            model_name='scheduleslot',
            constraint=models.UniqueConstraint(
                fields=['timetable', 'lecturer', 'time_slot'],
                name='uniq_lecturer_per_slot',
            ),
        ),
        migrations.AddConstraint(
            model_name='scheduleslot',
            constraint=models.UniqueConstraint(
                fields=['timetable', 'student_group', 'time_slot'],
                name='uniq_group_per_slot',
            ),
        ),
    ]
