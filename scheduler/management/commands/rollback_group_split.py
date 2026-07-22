"""
Management command: rollback_group_split
========================================
Reverses a previous split_overloaded_groups --execute run by restoring
each Course.student_group FK to the original group recorded in the backup.

Usage:
    python manage.py rollback_group_split --backup backups/group_split_backup_<ts>.json
    python manage.py rollback_group_split --backup backups/group_split_backup_<ts>.json --execute
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from scheduler.models import Course, StudentGroup


class Command(BaseCommand):
    help = "Rollback a split_overloaded_groups run using its JSON backup file."

    def add_arguments(self, parser):
        parser.add_argument(
            "--backup", type=str, required=True,
            help="Path to the JSON backup file produced by split_overloaded_groups --execute.",
        )
        parser.add_argument(
            "--execute", action="store_true", default=False,
            help="Actually write the rollback to the database (default: dry-run).",
        )

    def handle(self, *args, **options):
        backup_path = Path(options["backup"])
        execute     = options["execute"]

        if not backup_path.exists():
            raise CommandError(f"Backup file not found: {backup_path}")

        records = json.loads(backup_path.read_text())
        self.stdout.write(f"Backup file : {backup_path}")
        self.stdout.write(f"Records     : {len(records)}")
        self.stdout.write(f"Mode        : {'EXECUTE' if execute else 'DRY-RUN'}")
        self.stdout.write("")

        # Verify all original group PKs exist
        original_pks = {r["original_group_pk"] for r in records}
        existing_pks = set(
            StudentGroup.objects.filter(pk__in=original_pks).values_list("pk", flat=True)
        )
        missing = original_pks - existing_pks
        if missing:
            raise CommandError(
                f"Original group PKs not found in DB (were they deleted?): {missing}"
            )

        # Print what will be restored
        by_group: dict[int, list] = {}
        for r in records:
            by_group.setdefault(r["original_group_pk"], []).append(r)

        for gpk, recs in sorted(by_group.items()):
            gname = recs[0]["original_group_name"]
            self.stdout.write(
                f"  Restore {len(recs):3d} courses -> group '{gname}' (pk={gpk})"
            )

        self.stdout.write("")

        if not execute:
            self.stdout.write(
                self.style.WARNING("DRY-RUN - no changes made. Re-run with --execute to apply.")
            )
            return

        # Execute rollback in a single atomic transaction
        try:
            with transaction.atomic():
                for r in records:
                    Course.objects.filter(pk=r["course_pk"]).update(
                        student_group_id=r["original_group_pk"]
                    )
        except Exception as exc:
            raise CommandError(f"Rollback transaction failed: {exc}")

        self.stdout.write(self.style.SUCCESS(
            f"Rollback complete - {len(records)} course(s) restored to their original groups."
        ))
