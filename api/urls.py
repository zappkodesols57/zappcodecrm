from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

app_name = "api"

router = DefaultRouter()
router.register(r'leads', views.LeadViewSet, basename='lead')
router.register(r'follow-ups', views.FollowUpViewSet, basename='followup')

urlpatterns = [
    # Auth endpoints
    path('auth/login/', views.LoginAPIView.as_view(), name='api-login'),
    path('auth/verify-otp/', views.VerifyOTPAPIView.as_view(), name='api-verify-otp'),
    path('auth/forgot-password/', views.ForgotPasswordAPIView.as_view(), name='api-forgot-password'),
    path('auth/logout/', views.LogoutAPIView.as_view(), name='api-logout'),
    path('auth/profile/', views.UserProfileAPIView.as_view(), name='api-profile'),
    
    # Dashboard & Doctors & Appointments
    path('dashboard/stats/', views.DashboardAnalyticsAPIView.as_view(), name='api-dashboard-stats'),
    path('doctors/', views.DoctorListAPIView.as_view(), name='api-doctors'),
    path('appointments/', views.AppointmentListAPIView.as_view(), name='api-appointments'),
    
    # ViewSets (Leads, Follow-ups)
    path('', include(router.urls)),
]
