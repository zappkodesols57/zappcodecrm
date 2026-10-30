class CRMReadOnlyModeMiddleware:
    """
    Middleware that enforces read-only access for users with is_read_only=True.
    Blocks any state-mutating requests (POST, PUT, PATCH, DELETE) except authentication
    and password-changing endpoints.
    """
    EXEMPT_PATHS = [
        '/accounts/login/',
        '/accounts/logout/',
        '/accounts/users/',  # password reset by user if needed
    ]

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and getattr(request.user, 'is_read_only', False):
            # Block unsafe methods
            if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                # Check exemptions (e.g. logging out)
                path = request.path
                is_exempt = any(path.startswith(p) for p in self.EXEMPT_PATHS)
                # Allow logout
                if not is_exempt or ('logout' not in path and 'login' not in path):
                    is_ajax = (
                        request.headers.get('x-requested-with') == 'XMLHttpRequest' or
                        path.startswith('/api/') or
                        'application/json' in request.headers.get('Accept', '')
                    )
                    error_msg = "Read-Only Account: Your account is restricted to view-only access. Creating, updating, or deleting records is not permitted."
                    if is_ajax:
                        return JsonResponse({"status": "error", "message": error_msg}, status=403)
                    
                    messages.error(request, error_msg)
                    referer = request.META.get('HTTP_REFERER')
                    if referer:
                        return redirect(referer)
                    return redirect('/')

        return self.get_response(request)


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
        from django.core.exceptions import PermissionDenied
        from django.http import Http404
        if isinstance(exception, (PermissionDenied, Http404)):
            return None  # Let Django handle 403 / 404 standard handlers
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
