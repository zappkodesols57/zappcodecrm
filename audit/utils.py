from .middleware import get_current_user, get_current_ip
from .models import AuditLog


def log_action(action, obj=None, old_value="", new_value="", user=None):
    user = user or get_current_user()
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None
    AuditLog.objects.create(
        user=user,
        action=action,
        model_name=obj.__class__.__name__ if obj is not None else "",
        object_id=str(getattr(obj, "pk", "")) if obj is not None else "",
        object_repr=str(obj)[:255] if obj is not None else "",
        old_value=str(old_value)[:1000],
        new_value=str(new_value)[:1000],
        ip_address=get_current_ip(),
    )
