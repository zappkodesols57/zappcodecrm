import json
# CRM Views - Auto-reloaded for schema sync
from collections import defaultdict
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import models
from django.db.models import Q, F, Max
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone

from followups.models import FollowUp, Note, Activity, ActivityType, FollowUpMode, FollowUpStatus
from admissions.models import Admission
from accounts.models import User, Hospital
from leads.models import (
    Lead, SourceCategory, LeadSource, Campaign, Course, Trainer, Batch, LeadStage, Tag, 
    MasterGroup, MasterItem, HospitalBranch, HospitalDepartment, HospitalDoctor, 
    HospitalDisease, DoctorBranchAvailability, DealStatus, LeadTemperature,
    AdmissionStatus, Appointment, AppointmentStatus,
)
from leads.forms import (
    LeadForm, HospitalLeadForm, SourceCategoryForm, LeadSourceForm, CampaignForm, CourseForm, TrainerForm, BatchForm, LeadStageForm,
)




# ===========================================================================
# 🏥 HOSPITAL BUSINESS LEAD CYCLE & CONFIGURATION
# ===========================================================================

def book_appointment(request, pk):
    from django.core.exceptions import PermissionDenied
    from django.contrib import messages
    from leads.models import Lead, Appointment, AppointmentStatus
    
    lead = get_object_or_404(Lead, pk=pk)
    
    if not _can_access_lead(request.user, lead):
        raise PermissionDenied("You do not have access to this lead.")
        
    if request.method == "POST":
        doctor_name = request.POST.get('doctor_name')
        appointment_date = request.POST.get('appointment_date')
        appointment_time = request.POST.get('appointment_time') or None
        notes = request.POST.get('notes', '')
        
        if doctor_name and appointment_date:
            Appointment.objects.create(
                lead=lead,
                hospital=request.user.hospital,
                doctor_name=doctor_name,
                appointment_date=appointment_date,
                appointment_time=appointment_time,
                notes=notes,
                created_by=request.user
            )
            messages.success(request, "Appointment booked successfully.")
        else:
            messages.error(request, "Doctor name and date are required.")
            
    return redirect('leads:lead_detail', pk=pk)


@login_required
def check_duplicate_mobile(request):
    from django.http import JsonResponse
    raw_mobile = request.GET.get("mobile", "").strip()
    digits = Lead.clean_mobile(raw_mobile)
    if not digits or len(digits) < 8:
        return JsonResponse({"exists": False})
    
    existing = Lead.objects.filter(is_archived=False)
    for lead in existing.only("id", "lead_code", "name", "mobile", "assigned_to"):
        if Lead.clean_mobile(lead.mobile) == digits:
            assigned_name = lead.assigned_to.get_full_name() if lead.assigned_to else (lead.assigned_to.username if lead.assigned_to else "Unassigned")
            return JsonResponse({
                "exists": True,
                "lead_id": lead.pk,
                "lead_code": lead.lead_code,
                "name": lead.name,
                "assigned_to": assigned_name
            })
    return JsonResponse({"exists": False})


@login_required
def check_duplicate_uhid(request):
    from django.http import JsonResponse
    from django.db.models import Q
    raw_uhid = request.GET.get("uhid", "").strip()
    exclude_lead_id = request.GET.get("exclude_id", "").strip()
    if not raw_uhid:
        return JsonResponse({"exists": False})

    qs = Lead.objects.filter(is_archived=False)
    if request.user.hospital:
        qs = qs.filter(hospital=request.user.hospital)

    if exclude_lead_id and exclude_lead_id.isdigit():
        qs = qs.exclude(pk=int(exclude_lead_id))

    dup = qs.filter(
        Q(custom_data__uhid_id_no__iexact=raw_uhid) |
        Q(custom_data__uhid_no__iexact=raw_uhid)
    ).first()

    if dup:
        assigned_name = dup.assigned_to.get_full_name() if dup.assigned_to else (dup.assigned_to.username if dup.assigned_to else "Unassigned")
        return JsonResponse({
            "exists": True,
            "lead_id": dup.pk,
            "lead_code": dup.lead_code,
            "name": dup.name,
            "assigned_to": assigned_name
        })

    return JsonResponse({"exists": False})


