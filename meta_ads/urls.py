from django.urls import path
from . import views

app_name = "meta_ads"

urlpatterns = [
    path("dashboard/", views.campaign_dashboard, name="dashboard"),
    path("sync/", views.sync_campaigns, name="sync"),
    path("webhook/", views.meta_webhook, name="webhook"),
    path("api/recent-leads/", views.recent_leads_json, name="recent_leads_json"),
]
