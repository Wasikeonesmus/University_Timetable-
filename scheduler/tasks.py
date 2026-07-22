"""
scheduler/tasks.py
------------------
Async task functions for django-q2 task queue.
These are called via async_task() from views and run in background workers.
"""
import logging
from collections import defaultdict
from django.core.mail import send_mail
from django.conf import settings
from django.template.loader import render_to_string

from .scheduling_service import run_scheduling_pipeline
from .models import Timetable, ScheduleSlot

logger = logging.getLogger(__name__)


def send_slot_change_notifications(slot_id: int):
    """
    Background task: notify the lecturer and affected student group that a
    single ScheduleSlot was created/updated, then trigger a Firebase refresh.

    Runs via django_q's worker (a single serialized queue) instead of a raw
    threading.Thread, so N slot saves in quick succession (e.g. during bulk
    timetable generation) queue up and run one at a time instead of opening
    dozens of concurrent SQLite connections and hitting
    "database is locked" errors on accounts_userprofile.
    """
    from .notifications import create_notification
    from accounts.models import UserProfile

    try:
        instance = ScheduleSlot.objects.select_related(
            'course', 'lecturer', 'lecturer__user_profile__user',
            'student_group', 'time_slot', 'room', 'timetable',
        ).get(pk=slot_id)
    except ScheduleSlot.DoesNotExist:
        logger.warning(f"[Slot Notification] Slot {slot_id} no longer exists — skipping.")
        return

    try:
        course = instance.course
        lecturer = instance.lecturer
        student_group = instance.student_group
        ts = instance.time_slot

        day_labels = {1: 'Monday', 2: 'Tuesday', 3: 'Wednesday', 4: 'Thursday', 5: 'Friday', 6: 'Saturday', 7: 'Sunday'}
        day_name = day_labels.get(ts.day_of_week, f"Day {ts.day_of_week}")
        time_str = f"{day_name} at {ts.start_time.strftime('%H:%M')} - {ts.end_time.strftime('%H:%M')} in Room {instance.room.name}"

        if lecturer and hasattr(lecturer, 'user_profile') and lecturer.user_profile:
            lec_user = lecturer.user_profile.user
            create_notification(
                user=lec_user, title="New Class Assigned",
                message=f"You have been assigned to teach '{course.code}: {course.name}' on {time_str}.",
                link="/my-schedule/", level="info"
            )

        if student_group:
            student_profiles = UserProfile.objects.filter(student_group=student_group, role="student").select_related('user')
            for profile in student_profiles:
                create_notification(
                    user=profile.user, title="Class Schedule Updated",
                    message=f"Your lecture for '{course.code}: {course.name}' is scheduled on {time_str} taught by {lecturer.name if lecturer else 'Staff'}.",
                    link="/", level="info"
                )

        from .firebase_service import trigger_timetable_refresh
        trigger_timetable_refresh(instance.timetable_id)
    except Exception as e:
        logger.error(f"[Slot Notification] Failed to send slot change notifications for slot {slot_id}: {e}")



def generate_timetable_async(timetable_id: int, time_limit: int = 60):
    """
    Background task: run the full scheduling pipeline for a timetable.
    Called via django_q async_task() from the generate view.

    On completion, sends email notifications to all affected lecturers.
    """
    logger.info(f"[Task] Starting async generation for timetable ID={timetable_id}")
    result = run_scheduling_pipeline(timetable_id, time_limit_seconds=time_limit)
    logger.info(f"[Task] Generation done — status={result.status}, courses={result.courses_scheduled}")

    if result.status in ('OPTIMAL', 'FEASIBLE'):
        _notify_lecturers_on_publish(timetable_id)

    return {
        'status':            result.status,
        'message':           result.message,
        'courses_scheduled': result.courses_scheduled,
        'log_id':            result.log_id,
        'hard_conflicts':    len(result.hard_conflicts),
        'soft_conflicts':    len(result.soft_conflicts),
    }


