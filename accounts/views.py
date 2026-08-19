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
    if user.hospital:
        if user.role == User.Role.SUPER_ADMIN:
            return redirect("dashboard:superadmin_home")
        if user.role == User.Role.LEAD_ATTENDENT:
            return redirect("dashboard:telecaller_home")
        if user.role == User.Role.MANAGER:
            return redirect("dashboard:management_home")
        return redirect("dashboard:home")
    if user.role in (User.Role.SUPER_ADMIN, User.Role.MANAGER):
        return redirect("dashboard:management_home")
    return redirect("dashboard:home")


# ─── EMPLOYEE LOGIN (Counsellor / HR) ─────────────────────────────────────────

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

        login(request, user)
        next_url = request.POST.get("next")
        if next_url:
            return redirect(next_url)
        return _role_redirect(user)

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

        if not user.is_active or not user.is_approved:
            messages.error(request, "This account is inactive or pending approval.")
            return render(request, "accounts/login_management.html", {"form_error": True})

        login(request, user)
        next_url = request.POST.get("next")
        if next_url:
            return redirect(next_url)
        return _role_redirect(user)

    return render(request, "accounts/login_management.html")


def custom_logout(request):
    from django.contrib.auth import logout
    logout(request)
    messages.info(request, "You have been logged out successfully.")
    return redirect("accounts:portal_select")



@login_required
@user_passes_test(_is_admin)
def user_list(request):
    pending_users = User.objects.filter(is_approved=False).order_by("-date_joined")
    approved_users = User.objects.filter(is_approved=True).order_by("-date_joined")
    
    if request.user.hospital:
        pending_users = pending_users.filter(hospital=request.user.hospital)
        approved_users = approved_users.filter(hospital=request.user.hospital)
    return render(request, "accounts/user_list.html", {
        "active": "users",
        "users": approved_users,
        "pending_users": pending_users
    })


@login_required
@user_passes_test(_is_admin)
def user_add(request):
    if request.method == "POST":
        form = CRMUserCreateForm(request.POST, user=request.user)
        if form.is_valid():
            user = form.save(commit=False)
            user.is_active = True
            user.is_approved = True
            user.save()
            log_action("Employee Created by Admin", user, user=request.user)
            messages.success(request, f"Employee user '{user.username}' created successfully.")
            return redirect("accounts:user_list")
    else:
        form = CRMUserCreateForm(user=request.user)
        
    import json
    # Pass user roles mapping for frontend JS filtering
    reports_to_queryset = form.fields["reports_to"].queryset
    user_roles_map = {str(u.id): u.role for u in reports_to_queryset}
    
    return render(request, "accounts/user_form.html", {
        "active": "users", 
        "form": form, 
        "mode": "Add",
        "user_roles_map": json.dumps(user_roles_map)
    })


@login_required
@user_passes_test(_is_admin)
def user_edit(request, pk):
    obj = get_object_or_404(User, pk=pk)
    if request.method == "POST":
        form = CRMUserEditForm(request.POST, instance=obj, user=request.user)
        if form.is_valid():
            form.save()
            log_action("Employee Details Updated", obj, user=request.user)
            messages.success(request, f"Employee details for '{obj.username}' updated.")
            return redirect("accounts:user_list")
    else:
        form = CRMUserEditForm(instance=obj, user=request.user)
        
    import json
    reports_to_queryset = form.fields["reports_to"].queryset
    user_roles_map = {str(u.id): u.role for u in reports_to_queryset}
    
    return render(request, "accounts/user_form.html", {
        "active": "users", 
        "form": form, 
        "mode": "Edit", 
        "obj": obj,
        "user_roles_map": json.dumps(user_roles_map)
    })


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


