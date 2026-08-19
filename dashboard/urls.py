from django.urls import path
from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.home, name="home"),
    path("superadmin/", views.superadmin_home, name="superadmin_home"),
    path("management/", views.management_home, name="management_home"),
    path("nelson/roles-permissions/", views.roles_permissions_view, name="roles_permissions"),
    path("nelson/module/<str:module_name>/", views.nelson_module_view, name="nelson_module"),
    path("reports/source/", views.source_report, name="source_report"),
    path("reports/campaign/", views.campaign_report, name="campaign_report"),
    path("reports/employee/", views.employee_report, name="employee_report"),
    path("employee/<int:emp_id>/", views.employee_detail_activity, name="employee_detail_activity"),
    path("daily-report/", views.submit_daily_report, name="submit_daily_report"),
    path("reports/daily/", views.management_daily_reports, name="management_daily_reports"),

    path("telecaller/", views.telecaller_home, name="telecaller_home"),
    path("telecaller/my-leads/", views.telecaller_my_leads, name="telecaller_my_leads"),
    path("telecaller/search/", views.telecaller_search, name="telecaller_search"),
    path("telecaller/appointments/", views.telecaller_appointments, name="telecaller_appointments"),
    path("placeholder/<str:module_name>/", views.placeholder_view, name="placeholder"),

]
