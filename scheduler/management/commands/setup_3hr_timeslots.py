from django.core.management.base import BaseCommand
from scheduler.models import University, TimeSlot
import datetime

class Command(BaseCommand):
    help = "Sets up standard 3-hour time slots (Mon–Sun, 4 slots/day) for all universities."

    def handle(self, *args, **options):
        universities = University.objects.all()
        if not universities.exists():
            self.stdout.write(self.style.WARNING("No universities found in database."))
            return

        total_created = 0

        slot_templates = [
            (1, datetime.time(8, 0),  datetime.time(11, 0), False),
            (2, datetime.time(11, 0), datetime.time(14, 0), False),
            (3, datetime.time(14, 0), datetime.time(17, 0), False),
            (4, datetime.time(17, 30), datetime.time(20, 30), True),
        ]

        for uni in universities:
            # Delete existing slots for clean setup
            TimeSlot.objects.filter(university=uni).delete()

            slots = [
                TimeSlot(
                    university=uni,
                    day_of_week=day,
                    slot_number=slot_num,
                    start_time=start_t,
                    end_time=end_t,
                    is_evening=is_eve,
                )
                for day in range(1, 8)
                for slot_num, start_t, end_t, is_eve in slot_templates
            ]

            TimeSlot.objects.bulk_create(slots)
            total_created += len(slots)

            self.stdout.write(self.style.SUCCESS(
                f"University '{uni.name}': provisioned 28 standard 3-hour time slots (Mon–Sun)."
            ))

        self.stdout.write(self.style.SUCCESS(f"Done! Created a total of {total_created} 3-hour time slots across all universities."))
