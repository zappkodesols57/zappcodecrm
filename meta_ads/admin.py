from django.contrib import admin
from .models import MetaAdsConnection, MetaLeadForm, MetaCampaignStat


@admin.register(MetaAdsConnection)
class MetaAdsConnectionAdmin(admin.ModelAdmin):
    list_display = ["name", "ad_account_id", "page_id", "is_active", "connected_at", "last_synced_at"]
    readonly_fields = ["connected_at", "last_synced_at"]


@admin.register(MetaLeadForm)
class MetaLeadFormAdmin(admin.ModelAdmin):
    list_display = ["form_name", "form_id", "connection", "is_syncing", "created_at"]
    list_filter = ["is_syncing", "connection"]


@admin.register(MetaCampaignStat)
class MetaCampaignStatAdmin(admin.ModelAdmin):
    list_display = ["campaign_name", "date_preset", "spend", "leads_count", "clicks", "impressions", "synced_at"]
    list_filter = ["date_preset"]
    readonly_fields = ["synced_at"]
