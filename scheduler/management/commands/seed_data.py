import datetime
from django.core.management.base import BaseCommand
from django.db import transaction
from scheduler.models import (
    University, Campus, Faculty, Department, Program, Semester, 
    Course, Lecturer, StudentGroup, Room, TimeSlot, Constraint, Timetable
)

class Command(BaseCommand):
    help = "Populate the database with mock multi-university data for testing."

    def handle(self, *args, **options):
        self.stdout.write("Seeding university database...")
        
        with transaction.atomic():
            # Clean up old data
            self.stdout.write("Clearing existing data...")
            TimeSlot.objects.all().delete()
            University.objects.all().delete()

            # ==========================================
            # SEED TENANT 1: Antigravity State University
            # ==========================================
            self.stdout.write("Seeding Antigravity State University (ASU)...")
            uni_asu = University.objects.create(name="Antigravity State University", code="ASU")
            campus_asu = Campus.objects.create(university=uni_asu, name="Main Campus")
            faculty_asu = Faculty.objects.create(campus=campus_asu, name="Engineering & Applied Sciences")
            dept_asu = Department.objects.create(faculty=faculty_asu, name="Computer Science")
            program_asu = Program.objects.create(department=dept_asu, name="Bachelor of Science in Computer Science")
            
            semester_asu = Semester.objects.create(
                university=uni_asu,
                name="Semester 1 2026",
                start_date=datetime.date(2026, 9, 1),
                end_date=datetime.date(2026, 12, 20),
                is_active=True
            )

            # Rooms
            room_asu1 = Room.objects.create(campus=campus_asu, name="Room 101 (Lecture)", capacity=100, room_type="Lecture")
            room_asu2 = Room.objects.create(campus=campus_asu, name="Room 102 (Lab)", capacity=30, room_type="Lab")
            room_asu3 = Room.objects.create(campus=campus_asu, name="Room 103 (Lecture)", capacity=60, room_type="Lecture")
            room_asu4 = Room.objects.create(campus=campus_asu, name="Room 104 (Seminar)", capacity=20, room_type="Seminar")

            # Lecturers
            prof_turing = Lecturer.objects.create(department=dept_asu, name="Prof. Alan Turing", email="turing@asu.edu", max_hours_per_week=20)
            dr_lovelace = Lecturer.objects.create(department=dept_asu, name="Dr. Ada Lovelace", email="lovelace@asu.edu", max_hours_per_week=20)
            dr_dijkstra = Lecturer.objects.create(department=dept_asu, name="Dr. Edsger Dijkstra", email="dijkstra@asu.edu", max_hours_per_week=20)
            prof_hopper = Lecturer.objects.create(department=dept_asu, name="Prof. Grace Hopper", email="hopper@asu.edu", max_hours_per_week=20)

            # Student Groups
            group_y1 = StudentGroup.objects.create(program=program_asu, name="CS Year 1", size=75)
            group_y2 = StudentGroup.objects.create(program=program_asu, name="CS Year 2", size=45)
            group_y3 = StudentGroup.objects.create(program=program_asu, name="CS Year 3", size=18)

            # Time Slots (Monday & Tuesday, 5 slots each)
            asu_timeslots = []
            days_asu = [(1, "Monday"), (2, "Tuesday")]
            slots_asu = [
                (1, datetime.time(8, 30), datetime.time(10, 0), False),
                (2, datetime.time(10, 15), datetime.time(11, 45), False),
                (3, datetime.time(12, 0), datetime.time(13, 30), False),
                (4, datetime.time(13, 45), datetime.time(15, 15), False),
                (5, datetime.time(15, 30), datetime.time(17, 0), True)
            ]
            for d_num, d_name in days_asu:
                for s_num, start, end, is_eve in slots_asu:
                    ts, _ = TimeSlot.objects.get_or_create(
                        university=uni_asu, day_of_week=d_num, slot_number=s_num,
                        defaults={'start_time': start, 'end_time': end, 'is_evening': is_eve}
                    )
                    asu_timeslots.append(ts)

            # Courses
            c1 = Course.objects.create(program=program_asu, code="CS101", name="Intro to Computer Science", duration_slots=2, required_room_type="Lecture", lecturer=prof_turing, student_group=group_y1)
            c2 = Course.objects.create(program=program_asu, code="CS102", name="Computer Science Lab I", duration_slots=1, required_room_type="Lab", lecturer=dr_lovelace, student_group=group_y1)
            c3 = Course.objects.create(program=program_asu, code="CS201", name="Data Structures & Algorithms", duration_slots=2, required_room_type="Lecture", lecturer=dr_dijkstra, student_group=group_y2)
            c4 = Course.objects.create(program=program_asu, code="CS202", name="Object Oriented Programming", duration_slots=1, required_room_type="Lecture", lecturer=prof_hopper, student_group=group_y2)
            c5 = Course.objects.create(program=program_asu, code="CS301", name="Software Engineering", duration_slots=2, required_room_type="Seminar", lecturer=prof_turing, student_group=group_y3)
            c6 = Course.objects.create(program=program_asu, code="CS302", name="Distributed Systems", duration_slots=2, required_room_type="Lecture", lecturer=dr_dijkstra, student_group=group_y3)

            # Constraints Configs
            Constraint.objects.create(
                university=uni_asu,
                name="Prof. Turing Unavailable Monday Slot 1",
                constraint_type="LECTURER_AVAILABILITY",
                is_hard=True,
                parameters={"lecturer_id": prof_turing.id, "unavailable_slots": [asu_timeslots[0].id]}
            )
            Constraint.objects.create(
                university=uni_asu,
                name="CS301 Room 104 Preference",
                constraint_type="ROOM_PREFERENCE",
                is_hard=False,
                weight=25,
                parameters={"course_id": c5.id, "preferred_rooms": [room_asu4.id]}
            )

            # Timetable
            Timetable.objects.create(semester=semester_asu, name="Initial Schedule Draft V1", is_active=True)


            # ==========================================
            # SEED TENANT 2: Kenyatta University (KU)
            # ==========================================
            self.stdout.write("Seeding Kenyatta University (KU)...")
            uni_ku = University.objects.create(name="Kenyatta University", code="KU")
            campus_ku = Campus.objects.create(university=uni_ku, name="Main Campus (KU)")
            faculty_ku = Faculty.objects.create(campus=campus_ku, name="School of Engineering")
            dept_ku = Department.objects.create(faculty=faculty_ku, name="Electrical Engineering")
            program_ku = Program.objects.create(department=dept_ku, name="B.Sc. Electrical Engineering")
            
            semester_ku = Semester.objects.create(
                university=uni_ku,
                name="2026-1",
                start_date=datetime.date(2026, 9, 1),
                end_date=datetime.date(2026, 12, 15),
                is_active=True
            )

            # Rooms
            room_ku1 = Room.objects.create(campus=campus_ku, name="Room A101 (KU)", capacity=80, room_type="Lecture")
            room_ku2 = Room.objects.create(campus=campus_ku, name="Room A102 (KU)", capacity=40, room_type="Lab")

            # Lecturers
            dr_smith = Lecturer.objects.create(department=dept_ku, name="Dr. John Smith", email="smith@ku.ac.ke", max_hours_per_week=16)
            prof_jones = Lecturer.objects.create(department=dept_ku, name="Prof. Emily Jones", email="jones@ku.ac.ke", max_hours_per_week=16)

            # Student Groups
            group_ee1 = StudentGroup.objects.create(program=program_ku, name="EE Year 1", size=50)
            group_ee2 = StudentGroup.objects.create(program=program_ku, name="EE Year 2", size=30)

            # Time Slots (Monday & Tuesday, 4 slots each)
            days_ku = [(1, "Monday"), (2, "Tuesday")]
            slots_ku = [
                (1, datetime.time(8, 0), datetime.time(9, 30), False),
                (2, datetime.time(9, 45), datetime.time(11, 15), False),
                (3, datetime.time(11, 30), datetime.time(13, 0), False),
                (4, datetime.time(13, 15), datetime.time(14, 45), True)
            ]
            for d_num, d_name in days_ku:
                for s_num, start, end, is_eve in slots_ku:
                    TimeSlot.objects.get_or_create(
                        university=uni_ku, day_of_week=d_num, slot_number=s_num,
                        defaults={'start_time': start, 'end_time': end, 'is_evening': is_eve}
                    )

            # Courses
            Course.objects.create(program=program_ku, code="EE101", name="Circuit Theory", duration_slots=1, required_room_type="Lecture", lecturer=dr_smith, student_group=group_ee1)
            Course.objects.create(program=program_ku, code="EE102", name="Electronics Laboratory", duration_slots=2, required_room_type="Lab", lecturer=prof_jones, student_group=group_ee2)
            Course.objects.create(program=program_ku, code="EE201", name="Electromagnetics", duration_slots=1, required_room_type="Lecture", lecturer=dr_smith, student_group=group_ee2)

            # Timetable
            Timetable.objects.create(semester=semester_ku, name="Kenyatta University Schedule V1", is_active=True)

        self.stdout.write(self.style.SUCCESS("Database seeding with multi-university data completed successfully!"))
