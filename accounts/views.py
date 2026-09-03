from django.contrib.auth import authenticate, login
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.shortcuts import render, redirect, get_object_or_404
from django.core.paginator import Paginator

from audit.models import AuditLog
from audit.utils import log_action
from .models import User
from .forms import CRMUserCreateForm, CRMUserEditForm, CRMUserRegisterForm, CRMUserPasswordResetForm


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
        if user.role in (User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER):
            return redirect("dashboard:superadmin_home")
        if user.role == User.Role.DOCTOR:
            return redirect("dashboard:doctor_home")
        if user.role == User.Role.LEAD_ATTENDENT:
            return redirect("dashboard:telecaller_home")
        return redirect("dashboard:superadmin_home")
    if user.role in (User.Role.SUPER_ADMIN, User.Role.MANAGER):
        return redirect("dashboard:management_home")
    return redirect("dashboard:home")


from django.views.decorators.csrf import ensure_csrf_cookie


# ─── UNIFIED LOGIN ─────────────────────────────────────────────────────────────

@ensure_csrf_cookie
def crm_login(request):
    if request.user.is_authenticated:
        return _role_redirect(request.user)

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)

        if user is None:
            messages.error(request, "Invalid username or password.")
            return render(request, "accounts/login.html", {"form_error": True})

        if not user.is_approved or not user.is_active:
            messages.error(request, "Your account is pending approval or inactive. Please contact an admin.")
            return render(request, "accounts/login.html", {"form_error": True})

        login(request, user)
        request.session.set_expiry(2592000)  # 30 Days persistent session (2592000s)
        log_action(action="USER_LOGIN", obj=user, new_value=f"User {user.username} logged in", user=user)
        next_url = request.POST.get("next")
        if next_url:
            return redirect(next_url)
        return redirect("dashboard:welcome")

    return render(request, "accounts/login.html")


# Aliases for backwards-compatibility
employee_login = crm_login
management_login = crm_login
portal_select = crm_login


def custom_logout(request):
    from django.contrib.auth import logout
    if request.method == "POST":
        if request.user.is_authenticated:
            log_action(action="USER_LOGOUT", obj=request.user, new_value=f"User {request.user.username} logged out", user=request.user)
        logout(request)
        messages.info(request, "You have been logged out successfully.")
        return redirect("accounts:login")
    if request.user.is_authenticated:
        return _role_redirect(request.user)
    return redirect("accounts:login")



@login_required
@user_passes_test(_is_admin)
def user_list(request):
    from django.db.models import Q
    from django.core.paginator import Paginator

    q = request.GET.get("q", "").strip()
    role_filter = request.GET.get("role", "").strip()
    status_filter = request.GET.get("status", "").strip()
    page_number = request.GET.get("page", 1)
    
    try:
        page_size = int(request.GET.get("page_size", 10))
    except (ValueError, TypeError):
        page_size = 10
    if page_size not in [10, 25, 50, 100]:
        page_size = 10

    if request.method == "POST" and request.POST.get("action") == "generate_temp_profiles":
        hospital = request.user.hospital
        created = auto_generate_master_data_profiles(hospital=hospital)
        if created:
            messages.success(request, f"Successfully created {len(created)} temporary user profiles for Lead Attendants & Doctors from master data! (Default password: Nelson@123)")
        else:
            messages.info(request, "All Lead Attendants and Doctors in master data already have matching user profiles.")
        return redirect("accounts:user_list")

    pending_users = User.objects.filter(is_approved=False).order_by("-date_joined")
    users_qs = User.objects.filter(is_approved=True).order_by("-date_joined")
    
    if request.user.hospital:
        pending_users = pending_users.filter(hospital=request.user.hospital)
        users_qs = users_qs.filter(hospital=request.user.hospital)

    # Search keyword filter
    if q:
        users_qs = users_qs.filter(
            Q(username__icontains=q) |
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(email__icontains=q) |
            Q(phone__icontains=q) |
            Q(department__icontains=q) |
            Q(speciality__icontains=q)
        )

    # Role filter
    if role_filter:
        users_qs = users_qs.filter(role=role_filter)

    # Status filter
    if status_filter == "active":
        users_qs = users_qs.filter(is_active=True)
    elif status_filter == "inactive":
        users_qs = users_qs.filter(is_active=False)

    total_count = users_qs.count()

    # Roles for dropdown filter
    if request.user.hospital:
        if request.user.role == User.Role.MANAGER:
            available_roles = [
                (User.Role.LEAD_ATTENDENT, "Lead Attendant"),
                (User.Role.DOCTOR, "Doctor"),
            ]
        else:
            available_roles = [
                (User.Role.ADMIN, "Admin"),
                (User.Role.MANAGER, "Manager"),
                (User.Role.LEAD_ATTENDENT, "Lead Attendant"),
                (User.Role.DOCTOR, "Doctor"),
            ]
    else:
        available_roles = User.Role.choices

    paginator = Paginator(users_qs, page_size)
    page_obj = paginator.get_page(page_number)

    return render(request, "accounts/user_list.html", {
        "active": "users",
        "users": page_obj,
        "page_obj": page_obj,
        "paginator": paginator,
        "total_count": total_count,
        "pending_users": pending_users,
        "q": q,
        "role_filter": role_filter,
        "status_filter": status_filter,
        "page_size": page_size,
        "available_roles": available_roles,
    })


