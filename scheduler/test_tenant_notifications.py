from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from scheduler.models import University, Semester, Timetable, ScheduleSlot, Room, Campus
from accounts.models import UserProfile
from scheduler.notifications import create_notification, notify_university_managers
from scheduler.models import Notification, Subscription

class TenantNotificationsTestCase(TestCase):
    def setUp(self):
        # Create Universities
        self.uni_a = University.objects.create(name="University A", code="UNIA")
        self.uni_b = University.objects.create(name="University B", code="UNIB")

        # Create Semesters
        self.semester_a = Semester.objects.create(
            university=self.uni_a, name="Sem A", start_date="2026-01-01", end_date="2026-06-01"
        )
        self.semester_b = Semester.objects.create(
            university=self.uni_b, name="Sem B", start_date="2026-01-01", end_date="2026-06-01"
        )

        # Create Timetables
        self.timetable_a = Timetable.objects.create(semester=self.semester_a, name="Timetable A")
        self.timetable_b = Timetable.objects.create(semester=self.semester_b, name="Timetable B")

        # Create Users
        self.user_a = User.objects.create_user(username="user_a", password="password", email="user_a@unia.edu")
        self.profile_a = UserProfile.objects.create(
            user=self.user_a, role="institution_admin", university=self.uni_a
        )

        self.user_b = User.objects.create_user(username="user_b", password="password", email="user_b@unib.edu")
        self.profile_b = UserProfile.objects.create(
            user=self.user_b, role="institution_admin", university=self.uni_b
        )

        self.client = Client()

    def test_tenant_isolation_redirects_foreign_timetable(self):
        """
        Verify that a user from University A cannot view University B's timetable details.
        """
        self.client.login(username="user_a", password="password")
        
        # Request detail page of foreign timetable
        url = reverse("scheduler:timetable_detail", kwargs={"pk": self.timetable_b.pk})
        response = self.client.get(url)
        
        # Verify it redirects to dashboard
        self.assertRedirects(response, reverse("scheduler:dashboard"))
        
        # Verify that we can view our own timetable
        url_own = reverse("scheduler:timetable_detail", kwargs={"pk": self.timetable_a.pk})
        response_own = self.client.get(url_own)
        self.assertEqual(response_own.status_code, 200)

    def test_tenant_user_cannot_switch_university(self):
        """
        Verify that a non-global admin user cannot switch the active university context to another university.
        """
        self.client.login(username="user_a", password="password")
        
        url = reverse("scheduler:switch_university")
        response = self.client.post(url, {"university_id": self.uni_b.pk})
        
        # Verify it redirects and permission is denied
        self.assertRedirects(response, reverse("scheduler:dashboard"))
        
        # Verify active university is still University A
        session = self.client.session
        active_uni_id = session.get("active_university_id")
        # Since user is restricted, get_active_uni inside views always falls back to profile university
        self.assertEqual(active_uni_id, self.uni_a.pk)

    def test_notification_creation(self):
        """
        Verify create_notification successfully writes Notification records.
        """
        notifications = create_notification(
            user=self.user_a,
            title="Test Title",
            message="Test Message Details",
            level="success",
            channels=["in_app"]
        )
        self.assertEqual(len(notifications), 1)
        self.assertEqual(Notification.objects.filter(user=self.user_a).count(), 1)
        
        n = Notification.objects.first()
        self.assertEqual(n.title, "Test Title")
        self.assertEqual(n.level, "success")
        self.assertFalse(n.is_read)

    def test_notify_managers(self):
        """
        Verify notify_university_managers triggers alerts for correct user profiles.
        """
        # Create an extra manager profile for University A
        user_mgr = User.objects.create_user(username="mgr_a", password="password", email="mgr@unia.edu")
        UserProfile.objects.create(
            user=user_mgr, role="timetable_officer", university=self.uni_a
        )

        # Notify University A managers
        notify_university_managers(
            university=self.uni_a,
            title="System Maintenance",
            message="The system will be updated tonight."
        )

        # Both user_a (institution_admin) and user_mgr (timetable_officer) should have notifications
        self.assertEqual(Notification.objects.filter(user=self.user_a).count(), 1)
        self.assertEqual(Notification.objects.filter(user=user_mgr).count(), 1)
        
        # User B (University B) should not have any notifications
        self.assertEqual(Notification.objects.filter(user=self.user_b).count(), 0)

    def test_default_subscription_tier_created(self):
        """
        Verify subscription is created dynamically for the university.
        """
        # Initially University A has no subscription
        self.assertFalse(Subscription.objects.filter(university=self.uni_a).exists())
        
        # Request dashboard to trigger dynamic subscription creation
        self.client.login(username="user_a", password="password")
        response = self.client.get(reverse("scheduler:dashboard"))
        self.assertEqual(response.status_code, 200)
        
        # Verify a subscription record is created with default free limits
        self.assertTrue(Subscription.objects.filter(university=self.uni_a).exists())
        sub = Subscription.objects.get(university=self.uni_a)
        self.assertEqual(sub.tier, "free")
        self.assertEqual(sub.max_rooms, 10)
        self.assertEqual(sub.max_courses, 50)
