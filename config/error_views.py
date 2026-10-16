import logging
from django.shortcuts import render
from django.http import JsonResponse

logger = logging.getLogger('django.request')

def custom_page_not_found_view(request, exception=None):
    """
    User-friendly 404 handler for HTML and JSON requests.
    Prevents ugly default error screens.
    """
    if request.path.startswith('/api/') or request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', ''):
        return JsonResponse({
            'status': 'error',
            'code': 404,
            'message': 'The requested endpoint or resource was not found.'
        }, status=404)
    return render(request, '404.html', status=404)


def custom_server_error_view(request):
    """
    User-friendly 500 handler for HTML and JSON requests.
    Logs full exception trace internally and gives a clean, actionable UI to users.
    """
    logger.exception("An unhandled 500 server error occurred on path: %s", getattr(request, 'path', 'unknown'))
    if request.path.startswith('/api/') or request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', ''):
        return JsonResponse({
            'status': 'error',
            'code': 500,
            'message': 'An internal server error occurred. Our team has been notified.'
        }, status=500)
    return render(request, '500.html', status=500)


def custom_permission_denied_view(request, exception=None):
    """
    User-friendly 403 handler for HTML and JSON requests.
    """
    if request.path.startswith('/api/') or request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', ''):
        return JsonResponse({
            'status': 'error',
            'code': 403,
            'message': 'You do not have permission to access this resource.'
        }, status=403)
    return render(request, '403.html', status=403)


def custom_bad_request_view(request, exception=None):
    """
    User-friendly 400 handler for HTML and JSON requests.
    """
    if request.path.startswith('/api/') or request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', ''):
        return JsonResponse({
            'status': 'error',
            'code': 400,
            'message': 'Bad request parameters or invalid data submitted.'
        }, status=400)
    return render(request, '400.html', status=400)
