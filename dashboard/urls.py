from django.urls import path
from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.home, name="home"),
    path("management/", views.management_home, name="management_home"),
    path("reports/source/", views.source_report, name="source_report"),
    path("reports/campaign/", views.campaign_report, name="campaign_report"),
    path("reports/employee/", views.employee_report, name="employee_report"),
    path("employee/<int:emp_id>/", views.employee_detail_activity, name="employee_detail_activity"),
    path("daily-report/", views.submit_daily_report, name="submit_daily_report"),
    path("reports/daily/", views.management_daily_reports, name="management_daily_reports"),
]
