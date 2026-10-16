from django.contrib import admin
from django.conf import settings
from django.conf.urls.static import static
from django.urls import path, include
from django.views.generic import RedirectView

urlpatterns = [
    path('django-admin/', admin.site.urls),
    path('', RedirectView.as_view(pattern_name='accounts:login', permanent=False)),
    path('accounts/', include('accounts.urls')),
    path('leads/', include('leads.urls')),
    path('followups/', include('followups.urls')),
    path('imports/', include('imports.urls')),
    path('dashboard/', include('dashboard.urls')),
    path('admissions/', include('admissions.urls')),
    path('payments/', include('payments.urls')),
    path('meta-ads/', include('meta_ads.urls')),
    path('meta-ads', RedirectView.as_view(pattern_name='meta_ads:dashboard', permanent=False)),
    path('notifications/', include('notifications.urls')),
    path('api/', include('api.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# Central User-Friendly Error Handlers (Prevents ugly crash screens across the entire project)
handler400 = 'config.error_views.custom_bad_request_view'
handler403 = 'config.error_views.custom_permission_denied_view'
handler404 = 'config.error_views.custom_page_not_found_view'
handler500 = 'config.error_views.custom_server_error_view'

