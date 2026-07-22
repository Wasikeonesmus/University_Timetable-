from django.core.management.base import BaseCommand
from django.db.models import Q
from scheduler.models import Room

class Command(BaseCommand):
    help = "Backfill is_virtual=True for all ZOOM/Online/Virtual rooms"

    def handle(self, *args, **options):
        updated = Room.objects.filter(
            Q(name__icontains='zoom') |
            Q(name__icontains='online') |
            Q(name__icontains='virtual') |
            Q(name__icontains='teams') |
            Q(name__icontains='remote') |
            Q(name__icontains='webex')
        ).update(is_virtual=True)
        self.stdout.write(self.style.SUCCESS(f"Successfully backfilled {updated} virtual rooms to is_virtual=True!"))
