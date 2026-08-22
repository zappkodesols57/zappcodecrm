from .middleware import get_current_user, get_current_ip
from .models import AuditLog
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver


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


@receiver(user_logged_in)
def auto_log_login(sender, request, user, **kwargs):
    if user and getattr(user, 'is_authenticated', False):
        ip = request.META.get("REMOTE_ADDR") if request else None
        AuditLog.objects.create(
            user=user,
            action="USER_LOGIN",
            model_name="User",
            object_id=str(user.pk),
            object_repr=str(user),
            new_value=f"User {user.username} logged in",
            ip_address=ip,
        )


@receiver(user_logged_out)
def auto_log_logout(sender, request, user, **kwargs):
    if user:
        ip = request.META.get("REMOTE_ADDR") if request else None
        AuditLog.objects.create(
            user=user,
            action="USER_LOGOUT",
            model_name="User",
            object_id=str(user.pk),
            object_repr=str(user),
            new_value=f"User {user.username} logged out",
            ip_address=ip,
        )
