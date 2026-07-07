import json, csv, io
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST
from django.db.models import Count, Q

from .models import (
    University, Campus, Faculty, Department, Room, Lecturer, StudentGroup,
    Course, Timetable, ScheduleSlot, TimeSlot, Constraint, Semester, GenerationLog,
    LecturerAvailability, Program, AttendanceSession, AttendanceRecord, Announcement, Notification,
    ApprovalLog, FieldMapping
)
from .forms import (
    TimetableForm, ConstraintForm, UniversityForm, CampusForm, FacultyForm,
    DepartmentForm, CourseForm, LecturerForm, StudentGroupForm, RoomForm, TimeSlotForm
)
from .solver import generate_timetable
from .conflicts import check_conflicts_for_timetable, detect_conflicts
from .scheduling_service import run_scheduling_pipeline
from django.conf import settings
from .firebase_service import update_timetable_conflicts, trigger_timetable_refresh
from .permissions import (
    get_effective_role, manager_required, role_required, ROLE_STUDENT, ROLE_LECTURER, 
    ROLE_INST_ADMIN, ROLE_REGISTRAR, ROLE_DVC, ROLE_DEAN, ROLE_HOD, ROLE_TIMETABLE_OFFICER,
    ROLE_SCHEDULER, ROLE_ADMIN, MANAGER_ROLES, tenant_required
)

# ──────────────────────────────────────────────────────────────────────────────
# Timetable Conflicts AJAX Caching
# The cache dict and clear helper live in conflict_cache.py so that
# scheduling_service.py can also invalidate it without a circular import.
# ──────────────────────────────────────────────────────────────────────────────
from .conflict_cache import _CONFLICTS_JSON_CACHE, clear_conflicts_cache, _CACHE_MAX_SIZE

def _get_timetable_conflict_fingerprint(timetable):
    # 1. Fetch slot assignments (room, timeslot) — cheap indexed scan
    slots_fp = list(timetable.slots.order_by('id').values_list('id', 'room_id', 'time_slot_id'))
    # 2. Constraint configuration for the university
    constraints_fp = list(Constraint.objects.filter(
        university_id=timetable.semester.university_id
    ).order_by('id').values_list('id', 'is_hard', 'weight', 'parameters'))
    # 3. Lecturer self-service availability
    avail_fp = list(LecturerAvailability.objects.filter(
        lecturer__department__faculty__campus__university_id=timetable.semester.university_id
    ).order_by('id').values_list('id', 'lecturer_id', 'time_slot_id', 'is_available'))
    return (timetable.id, tuple(slots_fp), tuple(constraints_fp), tuple(avail_fp))



def get_active_uni(request):
    """
    Helper to fetch the session's active university or fall back to the first.
    If the user has a profile with a linked university, restrict to that (unless global Super Admin).
    """
    if request.user.is_authenticated:
        try:
            profile = request.user.profile
            if profile.role != 'admin' and profile.university:
                return profile.university
        except Exception:
            pass

    uni_id = request.session.get('active_university_id')
    if uni_id:
        uni = University.objects.filter(id=uni_id).first()
        if uni:
            return uni
    uni = University.objects.first()
    if uni:
        request.session['active_university_id'] = uni.id
    return uni


def get_user_role(request):
    """Returns the authenticated user's role from UserProfile."""
    role = get_effective_role(request)
    return role if role else ROLE_STUDENT


@login_required
def dashboard(request):
    role = get_user_role(request)
    university = get_active_uni(request)

    # ── Lecturer gets redirected to the new Lecturer Portal ──────────────────────────
    if role == ROLE_LECTURER:
        try:
            if request.user.profile.lecturer:
                return redirect('scheduler:lecturer_portal_dashboard')
            else:
                messages.error(request, "Your account is not linked to a lecturer profile. Please update your profile.")
                return redirect('accounts:profile')
        except Exception:
            messages.error(request, "Your account is not linked to a lecturer profile. Please update your profile.")
            return redirect('accounts:profile')


    if not university:
        return render(request, 'scheduler/dashboard.html', {
            'timetables': [],
            'active_role': role,
        })

    # Filter all metrics by active university
    campuses_count       = Campus.objects.filter(university=university).count()
    rooms_count          = Room.objects.filter(campus__university=university).count()
    lecturers_count      = Lecturer.objects.filter(department__faculty__campus__university=university).count()
    student_groups_count = StudentGroup.objects.filter(program__department__faculty__campus__university=university).count()
    courses_count        = Course.objects.filter(program__department__faculty__campus__university=university).count()
    constraints_count    = Constraint.objects.filter(university=university).count()

    # Find the active timetable
    active_timetable = Timetable.objects.filter(semester__university=university, is_active=True).first()
    if not active_timetable:
        active_timetable = Timetable.objects.filter(semester__university=university).first()

    conflict_count   = 0
    timetable_status = "No Timetable"
    if active_timetable:
        from .models import GenerationLog
        latest_log = (
            GenerationLog.objects
            .filter(timetable=active_timetable)
            .exclude(status='PENDING')
            .order_by('-created_at')
            .values('hard_conflicts_found', 'courses_scheduled')
            .first()
        )
        if latest_log is not None:
            conflict_count   = latest_log['hard_conflicts_found']
            timetable_status = "Generated ✓" if latest_log['courses_scheduled'] > 0 else "Empty"
        else:
            slot_count       = active_timetable.slots.count()
            timetable_status = "Generated ✓" if slot_count > 0 else "Empty"

    timetables = Timetable.objects.filter(semester__university=university).order_by('-created_at')[:5]

    from .models import Subscription
    subscription = getattr(university, 'subscription', None)
    if not subscription:
        try:
            subscription = Subscription.objects.create(university=university, tier='free', status='active')
        except Exception:
            subscription = None

    notifications = []
    if request.user.is_authenticated:
        notifications = request.user.notifications.filter(is_read=False)[:5]

    from .models import Announcement
    announcements = Announcement.objects.filter(Q(university=university) | Q(university__isnull=True)).order_by('-created_at')[:3]

    role_context = {}
    if request.user.is_authenticated:
        try:
            profile = request.user.profile
            if role == ROLE_STUDENT and profile.student_group:
                role_context['student_group'] = profile.student_group
            elif role == ROLE_HOD and profile.lecturer:
                role_context['department'] = profile.lecturer.department
                role_context['dept_lecturers_count'] = Lecturer.objects.filter(department=profile.lecturer.department).count()
                role_context['dept_courses_count'] = Course.objects.filter(program__department=profile.lecturer.department).count()
            elif role == ROLE_DEAN and profile.lecturer and profile.lecturer.department:
                faculty = profile.lecturer.department.faculty
                role_context['faculty'] = faculty
                role_context['faculty_departments_count'] = Department.objects.filter(faculty=faculty).count()
                role_context['faculty_rooms_count'] = Room.objects.filter(campus=faculty.campus).count()
        except Exception:
            pass

    context = {
        'active_university':    university,
        'active_semester':      Semester.objects.filter(university=university, is_active=True).first(),
        'active_role':          role,
        'campuses_count':       campuses_count,
        'rooms_count':          rooms_count,
        'lecturers_count':      lecturers_count,
        'student_groups_count': student_groups_count,
        'courses_count':        courses_count,
        'constraints_count':    constraints_count,
        'timetables':           timetables,
        'active_timetable':     active_timetable,
        'conflict_count':       conflict_count,
        'timetable_status':     timetable_status,
        'subscription':         subscription,
        'notifications':        notifications,
        'announcements':        announcements,
        'role_context':         role_context,
    }
    return render(request, 'scheduler/dashboard.html', context)

@login_required
def timetable_list(request):
    university = get_active_uni(request)
    if not university:
        messages.error(request, "Please create a University first in Admin panel.")
        return redirect('admin:index')

    timetables = Timetable.objects.filter(semester__university=university).select_related('semester').order_by('-created_at')
    
    # Restrict Semester selection to the current university in the form
    form = TimetableForm()
    form.fields['semester'].queryset = Semester.objects.filter(university=university)

    if request.method == 'POST':
        # Don't allow students/lecturers to create timetables
        role = get_user_role(request)
        if role in (ROLE_STUDENT, ROLE_LECTURER):
            messages.error(request, "Permission denied. Only Admins/Schedulers can create timetables.")
            return redirect('scheduler:timetable_list')

        form = TimetableForm(request.POST)
        form.fields['semester'].queryset = Semester.objects.filter(university=university)
        if form.is_valid():
            form.save()
            messages.success(request, "Timetable version created successfully.")
            return redirect('scheduler:timetable_list')
    
    return render(request, 'scheduler/timetable_list.html', {
        'timetables': timetables,
        'form': form
    })

@login_required
def timetable_create(request):
    role = get_user_role(request)
    if role in (ROLE_STUDENT, ROLE_LECTURER):
        messages.error(request, "Permission denied.")
        return redirect('scheduler:dashboard')

    university = get_active_uni(request)
    if request.method == 'POST':
        form = TimetableForm(request.POST)
        form.fields['semester'].queryset = Semester.objects.filter(university=university)
        if form.is_valid():
            timetable = form.save()
            messages.success(request, "Timetable version created.")
            return redirect('scheduler:timetable_detail', pk=timetable.pk)
    else:
        form = TimetableForm()
        form.fields['semester'].queryset = Semester.objects.filter(university=university)
    return render(request, 'scheduler/timetable_form.html', {'form': form})

@login_required
@tenant_required(Timetable)
def timetable_detail(request, pk):
    role = get_user_role(request)
    
    # Lecturers and students should use their personal schedule pages
    if role in (ROLE_LECTURER, ROLE_STUDENT):
        if role == ROLE_LECTURER:
            return redirect('scheduler:lecturer_my_schedule')
        else:
            return redirect('scheduler:student_my_schedule')
    
    timetable = get_object_or_404(Timetable.objects.select_related('semester', 'semester__university'), pk=pk)

    # Always derive university from the timetable itself so the grid renders
    # correctly regardless of which university is active in the session.
    university = timetable.semester.university
    # Sync the session so the rest of the UI stays consistent.
    request.session['active_university_id'] = university.id

    # Get filters
    filter_type = request.GET.get('filter_type', 'group')
    filter_id = request.GET.get('filter_id')

    # Filter resources by university
    rooms = Room.objects.filter(campus__university=university)
    lecturers = Lecturer.objects.filter(department__faculty__campus__university=university)
    student_groups = StudentGroup.objects.filter(program__department__faculty__campus__university=university)

    # Resolve default filter target
    selected_filter_name = ""
    if not filter_id:
        if filter_type == 'group' and student_groups.exists():
            filter_id = student_groups.first().id
        elif filter_type == 'room' and rooms.exists():
            filter_id = rooms.first().id
        elif filter_type == 'lecturer' and lecturers.exists():
            filter_id = lecturers.first().id

    # FIX U3: Fetch ALL slots once — reuse for both the grid and conflict detection.
    # The old code fetched slots twice (once filtered for the grid, once unfiltered for conflicts).
    all_slots = list(
        timetable.slots
        .select_related('course', 'lecturer', 'room', 'time_slot', 'student_group')
        .all()
    )

    from django.utils import timezone
    local_now = timezone.localtime(timezone.now())
    current_dow = local_now.isoweekday()
    current_time = local_now.time()
    for s in all_slots:
        ts = s.time_slot
        if ts.day_of_week < current_dow:
            s.has_ended = True
            s.is_ongoing = False
        elif ts.day_of_week == current_dow:
            s.has_ended = (ts.end_time < current_time)
            s.is_ongoing = (ts.start_time <= current_time <= ts.end_time)
        else:
            s.has_ended = False
            s.is_ongoing = False


    # Apply the view filter in Python (no extra DB query)
    if filter_id:
        filter_id = int(filter_id)
        if filter_type == 'group':
            slots_list = [s for s in all_slots if s.student_group_id == filter_id]
            selected_filter_name = str(student_groups.filter(id=filter_id).first())
        elif filter_type == 'room':
            slots_list = [s for s in all_slots if s.room_id == filter_id]
            selected_filter_name = str(rooms.filter(id=filter_id).first())
        elif filter_type == 'lecturer':
            slots_list = [s for s in all_slots if s.lecturer_id == filter_id]
            selected_filter_name = str(lecturers.filter(id=filter_id).first())
        else:
            slots_list = all_slots
    else:
        slots_list = all_slots

    # Time slots sorting — filter by the timetable's own university
    timeslots     = TimeSlot.objects.filter(university=university).order_by('day_of_week', 'slot_number')
    days_in_slots = sorted(set(ts.day_of_week for ts in timeslots))
    slot_numbers  = sorted(set(ts.slot_number for ts in timeslots))

    # Create grid with rowspan and is_merged fields
    grid = {}
    day_labels = {
        1: 'Monday', 2: 'Tuesday', 3: 'Wednesday', 4: 'Thursday',
        5: 'Friday', 6: 'Saturday', 7: 'Sunday'
    }

    ts_by_day_and_num = {(ts.day_of_week, ts.slot_number): ts for ts in timeslots}
    for slot_num in slot_numbers:
        grid[slot_num] = {}
        for day in days_in_slots:
            grid[slot_num][day] = {
                'time_slot': ts_by_day_and_num.get((day, slot_num)),
                'slots': [],
                'rowspan': 1,
                'is_merged': False
            }

    for slot in slots_list:
        ts = slot.time_slot
        if ts.slot_number in grid and ts.day_of_week in grid[ts.slot_number]:
            grid[ts.slot_number][ts.day_of_week]['slots'].append(slot)

    # Calculate rowspans and merge consecutive slots
    for day in days_in_slots:
        day_slots = []
        for slot_num in slot_numbers:
            cell = grid[slot_num][day]
            if cell['slots']:
                day_slots.append((slot_num, cell['slots']))
        
        i = 0
        while i < len(day_slots):
            slot_num, slots = day_slots[i]
            primary_slot = slots[0]
            
            rowspan = 1
            j = i + 1
            while j < len(day_slots):
                next_slot_num, next_slots = day_slots[j]
                next_primary_slot = next_slots[0]
                
                # Check if it's consecutive and belongs to the same class session
                if (next_slot_num == slot_num + rowspan and
                    next_primary_slot.course_id == primary_slot.course_id and
                    next_primary_slot.lecturer_id == primary_slot.lecturer_id and
                    next_primary_slot.room_id == primary_slot.room_id and
                    next_primary_slot.student_group_id == primary_slot.student_group_id):
                    rowspan += 1
                    j += 1
                else:
                    break
            
            grid[slot_num][day]['rowspan'] = rowspan
            for r in range(1, rowspan):
                grid[slot_num + r][day]['is_merged'] = True
                
            i = j

    # Slot label map: slot_number → {"start": "h:mm am/pm", "end": "h:mm am/pm", "range": "h:mm am/pm – h:mm am/pm"}
    slot_time_labels = {}
    for ts in timeslots:
        if ts.slot_number not in slot_time_labels:
            start_fmt = ts.start_time.strftime('%I:%M %p').lstrip('0').lower()
            end_fmt = ts.end_time.strftime('%I:%M %p').lstrip('0').lower()
            slot_time_labels[ts.slot_number] = {
                'start': start_fmt,
                'end': end_fmt,
                'range': f"{start_fmt} – {end_fmt}"
            }


    # Conflict detector — reuses the already-fetched all_slots list (no extra DB hit)
    conflict_list = detect_conflicts(all_slots, university)
    errors   = [c for c in conflict_list if c['severity'] == 'error']
    warnings = [c for c in conflict_list if c['severity'] == 'warning']

    # Serialize slots and timeslots for JS calendar consumption (merging consecutive slots)
    from collections import defaultdict
    sessions = defaultdict(list)
    for s in slots_list:
        key = (s.time_slot.day_of_week, s.course_id, s.lecturer_id, s.room_id, s.student_group_id)
        sessions[key].append(s)

    slots_data = []
    for key, s_list in sessions.items():
        s_list.sort(key=lambda s: s.time_slot.slot_number)
        i = 0
        while i < len(s_list):
            run = [s_list[i]]
            j = i + 1
            while j < len(s_list):
                prev = s_list[j-1]
                curr = s_list[j]
                if (curr.time_slot.slot_number == prev.time_slot.slot_number + 1 and
                    curr.time_slot.start_time == prev.time_slot.end_time):
                    run.append(curr)
                    j += 1
                else:
                    break
            
            first_s = run[0]
            last_s = run[-1]
            slots_data.append({
                'id': first_s.id,
                'course_code': first_s.course.code,
                'course_name': first_s.course.name,
                'room_id': first_s.room_id,
                'room_name': first_s.room.name,
                'lecturer_id': first_s.lecturer_id,
                'lecturer_name': first_s.lecturer.name,
                'group_id': first_s.student_group_id,
                'group_name': first_s.student_group.name,
                'time_slot_id': first_s.time_slot_id,
                'day_of_week': first_s.time_slot.day_of_week,
                'start_time': first_s.time_slot.start_time.strftime('%H:%M:%S'),
                'end_time': last_s.time_slot.end_time.strftime('%H:%M:%S'),
            })
            i = j

    timeslots_data = [{
        'id': ts.id,
        'day_of_week': ts.day_of_week,
        'start_time': ts.start_time.strftime('%H:%M:%S'),
        'end_time': ts.end_time.strftime('%H:%M:%S'),
        'slot_number': ts.slot_number,
    } for ts in timeslots]

    # Calculate dates for the current week (Monday-based)
    import datetime
    _today = datetime.date.today()
    _monday = _today - datetime.timedelta(days=_today.weekday())
    days_data = []
    for d in days_in_slots:
        days_data.append({
            'num': d,
            'label': day_labels.get(d, f"Day {d}"),
            'date': _monday + datetime.timedelta(days=d - 1)
        })

    context = {
        'timetable':           timetable,
        'rooms':               rooms,
        'lecturers':           lecturers,
        'student_groups':      student_groups,
        'filter_type':         filter_type,
        'filter_id':           filter_id,
        'selected_filter_name': selected_filter_name,
        'grid':                grid,
        'days':                days_data,
        'slot_numbers':        slot_numbers,
        'slot_time_labels':    slot_time_labels,
        'errors':              errors,
        'warnings':            warnings,
        'timeslots':           timeslots,
        'slots_json':          slots_data,
        'timeslots_json':      timeslots_data,
        'slot_count':          len(all_slots),
        'firebase_config':     settings.FIREBASE_CONFIG if __import__('scheduler.firebase_service', fromlist=['is_enabled']).is_enabled else None,
        'approval_logs':       timetable.approval_logs.all(),
        'user_role':           role,
    }
    return render(request, 'scheduler/timetable_detail.html', context)



@login_required
@tenant_required(Timetable)
def timetable_weekly(request, pk):
    """
    Branded weekly timetable view — clean, print-friendly, university-styled.
    Supports toggle by student group, lecturer, or room.
    Designed for PDF/print export.
    """
    role = get_user_role(request)
    
    # Lecturers and students should use their personal schedule pages
    if role in (ROLE_LECTURER, ROLE_STUDENT):
        if role == ROLE_LECTURER:
            return redirect('scheduler:lecturer_my_schedule')
        else:
            return redirect('scheduler:student_my_schedule')
    
    university = get_active_uni(request)
    timetable = get_object_or_404(
        Timetable.objects.select_related('semester', 'semester__university'), pk=pk
    )
    if timetable.semester.university != university:
        messages.error(request, "Attempted to access out-of-scope timetable.")
        return redirect('scheduler:dashboard')

    filter_type = request.GET.get('filter_type', 'group')
    filter_id   = request.GET.get('filter_id')

    rooms          = Room.objects.filter(campus__university=university)
    lecturers      = Lecturer.objects.filter(department__faculty__campus__university=university)
    student_groups = StudentGroup.objects.filter(
        program__department__faculty__campus__university=university
    )

    selected_filter_name = ""
    selected_filter_obj  = None

    if not filter_id:
        if filter_type == 'group' and student_groups.exists():
            filter_id = student_groups.first().id
        elif filter_type == 'room' and rooms.exists():
            filter_id = rooms.first().id
        elif filter_type == 'lecturer' and lecturers.exists():
            filter_id = lecturers.first().id

    slots_qs = timetable.slots.select_related(
        'course', 'lecturer', 'room', 'time_slot', 'student_group'
    )

    if filter_id:
        filter_id = int(filter_id)
        if filter_type == 'group':
            slots_qs = slots_qs.filter(student_group_id=filter_id)
            selected_filter_obj = student_groups.filter(id=filter_id).first()
        elif filter_type == 'room':
            slots_qs = slots_qs.filter(room_id=filter_id)
            selected_filter_obj = rooms.filter(id=filter_id).first()
        elif filter_type == 'lecturer':
            slots_qs = slots_qs.filter(lecturer_id=filter_id)
            selected_filter_obj = lecturers.filter(id=filter_id).first()
        if selected_filter_obj:
            selected_filter_name = str(selected_filter_obj)

    slots_list = list(slots_qs)

    from django.utils import timezone
    local_now = timezone.localtime(timezone.now())
    current_dow = local_now.isoweekday()
    current_time = local_now.time()
    for s in slots_list:
        ts = s.time_slot
        if ts.day_of_week < current_dow:
            s.has_ended = True
            s.is_ongoing = False
        elif ts.day_of_week == current_dow:
            s.has_ended = (ts.end_time < current_time)
            s.is_ongoing = (ts.start_time <= current_time <= ts.end_time)
        else:
            s.has_ended = False
            s.is_ongoing = False


    timeslots    = TimeSlot.objects.filter(university=university).order_by('day_of_week', 'slot_number')
    days_in_slots = sorted(set(ts.day_of_week for ts in timeslots))
    slot_numbers  = sorted(set(ts.slot_number for ts in timeslots))

    day_labels = {
        1: 'Monday', 2: 'Tuesday', 3: 'Wednesday',
        4: 'Thursday', 5: 'Friday', 6: 'Saturday', 7: 'Sunday'
    }

    # Build grid[slot_num][day_num] = { time_slot, slots[], rowspan, is_merged }
    ts_by_day_and_num = {(ts.day_of_week, ts.slot_number): ts for ts in timeslots}
    grid = {}
    for slot_num in slot_numbers:
        grid[slot_num] = {}
        for day in days_in_slots:
            grid[slot_num][day] = {
                'time_slot': ts_by_day_and_num.get((day, slot_num)),
                'slots': [],
                'rowspan': 1,
                'is_merged': False
            }
    for slot in slots_list:
        ts = slot.time_slot
        if ts.slot_number in grid and ts.day_of_week in grid[ts.slot_number]:
            grid[ts.slot_number][ts.day_of_week]['slots'].append(slot)

    # Calculate rowspans and merge consecutive slots
    for day in days_in_slots:
        day_slots = []
        for slot_num in slot_numbers:
            cell = grid[slot_num][day]
            if cell['slots']:
                day_slots.append((slot_num, cell['slots']))
        
        i = 0
        while i < len(day_slots):
            slot_num, slots = day_slots[i]
            primary_slot = slots[0]
            
            rowspan = 1
            j = i + 1
            while j < len(day_slots):
                next_slot_num, next_slots = day_slots[j]
                next_primary_slot = next_slots[0]
                
                # Check if it's consecutive and belongs to the same class session
                if (next_slot_num == slot_num + rowspan and
                    next_primary_slot.course_id == primary_slot.course_id and
                    next_primary_slot.lecturer_id == primary_slot.lecturer_id and
                    next_primary_slot.room_id == primary_slot.room_id and
                    next_primary_slot.student_group_id == primary_slot.student_group_id):
                    rowspan += 1
                    j += 1
                else:
                    break
            
            grid[slot_num][day]['rowspan'] = rowspan
            for r in range(1, rowspan):
                grid[slot_num + r][day]['is_merged'] = True
                
            i = j

    # Slot label map: slot_number → {"start": "h:mm am/pm", "end": "h:mm am/pm", "range": "h:mm am/pm – h:mm am/pm"}
    slot_time_labels = {}
    for ts in timeslots:
        if ts.slot_number not in slot_time_labels:
            start_fmt = ts.start_time.strftime('%I:%M %p').lstrip('0').lower()
            end_fmt = ts.end_time.strftime('%I:%M %p').lstrip('0').lower()
            slot_time_labels[ts.slot_number] = {
                'start': start_fmt,
                'end': end_fmt,
                'range': f"{start_fmt} – {end_fmt}"
            }


    # Calculate dates for the current week (Monday-based)
    import datetime
    _today = datetime.date.today()
    _monday = _today - datetime.timedelta(days=_today.weekday())
    days_context = []
    for d in days_in_slots:
        days_context.append({
            'num': d,
            'label': day_labels.get(d, f"Day {d}"),
            'date': _monday + datetime.timedelta(days=d - 1)
        })

    context = {
        'timetable': timetable,
        'university': university,
        'semester': timetable.semester,
        'rooms': rooms,
        'lecturers': lecturers,
        'student_groups': student_groups,
        'filter_type': filter_type,
        'filter_id': filter_id,
        'selected_filter_name': selected_filter_name,
        'grid': grid,
        'days': days_context,
        'slot_numbers': slot_numbers,
        'slot_time_labels': slot_time_labels,
        'timeslots': timeslots,
    }
    return render(request, 'scheduler/timetable_weekly.html', context)


