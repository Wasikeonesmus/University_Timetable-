from django.core.management.base import BaseCommand
from scheduler.models import University, TimeSlot
import datetime

class Command(BaseCommand):
    help = "Ensures all universities have time slots provisioned for the full week (Monday through Sunday, days 1–7)."

    def handle(self, *args, **options):
        universities = University.objects.all()
        if not universities.exists():
            self.stdout.write(self.style.WARNING("No universities found in database."))
            return

        total_created = 0

        for uni in universities:
            existing_slots = TimeSlot.objects.filter(university=uni)
            existing_days = set(existing_slots.values_list('day_of_week', flat=True))

            # Sample slot patterns from day 1 (or any existing day)
            day1_slots = existing_slots.filter(day_of_week=1).order_by('slot_number')

            if not day1_slots.exists() and existing_slots.exists():
                # Fallback to whatever day exists first
                first_day = min(existing_days)
                day1_slots = existing_slots.filter(day_of_week=first_day).order_by('slot_number')

            if not day1_slots.exists():
                # Default 5-slot structure if university has no slots at all
                slot_templates = [
                    (1, datetime.time(8, 30), datetime.time(10, 0), False),
                    (2, datetime.time(10, 15), datetime.time(11, 45), False),
                    (3, datetime.time(12, 0), datetime.time(13, 30), False),
                    (4, datetime.time(13, 45), datetime.time(15, 15), False),
                    (5, datetime.time(15, 30), datetime.time(17, 0), True),
                ]
            else:
                slot_templates = [
                    (ts.slot_number, ts.start_time, ts.end_time, ts.is_evening)
                    for ts in day1_slots
                ]

            uni_created_count = 0
            for day in range(1, 8):
                for slot_num, start_t, end_t, is_eve in slot_templates:
                    _, created = TimeSlot.objects.get_or_create(
                        university=uni,
                        day_of_week=day,
                        slot_number=slot_num,
                        defaults={
                            'start_time': start_t,
                            'end_time': end_t,
                            'is_evening': is_eve,
                        }
                    )
                    if created:
                        uni_created_count += 1

            total_created += uni_created_count
            self.stdout.write(self.style.SUCCESS(
                f"University '{uni.name}': added {uni_created_count} new time slot(s). "
                f"Total slots now: {TimeSlot.objects.filter(university=uni).count()}"
            ))

        self.stdout.write(self.style.SUCCESS(f"Done! Created a total of {total_created} full-week time slots across all universities."))
