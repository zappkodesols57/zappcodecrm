from django.urls import path
from . import views

app_name = "meta_ads"

urlpatterns = [
    path("", views.campaign_dashboard, name="index"),
    path("dashboard/", views.campaign_dashboard, name="dashboard"),
    path("sync/", views.sync_campaigns, name="sync"),
    path("sync-leads/", views.sync_leads_now, name="sync_leads_now"),
    path("update-token/", views.update_meta_token, name="update_token"),
    path("webhook/", views.meta_webhook, name="webhook"),
    path("webhook/justdial/", views.justdial_webhook, name="justdial_webhook"),
    path("webhook/practo/", views.practo_webhook, name="practo_webhook"),
    path("api/recent-leads/", views.recent_leads_json, name="recent_leads_json"),
]