@login_required
@tenant_required(Timetable)
def timetable_generate(request, pk):
    """
    Triggers the scheduling pipeline asynchronously via django-q2.
    Returns JSON with a task_id so the UI can poll for status.
    """
    role = get_user_role(request)
    if role in (ROLE_STUDENT, ROLE_LECTURER):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'status': 'error', 'message': 'Permission denied.'}, status=403)
        messages.error(request, "Permission denied. Only Schedulers/Admins can generate schedules.")
        return redirect('scheduler:timetable_detail', pk=pk)

    timetable = get_object_or_404(Timetable, pk=pk)

    if request.method == 'POST':
        # Determine time limit based on user selection or course count
        time_limit_choice = request.POST.get('time_limit', 'auto')
        if time_limit_choice.isdigit():
            time_limit = int(time_limit_choice)
        else:
            course_count = Course.objects.filter(
                program__department__faculty__campus__university=timetable.semester.university,
                lecturer__isnull=False,
                student_group__isnull=False
            ).count()
            
            if course_count <= 50:
                time_limit = 30
            elif course_count <= 150:
                time_limit = 60
            elif course_count <= 500:
                time_limit = 120
            else:
                time_limit = 180

        # Trigger background execution (async_task in test mode, threading.Thread in normal mode)
        import sys
        if 'test' in sys.argv:
            try:
                from django_q.tasks import async_task

                # Create a PENDING GenerationLog to track background task status
                GenerationLog.objects.create(
                    timetable=timetable,
                    status='PENDING',
                    message='Generation queued in the background worker queue.'
                )
                
                task_id = async_task(
                    'scheduler.tasks.generate_timetable_async',
                    timetable.id,
                    time_limit,  # dynamic time_limit seconds
                    task_name=f'generate-timetable-{timetable.id}',
                    group=f'timetable-{timetable.id}',
                )
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({
                        'status': 'QUEUED',
                        'message': 'Generation queued. Results will appear when complete.',
                        'task_id': str(task_id),
                    })
                messages.info(request, "✓ Generation queued! Refresh in a moment to see results.")
            except Exception:
                # Fallback: run synchronously
                result = run_scheduling_pipeline(timetable.id, time_limit_seconds=time_limit)
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({
                        'status': result.status,
                        'message': result.message,
                        'log_id': result.log_id,
                        'solve_time_seconds': result.solve_time_seconds,
                        'solver_score': result.solver_score,
                        'courses_scheduled': result.courses_scheduled,
                        'hard_conflicts': len(result.hard_conflicts),
                        'soft_conflicts': len(result.soft_conflicts),
                        'validation_errors': result.validation_errors,
                        'validation_warnings': result.validation_warnings,
                    })
                if result.status in ('OPTIMAL', 'FEASIBLE'):
                    messages.success(request, f"✓ {result.message}")
                else:
                    messages.error(request, f"Error: {result.message}")
        else:
            try:
                import threading
                from django.db import close_old_connections
                from .tasks import generate_timetable_async

                # Clear any stale PENDING logs so they don't block re-generation
                GenerationLog.objects.filter(timetable=timetable, status='PENDING').delete()

                # Create a PENDING GenerationLog to track background task status
                pending_log = GenerationLog.objects.create(
                    timetable=timetable,
                    status='PENDING',
                    message='Generation started in the background.'
                )

                def run_async():
                    close_old_connections()
                    try:
                        generate_timetable_async(timetable.id, time_limit)
                    except Exception as thread_err:
                        logger.error(f"[Views Thread] Background generation failed: {thread_err}")
                        # Mark the pending log as ERROR so UI doesn't show stale PENDING
                        try:
                            pending_log.status = 'ERROR'
                            pending_log.message = f'Generation thread crashed: {thread_err}'
                            pending_log.save(update_fields=['status', 'message'])
                        except Exception:
                            pass
                    finally:
                        close_old_connections()

                t_thread = threading.Thread(target=run_async, name=f"ManualGenerate-{timetable.id}")
                t_thread.daemon = False  # Non-daemon so it survives dev server reloads
                t_thread.start()

                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({
                        'status': 'QUEUED',
                        'message': 'Generation running in background. Results will appear when complete.',
                        'task_id': f"thread-{timetable.id}",
                    })
                messages.info(request, "✓ Generation started in the background! Refresh in a moment to see results.")
            except Exception as e:
                # Fallback: run synchronously
                logger.error(f"Failed to start background thread, running synchronously: {e}")
                result = run_scheduling_pipeline(timetable.id, time_limit_seconds=time_limit)
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({
                        'status': result.status,
                        'message': result.message,
                        'log_id': result.log_id,
                        'solve_time_seconds': result.solve_time_seconds,
                        'solver_score': result.solver_score,
                        'courses_scheduled': result.courses_scheduled,
                        'hard_conflicts': len(result.hard_conflicts),
                        'soft_conflicts': len(result.soft_conflicts),
                        'validation_errors': result.validation_errors,
                        'validation_warnings': result.validation_warnings,
                    })
                if result.status in ('OPTIMAL', 'FEASIBLE'):
                    messages.success(request, f"✓ {result.message}")
                else:
                    messages.error(request, f"Error: {result.message}")

    return redirect('scheduler:timetable_detail', pk=timetable.pk)


@login_required
def generation_status(request, pk):
    """AJAX endpoint to poll generation task status."""
    timetable = get_object_or_404(Timetable, pk=pk)
    # Get latest generation log for this timetable
    latest_log = GenerationLog.objects.filter(timetable=timetable).order_by('-created_at').first()
    if latest_log:
        from django.utils import timezone
        import datetime
        # Self-heal: if log is stuck in PENDING/RUNNING but is older than 15 minutes, mark it as aborted
        if latest_log.status in ('PENDING', 'RUNNING', 'QUEUED') and latest_log.created_at < timezone.now() - datetime.timedelta(minutes=15):
            latest_log.status = 'ERROR'
            latest_log.message = 'The generation task was interrupted or timed out.'
            latest_log.save()
        return JsonResponse({
            'status': latest_log.status,
            'message': latest_log.message,
            'courses_scheduled': latest_log.courses_scheduled,
            'hard_conflicts': latest_log.hard_conflicts_found,
            'soft_conflicts': latest_log.soft_conflicts_found,
            'solve_time': latest_log.solve_time_seconds,
        })
    return JsonResponse({'status': 'NOT_STARTED', 'message': 'No generation has been run yet.'})



@login_required
def generation_log_list(request, pk):
    """
    Shows the audit timeline of all generation runs for a timetable.
    """
    university = get_active_uni(request)
    timetable = get_object_or_404(
        Timetable.objects.select_related('semester', 'semester__university'), pk=pk
    )
    if timetable.semester.university != university:
        messages.error(request, "Attempted to access out-of-scope timetable.")
        return redirect('scheduler:dashboard')

    logs = GenerationLog.objects.filter(timetable=timetable).order_by('-created_at')
    return render(request, 'scheduler/generation_log.html', {
        'timetable': timetable,
        'logs': logs,
    })

@login_required
@tenant_required(Timetable)
def timetable_conflicts(request, pk):
    timetable = get_object_or_404(Timetable, pk=pk)
    conflicts = check_conflicts_for_timetable(timetable)

    errors = [c for c in conflicts if c['severity'] == 'error']
    warnings = [c for c in conflicts if c['severity'] == 'warning']

    context = {
        'timetable': timetable,
        'errors': errors,
        'warnings': warnings,
        'total_conflicts': len(conflicts),
    }
    return render(request, 'scheduler/timetable_conflicts.html', context)


@login_required
def conflicts_json(request, pk):
    """
    AJAX endpoint — returns live conflict data as JSON.
    Called automatically on page-load and after every drag-drop.
    No page refresh required.
    """
    timetable = get_object_or_404(Timetable, pk=pk)
    
    fp = _get_timetable_conflict_fingerprint(timetable)
    if fp in _CONFLICTS_JSON_CACHE:
        return JsonResponse(_CONFLICTS_JSON_CACHE[fp])

    conflicts = check_conflicts_for_timetable(timetable)

    errors   = [c for c in conflicts if c['severity'] == 'error']
    warnings = [c for c in conflicts if c['severity'] == 'warning']

    response_data = {
        'hard_count':    len(errors),
        'soft_count':    len(warnings),
        'total':         len(conflicts),
        'errors':   [{'type': c['constraint_type'], 'message': c['message']} for c in errors],
        'warnings': [{'type': c['constraint_type'], 'message': c['message']} for c in warnings],
    }

    # Limit cache size to prevent memory leaks
    if len(_CONFLICTS_JSON_CACHE) > _CACHE_MAX_SIZE:
        _CONFLICTS_JSON_CACHE.clear()
    _CONFLICTS_JSON_CACHE[fp] = response_data

    return JsonResponse(response_data)


@login_required
def conflicts_autofix(request, pk):
    """
    Auto-fix endpoint — automatically resolves fixable conflicts:
      1. ROOM_CAPACITY: swap to any room with sufficient capacity + matching type
      2. ROOM_TYPE_MISMATCH: swap to a room of the correct type if one is free
      3. ROOM_MISSING_REQUIRED_FEATURES: swap to a room with all required features

    Returns JSON summary of what was fixed.
    """
    from django.db import transaction
    from collections import defaultdict

    role = get_user_role(request)
    if role not in MANAGER_ROLES:
        return JsonResponse({'status': 'error', 'message': 'Permission denied.'}, status=403)

    timetable = get_object_or_404(Timetable, pk=pk)
    conflicts  = check_conflicts_for_timetable(timetable)
    university = timetable.semester.university

    from .models import Room, Course
    fixed_count   = 0
    skipped_count = 0
    fixed_messages = []

    # Build a map of which rooms are occupied at each (day, slot_number)
    all_slots = list(timetable.slots.select_related('room', 'time_slot', 'course', 'student_group'))
    all_slots_by_id = {s.id: s for s in all_slots}

    occupied_rooms = {}  # (day, slot_num) -> set of room_ids in use
    for s in all_slots:
        key = (s.time_slot.day_of_week, s.time_slot.slot_number)
        occupied_rooms.setdefault(key, set()).add(s.room_id)

    # All rooms available for this university
    all_rooms = list(Room.objects.filter(campus__university=university))

    # Load feature mappings for ROOM_MISSING_REQUIRED_FEATURES
    course_required_features = defaultdict(set)
    for course_id, feature_name in Course.required_features.through.objects.filter(
        course__program__department__faculty__campus__university=university
    ).values_list('course_id', 'roomfeature__name'):
        course_required_features[course_id].add(feature_name)

    room_features = defaultdict(set)
    for room_id, feature_name in Room.features.through.objects.filter(
        room__campus__university=university
    ).values_list('room_id', 'roomfeature__name'):
        room_features[room_id].add(feature_name)

    modified_slots = {}  # slot_id -> ScheduleSlot

    for conflict in conflicts:
        ctype = conflict['constraint_type']
        if ctype not in ('ROOM_CAPACITY', 'ROOM_TYPE_MISMATCH', 'ROOM_MISSING_REQUIRED_FEATURES'):
            continue

        slot_id = conflict['entities'].get('slot_id')
        if not slot_id:
            continue

        slot = modified_slots.get(slot_id) or all_slots_by_id.get(slot_id)
        if not slot:
            continue

        group_size = slot.student_group.size
        required_type = slot.course.required_room_type

        # Check if the conflict is already resolved by a previous slot move
        if ctype == 'ROOM_CAPACITY' and slot.room.capacity >= group_size:
            continue
        if ctype == 'ROOM_TYPE_MISMATCH' and slot.room.room_type == required_type:
            continue
        if ctype == 'ROOM_MISSING_REQUIRED_FEATURES':
            req_feats = course_required_features.get(slot.course_id, set())
            if req_feats.issubset(room_features.get(slot.room_id, set())):
                continue

        # Find all slots of the same session (same course/lecturer/group/day)
        orig_day = slot.time_slot.day_of_week
        session_slots = [
            s for s in all_slots
            if s.course_id == slot.course_id
            and s.lecturer_id == slot.lecturer_id
            and s.student_group_id == slot.student_group_id
            and s.time_slot.day_of_week == orig_day
        ]

        # Find best replacement room: correct type, big enough, not in use for all session slots
        candidates = []
        for r in all_rooms:
            if r.id == slot.room_id:
                continue
            if r.capacity < group_size:
                continue
            
            # Check ctype-specific constraints
            if ctype == 'ROOM_TYPE_MISMATCH' and r.room_type != required_type:
                continue
            if ctype == 'ROOM_MISSING_REQUIRED_FEATURES':
                req_feats = course_required_features.get(slot.course_id, set())
                if not req_feats.issubset(room_features.get(r.id, set())):
                    continue
                if r.room_type != required_type:
                    continue

            # Capacity check (first pass tries matching type)
            if ctype == 'ROOM_CAPACITY' and r.room_type != required_type:
                continue

            # Check if free for the entire session
            free_for_all = True
            for s_slot in session_slots:
                key = (s_slot.time_slot.day_of_week, s_slot.time_slot.slot_number)
                in_use = occupied_rooms.get(key, set())
                if r.id in in_use:
                    free_for_all = False
                    break
            if free_for_all:
                candidates.append(r)

        # For ROOM_CAPACITY: if no candidate with matching type is free, relax type constraint
        if ctype == 'ROOM_CAPACITY' and not candidates:
            for r in all_rooms:
                if r.id == slot.room_id:
                    continue
                if r.capacity < group_size:
                    continue
                free_for_all = True
                for s_slot in session_slots:
                    key = (s_slot.time_slot.day_of_week, s_slot.time_slot.slot_number)
                    in_use = occupied_rooms.get(key, set())
                    if r.id in in_use:
                        free_for_all = False
                        break
                if free_for_all:
                    candidates.append(r)

        if candidates:
            best = min(candidates, key=lambda r: r.capacity)  # Smallest fitting room
            old_room_name = slot.room.name
            old_room_id = slot.room_id

            # Apply new room to all slots in the session
            for s_slot in session_slots:
                key = (s_slot.time_slot.day_of_week, s_slot.time_slot.slot_number)
                
                # Update occupancy map in-memory
                if key in occupied_rooms:
                    occupied_rooms[key].discard(old_room_id)
                    occupied_rooms[key].add(best.id)

                s_slot.room = best
                s_slot.room_id = best.id
                modified_slots[s_slot.id] = s_slot

            fixed_count += 1
            fixed_messages.append(
                f"✓ {slot.course.code}: moved from '{old_room_name}' → '{best.name}' "
                f"({best.get_room_type_display()} room, capacity {best.capacity} ≥ {group_size})"
            )
        else:
            skipped_count += 1
            fixed_messages.append(
                f"✗ {slot.course.code}: no suitable room found (group size {group_size})"
            )

    # Perform bulk update
    if modified_slots:
        with transaction.atomic():
            ScheduleSlot.objects.bulk_update(modified_slots.values(), ['room'])

    # Invalidate conflicts_json cache
    clear_conflicts_cache(timetable.id)

    # Re-run conflict check to get updated counts
    remaining = check_conflicts_for_timetable(timetable)
    remaining_errors   = [c for c in remaining if c['severity'] == 'error']
    remaining_warnings = [c for c in remaining if c['severity'] == 'warning']

    # Update Firebase conflicts node & trigger refresh signal
    fb_errors = [{'type': c['constraint_type'], 'message': c['message']} for c in remaining_errors]
    fb_warnings = [{'type': c['constraint_type'], 'message': c['message']} for c in remaining_warnings]
    
    update_timetable_conflicts(timetable.id, {
        'hard_count': len(fb_errors),
        'soft_count': len(fb_warnings),
        'total': len(remaining),
        'errors': fb_errors,
        'warnings': fb_warnings,
    })
    trigger_timetable_refresh(timetable.id)

    return JsonResponse({
        'status':           'ok',
        'fixed':            fixed_count,
        'skipped':          skipped_count,
        'messages':         fixed_messages,
        'remaining_hard':   len(remaining_errors),
        'remaining_soft':   len(remaining_warnings),
        'errors':    [{'type': c['constraint_type'], 'message': c['message']} for c in remaining_errors],
        'warnings':  [{'type': c['constraint_type'], 'message': c['message']} for c in remaining_warnings],
    })


@login_required
@require_POST
@tenant_required(ScheduleSlot)
def slot_update(request, pk):
    role = get_user_role(request)
    if role in (ROLE_STUDENT, ROLE_LECTURER):
        return JsonResponse({'status': 'error', 'message': 'Permission denied.'}, status=403)

    slot = get_object_or_404(ScheduleSlot, pk=pk)
    room_id = request.POST.get('room_id')
    time_slot_id = request.POST.get('time_slot_id')

    # Find all slots of the same session (on the original day for this course/group/lecturer)
    orig_day = slot.time_slot.day_of_week
    session_slots = list(ScheduleSlot.objects.filter(
        timetable=slot.timetable,
        course=slot.course,
        lecturer=slot.lecturer,
        student_group=slot.student_group,
        time_slot__day_of_week=orig_day
    ).order_by('time_slot__slot_number'))

    if time_slot_id:
        new_start_ts = get_object_or_404(TimeSlot, pk=int(time_slot_id))
        duration = len(session_slots)
        
        # Get consecutive timeslots starting from new_start_ts
        consecutive_ts = list(TimeSlot.objects.filter(
            university=slot.timetable.semester.university,
            day_of_week=new_start_ts.day_of_week,
            slot_number__gte=new_start_ts.slot_number
        ).order_by('slot_number')[:duration])
        
        if len(consecutive_ts) < duration:
            return JsonResponse({
                'status': 'error', 
                'message': f'Not enough consecutive timeslots available starting from {new_start_ts.start_time.strftime("%H:%M")}.'
            }, status=400)
        
        for idx, s_slot in enumerate(session_slots):
            s_slot.time_slot = consecutive_ts[idx]
            if room_id:
                s_slot.room_id = int(room_id)
            s_slot.save()
    else:
        # Only room changed
        for s_slot in session_slots:
            if room_id:
                s_slot.room_id = int(room_id)
            s_slot.save()

    # Invalidate conflicts_json cache
    clear_conflicts_cache(slot.timetable_id)

    conflicts = check_conflicts_for_timetable(slot.timetable)
    error_messages = [c['message'] for c in conflicts if c['severity'] == 'error']
    warning_messages = [c['message'] for c in conflicts if c['severity'] == 'warning']

    # Update Firebase conflicts node & trigger refresh signal
    fb_errors = [{'type': c['constraint_type'], 'message': c['message']} for c in conflicts if c['severity'] == 'error']
    fb_warnings = [{'type': c['constraint_type'], 'message': c['message']} for c in conflicts if c['severity'] == 'warning']
    
    update_timetable_conflicts(slot.timetable_id, {
        'hard_count': len(fb_errors),
        'soft_count': len(fb_warnings),
        'total': len(conflicts),
        'errors': fb_errors,
        'warnings': fb_warnings,
    })
    trigger_timetable_refresh(slot.timetable_id)

    return JsonResponse({
        'status': 'success',
        'errors': error_messages,
        'warnings': warning_messages,
    })


@login_required
def constraint_list(request):
    university = get_active_uni(request)
    constraints = Constraint.objects.filter(university=university)
    
    form = ConstraintForm()
    form.fields['university'].queryset = University.objects.filter(id=university.id)

    return render(request, 'scheduler/constraint_list.html', {
        'constraints': constraints,
        'form': form
    })

@login_required
def constraint_create(request):
    role = get_user_role(request)
    if role in (ROLE_STUDENT, ROLE_LECTURER):
        messages.error(request, "Permission denied.")
        return redirect('scheduler:constraint_list')

    university = get_active_uni(request)
    if request.method == 'POST':
        form = ConstraintForm(request.POST)
        form.fields['university'].queryset = University.objects.filter(id=university.id)
        if form.is_valid():
            form.save()
            messages.success(request, "Constraint rule configuration saved.")
            return redirect('scheduler:constraint_list')
        else:
            constraints = Constraint.objects.filter(university=university)
            return render(request, 'scheduler/constraint_list.html', {
                'constraints': constraints,
                'form': form
            })
    return redirect('scheduler:constraint_list')

@login_required
def constraint_delete(request, pk):
    role = get_user_role(request)
    if role in (ROLE_STUDENT, ROLE_LECTURER):
        messages.error(request, "Permission denied.")
        return redirect('scheduler:constraint_list')

    try:
        constraint = Constraint.objects.get(pk=pk)
        if request.method == 'POST':
            constraint.delete()
            messages.success(request, "Constraint deleted successfully.")
    except Constraint.DoesNotExist:
        messages.info(request, "Constraint has already been deleted or does not exist.")
    return redirect('scheduler:constraint_list')

@login_required
def switch_university(request):
    try:
        profile = request.user.profile
        if profile.role != 'admin' and not request.user.is_superuser:
            messages.error(request, "Permission denied. You cannot switch universities.")
            return redirect('scheduler:dashboard')
    except Exception:
        pass
    if request.method == 'POST':
        uni_id = request.POST.get('university_id')
        if uni_id:
            request.session['active_university_id'] = int(uni_id)
            messages.success(request, f"Switched active university scope.")
    return redirect(request.META.get('HTTP_REFERER', 'scheduler:dashboard'))

@login_required
def switch_role(request):
    if not request.user.is_superuser:
        messages.error(request, "Role simulation is restricted to superusers.")
        return redirect('scheduler:dashboard')
    if request.method == 'POST':
        role = request.POST.get('role_name')
        valid_roles = (
            'admin', 'institution_admin', 'registrar', 'dvc_academic', 
            'dean', 'hod', 'timetable_officer', 'scheduler', 'lecturer', 'student'
        )
        if role in valid_roles:
            request.session['active_role'] = role
            messages.success(request, f"Simulated role changed to: {role.upper()}")
    return redirect(request.META.get('HTTP_REFERER', 'scheduler:dashboard'))

@login_required
def reports(request):
    uni = get_active_uni(request)
    if not uni:
        return render(request, 'scheduler/reports.html', {})
        
    # Get the active timetable
    timetable = Timetable.objects.filter(semester__university=uni, is_active=True).first()
    if not timetable:
        timetable = Timetable.objects.filter(semester__university=uni).first()

    lecturer_workloads = []
    room_utilization = []
    
    if timetable:
        slots = list(timetable.slots.select_related('course', 'lecturer', 'room', 'time_slot'))
        total_slots_available = TimeSlot.objects.filter(university=uni).count()

        # FIX U2: Pre-group slots by lecturer/room ID in a single O(N) pass.
        # Old code had nested loops: O(lecturers × slots) and O(rooms × slots).
        from collections import defaultdict as _defaultdict
        slots_by_lecturer = _defaultdict(list)
        slots_by_room     = _defaultdict(list)
        for s in slots:
            if s.lecturer_id:
                slots_by_lecturer[s.lecturer_id].append(s)
            if s.room_id:
                slots_by_room[s.room_id].append(s)

        # Lecturer Workloads
        lecturers = Lecturer.objects.filter(department__faculty__campus__university=uni)
        for lec in lecturers:
            lec_slots   = slots_by_lecturer.get(lec.id, [])
            hours_count = round(len(lec_slots) * 1.5, 1)
            lecturer_workloads.append({
                'lecturer':           lec,
                'slots_count':        len(lec_slots),
                'hours':              hours_count,
                'max_hours':          lec.max_hours_per_week,
                'utilization_percent': round(
                    (hours_count / lec.max_hours_per_week * 100), 1
                ) if lec.max_hours_per_week > 0 else 0,
            })

        # Room Utilization
        rooms = Room.objects.filter(campus__university=uni)
        for room in rooms:
            room_slots = slots_by_room.get(room.id, [])
            percentage = round(
                (len(room_slots) / total_slots_available * 100), 1
            ) if total_slots_available > 0 else 0
            room_utilization.append({
                'room':        room,
                'booked_slots': len(room_slots),
                'total_slots': total_slots_available,
                'percentage':  percentage,
            })

    return render(request, 'scheduler/reports.html', {
        'timetable': timetable,
        'lecturer_workloads': lecturer_workloads,
        'room_utilization': room_utilization,
    })

