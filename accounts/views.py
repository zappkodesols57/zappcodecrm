from django.contrib.auth import authenticate, login
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.shortcuts import render, redirect, get_object_or_404
from django.core.paginator import Paginator

from audit.models import AuditLog
from audit.utils import log_action
from .models import User
from .forms import CRMUserCreateForm, CRMUserEditForm, CRMUserRegisterForm


def _is_admin(u):
    return u.is_authenticated and u.can_manage_users


# ─── PORTAL SELECTION ──────────────────────────────────────────────────────────

def portal_select(request):
    if request.user.is_authenticated:
        return _role_redirect(request.user)
    return render(request, "accounts/portal_select.html")


def _role_redirect(user):
    """Return a redirect response based on user role."""
    if user.role in (User.Role.SUPER_ADMIN, User.Role.MANAGER):
        return redirect("dashboard:management_home")
    return redirect("dashboard:home")


# ─── EMPLOYEE LOGIN (Counsellor / HR) ─────────────────────────────────────────

def employee_login(request):
    if request.user.is_authenticated:
        return _role_redirect(request.user)

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)

        if user is None:
            messages.error(request, "Invalid username or password.")
            return render(request, "accounts/login_employee.html", {"form_error": True})

        if not user.is_approved or not user.is_active:
            messages.error(request, "Your account is pending approval. Please wait for an admin to activate your account.")
            return render(request, "accounts/login_employee.html", {"form_error": True})

        if user.role not in (User.Role.COUNSELLOR, User.Role.HR):
            messages.error(request, "This portal is for Counsellors and HR only. Please use the Management Portal instead.")
            return render(request, "accounts/login_employee.html", {"form_error": True})

        login(request, user)
        return redirect(request.POST.get("next") or "dashboard:home")

    return render(request, "accounts/login_employee.html")


# ─── MANAGEMENT LOGIN (Manager / Super Admin) ─────────────────────────────────

def management_login(request):
    if request.user.is_authenticated:
        return _role_redirect(request.user)

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)

        if user is None:
            messages.error(request, "Invalid username or password.")
            return render(request, "accounts/login_management.html", {"form_error": True})

        if not user.is_active:
            messages.error(request, "This account is inactive.")
            return render(request, "accounts/login_management.html", {"form_error": True})

        if user.role not in (User.Role.SUPER_ADMIN, User.Role.MANAGER):
            messages.error(request, "This portal is for Managers and Admins only. Please use the Employee Portal instead.")
            return render(request, "accounts/login_management.html", {"form_error": True})

        login(request, user)
        return redirect(request.POST.get("next") or "dashboard:management_home")

    return render(request, "accounts/login_management.html")



@login_required
@user_passes_test(_is_admin)
def user_list(request):
    pending_users = User.objects.filter(is_approved=False).order_by("-date_joined")
    approved_users = User.objects.filter(is_approved=True).order_by("-date_joined")
    return render(request, "accounts/user_list.html", {
        "active": "users",
        "users": approved_users,
        "pending_users": pending_users
    })


@login_required
@user_passes_test(_is_admin)
def user_add(request):
    if request.method == "POST":
        form = CRMUserCreateForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.is_active = True
            user.is_approved = True
            user.save()
            log_action("Employee Created by Admin", user, user=request.user)
            messages.success(request, f"Employee user '{user.username}' created successfully.")
            return redirect("accounts:user_list")
    else:
        form = CRMUserCreateForm()
    return render(request, "accounts/user_form.html", {"active": "users", "form": form, "mode": "Add"})


@login_required
@user_passes_test(_is_admin)
def user_edit(request, pk):
    obj = get_object_or_404(User, pk=pk)
    if request.method == "POST":
        form = CRMUserEditForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            log_action("Employee Details Updated", obj, user=request.user)
            messages.success(request, f"Employee details for '{obj.username}' updated.")
            return redirect("accounts:user_list")
    else:
        form = CRMUserEditForm(instance=obj)
    return render(request, "accounts/user_form.html", {"active": "users", "form": form, "mode": "Edit", "obj": obj})


@login_required
@user_passes_test(_is_admin)
def user_reset_password(request, pk):
    target_user = get_object_or_404(User, pk=pk)
    if request.method == "POST":
        form = CRMUserPasswordResetForm(request.POST)
        if form.is_valid():
            new_pass = form.cleaned_data["new_password"]
            target_user.set_password(new_pass)
            target_user.save()
            log_action("Employee Password Reset by Admin", target_user, user=request.user)
            messages.success(request, f"Password for '{target_user.username}' reset successfully.")
            return redirect("accounts:user_list")
    else:
        form = CRMUserPasswordResetForm()
    return render(request, "accounts/user_reset_password.html", {
        "active": "users", "form": form, "target_user": target_user
    })


@login_required
@user_passes_test(_is_admin)
def user_delete(request, pk):
    target_user = get_object_or_404(User, pk=pk)
    if request.user.pk == target_user.pk:
        messages.error(request, "You cannot delete your own account.")
        return redirect("accounts:user_list")

    if request.method == "POST":
        username = target_user.username
        log_action("Employee Deleted", target_user, user=request.user)
        target_user.delete()
        messages.success(request, f"Employee user '{username}' deleted successfully.")
    return redirect("accounts:user_list")


@login_required
@user_passes_test(_is_admin)
def audit_log(request):
    logs = AuditLog.objects.select_related("user").all()
    action = request.GET.get("action")
    if action:
        logs = logs.filter(action=action)
    paginator = Paginator(logs, 50)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "accounts/audit_log.html", {
        "active": "audit", "page_obj": page,
        "actions": AuditLog.objects.values_list("action", flat=True).distinct(),
    })


def register(request):
    if request.user.is_authenticated:
        return redirect("dashboard:home")
    if request.method == "POST":
        form = CRMUserRegisterForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.is_active = False
            user.is_approved = False
            user.save()
            log_action("Employee Registration Requested", user, user=user)
            messages.success(
                request,
                "Registration successful. Your account has been created and is pending approval from an administrator before you can log in."
            )
            return redirect("accounts:login")
    else:
        form = CRMUserRegisterForm()
    return render(request, "accounts/register.html", {"form": form})


@login_required
@user_passes_test(_is_admin)
def approve_user(request, pk):
    if request.method == "POST":
        user = get_object_or_404(User, pk=pk)
        user.is_approved = True
        user.is_active = True
        user.save()
        log_action("Employee Approved", user, user=request.user)
        messages.success(request, f"User {user.username} approved successfully.")
    return redirect("accounts:user_list")


@login_required
@user_passes_test(_is_admin)
def reject_user(request, pk):
    if request.method == "POST":
        user = get_object_or_404(User, pk=pk)
        username = user.username
        log_action("Employee Rejected & Deleted", user, user=request.user)
        user.delete()
        messages.warning(request, f"Registration request for {username} rejected and user deleted.")
    return redirect("accounts:user_list")
