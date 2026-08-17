from django.urls import path
from . import views
app_name = "payments"
urlpatterns = [
    path("", views.payment_list, name="list"),
    path("add/<int:admission_id>/", views.payment_add, name="add"),
]
