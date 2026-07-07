"""
Live-Database Student Login Integration Tests
=============================================
These tests run against the **real db.sqlite3** using actual user accounts.

Run with:
    python manage.py test scheduler.test_student_login_live --keepdb --verbosity=2

IMPORTANT: Uses --keepdb so Django reuses the existing database without
           dropping/recreating it. No production data is modified — all
           tests are read-only (GET/POST login) and any writes are rolled
           back per test via TestCase transaction isolation.

Real Accounts Under Test
------------------------
| Username    | Role     | Student Group | University |
|-------------|----------|---------------|------------|
| NES         | student  | PHD (Size:40) | CUK        |
| ALex23445   | student  | None          | CUK        |
| userb       | student  | None          | CUK        |
| argan       | lecturer | None          | CUK        |
| admin       | admin    | MASTERS       | CUK        |

Passwords are the real passwords set in the live database.
Update PASSWORD_MAP below if any account password changes.
"""

from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth.models import User


# ---------------------------------------------------------------------------
# Real credential map  — update here if passwords change in the live DB
# ---------------------------------------------------------------------------
PASSWORD_MAP = {
    'NES':       'nes12345',       # student, linked to PHD group
    'ALex23445': 'alex12345',      # student, no group
    'userb':     'userb12345',     # student, no group
    'argan':     'argan12345',     # lecturer
    'admin':     'admin12345',     # superuser / admin
}


