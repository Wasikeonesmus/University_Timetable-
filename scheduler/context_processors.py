from .models import University, Semester


def active_scheduler_context(request):
    """
    Context processor to inject active university, active semester,
    and real user role into all templates.

    FIX U4: Cache the University queryset on the request object so that
    repeated accesses within the same request (e.g. multiple context
    processors or template tags) don't trigger extra DB queries.
    """
    # ── FIX U4: Request-level cache for University list ───────────────────────
    # getattr/setattr on the request object is safe and lives only for this
    # single HTTP request — no cross-request leakage.
    universities = getattr(request, '_cached_universities', None)
    if universities is None:
        universities = list(University.objects.all())
        request._cached_universities = universities

    active_university = None
    active_semester   = None

    # 1. Resolve Active University
    university_id = request.session.get('active_university_id')

    # Prefer user profile university if no session choice
    if not university_id and request.user.is_authenticated:
        try:
            if request.user.profile.university:
                university_id = request.user.profile.university.id
                request.session['active_university_id'] = university_id
        except Exception:
            pass

    if university_id:
        # Use the in-memory list to avoid an extra DB query
        active_university = next((u for u in universities if u.id == university_id), None)

    if not active_university and universities:
        active_university = universities[0]
        request.session['active_university_id'] = active_university.id

    # 2. Resolve Active Semester for this University
    # FIX BUG 15: Cache the active semester in Django's cache (60 sec TTL) to avoid
    # running up to 2 DB queries on every single HTTP request for every user.
    if active_university:
        from django.core.cache import cache
        cache_key = f'active_semester_{active_university.id}'
        active_semester = cache.get(cache_key)
        if active_semester is None:
            active_semester = (
                Semester.objects.filter(university=active_university, is_active=True).first()
                or Semester.objects.filter(university=active_university).first()
            )
            # Cache for 60 seconds; semester changes are rare
            cache.set(cache_key, active_semester, timeout=60)

    # 3. Resolve Role — use get_effective_role helper for absolute consistency
    from .permissions import get_effective_role
    active_role = None  # FIX BUG 8: Default is None, not 'admin'. Anonymous users should have no role.
    if request.user.is_authenticated:
        role = get_effective_role(request)
        if role:
            active_role = role
        else:
            active_role = 'student'  # Authenticated but no role → default to least-privileged
    else:
        # FIX BUG 8: Unauthenticated users get None — templates should check `active_role` before
        # rendering role-sensitive content. Previously defaulted to 'admin' which was a
        # privilege escalation risk if the session was not properly cleared.
        active_role = None

    available_roles = [
        ('admin',     'Super Admin'),
        ('scheduler', 'Scheduler'),
        ('lecturer',  'Lecturer'),
        ('student',   'Student'),
    ]

    return {
        'all_universities': universities,
        'active_university': active_university,
        'active_semester':   active_semester,
        'active_role':       active_role,
        'available_roles':   available_roles,
    }