@login_required
def doctor_slots_api(request):
    import re
    from datetime import datetime, time, timedelta
    from django.http import JsonResponse
    from django.utils import timezone
    from django.db import models
    from django.db.models import Q
    from accounts.models import User
    from leads.models import Appointment, DoctorSchedule, DoctorLeave, AppointmentStatus
    
    if request.GET.get("clear_wa_session") == "1":
        request.session.pop("auto_open_whatsapp", None)
        return JsonResponse({"status": "cleared"})
        
    try:
        doctor_name = request.GET.get("doctor", "").strip()
        date_str = request.GET.get("date", "").strip()
        
        if not doctor_name or not date_str:
            return JsonResponse({"error": "Doctor and date are required", "slots": []})
            
        try:
            req_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return JsonResponse({"error": "Invalid date format", "slots": []})
            
        hospital = request.user.hospital
        
        # Clean doctor name (remove prefixes like Dr. / Dr / Doctor)
        clean_doc_name = re.sub(r'^(dr\.?|doctor)\s+', '', doctor_name, flags=re.IGNORECASE).strip()
        
        # Try finding doctor user
        doctor_user = None
        user_qs = User.objects.filter(role=User.Role.DOCTOR)
        if hospital:
            user_qs = user_qs.filter(hospital=hospital)
            
        for u in user_qs:
            full_name = (u.get_full_name() or "").strip().lower()
            u_clean = re.sub(r'^(dr\.?|doctor)\s+', '', full_name, flags=re.IGNORECASE).strip()
            username = u.username.lower()
            search_low = clean_doc_name.lower()
            doc_raw_low = doctor_name.lower()
            
            if (search_low and (search_low in full_name or search_low in u_clean or search_low in username)) or \
               (doc_raw_low and (doc_raw_low in full_name or doc_raw_low in username)):
                doctor_user = u
                break
        
        # Helper to parse time object from string or time
        def parse_time_val(t_val, default_time):
            if isinstance(t_val, time):
                return t_val
            if isinstance(t_val, str):
                for fmt in ("%H:%M:%S", "%H:%M", "%I:%M %p", "%I:%M%p"):
                    try:
                        return datetime.strptime(t_val.strip(), fmt).time()
                    except ValueError:
                        pass
            return default_time

        # Check doctor OPD start, end and slot duration
        duration = 30
        is_doctor_active = True
        off_days = ["Sunday"]
        
        doctor_opd_start = time(9, 0)
        doctor_opd_end = time(18, 0)
        
        if doctor_user:
            sched = getattr(doctor_user, 'doctor_schedule', None)
            if sched:
                if sched.opd_start_time:
                    doctor_opd_start = parse_time_val(sched.opd_start_time, time(9, 0))
                if sched.opd_end_time:
                    doctor_opd_end = parse_time_val(sched.opd_end_time, time(18, 0))
                if sched.slot_duration_minutes and sched.slot_duration_minutes > 0:
                    duration = sched.slot_duration_minutes
                is_doctor_active = sched.is_available
                if sched.off_days:
                    off_days = [d.strip().lower() for d in sched.off_days.split(",") if d.strip()]

        # If start >= end, fallback to 9 AM to 6 PM
        if doctor_opd_start >= doctor_opd_end:
            doctor_opd_start = time(9, 0)
            doctor_opd_end = time(18, 0)
                    
        # Check day of week off
        day_name = req_date.strftime("%A").lower()
        is_weekly_off = (day_name in [d.lower() for d in off_days]) or (not is_doctor_active)
        
        # Check doctor leave
        leaves = DoctorLeave.objects.filter(
            start_date__lte=req_date,
            end_date__gte=req_date
        )
        if doctor_user:
            leaves = leaves.filter(doctor=doctor_user)
        elif hospital:
            leaves = leaves.filter(hospital=hospital)
            
        full_day_leave = leaves.filter(is_full_day=True).first()

        # If on full day leave or weekly off, return no slots with clear alert message
        if full_day_leave:
            return JsonResponse({
                "slots": [],
                "doctor": doctor_name,
                "date": date_str,
                "is_off": False,
                "is_on_leave": True,
                "leave_reason": full_day_leave.reason or "Personal Leave"
            })

        if is_weekly_off:
            return JsonResponse({
                "slots": [],
                "doctor": doctor_name,
                "date": date_str,
                "is_off": True,
                "is_on_leave": False,
                "off_day_name": req_date.strftime("%A")
            })
            
        # Get booked appointments for this doctor on this date
        booked_apts = Appointment.objects.filter(
            hospital=hospital,
            appointment_date=req_date
        ).exclude(
            status__in=[AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW]
        )
        if doctor_user:
            booked_apts = booked_apts.filter(models.Q(doctor_user=doctor_user) | models.Q(doctor_name__iexact=doctor_name))
        else:
            booked_apts = booked_apts.filter(doctor_name__iexact=doctor_name)
            
        booked_times = set()
        for apt in booked_apts:
            if apt.appointment_time:
                booked_times.add(apt.appointment_time.strftime("%H:%M"))
                
        # Generate slots based on doctor's OPD timings
        curr_dt = datetime.combine(req_date, doctor_opd_start)
        end_dt = datetime.combine(req_date, doctor_opd_end)
        now = timezone.localtime()
        
        slots = []
        while curr_dt < end_dt:
            time_str_24 = curr_dt.strftime("%H:%M")
            time_str_12 = curr_dt.strftime("%I:%M %p")
            slot_t = curr_dt.time()
            
            # Check if partial leave blocks this slot
            partial_leave = False
            for l in leaves.filter(is_full_day=False):
                if l.start_time and l.end_time:
                    if l.start_time <= slot_t < l.end_time:
                        partial_leave = True
                        break
                        
            is_booked = time_str_24 in booked_times
            is_past = (req_date == now.date() and slot_t < now.time()) or (req_date < now.date())
            
            if full_day_leave:
                status = "leave"
                status_text = "On Leave"
            elif is_weekly_off:
                status = "off"
                status_text = "Doctor Off"
            elif partial_leave:
                status = "leave"
                status_text = "On Leave"
            elif is_booked:
                status = "booked"
                status_text = "Booked"
            elif is_past:
                status = "past"
                status_text = "Past"
            else:
                status = "available"
                status_text = "Available"
                
            slots.append({
                "time_24": time_str_24,
                "time_12": time_str_12,
                "status": status,
                "status_text": status_text,
                "available": (status == "available")
            })
            curr_dt += timedelta(minutes=duration)
            
        return JsonResponse({
            "slots": slots,
            "doctor": doctor_name,
            "date": date_str,
            "duration": duration,
            "is_off": is_weekly_off,
            "is_on_leave": bool(full_day_leave),
            "leave_reason": full_day_leave.reason if full_day_leave else ""
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({"error": str(e), "slots": []}, status=200)


def _get_target_hospital_for_config(request):
    """
    Resolve active Hospital for Hospital Master Configuration module.
    For Hospital-scoped users, returns request.user.hospital.
    For Global Super Admin, resolves from GET/POST parameter 'business', session, or first active hospital.
    """
    if request.user.hospital:
        return request.user.hospital
    
    is_global_admin = request.user.is_superuser or (
        request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital
    )
    if is_global_admin:
        biz_val = (
            request.GET.get("business", "").strip()
            or request.POST.get("business", "").strip()
            or str(request.session.get("active_business_id", "")).strip()
        )
        if biz_val and biz_val.isdigit():
            target = Hospital.objects.filter(id=int(biz_val), is_active=True).first()
            if target:
                return target
        # Fallback to first active hospital or any hospital
        return Hospital.objects.filter(is_active=True).order_by("id").first() or Hospital.objects.first()
    return None


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_configuration_view(request):
    """
    Dedicated Hospital Configuration Master Settings module for Hospital Admin, Permitted Managers & Super Admins.
    Manages:
    - Hospital Profile
    - Hospital Branches
    - Hospital Departments (Linked to Branches)
    - Doctors (Linked to Department, Diseases & Branches with availability)
    - Diseases & Medical Conditions (Linked to Department)
    """
    is_global_admin = request.user.is_superuser or (
        request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital
    )
    available_businesses = list(Hospital.objects.filter(is_active=True).order_by("id")) if is_global_admin else []

    hospital = _get_target_hospital_for_config(request)
    if not hospital:
        messages.error(request, "No active hospital found. Please create a Hospital organization first.")
        return redirect("dashboard:home")

    branches = HospitalBranch.objects.filter(hospital=hospital).prefetch_related("departments", "doctors")
    departments = HospitalDepartment.objects.filter(hospital=hospital).prefetch_related("branches", "doctors", "diseases")
    doctors = HospitalDoctor.objects.filter(hospital=hospital).select_related("department", "user").prefetch_related("branches", "associated_diseases", "availabilities")
    diseases = HospitalDisease.objects.filter(hospital=hospital).select_related("department")

    active_tab = request.GET.get("tab", "branches")

    # Doctor login users eligible for linking (strictly active DOCTOR role accounts in this hospital)
    doctor_users = User.objects.filter(
        hospital=hospital,
        role=User.Role.DOCTOR,
        is_active=True
    ).select_related("doctor_profile").order_by("first_name", "username")

    context = {
        "active": "hospital_config",
        "hospital": hospital,
        "branches": branches,
        "departments": departments,
        "doctors": doctors,
        "diseases": diseases,
        "active_tab": active_tab,
        "doctor_users": doctor_users,
        "is_global_admin": is_global_admin,
        "available_businesses": available_businesses,
    }
    return render(request, "hospital/leads/configuration.html", context)


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_profile_save(request):
    """Update Hospital / Organization Core Profile details."""
    hospital = _get_target_hospital_for_config(request)
    if not hospital:
        messages.error(request, "No hospital context found.")
        return redirect("dashboard:home")

    biz_param = f"&business={hospital.id}" if request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital else ""

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        contact_email = request.POST.get("contact_email", "").strip()
        phone = request.POST.get("phone", "").strip()
        registration_no = request.POST.get("registration_no", "").strip()
        address = request.POST.get("address", "").strip()

        if not name:
            messages.error(request, "Hospital / Organization name is required.")
            return redirect(f"/leads/hospital-configuration/?tab=profile{biz_param}")

        if phone:
            import re
            raw_digits = re.sub(r"\D", "", phone)
            # Accept valid 10-digit mobile (or prefixed with +91/0) or 10-11 digit landline
            if len(raw_digits) == 12 and raw_digits.startswith("91"):
                raw_digits = raw_digits[2:]
            elif len(raw_digits) == 11 and raw_digits.startswith("0"):
                raw_digits = raw_digits[1:]

            if len(raw_digits) < 10 or len(raw_digits) > 11:
                messages.error(request, "Please enter a valid contact phone number (10 digits for mobile or 10-11 digits with STD code).")
                return redirect(f"/leads/hospital-configuration/?tab=profile{biz_param}")
            phone = raw_digits

        hospital.name = name
        hospital.contact_email = contact_email
        hospital.phone = phone
        hospital.registration_no = registration_no
        hospital.address = address

        if "logo" in request.FILES:
            hospital.logo = request.FILES["logo"]

        hospital.save()
        messages.success(request, f"Hospital profile '{name}' updated successfully.")

    return redirect(f"/leads/hospital-configuration/?tab=profile{biz_param}")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_branch_save(request, pk=None):
    hospital = _get_target_hospital_for_config(request)
    if not hospital:
        messages.error(request, "No hospital context found.")
        return redirect("dashboard:home")

    biz_param = f"&business={hospital.id}" if request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital else ""

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        code = request.POST.get("code", "").strip()
        city = request.POST.get("city", "").strip()
        address = request.POST.get("address", "").strip()
        contact_number = request.POST.get("contact_number", "").strip()
        is_main = request.POST.get("is_main_branch") == "1"
        order = int(request.POST.get("order", 0) or 0)

        if not name:
            messages.error(request, "Branch name is required.")
            return redirect(f"/leads/hospital-configuration/?tab=branches{biz_param}")

        if contact_number:
            import re
            digits = re.sub(r"\D", "", contact_number)
            if len(digits) == 12 and digits.startswith("91"):
                digits = digits[2:]
            elif len(digits) == 11 and digits.startswith("0"):
                digits = digits[1:]
            if len(digits) != 10 or digits[0] not in '6789':
                messages.error(request, "Branch contact number must be a valid 10-digit number starting with 6, 7, 8, or 9.")
                return redirect(f"/leads/hospital-configuration/?tab=branches{biz_param}")
            contact_number = digits

        if is_main:
            # Only one main branch per hospital
            HospitalBranch.objects.filter(hospital=hospital).update(is_main_branch=False)

        if pk:
            branch = get_object_or_404(HospitalBranch, pk=pk, hospital=hospital)
            branch.name = name
            branch.code = code
            branch.city = city
            branch.address = address
            branch.contact_number = contact_number
            branch.is_main_branch = is_main
            branch.order = order
            branch.save()
            messages.success(request, f"Branch '{name}' updated successfully.")
        else:
            HospitalBranch.objects.create(
                hospital=hospital,
                name=name,
                code=code,
                city=city,
                address=address,
                contact_number=contact_number,
                is_main_branch=is_main,
                order=order,
                is_active=True,
            )
            messages.success(request, f"Branch '{name}' created successfully.")

    return redirect(f"/leads/hospital-configuration/?tab=branches{biz_param}")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_department_save(request, pk=None):
    hospital = _get_target_hospital_for_config(request)
    if not hospital:
        messages.error(request, "No hospital context found.")
        return redirect("dashboard:home")

    biz_param = f"&business={hospital.id}" if request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital else ""

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        code = request.POST.get("code", "").strip()
        description = request.POST.get("description", "").strip()
        order = int(request.POST.get("order", 0) or 0)
        branch_ids = request.POST.getlist("branches")

        if not name:
            messages.error(request, "Department name is required.")
            return redirect(f"/leads/hospital-configuration/?tab=departments{biz_param}")

        if pk:
            dept = get_object_or_404(HospitalDepartment, pk=pk, hospital=hospital)
            dept.name = name
            dept.code = code
            dept.description = description
            dept.order = order
            dept.save()
            dept.branches.set(HospitalBranch.objects.filter(id__in=branch_ids, hospital=hospital))
            messages.success(request, f"Department '{name}' updated successfully.")
        else:
            dept = HospitalDepartment.objects.create(
                hospital=hospital,
                name=name,
                code=code,
                description=description,
                order=order,
                is_active=True,
            )
            dept.branches.set(HospitalBranch.objects.filter(id__in=branch_ids, hospital=hospital))
            messages.success(request, f"Department '{name}' created successfully.")

    return redirect(f"/leads/hospital-configuration/?tab=departments{biz_param}")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_disease_save(request, pk=None):
    hospital = _get_target_hospital_for_config(request)
    if not hospital:
        messages.error(request, "No hospital context found.")
        return redirect("dashboard:home")

    biz_param = f"&business={hospital.id}" if request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital else ""

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        code = request.POST.get("code", "").strip()
        department_id = request.POST.get("department")
        description = request.POST.get("description", "").strip()
        order = int(request.POST.get("order", 0) or 0)

        if not name or not department_id:
            messages.error(request, "Disease name and Department are required.")
            return redirect(f"/leads/hospital-configuration/?tab=diseases{biz_param}")

        dept = get_object_or_404(HospitalDepartment, pk=department_id, hospital=hospital)

        if pk:
            disease = get_object_or_404(HospitalDisease, pk=pk, hospital=hospital)
            disease.name = name
            disease.code = code
            disease.department = dept
            disease.description = description
            disease.order = order
            disease.save()
            messages.success(request, f"Disease '{name}' updated successfully.")
        else:
            HospitalDisease.objects.create(
                hospital=hospital,
                name=name,
                code=code,
                department=dept,
                description=description,
                order=order,
                is_active=True,
            )
            messages.success(request, f"Disease '{name}' added successfully.")

    return redirect(f"/leads/hospital-configuration/?tab=diseases{biz_param}")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_doctor_save(request, pk=None):
    hospital = _get_target_hospital_for_config(request)
    if not hospital:
        messages.error(request, "No hospital context found.")
        return redirect("dashboard:home")

    biz_param = f"&business={hospital.id}" if request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital else ""

    if request.method == "POST":
        user_id = request.POST.get("user", "").strip()
        name = request.POST.get("name", "").strip()
        department_ids = request.POST.getlist("departments")
        if not department_ids and request.POST.get("department"):
            department_ids = [request.POST.get("department")]
        qualification = request.POST.get("qualification", "").strip()
        specialization = request.POST.get("specialization", "").strip()
        email = request.POST.get("email", "").strip()
        contact_number = request.POST.get("contact_number", "").strip()
        raw_fee = request.POST.get("consultation_fee", "0").strip()
        try:
            consultation_fee = max(0, int(round(float(raw_fee or 0))))
        except (ValueError, TypeError):
            consultation_fee = 0
        order = int(request.POST.get("order", 0) or 0)

        disease_ids = request.POST.getlist("associated_diseases")
        branch_ids = request.POST.getlist("branches")

        if not user_id:
            messages.error(request, "Link to a registered Doctor user login profile is mandatory. Unregistered doctors cannot be created.")
            return redirect(f"/leads/hospital-configuration/?tab=doctors{biz_param}")

        doc_user = User.objects.filter(pk=user_id, hospital=hospital, role=User.Role.DOCTOR, is_active=True).first()
        if not doc_user:
            messages.error(request, "Selected user is not a valid active Doctor account in this hospital.")
            return redirect(f"/leads/hospital-configuration/?tab=doctors{biz_param}")

        # Ensure no other HospitalDoctor profile is linked to this user
        existing_doc = HospitalDoctor.objects.filter(hospital=hospital, user=doc_user)
        if pk:
            existing_doc = existing_doc.exclude(pk=pk)
        if existing_doc.exists():
            messages.error(request, f"Doctor user '{doc_user.get_full_name() or doc_user.username}' is already linked to another doctor profile.")
            return redirect(f"/leads/hospital-configuration/?tab=doctors{biz_param}")

        if not name:
            name = doc_user.get_full_name().strip() or doc_user.username
        # Clean 'Dr.' prefix if manually typed in name field
        import re
        name = re.sub(r"^(dr\.?|doctor)\s+", "", name, flags=re.IGNORECASE).strip() or name

        if not email and doc_user.email:
            email = doc_user.email
        if email:
            email = email.strip()
            if "@" not in email or not re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", email):
                messages.error(request, "Please enter a valid Doctor email address containing '@' (e.g. doctor@hospital.com).")
                return redirect(f"/leads/hospital-configuration/?tab=doctors{biz_param}")

        if not contact_number and doc_user.phone:
            contact_number = doc_user.phone
        if not specialization and doc_user.speciality:
            specialization = doc_user.speciality

        if not department_ids:
            messages.error(request, "Please assign at least one Clinical Department to the doctor.")
            return redirect(f"/leads/hospital-configuration/?tab=doctors{biz_param}")

        selected_depts = HospitalDepartment.objects.filter(id__in=department_ids, hospital=hospital)
        primary_dept = selected_depts.first()

        if pk:
            doc = get_object_or_404(HospitalDoctor, pk=pk, hospital=hospital)
            doc.name = name
            doc.user = doc_user
            doc.department = primary_dept
            doc.qualification = qualification
            doc.specialization = specialization
            doc.contact_number = contact_number
            doc.email = email
            doc.consultation_fee = consultation_fee
            doc.order = order
            doc.save()
            doc.departments.set(selected_depts)
            if disease_ids:
                doc.associated_diseases.set(HospitalDisease.objects.filter(id__in=disease_ids, hospital=hospital))
            else:
                # If no specific disease is chosen, automatically assign all diseases belonging to the selected department(s)
                all_dept_diseases = HospitalDisease.objects.filter(department__in=selected_depts, hospital=hospital, is_active=True)
                doc.associated_diseases.set(all_dept_diseases)
            
            # Sync Branch availabilities
            selected_branches = HospitalBranch.objects.filter(id__in=branch_ids, hospital=hospital)
            for b in selected_branches:
                DoctorBranchAvailability.objects.get_or_create(doctor=doc, branch=b, defaults={"is_active": True})
            DoctorBranchAvailability.objects.filter(doctor=doc).exclude(branch__in=selected_branches).delete()

            messages.success(request, f"Doctor 'Dr. {name}' updated successfully.")
        else:
            doc = HospitalDoctor.objects.create(
                hospital=hospital,
                name=name,
                user=doc_user,
                department=primary_dept,
                qualification=qualification,
                specialization=specialization,
                contact_number=contact_number,
                email=email,
                consultation_fee=consultation_fee,
                order=order,
                is_active=True,
            )
            doc.departments.set(selected_depts)
            if disease_ids:
                doc.associated_diseases.set(HospitalDisease.objects.filter(id__in=disease_ids, hospital=hospital))
            else:
                # If no specific disease is chosen, automatically assign all diseases belonging to the selected department(s)
                all_dept_diseases = HospitalDisease.objects.filter(department__in=selected_depts, hospital=hospital, is_active=True)
                doc.associated_diseases.set(all_dept_diseases)
            
            selected_branches = HospitalBranch.objects.filter(id__in=branch_ids, hospital=hospital)
            for b in selected_branches:
                DoctorBranchAvailability.objects.create(doctor=doc, branch=b, is_active=True)

            messages.success(request, f"Doctor 'Dr. {name}' registered successfully.")

        # Sync MasterItem 'Doctors'
        from leads.models import MasterGroup, MasterItem
        doc_grp = MasterGroup.objects.filter(name__iexact='Doctors').first()
        if doc_grp:
            MasterItem.objects.get_or_create(
                group=doc_grp,
                hospital=hospital,
                name=name,
                defaults={"is_active": True}
            )

    return redirect(f"/leads/hospital-configuration/?tab=doctors{biz_param}")


@login_required
@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_branch_toggle(request, pk):
    is_global_admin = request.user.is_superuser or (
        request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital
    )
    if is_global_admin:
        branch = get_object_or_404(HospitalBranch, pk=pk)
        hospital = branch.hospital
    else:
        hospital = request.user.hospital
        branch = get_object_or_404(HospitalBranch, pk=pk, hospital=hospital)

    branch.is_active = not branch.is_active
    branch.save(update_fields=["is_active"])
    status_str = "activated" if branch.is_active else "deactivated"
    messages.success(request, f"Hospital Branch '{branch.name}' has been {status_str}.")
    biz_param = f"&business={hospital.id}" if is_global_admin and hospital else ""
    return redirect(f"/leads/hospital-configuration/?tab=branches{biz_param}")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_branch_delete(request, pk):
    is_global_admin = request.user.is_superuser or (
        request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital
    )
    if is_global_admin:
        branch = HospitalBranch.objects.filter(pk=pk).first()
        hospital = branch.hospital if branch else None
    else:
        hospital = request.user.hospital
        branch = HospitalBranch.objects.filter(pk=pk, hospital=hospital).first()

    biz_param = f"&business={hospital.id}" if is_global_admin and hospital else ""
    if not branch:
        messages.info(request, "This hospital branch no longer exists or was already deleted.")
        return redirect(f"/leads/hospital-configuration/?tab=branches{biz_param}")

    branch_name = branch.name
    try:
        branch.delete()
        messages.success(request, f"Hospital Branch '{branch_name}' has been permanently deleted.")
    except Exception as e:
        # Fallback to deactivation if protected by foreign keys
        branch.is_active = False
        branch.save(update_fields=["is_active"])
        messages.warning(request, f"Branch '{branch_name}' could not be permanently deleted due to associated records, but has been deactivated and hidden.")
    return redirect(f"/leads/hospital-configuration/?tab=branches{biz_param}")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_department_toggle(request, pk):
    is_global_admin = request.user.is_superuser or (
        request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital
    )
    if is_global_admin:
        dept = get_object_or_404(HospitalDepartment, pk=pk)
        hospital = dept.hospital
    else:
        hospital = request.user.hospital
        dept = get_object_or_404(HospitalDepartment, pk=pk, hospital=hospital)

    dept.is_active = not dept.is_active
    dept.save(update_fields=["is_active"])
    status_str = "activated" if dept.is_active else "deactivated"
    messages.success(request, f"Department '{dept.name}' has been {status_str}.")
    biz_param = f"&business={hospital.id}" if is_global_admin and hospital else ""
    return redirect(f"/leads/hospital-configuration/?tab=departments{biz_param}")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_department_delete(request, pk):
    is_global_admin = request.user.is_superuser or (
        request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital
    )
    if is_global_admin:
        dept = HospitalDepartment.objects.filter(pk=pk).first()
        hospital = dept.hospital if dept else None
    else:
        hospital = request.user.hospital
        dept = HospitalDepartment.objects.filter(pk=pk, hospital=hospital).first()

    biz_param = f"&business={hospital.id}" if is_global_admin and hospital else ""
    if not dept:
        messages.info(request, "This department no longer exists or was already deleted.")
        return redirect(f"/leads/hospital-configuration/?tab=departments{biz_param}")

    dept_name = dept.name
    try:
        dept.delete()
        messages.success(request, f"Department '{dept_name}' has been permanently deleted.")
    except Exception as e:
        dept.is_active = False
        dept.save(update_fields=["is_active"])
        messages.warning(request, f"Department '{dept_name}' could not be deleted due to associated records, but has been deactivated.")
    return redirect(f"/leads/hospital-configuration/?tab=departments{biz_param}")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_disease_toggle(request, pk):
    is_global_admin = request.user.is_superuser or (
        request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital
    )
    if is_global_admin:
        dis = get_object_or_404(HospitalDisease, pk=pk)
        hospital = dis.hospital
    else:
        hospital = request.user.hospital
        dis = get_object_or_404(HospitalDisease, pk=pk, hospital=hospital)

    dis.is_active = not dis.is_active
    dis.save(update_fields=["is_active"])
    status_str = "activated" if dis.is_active else "deactivated"
    messages.success(request, f"Disease / Condition '{dis.name}' has been {status_str}.")
    biz_param = f"&business={hospital.id}" if is_global_admin and hospital else ""
    return redirect(f"/leads/hospital-configuration/?tab=diseases{biz_param}")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_disease_delete(request, pk):
    is_global_admin = request.user.is_superuser or (
        request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital
    )
    if is_global_admin:
        dis = HospitalDisease.objects.filter(pk=pk).first()
        hospital = dis.hospital if dis else None
    else:
        hospital = request.user.hospital
        dis = HospitalDisease.objects.filter(pk=pk, hospital=hospital).first()

    biz_param = f"&business={hospital.id}" if is_global_admin and hospital else ""
    if not dis:
        messages.info(request, "This disease/condition no longer exists or was already deleted.")
        return redirect(f"/leads/hospital-configuration/?tab=diseases{biz_param}")

    dis_name = dis.name
    try:
        dis.delete()
        messages.success(request, f"Disease '{dis_name}' has been permanently deleted.")
    except Exception as e:
        dis.is_active = False
        dis.save(update_fields=["is_active"])
        messages.warning(request, f"Disease '{dis_name}' could not be deleted due to associated records, but has been deactivated.")
    return redirect(f"/leads/hospital-configuration/?tab=diseases{biz_param}")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_doctor_toggle(request, pk):
    is_global_admin = request.user.is_superuser or (
        request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital
    )
    if is_global_admin:
        doc = get_object_or_404(HospitalDoctor, pk=pk)
        hospital = doc.hospital
    else:
        hospital = request.user.hospital
        doc = get_object_or_404(HospitalDoctor, pk=pk, hospital=hospital)

    doc.is_active = not doc.is_active
    doc.save(update_fields=["is_active"])
    status_str = "activated" if doc.is_active else "deactivated"
    messages.success(request, f"Doctor 'Dr. {doc.name}' has been {status_str}.")
    biz_param = f"&business={hospital.id}" if is_global_admin and hospital else ""
    return redirect(f"/leads/hospital-configuration/?tab=doctors{biz_param}")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_doctor_delete(request, pk):
    is_global_admin = request.user.is_superuser or (
        request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital
    )
    if is_global_admin:
        doc = HospitalDoctor.objects.filter(pk=pk).first()
        hospital = doc.hospital if doc else None
    else:
        hospital = request.user.hospital
        doc = HospitalDoctor.objects.filter(pk=pk, hospital=hospital).first()

    biz_param = f"&business={hospital.id}" if is_global_admin and hospital else ""
    if not doc:
        messages.info(request, "This doctor record no longer exists or was already deleted.")
        return redirect(f"/leads/hospital-configuration/?tab=doctors{biz_param}")

    doc_name = doc.name
    try:
        doc.delete()
        messages.success(request, f"Doctor 'Dr. {doc_name}' has been permanently deleted.")
    except Exception as e:
        doc.is_active = False
        doc.save(update_fields=["is_active"])
        messages.warning(request, f"Doctor 'Dr. {doc_name}' could not be deleted due to associated records, but has been deactivated.")
    return redirect(f"/leads/hospital-configuration/?tab=doctors{biz_param}")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_master_excel_import(request):
    """
    Bulk import Hospital Master data from Excel/CSV containing:
    Hospital Branch | Department | Doctor | Disease
    """
    hospital = _get_target_hospital_for_config(request)
    if not hospital:
        messages.error(request, "No hospital context found.")
        return redirect("dashboard:home")

    biz_param = f"?business={hospital.id}" if request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital else ""

    if request.method == "POST" and request.FILES.get("excel_file"):
        uploaded_file = request.FILES["excel_file"]
        filename = uploaded_file.name.lower()

        try:
            import pandas as pd
            if filename.endswith(".csv"):
                df = pd.read_csv(uploaded_file)
            else:
                df = pd.read_excel(uploaded_file)

            # Clean and normalize column names
            df.columns = [str(c).strip().lower().replace(" ", "_").replace("-", "_") for c in df.columns]
            
            # Map possible column header variations
            branch_col = next((c for c in df.columns if "branch" in c), None)
            dept_col = next((c for c in df.columns if "dept" in c or "department" in c), None)
            doc_col = next((c for c in df.columns if "doc" in c or "doctor" in c), None)
            dis_col = next((c for c in df.columns if "dis" in c or "disease" in c or "condition" in c), None)

            # ---------------------------------------------------------
            # High-Performance In-Memory Pre-fetching & Caching (O(1))
            # ---------------------------------------------------------
            from django.db import transaction

            # Pre-load existing hospital masters to prevent thousands of DB queries
            existing_branches = {b.name.strip().lower(): b for b in HospitalBranch.objects.filter(hospital=hospital)}
            existing_depts = {d.name.strip().lower(): d for d in HospitalDepartment.objects.filter(hospital=hospital).prefetch_related("branches")}
            existing_docs = {doc.name.strip().lower(): doc for doc in HospitalDoctor.objects.filter(hospital=hospital).prefetch_related("departments", "associated_diseases")}
            existing_diseases = {(dis.department_id, dis.name.strip().lower()): dis for dis in HospitalDisease.objects.filter(hospital=hospital)}
            existing_availabilities = {(a.doctor_id, a.branch_id) for a in DoctorBranchAvailability.objects.filter(doctor__hospital=hospital)}

            # Track in-memory relations to avoid duplicate M2M operations
            dept_branch_links = {(d.id, b.id) for d in existing_depts.values() for b in d.branches.all()}
            doc_dept_links = {(doc.id, dept.id) for doc in existing_docs.values() for dept in doc.departments.all()}
            doc_disease_links = {(doc.id, dis.id) for doc in existing_docs.values() for dis in doc.associated_diseases.all()}

            branches_created = 0
            depts_created = 0
            docs_created = 0
            diseases_created = 0
            existing_skipped = 0
            rows_processed = 0

            with transaction.atomic():
                # Convert dataframe to simple Python dicts (much faster than df.iterrows())
                records = df.to_dict(orient="records")

                for row in records:
                    raw_branch = str(row.get(branch_col, "")).strip() if branch_col and pd.notna(row.get(branch_col)) else ""
                    raw_dept = str(row.get(dept_col, "")).strip() if dept_col and pd.notna(row.get(dept_col)) else ""
                    raw_doc = str(row.get(doc_col, "")).strip() if doc_col and pd.notna(row.get(doc_col)) else ""
                    raw_disease = str(row.get(dis_col, "")).strip() if dis_col and pd.notna(row.get(dis_col)) else ""

                    if not raw_dept and not raw_doc and not raw_disease and not raw_branch:
                        continue

                    rows_processed += 1
                    branch_obj = None
                    dept_obj = None
                    doc_obj = None
                    disease_obj = None

                    # 1. Branch
                    if raw_branch and raw_branch.lower() not in ["nan", "none", ""]:
                        b_key = raw_branch.lower()
                        if b_key in existing_branches:
                            branch_obj = existing_branches[b_key]
                        else:
                            branch_obj = HospitalBranch.objects.create(
                                hospital=hospital,
                                name=raw_branch,
                                is_active=True
                            )
                            existing_branches[b_key] = branch_obj
                            branches_created += 1

                    # 2. Department
                    if raw_dept and raw_dept.lower() not in ["nan", "none", ""]:
                        d_key = raw_dept.lower()
                        if d_key in existing_depts:
                            dept_obj = existing_depts[d_key]
                        else:
                            dept_obj = HospitalDepartment.objects.create(
                                hospital=hospital,
                                name=raw_dept,
                                is_active=True
                            )
                            existing_depts[d_key] = dept_obj
                            depts_created += 1

                        # Link branch to department if not already linked
                        if branch_obj and dept_obj:
                            link_key = (dept_obj.id, branch_obj.id)
                            if link_key not in dept_branch_links:
                                dept_obj.branches.add(branch_obj)
                                dept_branch_links.add(link_key)

                    # 3. Doctor
                    if raw_doc and raw_doc.lower() not in ["nan", "none", ""]:
                        clean_doc_name = raw_doc
                        if clean_doc_name.lower().startswith("dr.") or clean_doc_name.lower().startswith("dr "):
                            clean_doc_name = clean_doc_name[3:].strip()

                        doc_key = clean_doc_name.lower()
                        if doc_key in existing_docs:
                            doc_obj = existing_docs[doc_key]
                            existing_skipped += 1
                        else:
                            doc_obj = HospitalDoctor.objects.create(
                                hospital=hospital,
                                name=clean_doc_name,
                                department=dept_obj,
                                is_active=True
                            )
                            existing_docs[doc_key] = doc_obj
                            docs_created += 1

                        # Link department to doctor
                        if dept_obj:
                            doc_dept_key = (doc_obj.id, dept_obj.id)
                            if doc_dept_key not in doc_dept_links:
                                doc_obj.departments.add(dept_obj)
                                doc_dept_links.add(doc_dept_key)
                            if not doc_obj.department:
                                doc_obj.department = dept_obj
                                doc_obj.save(update_fields=["department"])

                        # Link branch availability
                        if branch_obj:
                            avail_key = (doc_obj.id, branch_obj.id)
                            if avail_key not in existing_availabilities:
                                DoctorBranchAvailability.objects.create(
                                    doctor=doc_obj,
                                    branch=branch_obj,
                                    is_active=True
                                )
                                existing_availabilities.add(avail_key)

                    # 4. Disease / Condition
                    if raw_disease and raw_disease.lower() not in ["nan", "none", ""] and dept_obj:
                        dis_key = (dept_obj.id, raw_disease.lower())
                        if dis_key in existing_diseases:
                            disease_obj = existing_diseases[dis_key]
                        else:
                            disease_obj = HospitalDisease.objects.create(
                                hospital=hospital,
                                department=dept_obj,
                                name=raw_disease,
                                is_active=True
                            )
                            existing_diseases[dis_key] = disease_obj
                            diseases_created += 1

                        # Link disease to doctor
                        if doc_obj and disease_obj:
                            doc_dis_key = (doc_obj.id, disease_obj.id)
                            if doc_dis_key not in doc_disease_links:
                                doc_obj.associated_diseases.add(disease_obj)
                                doc_disease_links.add(doc_dis_key)

            messages.success(
                request,
                f"Excel Import Completed Successfully! Processed {rows_processed} rows in milliseconds. "
                f"Added: {branches_created} branches, {depts_created} departments, "
                f"{docs_created} doctors, {diseases_created} diseases/conditions."
            )
        except Exception as ex:
            messages.error(request, f"Failed to import Excel/CSV file: {str(ex)}")

    return redirect(f"/leads/hospital-configuration/{biz_param}")


@login_required
def hospital_master_sample_download(request):
    """Download Sample Excel / CSV template for Hospital Master Configuration"""
    from django.http import HttpResponse
    fmt = request.GET.get("format", "csv").lower()
    hospital = _get_target_hospital_for_config(request)
    safe_biz = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in (hospital.name if hospital else "Hospital")).strip('_') or "Hospital"
    
    if fmt in ["xlsx", "excel"]:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Hospital Master Template"
        
        headers = ["hospital_branch", "department", "doctor", "disease"]
        ws.append(headers)
        
        header_fill = PatternFill(start_color="2563EB", end_color="2563EB", fill_type="solid")
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        thin_border = Border(
            left=Side(style='thin', color='E2E8F0'),
            right=Side(style='thin', color='E2E8F0'),
            top=Side(style='thin', color='E2E8F0'),
            bottom=Side(style='thin', color='E2E8F0')
        )
        
        for col_num, cell in enumerate(ws[1], 1):
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = thin_border
            
        sample_rows = [
            ["Hospital Branch 1", "Neurology", "Dr. Raj Sharma", "Migraine"],
            ["Hospital Branch 1", "Neurology", "Dr. Raj Sharma", "Stroke"],
            ["Hospital Branch 1", "Neurology", "Dr. Raj Sharma", "Dementia"],
            ["Hospital Branch 2", "Gynaecology", "Dr. Priya Sharma", "Infertility / IVF"],
            ["Hospital Branch 2", "Cardiology", "Dr. Amit Patel", "Hypertension"],
        ]
        
        for r_idx, row in enumerate(sample_rows, 2):
            ws.append(row)
            for cell in ws[r_idx]:
                cell.border = thin_border
                cell.alignment = Alignment(vertical="center")

        ws.column_dimensions['A'].width = 38
        ws.column_dimensions['B'].width = 24
        ws.column_dimensions['C'].width = 24
        ws.column_dimensions['D'].width = 28
        
        response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        response["Content-Disposition"] = f'attachment; filename="{safe_biz}_Configuration_Template.xlsx"'
        wb.save(response)
        return response

    import csv
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{safe_biz}_Configuration_Template.csv"'

    writer = csv.writer(response)
    writer.writerow(["hospital_branch", "department", "doctor", "disease"])
    writer.writerow(["Hospital Branch 1", "Neurology", "Dr. Raj Sharma", "Migraine"])
    writer.writerow(["Hospital Branch 1", "Neurology", "Dr. Raj Sharma", "Stroke"])
    writer.writerow(["Hospital Branch 1", "Neurology", "Dr. Raj Sharma", "Dementia"])
    writer.writerow(["Hospital Branch 2", "Gynaecology", "Dr. Priya Sharma", "Infertility / IVF"])
    writer.writerow(["Hospital Branch 2", "Cardiology", "Dr. Amit Patel", "Hypertension"])

    return response


@login_required
def export_hospital_config_excel(request):
    """
    Export full Hospital Configuration (Branches, Departments, Doctors, Diseases)
    into a beautifully formatted Excel (.xlsx) file with cascading mapping sheets.
    """
    from django.http import HttpResponse
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    
    hospital = _get_target_hospital_for_config(request)
    h_filter = {"hospital": hospital} if hospital else {}
    hospital_name = hospital.name if hospital else "All Hospitals"
    safe_biz = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in hospital_name).strip('_') or "Hospital"

    branches = HospitalBranch.objects.filter(**h_filter).order_by("order", "name")
    departments = HospitalDepartment.objects.filter(**h_filter).order_by("order", "name")
    doctors = HospitalDoctor.objects.filter(**h_filter).order_by("order", "name")
    diseases = HospitalDisease.objects.filter(**h_filter).order_by("department__name", "order", "name")

    wb = openpyxl.Workbook()
    
    header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    sec_fill = PatternFill(start_color="0284C7", end_color="0284C7", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    thin_border = Border(
        left=Side(style='thin', color='CBD5E1'),
        right=Side(style='thin', color='CBD5E1'),
        top=Side(style='thin', color='CBD5E1'),
        bottom=Side(style='thin', color='CBD5E1')
    )

    # ----------------------------------------------------
    # Sheet 1: Master Mapping (Import-Compatible Table)
    # ----------------------------------------------------
    ws1 = wb.active
    ws1.title = "Hospital Master Mapping"
    
    ws1.append(["hospital_branch", "department", "doctor", "disease"])
    for cell in ws1[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border
    
    row_count = 1
    # Build complete linked rows
    for dept in departments:
        dept_branches = list(dept.branches.all()) or [None]
        dept_diseases = list(dept.diseases.all()) or [None]
        dept_doctors = list(dept.doctors.all()) or [None]

        for b in dept_branches:
            b_name = b.name if b else ""
            for doc in dept_doctors:
                doc_name = f"Dr. {doc.name}" if doc else ""
                # Get diseases linked to this doctor or department
                doc_diseases = list(doc.associated_diseases.filter(department=dept)) if doc else []
                applicable_diseases = doc_diseases if doc_diseases else dept_diseases

                for dis in applicable_diseases:
                    dis_name = dis.name if dis else ""
                    row_count += 1
                    ws1.append([b_name, dept.name, doc_name, dis_name])
                    for cell in ws1[row_count]:
                        cell.border = thin_border
                        cell.alignment = Alignment(vertical="center")

    if row_count == 1:
        # Fallback if no deep mappings configured yet: export each master directly
        for b in branches:
            row_count += 1
            ws1.append([b.name, "", "", ""])
            for cell in ws1[row_count]:
                cell.border = thin_border

    ws1.column_dimensions['A'].width = 38
    ws1.column_dimensions['B'].width = 24
    ws1.column_dimensions['C'].width = 26
    ws1.column_dimensions['D'].width = 30

    # ----------------------------------------------------
    # Sheet 2: Branches Directory
    # ----------------------------------------------------
    ws2 = wb.create_sheet(title="Branches")
    ws2.append(["Branch Name", "Code", "City", "Address", "Contact Number", "Status"])
    for cell in ws2[1]:
        cell.fill = sec_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    for r_idx, b in enumerate(branches, 2):
        ws2.append([
            b.name,
            b.code or "",
            b.city or "",
            b.address or "",
            b.contact_number or "",
            "Active" if b.is_active else "Inactive"
        ])
        for cell in ws2[r_idx]:
            cell.border = thin_border
            cell.alignment = Alignment(vertical="center")

    ws2.column_dimensions['A'].width = 36
    ws2.column_dimensions['B'].width = 16
    ws2.column_dimensions['C'].width = 18
    ws2.column_dimensions['D'].width = 40
    ws2.column_dimensions['E'].width = 20
    ws2.column_dimensions['F'].width = 14

    # ----------------------------------------------------
    # Sheet 3: Departments Directory
    # ----------------------------------------------------
    ws3 = wb.create_sheet(title="Departments")
    ws3.append(["Department Name", "Code", "Associated Branches", "Status"])
    for cell in ws3[1]:
        cell.fill = sec_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    for r_idx, d in enumerate(departments, 2):
        branch_names = ", ".join([b.name for b in d.branches.all()])
        ws3.append([
            d.name,
            d.code or "",
            branch_names or "All Branches",
            "Active" if d.is_active else "Inactive"
        ])
        for cell in ws3[r_idx]:
            cell.border = thin_border
            cell.alignment = Alignment(vertical="center")

    ws3.column_dimensions['A'].width = 28
    ws3.column_dimensions['B'].width = 16
    ws3.column_dimensions['C'].width = 45
    ws3.column_dimensions['D'].width = 14

    # ----------------------------------------------------
    # Sheet 4: Doctors Directory
    # ----------------------------------------------------
    ws4 = wb.create_sheet(title="Doctors")
    ws4.append(["Doctor Name", "User Account", "Department(s)", "Specialization", "Fee (Rs.)", "Branches", "Status"])
    for cell in ws4[1]:
        cell.fill = sec_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    for r_idx, doc in enumerate(doctors, 2):
        dept_str = ", ".join([dept.name for dept in doc.departments.all()]) or (doc.department.name if doc.department else "")
        branch_str = ", ".join([b.name for b in doc.branches.all()])
        ws4.append([
            f"Dr. {doc.name}",
            doc.user.username if doc.user else "Not Linked",
            dept_str,
            doc.specialization or doc.qualification or "",
            float(doc.consultation_fee or 0),
            branch_str or "All Branches",
            "Active" if doc.is_active else "Inactive"
        ])
        for cell in ws4[r_idx]:
            cell.border = thin_border
            cell.alignment = Alignment(vertical="center")

    ws4.column_dimensions['A'].width = 26
    ws4.column_dimensions['B'].width = 18
    ws4.column_dimensions['C'].width = 30
    ws4.column_dimensions['D'].width = 26
    ws4.column_dimensions['E'].width = 14
    ws4.column_dimensions['F'].width = 35
    ws4.column_dimensions['G'].width = 14

    # ----------------------------------------------------
    # Sheet 5: Diseases Directory
    # ----------------------------------------------------
    ws5 = wb.create_sheet(title="Diseases & Conditions")
    ws5.append(["Disease / Condition", "Department", "Code", "Status"])
    for cell in ws5[1]:
        cell.fill = sec_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    for r_idx, dis in enumerate(diseases, 2):
        ws5.append([
            dis.name,
            dis.department.name if dis.department else "",
            dis.code or "",
            "Active" if dis.is_active else "Inactive"
        ])
        for cell in ws5[r_idx]:
            cell.border = thin_border
            cell.alignment = Alignment(vertical="center")

    ws5.column_dimensions['A'].width = 32
    ws5.column_dimensions['B'].width = 26
    ws5.column_dimensions['C'].width = 16
    ws5.column_dimensions['D'].width = 14

    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    filename = f"{safe_biz}_Configuration_{timezone.now().strftime('%Y%m%d_%H%M')}.xlsx"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


@login_required
def export_hospital_config_pdf(request):
    """
    Renders high-quality printable PDF view of all Hospital Configurations
    (Branches, Departments, Doctors, and Disease Mappings).
    """
    hospital = _get_target_hospital_for_config(request)
    h_filter = {"hospital": hospital} if hospital else {}
    hospital_name = hospital.name if hospital else "All Hospitals"

    branches = HospitalBranch.objects.filter(**h_filter).order_by("order", "name")
    departments = HospitalDepartment.objects.filter(**h_filter).order_by("order", "name")
    doctors = HospitalDoctor.objects.filter(**h_filter).prefetch_related("departments", "branches", "associated_diseases").order_by("order", "name")
    diseases = HospitalDisease.objects.filter(**h_filter).select_related("department").order_by("department__name", "order", "name")

    # Build master mapping rows
    mapping_rows = []
    for dept in departments:
        dept_branches = list(dept.branches.all()) or [None]
        dept_diseases = list(dept.diseases.all()) or [None]
        dept_doctors = list(dept.doctors.all()) or [None]

        for b in dept_branches:
            b_name = b.name if b else "-"
            for doc in dept_doctors:
                doc_name = f"Dr. {doc.name}" if doc else "-"
                doc_diseases = list(doc.associated_diseases.filter(department=dept)) if doc else []
                applicable_diseases = doc_diseases if doc_diseases else dept_diseases

                for dis in applicable_diseases:
                    dis_name = dis.name if dis else "-"
                    mapping_rows.append({
                        "branch": b_name,
                        "department": dept.name,
                        "doctor": doc_name,
                        "disease": dis_name
                    })

    if not mapping_rows:
        for b in branches:
            mapping_rows.append({
                "branch": b.name,
                "department": "-",
                "doctor": "-",
                "disease": "-"
            })

    context = {
        "hospital_name": hospital_name,
        "branches": branches,
        "departments": departments,
        "doctors": doctors,
        "diseases": diseases,
        "mapping_rows": mapping_rows,
        "now": timezone.now(),
    }
    return render(request, "hospital/leads/config_print_pdf.html", context)


@login_required
def cascading_hospital_data_api(request):
    """
    High-Performance JSON API for dynamic cascading dependent dropdowns:
    Branch -> Department -> Doctor / Disease
    """
    hospital = _get_target_hospital_for_config(request)
    if not hospital:
        return JsonResponse({"error": "No hospital context"}, status=400)

    branch_id = request.GET.get("branch_id")
    dept_id = request.GET.get("department_id")
    doctor_id = request.GET.get("doctor_id")

    res = {
        "departments": [],
        "doctors": [],
        "diseases": [],
    }

    # 1. If branch selected, filter departments available at this branch
    if branch_id:
        if str(branch_id).isdigit():
            branch = HospitalBranch.objects.filter(pk=branch_id, hospital=hospital, is_active=True).first()
        else:
            branch = HospitalBranch.objects.filter(name__iexact=branch_id, hospital=hospital, is_active=True).first()
            if not branch:
                branch = HospitalBranch.objects.filter(name__icontains=branch_id, hospital=hospital, is_active=True).first()

        if branch:
            dept_qs = branch.departments.filter(is_active=True).order_by("order", "name")
            depts = [{"id": d.id, "name": d.name} for d in dept_qs]
            # Ensure 'Other' department is always present
            if not any(d["name"].lower() == "other" for d in depts):
                other_dept, _ = HospitalDepartment.objects.get_or_create(hospital=hospital, name="Other", defaults={"order": 9999, "is_active": True})
                branch.departments.add(other_dept)
                depts.append({"id": other_dept.id, "name": other_dept.name})
            res["departments"] = depts
    else:
        dept_qs = HospitalDepartment.objects.filter(hospital=hospital, is_active=True).order_by("order", "name")
        depts = [{"id": d.id, "name": d.name} for d in dept_qs]
        if not any(d["name"].lower() == "other" for d in depts):
            other_dept, _ = HospitalDepartment.objects.get_or_create(hospital=hospital, name="Other", defaults={"order": 9999, "is_active": True})
            depts.append({"id": other_dept.id, "name": other_dept.name})
        res["departments"] = depts

    # 2. If department selected, filter doctors and diseases for this department
    if dept_id:
        if str(dept_id).isdigit():
            dept = HospitalDepartment.objects.filter(pk=dept_id, hospital=hospital, is_active=True).first()
        else:
            dept = HospitalDepartment.objects.filter(name__iexact=dept_id, hospital=hospital, is_active=True).first()
            
        if dept:
            doc_qs = HospitalDoctor.objects.filter(
                models.Q(departments=dept) | models.Q(department=dept),
                hospital=hospital,
                is_active=True
            ).filter(models.Q(user__isnull=True) | models.Q(user__is_active=True)).distinct().order_by("order", "name")

            # If 'Other' department and no specific doctor assigned, fallback to all active hospital doctors
            if not doc_qs.exists() and dept.name.lower() == "other":
                doc_qs = HospitalDoctor.objects.filter(hospital=hospital, is_active=True).filter(models.Q(user__isnull=True) | models.Q(user__is_active=True)).distinct().order_by("order", "name")
            
            if branch_id and str(branch_id).isdigit():
                doc_qs = doc_qs.filter(branches__id=branch_id)
            res["doctors"] = [{"id": doc.id, "name": doc.name, "display_name": f"Dr. {doc.name}" if not doc.name.lower().startswith("dr") else doc.name, "fee": float(doc.consultation_fee)} for doc in doc_qs]

            dis_qs = dept.diseases.filter(is_active=True).order_by("order", "name")
            diseases = [{"id": dis.id, "name": dis.name} for dis in dis_qs]
            # Ensure 'Other' disease is always present for each department
            if not any(dis["name"].lower() == "other" for dis in diseases):
                other_dis, _ = HospitalDisease.objects.get_or_create(hospital=hospital, department=dept, name="Other", defaults={"order": 9999, "is_active": True})
                diseases.append({"id": other_dis.id, "name": other_dis.name})
            res["diseases"] = diseases

    # 3. If doctor selected, return doctor's available branches and diseases
    if doctor_id:
        doc = HospitalDoctor.objects.filter(pk=doctor_id, hospital=hospital, is_active=True).first()
        if doc:
            res["diseases"] = [{"id": dis.id, "name": dis.name} for dis in doc.associated_diseases.filter(is_active=True)]
            res["branches"] = [{"id": b.id, "name": b.name} for b in doc.branches.filter(is_active=True)]

    return JsonResponse(res)


@login_required
def appointments_done_list(request):
    """
    Appointments Done / Conversion Page for Healthcare / Nelson Hospital.
    Shows booked & confirmed appointment leads with dynamic KPI calculations:
    - Total Leads, Appointment Done, Conversion Rate (%), Payment Done, Payment Pending
    - Filters: Date Range, Department, Telecaller/Attendant, Doctor, Source, Campaign, Search
    - Scoped by business selection: If an Academy business is active, returns 0 counts.
    """
    is_global_admin = request.user.is_superuser or request.user.role == User.Role.SUPER_ADMIN

    # Active business determination
    selected_hospital_id = (
        request.GET.get("business", "").strip()
        or request.GET.get("hospital", "").strip()
        or str(request.session.get("active_business_id", "")).strip()
    )

    target_hospital = None
    if request.user.hospital:
        target_hospital = request.user.hospital
    elif selected_hospital_id and selected_hospital_id.isdigit():
        target_hospital = Hospital.objects.filter(id=int(selected_hospital_id), is_active=True).first()

    # If an academy or non-hospital business is explicitly selected, appointment counts are 0
    is_healthcare_scope = True
    if target_hospital:
        is_healthcare_scope = (target_hospital.industry == Hospital.Industry.HOSPITAL)

    # Base Appointment / Healthcare lead queryset
    if not is_healthcare_scope:
        base_qs = Lead.objects.none()
    else:
        base_qs = Lead.objects.select_related(
            "hospital", "assigned_to", "stage", "campaign", "lead_source"
        ).filter(is_archived=False)

        if target_hospital:
            base_qs = base_qs.filter(hospital=target_hospital)
        elif request.user.hospital:
            base_qs = base_qs.filter(hospital=request.user.hospital)
        elif not is_global_admin:
            base_qs = base_qs.filter(hospital__isnull=True)

        if request.user.role == User.Role.MANAGER:
            mgr_branches = list(request.user.assigned_branches.filter(is_active=True)) if hasattr(request.user, 'assigned_branches') and request.user.assigned_branches.exists() else []
            if not mgr_branches and request.user.branch:
                mgr_branches = [request.user.branch]

            if mgr_branches:
                mgr_b_names = [b.name for b in mgr_branches]
                branch_team = User.objects.filter(
                    hospital=request.user.hospital
                ).filter(
                    Q(assigned_branches__in=mgr_branches) |
                    Q(branch__in=mgr_branches)
                ).distinct()

                lead_branch_q = (
                    Q(custom_data__hospital_branch__isnull=True, custom_data__branch__isnull=True) |
                    Q(custom_data__hospital_branch="") |
                    Q(custom_data__branch="")
                )
                for b_name in mgr_b_names:
                    lead_branch_q |= (
                        Q(custom_data__hospital_branch__iexact=b_name) |
                        Q(custom_data__branch__iexact=b_name) |
                        Q(custom_data__dyn_hospital_branch__iexact=b_name) |
                        Q(custom_data__dyn_branch__iexact=b_name)
                    )
                lead_branch_q |= (
                    Q(assigned_to__in=branch_team) |
                    Q(created_by__in=branch_team)
                )
                base_qs = base_qs.filter(lead_branch_q).distinct()
        elif request.user.role == User.Role.DOCTOR:
            doc_name = (request.user.get_full_name() or request.user.username).strip()
            base_qs = base_qs.filter(
                Q(assigned_to=request.user) |
                Q(appointments__doctor_user=request.user) |
                Q(appointments__doctor_name__icontains=doc_name) |
                Q(custom_data__doctor__icontains=doc_name)
            ).distinct()

    # Filter by OPD booking / appointment history criteria
    # Ensures all leads that reached OPD Booking are included, even if subsequent status became Visited, Payment Done, Cancelled, etc.
    appt_condition = (
        Q(custom_data__appo_booked_date__isnull=False)
        | Q(custom_data__appo_booked_date__gt="")
        | Q(appointments__isnull=False)
        | Q(custom_data__appointment_status__icontains="book")
        | Q(custom_data__appointment_status__icontains="confirm")
        | Q(custom_data__appointment_status__icontains="approv")
        | Q(custom_data__appointment_status__icontains="visit")
        | Q(custom_data__appointment_status__icontains="done")
        | Q(custom_data__appo_book__icontains="yes")
        | Q(custom_data__appo_book__icontains="book")
        | Q(deal_status__in=[DealStatus.WON, "BOOKING CONFIRMED", "PAYMENT DONE", "VISITED"])
        | Q(custom_data__deal_status__icontains="book")
        | Q(custom_data__deal_status__icontains="won")
        | Q(custom_data__deal_status__icontains="payment")
        | Q(custom_data__opd_bill__gt=0)
        | Q(custom_data__total__gt=0)
    )

    leads_qs = base_qs.filter(appt_condition).distinct()

    # Filter dropdown choices options
    if target_hospital:
        departments_list = list(HospitalDepartment.objects.filter(hospital=target_hospital, is_active=True).values_list("name", flat=True))
        if not departments_list:
            departments_list = list(MasterGroup.get_active_choices("Departments").filter(hospital=target_hospital).values_list("name", flat=True))
        doctors_list = list(HospitalDoctor.objects.filter(hospital=target_hospital, is_active=True).values_list("name", flat=True))
        if not doctors_list:
            doctors_list = list(MasterGroup.get_active_choices("Doctors").filter(hospital=target_hospital).values_list("name", flat=True))
        
        # Merge historical lead doctors into analytics filter list
        hist_docs = list(base_qs.exclude(custom_data__doctor="").values_list("custom_data__doctor", flat=True).distinct()) + list(base_qs.exclude(appointments__doctor_name="").values_list("appointments__doctor_name", flat=True).distinct())
        seen_d = set(d.lower() for d in doctors_list if d)
        for d in hist_docs:
            if d and str(d).strip() and str(d).strip().lower() not in seen_d:
                seen_d.add(str(d).strip().lower())
                doctors_list.append(str(d).strip())

        telecallers_qs = User.objects.filter(
            hospital=target_hospital,
            role__in=[User.Role.LEAD_ATTENDENT, User.Role.COUNSELLOR, User.Role.HR, User.Role.MANAGER],
            is_active=True
        ).order_by("first_name", "last_name", "username")
        campaigns_list = list(MasterGroup.get_active_choices("Campaigns").filter(hospital=target_hospital).values_list("name", flat=True))
        sources_list = list(MasterGroup.get_active_choices("Lead Sources").filter(hospital=target_hospital).values_list("name", flat=True))
    else:
        departments_list = list(HospitalDepartment.objects.filter(is_active=True).values_list("name", flat=True).distinct())
        if not departments_list:
            departments_list = list(MasterGroup.get_active_choices("Departments").values_list("name", flat=True).distinct())
        doctors_list = list(HospitalDoctor.objects.filter(is_active=True).values_list("name", flat=True).distinct())
        if not doctors_list:
            doctors_list = list(MasterGroup.get_active_choices("Doctors").values_list("name", flat=True).distinct())

        hist_docs = list(base_qs.exclude(custom_data__doctor="").values_list("custom_data__doctor", flat=True).distinct()) + list(base_qs.exclude(appointments__doctor_name="").values_list("appointments__doctor_name", flat=True).distinct())
        seen_d = set(d.lower() for d in doctors_list if d)
        for d in hist_docs:
            if d and str(d).strip() and str(d).strip().lower() not in seen_d:
                seen_d.add(str(d).strip().lower())
                doctors_list.append(str(d).strip())
        telecallers_qs = User.objects.filter(
            role__in=[User.Role.LEAD_ATTENDENT, User.Role.COUNSELLOR, User.Role.HR, User.Role.MANAGER],
            is_active=True
        ).order_by("first_name", "last_name", "username")
        campaigns_list = list(MasterGroup.get_active_choices("Campaigns").values_list("name", flat=True).distinct())
        sources_list = list(MasterGroup.get_active_choices("Lead Sources").values_list("name", flat=True).distinct())

    # Apply Header Filters
    search_q = request.GET.get("q", "").strip()
    date_from = request.GET.get("date_from", "").strip()
    date_to = request.GET.get("date_to", "").strip()
    sel_dept = request.GET.get("department", "").strip()
    sel_telecaller = request.GET.get("telecaller", "").strip()
    sel_doctor = request.GET.get("doctor", "").strip()
    sel_source = request.GET.get("lead_source", "").strip()
    sel_campaign = request.GET.get("campaign", "").strip()

    # Base filtered queryset across all leads of this business / healthcare industry
    filtered_all_leads_qs = base_qs

    if search_q:
        filtered_all_leads_qs = filtered_all_leads_qs.filter(
            Q(name__icontains=search_q) | Q(mobile__icontains=search_q) | Q(city__icontains=search_q)
        )
        leads_qs = leads_qs.filter(
            Q(name__icontains=search_q) | Q(mobile__icontains=search_q) | Q(city__icontains=search_q)
        )
    if date_from:
        filtered_all_leads_qs = filtered_all_leads_qs.filter(
            Q(inquiry_date__gte=date_from) | Q(custom_data__appo_booked_date__gte=date_from)
        )
        leads_qs = leads_qs.filter(
            Q(inquiry_date__gte=date_from) | Q(custom_data__appo_booked_date__gte=date_from)
        )
    if date_to:
        filtered_all_leads_qs = filtered_all_leads_qs.filter(
            Q(inquiry_date__lte=date_to) | Q(custom_data__appo_booked_date__lte=date_to)
        )
        leads_qs = leads_qs.filter(
            Q(inquiry_date__lte=date_to) | Q(custom_data__appo_booked_date__lte=date_to)
        )
    if sel_dept:
        filtered_all_leads_qs = filtered_all_leads_qs.filter(custom_data__department__iexact=sel_dept)
        leads_qs = leads_qs.filter(custom_data__department__iexact=sel_dept)
    if sel_telecaller:
        if sel_telecaller.isdigit():
            filtered_all_leads_qs = filtered_all_leads_qs.filter(assigned_to_id=int(sel_telecaller))
            leads_qs = leads_qs.filter(assigned_to_id=int(sel_telecaller))
        else:
            filtered_all_leads_qs = filtered_all_leads_qs.filter(
                Q(assigned_to__username__iexact=sel_telecaller)
                | Q(assigned_to__first_name__icontains=sel_telecaller)
            )
            leads_qs = leads_qs.filter(
                Q(assigned_to__username__iexact=sel_telecaller)
                | Q(assigned_to__first_name__icontains=sel_telecaller)
            )
    if sel_doctor:
        filtered_all_leads_qs = filtered_all_leads_qs.filter(
            Q(custom_data__doctor__icontains=sel_doctor)
            | Q(appointments__doctor_name__icontains=sel_doctor)
        )
        leads_qs = leads_qs.filter(
            Q(custom_data__doctor__icontains=sel_doctor)
            | Q(appointments__doctor_name__icontains=sel_doctor)
        )
    if sel_source:
        filtered_all_leads_qs = filtered_all_leads_qs.filter(
            Q(lead_source__name__iexact=sel_source)
            | Q(custom_data__lead_source__iexact=sel_source)
            | Q(raw_source_metadata__lead_source__iexact=sel_source)
            | Q(raw_source_metadata__LEAD_SOURCE__iexact=sel_source)
        )
        leads_qs = leads_qs.filter(
            Q(lead_source__name__iexact=sel_source)
            | Q(custom_data__lead_source__iexact=sel_source)
            | Q(raw_source_metadata__lead_source__iexact=sel_source)
            | Q(raw_source_metadata__LEAD_SOURCE__iexact=sel_source)
        )
    if sel_campaign:
        filtered_all_leads_qs = filtered_all_leads_qs.filter(
            Q(campaign__name__iexact=sel_campaign)
            | Q(custom_data__campaign__iexact=sel_campaign)
            | Q(raw_source_metadata__campaign_name__iexact=sel_campaign)
            | Q(raw_source_metadata__CAMPAIGN_NAME__iexact=sel_campaign)
        )
        leads_qs = leads_qs.filter(
            Q(campaign__name__iexact=sel_campaign)
            | Q(custom_data__campaign__iexact=sel_campaign)
            | Q(raw_source_metadata__campaign_name__iexact=sel_campaign)
            | Q(raw_source_metadata__CAMPAIGN_NAME__iexact=sel_campaign)
        )

    # Dynamic KPI Stats calculation:
    # 1. Total Leads: all leads in the active business / healthcare scope with applied filters
    total_leads_count = filtered_all_leads_qs.count()
    
    # 2. Appointments Done, Approval Pending, Upcoming Appointments calculated on appointment-booked leads
    leads_list = list(leads_qs)
    
    appointment_done_count = len(leads_list)
    approval_pending_count = 0
    upcoming_appointments_count = 0
    today_date = timezone.localdate()

    for l in leads_list:
        st = (l.display_status or "").lower()
        cd = l.custom_data or {}
        apt_st = str(cd.get("appointment_status") or "").lower()
        
        # Approval Pending check
        if (
            "approv" in st
            or "approv" in apt_st
            or "await" in apt_st
            or (l.stage and "approv" in (l.stage.name or "").lower())
        ):
            approval_pending_count += 1

        # Upcoming Appointments check (scheduled for today or future date)
        booked_date_val = cd.get("appo_booked_date")
        if booked_date_val:
            try:
                if isinstance(booked_date_val, str) and len(booked_date_val) >= 10:
                    d_obj = datetime.strptime(booked_date_val[:10], "%Y-%m-%d").date()
                    if d_obj >= today_date and "cancel" not in apt_st and "lost" not in st:
                        upcoming_appointments_count += 1
                elif hasattr(booked_date_val, 'year') and booked_date_val >= today_date:
                    upcoming_appointments_count += 1
            except Exception:
                pass

    # Conversion Rate (%) = (Total Appointments Done / Total Leads) * 100
    if total_leads_count > 0:
        conversion_rate = round((appointment_done_count / total_leads_count) * 100, 1)
    else:
        conversion_rate = 0.0

    # Sort and paginate
    leads_list.sort(key=lambda x: x.inquiry_date or timezone.localdate(), reverse=True)
    
    from django.core.paginator import Paginator
    paginator = Paginator(leads_list, 25)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # Build query string for pagination links
    query_params_dict = request.GET.copy()
    if "page" in query_params_dict:
        del query_params_dict["page"]
    query_params = query_params_dict.urlencode()

    # Campaign breakdown calculation for Appointments Done:
    # Compute over appointment leads matching date/search/doctor filters but excluding the specific campaign filter
    # so user can still see and switch between all available campaigns in the dropdown
    appts_for_camp_breakdown = base_qs.filter(appt_condition).distinct()
    if search_q:
        appts_for_camp_breakdown = appts_for_camp_breakdown.filter(
            Q(name__icontains=search_q) | Q(mobile__icontains=search_q) | Q(city__icontains=search_q)
        )
    if date_from:
        appts_for_camp_breakdown = appts_for_camp_breakdown.filter(
            Q(inquiry_date__gte=date_from) | Q(custom_data__appo_booked_date__gte=date_from)
        )
    if date_to:
        appts_for_camp_breakdown = appts_for_camp_breakdown.filter(
            Q(inquiry_date__lte=date_to) | Q(custom_data__appo_booked_date__lte=date_to)
        )
    if sel_dept:
        appts_for_camp_breakdown = appts_for_camp_breakdown.filter(custom_data__department__iexact=sel_dept)
    if sel_telecaller:
        if sel_telecaller.isdigit():
            appts_for_camp_breakdown = appts_for_camp_breakdown.filter(assigned_to_id=int(sel_telecaller))
        else:
            appts_for_camp_breakdown = appts_for_camp_breakdown.filter(
                Q(assigned_to__username__iexact=sel_telecaller)
                | Q(assigned_to__first_name__icontains=sel_telecaller)
            )
    if sel_doctor:
        appts_for_camp_breakdown = appts_for_camp_breakdown.filter(
            Q(custom_data__doctor__icontains=sel_doctor)
            | Q(appointments__doctor_name__icontains=sel_doctor)
        )
    if sel_source:
        appts_for_camp_breakdown = appts_for_camp_breakdown.filter(
            Q(lead_source__name__iexact=sel_source) | Q(custom_data__lead_source__iexact=sel_source)
        )
    campaign_breakdown = get_campaign_breakdown_stats(base_qs, appts_for_camp_breakdown)

    return render(request, "leads/appointments_done_list.html", {
        "active": "appointments",
        "target_hospital": target_hospital,
        "is_healthcare_scope": is_healthcare_scope,
        "page_obj": page_obj,
        "total_count": appointment_done_count,
        "total_leads_count": total_leads_count,
        "appointment_done_count": appointment_done_count,
        "conversion_rate": conversion_rate,
        "approval_pending_count": approval_pending_count,
        "upcoming_appointments_count": upcoming_appointments_count,
        "campaign_breakdown": campaign_breakdown,
        "q": search_q,
        # Filters state
        "search_q": search_q,
        "date_from": date_from,
        "date_to": date_to,
        "date_from_val": date_from or "",
        "date_to_val": date_to or "",
        "sel_dept": sel_dept,
        "sel_telecaller": sel_telecaller,
        "sel_doctor": sel_doctor,
        "sel_source": sel_source,
        "sel_campaign": sel_campaign,
        # Choices
        "departments_list": sorted(list(set(departments_list))),
        "doctors_list": sorted(list(set(doctors_list))),
        "telecallers_qs": telecallers_qs,
        "campaigns_list": sorted(list(set(campaigns_list))),
        "sources_list": sorted(list(set(sources_list))),
        "query_params": query_params,
    })


@login_required
def pending_leads(request):
    """
    Dedicated Pending Leads View for Hospital Admin / CRM.
    Displays leads in:
    - Follow-ups stage (Follow up Needed, Follow up Scheduled, Follow up Pending)
    - Uncontacted leads (Fresh, Open, Call not done)
    - New leads
    - Approval Pending (Awaiting Approval from Doctor, Booking Approval Pending)
    - Payment Pending (Visited / Completed appointment but payment pending)
    """
    pending_condition = (
        # 1. Follow-ups pending/stage
        Q(stage__name__icontains="follow")
        | Q(custom_data__deal_status__icontains="follow")
        | Q(followups__followup_status__in=["PENDING", "RESCHEDULED"])
        | Q(custom_data__followup_status__icontains="pending")
        # 2. Uncontacted / Fresh / Open
        | Q(stage__name__icontains="fresh")
        | Q(stage__name__icontains="open")
        | Q(stage__name__icontains="uncontacted")
        | Q(custom_data__deal_status__icontains="uncontacted")
        # 3. New leads
        | Q(stage__name__icontains="new")
        | Q(custom_data__deal_status__icontains="new")
        # 4. Approval pending
        | Q(stage__name__icontains="approv")
        | Q(custom_data__appointment_status__icontains="approv")
        | Q(custom_data__deal_status__icontains="approv")
        | Q(appointments__status=AppointmentStatus.PENDING_APPROVAL)
        # 5. Payment pending
        | Q(stage__name__icontains="payment pending")
        | Q(custom_data__deal_status__icontains="payment pending")
        | Q(custom_data__appointment_status__icontains="payment pending")
    )

    # Determine target hospital context
    selected_hospital_id = (
        request.GET.get("business", "").strip()
        or request.GET.get("hospital", "").strip()
        or str(request.session.get("active_business_id", "")).strip()
    )
    is_global_admin = request.user.is_superuser or (
        request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital
    )
    target_hospital = request.user.hospital
    if not target_hospital and is_global_admin and selected_hospital_id and selected_hospital_id.isdigit():
        target_hospital = Hospital.objects.filter(id=int(selected_hospital_id)).first()
    elif not target_hospital and is_global_admin:
        biz_id = request.session.get("active_business_id")
        if biz_id and str(biz_id).isdigit():
            target_hospital = Hospital.objects.filter(id=int(biz_id)).first()
        else:
            target_hospital = Hospital.objects.filter(name__icontains="nelson").first() or Hospital.objects.first()

    # Exclude definitively closed/won/lost leads unless they have pending payment/approval
    leads_base = Lead.objects.select_related(
        "course", "stage", "lead_source", "source_category", "campaign", "assigned_to"
    ).filter(is_archived=False)

    if target_hospital:
        leads_base = leads_base.filter(hospital=target_hospital)

    leads_base = leads_base.filter(pending_condition).exclude(
        Q(deal_status=DealStatus.LOST) | Q(stage__name__icontains="lost") | Q(stage__name__icontains="cancel")
    ).distinct()

    # Pass base_qs to get_filtered_leads so common search, date, and sidebar filters apply smoothly
    leads = get_filtered_leads(request, base_qs=leads_base)

    q = request.GET.get("q", "").strip()
    sort_by = request.GET.get("sort", "-created_at")
    date_from = request.GET.get("date_from") or request.GET.get("date")
    date_to = request.GET.get("date_to")

    # Pagination
    from django.core.paginator import Paginator
    paginator = Paginator(leads, 25)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    query_params = request.GET.copy()
    if "page" in query_params:
        del query_params["page"]

    # Calculate campaign breakdown for pending leads tab strictly for target_hospital
    date_scoped_pending = leads_base
    if date_from:
        date_scoped_pending = date_scoped_pending.filter(inquiry_date__gte=date_from)
    if date_to:
        date_scoped_pending = date_scoped_pending.filter(inquiry_date__lte=date_to)
    campaign_breakdown = get_campaign_breakdown_stats(leads_base, date_scoped_pending)

    context = {
        "query_params": query_params.urlencode(),
        "active": "pending_leads",
        "page_obj": page_obj,
        "total_count": leads.count(),
        "campaign_breakdown": campaign_breakdown,
        "q": q,
        "target_hospital": target_hospital,
        "is_viewing_hospital": True,
        "date_from_val": date_from or "",
        "date_to_val": date_to or "",
        "current_sort": sort_by,
        "request_get": request.GET,
    }

    if target_hospital:
        context["employees"] = User.objects.filter(
            hospital=target_hospital,
            role__in=[User.Role.LEAD_ATTENDENT, User.Role.COUNSELLOR, User.Role.HR],
            is_active=True,
            is_approved=True
        ).order_by("first_name", "last_name", "username")
        context["hospital_campaigns"] = MasterGroup.get_active_choices("Campaigns").filter(hospital=target_hospital)
        context["hospital_sources"] = MasterGroup.get_active_choices("Lead Sources").filter(hospital=target_hospital)
        context["hospital_statuses"] = MasterGroup.get_active_choices("Deal Statuses").filter(hospital=target_hospital)
        hospital_deal_statuses = list(context["hospital_statuses"].values_list("name", flat=True))
        if hospital_deal_statuses:
            context["bulk_stages"] = [{"id": s, "name": s} for s in hospital_deal_statuses]
        else:
            context["bulk_stages"] = [{"id": s.id, "name": s.name} for s in LeadStage.objects.filter(is_active=True)]

    return render(request, "hospital/leads/lead_list.html", context)
