from django.core.management.base import BaseCommand
from scheduler.models import Course, Room, TimeSlot, Timetable


class Command(BaseCommand):
    help = "Diagnose validation errors for a timetable"

    def add_arguments(self, parser):
        parser.add_argument('--id', type=int, help="Timetable ID to diagnose")

    def handle(self, *args, **options):
        tt_id = options.get('id')
        if tt_id:
            tt = Timetable.objects.get(pk=tt_id)
        else:
            tt = Timetable.objects.last()
        if not tt:
            self.stdout.write("No timetable found.")
            return
        uni = tt.semester.university

        courses = list(Course.objects.filter(
            program__department__faculty__campus__university=uni
        ).select_related('lecturer', 'student_group').prefetch_related('additional_student_groups'))
        rooms     = list(Room.objects.filter(campus__university=uni))
        timeslots = list(TimeSlot.objects.filter(university=uni))
        total_ts  = len(timeslots)

        self.stdout.write(f"University : {uni.name}")
        self.stdout.write(f"Timeslots  : {total_ts}")
        self.stdout.write(f"Rooms      : {len(rooms)}")
        self.stdout.write(f"Courses    : {len(courses)}")
        self.stdout.write("")

        # ── 1. Lecturer over-allocation (multiplier=1.5 as in validation.py) ──
        TOLERANCE = 1.10
        lecturer_hours = {}
        lecturer_map   = {}
        for c in courses:
            if c.lecturer:
                lec = c.lecturer
                lecturer_map[lec.id] = lec
                lecturer_hours[lec.id] = (
                    lecturer_hours.get(lec.id, 0.0) + c.duration_slots * c.sessions_per_week * 1.5
                )

        self.stdout.write("=== LECTURER OVER-ALLOCATION (blocking errors) ===")
        over_lec = [(lid, h) for lid, h in lecturer_hours.items()
                    if h > lecturer_map[lid].max_hours_per_week * TOLERANCE]
        if not over_lec:
            self.stdout.write("  (none)")
        for lid, hours in over_lec:
            lec = lecturer_map[lid]
            self.stdout.write(
                f"  {lec.name}: {hours}h assigned / {lec.max_hours_per_week}h max "
                f"(threshold={lec.max_hours_per_week * TOLERANCE:.1f}h)"
            )
            for c in courses:
                if c.lecturer_id == lid:
                    h = c.duration_slots * c.sessions_per_week * 1.5
                    self.stdout.write(
                        f"    - {c.code}: {c.sessions_per_week}sess x {c.duration_slots}slots x1.5 = {h}h"
                    )
        self.stdout.write("")

        # ── 2. Student group timeslot overload ──────────────────────────────
        group_demand = {}
        group_map    = {}
        for c in courses:
            if not c.student_group:
                continue
            demand = c.duration_slots * c.sessions_per_week
            g = c.student_group
            group_map[g.id]    = g
            group_demand[g.id] = group_demand.get(g.id, 0) + demand
            for eg in c.additional_student_groups.all():
                group_map[eg.id]    = eg
                group_demand[eg.id] = group_demand.get(eg.id, 0) + demand

        self.stdout.write("=== STUDENT GROUP OVERLOAD (blocking errors) ===")
        over_groups = [(gid, d) for gid, d in group_demand.items() if d > total_ts]
        if not over_groups:
            self.stdout.write("  (none)")
        for gid, demand in sorted(over_groups, key=lambda x: -x[1]):
            g = group_map[gid]
            excess = demand - total_ts
            self.stdout.write(
                f"  {g.name}: {demand} slot-units / {total_ts} available  (excess: +{excess})"
            )
            g_courses = [
                (c.code, c.name, c.sessions_per_week, c.duration_slots,
                 c.sessions_per_week * c.duration_slots)
                for c in courses
                if c.student_group_id == gid
            ]
            g_courses.sort(key=lambda x: -x[4])
            for code, name, spw, ds, units in g_courses[:15]:
                self.stdout.write(f"    - {code} {name}: {spw}sess x {ds}slots = {units} units")
            if len(g_courses) > 15:
                self.stdout.write(f"    ...and {len(g_courses)-15} more")
        self.stdout.write("")

        # ── 3. Campus capacity overload ─────────────────────────────────────
        rooms_by_campus = {}
        for r in rooms:
            rooms_by_campus.setdefault(r.campus_id, []).append(r)
        campus_demand = {}
        campus_obj    = {}
        for c in courses:
            campus = c.program.department.faculty.campus
            campus_obj[campus.id] = campus
            campus_demand[campus.id] = (
                campus_demand.get(campus.id, 0) + c.duration_slots * c.sessions_per_week
            )

        self.stdout.write("=== CAMPUS CAPACITY ERRORS (blocking) ===")
        campus_errors = []
        for cid, demand in campus_demand.items():
            cr = rooms_by_campus.get(cid, [])
            if not cr:
                continue
            supply = len(cr) * total_ts
            if demand > supply:
                campus_errors.append((campus_obj[cid].name, demand, len(cr), supply))
        if not campus_errors:
            self.stdout.write("  (none)")
        for name, demand, nrooms, supply in campus_errors:
            self.stdout.write(
                f"  {name}: needs {demand}, has {nrooms} rooms x {total_ts} ts = {supply}  "
                f"(excess: +{demand - supply})"
            )
        self.stdout.write("")

        # ── 4. Bug note: multiplier should be 0.5, not 1.5 ─────────────────
        self.stdout.write("=== NOTE: MULTIPLIER BUG CHECK ===")
        self.stdout.write("validation.py line 124 uses x1.5 — if slots are 30-min units, correct multiplier is x0.5")
        over_lec_correct = []
        for lid, lec in lecturer_map.items():
            hours_correct = sum(
                c.duration_slots * c.sessions_per_week * 0.5
                for c in courses if c.lecturer_id == lid
            )
            if hours_correct > lec.max_hours_per_week * TOLERANCE:
                over_lec_correct.append((lec.name, hours_correct, lec.max_hours_per_week))
        self.stdout.write(f"  Over-allocated with x0.5 multiplier: {len(over_lec_correct)} lecturers")
        for name, h, mx in over_lec_correct:
            self.stdout.write(f"    {name}: {h}h / {mx}h max")
        self.stdout.write("")

        self.stdout.write(f"=== TOTAL BLOCKING ERRORS: {len(over_lec) + len(over_groups) + len(campus_errors)} ===")
