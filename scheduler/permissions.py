"""
Role-based access helpers and multi-tenant security decorators.
"""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import redirect, get_object_or_404

# Expanded Roles Mapping
ROLE_ADMIN = 'admin'                    # Global Super Admin
ROLE_INST_ADMIN = 'institution_admin'   # Tenant-level Admin
ROLE_REGISTRAR = 'registrar'            # Tenant Registrar
ROLE_DVC = 'dvc_academic'              # Tenant DVC Academic
ROLE_DEAN = 'dean'                      # Tenant Faculty Dean
ROLE_HOD = 'hod'                        # Tenant Department Head
ROLE_TIMETABLE_OFFICER = 'timetable_officer' # Tenant Timetable Scheduler
ROLE_SCHEDULER = 'scheduler'            # Backward-compatible Scheduler role
ROLE_LECTURER = 'lecturer'              # Tenant Lecturer
ROLE_STUDENT = 'student'                # Tenant Student

# Roles authorized to generate, schedule, or configure core models
MANAGER_ROLES = (ROLE_ADMIN, ROLE_INST_ADMIN, ROLE_REGISTRAR, ROLE_SCHEDULER, ROLE_TIMETABLE_OFFICER)


def get_effective_role(request):
    """Return the authenticated user's role, or None if anonymous."""
    if not request.user.is_authenticated:
        return None
    
    # 1. Check session first (handles simulation and cached role)
    session_role = request.session.get('active_role')
    if session_role:
        return session_role
        
    # 2. Check profile role
    try:
        return request.user.profile.role
    except Exception:
        pass
        
    # 3. Fallback for superuser
    if request.user.is_superuser:
        return ROLE_ADMIN
        
    return None


def user_can_manage(request):
    role = get_effective_role(request)
    return role in MANAGER_ROLES or request.user.is_superuser


def role_required(*allowed_roles):
    """Require login and one of the given roles."""
    def decorator(view_func):
        @login_required
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            role = get_effective_role(request)
            if request.user.is_superuser:
                return view_func(request, *args, **kwargs)
            if role not in allowed_roles:
                messages.error(request, "Permission denied.")
                return redirect('scheduler:dashboard')
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator


def manager_required(view_func):
    """Require admin or scheduler role."""
    return role_required(*MANAGER_ROLES)(view_func)


def check_tenant_access(user, obj):
    """
    Verifies that the given user has access to the given object.
    For global super admins (role == 'admin'), allow access to everything.
    For other roles, check if the object belongs to their university.
    """
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    try:
        profile = user.profile
        if profile.role == 'admin':
            return True
        user_uni = profile.university
        if not user_uni:
            return False

        # Inspect model class to find university association
        model_name = obj.__class__.__name__
        if model_name == 'University':
            return obj == user_uni
        elif hasattr(obj, 'university'):
            return obj.university == user_uni
        elif hasattr(obj, 'campus'):
            return obj.campus.university == user_uni
        elif hasattr(obj, 'faculty'):
            return obj.faculty.campus.university == user_uni
        elif hasattr(obj, 'department'):
            return obj.department.faculty.campus.university == user_uni
        elif hasattr(obj, 'program'):
            return obj.program.department.faculty.campus.university == user_uni
        elif hasattr(obj, 'course'):
            return obj.course.program.department.faculty.campus.university == user_uni
        elif hasattr(obj, 'semester'):
            return obj.semester.university == user_uni
        elif hasattr(obj, 'timetable'):
            return obj.timetable.semester.university == user_uni
        elif model_name == 'ScheduleSlot':
            return obj.timetable.semester.university == user_uni
        elif model_name == 'GenerationLog':
            return obj.timetable.semester.university == user_uni
        elif model_name == 'Lecturer':
            return obj.department.faculty.campus.university == user_uni
        elif model_name == 'StudentGroup':
            return obj.program.department.faculty.campus.university == user_uni
        elif model_name == 'Room':
            return obj.campus.university == user_uni
        elif model_name == 'Building':
            return obj.campus.university == user_uni
        elif model_name == 'LecturerAvailability':
            return obj.lecturer.department.faculty.campus.university == user_uni
        elif model_name == 'LecturerTimeSlotPreference':
            return obj.lecturer.department.faculty.campus.university == user_uni
    except Exception:
        return False
    return False


def tenant_required(model_class, lookup_field='pk'):
    """
    Decorator that checks if the request user belongs to the same university
    as the retrieved object. If not, returns permission denied.
    """
    def decorator(view_func):
        @login_required
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            lookup_val = kwargs.get(lookup_field)
            if not lookup_val:
                messages.error(request, "Permission denied.")
                return redirect('scheduler:dashboard')
            obj = get_object_or_404(model_class, **{lookup_field: lookup_val})
            if not check_tenant_access(request.user, obj):
                messages.error(request, "Permission denied. You do not have access to this resource.")
                return redirect('scheduler:dashboard')
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator
