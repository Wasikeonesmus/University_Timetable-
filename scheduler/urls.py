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
    path('timetables/<int:pk>/export-excel/', views.export_timetable_excel, name='export_timetable_excel'),

    # Schedule slot editing
    path('slots/<int:pk>/update/', views.slot_update, name='slot_update'),

    # Constraints
    path('constraints/', views.constraint_list, name='constraint_list'),
    path('constraints/create/', views.constraint_create, name='constraint_create'),
    path('constraints/<int:pk>/delete/', views.constraint_delete, name='constraint_delete'),

    # University & role switching
    path('switch-university/', views.switch_university, name='switch_university'),
    path('switch-role/', views.switch_role, name='switch_role'),

    # Reports & resources
    path('reports/', views.reports, name='reports'),
    path('resources/', views.resources_manager, name='resources_manager'),
    path('resources/import/', views.import_resources, name='import_resources'),
    path('resources/delete/', views.bulk_delete_resources, name='bulk_delete_resources'),
    path('resources/delete/<str:model_type>/<int:pk>/', views.delete_resource, name='delete_resource'),

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
]