def auto_generate_master_data_profiles(hospital=None):
    """
    Scans Lead custom_data & relations to auto-generate temporary User profiles
    and HospitalDoctor entries for uncreated Lead Attendants and Doctors.
    Default Temporary Password: 'Nelson@123'
    """
    from django.db.models import Q
    from leads.models import Lead, HospitalDoctor
    import re

    # 1. Gather distinct Lead Attendant names and Doctor names from Lead records
    lead_qs = Lead.objects.all()
    if hospital:
        lead_qs = lead_qs.filter(hospital=hospital)

    attendants = set()
    doctors = set()

    for (cd,) in lead_qs.values_list('custom_data'):
        cd = cd or {}
        att = cd.get('lead_attendant')
        doc = cd.get('doctor')
        
        if att and str(att).strip().lower() not in ('none', 'nan', '', '-', 'unassigned'):
            attendants.add(str(att).strip())
        if doc and str(doc).strip().lower() not in ('none', 'nan', '', '-', 'select doctor', '-- select doctor / consultant --'):
            for d in str(doc).split(','):
                d_clean = d.strip()
                if d_clean and d_clean.lower() not in ('none', 'nan', '', '-', 'select doctor'):
                    doctors.add(d_clean)

    created_users = []

    # 2. Auto-generate Temporary User Profiles for Lead Attendants
    for att_name in attendants:
        # Check if matching user exists
        names = att_name.split()
        first_n = names[0]
        last_n = " ".join(names[1:]) if len(names) > 1 else ""

        # Normalize username
        base_username = re.sub(r'[^a-zA-Z0-9]', '', att_name).lower()
        if not base_username:
            continue

        existing_user = User.objects.filter(
            Q(username__iexact=base_username) | 
            Q(first_name__iexact=first_n, last_name__iexact=last_n)
        ).first()

        if not existing_user:
            # Generate unique username
            uname = base_username
            idx = 1
            while User.objects.filter(username=uname).exists():
                uname = f"{base_username}{idx}"
                idx += 1

            new_user = User.objects.create_user(
                username=uname,
                password="Nelson@123",
                first_name=first_n,
                last_name=last_n,
                role=User.Role.LEAD_ATTENDENT,
                hospital=hospital,
                is_active=True,
                is_approved=True,
            )
            created_users.append(new_user)

    # 3. Auto-generate Temporary User & HospitalDoctor Profiles for Doctors
    for doc_name in doctors:
        clean_doc_name = re.sub(r"^(dr\.?|doctor)\s+", "", doc_name, flags=re.IGNORECASE).strip()
        if not clean_doc_name:
            continue

        doc_names = clean_doc_name.split()
        first_n = doc_names[0]
        last_n = " ".join(doc_names[1:]) if len(doc_names) > 1 else ""
        raw_uname = f"dr_{re.sub(r'[^a-zA-Z0-9]', '', clean_doc_name).lower()}"

        existing_user = User.objects.filter(
            Q(username__iexact=raw_uname) | 
            Q(first_name__iexact=first_n, last_name__iexact=last_n)
        ).first()

        if not existing_user:
            uname = raw_uname
            idx = 1
            while User.objects.filter(username__iexact=uname).exists():
                uname = f"{raw_uname}{idx}"
                idx += 1

            existing_user = User.objects.create_user(
                username=uname,
                password="Nelson@123",
                first_name=first_n,
                last_name=last_n,
                role=User.Role.DOCTOR,
                hospital=hospital,
                is_active=True,
                is_approved=True,
            )
            created_users.append(existing_user)

        # Ensure sync to HospitalDoctor model
        if hospital and existing_user and existing_user.role == User.Role.DOCTOR:
            sync_doctor_profile(existing_user)

    return created_users


