from django.contrib import admin
from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "ip_address", "action", "user", "new_value")
    list_filter = ("action", "model_name")
    search_fields = ("ip_address", "user__username", "new_value", "action")
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False
