"""
fix_flattened_groups.py
------------------------
Fixes the "flattened group" data-quality issue found during a validation
audit: several student groups (e.g. "BCOM FT-MAIN") have courses attached
directly to them, while proper per-trimester subgroups also exist
(e.g. "BCOM FT-MAIN - YEAR1TRIM1") that already carry an identical copy of
almost every one of those courses. This is a duplicate-import artifact, not
a real course load -- the fix is to delete the duplicate rows on the flat
group, not to reassign or re-split them.

WHAT THIS DOES
  1. For every "flat" group that has at least one matching "<name> - ... YEAR..."
     subgroup: finds courses on the flat group whose (code, duration_slots,
     sessions_per_week) already exists on one of those subgroups, and marks
     them as duplicates.
  2. Backs up every row it's about to touch (as JSON) before deleting anything.
  3. Deletes the duplicate Course rows (this cascades to any ScheduleSlot rows
     referencing them -- see --check-slots below to see that impact first).
  4. Reports any flat-group courses that did NOT match anything on a subgroup
     ("genuinely unmatched") without touching them -- those need a human
     decision, not an automatic delete.

SAFE BY DEFAULT: running with no flags only prints a report. Nothing is
deleted unless you pass --apply.

USAGE
    python manage.py fix_flattened_groups                  # dry run, report only
    python manage.py fix_flattened_groups --check-slots     # also show any
                                                              ScheduleSlot rows
                                                              that would cascade-delete
    python manage.py fix_flattened_groups --apply           # actually delete
                                                              duplicates (writes a
                                                              backup file first)
    python manage.py fix_flattened_groups --university=CUK  # limit to one university
"""
import json
import datetime
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.forms.models import model_to_dict

from scheduler.models import University, StudentGroup, Course, ScheduleSlot


class Command(BaseCommand):
    help = "Finds and removes duplicate Course rows left on 'flattened' student groups."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                             help='Actually delete the duplicate rows (default: dry run only).')
        parser.add_argument('--check-slots', action='store_true',
                             help='Also report any ScheduleSlot rows that reference the duplicates.')
        parser.add_argument('--university', type=str, default=None,
                             help='Restrict to one university, matched by name or code.')

    def handle(self, *args, **options):
        apply_changes = options['apply']
        check_slots = options['check_slots']
        uni_filter = options['university']

        universities = University.objects.all()
        if uni_filter:
            universities = universities.filter(name__icontains=uni_filter) | universities.filter(code__icontains=uni_filter)

        if not universities.exists():
            self.stderr.write(self.style.ERROR(f"No university matched '{uni_filter}'."))
            return

        backup = {'generated_at': datetime.datetime.now().isoformat(), 'courses': []}
        all_dupe_course_ids = []
        total_flat_groups = 0
        total_dupes = 0
        total_unmatched = 0

        for university in universities:
            groups = list(StudentGroup.objects.filter(
                program__department__faculty__campus__university=university
            ))
            names = {g.name.strip(): g for g in groups}

            # A "flat" group is one whose name is a strict prefix of at least
            # one other group's name that also contains "YEAR" -- covers both
            # "X - YEAR1TRIM1" and "X - FT - YEAR1TRIM1" style conventions.
            flat_to_children = defaultdict(list)
            for name, g in names.items():
                if "YEAR" in name:
                    continue  # already a subgroup itself, not a flat parent
                for other_name, other_g in names.items():
                    if other_g.id == g.id:
                        continue
                    if other_name.startswith(name + " ") and "YEAR" in other_name:
                        flat_to_children[g.id].append(other_g)

            if not flat_to_children:
                continue

            self.stdout.write(self.style.MIGRATE_HEADING(f"\n{university.name}"))

            for flat_id, children in flat_to_children.items():
                flat_group = StudentGroup.objects.get(pk=flat_id)
                flat_courses = list(Course.objects.filter(student_group=flat_group))
                if not flat_courses:
                    continue

                child_ids = [c.id for c in children]
                subgroup_keys = set(
                    Course.objects.filter(student_group_id__in=child_ids)
                    .values_list('code', 'duration_slots', 'sessions_per_week')
                )

                dupes = [c for c in flat_courses
                         if (c.code, c.duration_slots, c.sessions_per_week) in subgroup_keys]
                unmatched = [c for c in flat_courses if c not in dupes]

                total_flat_groups += 1
                total_dupes += len(dupes)
                total_unmatched += len(unmatched)

                self.stdout.write(
                    f"  '{flat_group.name}' (id={flat_group.id}): "
                    f"{len(flat_courses)} courses, {len(dupes)} duplicate of a subgroup course, "
                    f"{len(unmatched)} unmatched"
                )
                if unmatched:
                    self.stdout.write(self.style.WARNING(
                        f"      NOT touching {len(unmatched)} unmatched course(s) -- review manually: "
                        + ", ".join(f"{c.code} (id={c.id})" for c in unmatched)
                    ))

                if check_slots and dupes:
                    slot_qs = ScheduleSlot.objects.filter(course_id__in=[c.id for c in dupes])
                    if slot_qs.exists():
                        self.stdout.write(self.style.WARNING(
                            f"      {slot_qs.count()} ScheduleSlot row(s) reference these duplicates "
                            f"and will cascade-delete with them."
                        ))

                for c in dupes:
                    backup['courses'].append(model_to_dict(c))
                    all_dupe_course_ids.append(c.id)

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nTOTAL: {total_flat_groups} flat groups, {total_dupes} duplicate courses found, "
            f"{total_unmatched} unmatched courses left untouched."
        ))

        if not all_dupe_course_ids:
            self.stdout.write("Nothing to do.")
            return

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                "\nDry run only -- no changes made. Re-run with --apply to delete the "
                f"{len(all_dupe_course_ids)} duplicate course rows listed above."
            ))
            return

        backup_path = f"flattened_groups_backup_{datetime.datetime.now():%Y%m%d_%H%M%S}.json"
        with open(backup_path, 'w') as f:
            json.dump(backup, f, indent=2, default=str)
        self.stdout.write(self.style.SUCCESS(f"Backup written to {backup_path}"))

        with transaction.atomic():
            deleted, _ = Course.objects.filter(id__in=all_dupe_course_ids).delete()
        self.stdout.write(self.style.SUCCESS(
            f"Deleted {len(all_dupe_course_ids)} duplicate Course rows ({deleted} total rows "
            f"including cascaded ScheduleSlot rows, if any). Re-run validation to confirm."
        ))
