from django.contrib import admin
from django.conf import settings
from django.conf.urls.static import static
from django.urls import path, include
from django.views.generic import RedirectView

urlpatterns = [
    path('django-admin/', admin.site.urls),
    path('', RedirectView.as_view(pattern_name='accounts:portal_select', permanent=False)),
    path('accounts/', include('accounts.urls')),
    path('leads/', include('leads.urls')),
    path('followups/', include('followups.urls')),
    path('imports/', include('imports.urls')),
    path('dashboard/', include('dashboard.urls')),
    path('admissions/', include('admissions.urls')),
    path('payments/', include('payments.urls')),
    path('meta-ads/', include('meta_ads.urls')),
    path('api/', include('api.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
