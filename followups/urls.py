from django.urls import path
from . import views

app_name = "followups"

urlpatterns = [
    path("today/", views.today, name="today"),
    path("upcoming/", views.upcoming, name="upcoming"),
    path("overdue/", views.overdue, name="overdue"),
    path("<int:pk>/complete/", views.complete, name="complete"),
]
