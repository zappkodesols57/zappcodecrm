from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

app_name = "accounts"

urlpatterns = [
    # Portal selection landing
    path("portal/", views.portal_select, name="portal_select"),
    # Dual login portals
    path("login/", views.employee_login, name="login"),          # default / fallback
    path("login/employee/", views.employee_login, name="employee_login"),
    path("login/management/", views.management_login, name="management_login"),
    path("logout/", views.custom_logout, name="logout"),
    path("register/", views.register, name="register"),
    path("users/", views.user_list, name="user_list"),
    path("users/add/", views.user_add, name="user_add"),
    path("users/<int:pk>/edit/", views.user_edit, name="user_edit"),
    path("users/<int:pk>/reset-password/", views.user_reset_password, name="user_reset_password"),
    path("users/<int:pk>/delete/", views.user_delete, name="user_delete"),
    path("users/<int:pk>/approve/", views.approve_user, name="approve_user"),
    path("users/<int:pk>/reject/", views.reject_user, name="reject_user"),
    path("audit-log/", views.audit_log, name="audit_log"),
]
