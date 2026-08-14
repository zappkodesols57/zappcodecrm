import threading

_thread_locals = threading.local()


def get_current_user():
    return getattr(_thread_locals, "user", None)


def get_current_ip():
    return getattr(_thread_locals, "ip", None)


class CurrentUserMiddleware:
    """Stashes the requesting user/IP in a thread-local so model signals
    (which have no access to `request`) can attribute AuditLog entries."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _thread_locals.user = getattr(request, "user", None)
        _thread_locals.ip = request.META.get("REMOTE_ADDR")
        response = self.get_response(request)
        return response