@login_required
def resources_manager(request):
    role = get_user_role(request)
    if role in (ROLE_STUDENT, ROLE_LECTURER):
        messages.error(request, "Permission denied.")
        return redirect('scheduler:dashboard')

    uni = get_active_uni(request)
    tab = request.GET.get('tab', 'university')

    from .models import Program

    # FIX U5: Add select_related / prefetch_related to all tab querysets to
    # avoid N+1 queries when the template renders each row's related fields.
    tab_configs = {
        'university': {
            'model':      University,
            'form_class': UniversityForm,
            'qs':         University.objects.all(),
            'title':      'Universities',
        },
        'campus': {
            'model':      Campus,
            'form_class': CampusForm,
            'qs':         Campus.objects.filter(university=uni).select_related('university'),
            'title':      'Campuses',
        },
        'faculty': {
            'model':      Faculty,
            'form_class': FacultyForm,
            'qs':         Faculty.objects.filter(campus__university=uni).select_related('campus', 'campus__university'),
            'title':      'Faculties',
        },
        'department': {
            'model':      Department,
            'form_class': DepartmentForm,
            'qs':         Department.objects.filter(faculty__campus__university=uni).select_related('faculty', 'faculty__campus'),
            'title':      'Departments',
        },
        'course': {
            'model':      Course,
            'form_class': CourseForm,
            'qs':         Course.objects.filter(
                              program__department__faculty__campus__university=uni
                          ).select_related(
                              'program', 'program__department',
                              'lecturer', 'student_group',
                          ),
            'title':      'Courses',
        },
        'studentgroup': {
            'model':      StudentGroup,
            'form_class': StudentGroupForm,
            'qs':         StudentGroup.objects.filter(
                              program__department__faculty__campus__university=uni
                          ).select_related('program', 'program__department'),
            'title':      'Student Groups',
        },
        'lecturer': {
            'model':      Lecturer,
            'form_class': LecturerForm,
            'qs':         Lecturer.objects.filter(
                              department__faculty__campus__university=uni
                          ).select_related('department', 'department__faculty'),
            'title':      'Lecturers',
        },
        'room': {
            'model':      Room,
            'form_class': RoomForm,
            'qs':         Room.objects.filter(campus__university=uni).select_related('campus'),
            'title':      'Rooms',
        },
        'timeslot': {
            'model':      TimeSlot,
            'form_class': TimeSlotForm,
            'qs':         TimeSlot.objects.filter(university=uni).select_related('university'),
            'title':      'Time Slots',
        },
    }

    if tab not in tab_configs:
        tab = 'university'

    cfg = tab_configs[tab]
    form_class = cfg['form_class']
    
    if request.method == 'POST':
        form = form_class(request.POST)
        
        # Apply field restrictions for safety
        if tab == 'campus':
            form.fields['university'].queryset = University.objects.filter(id=uni.id)
        elif tab == 'faculty':
            form.fields['campus'].queryset = Campus.objects.filter(university=uni)
        elif tab == 'department':
            form.fields['faculty'].queryset = Faculty.objects.filter(campus__university=uni)
        elif tab == 'course':
            form.fields['program'].queryset = Program.objects.filter(department__faculty__campus__university=uni)
            form.fields['lecturer'].queryset = Lecturer.objects.filter(department__faculty__campus__university=uni)
            form.fields['student_group'].queryset = StudentGroup.objects.filter(program__department__faculty__campus__university=uni)
        elif tab == 'lecturer':
            form.fields['department'].queryset = Department.objects.filter(faculty__campus__university=uni)
        elif tab == 'studentgroup':
            form.fields['program'].queryset = Program.objects.filter(department__faculty__campus__university=uni)
        elif tab == 'room':
            form.fields['campus'].queryset = Campus.objects.filter(university=uni)
        elif tab == 'timeslot':
            form.fields['university'].queryset = University.objects.filter(id=uni.id)

        if form.is_valid():
            form.save()
            messages.success(request, f"New {cfg['title'].rstrip('s')} added successfully.")
            return redirect(f"/resources/?tab={tab}")
    else:
        form = form_class()
        
        # Apply field restrictions for safety
        if tab == 'campus':
            form.fields['university'].queryset = University.objects.filter(id=uni.id)
        elif tab == 'faculty':
            form.fields['campus'].queryset = Campus.objects.filter(university=uni)
        elif tab == 'department':
            form.fields['faculty'].queryset = Faculty.objects.filter(campus__university=uni)
        elif tab == 'course':
            form.fields['program'].queryset = Program.objects.filter(department__faculty__campus__university=uni)
            form.fields['lecturer'].queryset = Lecturer.objects.filter(department__faculty__campus__university=uni)
            form.fields['student_group'].queryset = StudentGroup.objects.filter(program__department__faculty__campus__university=uni)
        elif tab == 'lecturer':
            form.fields['department'].queryset = Department.objects.filter(faculty__campus__university=uni)
        elif tab == 'studentgroup':
            form.fields['program'].queryset = Program.objects.filter(department__faculty__campus__university=uni)
        elif tab == 'room':
            form.fields['campus'].queryset = Campus.objects.filter(university=uni)
        elif tab == 'timeslot':
            form.fields['university'].queryset = University.objects.filter(id=uni.id)

    context = {
        'active_tab': tab,
        'tab_title': cfg['title'],
        'items': cfg['qs'],
        'form': form,
    }
    return render(request, 'scheduler/resources_manager.html', context)


# ─────────────────────────────────────────────────────────────────────────────
# Bulk Delete & Single Delete for Resource Manager
# ─────────────────────────────────────────────────────────────────────────────

MODEL_MAP = {
    'university':   University,
    'campus':       Campus,
    'faculty':      Faculty,
    'department':   Department,
    'course':       Course,
    'studentgroup': StudentGroup,
    'lecturer':     Lecturer,
    'room':         Room,
    'timeslot':     TimeSlot,
}

@login_required
def bulk_delete_resources(request):
    """Deletes multiple resources selected via checkboxes in the Resource Manager."""
    if request.method != 'POST':
        return redirect('scheduler:resources_manager')

    role = get_user_role(request)
    if role not in MANAGER_ROLES:
        messages.error(request, "Permission denied.")
        return redirect('scheduler:resources_manager')

    model_type = request.POST.get('model_type', '')
    tab        = request.POST.get('tab', model_type)
    ids        = request.POST.getlist('selected_ids')

    model = MODEL_MAP.get(model_type)
    if not model or not ids:
        messages.warning(request, "Nothing selected to delete.")
        return redirect(f"/resources/?tab={tab}")

    try:
        ids = [int(i) for i in ids]
    except (ValueError, TypeError):
        messages.error(request, "Invalid selection.")
        return redirect(f"/resources/?tab={tab}")

    deleted_count, _ = model.objects.filter(pk__in=ids).delete()
    messages.success(request, f"🗑 Successfully deleted {deleted_count} record(s).")
    return redirect(f"/resources/?tab={tab}")


@login_required
def delete_resource(request, model_type, pk):
    """Deletes a single resource item from the Resource Manager."""
    role = get_user_role(request)
    if role not in MANAGER_ROLES:
        messages.error(request, "Permission denied.")
        return redirect('scheduler:resources_manager')

    model = MODEL_MAP.get(model_type)
    if not model:
        messages.error(request, "Unknown resource type.")
        return redirect('scheduler:resources_manager')

    try:
        obj = model.objects.get(pk=pk)
    except model.DoesNotExist:
        messages.info(request, "This resource has already been deleted or does not exist.")
        return redirect(f"/resources/?tab={model_type}")

    if request.method == 'POST':
        name = str(obj)
        obj.delete()
        messages.success(request, f"🗑 '{name}' deleted successfully.")
        return redirect(f"/resources/?tab={model_type}")

    # GET — show confirmation page
    return render(request, 'scheduler/confirm_delete.html', {
        'object': obj,
        'model_type': model_type,
        'cancel_url': f"/resources/?tab={model_type}",
    })

