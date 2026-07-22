from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.contrib.auth import authenticate
from django.urls import reverse
from accounts.models import UserProfile

class EmailLoginTests(TestCase):
    """
    Test suite for checking email-based authentication and login.
    """
    def setUp(self):
        self.password = "securepass123"
        self.email = "teststudent@university.edu"
        self.username = "teststudent"
        
        # Create a test user
        self.user = User.objects.create_user(
            username=self.username,
            email=self.email,
            password=self.password
        )
        # Create user profile
        self.profile = UserProfile.objects.create(
            user=self.user,
            role="student"
        )
        self.client = Client()

    def test_authenticate_by_username(self):
        """Users can authenticate by username."""
        authenticated_user = authenticate(username=self.username, password=self.password)
        self.assertEqual(authenticated_user, self.user)

    def test_authenticate_by_email(self):
        """Users can authenticate by email."""
        authenticated_user = authenticate(username=self.email, password=self.password)
        self.assertEqual(authenticated_user, self.user)

    def test_authenticate_by_email_case_insensitive(self):
        """Email authentication is case-insensitive."""
        authenticated_user = authenticate(username=self.email.upper(), password=self.password)
        self.assertEqual(authenticated_user, self.user)

    def test_login_view_by_username(self):
        """Users can log in through the login view using username."""
        response = self.client.post(
            reverse('accounts:login'),
            {'username': self.username, 'password': self.password}
        )
        self.assertIn(response.status_code, (301, 302))
        self.assertTrue(self.client.session.get('_auth_user_id'))

    def test_login_view_by_email(self):
        """Users can log in through the login view using email."""
        response = self.client.post(
            reverse('accounts:login'),
            {'username': self.email, 'password': self.password}
        )
        self.assertIn(response.status_code, (301, 302))
        self.assertTrue(self.client.session.get('_auth_user_id'))

    def test_login_invalid_credentials_rejected(self):
        """Invalid passwords or usernames are rejected."""
        response = self.client.post(
            reverse('accounts:login'),
            {'username': self.email, 'password': 'wrongpassword'}
        )
        self.assertEqual(response.status_code, 200) # Re-renders page
        self.assertFalse(self.client.session.get('_auth_user_id'))
