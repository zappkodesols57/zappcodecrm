from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User


@admin.register(User)
class CRMUserAdmin(UserAdmin):
    list_display = ("username", "get_full_name", "email", "role", "is_active_employee", "reports_to")
    list_filter = ("role", "is_active_employee")
    fieldsets = UserAdmin.fieldsets + (
        ("CRM", {"fields": ("role", "phone", "is_active_employee", "reports_to")}),
    )
