import logging
import traceback
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.contrib import messages
from django.urls import reverse

logger = logging.getLogger('django.request')


class CRMGlobalExceptionMiddleware:
    """
    Global Fault-Tolerant Middleware for ZappCode CRM.
    Catches any unexpected 500 error / unhandled exception in views or background tasks,
    logs the complete traceback to django.log, and ensures the user always gets a clean,
    user-friendly experience instead of a blank screen or technical Django traceback.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        # 1. Log full exception with path, method, and authenticated user
        user_info = getattr(request, 'user', None)
        username = getattr(user_info, 'username', 'Anonymous')
        path = getattr(request, 'path', 'Unknown')
        method = getattr(request, 'method', 'GET')
        
        logger.error(
            "CRM Unhandled Exception caught by Middleware on [%s] %s by User '%s': %s\nTraceback:\n%s",
            method,
            path,
            username,
            str(exception),
            traceback.format_exc()
        )

        # 2. Check if the request is an AJAX or API call
        is_ajax = (
            request.headers.get('x-requested-with') == 'XMLHttpRequest' or
            request.path.startswith('/api/') or
            'application/json' in request.headers.get('Accept', '') or
            request.content_type == 'application/json'
        )

        if is_ajax:
            return JsonResponse({
                "status": "error",
                "code": 500,
                "message": "An unexpected error occurred while processing your request. Please try again or refresh the page.",
                "error_detail": str(exception) if getattr(request.user, 'is_superuser', False) else None
            }, status=500)

        # 3. For safe GET requests on dashboards or leads, attempt to redirect gracefully with an alert message
        if method == 'GET' and ('/dashboard/' in path or '/leads/' in path) and request.user.is_authenticated:
            # If query params caused the failure (e.g., malformed filter or invalid date), redirecting to clean view with message
            if request.GET:
                try:
                    messages.error(request, "Some applied filters or parameters were invalid and have been reset.")
                    clean_path = path
                    return redirect(clean_path)
                except Exception:
                    pass

        # 4. Render clean 500.html template with 500 status code
        context = {
            "error_message": "An unexpected error occurred while processing your request.",
            "path": path,
        }
        try:
            return render(request, "500.html", context, status=500)
        except Exception:
            # Fallback if template engine itself fails
            return JsonResponse({
                "status": "error",
                "message": "A system error occurred. Please return to the dashboard."
            }, status=500)