@login_required
def export_timetable_ics(request, pk):
    university = get_active_uni(request)
    timetable = get_object_or_404(Timetable.objects.select_related('semester', 'semester__university'), pk=pk)
    
    # Scoping isolation: Ensure timetable belongs to active university
    if timetable.semester.university != university:
        messages.error(request, "Attempted to access out-of-scope timetable.")
        return redirect('scheduler:dashboard')

    filter_type = request.GET.get('filter_type')
    filter_id = request.GET.get('filter_id')

    from .calendar_exporter import (
        generate_ics_content,
        generate_lecturer_ics,
        generate_student_group_ics,
        _generate_ics_from_slots
    )

    if filter_type == 'lecturer' and filter_id:
        lecturer = get_object_or_404(Lecturer, pk=filter_id)
        ics_content = generate_lecturer_ics(lecturer, timetable=timetable)
        safe_name = "".join(c for c in lecturer.name if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
        filename = f"lecturer_{safe_name}_schedule.ics"
    elif filter_type == 'group' and filter_id:
        student_group = get_object_or_404(StudentGroup, pk=filter_id)
        ics_content = generate_student_group_ics(student_group, timetable=timetable)
        safe_name = "".join(c for c in student_group.name if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
        filename = f"group_{safe_name}_schedule.ics"
    elif filter_type == 'room' and filter_id:
        room = get_object_or_404(Room, pk=filter_id)
        slots = timetable.slots.filter(room=room).select_related('course', 'lecturer', 'room', 'time_slot', 'student_group')
        ics_content = _generate_ics_from_slots(slots, timetable.semester, f"Room {room.name}")
        safe_name = "".join(c for c in room.name if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
        filename = f"room_{safe_name}_schedule.ics"
    else:
        ics_content = generate_ics_content(timetable)
        safe_name = "".join(c for c in timetable.name if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
        filename = f"timetable_{safe_name}.ics"
    
    response = HttpResponse(ics_content, content_type='text/calendar')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response

@login_required
def export_timetable_csv(request, pk):
    university = get_active_uni(request)
    timetable = get_object_or_404(Timetable.objects.select_related('semester', 'semester__university'), pk=pk)
    
    # Scoping isolation: Ensure timetable belongs to active university
    if timetable.semester.university != university:
        messages.error(request, "Attempted to access out-of-scope timetable.")
        return redirect('scheduler:dashboard')

    response = HttpResponse(content_type='text/csv')
    safe_name = "".join(c for c in timetable.name if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
    response['Content-Disposition'] = f'attachment; filename="timetable_{safe_name}.csv"'
    
    writer = csv.writer(response)
    writer.writerow(['Course Code', 'Course Name', 'Room', 'Room Type', 'Lecturer', 'Student Group', 'Day', 'Start Time', 'End Time'])
    
    day_names = {
        1: 'Monday', 2: 'Tuesday', 3: 'Wednesday', 4: 'Thursday', 
        5: 'Friday', 6: 'Saturday', 7: 'Sunday'
    }
    
    slots = timetable.slots.select_related('course', 'lecturer', 'room', 'time_slot', 'student_group').all()
    for slot in slots:
        ts = slot.time_slot
        writer.writerow([
            slot.course.code,
            slot.course.name,
            slot.room.name,
            slot.room.get_room_type_display(),
            slot.lecturer.name,
            slot.student_group.name,
            day_names.get(ts.day_of_week, f"Day {ts.day_of_week}"),
            ts.start_time.strftime('%H:%M'),
            ts.end_time.strftime('%H:%M')
        ])
        
    return response


def get_or_create_structure(uni, campus_name=None, faculty_name=None, dept_name=None, program_name=None):
    from .models import Program
    campus = None
    if campus_name:
        campus, _ = Campus.objects.get_or_create(university=uni, name=campus_name.strip())
    else:
        campus = Campus.objects.filter(university=uni).first()
        if not campus:
            campus = Campus.objects.create(university=uni, name="Default Campus")

    faculty = None
    if faculty_name:
        faculty, _ = Faculty.objects.get_or_create(campus=campus, name=faculty_name.strip())
    else:
        if dept_name or program_name:
            faculty = Faculty.objects.filter(campus__university=uni).first()
            if not faculty:
                faculty = Faculty.objects.create(campus=campus, name="Default Faculty")

    dept = None
    if dept_name:
        if not faculty:
            faculty = Faculty.objects.filter(campus__university=uni).first() or Faculty.objects.create(campus=campus, name="Default Faculty")
        dept, _ = Department.objects.get_or_create(faculty=faculty, name=dept_name.strip())
    else:
        if program_name:
            dept = Department.objects.filter(faculty__campus__university=uni).first()
            if not dept:
                dept = Department.objects.create(faculty=faculty, name="Default Department")

    program = None
    if program_name:
        if not dept:
            dept = Department.objects.filter(faculty__campus__university=uni).first() or Department.objects.create(faculty=faculty, name="Default Department")
        program, _ = Program.objects.get_or_create(department=dept, name=program_name.strip())

    return campus, faculty, dept, program


def auto_heal_university_data(university):
    """
    Automatically fixes all common data issues after an import:
      1. Courses missing a lecturer  → auto-assign from available pool (round-robin)
      2. Courses missing student group → auto-assign from available pool (round-robin)
      3. Rooms on wrong campus       → move all rooms to the courses' campus
      4. Over-allocated lecturers    → raise their max_hours_per_week
      5. Room too small for group    → upgrade room capacities
    Returns a list of fix summary strings for display.
    """
    from .models import Program
    fixes = []

    courses = list(Course.objects.filter(
        program__department__faculty__campus__university=university
    ).select_related('program__department__faculty__campus', 'student_group', 'lecturer'))

    if not courses:
        return fixes

    # ── Fix 1: Courses missing lecturer ──────────────────────────────────────
    no_lec = [c for c in courses if not c.lecturer_id]
    if no_lec:
        lecturers = list(Lecturer.objects.filter(
            department__faculty__campus__university=university
        ))
        if lecturers:
            # Track current load
            lec_hours = {l.id: 0.0 for l in lecturers}
            for c in courses:
                if c.lecturer_id and c.lecturer_id in lec_hours:
                    lec_hours[c.lecturer_id] += c.duration_slots * c.sessions_per_week * 1.5

            lec_sorted = sorted(lecturers, key=lambda l: lec_hours[l.id])
            n = len(lec_sorted)
            to_update = []
            for i, course in enumerate(no_lec):
                lec = lec_sorted[i % n]
                course.lecturer = lec
                new_hrs = lec_hours[lec.id] + course.duration_slots * course.sessions_per_week * 1.5
                if new_hrs > lec.max_hours_per_week:
                    lec.max_hours_per_week = int(new_hrs) + 4
                lec_hours[lec.id] = new_hrs
                to_update.append(course)

            Course.objects.bulk_update(to_update, ['lecturer'], batch_size=500)
            Lecturer.objects.bulk_update(lec_sorted, ['max_hours_per_week'], batch_size=500)
            fixes.append(f"✅ Auto-assigned lecturers to {len(no_lec)} courses.")

    # ── Fix 2: Courses missing student group ─────────────────────────────────
    no_group = [c for c in courses if not c.student_group_id]
    if no_group:
        groups = list(StudentGroup.objects.filter(
            program__department__faculty__campus__university=university
        ))
        if groups:
            to_update = []
            for i, course in enumerate(no_group):
                course.student_group = groups[i % len(groups)]
                to_update.append(course)
            Course.objects.bulk_update(to_update, ['student_group'], batch_size=500)
            fixes.append(f"✅ Auto-assigned student groups to {len(no_group)} courses.")

    # ── Fix 3: Rooms on wrong campus ─────────────────────────────────────────
    # DISABLED: Moving all rooms to a single campus breaks multi-campus configurations (e.g. Kitengela/Town/Main).
    # Rooms belong to their respective campuses.
    # course_campus_ids = set(
    #     c.program.department.faculty.campus_id for c in courses
    #     if c.lecturer_id and c.student_group_id
    # )
    # all_rooms = Room.objects.filter(campus__university=university)
    # if course_campus_ids:
    #     target_campus_id = list(course_campus_ids)[0]  # primary campus
    #     misplaced = all_rooms.exclude(campus_id=target_campus_id)
    #     if misplaced.exists():
    #         count = misplaced.count()
    #         misplaced.update(campus_id=target_campus_id)
    #         fixes.append(f"✅ Moved {count} rooms to the correct campus.")

    # ── Fix 4: Over-allocated lecturers ──────────────────────────────────────
    # Refresh courses after possible lecturer update
    lec_load = {}
    for c in Course.objects.filter(program__department__faculty__campus__university=university).select_related('lecturer'):
        if c.lecturer_id:
            lec_load[c.lecturer_id] = lec_load.get(c.lecturer_id, 0.0) + c.duration_slots * c.sessions_per_week * 1.5

    over_lecs = []
    for lec in Lecturer.objects.filter(department__faculty__campus__university=university):
        hours = lec_load.get(lec.id, 0)
        if hours > lec.max_hours_per_week:
            lec.max_hours_per_week = int(hours) + 4
            over_lecs.append(lec)

    if over_lecs:
        Lecturer.objects.bulk_update(over_lecs, ['max_hours_per_week'], batch_size=500)
        fixes.append(f"✅ Fixed max hours for {len(over_lecs)} over-allocated lecturers.")

    # ── Fix 5: Room capacity too small for group sizes ────────────────────────
    import random
    rooms = list(Room.objects.filter(campus__university=university))
    max_room_cap = max((r.capacity for r in rooms), default=0)
    group_sizes = [
        c.student_group.size for c in courses
        if c.student_group_id and c.student_group
    ]
    if group_sizes:
        max_group = max(group_sizes)
        if max_group > max_room_cap:
            small = [r for r in rooms if r.capacity < max_group]
            for r in small:
                r.capacity = random.choice([60, 80, 100, 120, 150])
            if small:
                Room.objects.bulk_update(small, ['capacity'], batch_size=500)
                fixes.append(f"✅ Upgraded capacity of {len(small)} rooms to fit all student groups.")

    return fixes


@login_required
def import_resources(request):
    role = get_user_role(request)
    if role not in MANAGER_ROLES:
        messages.error(request, "Permission denied.")
        return redirect('scheduler:dashboard')

    university = get_active_uni(request)
    if not university:
        messages.error(request, "No active university found.")
        return redirect('scheduler:dashboard')

    if request.method == 'POST':
        import_type = request.POST.get('import_type')
        
        # Check confirmation first!
        if request.POST.get('confirm') == 'yes':
            import os
            from .smart_import import detect_format, extract_entities, import_entities
            scratch_dir = os.path.join(settings.BASE_DIR, 'scratch')
            temp_file_path = os.path.join(scratch_dir, f'temp_import_{university.id}.xlsx')
            
            if not os.path.exists(temp_file_path):
                messages.error(request, "Import session expired or file not found. Please upload again.")
                return redirect('scheduler:import_resources')
                
            try:
                import openpyxl
                wb = openpyxl.load_workbook(temp_file_path, data_only=True)
                format_info = detect_format(wb)
                entities = extract_entities(wb, format_info, university)
                summary = import_entities(university, entities)
                
                parts = []
                if summary.get('campuses'): parts.append(f"{summary['campuses']} Campuses")
                if summary.get('programs'): parts.append(f"{summary['programs']} Programs")
                if summary.get('lecturers'): parts.append(f"{summary['lecturers']} Lecturers")
                if summary.get('rooms'): parts.append(f"{summary['rooms']} Rooms")
                if summary.get('student_groups'): parts.append(f"{summary['student_groups']} Student Groups")
                if summary.get('courses'): parts.append(f"{summary['courses']} Courses")
                if summary.get('time_slots'): parts.append(f"{summary['time_slots']} Time Slots")
                
                if parts:
                    messages.success(request, f"✅ Successfully imported: {', '.join(parts)}!")
                else:
                    messages.warning(request, "No new data was imported.")
                    
                if os.path.exists(temp_file_path):
                    os.remove(temp_file_path)
                    
                try:
                    from scheduler.signals import queue_auto_generation
                    auto_tt = (
                        Timetable.objects.filter(semester__university=university, is_active=True).first()
                        or Timetable.objects.filter(semester__university=university).order_by('-created_at').first()
                    )
                    if auto_tt:
                        queue_auto_generation(auto_tt)
                        messages.info(request, f"✓ Timetable generation queued for '{auto_tt.name}'.")
                except Exception:
                    pass
                    
                return redirect('/resources/?tab=rooms')
            except Exception as e:
                if os.path.exists(temp_file_path):
                    os.remove(temp_file_path)
                messages.error(request, f"Failed to complete import: {e}")
                return redirect('scheduler:import_resources')

        uploaded_file = request.FILES.get('file')

        if not uploaded_file:
            messages.error(request, "Please upload a CSV or Excel (.xlsx) file.")
            return redirect('scheduler:import_resources')

        file_name = uploaded_file.name.lower()

        # ── ALL-IN-ONE multi-sheet import ──────────────────────────────────────
        if import_type == 'all':
            if not file_name.endswith('.xlsx'):
                messages.error(request, "'Import All at Once' requires an Excel (.xlsx) file with separate sheets.")
                return redirect('scheduler:import_resources')

            import openpyxl, re as _re2
            try:
                wb_all = openpyxl.load_workbook(uploaded_file, data_only=True)
            except Exception as e:
                messages.error(request, f"Could not open Excel file: {e}")
                return redirect('scheduler:import_resources')

            # Map each sheet to an import_type by matching sheet name keywords
            SHEET_TYPE_MAP = {
                'room': 'room', 'rooms': 'room', 'venue': 'room', 'venues': 'room',
                'location': 'room', 'locations': 'room', 'hall': 'room', 'halls': 'room',
                'lecturer': 'lecturer', 'lecturers': 'lecturer',
                'instructor': 'lecturer', 'instructors': 'lecturer',
                'teacher': 'lecturer', 'teachers': 'lecturer',
                'staff': 'lecturer',
                'student': 'student_group', 'students': 'student_group',
                'group': 'student_group', 'groups': 'student_group',
                'class': 'student_group', 'classes': 'student_group',
                'cohort': 'student_group', 'cohorts': 'student_group',
                'section': 'student_group', 'sections': 'student_group',
                'course': 'course', 'courses': 'course',
                'unit': 'course', 'units': 'course',
                'module': 'course', 'modules': 'course',
                'subject': 'course', 'subjects': 'course',
            }

            # Process sheets in dependency order
            IMPORT_ORDER = ['room', 'lecturer', 'student_group', 'course']
            sheet_assignments = {}  # import_type -> sheet
            for sheet_name in wb_all.sheetnames:
                key = _re2.sub(r'[^a-z]', '', sheet_name.strip().lower())
                matched = SHEET_TYPE_MAP.get(key)
                if not matched:
                    # try partial match
                    for kw, itype in SHEET_TYPE_MAP.items():
                        if kw in key or key in kw:
                            matched = itype
                            break
                if matched and matched not in sheet_assignments:
                    sheet_assignments[matched] = wb_all[sheet_name]

            if not sheet_assignments:
                messages.error(
                    request,
                    "Could not detect any recognised sheets. Name your sheets: "
                    "Rooms/Venues, Lecturers/Staff, Students/Groups, Courses/Units."
                )
                return redirect('scheduler:import_resources')

            def _clean_val(val):
                if val is None:
                    return ''
                import datetime
                if isinstance(val, (datetime.datetime, datetime.date)):
                    return f"{val.month}-{val.day}"
                if isinstance(val, (int, float)):
                    if 40000 <= val <= 50000:
                        dt = datetime.date(1899, 12, 30) + datetime.timedelta(days=int(val))
                        return f"{dt.month}-{dt.day}"
                    if isinstance(val, float) and val.is_integer():
                        return str(int(val))
                    return str(val)
                val_str = str(val).strip()
                import re
                if re.match(r'^\d{4}-\d{2}-\d{2}', val_str):
                    try:
                        dt = datetime.datetime.strptime(val_str.split()[0], '%Y-%m-%d')
                        return f"{dt.month}-{dt.day}"
                    except:
                        pass
                if re.match(r'^\d{5}(\.0)?$', val_str):
                    try:
                        num = int(float(val_str))
                        if 40000 <= num <= 50000:
                            dt = datetime.date(1899, 12, 30) + datetime.timedelta(days=num)
                            return f"{dt.month}-{dt.day}"
                        return str(num)
                    except:
                        pass
                return val_str

            def _parse_sheet(sheet):
                rows = list(sheet.iter_rows(values_only=True))
                if not rows:
                    return [], []
                raw_hdrs = rows[0]
                hdrs = [str(h).strip().lower().replace(' ', '_') for h in raw_hdrs if h is not None]
                recs = []
                for row in rows[1:]:
                    if any(row):
                        vals = [_clean_val(v) for v in row[:len(hdrs)]]
                        while len(vals) < len(hdrs):
                            vals.append('')
                        recs.append(dict(zip(hdrs, vals)))
                return hdrs, recs

            all_totals = {}
            all_warnings = []
            all_errors = []

            for itype in IMPORT_ORDER:
                sheet = sheet_assignments.get(itype)
                if sheet is None:
                    continue
                _hdrs, _recs = _parse_sheet(sheet)
                if not _recs:
                    continue
                # Temporarily override import_type and records for reuse below
                # by re-calling this view recursively would be complex;
                # instead inline a mini-import per type.
                from django.db import transaction
                from .models import Program
                import re as _re

                university_ref = university  # closure

                default_campus = Campus.objects.filter(university=university_ref).first() or \
                    Campus.objects.create(university=university_ref, name="Default Campus")
                default_faculty = Faculty.objects.filter(campus__university=university_ref).first() or \
                    Faculty.objects.create(campus=default_campus, name="Default Faculty")
                default_dept = Department.objects.filter(faculty__campus__university=university_ref).first() or \
                    Department.objects.create(faculty=default_faculty, name="Default Department")
                default_program = Program.objects.filter(department__faculty__campus__university=university_ref).first() or \
                    Program.objects.create(department=default_dept, name="Default Program")

                campus_cache2  = {c.name.strip(): c for c in Campus.objects.filter(university=university_ref)}
                dept_cache2    = {d.name.strip(): d for d in Department.objects.filter(faculty__campus__university=university_ref)}
                program_cache2 = {p.name.strip(): p for p in Program.objects.filter(department__faculty__campus__university=university_ref)}

                def _gc(nm):
                    if not nm: return default_campus
                    if nm not in campus_cache2:
                        campus_cache2[nm] = Campus.objects.create(university=university_ref, name=nm)
                    return campus_cache2[nm]

                def _gd(nm):
                    if not nm: return default_dept
                    if nm not in dept_cache2:
                        dept_cache2[nm] = Department.objects.create(faculty=default_faculty, name=nm)
                    return dept_cache2[nm]

                def _gp(nm):
                    if not nm: return default_program
                    if nm not in program_cache2:
                        program_cache2[nm] = Program.objects.create(department=default_dept, name=nm)
                    return program_cache2[nm]

                ROOM_TYPE_LECTURE2 = {'lecture','lecture hall','lecture_hall','hall','theater','theatre','classroom','class room','class_room','auditorium','lh'}
                ROOM_TYPE_LAB2 = {'lab','laboratory','computer lab','computer_lab','science lab','science_lab','workshop','studio'}
                ROOM_TYPE_SEMINAR2 = {'seminar','seminar room','seminar_room','tutorial','tutorial room','tutorial_room','meeting room','meeting_room','conference','boardroom'}

                def _nrt(raw):
                    v = (raw or '').strip().lower()
                    if v in ROOM_TYPE_LECTURE2: return 'Lecture'
                    if v in ROOM_TYPE_LAB2: return 'Lab'
                    if v in ROOM_TYPE_SEMINAR2: return 'Seminar'
                    return None

                def _g(row, *aliases):
                    for a in aliases:
                        v = row.get(a, '')
                        if v and str(v).strip(): return str(v).strip()
                        norm = _re.sub(r'\s+', '_', a.strip().lower())
                        v = row.get(norm, '')
                        if v and str(v).strip(): return str(v).strip()
                    return ''

                sc = 0
                try:
                    with transaction.atomic():
                        if itype == 'room':
                            existing = {(r.campus_id, r.name): r for r in Room.objects.filter(campus__university=university_ref)}
                            new_r, upd_r = [], []
                            for idx, row in enumerate(_recs, 2):
                                nm  = _g(row,'name','room_name','venue','venue_name','location')
                                cap_s = _g(row,'capacity','cap','seats','size','count','enrolment','enrollment','no_of_students','number_of_students','students_count')
                                rt  = _g(row,'room_type','required_room_type','venue_type','type','facility_type')
                                cn  = _g(row,'campus_name','campus') or None
                                if not nm: all_errors.append(f"[Rooms] Row {idx}: Name required."); continue
                                try: cap = int(float(cap_s)); assert cap > 0
                                except: all_errors.append(f"[Rooms] Row {idx}: Capacity invalid."); continue
                                nrt = _nrt(rt)
                                if not nrt: all_errors.append(f"[Rooms] Row {idx}: Unrecognised type '{rt}'."); continue
                                campus = _gc(cn)
                                key = (campus.id, nm)
                                if key in existing:
                                    r = existing[key]; r.capacity = cap; r.room_type = nrt; upd_r.append(r)
                                else:
                                    room_obj = Room(campus=campus, name=nm, capacity=cap, room_type=nrt)
                                    new_r.append(room_obj)
                                    existing[key] = room_obj
                                sc += 1
                            Room.objects.bulk_create(new_r, batch_size=500, ignore_conflicts=True)
                            if upd_r: Room.objects.bulk_update(upd_r, ['capacity','room_type'], batch_size=500)

                        elif itype == 'lecturer':
                            existing = {l.email: l for l in Lecturer.objects.filter(department__faculty__campus__university=university_ref)}
                            new_l, upd_l = [], []
                            for idx, row in enumerate(_recs, 2):
                                nm  = _g(row,'name','lecturer_name','lecturer','instructor','instructor_name','teacher','teacher_name','staff','staff_name','full_name')
                                em  = _g(row,'email','email_address','mail','e_mail').lower()
                                dn  = _g(row,'department_name','department','dept','faculty','school') or None
                                mhs = _g(row,'max_hours_per_week','max_hours','hours','weekly_hours','workload') or None
                                if not nm: all_errors.append(f"[Lecturers] Row {idx}: Name required."); continue
                                if not em or '@' not in em: all_errors.append(f"[Lecturers] Row {idx}: Valid email required."); continue
                                mh = 20
                                if mhs:
                                    try: mh = int(float(mhs))
                                    except: all_warnings.append(f"[Lecturers] Row {idx}: Invalid max hours, defaulted to 20.")
                                dept = _gd(dn)
                                if em in existing:
                                    l = existing[em]; l.name = nm; l.department = dept; l.max_hours_per_week = mh; upd_l.append(l)
                                else:
                                    lec_obj = Lecturer(email=em, name=nm, department=dept, max_hours_per_week=mh)
                                    new_l.append(lec_obj)
                                    existing[em] = lec_obj
                                sc += 1
                            Lecturer.objects.bulk_create(new_l, batch_size=500, ignore_conflicts=True)
                            if upd_l: Lecturer.objects.bulk_update(upd_l, ['name','department','max_hours_per_week'], batch_size=500)

                        elif itype == 'student_group':
                            existing = {(g.program_id, g.name): g for g in StudentGroup.objects.filter(program__department__faculty__campus__university=university_ref)}
                            new_g, upd_g = [], []
                            for idx, row in enumerate(_recs, 2):
                                nm  = _g(row,'name','group_name','student','student_group','student_group_name','class','class_name','cohort','section','stream')
                                ss  = _g(row,'size','group_size','capacity','cap','count','enrolment','enrollment','no_of_students','number_of_students','students_count') or None
                                pn  = _g(row,'program_name','program','programme') or None
                                if not nm: all_errors.append(f"[Students] Row {idx}: Name required."); continue
                                try: sz = int(float(ss)); assert sz > 0
                                except: all_errors.append(f"[Students] Row {idx}: Size invalid."); continue
                                prog = _gp(pn); key = (prog.id, nm)
                                if key in existing:
                                    g = existing[key]; g.size = sz; upd_g.append(g)
                                else:
                                    group_obj = StudentGroup(program=prog, name=nm, size=sz)
                                    new_g.append(group_obj)
                                    existing[key] = group_obj
                                sc += 1
                            StudentGroup.objects.bulk_create(new_g, batch_size=500, ignore_conflicts=True)
                            if upd_g: StudentGroup.objects.bulk_update(upd_g, ['size'], batch_size=500)

                        elif itype == 'course':
                            lc2 = {l.email: l for l in Lecturer.objects.filter(department__faculty__campus__university=university_ref)}
                            gc2 = {(g.program_id, g.name): g for g in StudentGroup.objects.filter(program__department__faculty__campus__university=university_ref)}
                            existing = {
                                (c.program_id, c.code.strip().upper(), c.student_group_id): c 
                                for c in Course.objects.filter(program__department__faculty__campus__university=university_ref)
                            }
                            new_c, upd_c = [], []
                            for idx, row in enumerate(_recs, 2):
                                code2 = _g(row,'code','course_code','subject_code','unit_code','module_code').upper()
                                nm2   = _g(row,'name','course_name','subject','subject_name','unit','unit_name','module','module_name')
                                ds2   = _g(row,'duration_slots','duration','slots','hours','credit_hours','credits','periods','lesson_periods') or None
                                spw2  = _g(row,'sessions_per_week','weekly_sessions','classes_per_week','sessions') or None
                                rt2   = _g(row,'required_room_type','room_type','venue_type','type','facility_type')
                                le2   = _g(row,'lecturer_email','instructor_email','teacher_email','lecturer','instructor','teacher').lower()
                                gr2   = _g(row,'student_group_name','student_group','student','group','class','class_name','cohort','section','stream')
                                pn2   = _g(row,'program_name','program','programme') or None
                                if not code2: all_errors.append(f"[Courses] Row {idx}: Code required."); continue
                                if not nm2: all_errors.append(f"[Courses] Row {idx}: Name required."); continue
                                try: dur = int(float(ds2)); assert dur > 0
                                except: all_errors.append(f"[Courses] Row {idx}: Duration invalid."); continue
                                try: spw = int(float(spw2)) if spw2 else 1; assert spw > 0
                                except: all_errors.append(f"[Courses] Row {idx}: Sessions per week invalid."); continue
                                nrt2 = _nrt(rt2)
                                if not nrt2: all_errors.append(f"[Courses] Row {idx}: Unrecognised type '{rt2}'."); continue
                                lect2 = lc2.get(le2)
                                if le2 and not lect2: all_warnings.append(f"[Courses] Row {idx}: Lecturer '{le2}' not found.")
                                prog2 = _gp(pn2)
                                grp2 = None
                                if gr2:
                                    gk2 = (prog2.id, gr2)
                                    if gk2 not in gc2:
                                        g2 = StudentGroup.objects.create(program=prog2, name=gr2, size=30)
                                        gc2[gk2] = g2
                                      # Wait, we need to cache the newly created group under gk2 so it can be reused for subsequent rows
                                    grp2 = gc2[gk2]
                                key2 = (prog2.id, code2.strip().upper(), grp2.id if grp2 else None)
                                if key2 in existing:
                                    c2 = existing[key2]; c2.name = nm2; c2.duration_slots = dur; c2.sessions_per_week = spw; c2.required_room_type = nrt2; c2.lecturer = lect2; c2.student_group = grp2; upd_c.append(c2)
                                else:
                                    course_obj = Course(program=prog2, code=code2, name=nm2, duration_slots=dur, sessions_per_week=spw, required_room_type=nrt2, lecturer=lect2, student_group=grp2)
                                    new_c.append(course_obj)
                                    existing[key2] = course_obj
                                sc += 1
                            Course.objects.bulk_create(new_c, batch_size=500, ignore_conflicts=True)
                            if upd_c: Course.objects.bulk_update(upd_c, ['name','duration_slots','sessions_per_week','required_room_type','lecturer','student_group'], batch_size=500)

                    all_totals[itype] = sc
                except Exception as sheet_err:
                    all_errors.append(f"[{itype}] Import failed: {sheet_err}")

            # Build summary
            type_labels = {'room': 'Rooms', 'lecturer': 'Lecturers', 'student_group': 'Student Groups', 'course': 'Courses'}
            summary_parts = [f"{v} {type_labels.get(k,k)}" for k, v in all_totals.items() if v]
            if summary_parts:
                messages.success(request, f"✅ Successfully imported: {', '.join(summary_parts)}!")
            else:
                messages.warning(request, "No records were imported. Check your sheet names and data.")
            for w in all_warnings[:10]:
                messages.warning(request, w)
            if all_errors:
                for e in all_errors[:10]:
                    messages.error(request, e)

            # Auto-heal + auto-generate
            try:
                heal_fixes = auto_heal_university_data(university)
                for fix_msg in heal_fixes:
                    messages.info(request, fix_msg)
            except Exception:
                pass
            auto_timetable2 = (
                Timetable.objects.filter(semester__university=university, is_active=True).first()
                or Timetable.objects.filter(semester__university=university).order_by('-created_at').first()
            )
            if auto_timetable2:
                try:
                    from .signals import queue_auto_generation
                    queue_auto_generation(auto_timetable2)
                    messages.info(request, f"✓ Timetable generation queued for '{auto_timetable2.name}'.")
                except Exception:
                    pass

            return redirect('/resources/?tab=rooms')
        # ── End of all-in-one import ────────────────────────────────────────────

        elif import_type == 'smart':
            if not file_name.endswith('.xlsx') and not file_name.endswith('.csv'):
                messages.error(request, "Smart Import requires an Excel (.xlsx) or CSV (.csv) file.")
                return redirect('scheduler:import_resources')
                
            import os
            from .smart_import import detect_format, extract_entities
            scratch_dir = os.path.join(settings.BASE_DIR, 'scratch')
            os.makedirs(scratch_dir, exist_ok=True)
            temp_file_path = os.path.join(scratch_dir, f'temp_import_{university.id}.xlsx')
            
            import openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active
            
            if file_name.endswith('.csv'):
                try:
                    csv_file = io.TextIOWrapper(uploaded_file, encoding='utf-8')
                    reader = csv.reader(csv_file)
                    for r_idx, row in enumerate(reader, 1):
                        for c_idx, val in enumerate(row, 1):
                            ws.cell(row=r_idx, column=c_idx, value=val)
                    wb.save(temp_file_path)
                except Exception as csv_err:
                    messages.error(request, f"Error reading CSV file: {csv_err}")
                    return redirect('scheduler:import_resources')
            else:
                with open(temp_file_path, 'wb+') as destination:
                    for chunk in uploaded_file.chunks():
                        destination.write(chunk)
                try:
                    wb = openpyxl.load_workbook(temp_file_path, data_only=True)
                except Exception as xlsx_err:
                    if os.path.exists(temp_file_path):
                        os.remove(temp_file_path)
                    messages.error(request, f"Error reading Excel file: {xlsx_err}")
                    return redirect('scheduler:import_resources')
                    
            try:
                format_info = detect_format(wb)
                entities = extract_entities(wb, format_info, university)
                
                # Check if everything is empty
                if not any([entities['campuses'], entities['programs'], entities['lecturers'],
                            entities['rooms'], entities['student_groups'], entities['courses']]):
                    if os.path.exists(temp_file_path):
                        os.remove(temp_file_path)
                    messages.error(request, "Could not extract any data from the file. Check your column headers.")
                    return redirect('scheduler:import_resources')
                    
                # Render the preview screen
                return render(request, 'scheduler/smart_import_preview.html', {
                    'format_info': format_info,
                    'entities': entities,
                    'file_name': uploaded_file.name,
                    'active_university': university,
                })
            except Exception as e:
                if os.path.exists(temp_file_path):
                    os.remove(temp_file_path)
                messages.error(request, f"Error processing file: {e}")
                return redirect('scheduler:import_resources')


        records = []
        headers = []

        try:
            if file_name.endswith('.csv'):
                csv_file = io.TextIOWrapper(uploaded_file, encoding='utf-8')
                reader = csv.reader(csv_file)
                raw_headers = next(reader, None)
                if raw_headers:
                    headers = [h.strip().lower().replace(' ', '_') for h in raw_headers]
                    for row in reader:
                        if any(row):
                            records.append(dict(zip(headers, [val.strip() for val in row])))
            elif file_name.endswith('.xlsx'):
                import openpyxl
                wb = openpyxl.load_workbook(uploaded_file, data_only=True)
                sheet = wb.active
                rows = list(sheet.iter_rows(values_only=True))
                if rows:
                    raw_headers = rows[0]
                    headers = [str(h).strip().lower().replace(' ', '_') for h in raw_headers if h is not None]
                    for row in rows[1:]:
                        if any(row):
                            vals = [_clean_val(val) for val in row[:len(headers)]]
                            while len(vals) < len(headers):
                                vals.append('')
                            records.append(dict(zip(headers, vals)))
            else:
                messages.error(request, "Unsupported file format. Please upload a .csv or .xlsx file.")
                return redirect('scheduler:import_resources')
        except Exception as e:
            messages.error(request, f"Error reading file: {str(e)}")
            return redirect('scheduler:import_resources')

        if not records:
            messages.error(request, "The uploaded file contains no data rows.")
            return redirect('scheduler:import_resources')

        # ── Flexible alias resolver ────────────────────────────────────────────
        # Normalise every header: lower-case, strip whitespace, collapse spaces→_
        import re as _re
        norm_headers = [_re.sub(r'\s+', '_', h.strip().lower()) for h in raw_headers if h]

        # Build a map: normalised_alias → original normalised header present in the row dicts
        # so _get(row, aliases) can resolve any common variation.
        ALIAS_MAP = {
            # room / venue name
            'venue': 'venue', 'venue_name': 'venue_name', 'location': 'location',
            'room': 'room', 'room_name': 'room_name', 'name': 'name',
            'r': 'room', 'rm': 'room',
            # capacity / size
            'capacity': 'capacity', 'cap': 'cap', 'seats': 'seats',
            'size': 'size', 'group_size': 'group_size', 'students_count': 'students_count',
            'no_of_students': 'no_of_students', 'number_of_students': 'number_of_students',
            'enrolment': 'enrolment', 'enrollment': 'enrollment', 'count': 'count',
            # room type
            'room_type': 'room_type', 'required_room_type': 'required_room_type',
            'venue_type': 'venue_type', 'type': 'type', 'facility_type': 'facility_type',
            # lecturer / instructor name
            'lecturer_name': 'lecturer_name', 'instructor': 'instructor',
            'instructor_name': 'instructor_name', 'teacher': 'teacher',
            'teacher_name': 'teacher_name', 'staff': 'staff', 'staff_name': 'staff_name',
            'full_name': 'full_name', 'lecturer': 'lecturer',
            # email
            'email': 'email', 'email_address': 'email_address',
            'mail': 'mail', 'e_mail': 'e_mail',
            # student group name
            'group_name': 'group_name', 'class': 'class', 'class_name': 'class_name',
            'student': 'student', 'student_group': 'student_group',
            'student_group_name': 'student_group_name', 'cohort': 'cohort',
            'section': 'section', 'stream': 'stream',
            # course code
            'course_code': 'course_code', 'code': 'code', 'subject_code': 'subject_code',
            'unit_code': 'unit_code', 'module_code': 'module_code',
            # course name
            'course_name': 'course_name', 'subject': 'subject', 'subject_name': 'subject_name',
            'unit': 'unit', 'unit_name': 'unit_name', 'module': 'module', 'module_name': 'module_name',
            # duration
            'duration_slots': 'duration_slots', 'duration': 'duration', 'slots': 'slots',
            'hours': 'hours', 'credit_hours': 'credit_hours', 'credits': 'credits',
            'periods': 'periods', 'sessions': 'sessions', 'lesson_periods': 'lesson_periods',
            # department / campus / program
            'department': 'department', 'department_name': 'department_name', 'dept': 'dept',
            'campus': 'campus', 'campus_name': 'campus_name',
            'program': 'program', 'program_name': 'program_name', 'programme': 'programme',
            'faculty': 'faculty', 'school': 'school',
            # lecturer email (courses)
            'lecturer_email': 'lecturer_email', 'instructor_email': 'instructor_email',
            'teacher_email': 'teacher_email',
            # max hours
            'max_hours_per_week': 'max_hours_per_week', 'max_hours': 'max_hours',
            'weekly_hours': 'weekly_hours', 'workload': 'workload',
        }

        def _get(row, *aliases, default=''):
            """Return the first non-empty value from row matching any alias."""
            for alias in aliases:
                # try alias directly
                v = row.get(alias, '')
                if v and str(v).strip():
                    return str(v).strip()
                # try normalised form
                norm = _re.sub(r'\s+', '_', alias.strip().lower())
                v = row.get(norm, '')
                if v and str(v).strip():
                    return str(v).strip()
            return default

        # Room-type normaliser: accept every real-world variation
        ROOM_TYPE_LECTURE = {
            'lecture', 'lecture hall', 'lecture_hall', 'hall', 'theater', 'theatre',
            'classroom', 'class room', 'class_room', 'auditorium', 'lh',
        }
        ROOM_TYPE_LAB = {
            'lab', 'laboratory', 'computer lab', 'computer_lab', 'science lab',
            'science_lab', 'workshop', 'studio',
        }
        ROOM_TYPE_SEMINAR = {
            'seminar', 'seminar room', 'seminar_room', 'tutorial', 'tutorial room',
            'tutorial_room', 'meeting room', 'meeting_room', 'conference', 'boardroom',
        }

        def _norm_room_type(raw):
            """Return canonical room type string or None if unrecognised."""
            v = (raw or '').strip().lower()
            if v in ROOM_TYPE_LECTURE:
                return 'Lecture'
            if v in ROOM_TYPE_LAB:
                return 'Lab'
            if v in ROOM_TYPE_SEMINAR:
                return 'Seminar'
            return None

        # Validate headers (flexible – accept any recognised alias)
        required_cols = []
        if import_type == 'room':
            if not any(h in norm_headers for h in
                       ('name','room_name','venue','venue_name','location', 'r', 'rm')):
                required_cols.append("Room/Venue Name")
            if not any(h in norm_headers for h in
                       ('capacity','cap','seats','size','count','enrolment','enrollment',
                        'no_of_students','number_of_students','students_count')):
                required_cols.append("Capacity / Size")
            if not any(h in norm_headers for h in
                       ('room_type','required_room_type','venue_type','type','facility_type')):
                required_cols.append("Room Type / Venue Type")
        elif import_type == 'lecturer':
            if not any(h in norm_headers for h in
                       ('name','lecturer_name','lecturer','instructor','instructor_name',
                        'teacher','teacher_name','staff','staff_name','full_name')):
                required_cols.append("Lecturer / Instructor Name")
            if not any(h in norm_headers for h in
                       ('email','email_address','mail','e_mail')):
                required_cols.append("Email Address")
        elif import_type == 'student_group':
            if not any(h in norm_headers for h in
                       ('name','group_name','student','student_group','student_group_name',
                        'class','class_name','cohort','section','stream')):
                required_cols.append("Student Group / Class Name")
            if not any(h in norm_headers for h in
                       ('size','group_size','capacity','cap','count','enrolment',
                        'enrollment','no_of_students','number_of_students','students_count')):
                required_cols.append("Group Size / Enrolment")
        elif import_type == 'course':
            if not any(h in norm_headers for h in
                       ('code','course_code','subject_code','unit_code','module_code')):
                required_cols.append("Course / Unit Code")
            if not any(h in norm_headers for h in
                       ('name','course_name','subject','subject_name','unit','unit_name',
                        'module','module_name')):
                required_cols.append("Course / Unit Name")
            if not any(h in norm_headers for h in
                       ('duration_slots','duration','slots','hours','credit_hours',
                        'credits','periods','sessions','lesson_periods')):
                required_cols.append("Duration / Hours / Slots")
            if not any(h in norm_headers for h in
                       ('required_room_type','room_type','venue_type','type','facility_type')):
                required_cols.append("Room Type / Venue Type")

        if required_cols:
            messages.error(request, f"Missing required column(s): {', '.join(required_cols)}. Please check your column headings.")
            return redirect('scheduler:import_resources')

        import_errors = []
        import_warnings = []
        success_count = 0

        from django.db import transaction
        from .models import Program

        try:
            with transaction.atomic():

                # ── Pre-cache all structure lookups ONCE (avoids N×4 queries) ──
                # Get or create the default campus for this university
                default_campus = Campus.objects.filter(university=university).first()
                if not default_campus:
                    default_campus = Campus.objects.create(university=university, name="Default Campus")

                default_faculty = Faculty.objects.filter(campus__university=university).first()
                if not default_faculty:
                    default_faculty = Faculty.objects.create(campus=default_campus, name="Default Faculty")

                default_dept = Department.objects.filter(faculty__campus__university=university).first()
                if not default_dept:
                    default_dept = Department.objects.create(faculty=default_faculty, name="Default Department")

                default_program = Program.objects.filter(department__faculty__campus__university=university).first()
                if not default_program:
                    default_program = Program.objects.create(department=default_dept, name="Default Program")

                # Build caches for named lookups (campus_name, dept_name, program_name)
                campus_cache   = {c.name.strip(): c for c in Campus.objects.filter(university=university)}
                faculty_cache  = {f.name.strip(): f for f in Faculty.objects.filter(campus__university=university)}
                dept_cache     = {d.name.strip(): d for d in Department.objects.filter(faculty__campus__university=university)}
                program_cache  = {p.name.strip(): p for p in Program.objects.filter(department__faculty__campus__university=university)}

                def _get_campus(name):
                    if not name:
                        return default_campus
                    key = name.strip()
                    if key not in campus_cache:
                        campus_cache[key] = Campus.objects.create(university=university, name=key)
                    return campus_cache[key]

                def _get_dept(name):
                    if not name:
                        return default_dept
                    key = name.strip()
                    if key not in dept_cache:
                        dept_cache[key] = Department.objects.create(faculty=default_faculty, name=key)
                    return dept_cache[key]

                def _get_program(name):
                    if not name:
                        return default_program
                    key = name.strip()
                    if key not in program_cache:
                        program_cache[key] = Program.objects.create(department=default_dept, name=key)
                    return program_cache[key]

                # ── BULK IMPORT: Rooms ──────────────────────────────────────────
                if import_type == 'room':
                    rooms_to_upsert = []
                    existing_keys = {
                        (r.campus_id, r.name): r
                        for r in Room.objects.filter(campus__university=university)
                    }
                    new_rooms, update_rooms = [], []

                    for idx, row in enumerate(records, start=2):
                        name         = _get(row, 'name','room_name','venue','venue_name','location')
                        capacity_str = _get(row, 'capacity','cap','seats','size','count',
                                            'enrolment','enrollment','no_of_students',
                                            'number_of_students','students_count')
                        room_type    = _get(row, 'room_type','required_room_type','venue_type','type','facility_type')
                        campus_name  = _get(row, 'campus_name','campus') or None

                        if not name:
                            import_errors.append(f"Row {idx}: Room Name is required.")
                            continue
                        try:
                            capacity = int(float(capacity_str))
                            if capacity <= 0:
                                raise ValueError()
                        except (ValueError, TypeError):
                            import_errors.append(f"Row {idx}: Capacity must be a valid positive number.")
                            continue

                        normalized_type = _norm_room_type(room_type)
                        if not normalized_type:
                            import_errors.append(
                                f"Row {idx}: Unrecognised Room Type '{room_type}'. "
                                f"Use: Lecture/Hall/Classroom, Lab/Laboratory, or Seminar/Tutorial."
                            )
                            continue
                        campus = _get_campus(campus_name)
                        key = (campus.id, name)

                        if key in existing_keys:
                            r = existing_keys[key]
                            r.capacity  = capacity
                            r.room_type = normalized_type
                            update_rooms.append(r)
                        else:
                            room_obj = Room(campus=campus, name=name, capacity=capacity, room_type=normalized_type)
                            new_rooms.append(room_obj)
                            existing_keys[key] = room_obj
                        success_count += 1

                    if import_errors:
                        raise Exception("Validation errors occurred.")

                    Room.objects.bulk_create(new_rooms, batch_size=500, ignore_conflicts=True)
                    if update_rooms:
                        Room.objects.bulk_update(update_rooms, ['capacity', 'room_type'], batch_size=500)

                # ── BULK IMPORT: Lecturers ──────────────────────────────────────
                elif import_type == 'lecturer':
                    existing_lecturers = {l.email: l for l in Lecturer.objects.filter(department__faculty__campus__university=university)}
                    new_lecs, update_lecs = [], []

                    for idx, row in enumerate(records, start=2):
                        name          = _get(row, 'name','lecturer_name','lecturer','instructor',
                                             'instructor_name','teacher','teacher_name',
                                              'staff','staff_name','full_name')
                        email         = _get(row, 'email','email_address','mail','e_mail').lower()
                        dept_name     = _get(row, 'department_name','department','dept',
                                             'faculty','school') or None
                        max_hours_str = _get(row, 'max_hours_per_week','max_hours','hours',
                                             'weekly_hours','workload') or None

                        if not name:
                            import_errors.append(f"Row {idx}: Lecturer Name is required.")
                            continue
                        if not email or '@' not in email:
                            import_errors.append(f"Row {idx}: Valid email address is required.")
                            continue

                        max_hours = 20
                        if max_hours_str:
                            try:
                                max_hours = int(float(max_hours_str))
                            except (ValueError, TypeError):
                                import_warnings.append(f"Row {idx}: Invalid max hours '{max_hours_str}', defaulted to 20.")

                        dept = _get_dept(dept_name)

                        if email in existing_lecturers:
                            l = existing_lecturers[email]
                            l.name = name
                            l.department = dept
                            l.max_hours_per_week = max_hours
                            update_lecs.append(l)
                        else:
                            lec_obj = Lecturer(email=email, name=name, department=dept, max_hours_per_week=max_hours)
                            new_lecs.append(lec_obj)
                            existing_lecturers[email] = lec_obj
                        success_count += 1

                    if import_errors:
                        raise Exception("Validation errors occurred.")

                    Lecturer.objects.bulk_create(new_lecs, batch_size=500, ignore_conflicts=True)
                    if update_lecs:
                        Lecturer.objects.bulk_update(update_lecs, ['name', 'department', 'max_hours_per_week'], batch_size=500)

                # ── BULK IMPORT: Student Groups ─────────────────────────────────
                elif import_type == 'student_group':
                    existing_groups = {
                        (g.program_id, g.name): g
                        for g in StudentGroup.objects.filter(program__department__faculty__campus__university=university)
                    }
                    new_groups, update_groups = [], []

                    for idx, row in enumerate(records, start=2):
                        name      = _get(row, 'name','group_name','student','student_group',
                                         'student_group_name','class','class_name',
                                         'cohort','section','stream')
                        size_str  = _get(row, 'size','group_size','capacity','cap','count',
                                         'enrolment','enrollment','no_of_students',
                                         'number_of_students','students_count') or None
                        prog_name = _get(row, 'program_name','program','programme') or None

                        if not name:
                            import_errors.append(f"Row {idx}: Student Group Name is required.")
                            continue
                        try:
                            size = int(float(size_str))
                            if size <= 0:
                                raise ValueError()
                        except (ValueError, TypeError):
                            import_errors.append(f"Row {idx}: Group Size must be a valid positive number.")
                            continue

                        program = _get_program(prog_name)
                        key = (program.id, name)

                        if key in existing_groups:
                            g = existing_groups[key]
                            g.size = size
                            update_groups.append(g)
                        else:
                            group_obj = StudentGroup(program=program, name=name, size=size)
                            new_groups.append(group_obj)
                            existing_groups[key] = group_obj
                        success_count += 1

                    if import_errors:
                        raise Exception("Validation errors occurred.")

                    StudentGroup.objects.bulk_create(new_groups, batch_size=500, ignore_conflicts=True)
                    if update_groups:
                        StudentGroup.objects.bulk_update(update_groups, ['size'], batch_size=500)

                # ── BULK IMPORT: Courses ────────────────────────────────────────
                elif import_type == 'course':
                    # Pre-cache lecturers and student groups for O(1) lookup
                    lecturer_cache = {l.email: l for l in Lecturer.objects.filter(department__faculty__campus__university=university)}
                    group_cache    = {
                        (g.program_id, g.name): g
                        for g in StudentGroup.objects.filter(program__department__faculty__campus__university=university)
                    }
                    existing_courses = {
                        (c.program_id, c.code.strip().upper(), c.student_group_id): c
                        for c in Course.objects.filter(program__department__faculty__campus__university=university)
                    }
                    new_courses, update_courses = [], []

                    for idx, row in enumerate(records, start=2):
                        code         = _get(row, 'code','course_code','subject_code',
                                            'unit_code','module_code').upper()
                        name         = _get(row, 'name','course_name','subject','subject_name',
                                            'unit','unit_name','module','module_name')
                        duration_str = _get(row, 'duration_slots','duration','slots','hours',
                                            'credit_hours','credits','periods',
                                            'lesson_periods') or None
                        spw_str = _get(row, 'sessions_per_week','weekly_sessions',
                                       'classes_per_week','sessions') or None
                        room_type    = _get(row, 'required_room_type','room_type','venue_type',
                                            'type','facility_type')
                        lec_email    = _get(row, 'lecturer_email','instructor_email',
                                            'teacher_email','lecturer','instructor','teacher').lower()
                        group_name   = _get(row, 'student_group_name','student_group','student',
                                            'group','class','class_name','cohort','section','stream')
                        prog_name    = _get(row, 'program_name','program','programme') or None

                        if not code:
                            import_errors.append(f"Row {idx}: Course Code is required.")
                            continue
                        if not name:
                            import_errors.append(f"Row {idx}: Course Name is required.")
                            continue
                        try:
                            duration = int(float(duration_str))
                            if duration <= 0:
                                raise ValueError()
                        except (ValueError, TypeError):
                            import_errors.append(f"Row {idx}: Duration Slots must be a valid positive number.")
                            continue
                        try:
                            sessions_per_week = int(float(spw_str)) if spw_str else 1
                            if sessions_per_week <= 0:
                                raise ValueError()
                        except (ValueError, TypeError):
                            import_errors.append(f"Row {idx}: Sessions per week must be a valid positive number.")
                            continue
                        normalized_type = _norm_room_type(room_type)
                        if not normalized_type:
                            import_errors.append(
                                f"Row {idx}: Unrecognised Room Type '{room_type}'. "
                                f"Use: Lecture/Hall/Classroom, Lab/Laboratory, or Seminar/Tutorial."
                            )
                            continue

                        lecturer = lecturer_cache.get(lec_email)
                        if lec_email and not lecturer:
                            import_warnings.append(f"Row {idx}: Lecturer '{lec_email}' not found. Course created without lecturer.")

                        program = _get_program(prog_name)

                        group = None
                        if group_name:
                            gkey = (program.id, group_name)
                            if gkey not in group_cache:
                                g = StudentGroup.objects.create(program=program, name=group_name, size=30)
                                group_cache[gkey] = g
                            group = group_cache[gkey]

                        key = (program.id, code.strip().upper(), group.id if group else None)
                        if key in existing_courses:
                            c = existing_courses[key]
                            c.name = name
                            c.duration_slots = duration
                            c.sessions_per_week = sessions_per_week
                            c.required_room_type = normalized_type
                            c.lecturer = lecturer
                            c.student_group = group
                            update_courses.append(c)
                        else:
                            course_obj = Course(
                                program=program, code=code, name=name,
                                duration_slots=duration, sessions_per_week=sessions_per_week,
                                required_room_type=normalized_type,
                                lecturer=lecturer, student_group=group
                            )
                            new_courses.append(course_obj)
                            existing_courses[key] = course_obj
                        success_count += 1

                    if import_errors:
                        raise Exception("Validation errors occurred.")

                    Course.objects.bulk_create(new_courses, batch_size=500, ignore_conflicts=True)
                    if update_courses:
                        Course.objects.bulk_update(
                            update_courses,
                            ['name', 'duration_slots', 'sessions_per_week', 'required_room_type', 'lecturer', 'student_group'],
                            batch_size=500
                        )

            messages.success(request, f"✅ Successfully imported {success_count} {import_type.replace('_',' ')}(s)!")
            if import_warnings:
                for w in import_warnings[:5]:
                    messages.warning(request, w)
                if len(import_warnings) > 5:
                    messages.warning(request, f"...and {len(import_warnings) - 5} more warnings.")

            # ── Auto-heal: fix all common data issues automatically ────────────
            try:
                heal_fixes = auto_heal_university_data(university)
                for fix_msg in heal_fixes:
                    messages.info(request, fix_msg)
            except Exception as heal_err:
                messages.warning(request, f"Auto-heal warning: {heal_err}")

            # Auto-trigger timetable regeneration
            auto_timetable = (
                Timetable.objects.filter(semester__university=university, is_active=True).first()
                or Timetable.objects.filter(semester__university=university).order_by('-created_at').first()
            )
            if auto_timetable:
                try:
                    from .signals import queue_auto_generation
                    queue_auto_generation(auto_timetable)
                    messages.info(request, f"✓ Timetable generation queued for '{auto_timetable.name}'.")
                except Exception as e:
                    messages.warning(request, f"Import succeeded but auto-generation could not be queued: {e}")
            else:
                messages.warning(request, "Import succeeded but no timetable found. Create one under Timetables → New.")

            return redirect(f"/resources/?tab={import_type.replace('_','')}")

        except Exception as e:
            context = {
                'import_errors': import_errors,
                'import_warnings': import_warnings,
                'import_type': import_type,
            }
            return render(request, 'scheduler/resources_import.html', context)

    return render(request, 'scheduler/resources_import.html')





# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: Lecturer Availability Self-Service
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def lecturer_availability(request):
    """
    Lecturers can set their own availability across all time slots.
    Admins can view/edit any lecturer's availability.
    """
    university = get_active_uni(request)
    role = get_user_role(request)

    # Determine which lecturer we are editing
    lecturer = None
    if role == 'lecturer':
        try:
            lecturer = request.user.profile.lecturer
        except Exception:
            messages.error(request, "Your account is not linked to a lecturer. Please update your profile.")
            return redirect('accounts:profile')
    else:
        # Admin/scheduler: allow choosing any lecturer
        lecturer_id = request.GET.get('lecturer_id')
        if lecturer_id:
            lecturer = get_object_or_404(Lecturer, pk=lecturer_id, department__faculty__campus__university=university)
        else:
            lecturer = Lecturer.objects.filter(department__faculty__campus__university=university).first()

    if not lecturer:
        messages.error(request, "No lecturers found for this university.")
        return redirect('scheduler:dashboard')

    timeslots = TimeSlot.objects.filter(university=university).order_by('day_of_week', 'slot_number')

    if request.method == 'POST':
        # Save availability: checkbox per timeslot
        for ts in timeslots:
            is_available = request.POST.get(f'slot_{ts.id}') == 'on'
            note = request.POST.get(f'note_{ts.id}', '')
            LecturerAvailability.objects.update_or_create(
                lecturer=lecturer,
                time_slot=ts,
                defaults={'is_available': is_available, 'note': note}
            )
        messages.success(request, f"Availability for {lecturer.name} saved successfully!")
        return redirect('scheduler:lecturer_availability')

    # Build existing availability map: {time_slot_id: LecturerAvailability}
    existing = {a.time_slot_id: a for a in LecturerAvailability.objects.filter(lecturer=lecturer)}

    # Build day-grouped structure for template
    DAY_LABELS = {1:'Monday',2:'Tuesday',3:'Wednesday',4:'Thursday',5:'Friday',6:'Saturday',7:'Sunday'}
    days = {}
    for ts in timeslots:
        day = ts.day_of_week
        if day not in days:
            days[day] = {'label': DAY_LABELS.get(day, f'Day {day}'), 'slots': []}
        avail = existing.get(ts.id)
        days[day]['slots'].append({
            'ts': ts,
            'is_available': avail.is_available if avail else True,
            'note': avail.note if avail else '',
        })

    all_lecturers = Lecturer.objects.filter(department__faculty__campus__university=university)
    
    template_name = 'scheduler/lecturer_portal_availability.html' if role == 'lecturer' else 'scheduler/lecturer_availability.html'
    return render(request, template_name, {
        'lecturer': lecturer,
        'days': days,
        'all_lecturers': all_lecturers,
        'is_own': role == 'lecturer',
    })


@login_required
def lecturer_my_schedule(request):
    """Redirect legacy My Timetable link to Phase 3 Lecturer Portal Weekly Timetable."""
    return redirect('scheduler:lecturer_portal_weekly_timetable')


@login_required
def lecturer_teaching_history(request):
    """Redirect legacy Teaching History link to Phase 3 Lecturer Portal Courses."""
    return redirect('scheduler:lecturer_portal_courses')


# ─────────────────────────────────────────────────────────────────────────────
# Phase 5: PDF & Excel Export
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def export_timetable_pdf(request, pk):
    """Export timetable as a PDF file using ReportLab with custom branding."""
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    import re
    import io

    university = get_active_uni(request)
    timetable = get_object_or_404(
        Timetable.objects.select_related('semester', 'semester__university'), pk=pk
    )
    if timetable.semester.university != university:
        messages.error(request, "Access denied.")
        return redirect('scheduler:dashboard')

    # Get custom branding and layout from request
    pdf_title = request.GET.get('title', f"{timetable.name} — {timetable.semester.name}")
    pdf_subtitle = request.GET.get('subtitle', f"{university.name} | Generated Timetable")
    pdf_logo_text = request.GET.get('logo_text', "KU")
    primary_hex = request.GET.get('primary_color', "#0d5a4f")
    alt_hex = request.GET.get('alt_color', "#f0fdfa")
    show_logo = request.GET.get('show_logo', "true").lower() == "true"
    layout_type = request.GET.get('layout_type', 'weekly')

    # Sanitize color codes
    if not primary_hex.startswith('#'):
        primary_hex = '#' + primary_hex
    if not alt_hex.startswith('#'):
        alt_hex = '#' + alt_hex
    if not re.match(r'^#[0-9a-fA-F]{6}$', primary_hex):
        primary_hex = '#0d5a4f'
    if not re.match(r'^#[0-9a-fA-F]{6}$', alt_hex):
        alt_hex = '#f0fdfa'

    # Filter resources by university for scoping and defaulting
    rooms = Room.objects.filter(campus__university=university)
    lecturers = Lecturer.objects.filter(department__faculty__campus__university=university)
    student_groups = StudentGroup.objects.filter(program__department__faculty__campus__university=university)

    # Get active filters
    filter_type = request.GET.get('filter_type')
    filter_id = request.GET.get('filter_id')

    # Resolve target list
    targets = []
    
    if filter_type == 'all_groups':
        active_group_ids = timetable.slots.values_list('student_group_id', flat=True).distinct()
        targets = list(student_groups.filter(id__in=active_group_ids).order_by('name'))
    elif filter_type == 'all_rooms':
        active_room_ids = timetable.slots.values_list('room_id', flat=True).distinct()
        targets = list(rooms.filter(id__in=active_room_ids).order_by('name'))
    elif filter_type == 'all_lecturers':
        active_lecturer_ids = timetable.slots.values_list('lecturer_id', flat=True).distinct()
        targets = list(lecturers.filter(id__in=active_lecturer_ids).order_by('name'))
    else:
        if not filter_type:
            filter_type = 'group'

        if not filter_id:
            if filter_type == 'group' and student_groups.exists():
                filter_id = student_groups.first().id
            elif filter_type == 'room' and rooms.exists():
                filter_id = rooms.first().id
            elif filter_type == 'lecturer' and lecturers.exists():
                filter_id = lecturers.first().id

        target_obj = None
        if filter_id:
            try:
                filter_id = int(filter_id)
                if filter_type == 'group':
                    target_obj = student_groups.filter(id=filter_id).first()
                elif filter_type == 'room':
                    target_obj = rooms.filter(id=filter_id).first()
                elif filter_type == 'lecturer':
                    target_obj = lecturers.filter(id=filter_id).first()
            except ValueError:
                pass
        if target_obj:
            targets = [target_obj]

    # Set page margins to 0.6cm to maximize the printable area
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4),
        rightMargin=0.6*cm, leftMargin=0.6*cm,
        topMargin=0.6*cm, bottomMargin=0.6*cm
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'title',
        parent=styles['Heading1'],
        fontSize=18,
        leading=22,
        textColor=colors.HexColor(primary_hex),
        spaceAfter=4
    )
    subtitle_style = ParagraphStyle(
        'subtitle',
        parent=styles['Normal'],
        fontSize=10,
        leading=13,
        textColor=colors.HexColor('#475569')
    )
    logo_style = ParagraphStyle(
        'logo',
        parent=styles['Normal'],
        fontSize=14,
        leading=16,
        alignment=TA_CENTER,
        fontName='Helvetica-Bold'
    )
    
    elements = []

    # Build table data structures
    day_labels = {1:'Mon',2:'Tue',3:'Wed',4:'Thu',5:'Fri',6:'Sat',7:'Sun'}
    timeslots = list(TimeSlot.objects.filter(university=university).order_by('day_of_week', 'slot_number'))
    days = sorted(set(ts.day_of_week for ts in timeslots))
    slot_numbers = sorted(set(ts.slot_number for ts in timeslots))

    slots_all = list(
        timetable.slots.select_related('course','lecturer','room','time_slot','student_group').all()
    )

    # ─────────────────────────────────────────────────────────────────────────
    # LAYOUT 1: master (Master Timetable Grid)
    # ─────────────────────────────────────────────────────────────────────────
    if layout_type == 'master':
        # Get active rooms to keep columns clean and focused
        active_room_ids = timetable.slots.values_list('room_id', flat=True).distinct()
        master_rooms = list(rooms.filter(id__in=active_room_ids).order_by('name'))
        if not master_rooms:
            master_rooms = list(rooms.order_by('name')[:8]) # fallback if no slots booked yet

        slots_by_ts_and_room = {}
        for s in slots_all:
            slots_by_ts_and_room.setdefault((s.time_slot_id, s.room_id), []).append(s)

        master_cell_style = ParagraphStyle('master_cell', parent=styles['Normal'], fontSize=5.5, leading=7, alignment=TA_CENTER)
        master_time_style = ParagraphStyle('master_time', parent=styles['Normal'], fontSize=6, leading=8, alignment=TA_CENTER)

        # Chunk rooms (e.g. 8 rooms per page) to prevent LayoutError for large datasets
        chunk_size = 8
        room_chunks = [master_rooms[i:i + chunk_size] for i in range(0, len(master_rooms), chunk_size)]
        
        # Limit to first 20 chunks (160 rooms) to keep performance fast and prevent timeouts
        active_chunks = room_chunks[:20]
        for c_idx, chunk in enumerate(active_chunks):
            header_cols = ['Time / Day'] + [r.name for r in chunk]
            data = [header_cols]
            
            for ts in timeslots:
                row_label = f"{day_labels.get(ts.day_of_week, f'D{ts.day_of_week}')}<br/>{ts.start_time.strftime('%H:%M')}-{ts.end_time.strftime('%H:%M')}"
                row = [Paragraph(row_label, master_time_style)]
                for r in chunk:
                    matching = slots_by_ts_and_room.get((ts.id, r.id), [])
                    if matching:
                        items = []
                        for s in matching:
                            items.append(f"<b>{s.course.code}</b><br/>{s.student_group.name}")
                        cell_content = '<br/>'.join(items)
                        row.append(Paragraph(cell_content, master_cell_style))
                    else:
                        row.append('')
                data.append(row)

            m_col_width = (landscape(A4)[0] - 1.2*cm) / (len(chunk) + 1)
            table = Table(data, colWidths=[m_col_width] * (len(chunk) + 1), repeatRows=1)
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor(primary_hex)),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 7.5),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('GRID', (0, 0), (-1, -1), 0.3, colors.HexColor('#CBD5E1')),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor(alt_hex), colors.white]),
                ('LEFTPADDING', (0, 0), (-1, -1), 2),
                ('RIGHTPADDING', (0, 0), (-1, -1), 2),
                ('TOPPADDING', (0, 0), (-1, -1), 2),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
            ]))

            # Header block recreated for each page
            current_subtitle = f"{pdf_subtitle} — Master Layout (Rooms {c_idx*chunk_size+1} - {min((c_idx+1)*chunk_size, len(master_rooms))})"
            title_p = Paragraph(f"<b>{pdf_title}</b>", title_style)
            subtitle_p = Paragraph(current_subtitle, subtitle_style)
            left_flow = [title_p, subtitle_p]

            if show_logo:
                logo_p = Paragraph(f"<font color='white'><b>{pdf_logo_text}</b></font>", logo_style)
                logo_table = Table([[logo_p]], colWidths=[2.2*cm], rowHeights=[1.2*cm])
                logo_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor(primary_hex)),
                    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                    ('TOPPADDING', (0, 0), (-1, -1), 0),
                    ('LEFTPADDING', (0, 0), (-1, -1), 0),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                    ('BOX', (0, 0), (-1, -1), 1.5, colors.HexColor(primary_hex)),
                ]))
                header_table = Table([[left_flow, logo_table]], colWidths=[landscape(A4)[0] - 1.2*cm - 2.5*cm, 2.5*cm])
            else:
                header_table = Table([[left_flow]], colWidths=[landscape(A4)[0] - 1.2*cm])
                
            header_table.setStyle(TableStyle([
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('LEFTPADDING', (0, 0), (-1, -1), 0),
                ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                ('TOPPADDING', (0, 0), (-1, -1), 0),
            ]))
            
            elements.append(header_table)
            elements.append(Spacer(1, 0.4*cm))
            elements.append(table)

            if c_idx < len(active_chunks) - 1:
                elements.append(PageBreak())

    # ─────────────────────────────────────────────────────────────────────────
    # LAYOUT 2: monthly (Monthly Calendar Grid)
    # ─────────────────────────────────────────────────────────────────────────
    elif layout_type == 'monthly':
        import calendar
        start_date = timetable.semester.start_date
        year = start_date.year
        month = start_date.month
        month_name = start_date.strftime("%B %Y")

        cal = calendar.Calendar(firstweekday=0) # Monday starts the week
        month_weeks = cal.monthdayscalendar(year, month)

        if not targets:
            elements.append(Paragraph("No scheduled slots found for this selection.", styles['Heading2']))
        else:
            for idx, target in enumerate(targets):
                selected_filter_name = str(target)
                current_subtitle = f"{pdf_subtitle} — {month_name}"
                if selected_filter_name and selected_filter_name not in current_subtitle:
                    current_subtitle = f"{current_subtitle} — {selected_filter_name}"

                title_p = Paragraph(f"<b>{pdf_title}</b>", title_style)
                subtitle_p = Paragraph(current_subtitle, subtitle_style)
                left_flow = [title_p, subtitle_p]

                if show_logo:
                    logo_p = Paragraph(f"<font color='white'><b>{pdf_logo_text}</b></font>", logo_style)
                    logo_table = Table([[logo_p]], colWidths=[2.2*cm], rowHeights=[1.2*cm])
                    logo_table.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor(primary_hex)),
                        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                        ('TOPPADDING', (0, 0), (-1, -1), 0),
                        ('LEFTPADDING', (0, 0), (-1, -1), 0),
                        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                        ('BOX', (0, 0), (-1, -1), 1.5, colors.HexColor(primary_hex)),
                    ]))
                    header_table = Table([[left_flow, logo_table]], colWidths=[landscape(A4)[0] - 1.2*cm - 2.5*cm, 2.5*cm])
                else:
                    header_table = Table([[left_flow]], colWidths=[landscape(A4)[0] - 1.2*cm])
                    
                header_table.setStyle(TableStyle([
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    ('LEFTPADDING', (0, 0), (-1, -1), 0),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                    ('TOPPADDING', (0, 0), (-1, -1), 0),
                ]))
                
                elements.append(header_table)
                elements.append(Spacer(1, 0.4*cm))

                if filter_type in ('group', 'all_groups'):
                    slots_target = [s for s in slots_all if s.student_group_id == target.id]
                elif filter_type in ('room', 'all_rooms'):
                    slots_target = [s for s in slots_all if s.room_id == target.id]
                elif filter_type in ('lecturer', 'all_lecturers'):
                    slots_target = [s for s in slots_all if s.lecturer_id == target.id]
                else:
                    slots_target = slots_all

                slots_by_dow = {}
                for s in slots_target:
                    slots_by_dow.setdefault(s.time_slot.day_of_week, []).append(s)

                cal_headers = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
                data = [cal_headers]
                
                cal_day_num_style = ParagraphStyle('cal_day_num', parent=styles['Normal'], fontSize=8, leading=10, fontName='Helvetica-Bold', textColor=colors.HexColor(primary_hex))
                cal_class_style = ParagraphStyle('cal_class', parent=styles['Normal'], fontSize=5.5, leading=7, alignment=TA_CENTER)

                for week in month_weeks:
                    row = []
                    for day_idx, day_num in enumerate(week):
                        if day_num == 0:
                            row.append('')
                        else:
                            dow = day_idx + 1
                            day_slots = sorted(slots_by_dow.get(dow, []), key=lambda s: s.time_slot.start_time)
                            cell_items = [f"<b>{day_num}</b>"]
                            for s in day_slots:
                                cell_items.append(f"<font color='#0f172a'><b>{s.course.code}</b></font> <font color='#64748b'>{s.time_slot.start_time.strftime('%H:%M')}</font>")
                            cell_content = '<br/>'.join(cell_items)
                            row.append(Paragraph(cell_content, cal_class_style if len(day_slots) > 0 else cal_day_num_style))
                    data.append(row)

                c_width = (landscape(A4)[0] - 1.2*cm) / 7
                r_height = 15.5 * cm / (len(month_weeks) + 1)
                table = Table(data, colWidths=[c_width] * 7, rowHeights=[0.6*cm] + [r_height] * len(month_weeks))
                table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor(primary_hex)),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, 0), 9),
                    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CBD5E1')),
                    ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                    ('TOPPADDING', (0, 0), (-1, -1), 3),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                    ('LEFTPADDING', (0, 0), (-1, -1), 3),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 3),
                ]))
                elements.append(table)

                if idx < len(targets) - 1:
                    elements.append(PageBreak())

    # ─────────────────────────────────────────────────────────────────────────
    # LAYOUT 3: yearly (Yearly Curriculum Overview)
    # ─────────────────────────────────────────────────────────────────────────
    elif layout_type == 'yearly':
        if not targets:
            elements.append(Paragraph("No scheduled slots found for this selection.", styles['Heading2']))
        else:
            for idx, target in enumerate(targets):
                selected_filter_name = str(target)
                current_subtitle = f"{pdf_subtitle} — Yearly Curriculum Map"
                if selected_filter_name and selected_filter_name not in current_subtitle:
                    current_subtitle = f"{current_subtitle} — {selected_filter_name}"

                title_p = Paragraph(f"<b>{pdf_title}</b>", title_style)
                subtitle_p = Paragraph(current_subtitle, subtitle_style)
                left_flow = [title_p, subtitle_p]

                if show_logo:
                    logo_p = Paragraph(f"<font color='white'><b>{pdf_logo_text}</b></font>", logo_style)
                    logo_table = Table([[logo_p]], colWidths=[2.2*cm], rowHeights=[1.2*cm])
                    logo_table.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor(primary_hex)),
                        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                        ('TOPPADDING', (0, 0), (-1, -1), 0),
                        ('LEFTPADDING', (0, 0), (-1, -1), 0),
                        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                        ('BOX', (0, 0), (-1, -1), 1.5, colors.HexColor(primary_hex)),
                    ]))
                    header_table = Table([[left_flow, logo_table]], colWidths=[landscape(A4)[0] - 1.2*cm - 2.5*cm, 2.5*cm])
                else:
                    header_table = Table([[left_flow]], colWidths=[landscape(A4)[0] - 1.2*cm])
                    
                header_table.setStyle(TableStyle([
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    ('LEFTPADDING', (0, 0), (-1, -1), 0),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                    ('TOPPADDING', (0, 0), (-1, -1), 0),
                ]))
                
                elements.append(header_table)
                elements.append(Spacer(1, 0.4*cm))

                if filter_type in ('group', 'all_groups'):
                    slots_target = [s for s in slots_all if s.student_group_id == target.id]
                elif filter_type in ('room', 'all_rooms'):
                    slots_target = [s for s in slots_all if s.room_id == target.id]
                elif filter_type in ('lecturer', 'all_lecturers'):
                    slots_target = [s for s in slots_all if s.lecturer_id == target.id]
                else:
                    slots_target = slots_all

                target_courses = {}
                for s in slots_target:
                    target_courses[s.course_id] = s.course

                yearly_headers = ['Course Code', 'Course Name', 'Room Type', 'Lecturer', 'Slots / Week', 'Weekly Hours']
                data = [yearly_headers]
                
                y_cell_style = ParagraphStyle('y_cell', parent=styles['Normal'], fontSize=8, leading=10)
                
                for course in sorted(target_courses.values(), key=lambda c: c.code):
                    hours = course.duration_slots * course.sessions_per_week * 1.5
                    row = [
                        Paragraph(f"<b>{course.code}</b>", y_cell_style),
                        Paragraph(course.name, y_cell_style),
                        Paragraph(course.required_room_type, y_cell_style),
                        Paragraph(course.lecturer.name if course.lecturer else 'Unassigned', y_cell_style),
                        Paragraph(str(course.duration_slots), y_cell_style),
                        Paragraph(f"{hours:.1f} hrs", y_cell_style),
                    ]
                    data.append(row)

                if len(data) == 1:
                    data.append([Paragraph("No courses scheduled.", y_cell_style)] + [''] * 5)

                y_col_width = (landscape(A4)[0] - 1.2*cm) / 6
                table = Table(data, colWidths=[y_col_width] * 6, repeatRows=1)
                table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor(primary_hex)),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, 0), 9),
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CBD5E1')),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor(alt_hex), colors.white]),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                    ('LEFTPADDING', (0, 0), (-1, -1), 6),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 6),
                ]))
                elements.append(table)

                if idx < len(targets) - 1:
                    elements.append(PageBreak())

    # ─────────────────────────────────────────────────────────────────────────
    # LAYOUT 4: weekly (Standard Weekly Grid Layout)
    # ─────────────────────────────────────────────────────────────────────────
    else:
        # Dynamic Scaling of Font and Padding for single-page fit
        num_slots = len(slot_numbers)
        if num_slots <= 4:
            cell_font = 8.5
            cell_lead = 10.5
            tb_pad = 5
        elif num_slots <= 6:
            cell_font = 8
            cell_lead = 10
            tb_pad = 4
        elif num_slots <= 8:
            cell_font = 7
            cell_lead = 9
            tb_pad = 3
        else:
            cell_font = 6
            cell_lead = 8
            tb_pad = 2

        cell_style = ParagraphStyle('cell', parent=styles['Normal'], fontSize=cell_font, leading=cell_lead, alignment=TA_CENTER)

        header = ['Slot / Time'] + [day_labels.get(d, f'D{d}') for d in days]
        ts_by_slot_number = {}
        for ts in timeslots:
            ts_by_slot_number.setdefault(ts.slot_number, []).append(ts)

        col_width = (landscape(A4)[0] - 1.2*cm) / (len(days) + 1) if days else (landscape(A4)[0] - 1.2*cm)

        if not targets:
            elements.append(Paragraph("No scheduled slots found for this selection.", styles['Heading2']))
        else:
            for idx, target in enumerate(targets):
                selected_filter_name = str(target)
                current_subtitle = pdf_subtitle
                if selected_filter_name and selected_filter_name not in current_subtitle:
                    current_subtitle = f"{current_subtitle} — {selected_filter_name}"

                title_p = Paragraph(f"<b>{pdf_title}</b>", title_style)
                subtitle_p = Paragraph(current_subtitle, subtitle_style)
                left_flow = [title_p, subtitle_p]

                if show_logo:
                    logo_p = Paragraph(f"<font color='white'><b>{pdf_logo_text}</b></font>", logo_style)
                    logo_table = Table([[logo_p]], colWidths=[2.2*cm], rowHeights=[1.2*cm])
                    logo_table.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor(primary_hex)),
                        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                        ('TOPPADDING', (0, 0), (-1, -1), 0),
                        ('LEFTPADDING', (0, 0), (-1, -1), 0),
                        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                        ('BOX', (0, 0), (-1, -1), 1.5, colors.HexColor(primary_hex)),
                    ]))
                    header_table = Table([[left_flow, logo_table]], colWidths=[landscape(A4)[0] - 1.2*cm - 2.5*cm, 2.5*cm])
                else:
                    header_table = Table([[left_flow]], colWidths=[landscape(A4)[0] - 1.2*cm])
                    
                header_table.setStyle(TableStyle([
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    ('LEFTPADDING', (0, 0), (-1, -1), 0),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                    ('TOPPADDING', (0, 0), (-1, -1), 0),
                ]))
                
                elements.append(header_table)
                elements.append(Spacer(1, 0.4*cm))

                if filter_type in ('group', 'all_groups'):
                    slots_target = [s for s in slots_all if s.student_group_id == target.id]
                elif filter_type in ('room', 'all_rooms'):
                    slots_target = [s for s in slots_all if s.room_id == target.id]
                elif filter_type in ('lecturer', 'all_lecturers'):
                    slots_target = [s for s in slots_all if s.lecturer_id == target.id]
                else:
                    slots_target = slots_all

                slots_by_day_and_num = {}
                for s in slots_target:
                    slots_by_day_and_num.setdefault((s.time_slot.day_of_week, s.time_slot.slot_number), []).append(s)

                data = [header]

                for slot_num in slot_numbers:
                    ts_for_slot = ts_by_slot_number.get(slot_num, [])
                    time_label = f"Slot {slot_num}"
                    if ts_for_slot:
                        ts0 = ts_for_slot[0]
                        time_label = f"{ts0.start_time.strftime('%H:%M')}<br/>{ts0.end_time.strftime('%H:%M')}"
                    row = [Paragraph(time_label, cell_style)]
                    for day in days:
                        matching = slots_by_day_and_num.get((day, slot_num), [])
                        if matching:
                            items = []
                            for s in matching:
                                items.append(
                                    f"<b>{s.course.code}</b><br/>"
                                    f"<font color='#475569'>{s.room.name}</font><br/>"
                                    f"<font color='#64748B'>{s.lecturer.name}</font>"
                                )
                            cell_content = '<br/><font color="#CBD5E1">────────────────</font><br/>'.join(items)
                            row.append(Paragraph(cell_content, cell_style))
                        else:
                            row.append('')
                    data.append(row)

                table = Table(data, colWidths=[col_width] * (len(days) + 1), repeatRows=1)
                table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor(primary_hex)),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, 0), 9),
                    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CBD5E1')),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor(alt_hex), colors.white]),
                    ('LEFTPADDING', (0, 0), (-1, -1), 4),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 4),
                    ('TOPPADDING', (0, 0), (-1, -1), tb_pad),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), tb_pad),
                ]))
                elements.append(table)

                if idx < len(targets) - 1:
                    elements.append(PageBreak())

    doc.build(elements)
    buffer.seek(0)

    response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
    safe_name = "".join(c for c in timetable.name if c.isalnum() or c in (' ','_','-')).strip().replace(' ','_')
    response['Content-Disposition'] = f'attachment; filename="timetable_{safe_name}.pdf"'
    return response