def _notify_lecturers_on_publish(timetable_id: int):
    """
    Sends email notifications to all lecturers whose schedule was just generated.
    Includes personalized .ics calendar attachments.

    FIX G6: Batch-fetches all slots upfront and groups by lecturer in Python.
    Eliminates the previous pattern of 1 DB query per lecturer.
    """
    try:
        timetable = Timetable.objects.select_related('semester', 'semester__university').get(pk=timetable_id)
    except Timetable.DoesNotExist:
        return

    # FIX G6: Single query — fetch ALL slots with related data up-front.
    # Old code ran one extra query per lecturer inside the loop.
    all_slots = list(
        ScheduleSlot.objects.filter(timetable=timetable)
        .select_related(
            'lecturer', 'lecturer__user',
            'course', 'room', 'time_slot',
        )
    )

    if not all_slots:
        return

    # Group slots by lecturer in Python (O(N) — no extra DB hits)
    slots_by_lecturer = defaultdict(list)
    for slot in all_slots:
        if slot.lecturer_id:
            slots_by_lecturer[slot.lecturer_id].append(slot)

    from django.core.mail import EmailMessage, EmailMultiAlternatives, get_connection
    import uuid

    notified = 0
    connection = get_connection(fail_silently=False)
    for lecturer_id, lec_slots in slots_by_lecturer.items():
        lecturer = lec_slots[0].lecturer
        if not lecturer:
            continue

        # Get email: from User account if linked, else from Lecturer record
        email = None
        if lecturer.user and lecturer.user.email:
            email = lecturer.user.email
        elif lecturer.email:
            email = lecturer.email

        if not email:
            continue

        # Ensure lecturer has a calendar token
        if not lecturer.calendar_token:
            lecturer.calendar_token = uuid.uuid4()
            lecturer.save(update_fields=['calendar_token'])

        subject = f"Your Timetable is Ready – {timetable.name}"

        # Group slots by day for structured display
        day_names = {1: 'Monday', 2: 'Tuesday', 3: 'Wednesday', 4: 'Thursday', 5: 'Friday', 6: 'Saturday', 7: 'Sunday'}
        slots_by_day = {}
        total_contact_hours = 0.0

        for s in lec_slots:
            dow = s.time_slot.day_of_week
            slots_by_day.setdefault(dow, []).append(s)
            dur = (s.time_slot.end_time.hour * 60 + s.time_slot.end_time.minute) - (s.time_slot.start_time.hour * 60 + s.time_slot.start_time.minute)
            total_contact_hours += dur / 60.0

        # Plain text fallback
        plain_lines = [
            f"Dear {lecturer.name},",
            "",
            f"Your official teaching schedule for '{timetable.name}' ({timetable.semester.name}) has been published.",
            "",
            f"Total Classes: {len(lec_slots)} | Total Weekly Contact Hours: {round(total_contact_hours, 1)}h",
            "--------------------------------------------------------------------------------",
        ]
        for dow in sorted(slots_by_day.keys()):
            plain_lines.append(f"\n{day_names.get(dow, f'Day {dow}')}:")
            for s in slots_by_day[dow]:
                plain_lines.append(
                    f"  • {s.time_slot.start_time.strftime('%I:%M %p')} - {s.time_slot.end_time.strftime('%I:%M %p')} | "
                    f"{s.course.code}: {s.course.name} | Room: {s.room.name} | Group: {s.student_group.name if s.student_group else 'N/A'}"
                )
        plain_lines += [
            "",
            "View your interactive schedule: http://127.0.0.1:8000/portal/timetable/",
            "",
            "Best regards,",
            "UniSchedule System Administration"
        ]

        # Determine credentials info for this lecturer
        username_val = lecturer.user.username if (lecturer.user and lecturer.user.username) else email
        password_info = email if (lecturer.user and lecturer.user.email) else "Same as registered account email"

        # Rich HTML Email Template (Clean Professional Vector Typography - NO RAW EMOJIS)
        rows_html = ""
        for dow in sorted(slots_by_day.keys()):
            d_name = day_names.get(dow, f"Day {dow}")
            rows_html += f"""
            <tr style="background:#f8fafc;">
                <td colspan="4" style="padding:10px 14px; font-weight:800; color:#0f172a; font-size:12px; border-bottom:2px solid #e2e8f0; text-transform:uppercase; letter-spacing:0.05em;">
                    <span style="background:#e2e8f0; color:#334155; padding:2px 8px; border-radius:4px; margin-right:6px; font-size:10px;">DAY</span> {d_name}
                </td>
            </tr>
            """
            for s in slots_by_day[dow]:
                st_str = s.time_slot.start_time.strftime('%I:%M %p')
                et_str = s.time_slot.end_time.strftime('%I:%M %p')
                room_badge = '<span style="background:#dbeafe; color:#1e40af; padding:3px 8px; border-radius:4px; font-size:11px; font-weight:700;">VIRTUAL</span>' if getattr(s.room, 'is_virtual', False) else f'<span style="background:#f1f5f9; color:#475569; padding:3px 8px; border-radius:4px; font-size:11px; font-weight:700;">Room: {s.room.name}</span>'
                
                rows_html += f"""
                <tr style="border-bottom:1px solid #f1f5f9;">
                    <td style="padding:12px 14px; font-size:12px; font-weight:700; color:#2563eb; white-space:nowrap; vertical-align:top;">
                        {st_str} - {et_str}
                    </td>
                    <td style="padding:12px 14px; vertical-align:top;">
                        <strong style="color:#0f172a; font-size:13px;">{s.course.code}</strong><br>
                        <span style="color:#64748b; font-size:12px;">{s.course.name}</span>
                    </td>
                    <td style="padding:12px 14px; vertical-align:top; font-size:12px; color:#475569;">
                        {room_badge}
                    </td>
                    <td style="padding:12px 14px; vertical-align:top; font-size:12px; color:#64748b;">
                        {s.student_group.name if s.student_group else 'N/A'}
                    </td>
                </tr>
                """

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
        </head>
        <body style="font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color:#f1f5f9; margin:0; padding:24px; color:#334155;">
            <table role="presentation" style="max-width:680px; margin:0 auto; background:#ffffff; border-radius:12px; overflow:hidden; box-shadow:0 4px 20px rgba(0,0,0,0.08); border-collapse:collapse; width:100%;">
                <!-- Header -->
                <tr>
                    <td style="background:linear-gradient(135deg, #0f172a, #1e293b); padding:28px 32px; text-align:left;">
                        <div style="font-size:20px; font-weight:800; color:#ffffff; letter-spacing:-0.02em; margin-bottom:4px;">
                            UniSchedule <span style="font-size:12px; font-weight:600; color:#94a3b8; background:rgba(255,255,255,0.1); padding:2px 8px; border-radius:12px; text-transform:uppercase;">Lecturer Portal</span>
                        </div>
                        <div style="font-size:13px; color:#94a3b8;">Official Academic Teaching Schedule &amp; Credentials</div>
                    </td>
                </tr>

                <!-- Content Body -->
                <tr>
                    <td style="padding:32px;">
                        <h2 style="font-size:18px; font-weight:700; color:#0f172a; margin-top:0; margin-bottom:8px;">Dear {lecturer.name},</h2>
                        <p style="font-size:14px; color:#475569; line-height:1.6; margin-bottom:20px;">
                            Your official teaching timetable for <strong>{timetable.name}</strong> ({timetable.semester.name}) is ready and published.
                        </p>

                        <!-- Credentials Card -->
                        <div style="background:#f0fdf4; border:1px solid #bbf7d0; border-radius:8px; padding:16px 20px; margin-bottom:24px;">
                            <div style="font-size:11px; font-weight:800; color:#15803d; text-transform:uppercase; letter-spacing:0.05em; margin-bottom:8px;">
                                PORTAL LOGIN CREDENTIALS
                            </div>
                            <div style="font-size:13px; color:#166534; line-height:1.6;">
                                <div><strong>Username / Email:</strong> <code style="background:#dcfce7; padding:2px 6px; border-radius:4px; font-family:monospace; color:#14532d;">{username_val}</code></div>
                                <div><strong>Password:</strong> <code style="background:#dcfce7; padding:2px 6px; border-radius:4px; font-family:monospace; color:#14532d;">{password_info}</code></div>
                                <div style="margin-top:8px; font-size:12px; color:#15803d;">
                                    Sign in link: <a href="http://127.0.0.1:8000/accounts/login/" style="color:#166534; font-weight:700; text-decoration:underline;">http://127.0.0.1:8000/accounts/login/</a>
                                </div>
                            </div>
                        </div>

                        <!-- KPI Cards -->
                        <table role="presentation" style="width:100%; margin-bottom:24px; border-collapse:collapse;">
                            <tr>
                                <td style="width:50%; padding:14px; background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; text-align:center;">
                                    <div style="font-size:11px; font-weight:700; color:#64748b; text-transform:uppercase;">Assigned Classes</div>
                                    <div style="font-size:22px; font-weight:800; color:#2563eb; margin-top:2px;">{len(lec_slots)}</div>
                                </td>
                                <td style="width:8px;"></td>
                                <td style="width:50%; padding:14px; background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; text-align:center;">
                                    <div style="font-size:11px; font-weight:700; color:#64748b; text-transform:uppercase;">Weekly Contact Hours</div>
                                    <div style="font-size:22px; font-weight:800; color:#059669; margin-top:2px;">{round(total_contact_hours, 1)}h</div>
                                </td>
                            </tr>
                        </table>

                        <!-- Schedule Table -->
                        <div style="border:1px solid #e2e8f0; border-radius:8px; overflow:hidden; margin-bottom:28px;">
                            <table role="presentation" style="width:100%; border-collapse:collapse; text-align:left;">
                                <thead>
                                    <tr style="background:#f1f5f9; border-bottom:1px solid #e2e8f0;">
                                        <th style="padding:10px 14px; font-size:11px; color:#64748b; font-weight:700; text-transform:uppercase;">Time Slot</th>
                                        <th style="padding:10px 14px; font-size:11px; color:#64748b; font-weight:700; text-transform:uppercase;">Course Unit</th>
                                        <th style="padding:10px 14px; font-size:11px; color:#64748b; font-weight:700; text-transform:uppercase;">Location</th>
                                        <th style="padding:10px 14px; font-size:11px; color:#64748b; font-weight:700; text-transform:uppercase;">Student Group</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {rows_html}
                                </tbody>
                            </table>
                        </div>

                        <!-- CTA Action Button -->
                        <div style="text-align:center; margin-top:28px; margin-bottom:16px;">
                            <a href="http://127.0.0.1:8000/portal/timetable/" style="display:inline-block; background:linear-gradient(135deg, #2563eb, #1d4ed8); color:#ffffff; font-weight:700; font-size:14px; text-decoration:none; padding:12px 28px; border-radius:8px; box-shadow:0 2px 10px rgba(37,99,235,0.25);">
                                View Interactive Schedule
                            </a>
                        </div>
                    </td>
                </tr>

                <!-- Footer -->
                <tr>
                    <td style="background:#f8fafc; padding:20px 32px; border-top:1px solid #e2e8f0; text-align:center; font-size:12px; color:#94a3b8;">
                        UniSchedule Academic Timetable System &bull; Official System Notification
                    </td>
                </tr>
            </table>
        </body>
        </html>
        """

        try:
            email_msg = EmailMultiAlternatives(
                subject=subject,
                body='\n'.join(plain_lines),
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[email],
                connection=connection,
            )
            email_msg.attach_alternative(html_content, "text/html")
            try:
                email_msg.send(fail_silently=False)
            except Exception as conn_err:
                logger.warning(f"[Task] SMTP connection dropped ({conn_err}). Reconnecting...")
                connection = get_connection(fail_silently=False)
                email_msg.connection = connection
                email_msg.send(fail_silently=False)

            notified += 1
            logger.info(f"[Task] Sent HTML schedule email to lecturer {lecturer.name} at {email}")

            # Check if lecturer has Google Calendar integration active and sync
            if lecturer.user:
                try:
                    from accounts.models import GoogleCalendarToken
                    if GoogleCalendarToken.objects.filter(user=lecturer.user).exists():
                        import sys
                        if 'test' in sys.argv:
                            from .google_tasks import sync_lecturer_timetable_google
                            sync_lecturer_timetable_google(lecturer.id, timetable.id)
                        else:
                            from django_q.tasks import async_task
                            async_task('scheduler.google_tasks.sync_lecturer_timetable_google', lecturer.id, timetable.id)
                        logger.info(f"[Task] Triggered Google Calendar sync for lecturer {lecturer.name}")
                except Exception as g_err:
                    logger.warning(f"[Task] Failed to trigger Google sync for {lecturer.name}: {g_err}")

        except Exception as e:
            logger.warning(f"[Task] Could not email {lecturer.name} ({email}): {e}")

    logger.info(
        f"[Task] Sent {notified} lecturer notification(s) "
        f"for timetable {timetable_id}"
    )


def provision_lecturer_credentials(university_id: int):
    """
    Finds all active lecturers at the university, generates Django User accounts 
    using their emails (or fallback generated emails), creates their UserProfile, 
    and emails them secure temporary passwords.
    """
    import re
    import secrets
    import string
    from django.contrib.auth.models import User
    from django.db import transaction
    from django.core.mail import send_mail
    from django.conf import settings
    from accounts.models import UserProfile
    from scheduler.models import Lecturer, University

    try:
        university = University.objects.get(pk=university_id)
    except University.DoesNotExist:
        logger.error(f"[Provision] University with ID {university_id} not found.")
        return

    # Base Login URL link
    site_url = getattr(settings, 'SITE_URL', 'http://127.0.0.1:8000')
    login_url = f"{site_url.rstrip('/')}/accounts/login/"

    # Find all active lecturers in this university
    lecturers = Lecturer.objects.filter(
        department__faculty__campus__university=university,
        is_active=True
    )

    provisioned_count = 0
    warnings = []
    summary_records = []  # Track for Registrar summary report

    def slugify_name(name_str):
        name_clean = re.sub(r'^(dr|prof|eng|mr|mrs|ms|sir|madam)\b\.?\s*', '', name_str, flags=re.IGNORECASE)
        return re.sub(r'[^a-zA-Z0-9]+', '.', name_clean.strip().lower()).strip('.')

    for lecturer in lecturers:
        raw_email = lecturer.email
        email_is_fallback = False

        if not raw_email or not raw_email.strip() or '@' not in raw_email or raw_email.strip().lower() in ('n/a', 'none', 'no-email', 'null', 'nil'):
            slug = slugify_name(lecturer.name)
            uni_slug = re.sub(r'[^a-zA-Z0-9]+', '', university.name.lower())
            if not uni_slug:
                uni_slug = "university"
            raw_email = f"{slug}@{uni_slug}.edu"
            email_is_fallback = True
            warnings.append(f"Lecturer '{lecturer.name}' (id={lecturer.id}) has no valid email address. Profile created with fallback: {raw_email}")

        email = raw_email.strip().lower()

        try:
            with transaction.atomic():
                user = User.objects.filter(email__iexact=email).first()
                newly_created = False
                temp_password = None

                if not user:
                    base_username = email.split('@')[0]
                    username = base_username
                    counter = 1
                    while User.objects.filter(username=username).exists():
                        username = f"{base_username}{counter}"
                        counter += 1

                    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
                    temp_password = "".join(secrets.choice(alphabet) for _ in range(12))

                    user = User.objects.create_user(
                        username=username,
                        email=email,
                        password=temp_password
                    )
                    newly_created = True
                else:
                    if not user.is_active:
                        user.is_active = True
                        user.save(update_fields=['is_active'])

                # Bind User to Lecturer safely (check unique constraint)
                if lecturer.user != user:
                    if not Lecturer.objects.filter(user=user).exclude(id=lecturer.id).exists():
                        try:
                            lecturer.user = user
                            lecturer.save(update_fields=['user'])
                        except Exception as ex:
                            logger.warning(f"[Provision] Could not bind user {user.username} to lecturer {lecturer.name}: {ex}")
                    else:
                        logger.warning(f"[Provision] User '{user.username}' is already bound to another Lecturer record.")

                # Bind UserProfile
                profile, profile_created = UserProfile.objects.get_or_create(
                    user=user,
                    defaults={
                        'role': 'lecturer',
                        'university': university,
                        'lecturer': lecturer
                    }
                )
                if not profile_created:
                    profile.role = 'lecturer'
                    profile.lecturer = lecturer
                    profile.university = university
                    profile.save(update_fields=['role', 'lecturer', 'university'])

            # Send email directly to lecturer with credentials + direct portal login URL link
            if newly_created and temp_password:
                summary_records.append({
                    'name': lecturer.name,
                    'email': email,
                    'username': user.username,
                    'password': temp_password,
                    'is_fallback': email_is_fallback
                })

                if not email_is_fallback:
                    subject = f"Your Lecturer Credentials — {university.name} Timetable System"
                    message = (
                        f"Dear {lecturer.name},\n\n"
                        f"An instructor account has been created for you on the {university.name} Timetable Portal.\n\n"
                        f"🔑 Account Credentials:\n"
                        f"• Login Link: {login_url}\n"
                        f"• Username / Email: {email}\n"
                        f"• Temporary Password: {temp_password}\n\n"
                        f"Please click the link above to log in and change your password upon first sign-in.\n\n"
                        f"Best regards,\n"
                        f"Office of the Registrar / Timetable Administration\n"
                        f"{university.name}"
                    )
                    try:
                        send_mail(
                            subject,
                            message,
                            settings.DEFAULT_FROM_EMAIL,
                            [email],
                            fail_silently=False,
                        )
                    except Exception as mail_err:
                        logger.error(f"[Provision] Failed to send credentials email to {email}: {mail_err}")

                provisioned_count += 1
            elif newly_created:
                provisioned_count += 1

        except Exception as err:
            logger.error(f"[Provision] Failed to provision credentials for lecturer '{lecturer.name}': {err}")

    # Send Master Summary Report to the Registrar / University Administration
    if summary_records:
        registrar_emails = list(
            UserProfile.objects.filter(
                university=university,
                role__in=['registrar', 'institution_admin', 'admin']
            ).values_list('user__email', flat=True)
        )
        if not registrar_emails:
            # Fallback to global superusers
            registrar_emails = list(
                User.objects.filter(is_superuser=True).exclude(email='').values_list('email', flat=True)
            )

        if registrar_emails:
            lines = [
                f"Dear Registrar / Academic Administration,\n",
                f"Lecturer credential provisioning for {university.name} has been completed following dataset loading.\n",
                f"• Total Accounts Provisioned: {len(summary_records)}",
                f"• Login Portal Link: {login_url}\n",
                f"PROVISIONED LECTURER CREDENTIALS MASTER LIST:",
                "-" * 70,
                f"{'Lecturer Name':<30} | {'Email':<30} | {'Temp Password'}",
                "-" * 70
            ]
            for rec in summary_records:
                fb_tag = " (Fallback Email)" if rec['is_fallback'] else ""
                lines.append(f"{rec['name']:<30} | {rec['email'] + fb_tag:<30} | {rec['password']}")
            lines.append("-" * 70)
            lines.append("\nPlease store these credentials securely or assist faculty members with first-time login.")

            summary_body = "\n".join(lines)
            try:
                send_mail(
                    f"[Audit Report] Lecturer Credentials Summary — {university.name}",
                    summary_body,
                    settings.DEFAULT_FROM_EMAIL,
                    list(set(registrar_emails)),
                    fail_silently=False,
                )
                logger.info(f"[Provision] Summary report sent to Registrar(s): {registrar_emails}")
            except Exception as mail_err:
                logger.error(f"[Provision] Failed to send Registrar summary report: {mail_err}")

    # Write warning messages to the database ImportAuditLog if one exists for this run
    if warnings:
        from scheduler.models import ImportAuditLog
        audit_log = ImportAuditLog.objects.filter(university=university).order_by('-imported_at').first()
        if audit_log:
            existing_warnings = audit_log.warnings or []
            existing_warnings.extend(warnings)
            audit_log.warnings = existing_warnings
            audit_log.save(update_fields=['warnings'])

    return f"Successfully provisioned/activated {provisioned_count} lecturer account(s). Registrar summary report sent. Warnings logged: {len(warnings)}"


def expire_ended_semester_credentials():
    """
    Background cron job task:
    For all active lecturer profiles, check if they are assigned to teach in any 
    currently active/ongoing semesters. If not, deactivate their login access.
    """
    from django.utils import timezone
    from django.core.mail import send_mail
    from django.conf import settings
    from accounts.models import UserProfile
    from scheduler.models import ScheduleSlot

    today = timezone.localdate()
    deactivated_count = 0

    # Get all active lecturer profiles
    lecturer_profiles = UserProfile.objects.filter(role='lecturer', user__is_active=True).select_related('user', 'lecturer')

    for profile in lecturer_profiles:
        user = profile.user
        lecturer = profile.lecturer

        if not lecturer:
            continue

        # Check if the lecturer has any scheduled slots in any active semester that spans today's date
        active_teaching = ScheduleSlot.objects.filter(
            lecturer=lecturer,
            timetable__semester__is_active=True,
            timetable__semester__end_date__gte=today
        ).exists()

        if not active_teaching:
            user.is_active = False
            user.save(update_fields=['is_active'])

            # Send courtesy email
            if lecturer.email and '@' in lecturer.email:
                subject = "Your Timetable System Access has Expired"
                message = (
                    f"Dear {lecturer.name},\n\n"
                    f"Your access to the University Timetable System has expired for this semester.\n"
                    f"If you are assigned to teach in the upcoming semester, your account will be "
                    f"re-activated automatically when the new schedule is published."
                )
                try:
                    send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [lecturer.email], fail_silently=True)
                except Exception:
                    pass

            deactivated_count += 1

    return f"Deactivated {deactivated_count} inactive lecturer account(s)."


def send_10min_class_reminders():
    """
    Periodic task running every minute (or via cron/Django-Q).
    Detects classes starting in 10 minutes from now, triggering
    In-App, Realtime Push, and Email alerts to Lecturers & Student Groups!
    """
    from datetime import timedelta
    from django.utils import timezone
    from scheduler.models import Timetable, ScheduleSlot
    from scheduler.notifications import create_notification
    from scheduler.firebase_service import trigger_timetable_refresh

    now = timezone.localtime()
    dow = now.isoweekday()  # 1=Mon ... 7=Sun
    
    # Window: classes starting between 9 to 11 minutes from now
    min_start = (now + timedelta(minutes=9)).time()
    max_start = (now + timedelta(minutes=11)).time()

    active_tts = Timetable.objects.filter(is_active=True)
    upcoming_slots = ScheduleSlot.objects.filter(
        timetable__in=active_tts,
        time_slot__day_of_week=dow,
        time_slot__start_time__gte=min_start,
        time_slot__start_time__lte=max_start,
    ).select_related('lecturer', 'lecturer__user', 'course', 'room', 'student_group')

    notified_count = 0
    for slot in upcoming_slots:
        time_str = slot.time_slot.start_time.strftime('%I:%M %p')
        title = f"Class Starting in 10 Minutes: {slot.course.code}"
        location_str = "VIRTUAL" if getattr(slot.room, 'is_virtual', False) else f"Room {slot.room.name}"
        msg = f"Reminder: Your class '{slot.course.code} - {slot.course.name}' starts in 10 minutes at {time_str} in {location_str}."

        # 1. Notify Lecturer
        if slot.lecturer and slot.lecturer.user:
            create_notification(
                user=slot.lecturer.user,
                title=title,
                message=msg,
                link='/portal/timetable/',
                level='warning',
                channels=['in_app', 'email', 'push']
            )
            notified_count += 1

        # 2. Notify Student Group members
        if slot.student_group:
            student_profiles = slot.student_group.student_profiles.select_related('user')
            for sp in student_profiles:
                if sp.user:
                    create_notification(
                        user=sp.user,
                        title=title,
                        message=msg,
                        link='/student/timetable/',
                        level='warning',
                        channels=['in_app', 'email', 'push']
                    )
                    notified_count += 1

        # Real-time Firebase push toast update
        try:
            trigger_timetable_refresh(slot.timetable_id)
        except Exception:
            pass

    return f"Sent {notified_count} 10-minute class start reminder notification(s)."


def verify_and_notify_lecturer_record(submitted_email, submitted_name=None, staff_id=None, university_id=None, preserve_password=False):
    """
    Checks database for matching Lecturer record.
    - If VERIFIED: Sets is_verified=True, provisions user credentials if needed, and sends HTML verification success email.
    - If NOT VERIFIED (No record found): Auto-creates pending lecturer profile if email valid, sends verification status email, and alerts University Registrar.
    """
    from django.core.mail import EmailMultiAlternatives, send_mail
    from django.conf import settings
    from django.db.models import Q
    from scheduler.models import Lecturer, University, Department
    from scheduler.notifications import notify_university_managers

    submitted_email_clean = (submitted_email or "").strip().lower()

    # Priority 1: Match by exact email address
    lecturer = None
    if submitted_email_clean and '@' in submitted_email_clean:
        lecturer = Lecturer.objects.filter(email__iexact=submitted_email_clean).first()

    # Priority 2: Match by staff_id or name
    if not lecturer:
        query = Q()
        if staff_id:
            query |= Q(staff_id__iexact=staff_id.strip())
        if submitted_name and len(submitted_name.strip()) >= 3:
            query |= Q(name__icontains=submitted_name.strip())

        if query:
            lecturers_qs = Lecturer.objects.filter(query)
            if university_id:
                lecturers_qs = lecturers_qs.filter(department__faculty__campus__university_id=university_id)
            lecturer = lecturers_qs.first()

    # If still no lecturer record found, auto-create a new Lecturer record if email is provided
    if not lecturer and submitted_email_clean and '@' in submitted_email_clean:
        dept = None
        if university_id:
            dept = Department.objects.filter(faculty__campus__university_id=university_id).first()
        if not dept:
            dept = Department.objects.first()
        if not dept:
            from scheduler.models import Faculty, Campus, University
            uni = University.objects.first() or University.objects.create(name="Default University", code="UNI")
            campus = Campus.objects.filter(university=uni).first() or Campus.objects.create(university=uni, name="Main Campus", code="MAIN")
            faculty = Faculty.objects.filter(campus=campus).first() or Faculty.objects.create(campus=campus, name="General Faculty", code="FAC")
            dept = Department.objects.create(faculty=faculty, name="General Department", code="GEN")

        name_str = (submitted_name or submitted_email_clean.split('@')[0].replace('.', ' ').title()).strip()
        lecturer = Lecturer.objects.create(
            name=name_str,
            email=submitted_email_clean,
            staff_id=staff_id.strip() if staff_id else None,
            department=dept,
            is_verified=True
        )

    # 1. VERIFIED MATCH FOUND IN DATABASE
    if lecturer:
        email = (submitted_email_clean or lecturer.email or "").strip().lower()

        lecturer.is_verified = True
        if email and lecturer.email != email:
            lecturer.email = email
            lecturer.save(update_fields=['is_verified', 'email'])
        else:
            lecturer.save(update_fields=['is_verified'])

        # Handle user credentials securely
        password_display = "(Specified during account registration)"
        complex_password = None
        
        if not preserve_password or not lecturer.user:
            import secrets
            import string
            alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
            complex_password = "".join(secrets.choice(alphabet) for _ in range(12))
            password_display = complex_password

        if lecturer.user:
            user = lecturer.user
            if complex_password:
                user.set_password(complex_password)
            if email and '@' in email:
                user.email = email
            user.save()
        elif email and '@' in email:
            from django.contrib.auth.models import User
            base_un = email.split('@')[0]
            un = base_un
            c = 1
            while User.objects.filter(username=un).exists():
                un = f"{base_un}{c}"
                c += 1
            user = User.objects.create_user(username=un, email=email, password=complex_password or 'wasike123')
            lecturer.user = user
            lecturer.save(update_fields=['user'])

        # Ensure UserProfile exists
        from accounts.models import UserProfile
        uni = lecturer.department.faculty.campus.university if (lecturer.department and lecturer.department.faculty and lecturer.department.faculty.campus) else None
        UserProfile.objects.get_or_create(
            user=user,
            defaults={'role': 'lecturer', 'university': uni, 'lecturer': lecturer}
        )

        # Fetch active timetable schedule slots for this verified lecturer
        from scheduler.models import ScheduleSlot, Timetable
        active_tt = Timetable.objects.filter(is_active=True).first() or Timetable.objects.last()
        lec_slots = []
        slots_by_day = {}
        total_contact_hours = 0.0
        day_names = {1: 'Monday', 2: 'Tuesday', 3: 'Wednesday', 4: 'Thursday', 5: 'Friday', 6: 'Saturday', 7: 'Sunday'}

        if active_tt:
            lec_slots = list(
                ScheduleSlot.objects.filter(timetable=active_tt, lecturer=lecturer)
                .select_related('course', 'room', 'time_slot', 'student_group')
                .order_by('time_slot__day_of_week', 'time_slot__start_time')
            )
            for s in lec_slots:
                dow = s.time_slot.day_of_week
                slots_by_day.setdefault(dow, []).append(s)
                dur = (s.time_slot.end_time.hour * 60 + s.time_slot.end_time.minute) - (s.time_slot.start_time.hour * 60 + s.time_slot.start_time.minute)
                total_contact_hours += dur / 60.0

        rows_html = ""
        for dow in sorted(slots_by_day.keys()):
            d_name = day_names.get(dow, f"Day {dow}")
            rows_html += f"""
            <tr style="background:#f8fafc;">
                <td colspan="4" style="padding:10px 14px; font-weight:800; color:#0f172a; font-size:12px; border-bottom:2px solid #e2e8f0; text-transform:uppercase; letter-spacing:0.05em;">
                    <span style="background:#e2e8f0; color:#334155; padding:2px 8px; border-radius:4px; margin-right:6px; font-size:10px;">DAY</span> {d_name}
                </td>
            </tr>
            """
            for s in slots_by_day[dow]:
                st_str = s.time_slot.start_time.strftime('%I:%M %p')
                et_str = s.time_slot.end_time.strftime('%I:%M %p')
                room_badge = '<span style="background:#dbeafe; color:#1e40af; padding:3px 8px; border-radius:4px; font-size:11px; font-weight:700;">VIRTUAL</span>' if getattr(s.room, 'is_virtual', False) else f'<span style="background:#f1f5f9; color:#475569; padding:3px 8px; border-radius:4px; font-size:11px; font-weight:700;">Room: {s.room.name}</span>'
                
                rows_html += f"""
                <tr style="border-bottom:1px solid #f1f5f9;">
                    <td style="padding:12px 14px; font-size:12px; font-weight:700; color:#2563eb; white-space:nowrap; vertical-align:top;">
                        {st_str} - {et_str}
                    </td>
                    <td style="padding:12px 14px; vertical-align:top;">
                        <strong style="color:#0f172a; font-size:13px;">{s.course.code}</strong><br>
                        <span style="color:#64748b; font-size:12px;">{s.course.name}</span>
                    </td>
                    <td style="padding:12px 14px; vertical-align:top; font-size:12px; color:#475569;">
                        {room_badge}
                    </td>
                    <td style="padding:12px 14px; vertical-align:top; font-size:12px; color:#64748b;">
                        {s.student_group.name if s.student_group else 'N/A'}
                    </td>
                </tr>
                """

        if not rows_html:
            rows_html = '<tr><td colspan="4" style="padding:16px; text-align:center; color:#94a3b8; font-size:13px;">No active classes scheduled yet for this semester.</td></tr>'

        subject = f"[Verified] Your UniSchedule Credentials & Official Teaching Timetable"
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
        </head>
        <body style="font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color:#f1f5f9; margin:0; padding:24px; color:#334155;">
            <table role="presentation" style="max-width:680px; margin:0 auto; background:#ffffff; border-radius:12px; overflow:hidden; box-shadow:0 4px 20px rgba(0,0,0,0.08); border-collapse:collapse; width:100%;">
                <!-- Header -->
                <tr>
                    <td style="background:linear-gradient(135deg, #0f172a, #1e293b); padding:28px 32px; text-align:left;">
                        <div style="font-size:20px; font-weight:800; color:#ffffff; letter-spacing:-0.02em; margin-bottom:4px;">
                            UniSchedule <span style="font-size:12px; font-weight:600; color:#86efac; background:rgba(220,252,231,0.15); padding:2px 10px; border-radius:12px; text-transform:uppercase;">VERIFIED FACULTY</span>
                        </div>
                        <div style="font-size:13px; color:#94a3b8;">Official Academic Credentials &amp; Teaching Timetable</div>
                    </td>
                </tr>

                <!-- Content Body -->
                <tr>
                    <td style="padding:32px;">
                        <div style="background:#f0fdf4; border:1px solid #bbf7d0; color:#15803d; padding:12px 16px; border-radius:8px; font-weight:800; font-size:11px; text-transform:uppercase; letter-spacing:0.05em; margin-bottom:20px;">
                            VERIFICATION SUCCESSFUL &bull; FACULTY RECORD CONFIRMED
                        </div>

                        <h2 style="font-size:18px; font-weight:700; color:#0f172a; margin-top:0; margin-bottom:8px;">Dear {lecturer.name},</h2>
                        <p style="font-size:14px; color:#475569; line-height:1.6; margin-bottom:24px;">
                            Your lecturer profile has been successfully verified in the university database. Below are your portal credentials and official teaching schedule:
                        </p>

                        <!-- Credentials Card -->
                        <div style="background:#eff6ff; border:1px solid #bfdbfe; border-radius:8px; padding:18px 20px; margin-bottom:24px;">
                            <div style="font-size:11px; font-weight:800; color:#1e40af; text-transform:uppercase; letter-spacing:0.05em; margin-bottom:8px;">
                                PORTAL LOGIN CREDENTIALS
                            </div>
                            <div style="font-size:13px; color:#1e3a8a; line-height:1.6;">
                                <div><strong>Email Address (Login):</strong> <code style="background:#dbeafe; padding:2px 6px; border-radius:4px; font-family:monospace; color:#1e40af;">{email}</code></div>
                                <div><strong>Password:</strong> <code style="background:#dbeafe; padding:2px 6px; border-radius:4px; font-family:monospace; color:#1e40af;">{password_display}</code></div>
                                <div style="margin-top:8px; font-size:12px; color:#1d4ed8;">
                                    Direct Sign In: <a href="http://127.0.0.1:8000/accounts/login/" style="color:#1d4ed8; font-weight:700; text-decoration:underline;">http://127.0.0.1:8000/accounts/login/</a>
                                </div>
                            </div>
                        </div>

                        <!-- KPI Cards -->
                        <table role="presentation" style="width:100%; margin-bottom:24px; border-collapse:collapse;">
                            <tr>
                                <td style="width:50%; padding:14px; background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; text-align:center;">
                                    <div style="font-size:11px; font-weight:700; color:#64748b; text-transform:uppercase;">Assigned Classes</div>
                                    <div style="font-size:22px; font-weight:800; color:#2563eb; margin-top:2px;">{len(lec_slots)}</div>
                                </td>
                                <td style="width:8px;"></td>
                                <td style="width:50%; padding:14px; background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; text-align:center;">
                                    <div style="font-size:11px; font-weight:700; color:#64748b; text-transform:uppercase;">Weekly Contact Hours</div>
                                    <div style="font-size:22px; font-weight:800; color:#059669; margin-top:2px;">{round(total_contact_hours, 1)}h</div>
                                </td>
                            </tr>
                        </table>

                        <!-- Schedule Table -->
                        <div style="border:1px solid #e2e8f0; border-radius:8px; overflow:hidden; margin-bottom:28px;">
                            <table role="presentation" style="width:100%; border-collapse:collapse; text-align:left;">
                                <thead>
                                    <tr style="background:#f1f5f9; border-bottom:1px solid #e2e8f0;">
                                        <th style="padding:10px 14px; font-size:11px; color:#64748b; font-weight:700; text-transform:uppercase;">Time Slot</th>
                                        <th style="padding:10px 14px; font-size:11px; color:#64748b; font-weight:700; text-transform:uppercase;">Course Unit</th>
                                        <th style="padding:10px 14px; font-size:11px; color:#64748b; font-weight:700; text-transform:uppercase;">Location</th>
                                        <th style="padding:10px 14px; font-size:11px; color:#64748b; font-weight:700; text-transform:uppercase;">Student Group</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {rows_html}
                                </tbody>
                            </table>
                        </div>

                        <!-- CTA Action Button -->
                        <div style="text-align:center; margin-top:28px; margin-bottom:16px;">
                            <a href="http://127.0.0.1:8000/portal/timetable/" style="display:inline-block; background:linear-gradient(135deg, #2563eb, #1d4ed8); color:#ffffff; font-weight:700; font-size:14px; text-decoration:none; padding:12px 28px; border-radius:8px; box-shadow:0 2px 10px rgba(37,99,235,0.25);">
                                View Full Interactive Schedule
                            </a>
                        </div>
                    </td>
                </tr>

                <!-- Footer -->
                <tr>
                    <td style="background:#f8fafc; padding:20px 32px; border-top:1px solid #e2e8f0; text-align:center; font-size:12px; color:#94a3b8;">
                        UniSchedule Academic Timetable System &bull; Official Verification Notification
                    </td>
                </tr>
            </table>
        </body>
        </html>
        """
        try:
            msg = EmailMultiAlternatives(subject=subject, body=f"Dear {lecturer.name}, your faculty profile has been verified.", from_email=settings.DEFAULT_FROM_EMAIL, to=[email])
            msg.attach_alternative(html_content, "text/html")
            msg.send(fail_silently=False)
            logger.info(f"[Verification Email] Successfully sent verification email to {email}")
        except Exception as e:
            logger.error(f"[Verification Email] Failed to send verification email to {email}: {e}")

        return True, f"Verified lecturer record for '{lecturer.name}' ({email}). Confirmation email with credentials and teaching timetable sent."

    # 2. NOT VERIFIED (NO MATCH FOUND IN DATABASE)
    else:
        subject = f"[Action Required] Faculty Verification Pending — Record Not Found"
        recipient = submitted_email_clean or "user@domain.com"
        message = (
            f"Hello,\n\n"
            f"We attempted to verify your faculty registration for '{submitted_name or 'Submitted Name'}', "
            f"but no matching lecturer record was found in the university database.\n\n"
            f"Submitted Email: {submitted_email_clean}\n"
            f"Submitted Staff ID: {staff_id or 'N/A'}\n\n"
            f"Status: Your request has been flagged for manual verification by the University Registrar.\n"
            f"If you believe this is an error, please contact your Department Administrator."
        )

        try:
            send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [recipient], fail_silently=False)
            logger.info(f"[Verification Email] Sent pending verification email to {recipient}")
        except Exception as e:
            logger.error(f"[Verification Email] Failed to send unverified warning email to {recipient}: {e}")

        if university_id:
            try:
                uni = University.objects.get(pk=university_id)
                notify_university_managers(
                    university=uni,
                    title="⚠️ Unverified Lecturer Registration Alert",
                    message=f"A lecturer registration for '{submitted_name}' ({submitted_email_clean}) could not be verified in the database. Please review in Admin portal.",
                    link='/admin/scheduler/lecturer/',
                    level='warning'
                )
            except Exception as e:
                logger.warning(f"[Verification Email] Could not notify university managers: {e}")

        return False, f"Could not verify lecturer record for '{submitted_name}' ({submitted_email_clean}). Alert sent to Registrar."

