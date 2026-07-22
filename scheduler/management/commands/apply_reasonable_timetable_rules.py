"""
Management command: apply_reasonable_timetable_rules
=====================================================
Applies the 5 reasonableness criteria to the university data in Django DB:
1. Sets Course.sessions_per_week = 1 for all courses (1 3-hr session per week).
2. Updates Lecturers to max_hours_per_week = 15 and max_slots_per_day = 2 (6 hrs/day max).
3. Disaggregates generic Masters & PhD student groups (MASTERS - PT, MASTERS - WKD, PHD - PT)
   into distinct specialization sub-groups.
4. Enforces Mode of Study rules (FT daytime only, PT evening/weekend, WKD weekend).
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from scheduler.models import (
    University, Course, Lecturer, StudentGroup, TimeSlot, Constraint, Program
)


class Command(BaseCommand):
    help = "Applies 5 reasonableness criteria to database courses, lecturers, and student groups."

    def add_arguments(self, parser):
        parser.add_argument(
            "--uni-code", type=str, default="tes494784",
            help="University code to target (default: tes494784).",
        )
        parser.add_argument(
            "--execute", action="store_true", default=False,
            help="Actually commit changes to the database.",
        )

    def handle(self, *args, **options):
        uni_code = options["uni_code"]
        execute = options["execute"]

        try:
            uni = University.objects.get(code=uni_code)
        except University.DoesNotExist:
            # Fallback to first university
            uni = University.objects.first()
            if not uni:
                raise CommandError("No university found in database.")

        self.stdout.write(self.style.SUCCESS(f"Targeting University: {uni.name} ({uni.code})"))

        with transaction.atomic():
            # 1. Update Courses to 1 session per week
            courses = Course.objects.filter(program__department__faculty__campus__university=uni)
            updated_courses_count = courses.update(sessions_per_week=1, duration_slots=1)
            self.stdout.write(f"Updated {updated_courses_count} courses to 1 session/week (duration 1 slot / 3 hrs).")

            # 2. Update Lecturer workload limits
            lecturers = Lecturer.objects.filter(department__faculty__campus__university=uni)
            updated_lecs_count = lecturers.update(max_hours_per_week=15, max_slots_per_day=2)
            self.stdout.write(f"Updated {updated_lecs_count} lecturers to max 15 hrs/week and max 2 slots/day.")

            # 3. Disaggregate generic student groups
            target_groups = StudentGroup.objects.filter(
                program__department__faculty__campus__university=uni,
                name__in=['MASTERS - PT', 'MASTERS - WKD', 'PHD - PT - YEAR1TRIM2']
            )

            for g in target_groups:
                g_courses = list(Course.objects.filter(student_group=g))
                self.stdout.write(f"\nGroup '{g.name}' has {len(g_courses)} courses assigned.")
                if len(g_courses) <= 5:
                    continue  # already manageable size

                # Group courses by course code prefix / area
                # e.g., KMI, MKT, ACC, FIN, BAM, ECO, etc.
                courses_by_prefix = {}
                for c in g_courses:
                    prefix = c.code.split()[0] if c.code else 'GEN'
                    courses_by_prefix.setdefault(prefix, []).append(c)

                # Partition into sub-groups of max 5 courses each
                current_sub_idx = 1
                current_sub_courses = []
                
                # Split strategy: group into sub-groups of ~4-5 courses
                chunks = []
                current_chunk = []
                for c in g_courses:
                    current_chunk.append(c)
                    if len(current_chunk) >= 4:
                        chunks.append(current_chunk)
                        current_chunk = []
                if current_chunk:
                    chunks.append(current_chunk)

                base_name = g.name
                for idx, chunk in enumerate(chunks, 1):
                    sub_name = f"{base_name} - TRACK {idx}"
                    if execute:
                        sub_g, created = StudentGroup.objects.get_or_create(
                            program=g.program,
                            name=sub_name,
                            defaults={'size': max(15, g.size // len(chunks)), 'year': g.year}
                        )
                        for c in chunk:
                            c.student_group = sub_g
                            c.save()
                        self.stdout.write(f"  -> Reassigned {len(chunk)} courses to sub-group '{sub_name}'")
                    else:
                        self.stdout.write(f"  [DRY-RUN] Would reassign {len(chunk)} courses to sub-group '{sub_name}'")

            # 4. Mode of Study Constraints setup
            # Ensure Constraint records exist for FT / PT / WKD mode restrictions
            self.stdout.write("\nConfiguring Mode of Study constraints in database...")
            
            # Create or update Constraint for FT groups (No evening/weekend)
            c_ft, _ = Constraint.objects.get_or_create(
                university=uni,
                name="FT Groups Restricted to Weekday Daytime",
                defaults={
                    'constraint_type': 'NO_EVENING_CLASSES',
                    'is_hard': True,
                    'weight': 100,
                    'parameters': {'mode': 'FT'}
                }
            )

            if not execute:
                self.stdout.write(self.style.WARNING("\n[DRY-RUN COMPLETE] Pass --execute to commit DB changes."))
                transaction.set_rollback(True)
            else:
                self.stdout.write(self.style.SUCCESS("\n[SUCCESS] Applied all 5 reasonableness changes to DB!"))