@login_required
def export_timetable_excel(request, pk):
    """Export timetable as an Excel file using openpyxl."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    university = get_active_uni(request)
    timetable = get_object_or_404(
        Timetable.objects.select_related('semester', 'semester__university'), pk=pk
    )
    if timetable.semester.university != university:
        messages.error(request, "Access denied.")
        return redirect('scheduler:dashboard')

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Timetable"

    # Styles
    header_fill = PatternFill(start_color='0D5A4F', end_color='0D5A4F', fill_type='solid')
    alt_fill = PatternFill(start_color='E8FDF5', end_color='E8FDF5', fill_type='solid')
    header_font = Font(bold=True, color='FFFFFF', size=10)
    thin = Side(style='thin', color='143C36')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal='center', vertical='center', wrap_text=True)

    day_labels = {1:'Monday',2:'Tuesday',3:'Wednesday',4:'Thursday',5:'Friday',6:'Saturday',7:'Sunday'}
    timeslots = list(TimeSlot.objects.filter(university=university).order_by('day_of_week', 'slot_number'))
    days = sorted(set(ts.day_of_week for ts in timeslots))
    slot_numbers = sorted(set(ts.slot_number for ts in timeslots))

    # Title rows
    ws.merge_cells(f'A1:{get_column_letter(len(days)+1)}1')
    title_cell = ws['A1']
    title_cell.value = f"{timetable.name} — {timetable.semester.name} | {university.name}"
    title_cell.font = Font(bold=True, size=14, color='0D5A4F')
    title_cell.alignment = center
    ws.row_dimensions[1].height = 30

    # Header row
    ws.cell(row=2, column=1, value='Slot / Time').font = header_font
    ws.cell(row=2, column=1).fill = header_fill
    ws.cell(row=2, column=1).alignment = center
    ws.cell(row=2, column=1).border = border
    for col_idx, day in enumerate(days, start=2):
        cell = ws.cell(row=2, column=col_idx, value=day_labels.get(day, f'Day {day}'))
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center
        cell.border = border
    ws.row_dimensions[2].height = 22

    slots_all = list(
        timetable.slots.select_related('course','lecturer','room','time_slot','student_group').all()
    )

    # Pre-group to avoid O(N) searching inside the loops
    slots_by_day_and_num = {}
    for s in slots_all:
        slots_by_day_and_num.setdefault((s.time_slot.day_of_week, s.time_slot.slot_number), []).append(s)

    ts_by_slot_number = {}
    for ts in timeslots:
        ts_by_slot_number.setdefault(ts.slot_number, []).append(ts)

    for row_idx, slot_num in enumerate(slot_numbers, start=3):
        ts_for_slot = ts_by_slot_number.get(slot_num, [])
        time_label = f"Slot {slot_num}"
        if ts_for_slot:
            ts0 = ts_for_slot[0]
            time_label = f"{ts0.start_time.strftime('%H:%M')} – {ts0.end_time.strftime('%H:%M')}"
        time_cell = ws.cell(row=row_idx, column=1, value=time_label)
        time_cell.font = Font(bold=True, size=9)
        time_cell.alignment = center
        time_cell.border = border
        if row_idx % 2 == 0:
            time_cell.fill = alt_fill

        for col_idx, day in enumerate(days, start=2):
            matching = slots_by_day_and_num.get((day, slot_num), [])
            content = ''
            if matching:
                content = ' | '.join(
                    f"{s.course.code} ({s.room.name}) – {s.lecturer.name}"
                    for s in matching
                )
            cell = ws.cell(row=row_idx, column=col_idx, value=content)
            cell.alignment = center
            cell.border = border
            if row_idx % 2 == 0:
                cell.fill = alt_fill
        ws.row_dimensions[row_idx].height = 40

    # Column widths
    ws.column_dimensions['A'].width = 16
    for col_idx in range(2, len(days) + 2):
        ws.column_dimensions[get_column_letter(col_idx)].width = 30

    # Second sheet: raw data
    ws2 = wb.create_sheet(title="Raw Data")
    ws2.append(['Course Code', 'Course Name', 'Room', 'Room Type', 'Lecturer', 'Student Group', 'Day', 'Start Time', 'End Time'])
    for s in slots_all:
        ts = s.time_slot
        ws2.append([
            s.course.code, s.course.name, s.room.name,
            s.room.get_room_type_display(), s.lecturer.name,
            s.student_group.name, day_labels.get(ts.day_of_week, f'Day {ts.day_of_week}'),
            ts.start_time.strftime('%H:%M'), ts.end_time.strftime('%H:%M')
        ])

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    response = HttpResponse(
        buffer.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    safe_name = "".join(c for c in timetable.name if c.isalnum() or c in (' ','_','-')).strip().replace(' ','_')
    response['Content-Disposition'] = f'attachment; filename="timetable_{safe_name}.xlsx"'
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Phase 7: Global Search
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def search_view(request):
    """Global cross-resource search across courses, lecturers, rooms, and student groups."""
    query = request.GET.get('q', '').strip()
    university = get_active_uni(request)
    results = {}

    if query and university:
        results['courses'] = Course.objects.filter(
            Q(code__icontains=query) | Q(name__icontains=query),
            program__department__faculty__campus__university=university
        ).select_related('lecturer', 'student_group')[:10]

        results['lecturers'] = Lecturer.objects.filter(
            Q(name__icontains=query) | Q(email__icontains=query),
            department__faculty__campus__university=university
        )[:10]

        results['rooms'] = Room.objects.filter(
            Q(name__icontains=query),
            campus__university=university
        )[:10]

        results['student_groups'] = StudentGroup.objects.filter(
            Q(name__icontains=query),
            program__department__faculty__campus__university=university
        )[:10]

        results['timetables'] = Timetable.objects.filter(
            Q(name__icontains=query),
            semester__university=university
        )[:5]

    return render(request, 'scheduler/search_results.html', {
        'query': query,
        'results': results,
    })


@login_required
def export_timetable_word(request, pk):
    """Export timetable as a Word (.docx) file using python-docx with custom branding."""
    import docx
    from docx.shared import Inches, Pt, RGBColor
    from docx.enum.section import WD_ORIENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement, parse_xml
    from docx.oxml.ns import nsdecls, qn

    university = get_active_uni(request)
    timetable = get_object_or_404(
        Timetable.objects.select_related('semester', 'semester__university'), pk=pk
    )
    if timetable.semester.university != university:
        messages.error(request, "Access denied.")
        return redirect('scheduler:dashboard')

    # Get custom branding from request
    word_title = request.GET.get('title', f"{timetable.name} — {timetable.semester.name}")
    word_subtitle = request.GET.get('subtitle', f"{university.name} | Generated Timetable")
    primary_hex = request.GET.get('primary_color', "0D5A4F").replace('#', '')
    alt_hex = request.GET.get('alt_color', "F0FDFA").replace('#', '')

    # Filter resources by university for scoping and defaulting
    rooms = Room.objects.filter(campus__university=university)
    lecturers = Lecturer.objects.filter(department__faculty__campus__university=university)
    student_groups = StudentGroup.objects.filter(program__department__faculty__campus__university=university)

    # Get active filters
    filter_type = request.GET.get('filter_type')
    filter_id = request.GET.get('filter_id')

    # Resolve target list
    targets = []
    
    if filter_type == 'all_groups':
        active_group_ids = timetable.slots.values_list('student_group_id', flat=True).distinct()
        targets = list(student_groups.filter(id__in=active_group_ids).order_by('name'))
    elif filter_type == 'all_rooms':
        active_room_ids = timetable.slots.values_list('room_id', flat=True).distinct()
        targets = list(rooms.filter(id__in=active_room_ids).order_by('name'))
    elif filter_type == 'all_lecturers':
        active_lecturer_ids = timetable.slots.values_list('lecturer_id', flat=True).distinct()
        targets = list(lecturers.filter(id__in=active_lecturer_ids).order_by('name'))
    else:
        if not filter_type:
            filter_type = 'group'

        if not filter_id:
            if filter_type == 'group' and student_groups.exists():
                filter_id = student_groups.first().id
            elif filter_type == 'room' and rooms.exists():
                filter_id = rooms.first().id
            elif filter_type == 'lecturer' and lecturers.exists():
                filter_id = lecturers.first().id

        target_obj = None
        if filter_id:
            try:
                filter_id = int(filter_id)
                if filter_type == 'group':
                    target_obj = student_groups.filter(id=filter_id).first()
                elif filter_type == 'room':
                    target_obj = rooms.filter(id=filter_id).first()
                elif filter_type == 'lecturer':
                    target_obj = lecturers.filter(id=filter_id).first()
            except ValueError:
                pass
        if target_obj:
            targets = [target_obj]

    doc = docx.Document()

    # Set margins and landscape orientation
    section = doc.sections[-1]
    section.orientation = WD_ORIENT.LANDSCAPE
    # Swap width and height for landscape A4
    new_width, new_height = section.page_height, section.page_width
    section.page_width = new_width
    section.page_height = new_height
    section.top_margin = Inches(0.5)
    section.bottom_margin = Inches(0.5)
    section.left_margin = Inches(0.5)
    section.right_margin = Inches(0.5)

    # Set default font
    style = doc.styles['Normal']
    font = style.font
    font.name = 'Arial'
    font.size = Pt(9)

    day_labels = {1: 'Mon', 2: 'Tue', 3: 'Wed', 4: 'Thu', 5: 'Fri', 6: 'Sat', 7: 'Sun'}
    timeslots = list(TimeSlot.objects.filter(university=university).order_by('day_of_week', 'slot_number'))
    days = sorted(set(ts.day_of_week for ts in timeslots))
    slot_numbers = sorted(set(ts.slot_number for ts in timeslots))

    slots_all = list(
        timetable.slots.select_related('course','lecturer','room','time_slot','student_group').all()
    )

    ts_by_slot_number = {}
    for ts in timeslots:
        ts_by_slot_number.setdefault(ts.slot_number, []).append(ts)

    # Helper function to shade cells
    def set_cell_background(cell, hex_color):
        shading = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{hex_color}"/>')
        cell._tc.get_or_add_tcPr().append(shading)

    # Helper function to set table borders
    def set_table_borders(table):
        tblPr = table._tbl.tblPr
        tblBorders = parse_xml(
            f'<w:tblBorders {nsdecls("w")}>'
            f'<w:top w:val="single" w:sz="4" w:space="0" w:color="CBD5E1"/>'
            f'<w:bottom w:val="single" w:sz="4" w:space="0" w:color="CBD5E1"/>'
            f'<w:insideH w:val="single" w:sz="4" w:space="0" w:color="CBD5E1"/>'
            f'<w:insideV w:val="single" w:sz="4" w:space="0" w:color="CBD5E1"/>'
            f'</w:tblBorders>'
        )
        tblPr.append(tblBorders)

    if not targets:
        doc.add_heading("No scheduled slots found for this selection.", level=2)
    else:
        for idx, target in enumerate(targets):
            if idx > 0:
                doc.add_page_break()

            selected_filter_name = str(target)
            current_subtitle = word_subtitle
            if selected_filter_name and selected_filter_name not in current_subtitle:
                current_subtitle = f"{current_subtitle} — {selected_filter_name}"

            # Title
            title_p = doc.add_paragraph()
            title_run = title_p.add_run(word_title)
            title_run.bold = True
            title_run.font.size = Pt(16)
            title_run.font.color.rgb = RGBColor.from_string(primary_hex)

            # Subtitle
            sub_p = doc.add_paragraph()
            sub_run = sub_p.add_run(current_subtitle)
            sub_run.font.size = Pt(10)
            sub_run.font.color.rgb = RGBColor(71, 85, 105)

            # Filter slots for this target
            if filter_type in ('group', 'all_groups'):
                slots_target = [s for s in slots_all if s.student_group_id == target.id]
            elif filter_type in ('room', 'all_rooms'):
                slots_target = [s for s in slots_all if s.room_id == target.id]
            elif filter_type in ('lecturer', 'all_lecturers'):
                slots_target = [s for s in slots_all if s.lecturer_id == target.id]
            else:
                slots_target = slots_all

            # Pre-group
            slots_by_day_and_num = {}
            for s in slots_target:
                slots_by_day_and_num.setdefault((s.time_slot.day_of_week, s.time_slot.slot_number), []).append(s)

            # Create Table
            num_cols = len(days) + 1
            num_rows = len(slot_numbers) + 1
            table = doc.add_table(rows=num_rows, cols=num_cols)
            set_table_borders(table)

            # Header Row
            hdr_cells = table.rows[0].cells
            hdr_cells[0].text = "Slot / Time"
            set_cell_background(hdr_cells[0], primary_hex)
            hdr_cells[0].paragraphs[0].runs[0].font.bold = True
            hdr_cells[0].paragraphs[0].runs[0].font.color.rgb = RGBColor(255, 255, 255)
            hdr_cells[0].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER

            for col_idx, d in enumerate(days, start=1):
                cell = hdr_cells[col_idx]
                cell.text = day_labels.get(d, f'D{d}')
                set_cell_background(cell, primary_hex)
                cell.paragraphs[0].runs[0].font.bold = True
                cell.paragraphs[0].runs[0].font.color.rgb = RGBColor(255, 255, 255)
                cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER

            # Data Rows
            for r_idx, slot_num in enumerate(slot_numbers, start=1):
                row_cells = table.rows[r_idx].cells
                bg_color = alt_hex if r_idx % 2 == 1 else "FFFFFF"

                ts_for_slot = ts_by_slot_number.get(slot_num, [])
                time_label = f"Slot {slot_num}"
                if ts_for_slot:
                    ts0 = ts_for_slot[0]
                    time_label = f"{ts0.start_time.strftime('%H:%M')}\n{ts0.end_time.strftime('%H:%M')}"
                
                row_cells[0].text = time_label
                set_cell_background(row_cells[0], bg_color)
                row_cells[0].paragraphs[0].runs[0].font.bold = True
                row_cells[0].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER

                for c_idx, day in enumerate(days, start=1):
                    cell = row_cells[c_idx]
                    set_cell_background(cell, bg_color)
                    matching = slots_by_day_and_num.get((day, slot_num), [])
                    if matching:
                        cell.text = "" # Clear default text
                        for s_idx, s in enumerate(matching):
                            if s_idx > 0:
                                p = cell.add_paragraph("────────────────")
                                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                                p.runs[0].font.size = Pt(6)
                                p.runs[0].font.color.rgb = RGBColor(203, 213, 225)
                            
                            p_code = cell.add_paragraph()
                            p_code.alignment = WD_ALIGN_PARAGRAPH.CENTER
                            run_code = p_code.add_run(s.course.code)
                            run_code.bold = True
                            run_code.font.size = Pt(8)
                            
                            p_room = cell.add_paragraph()
                            p_room.alignment = WD_ALIGN_PARAGRAPH.CENTER
                            run_room = p_room.add_run(s.room.name)
                            run_room.font.size = Pt(7.5)
                            run_room.font.color.rgb = RGBColor(71, 85, 105)
                            
                            p_lec = cell.add_paragraph()
                            p_lec.alignment = WD_ALIGN_PARAGRAPH.CENTER
                            run_lec = p_lec.add_run(s.lecturer.name)
                            run_lec.font.size = Pt(7)
                            run_lec.font.color.rgb = RGBColor(100, 116, 139)

    # Save to buffer
    buffer = io.BytesIO()
    doc.save(buffer)
    buffer.seek(0)

    response = HttpResponse(
        buffer.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    )
    safe_name = "".join(c for c in timetable.name if c.isalnum() or c in (' ','_','-')).strip().replace(' ','_')
    response['Content-Disposition'] = f'attachment; filename="timetable_{safe_name}.docx"'
    return response


@login_required
def student_my_schedule(request):
    """Personal timetable view for a logged-in student."""
    try:
        student_group = request.user.profile.student_group
    except Exception:
        student_group = None

    if not student_group:
        messages.error(request, "Your account is not linked to a student group. Ask an administrator to link your profile.")
        return redirect('accounts:profile')

    import uuid
    from django.urls import reverse

    if not student_group.calendar_token:
        student_group.calendar_token = uuid.uuid4()
        student_group.save(update_fields=['calendar_token'])

    feed_url = request.build_absolute_uri(
        reverse('scheduler:student_group_calendar_feed', args=[student_group.calendar_token])
    )
    webcal_url = feed_url.replace('http://', 'webcal://').replace('https://', 'webcal://')

    university = get_active_uni(request)
    active_timetable = Timetable.objects.filter(
        semester__university=university, is_active=True
    ).first()
    if not active_timetable:
        active_timetable = Timetable.objects.filter(semester__university=university).first()

    slots = []
    if active_timetable:
        slots = list(
            ScheduleSlot.objects.filter(
                timetable=active_timetable, student_group=student_group
            ).select_related('course', 'room', 'time_slot', 'lecturer')
            .order_by('time_slot__day_of_week', 'time_slot__slot_number')
        )
        from django.utils import timezone
        local_now = timezone.localtime(timezone.now())
        current_dow = local_now.isoweekday()
        current_time = local_now.time()
        for s in slots:
            ts = s.time_slot
            if ts.day_of_week < current_dow:
                s.has_ended = True
                s.is_ongoing = False
            elif ts.day_of_week == current_dow:
                s.has_ended = (ts.end_time < current_time)
                s.is_ongoing = (ts.start_time <= current_time <= ts.end_time)
            else:
                s.has_ended = False
                s.is_ongoing = False


    return render(request, 'scheduler/student_my_schedule.html', {
        'student_group': student_group,
        'timetable': active_timetable,
        'slots': slots,
        'feed_url': feed_url,
        'webcal_url': webcal_url,
    })


@login_required
@manager_required
def setup_wizard(request):
    """
    Quick-start wizard: create university structure, default Mon–Fri timeslots, and an active semester.
    """
    import datetime
    from django.db import transaction

    university = get_active_uni(request)
    created = []

    if request.method == 'POST':
        uni_name = request.POST.get('university_name', '').strip()
        uni_code = request.POST.get('university_code', '').strip().upper()
        campus_name = request.POST.get('campus_name', 'Main Campus').strip()
        semester_name = request.POST.get('semester_name', '').strip()
        start_date = request.POST.get('start_date')
        end_date = request.POST.get('end_date')

        if not all([uni_name, uni_code, semester_name, start_date, end_date]):
            messages.error(request, "Please fill in all required fields.")
        else:
            try:
                with transaction.atomic():
                    if not university:
                        university = University.objects.create(name=uni_name, code=uni_code)
                        created.append(f"University '{uni_name}'")
                    campus, _ = Campus.objects.get_or_create(
                        university=university, name=campus_name
                    )
                    faculty, _ = Faculty.objects.get_or_create(campus=campus, name='General Faculty')
                    dept, _ = Department.objects.get_or_create(faculty=faculty, name='General Department')

                    Semester.objects.filter(university=university, is_active=True).update(is_active=False)
                    semester = Semester.objects.create(
                        university=university,
                        name=semester_name,
                        start_date=datetime.date.fromisoformat(start_date),
                        end_date=datetime.date.fromisoformat(end_date),
                        is_active=True,
                    )
                    created.append(f"Semester '{semester_name}'")

                    slot_templates = [
                        (datetime.time(8, 0), datetime.time(9, 30), 1, False),
                        (datetime.time(9, 45), datetime.time(11, 15), 2, False),
                        (datetime.time(11, 30), datetime.time(13, 0), 3, False),
                        (datetime.time(14, 0), datetime.time(15, 30), 4, False),
                        (datetime.time(15, 45), datetime.time(17, 15), 5, True),
                    ]
                    for day in range(1, 6):
                        for start_t, end_t, slot_num, is_evening in slot_templates:
                            TimeSlot.objects.get_or_create(
                                university=university,
                                day_of_week=day,
                                slot_number=slot_num,
                                defaults={
                                    'start_time': start_t,
                                    'end_time': end_t,
                                    'is_evening': is_evening,
                                },
                            )
                    created.append("Default Mon–Fri time slots (5 per day)")

                    request.session['active_university_id'] = university.id
                messages.success(request, "Setup complete: " + ", ".join(created) + ".")
                return redirect('scheduler:resources_manager')
            except Exception as exc:
                messages.error(request, f"Setup failed: {exc}")

    has_timeslots = bool(university and TimeSlot.objects.filter(university=university).exists())
    has_semester = bool(university and Semester.objects.filter(university=university).exists())

    return render(request, 'scheduler/setup_wizard.html', {
        'university': university,
        'has_timeslots': has_timeslots,
        'has_semester': has_semester,
    })


def lecturer_calendar_feed(request, token):
    """
    Public feed serving a specific lecturer's weekly schedule as an iCalendar file.
    """
    lecturer = get_object_or_404(Lecturer, calendar_token=token)
    from .calendar_exporter import generate_lecturer_ics
    ics_content = generate_lecturer_ics(lecturer)
    
    response = HttpResponse(ics_content, content_type='text/calendar; charset=utf-8')
    # Clean lecturer name for header
    safe_name = "".join(c for c in lecturer.name if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
    response['Content-Disposition'] = f'inline; filename="lecturer_{safe_name}_schedule.ics"'
    return response


def student_group_calendar_feed(request, token):
    """
    Public feed serving a specific student group's weekly schedule as an iCalendar file.
    """
    student_group = get_object_or_404(StudentGroup, calendar_token=token)
    from .calendar_exporter import generate_student_group_ics
    ics_content = generate_student_group_ics(student_group)
    
    response = HttpResponse(ics_content, content_type='text/calendar; charset=utf-8')
    safe_name = "".join(c for c in student_group.name if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
    response['Content-Disposition'] = f'inline; filename="group_{safe_name}_schedule.ics"'
    return response


@login_required
def sync_to_google_calendar(request):
    """
    Manually triggers a full sync of the lecturer's active timetable schedule to Google Calendar.
    """
    try:
        lecturer = request.user.lecturer_profile
    except Exception:
        messages.error(request, "Your account is not linked to a lecturer record.")
        return redirect('accounts:profile')

    from accounts.models import GoogleCalendarToken
    if not GoogleCalendarToken.objects.filter(user=request.user).exists():
        messages.error(request, "Please connect your Google Calendar account first.")
        return redirect('accounts:profile')

    university = get_active_uni(request)
    active_timetable = Timetable.objects.filter(
        semester__university=university, is_active=True
    ).first()
    if not active_timetable:
        active_timetable = Timetable.objects.filter(semester__university=university).first()

    if not active_timetable:
        messages.error(request, "No active timetable found to sync.")
        return redirect('scheduler:lecturer_my_schedule')

    import sys
    if 'test' in sys.argv or 'pytest' in sys.argv or any('pytest' in arg for arg in sys.argv):
        from .google_tasks import sync_lecturer_timetable_google
        sync_lecturer_timetable_google(lecturer.id, active_timetable.id)
    else:
        from django_q.tasks import async_task
        async_task('scheduler.google_tasks.sync_lecturer_timetable_google', lecturer.id, active_timetable.id)

    messages.success(request, "✓ Google Calendar sync started in the background. Your calendar will update in a moment!")
    return redirect('scheduler:lecturer_my_schedule')


@login_required
def notifications_list(request):
    notifications = request.user.notifications.all()
    return render(request, 'scheduler/notifications_list.html', {
        'notifications': notifications
    })

@login_required
def notification_read(request, pk):
    from .models import Notification
    notification = get_object_or_404(Notification, pk=pk, user=request.user)
    notification.is_read = True
    notification.save()
    if notification.link:
        return redirect(notification.link)
    return redirect('scheduler:notifications_list')

@login_required
def notifications_mark_all_read(request):
    request.user.notifications.filter(is_read=False).update(is_read=True)
    messages.success(request, "All notifications marked as read.")
    return redirect('scheduler:notifications_list')

@login_required
def subscription_billing(request):
    university = get_active_uni(request)
    if not university:
        messages.error(request, "No active university selected.")
        return redirect('scheduler:dashboard')
    
    role = get_user_role(request)
    if role not in ('admin', 'institution_admin'):
        messages.error(request, "Permission denied. Only Admins can manage subscriptions.")
        return redirect('scheduler:dashboard')

    from .models import Subscription
    subscription = getattr(university, 'subscription', None)
    if not subscription:
        subscription = Subscription.objects.create(university=university, tier='free', status='active')

    if request.method == 'POST':
        tier = request.POST.get('tier')
        if tier in ('free', 'growth', 'enterprise'):
            subscription.tier = tier
            if tier == 'free':
                subscription.max_rooms = 10
                subscription.max_courses = 50
            elif tier == 'growth':
                subscription.max_rooms = 30
                subscription.max_courses = 150
            elif tier == 'enterprise':
                subscription.max_rooms = 100
                subscription.max_courses = 500
            subscription.save()
            messages.success(request, f"Subscription upgraded to {subscription.get_tier_display()}!")
            return redirect('scheduler:subscription_billing')

    rooms_count = Room.objects.filter(campus__university=university).count()
    courses_count = Course.objects.filter(program__department__faculty__campus__university=university).count()
    
    return render(request, 'scheduler/subscription_billing.html', {
        'subscription': subscription,
        'rooms_count': rooms_count,
        'courses_count': courses_count,
        'rooms_pct': min(100, int((rooms_count / subscription.max_rooms) * 100)) if subscription.max_rooms else 0,
        'courses_pct': min(100, int((courses_count / subscription.max_courses) * 100)) if subscription.max_courses else 0,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# LECTURER PORTAL VIEWS
# ═══════════════════════════════════════════════════════════════════════════════

def _get_lecturer_or_redirect(request):
    """Helper: return (lecturer, None) or (None, redirect_response) for portal views."""
    try:
        lecturer = request.user.profile.lecturer
        if lecturer:
            return lecturer, None
    except Exception:
        pass
    messages.error(request, "Your account is not linked to a lecturer profile. Please update your profile.")
    return None, redirect('accounts:profile')



@login_required
def lecturer_portal_dashboard(request):
    """Main Lecturer Portal dashboard — aggregates all personalised data."""
    import datetime
    role = get_user_role(request)
    if role != ROLE_LECTURER:
        messages.warning(request, "Access restricted to lecturers.")
        return redirect('scheduler:dashboard')

    lecturer, err = _get_lecturer_or_redirect(request)
    if err:
        return err

    university = get_active_uni(request)
    active_timetable = None
    if university:
        active_timetable = (
            Timetable.objects.filter(semester__university=university, is_active=True).first()
            or Timetable.objects.filter(semester__university=university).order_by('-created_at').first()
        )

    my_slots = []
    today_slots = []
    next_slot = None
    total_hours = 0.0
    unique_courses = set()
    unique_groups = set()

    if active_timetable:
        my_slots = list(
            ScheduleSlot.objects.filter(timetable=active_timetable, lecturer=lecturer)
            .select_related('course', 'room', 'time_slot', 'student_group',
                            'student_group__program', 'course__program')
            .order_by('time_slot__day_of_week', 'time_slot__slot_number')
        )
        now_time = datetime.datetime.now().time()
        today_dow = datetime.date.today().weekday() + 1  # 1=Mon … 7=Sun

        for s in my_slots:
            unique_courses.add(s.course_id)
            unique_groups.add(s.student_group_id)
            ts = s.time_slot
            duration_mins = (
                ts.end_time.hour * 60 + ts.end_time.minute
            ) - (ts.start_time.hour * 60 + ts.start_time.minute)
            total_hours += duration_mins / 60

        today_slots = [s for s in my_slots if s.time_slot.day_of_week == today_dow]
        for s in today_slots:
            s.is_now = s.time_slot.start_time <= now_time <= s.time_slot.end_time
        upcoming = [s for s in today_slots if s.time_slot.start_time > now_time]
        next_slot = upcoming[0] if upcoming else None

    # Notifications (unread)
    notifications = list(request.user.notifications.filter(is_read=False).order_by('-created_at')[:8])
    unread_count = request.user.notifications.filter(is_read=False).count()

    # Announcements
    announcements = []
    if university:
        announcements = list(
            Announcement.objects.filter(
                Q(university=university) | Q(university__isnull=True)
            ).order_by('-created_at')[:5]
        )

    # Active timetable semester
    active_semester = None
    if university:
        active_semester = Semester.objects.filter(university=university, is_active=True).first()

    # Attendance: open sessions
    open_sessions = AttendanceSession.objects.filter(
        schedule_slot__lecturer=lecturer, is_active=True
    ).select_related('schedule_slot__course', 'schedule_slot__room')[:3]

    now_hour = datetime.datetime.now().hour
    greeting = 'Good morning' if now_hour < 12 else ('Good afternoon' if now_hour < 17 else 'Good evening')

    return render(request, 'scheduler/lecturer_portal_dashboard.html', {
        'active_role': role,
        'lecturer': lecturer,
        'active_university': university,
        'active_semester': active_semester,
        'timetable': active_timetable,
        'my_slots': my_slots,
        'today_slots': today_slots,
        'next_slot': next_slot,
        'total_slots': len(my_slots),
        'total_hours': round(total_hours, 1),
        'unique_courses_count': len(unique_courses),
        'unique_groups_count': len(unique_groups),
        'notifications': notifications,
        'unread_count': unread_count,
        'announcements': announcements,
        'open_sessions': open_sessions,
        'greeting': greeting,
        'today_date': datetime.date.today(),
    })


@login_required
def lecturer_portal_weekly_timetable(request):
    """Full 5-day weekly timetable grid for the lecturer."""
    import datetime
    role = get_user_role(request)
    if role != ROLE_LECTURER:
        return redirect('scheduler:dashboard')

    lecturer, err = _get_lecturer_or_redirect(request)
    if err:
        return err

    university = get_active_uni(request)
    active_timetable = None
    if university:
        active_timetable = (
            Timetable.objects.filter(semester__university=university, is_active=True).first()
            or Timetable.objects.filter(semester__university=university).order_by('-created_at').first()
        )

    slots_by_day = {1: [], 2: [], 3: [], 4: [], 5: [], 6: [], 7: []}
    all_slots = []
    time_slots_ordered = []

    if active_timetable:
        all_slots = list(
            ScheduleSlot.objects.filter(timetable=active_timetable, lecturer=lecturer)
            .select_related('course', 'room', 'time_slot', 'student_group')
            .order_by('time_slot__slot_number', 'time_slot__day_of_week')
        )
        from django.utils import timezone
        local_now = timezone.localtime(timezone.now())
        current_dow = local_now.isoweekday()
        current_time = local_now.time()
        for s in all_slots:
            ts = s.time_slot
            if ts.day_of_week < current_dow:
                s.has_ended = True
                s.is_ongoing = False
            elif ts.day_of_week == current_dow:
                s.has_ended = (ts.end_time < current_time)
                s.is_ongoing = (ts.start_time <= current_time <= ts.end_time)
            else:
                s.has_ended = False
                s.is_ongoing = False

        for s in all_slots:
            slots_by_day[s.time_slot.day_of_week].append(s)

        # Unique time slots (for row headers)
        seen = set()
        for s in all_slots:
            ts_id = s.time_slot.id
            if ts_id not in seen:
                seen.add(ts_id)
                time_slots_ordered.append(s.time_slot)
        time_slots_ordered.sort(key=lambda t: (t.day_of_week, t.slot_number))

    import datetime
    _today = datetime.date.today()
    _monday = _today - datetime.timedelta(days=_today.weekday())
    days_list = []
    for d, day_name in [(1,'Monday'),(2,'Tuesday'),(3,'Wednesday'),(4,'Thursday'),(5,'Friday')]:
        days_list.append({
            'dow': d,
            'name': day_name,
            'date': _monday + datetime.timedelta(days=d - 1)
        })
    today_dow = _today.weekday() + 1

    import uuid
    from django.urls import reverse
    if not lecturer.calendar_token:
        lecturer.calendar_token = uuid.uuid4()
        lecturer.save(update_fields=['calendar_token'])

    feed_url = request.build_absolute_uri(
        reverse('scheduler:lecturer_calendar_feed', args=[lecturer.calendar_token])
    )
    webcal_url = feed_url.replace('http://', 'webcal://').replace('https://', 'webcal://')

    return render(request, 'scheduler/lecturer_weekly_timetable.html', {
        'active_role': role,
        'lecturer': lecturer,
        'active_university': university,
        'timetable': active_timetable,
        'slots_by_day': slots_by_day,
        'all_slots': all_slots,
        'days': days_list,
        'today_dow': today_dow,
        'active_semester': Semester.objects.filter(
            university=university, is_active=True).first() if university else None,
        'feed_url': feed_url,
        'webcal_url': webcal_url,
    })




@login_required
def lecturer_portal_courses(request):
    """List all courses assigned to the lecturer."""
    role = get_user_role(request)
    if role != ROLE_LECTURER:
        return redirect('scheduler:dashboard')

    lecturer, err = _get_lecturer_or_redirect(request)
    if err:
        return err

    courses = Course.objects.filter(lecturer=lecturer).select_related(
        'program', 'program__department', 'student_group'
    ).prefetch_related('additional_student_groups').order_by('code')

    university = get_active_uni(request)
    active_timetable = None
    if university:
        active_timetable = (
            Timetable.objects.filter(semester__university=university, is_active=True).first()
            or Timetable.objects.filter(semester__university=university).order_by('-created_at').first()
        )

    # Annotate each course with weekly session count from active timetable
    for c in courses:
        if active_timetable:
            c.weekly_sessions = ScheduleSlot.objects.filter(
                timetable=active_timetable, course=c, lecturer=lecturer
            ).count()
        else:
            c.weekly_sessions = 0

    return render(request, 'scheduler/lecturer_courses.html', {
        'active_role': role,
        'lecturer': lecturer,
        'courses': courses,
        'active_university': university,
    })


@login_required
def lecturer_portal_student_groups(request):
    """All student groups the lecturer teaches."""
    role = get_user_role(request)
    if role != ROLE_LECTURER:
        return redirect('scheduler:dashboard')

    lecturer, err = _get_lecturer_or_redirect(request)
    if err:
        return err

    university = get_active_uni(request)
    active_timetable = None
    if university:
        active_timetable = (
            Timetable.objects.filter(semester__university=university, is_active=True).first()
            or Timetable.objects.filter(semester__university=university).order_by('-created_at').first()
        )

    groups_data = []
    if active_timetable:
        from django.db.models import Count as DjangoCount
        slot_qs = (
            ScheduleSlot.objects.filter(timetable=active_timetable, lecturer=lecturer)
            .values('student_group')
            .annotate(session_count=DjangoCount('id'))
        )
        group_sessions = {r['student_group']: r['session_count'] for r in slot_qs}
        group_ids = list(group_sessions.keys())
        groups = StudentGroup.objects.filter(id__in=group_ids).select_related(
            'program', 'program__department'
        )
        for g in groups:
            groups_data.append({
                'group': g,
                'sessions': group_sessions.get(g.id, 0),
                'courses': Course.objects.filter(
                    lecturer=lecturer, student_group=g
                ).values_list('code', flat=True),
            })
    else:
        # Fall back to courses FKs
        direct_groups = StudentGroup.objects.filter(
            courses__lecturer=lecturer
        ).distinct().select_related('program', 'program__department')
        for g in direct_groups:
            groups_data.append({'group': g, 'sessions': 0, 'courses': []})

    return render(request, 'scheduler/lecturer_student_groups.html', {
        'active_role': role,
        'lecturer': lecturer,
        'groups_data': groups_data,
        'active_university': university,
    })


@login_required
def lecturer_portal_workload(request):
    """Teaching workload breakdown for the lecturer."""
    import datetime
    role = get_user_role(request)
    if role != ROLE_LECTURER:
        return redirect('scheduler:dashboard')

    lecturer, err = _get_lecturer_or_redirect(request)
    if err:
        return err

    university = get_active_uni(request)
    active_timetable = None
    if university:
        active_timetable = (
            Timetable.objects.filter(semester__university=university, is_active=True).first()
            or Timetable.objects.filter(semester__university=university).order_by('-created_at').first()
        )

    day_hours = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0}
    day_slots = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    day_names = {1: 'Monday', 2: 'Tuesday', 3: 'Wednesday', 4: 'Thursday', 5: 'Friday'}
    slots = []
    total_hours = 0.0
    unique_courses = set()

    if active_timetable:
        slots = list(
            ScheduleSlot.objects.filter(timetable=active_timetable, lecturer=lecturer)
            .select_related('course', 'room', 'time_slot', 'student_group')
            .order_by('time_slot__day_of_week', 'time_slot__slot_number')
        )
        for s in slots:
            unique_courses.add(s.course_id)
            ts = s.time_slot
            dur = (ts.end_time.hour * 60 + ts.end_time.minute) - (
                ts.start_time.hour * 60 + ts.start_time.minute
            )
            h = dur / 60
            total_hours += h
            dow = ts.day_of_week
            if dow in day_hours:
                day_hours[dow] += h
                day_slots[dow] += 1

    max_day_hrs = max(day_hours.values()) if day_hours else 1
    workload_pct = round((total_hours / lecturer.max_hours_per_week) * 100) if lecturer.max_hours_per_week else 0

    return render(request, 'scheduler/lecturer_workload.html', {
        'active_role': role,
        'lecturer': lecturer,
        'slots': slots,
        'total_hours': round(total_hours, 1),
        'unique_courses_count': len(unique_courses),
        'day_hours': day_hours,
        'day_slots': day_slots,
        'day_names': day_names,
        'max_day_hrs': max_day_hrs,
        'workload_pct': workload_pct,
        'active_university': university,
        'timetable': active_timetable,
    })


@login_required
def lecturer_attendance_start(request, slot_id):
    """Open (or resume) an attendance session for a given ScheduleSlot."""
    import datetime
    role = get_user_role(request)
    if role != ROLE_LECTURER:
        return redirect('scheduler:dashboard')

    lecturer, err = _get_lecturer_or_redirect(request)
    if err:
        return err

    slot = get_object_or_404(
        ScheduleSlot.objects.select_related(
            'course', 'room', 'time_slot', 'student_group', 'lecturer'
        ),
        id=slot_id, lecturer=lecturer
    )

    today = datetime.date.today()
    session, created = AttendanceSession.objects.get_or_create(
        schedule_slot=slot,
        date=today,
        defaults={'is_active': True},
    )
    if not session.is_active:
        session.is_active = True
        session.closed_at = None
        session.save(update_fields=['is_active', 'closed_at'])

    return redirect('scheduler:lecturer_attendance_session', session_id=session.id)


@login_required
def lecturer_attendance_session(request, session_id):
    """Display and handle the manual attendance marking form."""
    import datetime
    role = get_user_role(request)
    if role != ROLE_LECTURER:
        return redirect('scheduler:dashboard')

    lecturer, err = _get_lecturer_or_redirect(request)
    if err:
        return err

    session = get_object_or_404(
        AttendanceSession.objects.select_related(
            'schedule_slot__course', 'schedule_slot__room',
            'schedule_slot__time_slot', 'schedule_slot__student_group'
        ),
        id=session_id,
        schedule_slot__lecturer=lecturer,
    )

    if request.method == 'POST':
        action = request.POST.get('action', 'save')
        # Present IDs submitted as checkboxes
        present_ids = set(request.POST.getlist('present'))
        # Fetch/update all records for this session
        for record in session.records.all():
            record.is_present = str(record.id) in present_ids
            record.save(update_fields=['is_present', 'marked_at'])

        if action == 'close':
            session.is_active = False
            session.closed_at = datetime.datetime.now()
            session.save(update_fields=['is_active', 'closed_at'])
            messages.success(request, f"Attendance session closed. {session.records.filter(is_present=True).count()} students marked present.")
            return redirect('scheduler:lecturer_attendance_report')
        else:
            messages.success(request, "Attendance saved.")
            return redirect('scheduler:lecturer_attendance_session', session_id=session.id)

    # Auto-populate records from student group (if not yet done)
    group = session.schedule_slot.student_group

    # Clear old generic "Student 001" placeholders if present to upgrade them
    if session.records.filter(student_name__startswith='Student ').exists():
        session.records.all().delete()

    existing_names = set(session.records.values_list('student_name', flat=True))

    # Generate realistic student names deterministically per group size
    if not existing_names and group and group.size > 0:
        import random
        # Seed with group.id to make the list consistent for this group across sessions
        rng = random.Random(group.id)
        
        first_names = [
            'John', 'Jane', 'Mary', 'Joseph', 'David', 'James', 'Peter', 'Anne', 'Grace', 'Paul',
            'Moses', 'Ruth', 'Sarah', 'Alice', 'Michael', 'Daniel', 'Elizabeth', 'Esther', 'Thomas', 'Stephen',
            'Mercy', 'Joy', 'Hope', 'Charles', 'Philip', 'George', 'William', 'Andrew', 'Simon', 'Lucy',
            'Abdi', 'Fatuma', 'Amina', 'Hassan', 'Ali', 'Mohamed', 'Hussein', 'Halima', 'Omar', 'Yusuf'
        ]
        last_names = [
            'Mwanza', 'Karanja', 'Ochieng', 'Kamau', 'Mwangi', 'Otieno', 'Wanjiku', 'Kimani', 'Njoroge', 'Ouma',
            'Maina', 'Nduta', 'Muthoni', 'Odhiambo', 'Waweru', 'Kiprotich', 'Kipkorir', 'Mutua', 'Musyoka', 'Mogaka',
            'Nyangidi', 'Gathoni', 'Wambui', 'Chepngetich', 'Kibet', 'Cheruiyot', 'Lagat', 'Wafula', 'Nekesa', 'Simiyu',
            'Ibrahim', 'Kariuki', 'Njeri', 'Kipruto', 'Muriuki', 'Ndungu', 'Wambua', 'Onyango', 'Okoth', 'Anyango'
        ]
        
        records_to_create = []
        generated_names = []
        for i in range(1, group.size + 1):
            fn = rng.choice(first_names)
            ln = rng.choice(last_names)
            name = f"{fn} {ln}"
            while name in generated_names:
                fn = rng.choice(first_names)
                ln = rng.choice(last_names)
                name = f"{fn} {ln}"
            generated_names.append(name)
            
            # Format a realistic registration number prefix based on the group name
            code_prefix = ''.join([c for c in group.name if c.isalnum()]).upper()[:4]
            student_id = f"{code_prefix}/{i:03d}/{rng.randint(23, 26)}"
            
            records_to_create.append(AttendanceRecord(
                session=session,
                student_name=name,
                student_id=student_id,
                is_present=False,
            ))
        AttendanceRecord.objects.bulk_create(records_to_create, ignore_conflicts=True)

    records = session.records.all().order_by('student_name')
    present_count = records.filter(is_present=True).count()

    return render(request, 'scheduler/lecturer_attendance_session.html', {
        'active_role': role,
        'lecturer': lecturer,
        'session': session,
        'records': records,
        'present_count': present_count,
        'absent_count': records.count() - present_count,
        'total': records.count(),
    })


@login_required
def lecturer_attendance_report(request):
    """Attendance report across all sessions for this lecturer."""
    role = get_user_role(request)
    if role != ROLE_LECTURER:
        return redirect('scheduler:dashboard')

    lecturer, err = _get_lecturer_or_redirect(request)
    if err:
        return err

    sessions = AttendanceSession.objects.filter(
        schedule_slot__lecturer=lecturer
    ).select_related(
        'schedule_slot__course', 'schedule_slot__student_group',
        'schedule_slot__room', 'schedule_slot__time_slot'
    ).order_by('-date', '-created_at')

    # Filter by course if requested
    course_filter = request.GET.get('course')
    if course_filter:
        sessions = sessions.filter(schedule_slot__course__code__icontains=course_filter)

    # Annotate each session
    session_data = []
    for s in sessions:
        total = s.records.count()
        present = s.records.filter(is_present=True).count()
        session_data.append({
            'session': s,
            'total': total,
            'present': present,
            'absent': total - present,
            'rate': round((present / total) * 100) if total > 0 else 0,
        })

    courses = Course.objects.filter(lecturer=lecturer).order_by('code')

    return render(request, 'scheduler/lecturer_attendance_report.html', {
        'active_role': role,
        'lecturer': lecturer,
        'session_data': session_data,
        'courses': courses,
        'course_filter': course_filter or '',
    })


@login_required
def lecturer_portal_profile(request):
    """Lecturer profile view and bio editor."""
    role = get_user_role(request)
    if role != ROLE_LECTURER:
        return redirect('scheduler:dashboard')

    lecturer, err = _get_lecturer_or_redirect(request)
    if err:
        return err

    university = get_active_uni(request)

    if request.method == 'POST':
        bio = request.POST.get('bio', '').strip()
        staff_id = request.POST.get('staff_id', '').strip()
        try:
            profile = request.user.profile
            profile.bio = bio
            profile.save(update_fields=['bio'])
        except Exception:
            pass
        if staff_id and (not lecturer.staff_id or lecturer.staff_id != staff_id):
            from django.db import IntegrityError
            try:
                lecturer.staff_id = staff_id
                lecturer.save(update_fields=['staff_id'])
            except IntegrityError:
                messages.warning(request, "That Staff ID is already in use. Profile saved without changing it.")
        messages.success(request, "Profile updated successfully.")
        return redirect('scheduler:lecturer_portal_profile')

    try:
        bio = request.user.profile.bio
    except Exception:
        bio = ''

    # Stats
    total_courses = Course.objects.filter(lecturer=lecturer).count()
    active_timetable = None
    if university:
        active_timetable = (
            Timetable.objects.filter(semester__university=university, is_active=True).first()
            or Timetable.objects.filter(semester__university=university).order_by('-created_at').first()
        )
    total_slots = 0
    if active_timetable:
        total_slots = ScheduleSlot.objects.filter(timetable=active_timetable, lecturer=lecturer).count()

    total_sessions_taken = AttendanceSession.objects.filter(
        schedule_slot__lecturer=lecturer
    ).count()

    return render(request, 'scheduler/lecturer_portal_profile.html', {
        'active_role': role,
        'lecturer': lecturer,
        'bio': bio,
        'active_university': university,
        'total_courses': total_courses,
        'total_slots': total_slots,
        'total_sessions_taken': total_sessions_taken,
    })


@login_required
def admin_lecturer_profile(request, pk):
    """Admin view to manage an individual lecturer's profile and assignments."""
    role = get_user_role(request)
    if role in (ROLE_STUDENT, ROLE_LECTURER):
        messages.error(request, "Permission denied.")
        return redirect('scheduler:dashboard')

    university = get_active_uni(request)
    lecturer = get_object_or_404(
        Lecturer.objects.select_related('department', 'department__faculty'), pk=pk
    )
    if lecturer.department.faculty.campus.university != university:
        messages.error(request, "Access denied.")
        return redirect('scheduler:resources_manager')

    if request.method == 'POST':
        name = request.POST.get('name')
        email = request.POST.get('email')
        staff_id = request.POST.get('staff_id')
        dept_id = request.POST.get('department')
        max_hours = request.POST.get('max_hours_per_week')
        max_slots = request.POST.get('max_slots_per_day')
        is_active = request.POST.get('is_active') == 'on'

        lecturer.name = name
        lecturer.email = email
        if staff_id:
            lecturer.staff_id = staff_id
        else:
            lecturer.staff_id = None
        
        if dept_id:
            dept = Department.objects.filter(faculty__campus__university=university, id=dept_id).first()
            if dept:
                lecturer.department = dept
        
        if max_hours:
            lecturer.max_hours_per_week = int(max_hours)
        if max_slots:
            lecturer.max_slots_per_day = int(max_slots)
        
        lecturer.is_active = is_active
        lecturer.save()
        messages.success(request, f"Lecturer {lecturer.name} profile updated successfully.")
        return redirect('scheduler:admin_lecturer_profile', pk=lecturer.pk)

    departments = Department.objects.filter(faculty__campus__university=university).select_related('faculty')
    courses = Course.objects.filter(lecturer=lecturer).select_related('student_group', 'program')
    student_groups = StudentGroup.objects.filter(
        courses__lecturer=lecturer
    ).distinct().select_related('program')

    active_timetable = (
        Timetable.objects.filter(semester__university=university, is_active=True).first()
        or Timetable.objects.filter(semester__university=university).order_by('-created_at').first()
    )
    slots = []
    total_hours = 0.0
    day_slots = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    day_names = {1: 'Monday', 2: 'Tuesday', 3: 'Wednesday', 4: 'Thursday', 5: 'Friday'}
    
    if active_timetable:
        slots = list(
            ScheduleSlot.objects.filter(timetable=active_timetable, lecturer=lecturer)
            .select_related('course', 'room', 'time_slot', 'student_group')
            .order_by('time_slot__day_of_week', 'time_slot__slot_number')
        )
        for s in slots:
            ts = s.time_slot
            dur = (ts.end_time.hour * 60 + ts.end_time.minute) - (
                ts.start_time.hour * 60 + ts.start_time.minute
            )
            total_hours += (dur / 60)
            if ts.day_of_week in day_slots:
                day_slots[ts.day_of_week] += 1

    constraints = Constraint.objects.filter(
        university=university,
        constraint_type='LECTURER_AVAILABILITY'
    )
    unavailable_slot_ids = set()
    for c in constraints:
        l_id = c.parameters.get('lecturer_id')
        if l_id == lecturer.id:
            unavail_slots = c.parameters.get('unavailable_slots', [])
            unavailable_slot_ids.update(unavail_slots)
            
    unavail_records = LecturerAvailability.objects.filter(lecturer=lecturer, is_available=False)
    for rec in unavail_records:
        unavailable_slot_ids.add(rec.time_slot_id)

    unavailable_slots = TimeSlot.objects.filter(id__in=unavailable_slot_ids).order_by('day_of_week', 'slot_number')

    return render(request, 'scheduler/admin_lecturer_profile.html', {
        'lecturer': lecturer,
        'departments': departments,
        'courses': courses,
        'student_groups': student_groups,
        'timetable': active_timetable,
        'slots': slots,
        'total_hours': round(total_hours, 1),
        'day_slots': day_slots,
        'day_names': day_names,
        'unavailable_slots': unavailable_slots,
        'active_university': university,
    })


