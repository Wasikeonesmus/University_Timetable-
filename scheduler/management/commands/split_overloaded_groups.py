"""
Management command: split_overloaded_groups
===========================================
Splits student groups whose total course slot-units exceed the university's
timeslot count, by partitioning their courses into per-year-level sub-groups.

Safe by default — pass --execute to commit changes to the database.
A JSON backup is written to <BASE_DIR>/backups/ before any write so the
operation can be fully rolled back with the companion rollback_group_split command.

Usage:
    # Dry-run (default — prints what would change, touches nothing)
    python manage.py split_overloaded_groups

    # Live run (writes to DB, creates backup file)
    python manage.py split_overloaded_groups --execute

    # Target a specific timetable pk (default: 32)
    python manage.py split_overloaded_groups --timetable 32 --execute
"""

import json
import re
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from scheduler.models import (
    Course, StudentGroup, TimeSlot, Timetable,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _course_year_level(course_code: str) -> int:
    """
    Infer the academic year level (1/2/3) from a course code.

    Strategy — first digit of any digit-run found in the code:
      BAM 1102  → level 1
      ICT 2104  → level 2
      ACC 3106  → level 3
      BUD 001   → level 0  (diploma/foundation — treated as Year 1)
      DCU OO4   → level 0  (OCR-noise — treated as Year 1)

    Returns 1, 2, or 3 (clamps anything outside 1-3 to 1).
    """
    digits = re.search(r'\d', course_code)
    if not digits:
        return 1
    first_digit = int(digits.group())
    if first_digit == 0:
        return 1          # foundation / diploma codes (BUD 001, DCU OO4 etc.)
    return max(1, min(3, first_digit))


def _group_year_name(base_name: str, year: int) -> str:
    """Return the sub-group name for a given year level."""
    return f"{base_name}-Y{year}"


def _slot_units(course) -> int:
    return course.duration_slots * course.sessions_per_week


# ── command ───────────────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = (
        "Split overloaded student groups into per-year-level sub-groups. "
        "Dry-run by default; pass --execute to write to the database."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--timetable", type=int, default=32,
            help="Primary key of the Timetable to fix (default: 32).",
        )
        parser.add_argument(
            "--execute", action="store_true", default=False,
            help="Actually write changes to the database (default: dry-run only).",
        )
        parser.add_argument(
            "--backup-dir", type=str, default=None,
            help="Directory for the rollback JSON backup (default: <BASE_DIR>/backups/).",
        )

    def handle(self, *args, **options):
        timetable_pk = options["timetable"]
        execute       = options["execute"]
        backup_dir    = options["backup_dir"]

        # ── load timetable & university ──────────────────────────────────────
        try:
            tt = Timetable.objects.get(pk=timetable_pk)
        except Timetable.DoesNotExist:
            raise CommandError(f"Timetable pk={timetable_pk} does not exist.")

        uni = tt.semester.university
        self.stdout.write(f"Timetable : {tt.name}  (pk={tt.pk})")
        self.stdout.write(f"University: {uni.name}")
        self.stdout.write(f"Mode      : {'EXECUTE (live write)' if execute else 'DRY-RUN (no DB changes)'}")
        self.stdout.write("")

        # ── gather data ──────────────────────────────────────────────────────
        courses = list(
            Course.objects
            .filter(program__department__faculty__campus__university=uni)
            .select_related("student_group", "program")
        )
        total_ts = TimeSlot.objects.filter(university=uni).count()
        self.stdout.write(f"Total timeslots : {total_ts}")
        self.stdout.write(f"Total courses   : {len(courses)}")
        self.stdout.write("")

        # ── identify overloaded groups ───────────────────────────────────────
        group_demand: dict[int, int] = {}
        group_map: dict[int, StudentGroup] = {}
        for c in courses:
            if not c.student_group:
                continue
            g = c.student_group
            group_map[g.id]    = g
            group_demand[g.id] = group_demand.get(g.id, 0) + _slot_units(c)

        overloaded = {
            gid: demand
            for gid, demand in group_demand.items()
            if demand > total_ts
        }

        if not overloaded:
            self.stdout.write(self.style.SUCCESS("No overloaded groups found — nothing to do."))
            return

        self.stdout.write(
            f"Found {len(overloaded)} overloaded group(s) "
            f"(need more than {total_ts} timeslot-units):"
        )
        for gid, demand in sorted(overloaded.items(), key=lambda x: -x[1]):
            g = group_map[gid]
            self.stdout.write(f"  {g.name:40s}  {demand} units  (excess +{demand - total_ts})")
        self.stdout.write("")

        # ── build the split plan ─────────────────────────────────────────────
        #
        # For each overloaded group we:
        #   1. Bucket its courses by year level (1/2/3).
        #   2. For each non-empty bucket, create (or reuse) a sub-group named
        #      "<original_name>-Y<level>".
        #   3. Re-point each course's student_group FK to the sub-group.
        #
        # We never delete the original group — existing ScheduleSlot records
        # still reference it, and removing it would cascade-delete history.

        Plan = list  # just a plain list of dicts
        plan: list[dict] = []   # one entry per (group, year_level) bucket

        for gid, demand in overloaded.items():
            g = group_map[gid]
            g_courses = [c for c in courses if c.student_group_id == gid]

            # Bucket courses by year level
            buckets: dict[int, list] = {}
            for c in g_courses:
                lvl = _course_year_level(c.code)
                buckets.setdefault(lvl, []).append(c)

            for year_level, bucket_courses in sorted(buckets.items()):
                sub_name   = _group_year_name(g.name, year_level)
                bucket_units = sum(_slot_units(c) for c in bucket_courses)
                plan.append({
                    "original_group_id":   gid,
                    "original_group_name": g.name,
                    "original_group_obj":  g,
                    "year_level":          year_level,
                    "sub_group_name":      sub_name,
                    "courses":             bucket_courses,
                    "slot_units":          bucket_units,
                    "still_over":          bucket_units > total_ts,
                })

        # ── print plan ───────────────────────────────────────────────────────
        self.stdout.write("=" * 70)
        self.stdout.write("SPLIT PLAN")
        self.stdout.write("=" * 70)

        warnings_found = False
        for entry in plan:
            status = (
                self.style.ERROR("STILL OVER") if entry["still_over"]
                else self.style.SUCCESS("OK ")
            )
            self.stdout.write(
                f"  {entry['original_group_name']:40s} -> "
                f"{entry['sub_group_name']:50s}  "
                f"{entry['slot_units']:3d} units  {status}"
            )
            if entry["still_over"]:
                warnings_found = True
                self.stdout.write(
                    f"    -> still {entry['slot_units'] - total_ts} units over — "
                    f"you'll need to manually remove a course from this sub-group."
                )
            # Show sample course codes
            samples = [c.code for c in entry["courses"][:5]]
            extra   = len(entry["courses"]) - 5
            tail    = f" … +{extra} more" if extra > 0 else ""
            self.stdout.write(f"    Courses ({len(entry['courses'])}): {', '.join(samples)}{tail}")

        self.stdout.write("")

        if not execute:
            self.stdout.write(
                self.style.WARNING(
                    "DRY-RUN complete — no changes made.\n"
                    "Re-run with --execute to apply."
                )
            )
            return

        # ── write backup ─────────────────────────────────────────────────────
        from django.conf import settings

        backup_root = Path(backup_dir) if backup_dir else (
            Path(settings.BASE_DIR) / "backups"
        )
        backup_root.mkdir(parents=True, exist_ok=True)
        ts_str      = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_root / f"group_split_backup_{ts_str}.json"

        backup_data = []
        for entry in plan:
            for c in entry["courses"]:
                backup_data.append({
                    "course_pk":             c.pk,
                    "course_code":           c.code,
                    "original_group_pk":     entry["original_group_id"],
                    "original_group_name":   entry["original_group_name"],
                })

        backup_path.write_text(json.dumps(backup_data, indent=2))
        self.stdout.write(f"Backup written -> {backup_path}")
        self.stdout.write("")

        # ── execute inside a single transaction ──────────────────────────────
        created_groups: dict[str, StudentGroup] = {}   # name → instance
        course_updates: list[tuple[int, int]]   = []   # (course_pk, new_group_pk)

        try:
            with transaction.atomic():
                for entry in plan:
                    sub_name = entry["sub_group_name"]
                    orig_g   = entry["original_group_obj"]

                    # Create sub-group if it doesn't already exist
                    if sub_name not in created_groups:
                        sub_g, created = StudentGroup.objects.get_or_create(
                            name=sub_name,
                            program=orig_g.program,
                            defaults={
                                "size":         orig_g.size,
                                "year":         entry["year_level"],
                                "parent_group": orig_g,
                            },
                        )
                        created_groups[sub_name] = sub_g
                        verb = "Created" if created else "Reused existing"
                        self.stdout.write(f"  {verb} group: {sub_name} (pk={sub_g.pk})")
                    else:
                        sub_g = created_groups[sub_name]

                    # Re-point courses
                    course_pks = [c.pk for c in entry["courses"]]
                    updated    = Course.objects.filter(pk__in=course_pks).update(
                        student_group=sub_g
                    )
                    course_updates.extend((pk, sub_g.pk) for pk in course_pks)
                    self.stdout.write(
                        f"    Moved {updated} course(s) -> {sub_name}"
                    )

        except Exception as exc:
            raise CommandError(
                f"Transaction rolled back due to error: {exc}\n"
                f"No changes were committed. Backup (unused) is at {backup_path}."
            )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"OK. {len(created_groups)} sub-group(s) created/reused, "
            f"{len(course_updates)} course(s) re-assigned.\n"
            f"Rollback: python manage.py rollback_group_split --backup {backup_path}"
        ))
        if warnings_found:
            self.stdout.write(self.style.WARNING(
                "!  One or more sub-groups are still over the timeslot limit - "
                "see the plan output above for details."
            ))