import re

def sync_doctor_profile(user):
    """
    Syncs a User (with role DOCTOR) to HospitalDoctor and MasterItem (Doctors).
    """
    if not user or user.role != User.Role.DOCTOR or not user.hospital:
        return None
    from leads.models import HospitalDoctor, HospitalDepartment, MasterGroup, MasterItem
    
    doc_name = user.get_full_name().strip() or user.username
    # Clean redundant 'Dr.' or 'Doctor' prefixes
    clean_name = re.sub(r"^(dr\.?|doctor)\s+", "", doc_name, flags=re.IGNORECASE).strip()
    if not clean_name:
        clean_name = doc_name

    doc = HospitalDoctor.objects.filter(hospital=user.hospital, user=user).first()
    if not doc:
        doc = HospitalDoctor.objects.filter(hospital=user.hospital, name__iexact=clean_name).first()

    if not doc:
        doc = HospitalDoctor.objects.create(
            hospital=user.hospital,
            user=user,
            name=clean_name,
            contact_number=user.phone or "",
            email=user.email or "",
            specialization=user.speciality or "",
            is_active=user.is_active and user.is_active_employee,
        )
    else:
        doc.user = user
        doc.name = clean_name
        if user.phone:
            doc.contact_number = user.phone
        if user.email:
            doc.email = user.email
        if user.speciality:
            doc.specialization = user.speciality
        doc.is_active = (user.is_active and user.is_active_employee)
        doc.save()

    # Link department if available in hospital masters
    if user.department:
        dept = HospitalDepartment.objects.filter(
            hospital=user.hospital,
            name__iexact=user.department
        ).first()
        if dept:
            if not doc.department:
                doc.department = dept
            doc.departments.add(dept)
            doc.save()

    # Auto-sync DOCTOR role users to MasterItem 'Doctors' group
    doc_grp = MasterGroup.objects.filter(name__iexact='Doctors').first()
    if doc_grp:
        MasterItem.objects.get_or_create(
            group=doc_grp,
            hospital=user.hospital,
            name=clean_name,
            defaults={"is_active": True}
        )
    return doc


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

            # Auto-sync DOCTOR role users to HospitalDoctor and MasterItem
            if user.role == User.Role.DOCTOR:
                sync_doctor_profile(user)

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
    if request.user.role == User.Role.MANAGER and request.user.hospital:
        if request.user.pk != obj.pk and obj.role not in [User.Role.DOCTOR, User.Role.LEAD_ATTENDENT]:
            messages.error(request, "Managers can only edit Doctors, Lead Attendants, or their own profile.")
            return redirect("accounts:user_list")
    if request.method == "POST":
        form = CRMUserEditForm(request.POST, instance=obj, user=request.user)
        if form.is_valid():
            saved_user = form.save()
            log_action("Employee Details Updated", obj, user=request.user)

            # Auto-sync DOCTOR role users to HospitalDoctor and MasterItem
            if saved_user.role == User.Role.DOCTOR:
                sync_doctor_profile(saved_user)

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


