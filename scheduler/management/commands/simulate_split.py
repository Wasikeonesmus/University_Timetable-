"""
Management command: simulate_split
===================================
Read-only simulation of split_overloaded_groups.
Validates the year-level detection algorithm against real DB data.
No writes, no backup, no side effects.

Usage:
    python manage.py simulate_split
    python manage.py simulate_split --timetable 32
"""

import re
from django.core.management.base import BaseCommand, CommandError
from scheduler.models import Course, StudentGroup, TimeSlot, Timetable


def _course_year_level(course_code: str) -> int:
    """Exact copy of the algorithm in split_overloaded_groups."""
    digits = re.search(r'\d', course_code)
    if not digits:
        return 1
    first_digit = int(digits.group())
    if first_digit == 0:
        return 1
    return max(1, min(3, first_digit))


def _group_year_name(base_name: str, year: int) -> str:
    return f"{base_name}-Y{year}"


def _slot_units(course) -> int:
    return course.duration_slots * course.sessions_per_week


class Command(BaseCommand):
    help = "Read-only simulation of split_overloaded_groups — validates detection logic."

    def add_arguments(self, parser):
        parser.add_argument("--timetable", type=int, default=32)

    def handle(self, *args, **options):
        tt = Timetable.objects.get(pk=options["timetable"])
        uni = tt.semester.university
        total_ts = TimeSlot.objects.filter(university=uni).count()

        courses = list(
            Course.objects
            .filter(program__department__faculty__campus__university=uni)
            .select_related("student_group", "program")
        )

        self.stdout.write(f"University  : {uni.name}")
        self.stdout.write(f"Timeslots   : {total_ts}")
        self.stdout.write(f"Courses     : {len(courses)}")
        self.stdout.write("")

        # Find overloaded groups
        group_demand: dict[int, int] = {}
        group_map: dict[int, StudentGroup] = {}
        for c in courses:
            if not c.student_group:
                continue
            g = c.student_group
            group_map[g.id] = g
            group_demand[g.id] = group_demand.get(g.id, 0) + _slot_units(c)

        overloaded = {
            gid: d for gid, d in group_demand.items() if d > total_ts
        }

        if not overloaded:
            self.stdout.write(self.style.SUCCESS("No overloaded groups — nothing to split."))
            return

        self.stdout.write(f"Overloaded groups: {len(overloaded)}")
        self.stdout.write("")

        total_still_over = 0
        ambiguous_codes  = []
        clamped_codes    = []

        for gid in sorted(overloaded, key=lambda x: -overloaded[x]):
            g         = group_map[gid]
            demand    = overloaded[gid]
            g_courses = [c for c in courses if c.student_group_id == gid]

            self.stdout.write("-" * 70)
            self.stdout.write(f"GROUP: {g.name}  (pk={gid})")
            self.stdout.write(
                f"  Original: {demand} slot-units / {len(g_courses)} courses  "
                f"(excess +{demand - total_ts})"
            )

            # Bucket by year level
            buckets: dict[int, list] = {}
            for c in g_courses:
                lvl = _course_year_level(c.code)
                buckets.setdefault(lvl, []).append(c)

                if not re.search(r'\d', c.code):
                    ambiguous_codes.append((g.name, c.code))
                elif int(re.search(r'\d', c.code).group()) == 0:
                    clamped_codes.append((g.name, c.code, lvl))

            for year in sorted(buckets):
                bucket   = buckets[year]
                units    = sum(_slot_units(c) for c in bucket)
                sub_name = _group_year_name(g.name, year)
                still    = units > total_ts

                if still:
                    total_still_over += 1
                    flag = self.style.ERROR("  !! STILL OVER")
                else:
                    flag = self.style.SUCCESS("  OK")

                self.stdout.write(
                    f"  -> {sub_name:55s} {units:3d} units / {len(bucket)} courses{flag}"
                )

                if still:
                    sorted_c = sorted(bucket, key=lambda c: -_slot_units(c))
                    self.stdout.write(
                        f"    Need to remove {units - total_ts} unit(s). Top contributors:"
                    )
                    for c in sorted_c[:5]:
                        self.stdout.write(
                            f"      {c.code}: {c.sessions_per_week}x{c.duration_slots}="
                            f"{_slot_units(c)}u  {c.name[:50]}"
                        )

                samples = [c.code for c in bucket[:6]]
                extra   = len(bucket) - 6
                tail    = f" … +{extra} more" if extra > 0 else ""
                self.stdout.write(f"    Sample: {', '.join(samples)}{tail}")

            self.stdout.write("")

        # Edge case report
        self.stdout.write("=" * 70)
        self.stdout.write("EDGE CASES")
        self.stdout.write("=" * 70)

        self.stdout.write(
            f"Courses with no digits in code (-> defaulted to Y1): {len(ambiguous_codes)}"
        )
        for gname, code in ambiguous_codes[:20]:
            self.stdout.write(f"  [{gname}]  {code}")

        self.stdout.write(
            f"Courses with leading 0 (diploma/foundation, clamped -> Y1): {len(clamped_codes)}"
        )
        for gname, code, lvl in clamped_codes[:20]:
            self.stdout.write(f"  [{gname}]  {code}  -> Y{lvl}")

        self.stdout.write("")
        self.stdout.write("=" * 70)
        self.stdout.write("RESULT")
        self.stdout.write("=" * 70)

        if total_still_over == 0:
            self.stdout.write(self.style.SUCCESS(
                "OK All sub-groups would be within the timeslot limit after splitting."
            ))
            self.stdout.write("")
            self.stdout.write("Ready to apply:")
            self.stdout.write("  python manage.py split_overloaded_groups            (dry-run)")
            self.stdout.write("  python manage.py split_overloaded_groups --execute  (live)")
        else:
            self.stdout.write(self.style.ERROR(
                f"⚠  {total_still_over} sub-group(s) would still be over the limit."
            ))
            self.stdout.write("Fix those before running --execute.")