def forgot_password(request):
    """Single-page 6-Digit OTP Password Reset Flow using Cryptographically Signed Tokens"""
    import random
    from django.core import signing
    from django.core.mail import send_mail
    from django.conf import settings

    if request.user.is_authenticated:
        return redirect("dashboard:home")

    if request.method == "POST":
        action = request.POST.get("action", "")

        if action == "send_otp":
            email_or_username = request.POST.get("email_or_username", "").strip()
            user = (
                User.objects.filter(email__iexact=email_or_username).first()
                or User.objects.filter(username__iexact=email_or_username).first()
            )

            if not user:
                messages.error(request, "No account found matching that email or username.")
                return render(request, "accounts/forgot_password.html", {"step": 1, "email_or_username": email_or_username})

            otp = str(random.randint(100000, 999999))
            
            # Create cryptographic token signed with SECRET_KEY (valid for 15 mins)
            payload = {"user_id": user.id, "otp": otp, "email": user.email}
            token = signing.dumps(payload)

            # Send OTP email via Brevo SMTP
            subject = "[Zappkode CRM] Your Password Reset OTP Code"
            message = (
                f"Hello {user.get_full_name() or user.username},\n\n"
                f"Your 6-digit OTP code to reset your password is:\n\n"
                f"🔑 {otp}\n\n"
                f"This code is valid for 15 minutes. If you did not request a password reset, please ignore this email.\n\n"
                f"Best regards,\nZappkode CRM Team"
            )
            try:
                send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=False)
                messages.success(request, f"A fresh 6-digit OTP code has been sent to {user.email}. Check your inbox.")
            except Exception as e:
                messages.error(request, f"Could not send OTP email via Brevo SMTP ({e}).")
                if settings.DEBUG:
                    messages.info(request, f"[DEBUG] Generated OTP is: {otp}")

            return render(request, "accounts/forgot_password.html", {
                "step": 2,
                "user_email": user.email,
                "token": token,
            })

        elif action == "verify_otp":
            token = request.POST.get("token", "")
            input_otp = request.POST.get("otp", "").replace(" ", "").strip()

            if not token:
                messages.error(request, "No active security token found. Please request a new OTP.")
                return render(request, "accounts/forgot_password.html", {"step": 1})

            try:
                # Valid for 15 minutes (900 seconds)
                data = signing.loads(token, max_age=900)
            except signing.SignatureExpired:
                messages.error(request, "The OTP code has expired (15 minutes limit). Please request a new OTP.")
                return render(request, "accounts/forgot_password.html", {"step": 1})
            except signing.BadSignature:
                messages.error(request, "Invalid security token. Please request a new OTP.")
                return render(request, "accounts/forgot_password.html", {"step": 1})

            if input_otp and input_otp == data.get("otp"):
                verified_payload = {"user_id": data["user_id"], "verified": True}
                verified_token = signing.dumps(verified_payload)
                messages.success(request, "OTP verified successfully! Please enter your new password.")
                return render(request, "accounts/forgot_password.html", {
                    "step": 3,
                    "verified_token": verified_token,
                })
            else:
                messages.error(request, "Invalid 6-digit OTP code. Please enter the correct OTP received in your email.")
                return render(request, "accounts/forgot_password.html", {
                    "step": 2,
                    "user_email": data.get("email"),
                    "token": token,
                })

        elif action == "reset_password":
            verified_token = request.POST.get("verified_token", "")
            if not verified_token:
                messages.error(request, "Security verification missing. Please request a new OTP.")
                return render(request, "accounts/forgot_password.html", {"step": 1})

            try:
                data = signing.loads(verified_token, max_age=900)
            except (signing.SignatureExpired, signing.BadSignature):
                messages.error(request, "Session expired or invalid token. Please request a new OTP.")
                return render(request, "accounts/forgot_password.html", {"step": 1})

            p1 = request.POST.get("password1", "").strip()
            p2 = request.POST.get("password2", "").strip()

            if len(p1) < 6:
                messages.error(request, "Password must be at least 6 characters long.")
                return render(request, "accounts/forgot_password.html", {
                    "step": 3,
                    "verified_token": verified_token,
                })

            if p1 != p2:
                messages.error(request, "Passwords do not match. Please re-enter.")
                return render(request, "accounts/forgot_password.html", {
                    "step": 3,
                    "verified_token": verified_token,
                })

            user_id = data.get("user_id")
            user = get_object_or_404(User, pk=user_id)
            user.set_password(p1)
            user.save()

            messages.success(request, f"Password for '{user.username}' reset successfully! You can now log in.")
            return redirect("accounts:portal_select")

    return render(request, "accounts/forgot_password.html", {"step": 1})


from .models import Hospital
from .forms import BusinessForm

@login_required
def business_list(request):
    if request.user.role != User.Role.SUPER_ADMIN or request.user.hospital:
        messages.error(request, "Permission denied.")
        return redirect("dashboard:home")
    businesses = Hospital.objects.all().order_by('-created_at')
    return render(request, "accounts/business_list.html", {"businesses": businesses})

@login_required
def business_add(request):
    if request.user.role != User.Role.SUPER_ADMIN or request.user.hospital:
        messages.error(request, "Permission denied.")
        return redirect("dashboard:home")
    if request.method == "POST":
        form = BusinessForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Business created successfully.")
            return redirect("accounts:business_list")
    else:
        form = BusinessForm()
    return render(request, "accounts/business_form.html", {"form": form, "mode": "Add"})

@login_required
def business_edit(request, pk):
    if request.user.role != User.Role.SUPER_ADMIN or request.user.hospital:
        messages.error(request, "Permission denied.")
        return redirect("dashboard:home")
    business = get_object_or_404(Hospital, pk=pk)
    if request.method == "POST":
        form = BusinessForm(request.POST, instance=business)
        if form.is_valid():
            form.save()
            messages.success(request, "Business updated successfully.")
            return redirect("accounts:business_list")
    else:
        form = BusinessForm(instance=business)
    return render(request, "accounts/business_form.html", {"form": form, "mode": "Edit"})

@login_required
def user_profile(request):
    from django.contrib.auth import update_session_auth_hash
    from django.contrib.auth.forms import PasswordChangeForm
    from .forms import UserProfileForm

    if request.method == 'POST':
        if 'update_profile' in request.POST:
            profile_form = UserProfileForm(request.POST, request.FILES, instance=request.user)
            password_form = PasswordChangeForm(request.user)
            if profile_form.is_valid():
                profile_form.save()
                messages.success(request, 'Your profile has been updated successfully!')
                return redirect('accounts:profile')
        elif 'update_password' in request.POST:
            profile_form = UserProfileForm(instance=request.user)
            password_form = PasswordChangeForm(request.user, request.POST)
            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(request, user)  # Keeps the user logged in
                messages.success(request, 'Your password was successfully updated!')
                return redirect('accounts:profile')
            else:
                messages.error(request, 'Please correct the error below in the password form.')
    else:
        profile_form = UserProfileForm(instance=request.user)
        password_form = PasswordChangeForm(request.user)

    return render(request, 'accounts/profile.html', {
        'profile_form': profile_form,
        'password_form': password_form,
        'active': 'profile'
    })
