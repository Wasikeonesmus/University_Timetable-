from django.core.management.base import BaseCommand
from django.core.mail import send_mail
from django.conf import settings
import sys

class Command(BaseCommand):
    help = "Tests SMTP email settings and prints detailed diagnostic logs."

    def add_arguments(self, parser):
        parser.add_argument('--to', type=str, default='wasikeonesmus980@gmail.com', help='Recipient email address')

    def handle(self, *args, **options):
        to_email = options['to']
        self.stdout.write("=" * 60)
        self.stdout.write("UNISCHEDULE SMTP EMAIL DIAGNOSTIC TEST")
        self.stdout.write("=" * 60)
        self.stdout.write(f"  Backend  : {settings.EMAIL_BACKEND}")
        self.stdout.write(f"  Host     : {settings.EMAIL_HOST}:{settings.EMAIL_PORT}")
        self.stdout.write(f"  TLS      : {settings.EMAIL_USE_TLS}")
        self.stdout.write(f"  SSL      : {settings.EMAIL_USE_SSL}")
        self.stdout.write(f"  User     : {settings.EMAIL_HOST_USER}")
        self.stdout.write(f"  From     : {settings.DEFAULT_FROM_EMAIL}")
        self.stdout.write(f"  Recipient: {to_email}")
        self.stdout.write("-" * 60)

        try:
            sent = send_mail(
                subject="[UniSchedule Diagnostic] SMTP Verification Test",
                message="Hello!\n\nThis is a test email sent from your UniSchedule server to confirm SMTP configuration.",
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[to_email],
                fail_silently=False
            )
            self.stdout.write(self.style.SUCCESS(f"✓ SUCCESS: Email sent to {to_email} (sent count: {sent})"))
        except Exception as err:
            self.stderr.write(self.style.ERROR(f"✗ ERROR: Failed to send email via SMTP: {type(err).__name__}: {err}"))
            import traceback
            traceback.print_exc()
