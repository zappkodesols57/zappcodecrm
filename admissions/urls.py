from django.urls import path
from . import views
app_name = "admissions"
urlpatterns = [
    path("", views.admission_list, name="list"),
    path("add/", views.add_admission, name="add"),
    path("bulk-action/", views.admissions_bulk_action, name="bulk_action"),
    path("<int:pk>/update-status/", views.admission_update_status, name="update_status"),
    path("<int:pk>/add-installment/", views.add_installment, name="add_installment"),
    path("installment/<int:pk>/toggle-paid/", views.installment_toggle_paid, name="installment_toggle_paid"),
]
