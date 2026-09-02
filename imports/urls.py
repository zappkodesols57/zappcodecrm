from django.urls import path
from . import views

app_name = "imports"

urlpatterns = [
    path("upload/", views.upload, name="upload"),
    path("campaign-process/", views.campaign_import_process, name="campaign_import_process"),
    path("campaign-execute/", views.campaign_import_execute, name="campaign_import_execute"),
    path("ajax/campaign/create/", views.ajax_create_campaign, name="ajax_create_campaign"),
    path("download-template/", views.download_template, name="download_template"),
    path("quick-import/", views.quick_import, name="quick_import"),
    path("job/<int:pk>/pick-sheet/", views.pick_sheet, name="pick_sheet"),
    path("job/<int:pk>/preview/", views.preview, name="preview"),
    path("job/<int:pk>/run/", views.run_import, name="run_import"),
    path("history/", views.history, name="history"),
    path("job/<int:pk>/", views.job_detail, name="job_detail"),
    path("job/<int:pk>/delete/", views.delete_import, name="delete_job"),
    path("export/", views.export_leads, name="export"),
]
