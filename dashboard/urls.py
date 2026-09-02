from django.urls import path
from . import views
from . import zappcode_views

app_name = "dashboard"

urlpatterns = [
    path("reports/admin/", views.admin_reports_view, name="admin_reports"),

    path("call-history/", views.call_history_view, name="call_history"),

    path("tasks/", views.task_list_view, name="tasks"),
    path("tasks/create/", views.task_create_view, name="task_create"),
    path("tasks/<int:pk>/status/", views.task_update_status, name="task_update_status"),
    path("tasks/send-report/", views.task_send_report_to_admin, name="task_send_report"),

    path("", views.home, name="home"),
    path("superadmin/", views.superadmin_home, name="superadmin_home"),
    path("superadmin/card-drilldown-api/", views.nel_card_drilldown_api, name="nel_card_drilldown_api"),
    path("management/", zappcode_views.management_home, name="management_home"),
    path("nelson/roles-permissions/", views.roles_permissions_view, name="roles_permissions"),
    path("nelson/module/<str:module_name>/", views.nelson_module_view, name="nelson_module"),
    path("reports/source/", views.source_report, name="source_report"),
    path("reports/campaign/", views.campaign_report, name="campaign_report"),
    path("reports/employee/", views.employee_report, name="employee_report"),
    path("employee/<int:emp_id>/", views.employee_detail_activity, name="employee_detail_activity"),
    path("daily-report/", views.submit_daily_report, name="submit_daily_report"),
    path("reports/daily/", views.management_daily_reports, name="management_daily_reports"),

    path("doctor/", views.doctor_home, name="doctor_home"),
    path("doctor/appointments/", views.doctor_appointments, name="doctor_appointments"),
    path("telecaller/", views.telecaller_home, name="telecaller_home"),
    path("telecaller/my-leads/", views.telecaller_my_leads, name="telecaller_my_leads"),
    path("telecaller/new-enquiries/", views.telecaller_new_enquiries, name="telecaller_new_enquiries"),
    path("telecaller/today-team-activity/", views.telecaller_today_team_activity, name="telecaller_today_team_activity"),
    path("telecaller/search/", views.telecaller_search, name="telecaller_search"),
    path("telecaller/appointments/", views.telecaller_appointments, name="telecaller_appointments"),
    path("placeholder/<str:module_name>/", views.placeholder_view, name="placeholder"),
]