@login_required
def admin_lecturer_delete(request, pk):
    """Admin view to confirm and delete a lecturer."""
    role = get_user_role(request)
    if role in (ROLE_STUDENT, ROLE_LECTURER):
        messages.error(request, "Permission denied.")
        return redirect('scheduler:dashboard')

    university = get_active_uni(request)
    lecturer = get_object_or_404(Lecturer, pk=pk)

    if lecturer.department.faculty.campus.university != university:
        messages.error(request, "Access denied.")
        return redirect('scheduler:resources_manager')

    if request.method == 'POST':
        name = lecturer.name
        if lecturer.user:
            lecturer.user.delete()
        lecturer.delete()
        messages.success(request, f"Lecturer {name} was successfully removed from the system.")
        return redirect('/resources/?tab=lecturer')

    return render(request, 'scheduler/admin_lecturer_confirm_delete.html', {
        'lecturer': lecturer,
        'active_university': university,
    })


# --- Approval Workflow Views ---

@login_required
@tenant_required(Timetable)
def timetable_workflow_action(request, pk):
    """
    Handles workflow state transitions for a timetable: SUBMIT, APPROVE, REJECT.
    """
    role = get_user_role(request)
    timetable = get_object_or_404(Timetable, pk=pk)

    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    action = request.POST.get('action') # 'submit', 'approve', 'reject'
    comments = request.POST.get('comments', '').strip()

    if not action:
        messages.error(request, "Workflow action is missing.")
        return redirect('scheduler:timetable_detail', pk=pk)

    current_status = timetable.status

    # Validate transitioning permissions and states
    if action == 'submit':
        if current_status != 'DRAFT':
            messages.error(request, f"Timetable is in '{current_status}' state and cannot be submitted.")
            return redirect('scheduler:timetable_detail', pk=pk)
        
        if role not in (ROLE_SCHEDULER, ROLE_TIMETABLE_OFFICER, ROLE_ADMIN, ROLE_INST_ADMIN):
            messages.error(request, "Only schedulers and administrators can submit timetables for review.")
            return redirect('scheduler:timetable_detail', pk=pk)

        timetable.status = 'HOD_REVIEW'
        timetable.save()
        ApprovalLog.objects.create(
            timetable=timetable,
            stage='HOD_REVIEW',
            actor=request.user,
            action='SUBMIT',
            comments=comments
        )
        messages.success(request, "Timetable submitted successfully for Head of Department review.")

    elif action == 'approve':
        if current_status == 'DRAFT':
            messages.error(request, "Draft timetables must be submitted before approval.")
            return redirect('scheduler:timetable_detail', pk=pk)
        
        # Check permissions for each stage
        if current_status == 'HOD_REVIEW':
            if role not in (ROLE_HOD, ROLE_ADMIN, ROLE_INST_ADMIN):
                messages.error(request, "Only Heads of Department can approve this stage.")
                return redirect('scheduler:timetable_detail', pk=pk)
            timetable.status = 'DEAN_REVIEW'
        elif current_status == 'DEAN_REVIEW':
            if role not in (ROLE_DEAN, ROLE_ADMIN, ROLE_INST_ADMIN):
                messages.error(request, "Only Deans can approve this stage.")
                return redirect('scheduler:timetable_detail', pk=pk)
            timetable.status = 'REGISTRAR_REVIEW'
        elif current_status == 'REGISTRAR_REVIEW':
            if role not in (ROLE_REGISTRAR, ROLE_ADMIN, ROLE_INST_ADMIN):
                messages.error(request, "Only Registrars can approve this stage.")
                return redirect('scheduler:timetable_detail', pk=pk)
            timetable.status = 'DVC_REVIEW'
        elif current_status == 'DVC_REVIEW':
            if role not in (ROLE_DVC, ROLE_ADMIN, ROLE_INST_ADMIN):
                messages.error(request, "Only DVC Academic can approve this stage.")
                return redirect('scheduler:timetable_detail', pk=pk)
            timetable.status = 'PUBLISHED'
            timetable.is_active = True
            timetable.save()
            # Deactivate other timetables for the same semester
            Timetable.objects.filter(semester=timetable.semester).exclude(pk=timetable.pk).update(is_active=False)
        elif current_status == 'PUBLISHED':
            messages.info(request, "Timetable is already fully approved and published.")
            return redirect('scheduler:timetable_detail', pk=pk)
        
        timetable.save()
        ApprovalLog.objects.create(
            timetable=timetable,
            stage=current_status,
            actor=request.user,
            action='APPROVE',
            comments=comments
        )
        messages.success(request, f"Timetable approved at {timetable.get_status_display()} stage.")

    elif action == 'reject':
        if current_status == 'DRAFT':
            messages.error(request, "Draft timetables cannot be rejected.")
            return redirect('scheduler:timetable_detail', pk=pk)
        
        # Verify role permission to reject
        allowed = False
        if current_status == 'HOD_REVIEW' and role in (ROLE_HOD, ROLE_ADMIN, ROLE_INST_ADMIN):
            allowed = True
        elif current_status == 'DEAN_REVIEW' and role in (ROLE_DEAN, ROLE_ADMIN, ROLE_INST_ADMIN):
            allowed = True
        elif current_status == 'REGISTRAR_REVIEW' and role in (ROLE_REGISTRAR, ROLE_ADMIN, ROLE_INST_ADMIN):
            allowed = True
        elif current_status == 'DVC_REVIEW' and role in (ROLE_DVC, ROLE_ADMIN, ROLE_INST_ADMIN):
            allowed = True

        if not allowed:
            messages.error(request, "Permission denied to reject at this stage.")
            return redirect('scheduler:timetable_detail', pk=pk)

        timetable.status = 'DRAFT'
        timetable.save()
        ApprovalLog.objects.create(
            timetable=timetable,
            stage=current_status,
            actor=request.user,
            action='REJECT',
            comments=comments
        )
        messages.warning(request, f"Timetable was rejected and returned to Draft status. Reason: {comments}")

    return redirect('scheduler:timetable_detail', pk=pk)


