from django.urls import path
from . import views

app_name = "notifications"

urlpatterns = [
    path('api/unread/', views.get_unread_notifications, name='api_unread'),
    path('api/mark-read/<int:pk>/', views.mark_notification_read, name='api_mark_read'),
    path('api/mark-all-read/', views.mark_all_read, name='api_mark_all_read'),
    path('', views.notification_list, name='list'),
]
