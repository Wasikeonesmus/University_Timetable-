"""
fix_dbm_common_units.py
-------------------------
One-off fix for DBM FT-MAIN (id=21250), the one flagged group that had no
existing trimester subgroups to dedupe against (unlike the other 6, which
were fixed by fix_flattened_groups.py).

Confirmed with the university:
  - DCU/BUD course codes are genuine common units shared across multiple
    diploma programs (Communication Skills, Health Awareness, Information
    Literacy, Entrepreneurship, Computer Applications, Business
    Mathematics, Theories of Commerce) -- not DBM-specific.
  - DFI 001 (Fundamentals of International Business) is NOT touched here --
    still unconfirmed whether it belongs to DBM at all. Left exactly where
    it is.

This moves the 7 confirmed common-unit courses off DBM FT-MAIN onto a new
"DBM FT-MAIN - COMMON UNITS" group (linked via parent_group so the solver/
reports can still see they're associated with DBM FT-MAIN). That drops
DBM FT-MAIN from 61 to 44 slot-units/week -- comfortably under the 59
available timeslots, with no new timeslots and no data loss.

SAFE BY DEFAULT: prints a plan only, unless --apply is passed.

USAGE
    python manage.py fix_dbm_common_units              # dry run
    python manage.py fix_dbm_common_units --apply       # actually move courses
"""
import datetime
import json

from django.core.management.base import BaseCommand
from django.db import transaction
from django.forms.models import model_to_dict

from scheduler.models import StudentGroup, Course

# Course codes confirmed as shared common units, not DBM-specific
COMMON_UNIT_CODES = ['BUD 001', 'BUD 003', 'DCU 001', 'DCU 002', 'DCU 003', 'DCU 005', 'DCU OO4']
DBM_GROUP_ID = 21250


class Command(BaseCommand):
    help = "Splits confirmed shared common units off DBM FT-MAIN onto their own group."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                             help='Actually move the courses (default: dry run only).')

    def handle(self, *args, **options):
        try:
            dbm_group = StudentGroup.objects.get(pk=DBM_GROUP_ID)
        except StudentGroup.DoesNotExist:
            self.stderr.write(self.style.ERROR(
                f"StudentGroup id={DBM_GROUP_ID} not found -- check DBM_GROUP_ID is still correct."
            ))
            return

        courses = list(Course.objects.filter(student_group=dbm_group, code__in=COMMON_UNIT_CODES))
        if not courses:
            self.stdout.write("No matching common-unit courses found on this group -- nothing to do.")
            return

        moved_units = sum(c.duration_slots * c.sessions_per_week for c in courses)
        remaining_courses = Course.objects.filter(student_group=dbm_group).exclude(id__in=[c.id for c in courses])
        remaining_units = sum(c.duration_slots * c.sessions_per_week for c in remaining_courses)

        self.stdout.write(f"'{dbm_group.name}' (id={dbm_group.id}):")
        self.stdout.write(f"  {len(courses)} common-unit course(s) to move ({moved_units} slot-units): "
                           + ", ".join(c.code for c in courses))
        self.stdout.write(f"  {remaining_courses.count()} course(s) staying ({remaining_units} slot-units) "
                           f"-- {'OK, under 59-slot cap' if remaining_units <= 59 else 'STILL OVER CAP, investigate further'}")

        if not options['apply']:
            self.stdout.write(self.style.WARNING("\nDry run only -- re-run with --apply to make this change."))
            return

        backup = {
            'generated_at': datetime.datetime.now().isoformat(),
            'moved_courses': [model_to_dict(c) for c in courses],
        }
        backup_path = f"dbm_common_units_backup_{datetime.datetime.now():%Y%m%d_%H%M%S}.json"
        with open(backup_path, 'w') as f:
            json.dump(backup, f, indent=2, default=str)
        self.stdout.write(self.style.SUCCESS(f"Backup written to {backup_path}"))

        with transaction.atomic():
            common_group, created = StudentGroup.objects.get_or_create(
                name=f"{dbm_group.name} - COMMON UNITS",
                program=dbm_group.program,
                defaults={
                    'size': dbm_group.size,
                    'year': dbm_group.year,
                    'parent_group': dbm_group,
                }
            )
            Course.objects.filter(id__in=[c.id for c in courses]).update(student_group=common_group)

        self.stdout.write(self.style.SUCCESS(
            f"Moved {len(courses)} courses to '{common_group.name}' (id={common_group.id}, "
            f"{'created' if created else 'already existed'}). Re-run validation to confirm."
        ))
