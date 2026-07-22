import logging
from django.core.mail import send_mail
from django.conf import settings
from .models import Notification
from django.contrib.auth.models import User

logger = logging.getLogger(__name__)

def create_notification(user, title, message, link=None, level='info', channels=None):
    """
    Creates an in-app notification and optionally sends an email.
    FIX BUG 13: Changed channels default from mutable list [] to None to avoid the classic
    Python mutable-default-argument bug where all callers share the same list object.
    """
    # Initialise default channels inside the function so each call gets a fresh list
    if channels is None:
        channels = ['in_app', 'email']
    notifications = []
    
    # 1. In-App Notification
    if 'in_app' in channels:
        try:
            notification = Notification.objects.create(
                user=user,
                title=title,
                message=message,
                link=link,
                level=level
            )
            notifications.append(notification)
        except Exception as e:
            logger.error(f"Failed to create in-app notification for {user.username}: {e}")
            
    # 2. Email Notification
    if 'email' in channels and user.email:
        try:
            subject = f"[Timetable System] {title}"
            send_mail(
                subject=subject,
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                fail_silently=True
            )
        except Exception as e:
            logger.error(f"Failed to send email notification to {user.email}: {e}")
            
    # 3. SMS & Push notifications stub
    if 'sms' in channels:
        logger.info(f"[SMS MOCK] To {user.username}: {title} - {message}")
    if 'push' in channels:
        logger.info(f"[PUSH MOCK] To {user.username}: {title} - {message}")

    return notifications

def notify_university_managers(university, title, message, link=None, level='info'):
    """
    Helper to notify all administrators and managers (schedulers, timetable officers, registrars)
    belonging to a specific university.
    """
    from accounts.models import UserProfile
    from django.db.models import Q
    
    # Get all users with manager roles for this university
    manager_profiles = UserProfile.objects.filter(
        Q(university=university) & 
        Q(role__in=['admin', 'institution_admin', 'registrar', 'timetable_officer', 'scheduler'])
    ).select_related('user')
    
    notifications = []
    for profile in manager_profiles:
        n = create_notification(profile.user, title, message, link=link, level=level)
        if n:
            notifications.extend(n)
            
    # Also notify superusers
    superusers = User.objects.filter(is_superuser=True)
    for su in superusers:
        if su not in [p.user for p in manager_profiles]:
            n = create_notification(su, title, message, link=link, level=level)
            if n:
                notifications.extend(n)
                
    return notifications


def notify_university_managers_async(university, title, message, link=None, level='info'):
    """
    Non-blocking wrapper around notify_university_managers().

    Runs the full email + in-app notification fan-out on a daemon thread so
    SMTP latency (or an unreachable mail server) can never stall the caller.
    """
    import threading

    def _do():
        try:
            notify_university_managers(university, title, message, link=link, level=level)
        except Exception as e:
            logger.error(f"[notify_async] Unhandled error in background notification thread: {e}")

    t = threading.Thread(target=_do, daemon=True)
    t.start()