class LiveStudentLoginTests(TestCase):
    """
    Integration tests against the real database.
    Run with --keepdb to avoid dropping production data.
    """

    # Tell Django which databases to allow — uses the default (db.sqlite3)
    databases = ['default']

    def setUp(self):
        self.client = Client()

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------
    def _login(self, username, password=None):
        pwd = password or PASSWORD_MAP.get(username, '')
        return self.client.post(
            reverse('accounts:login'),
            {'username': username, 'password': pwd},
            follow=False,
        )

    # ==================================================================
    # 1. Login page renders for anonymous visitors
    # ==================================================================
    def test_login_page_renders_200(self):
        """GET /accounts/login/ returns 200 and uses login template."""
        response = self.client.get(reverse('accounts:login'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accounts/login.html')

    # ==================================================================
    # 2. NES — student with PHD group linked
    # ==================================================================
    def test_nes_valid_login_redirects(self):
        """NES (student / PHD group) logs in successfully and is redirected."""
        response = self._login('NES')
        self.assertIn(response.status_code, (301, 302),
                      "Expected redirect after successful login")
        self.assertTrue(self.client.session.get('_auth_user_id'),
                        "Session should be authenticated after login")

    def test_nes_session_role_is_student(self):
        """NES login sets active_role='student' in the session."""
        self._login('NES')
        self.assertEqual(self.client.session.get('active_role'), 'student')

    def test_nes_session_has_university_id(self):
        """NES login stores CUK's university ID in the session."""
        self._login('NES')
        self.assertIsNotNone(self.client.session.get('active_university_id'),
                             "active_university_id must be set after login")

    def test_nes_student_my_schedule_accessible(self):
        """NES (linked to PHD group) can access student_my_schedule."""
        self._login('NES')
        session = self.client.session
        session['active_role'] = 'student'
        session.save()

        response = self.client.get(reverse('scheduler:student_my_schedule'))
        # NES has a linked PHD group → should get 200 (not redirect to profile)
        self.assertEqual(response.status_code, 200)
        self.assertIn('student_group', response.context)
        self.assertIsNotNone(response.context['student_group'])
        self.assertEqual(response.context['student_group'].name, 'PHD')

    def test_nes_student_portal_weekly_timetable_accessible(self):
        """NES can access the full weekly timetable portal."""
        self._login('NES')
        session = self.client.session
        session['active_role'] = 'student'
        session.save()

        response = self.client.get(
            reverse('scheduler:student_portal_weekly_timetable')
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('slots_by_day', response.context)

    # ==================================================================
    # 3. ALex23445 — student with NO group linked
    # ==================================================================
    def test_alex_valid_login_redirects(self):
        """ALex23445 (student / no group) logs in and is redirected."""
        response = self._login('ALex23445')
        self.assertIn(response.status_code, (301, 302))
        self.assertTrue(self.client.session.get('_auth_user_id'))

    def test_alex_session_role_is_student(self):
        """ALex23445 login sets active_role='student'."""
        self._login('ALex23445')
        self.assertEqual(self.client.session.get('active_role'), 'student')

    def test_alex_my_schedule_redirects_to_profile(self):
        """ALex23445 has no student_group → student_my_schedule redirects to profile."""
        self._login('ALex23445')
        session = self.client.session
        session['active_role'] = 'student'
        session.save()

        response = self.client.get(reverse('scheduler:student_my_schedule'))
        self.assertRedirects(response, reverse('accounts:profile'),
                             msg_prefix="Unlinked student should be sent to profile page")

    def test_alex_weekly_timetable_redirects_to_profile(self):
        """ALex23445 has no student_group → weekly timetable redirects to profile."""
        self._login('ALex23445')
        session = self.client.session
        session['active_role'] = 'student'
        session.save()

        response = self.client.get(
            reverse('scheduler:student_portal_weekly_timetable')
        )
        self.assertIn(response.status_code, (301, 302))

    # ==================================================================
    # 4. userb — student with NO group linked
    # ==================================================================
    def test_userb_valid_login_redirects(self):
        """userb (student / no group) logs in and is redirected."""
        response = self._login('userb')
        self.assertIn(response.status_code, (301, 302))
        self.assertTrue(self.client.session.get('_auth_user_id'))

    def test_userb_my_schedule_redirects_to_profile(self):
        """userb has no group → student_my_schedule redirects to profile."""
        self._login('userb')
        session = self.client.session
        session['active_role'] = 'student'
        session.save()

        response = self.client.get(reverse('scheduler:student_my_schedule'))
        self.assertRedirects(response, reverse('accounts:profile'))

    # ==================================================================
    # 5. Invalid credentials — all users
    # ==================================================================
    def test_nes_wrong_password_rejected(self):
        """NES with wrong password stays on login page (no session created)."""
        response = self._login('NES', password='totally_wrong_pw')
        self.assertEqual(response.status_code, 200,
                         "Failed login should re-render login page (200)")
        self.assertFalse(self.client.session.get('_auth_user_id'))

    def test_alex_wrong_password_rejected(self):
        """ALex23445 with wrong password is rejected."""
        response = self._login('ALex23445', password='bad_password')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.client.session.get('_auth_user_id'))

    def test_nonexistent_user_rejected(self):
        """Logging in with a username that doesn't exist fails gracefully."""
        response = self._login('ghost_user_xyz', password='anything')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.client.session.get('_auth_user_id'))

    # ==================================================================
    # 6. argan — lecturer (must NOT access student portal)
    # ==================================================================
    def test_argan_login_sets_lecturer_role(self):
        """argan (lecturer) logs in and gets active_role='lecturer'."""
        self._login('argan')
        self.assertEqual(self.client.session.get('active_role'), 'lecturer')

    def test_argan_blocked_from_student_weekly_timetable(self):
        """argan (no student_group) accessing student weekly timetable is redirected."""
        self._login('argan')
        session = self.client.session
        session['active_role'] = 'lecturer'
        session.save()

        response = self.client.get(
            reverse('scheduler:student_portal_weekly_timetable')
        )
        # Lecturer has no student_group → profile redirect
        self.assertIn(response.status_code, (301, 302))

    # ==================================================================
    # 7. Logout
    # ==================================================================
    def test_logout_clears_nes_session(self):
        """NES logout destroys the auth session and redirects to login."""
        self._login('NES')
        self.assertTrue(self.client.session.get('_auth_user_id'))

        response = self.client.get(reverse('accounts:logout'))
        self.assertIn(response.status_code, (301, 302))
        self.assertFalse(self.client.session.get('_auth_user_id'),
                         "Session must be cleared after logout")

    # ==================================================================
    # 8. Already-authenticated user redirected away from login page
    # ==================================================================
    def test_authenticated_nes_redirected_from_login_page(self):
        """NES, already logged in, visiting /login/ is redirected away."""
        self._login('NES')
        response = self.client.get(reverse('accounts:login'))
        self.assertIn(response.status_code, (301, 302))

    def test_authenticated_alex_redirected_from_login_page(self):
        """ALex23445, already logged in, visiting /login/ is redirected away."""
        self._login('ALex23445')
        response = self.client.get(reverse('accounts:login'))
        self.assertIn(response.status_code, (301, 302))

    # ==================================================================
    # 9. Unauthenticated access to protected views
    # ==================================================================
    def test_unauthenticated_student_my_schedule_redirects_to_login(self):
        """Anonymous access to student_my_schedule redirects to the login page."""
        response = self.client.get(reverse('scheduler:student_my_schedule'))
        self.assertIn(response.status_code, (301, 302))
        self.assertIn('/login/', response['Location'])

    def test_unauthenticated_weekly_timetable_redirects_to_login(self):
        """Anonymous access to student_portal_weekly_timetable redirects to login."""
        response = self.client.get(
            reverse('scheduler:student_portal_weekly_timetable')
        )
        self.assertIn(response.status_code, (301, 302))
        self.assertIn('/login/', response['Location'])