def check_username_availability(request):
    """Check username duplicity in background and return suggestions if taken."""
    import random
    import re
    from django.http import JsonResponse

    username = request.GET.get("username", "").strip()
    exclude_id = request.GET.get("exclude_id", None)

    if not username:
        return JsonResponse({"available": True, "message": "", "suggestions": []})

    base_cleaned = re.sub(r"[^a-zA-Z0-9._-]", "", username).lower()
    if not base_cleaned:
        base_cleaned = "user"

    qs = User.objects.filter(username__iexact=username)
    if exclude_id and str(exclude_id).isdigit():
        qs = qs.exclude(pk=int(exclude_id))

    is_taken = qs.exists()

    suggestions = []
    if is_taken:
        # Generate 4-5 unique available username suggestions
        candidates = [
            f"{base_cleaned}{random.randint(10, 99)}",
            f"{base_cleaned}_{random.randint(10, 999)}",
            f"{base_cleaned}.{random.randint(10, 99)}",
            f"{base_cleaned}_crm",
            f"{base_cleaned}{random.randint(100, 999)}",
            f"{base_cleaned}2026",
            f"user_{base_cleaned}" if not base_cleaned.startswith("user") else f"{base_cleaned}_pro",
        ]
        for cand in candidates:
            if cand != username.lower() and not User.objects.filter(username__iexact=cand).exists():
                if cand not in suggestions:
                    suggestions.append(cand)
            if len(suggestions) >= 4:
                break

    return JsonResponse({
        "available": not is_taken,
        "username": username,
        "suggestions": suggestions,
        "message": f"Username '@{username}' is available!" if not is_taken else f"Username '@{username}' is already taken.",
    })