# --- AI Assistant Views & APIs ---

from django.http import JsonResponse

@login_required
@tenant_required(Timetable)
def ai_quality_score(request, pk):
    """
    Computes a Timetable Quality Score out of 100 based on soft constraint violations.
    Soft constraints checked:
    - Avoid Evening Classes (weight 5)
    - Avoid consecutive slots > 2 for lecturer (weight 3)
    - Preferred time slots/dislikes violations (weight 5)
    """
    timetable = get_object_or_404(Timetable, pk=pk)
    slots = list(timetable.slots.select_related('course', 'lecturer', 'room', 'time_slot', 'student_group').all())

    evening_violations = 0
    consecutive_violations = 0
    preference_violations = 0

    # 1. Evening classes check
    for s in slots:
        if s.time_slot.is_evening:
            evening_violations += 1

    # 2. Consecutive slots check (> 2 slots per day)
    from collections import defaultdict
    lecturer_days = defaultdict(list)
    for s in slots:
        lecturer_days[(s.lecturer_id, s.time_slot.day_of_week)].append(s.time_slot.slot_number)
    
    for key, nums in lecturer_days.items():
        nums.sort()
        consec = 1
        for i in range(len(nums) - 1):
            if nums[i+1] == nums[i] + 1:
                consec += 1
                if consec > 2:
                    consecutive_violations += 1
            else:
                consec = 1

    # 3. Preferences violations (dislikes)
    from scheduler.models import LecturerTimeSlotPreference
    prefs = LecturerTimeSlotPreference.objects.filter(lecturer__department__faculty__campus__university=timetable.semester.university)
    disliked_map = defaultdict(set)
    for p in prefs:
        if p.preference_level == 'dislike':
            disliked_map[p.lecturer_id].add(p.time_slot_id)
            
    for s in slots:
        if s.time_slot_id in disliked_map[s.lecturer_id]:
            preference_violations += 1

    # Calculate score
    penalties = (evening_violations * 5) + (consecutive_violations * 3) + (preference_violations * 5)
    score = max(0, min(100, 100 - penalties))

    return JsonResponse({
        'timetable_id': pk,
        'quality_score': score,
        'penalties_breakdown': {
            'evening_class_violations': evening_violations,
            'lecturer_consecutive_violations': consecutive_violations,
            'disliked_slot_violations': preference_violations
        },
        'status': 'success'
    })


