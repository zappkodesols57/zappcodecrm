from django.contrib import admin
from .models import (
    SourceCategory, LeadSource, Campaign, Course, LeadStage, Tag, Lead,
    MasterGroup, MasterItem,
)


class MasterItemInline(admin.TabularInline):
    model = MasterItem
    extra = 1
    fields = ("name", "code", "order", "is_active")


@admin.register(MasterGroup)
class MasterGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "created_at")
    search_fields = ("name", "description")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [MasterItemInline]


@admin.register(MasterItem)
class MasterItemAdmin(admin.ModelAdmin):
    list_display = ("name", "group", "code", "order", "is_active")
    list_filter = ("group", "is_active")
    search_fields = ("name", "code", "group__name")
    list_editable = ("order", "is_active")


@admin.register(SourceCategory)
class SourceCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "order")
    list_editable = ("is_active", "order")


@admin.register(LeadSource)
class LeadSourceAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "is_active", "order")
    list_filter = ("category", "is_active")
    search_fields = ("name",)


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "platform", "campaign_id", "cost", "is_active")
    search_fields = ("name", "campaign_id")


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active")
    search_fields = ("name",)


@admin.register(LeadStage)
class LeadStageAdmin(admin.ModelAdmin):
    list_display = ("name", "order", "is_active")
    list_editable = ("order", "is_active")


admin.site.register(Tag)


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("lead_code", "name", "mobile", "course", "stage", "temperature", "deal_status", "assigned_to", "created_at")
    list_filter = ("stage", "temperature", "deal_status", "source_category", "lead_source")
    search_fields = ("lead_code", "name", "mobile", "email")
    autocomplete_fields = ("course", "lead_source", "campaign", "assigned_to")
    readonly_fields = ("lead_code", "original_source_category", "original_lead_source", "original_campaign")