@login_required
def doctor_schedule_manage(request, pk):
    from leads.models import DoctorSchedule, DoctorLeave, Appointment
    doctor = get_object_or_404(User, pk=pk)
    
    # Check permissions
    is_self_doctor = (request.user.pk == doctor.pk and request.user.role == User.Role.DOCTOR)
    is_admin = request.user.can_manage_users or request.user.role in [User.Role.SUPER_ADMIN, User.Role.ADMIN]
    if not (is_self_doctor or is_admin):
        messages.error(request, "Access denied.")
        return redirect("dashboard:home")
        
    schedule, _ = DoctorSchedule.objects.get_or_create(
        doctor=doctor,
        defaults={"hospital": doctor.hospital or request.user.hospital}
    )
    
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "update_schedule":
            schedule.opd_start_time = request.POST.get("opd_start_time", "09:00")
            schedule.opd_end_time = request.POST.get("opd_end_time", "17:00")
            schedule.slot_duration_minutes = int(request.POST.get("slot_duration_minutes", 30))
            schedule.is_available = (request.POST.get("is_available") == "1")
            schedule.off_days = request.POST.get("off_days", "Sunday")
            schedule.save()
            messages.success(request, "Doctor schedule updated successfully.")
            
        elif action in ["add_leave", "edit_leave"]:
            from datetime import datetime
            from django.utils import timezone
            from django.db.models import Q

            leave_id = request.POST.get("leave_id")
            start_date_str = request.POST.get("start_date")
            end_date_str = request.POST.get("end_date") or start_date_str
            reason = request.POST.get("reason", "").strip()
            is_full_day = (request.POST.get("is_full_day") == "1")
            start_time = request.POST.get("start_time") or None
            end_time = request.POST.get("end_time") or None

            if not start_date_str:
                messages.error(request, "Start date is required.")
                return redirect("accounts:doctor_schedule_manage", pk=doctor.pk)

            try:
                today = timezone.localdate()
                s_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
                e_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()

                if s_date < today:
                    messages.error(request, "Leave date cannot be in the past. Please select today or a future date.")
                    return redirect("accounts:doctor_schedule_manage", pk=doctor.pk)
                if e_date < s_date:
                    messages.error(request, "End date cannot be earlier than start date.")
                    return redirect("accounts:doctor_schedule_manage", pk=doctor.pk)

                # 1. If explicit edit/extend of an existing leave record
                if leave_id and str(leave_id).isdigit():
                    existing_leave = DoctorLeave.objects.filter(pk=int(leave_id), doctor=doctor).first()
                    if existing_leave:
                        existing_leave.start_date = s_date
                        existing_leave.end_date = e_date
                        existing_leave.is_full_day = is_full_day
                        existing_leave.start_time = start_time if not is_full_day else None
                        existing_leave.end_time = end_time if not is_full_day else None
                        if reason:
                            existing_leave.reason = reason
                        existing_leave.save()
                        # Cleanup any duplicate/redundant overlapping leaves
                        DoctorLeave.objects.filter(doctor=doctor, start_date=s_date, end_date=e_date).exclude(pk=existing_leave.pk).delete()
                        messages.success(request, f"Leave record updated successfully ({s_date.strftime('%d-%m-%Y')} to {e_date.strftime('%d-%m-%Y')}).")
                        return redirect("accounts:doctor_schedule_manage", pk=doctor.pk)

                # 2. Check if a leave with SAME start_date already exists -> EXTEND/UPDATE IT!
                same_start_leave = DoctorLeave.objects.filter(doctor=doctor, start_date=s_date).first()
                if same_start_leave:
                    same_start_leave.end_date = max(same_start_leave.end_date, e_date)
                    same_start_leave.is_full_day = is_full_day
                    same_start_leave.start_time = start_time if not is_full_day else None
                    same_start_leave.end_time = end_time if not is_full_day else None
                    if reason:
                        same_start_leave.reason = reason
                    same_start_leave.save()
                    # Delete any other exact duplicates
                    DoctorLeave.objects.filter(doctor=doctor, start_date=s_date).exclude(pk=same_start_leave.pk).delete()
                    messages.success(request, f"Existing leave extended/updated from {s_date.strftime('%d-%m-%Y')} to {same_start_leave.end_date.strftime('%d-%m-%Y')}.")
                    return redirect("accounts:doctor_schedule_manage", pk=doctor.pk)

                # 3. Check if exact (start_date, end_date) or overlapping range already exists
                exact_leave = DoctorLeave.objects.filter(doctor=doctor, start_date=s_date, end_date=e_date).first()
                if exact_leave:
                    exact_leave.is_full_day = is_full_day
                    exact_leave.start_time = start_time if not is_full_day else None
                    exact_leave.end_time = end_time if not is_full_day else None
                    if reason:
                        exact_leave.reason = reason
                    exact_leave.save()
                    DoctorLeave.objects.filter(doctor=doctor, start_date=s_date, end_date=e_date).exclude(pk=exact_leave.pk).delete()
                    messages.info(request, f"Leave for {s_date.strftime('%d-%m-%Y')} to {e_date.strftime('%d-%m-%Y')} was updated.")
                    return redirect("accounts:doctor_schedule_manage", pk=doctor.pk)

                # 4. Otherwise create new leave record
                DoctorLeave.objects.create(
                    doctor=doctor,
                    hospital=doctor.hospital or request.user.hospital,
                    start_date=s_date,
                    end_date=e_date,
                    is_full_day=is_full_day,
                    start_time=start_time if not is_full_day else None,
                    end_time=end_time if not is_full_day else None,
                    reason=reason or "Personal Leave",
                    created_by=request.user
                )
                messages.success(request, f"Doctor leave successfully scheduled from {s_date.strftime('%d-%m-%Y')} to {e_date.strftime('%d-%m-%Y')}.")
            except ValueError:
                messages.error(request, "Invalid date format provided.")
                
        elif action == "delete_leave":
            leave_id = request.POST.get("leave_id")
            DoctorLeave.objects.filter(pk=leave_id, doctor=doctor).delete()
            messages.success(request, "Leave removed.")
            
        return redirect("accounts:doctor_schedule_manage", pk=doctor.pk)
        
    # Auto-cleanup historic duplicate rows in database
    seen_leave_keys = set()
    for l in DoctorLeave.objects.filter(doctor=doctor).order_by("id"):
        key = (l.start_date, l.end_date, l.is_full_day, l.start_time, l.end_time)
        if key in seen_leave_keys:
            l.delete()
        else:
            seen_leave_keys.add(key)

    leaves = DoctorLeave.objects.filter(doctor=doctor).order_by("-start_date")
    appointments = Appointment.objects.filter(doctor_user=doctor).select_related("lead").order_by("-appointment_date")[:50]
    
    from django.utils import timezone
    today = timezone.localdate()

    return render(request, "accounts/doctor_schedule_manage.html", {
        "doctor": doctor,
        "schedule": schedule,
        "leaves": leaves,
        "appointments": appointments,
        "active": "users" if is_admin else "doctor_schedule",
        "is_admin": is_admin,
        "today": today,
    })


