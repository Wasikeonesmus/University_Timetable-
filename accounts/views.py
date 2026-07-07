from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction

from .forms import LoginForm, RegisterForm, ProfileForm
from .models import UserProfile, GoogleCalendarToken

import json
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from scheduler import google_calendar_service



def login_view(request):
    """Handles user login with role-based redirect."""
    if request.user.is_authenticated:
        return redirect('scheduler:dashboard')

    if request.method == 'POST':
        form = LoginForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)

            # Set session role from profile
            try:
                profile = user.profile
                request.session['active_role'] = profile.role
                if profile.university:
                    request.session['active_university_id'] = profile.university.id
            except UserProfile.DoesNotExist:
                request.session['active_role'] = 'admin'

            messages.success(request, f"Welcome back, {user.get_full_name() or user.username}!")
            # Lecturers go straight to their personal portal
            next_url = request.GET.get('next')
            if not next_url:
                if profile.role == 'lecturer':
                    next_url = 'scheduler:lecturer_my_schedule'
                else:
                    next_url = 'scheduler:dashboard'
            return redirect(next_url)
        else:
            messages.error(request, "Invalid username or password. Please try again.")
    else:
        form = LoginForm(request)

    return render(request, 'accounts/login.html', {'form': form})


def logout_view(request):
    """Logs out the user and redirects to login."""
    logout(request)
    messages.info(request, "You have been logged out.")
    return redirect('accounts:login')


def register_view(request):
    """Handles new user registration."""
    if request.user.is_authenticated:
        return redirect('scheduler:dashboard')

    if request.method == 'POST':
        form = RegisterForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                user = form.save()
            messages.success(request, "Account created successfully! Please log in.")
            return redirect('accounts:login')
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = RegisterForm()

    return render(request, 'accounts/register.html', {'form': form})


@login_required
def profile_view(request):
    """View and edit user profile."""
    try:
        profile = request.user.profile
    except UserProfile.DoesNotExist:
        profile = UserProfile.objects.create(user=request.user, role='admin')

    if request.method == 'POST':
        form = ProfileForm(request.POST, instance=profile, is_admin=request.user.is_superuser, user_role=profile.role)
        if form.is_valid():
            # Update User fields
            request.user.first_name = form.cleaned_data['first_name']
            request.user.last_name = form.cleaned_data['last_name']
            request.user.email = form.cleaned_data['email']
            request.user.save()

            form.save()
            # Sync session role
            request.session['active_role'] = profile.role
            if profile.university:
                request.session['active_university_id'] = profile.university.id
            messages.success(request, "Profile updated successfully!")
            return redirect('accounts:profile')
    else:
        form = ProfileForm(
            instance=profile,
            is_admin=request.user.is_superuser,
            user_role=profile.role,
            initial={
                'first_name': request.user.first_name,
                'last_name': request.user.last_name,
                'email': request.user.email,
            }
        )

    return render(request, 'accounts/profile.html', {'form': form, 'profile': profile})


@login_required
def google_calendar_auth(request):
    """
    Initiates Google OAuth2 authentication flow for calendar access.
    """
    try:
        flow = google_calendar_service.get_auth_flow(request)
    except FileNotFoundError:
        messages.error(request, "Google Calendar integration is not configured. Please contact your administrator to set up the client_secret.json file.")
        return redirect('accounts:profile')
    
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'
    )
    request.session['oauth_state'] = state
    request.session['code_verifier'] = flow.code_verifier
    return redirect(authorization_url)


@login_required
def google_calendar_callback(request):
    """
    Callback handler for Google OAuth2. Processes redirect query parameters,
    exchanges auth code for tokens, saves them, and triggers initial schedule sync.
    """
    state = request.session.get('oauth_state')
    if not state or state != request.GET.get('state'):
        messages.error(request, "Google authentication state mismatch. Please try again.")
        return redirect('accounts:profile')

    code_verifier = request.session.get('code_verifier')

    if 'oauth_state' in request.session:
        del request.session['oauth_state']
    if 'code_verifier' in request.session:
        del request.session['code_verifier']

    try:
        flow = google_calendar_service.get_auth_flow(request)
        flow.fetch_token(
            authorization_response=request.build_absolute_uri(),
            code_verifier=code_verifier
        )
        credentials = flow.credentials
        
        # Get authorized Google account email from userinfo details
        oauth2_service = build('oauth2', 'v2', credentials=credentials)
        user_info = oauth2_service.userinfo().get().execute()
        google_email = user_info.get('email')

        # Save credentials to user profile
        gtoken, created = GoogleCalendarToken.objects.update_or_create(
            user=request.user,
            defaults={
                'token': credentials.to_json(),
                'email': google_email
            }
        )
        messages.success(request, f"Successfully connected Google Calendar account ({google_email})!")

        # Trigger initial sync for lecturer schedule
        profile = request.user.profile
        if profile.role == 'lecturer' and profile.lecturer:
            from scheduler.models import Timetable
            timetable = Timetable.objects.filter(semester__university=profile.university, is_active=True).first()
            if not timetable:
                timetable = Timetable.objects.filter(semester__university=profile.university).order_by('-created_at').first()
            
            if timetable:
                import sys
                if 'test' in sys.argv:
                    from scheduler.google_tasks import sync_lecturer_timetable_google
                    sync_lecturer_timetable_google(profile.lecturer.id, timetable.id)
                else:
                    from django_q.tasks import async_task
                    async_task('scheduler.google_tasks.sync_lecturer_timetable_google', profile.lecturer.id, timetable.id)

    except Exception as e:
        messages.error(request, f"Failed to authenticate with Google Calendar: {str(e)}")

    return redirect('accounts:profile')


@login_required
def google_calendar_disconnect(request):
    """
    Removes Google OAuth2 credentials from user account.
    """
    try:
        gtoken = request.user.google_token
        email = gtoken.email
        gtoken.delete()
        messages.success(request, f"Disconnected Google Calendar account ({email}).")
    except GoogleCalendarToken.DoesNotExist:
        messages.warning(request, "No Google Calendar account is connected.")

    return redirect('accounts:profile')