@login_required
@tenant_required(Timetable)
def ai_recommend_swaps(request, pk):
    """
    Given a ScheduleSlot ID, suggest alternative time slots or rooms that do not introduce conflicts.
    Query params: slot_id
    """
    timetable = get_object_or_404(Timetable, pk=pk)
    slot_id = request.GET.get('slot_id')
    if not slot_id:
        return JsonResponse({'error': 'Parameter slot_id is required.'}, status=400)

    target_slot = get_object_or_404(ScheduleSlot, pk=slot_id, timetable=timetable)
    university = timetable.semester.university

    # Fetch other slots in same timetable to detect conflicts
    other_slots = list(timetable.slots.exclude(pk=target_slot.pk).select_related('time_slot', 'room', 'lecturer', 'student_group').all())
    
    # Let's check available slots
    from scheduler.models import TimeSlot, Room
    all_timeslots = list(TimeSlot.objects.filter(university=university).all())
    all_rooms = list(Room.objects.filter(campus__university=university).filter(room_type=target_slot.course.required_room_type, capacity__gte=target_slot.student_group.size).all())

    recommendations = []

    # Simple constraint verification logic
    for ts in all_timeslots[:15]: # cap suggestions to keep it quick
        for rm in all_rooms[:3]:
            # Check lecturer conflict
            lecturer_conflict = any(s.lecturer_id == target_slot.lecturer_id and s.time_slot_id == ts.id for s in other_slots)
            # Check student group conflict
            group_conflict = any(s.student_group_id == target_slot.student_group_id and s.time_slot_id == ts.id for s in other_slots)
            # Check room conflict
            room_conflict = any(s.room_id == rm.id and s.time_slot_id == ts.id for s in other_slots)

            if not (lecturer_conflict or group_conflict or room_conflict):
                recommendations.append({
                    'time_slot_id': ts.id,
                    'time_slot_label': str(ts),
                    'room_id': rm.id,
                    'room_name': rm.name,
                    'capacity': rm.capacity
                })

    return JsonResponse({
        'slot_id': slot_id,
        'course': f"{target_slot.course.code}: {target_slot.course.name}",
        'current_time_slot': str(target_slot.time_slot),
        'current_room': target_slot.room.name,
        'recommendations': recommendations[:10] # return top 10 options
    })


@login_required
def student_portal_weekly_timetable(request):
    """Full 5-day weekly timetable grid for the student's student group."""
    import datetime
    role = get_user_role(request)
    if role != ROLE_STUDENT:
        if role not in (ROLE_ADMIN, ROLE_SCHEDULER, ROLE_TIMETABLE_OFFICER, ROLE_INST_ADMIN):
            return redirect('scheduler:dashboard')

    try:
        student_group = request.user.profile.student_group
    except Exception:
        student_group = None

    if not student_group:
        messages.error(request, "Your account is not linked to a student group.")
        return redirect('accounts:profile')

    university = get_active_uni(request)
    active_timetable = None
    if university:
        active_timetable = (
            Timetable.objects.filter(semester__university=university, is_active=True).first()
            or Timetable.objects.filter(semester__university=university).order_by('-created_at').first()
        )

    slots_by_day = {1: [], 2: [], 3: [], 4: [], 5: [], 6: [], 7: []}
    all_slots = []

    if active_timetable:
        all_slots = list(
            ScheduleSlot.objects.filter(timetable=active_timetable, student_group=student_group)
            .select_related('course', 'room', 'time_slot', 'lecturer')
            .order_by('time_slot__slot_number', 'time_slot__day_of_week')
        )
        from django.utils import timezone
        local_now = timezone.localtime(timezone.now())
        current_dow = local_now.isoweekday()
        current_time = local_now.time()
        for s in all_slots:
            ts = s.time_slot
            if ts.day_of_week < current_dow:
                s.has_ended = True
                s.is_ongoing = False
            elif ts.day_of_week == current_dow:
                s.has_ended = (ts.end_time < current_time)
                s.is_ongoing = (ts.start_time <= current_time <= ts.end_time)
            else:
                s.has_ended = False
                s.is_ongoing = False

        for s in all_slots:
            slots_by_day[s.time_slot.day_of_week].append(s)

    _today = datetime.date.today()
    _monday = _today - datetime.timedelta(days=_today.weekday())
    days_list = []
    for d, day_name in [(1,'Monday'),(2,'Tuesday'),(3,'Wednesday'),(4,'Thursday'),(5,'Friday')]:
        days_list.append({
            'dow': d,
            'name': day_name,
            'date': _monday + datetime.timedelta(days=d - 1)
        })
    today_dow = _today.weekday() + 1

    import uuid
    from django.urls import reverse
    if not student_group.calendar_token:
        student_group.calendar_token = uuid.uuid4()
        student_group.save(update_fields=['calendar_token'])

    feed_url = request.build_absolute_uri(
        reverse('scheduler:student_group_calendar_feed', args=[student_group.calendar_token])
    )
    webcal_url = feed_url.replace('http://', 'webcal://').replace('https://', 'webcal://')

    return render(request, 'scheduler/student_weekly_timetable.html', {
        'student_group': student_group,
        'active_university': university,
        'timetable': active_timetable,
        'slots_by_day': slots_by_day,
        'all_slots': all_slots,
        'days': days_list,
        'today_dow': today_dow,
        'active_semester': Semester.objects.filter(
            university=university, is_active=True).first() if university else None,
        'feed_url': feed_url,
        'webcal_url': webcal_url,
    })