@login_required
@user_passes_test(_is_admin)
def user_reset_password(request, pk):
    target_user = get_object_or_404(User, pk=pk)
    if request.user.role == User.Role.MANAGER and request.user.hospital:
        if request.user.pk != target_user.pk and target_user.role not in [User.Role.DOCTOR, User.Role.LEAD_ATTENDENT]:
            messages.error(request, "Managers can only reset passwords for Doctors, Lead Attendants, or their own account.")
            return redirect("accounts:user_list")
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

    if request.user.role == User.Role.MANAGER and request.user.hospital:
        if target_user.role not in [User.Role.DOCTOR, User.Role.LEAD_ATTENDENT]:
            messages.error(request, "Managers can only delete Doctor and Lead Attendant accounts.")
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
    user = get_object_or_404(User, pk=pk)
    if request.user.role == User.Role.MANAGER and request.user.hospital:
        if user.role not in [User.Role.DOCTOR, User.Role.LEAD_ATTENDENT]:
            messages.error(request, "Managers can only approve Doctor and Lead Attendant accounts.")
            return redirect("accounts:user_list")
    if request.method == "POST":
        user.is_approved = True
        user.is_active = True
        user.save()
        log_action("Employee Approved", user, user=request.user)
        if user.role == User.Role.DOCTOR:
            sync_doctor_profile(user)
        messages.success(request, f"User {user.username} approved successfully.")
    return redirect("accounts:user_list")


@login_required
@user_passes_test(_is_admin)
def reject_user(request, pk):
    user = get_object_or_404(User, pk=pk)
    if request.user.role == User.Role.MANAGER and request.user.hospital:
        if user.role not in [User.Role.DOCTOR, User.Role.LEAD_ATTENDENT]:
            messages.error(request, "Managers can only reject Doctor and Lead Attendant accounts.")
            return redirect("accounts:user_list")
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

            if not user.email:
                messages.error(request, "This account does not have a registered email address. Please contact your system administrator.")
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

            messages.success(request, f"Password for '{user.username}' reset successfully! You can now log in with your new password.")
            return redirect("accounts:login")

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
            messages.success(request, f"Business '{business.name}' updated successfully.")
            return redirect("accounts:business_list")
    else:
        form = BusinessForm(instance=business)
    return render(request, "accounts/business_form.html", {"form": form, "mode": "Edit"})

@login_required
def business_toggle_active(request, pk):
    """Activate or Deactivate a Business/Tenant."""
    if request.user.role != User.Role.SUPER_ADMIN or request.user.hospital:
        messages.error(request, "Permission denied.")
        return redirect("dashboard:home")
    if request.method == "POST":
        business = get_object_or_404(Hospital, pk=pk)
        business.is_active = not business.is_active
        business.save(update_fields=['is_active'])
        status_text = "activated" if business.is_active else "deactivated"
        messages.success(request, f"Business '{business.name}' has been {status_text} successfully.")
    return redirect("accounts:business_list")

@login_required
def business_delete(request, pk):
    """Safely remove a business. Protects from accidental deletion if critical data exists."""
    if request.user.role != User.Role.SUPER_ADMIN or request.user.hospital:
        messages.error(request, "Permission denied.")
        return redirect("dashboard:home")
    if request.method == "POST":
        business = get_object_or_404(Hospital, pk=pk)
        name = business.name
        try:
            # Delete associated users or unassign them first if any
            User.objects.filter(hospital=business).update(hospital=None, is_active=False)
            business.delete()
            messages.success(request, f"Business '{name}' and associated configuration have been removed.")
        except Exception as e:
            messages.error(request, f"Could not delete business '{name}': {e}. You can deactivate it instead.")
    return redirect("accounts:business_list")

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
            else:
                messages.error(request, 'Profile update failed. Please check and correct the errors below.')
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
