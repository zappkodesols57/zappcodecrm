from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from .models import SystemDefaultProfile

@login_required
def default_system_profile(request):
    """
    SuperAdmin view to manage the Global Default CRM Branding & Logo.
    This default logo is rendered everywhere when a business/tenant does not have its own custom logo.
    """
    if not (request.user.is_superuser or request.user.role == 'SUPER_ADMIN'):
        raise PermissionDenied("Only SuperAdmin can manage default system profile.")

    system_profile = SystemDefaultProfile.get_instance()

    if request.method == "POST":
        system_name = request.POST.get("system_name", "").strip()
        if system_name:
            system_profile.system_name = system_name

        if request.POST.get("clear_default_logo") == "true":
            if system_profile.default_logo:
                system_profile.default_logo.delete(save=False)
            system_profile.default_logo = None
            messages.success(request, "Default system logo cleared. Reverted to core base logo.")
        elif "default_logo" in request.FILES:
            system_profile.default_logo = request.FILES["default_logo"]
            messages.success(request, "Default system logo updated successfully!")

        system_profile.updated_by = request.user
        system_profile.save()
        return redirect("accounts:default_system_profile")

    return render(request, "accounts/default_system_profile.html", {
        "title": "Default Profile & Logo",
        "active": "default_profile",
        "system_profile": system_profile,
    })

