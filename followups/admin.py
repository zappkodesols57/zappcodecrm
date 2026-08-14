from django.contrib import admin
from .models import FollowUp, Note, Activity


@admin.register(FollowUp)
class FollowUpAdmin(admin.ModelAdmin):
    list_display = ("lead", "followup_date", "followup_mode", "followup_status", "next_followup_date")
    list_filter = ("followup_mode", "followup_status")
    date_hierarchy = "followup_date"


admin.site.register(Note)


@admin.register(Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = ("lead", "activity_type", "created_at")
    list_filter = ("activity_type",)
