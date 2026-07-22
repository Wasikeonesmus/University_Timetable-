from django.urls import path
from . import views

app_name = 'scheduler'

urlpatterns = [
    # Dashboard & core
    path('', views.dashboard, name='dashboard'),
    path('search/', views.search_view, name='search'),

    # Timetables
    path('timetables/', views.timetable_list, name='timetable_list'),
    path('timetables/create/', views.timetable_create, name='timetable_create'),
    path('timetables/<int:pk>/', views.timetable_detail, name='timetable_detail'),
    path('timetables/<int:pk>/generate/', views.timetable_generate, name='timetable_generate'),
    path('timetables/<int:pk>/status/', views.generation_status, name='generation_status'),
    path('timetables/<int:pk>/weekly/', views.timetable_weekly, name='timetable_weekly'),
    path('timetables/<int:pk>/conflicts/', views.timetable_conflicts, name='timetable_conflicts'),
    path('timetables/<int:pk>/conflicts/json/', views.conflicts_json, name='conflicts_json'),
    path('timetables/<int:pk>/conflicts/autofix/', views.conflicts_autofix, name='conflicts_autofix'),
    path('timetables/<int:pk>/logs/', views.generation_log_list, name='generation_log_list'),

    # Export
    path('timetables/<int:pk>/export/', views.export_timetable_ics, name='export_timetable_ics'),
    path('timetables/<int:pk>/export-csv/', views.export_timetable_csv, name='export_timetable_csv'),
    path('timetables/<int:pk>/export-pdf/', views.export_timetable_pdf, name='export_timetable_pdf'),
    path('timetables/<int:pk>/export-word/', views.export_timetable_word, name='export_timetable_word'),
    path('timetables/<int:pk>/export-workload-word/', views.export_workload_word, name='export_workload_word'),
    path('timetables/<int:pk>/export-excel/', views.export_timetable_excel, name='export_timetable_excel'),

    # Schedule slot editing
    path('slots/<int:pk>/update/', views.slot_update, name='slot_update'),

    # Constraints
    path('constraints/', views.constraint_list, name='constraint_list'),
    path('constraints/create/', views.constraint_create, name='constraint_create'),
    path('constraints/<int:pk>/delete/', views.constraint_delete, name='constraint_delete'),
    path('constraints/<int:pk>/edit/', views.constraint_edit, name='constraint_edit'),

    # University & role switching
    path('switch-university/', views.switch_university, name='switch_university'),
    path('switch-role/', views.switch_role, name='switch_role'),

    # Reports & resources
    path('reports/', views.reports, name='reports'),
    path('reports/workloads/', views.reports_workloads, name='reports_workloads'),
    path('reports/rooms/', views.reports_rooms, name='reports_rooms'),
    path('resources/', views.resources_manager, name='resources_manager'),
    path('resources/import/', views.import_resources, name='import_resources'),
    path('resources/auto-heal/', views.manual_auto_heal, name='manual_auto_heal'),
    path('resources/delete/', views.bulk_delete_resources, name='bulk_delete_resources'),
    path('resources/delete/<str:model_type>/<int:pk>/', views.delete_resource, name='delete_resource'),
    path('resources/apply-3hr-timeslots/', views.apply_default_3hr_timeslots, name='apply_default_3hr_timeslots'),

    # Lecturer self-service (Phase 2)
    path('availability/', views.lecturer_availability, name='lecturer_availability'),
    path('my-schedule/', views.lecturer_my_schedule, name='lecturer_my_schedule'),
    path('my-teaching-history/', views.lecturer_teaching_history, name='lecturer_teaching_history'),
    path('student/my-schedule/', views.student_my_schedule, name='student_my_schedule'),
    path('setup/', views.setup_wizard, name='setup_wizard'),
    path('sync-google-calendar/', views.sync_to_google_calendar, name='sync_to_google_calendar'),

    # Calendar integration feeds (Public feeds for sync)
    path('feed/lecturer/<uuid:token>/', views.lecturer_calendar_feed, name='lecturer_calendar_feed'),
    path('feed/student-group/<uuid:token>/', views.student_group_calendar_feed, name='student_group_calendar_feed'),

    # Notifications & Subscriptions
    path('notifications/', views.notifications_list, name='notifications_list'),
    path('notifications/<int:pk>/read/', views.notification_read, name='notification_read'),
    path('notifications/read-all/', views.notifications_mark_all_read, name='notifications_mark_all_read'),
    path('subscription/', views.subscription_billing, name='subscription_billing'),

    # Admin Lecturer Management (Phase 3 addition)
    path('portal/admin/lecturers/<int:pk>/', views.admin_lecturer_profile, name='admin_lecturer_profile'),
    path('portal/admin/lecturers/<int:pk>/delete/', views.admin_lecturer_delete, name='admin_lecturer_delete'),

    # Admin Resource Profiles (Room, Student Group, Course, University, Department, TimeSlot)
    path('portal/admin/rooms/<int:pk>/', views.admin_room_profile, name='admin_room_profile'),
    path('portal/admin/studentgroups/<int:pk>/', views.admin_studentgroup_profile, name='admin_studentgroup_profile'),
    path('portal/admin/courses/<int:pk>/', views.admin_course_profile, name='admin_course_profile'),
    path('portal/admin/universities/<int:pk>/', views.admin_university_profile, name='admin_university_profile'),
    path('portal/admin/departments/<int:pk>/', views.admin_department_profile, name='admin_department_profile'),
    path('portal/admin/timeslots/<int:pk>/', views.admin_timeslot_profile, name='admin_timeslot_profile'),

    # ── Lecturer Portal (Phase 3) ────────────────────────────────────────────
    path('portal/', views.lecturer_portal_dashboard, name='lecturer_portal_dashboard'),
    path('portal/timetable/', views.lecturer_portal_weekly_timetable, name='lecturer_portal_weekly_timetable'),
    path('portal/courses/', views.lecturer_portal_courses, name='lecturer_portal_courses'),
    path('portal/student-groups/', views.lecturer_portal_student_groups, name='lecturer_portal_student_groups'),
    path('portal/workload/', views.lecturer_portal_workload, name='lecturer_portal_workload'),
    path('portal/profile/', views.lecturer_portal_profile, name='lecturer_portal_profile'),
    path('portal/attendance/start/<int:slot_id>/', views.lecturer_attendance_start, name='lecturer_attendance_start'),
    path('portal/attendance/session/<int:session_id>/', views.lecturer_attendance_session, name='lecturer_attendance_session'),
    path('portal/attendance/report/', views.lecturer_attendance_report, name='lecturer_attendance_report'),

    # Enterprise workflow & AI
    path('timetables/<int:pk>/workflow/', views.timetable_workflow_action, name='timetable_workflow_action'),
    path('timetables/<int:pk>/ai/quality-score/', views.ai_quality_score, name='ai_quality_score'),
    path('timetables/<int:pk>/ai/recommend-swaps/', views.ai_recommend_swaps, name='ai_recommend_swaps'),
    path('student/timetable/', views.student_portal_weekly_timetable, name='student_portal_weekly_timetable'),
    path('lecturers/update-hours/', views.update_lecturer_hours, name='update_lecturer_hours'),
    path('courses/reassign/', views.reassign_course, name='reassign_course'),
    path('courses/auto-balance/', views.auto_balance_workloads_view, name='auto_balance_workloads'),
    path('portal/onboarding/', views.public_lecturer_onboarding, name='public_lecturer_onboarding_direct'),
    path('portal/onboarding/<uuid:token>/', views.public_lecturer_onboarding, name='public_lecturer_onboarding'),

    # Import audit
    path('import-audit/<int:pk>/', views.import_audit_report, name='import_audit_report'),
    path('import-audit/', views.import_audit_log_list, name='import_audit_log_list'),
]

