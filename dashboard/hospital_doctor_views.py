from django.core.paginator import Paginator
import json
import calendar
from datetime import datetime, date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum, Q, Case, When, Value, IntegerField, Max
from django.db.models.functions import TruncMonth
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone

from leads.models import Lead, LeadSource, SourceCategory, Course, Campaign, LeadStage, Appointment, AppointmentStatus, DealStatus, LeadTemperature, AdmissionStatus
from admissions.models import Admission
from payments.models import Payment, PaymentStatus
from accounts.models import User, Hospital
from dashboard.models import DailyReport, TaskReminder
from notifications.models import Notification
from imports.models import ImportJob
from followups.models import FollowUp
from dashboard.helpers import filter_uncontacted_leads_ids, extract_lead_followup_date, _get_effective_hospital

def doctor_home(request):
    from accounts.models import User
    from leads.models import Appointment, AppointmentStatus, DoctorSchedule, DoctorLeave, Lead
    from notifications.models import Notification
    from datetime import datetime
    
    if request.user.role != User.Role.DOCTOR or not request.user.hospital:
        messages.error(request, "Doctor access required.")
        return redirect("dashboard:home")
        
    doctor = request.user
    today = timezone.localdate()
    
    # Handle actions (Doctor Note/Remark, Schedule, Leave)
    if request.method == "POST":
        action = request.POST.get('action')
        lead_id = request.POST.get('lead_id')
        apt_id = request.POST.get('appointment_id')
        apt = None
        if apt_id and str(apt_id).isdigit():
            apt = Appointment.objects.filter(pk=int(apt_id), hospital=doctor.hospital).first()

        lead = apt.lead if apt else None
        if not lead and lead_id and str(lead_id).isdigit():
            lead = Lead.objects.filter(pk=int(lead_id), hospital=doctor.hospital).first()

        # Doctor Note / Remark Action
        if action in ["add_note", "doctor_remark"] and lead:
            note_text = request.POST.get('doctor_notes', '').strip() or request.POST.get('note', '').strip() or request.POST.get('remark', '').strip()
            if note_text:
                from followups.models import Note, Activity, ActivityType
                doc_name = doctor.get_full_name() or doctor.username
                formatted_note = f"Doctor Note (Dr. {doc_name}): {note_text}"
                
                # Create Note record
                Note.objects.create(
                    lead=lead,
                    created_by=doctor,
                    note=formatted_note
                )
                
                # Create Activity record for timeline
                Activity.objects.create(
                    lead=lead,
                    created_by=doctor,
                    activity_type=ActivityType.NOTE,
                    description=formatted_note
                )
                
                # Save into custom_data
                cd = lead.custom_data or {}
                cd['last_doctor_remark'] = note_text
                cd['doctor_remark'] = note_text
                lead.custom_data = cd
                lead.save(update_fields=['custom_data'])

                # Send Notification to Assigned Telecaller (lead attendant / lead owner)
                if lead.assigned_to and lead.assigned_to != doctor:
                    Notification.objects.create(
                        user=lead.assigned_to,
                        title=f"Doctor Added Clinical Remark: {lead.name}",
                        message=f"Dr. {doc_name} added a note on patient {lead.name} (#{lead.lead_code or lead.pk}): '{note_text}'.",
                        link=f"/leads/{lead.pk}/",
                    )

                # Send Notification to Hospital Admin(s)
                admin_users = User.objects.filter(
                    hospital=doctor.hospital,
                    role__in=[User.Role.ADMIN, User.Role.SUPER_ADMIN],
                    is_active=True
                ).exclude(pk=doctor.pk)
                if lead.assigned_to:
                    admin_users = admin_users.exclude(pk=lead.assigned_to.pk)

                for admin_u in admin_users:
                    Notification.objects.create(
                        user=admin_u,
                        title=f"Doctor Remark Added: {lead.name}",
                        message=f"Dr. {doc_name} added remark on patient {lead.name} (#{lead.lead_code or lead.pk}): '{note_text}'.",
                        link=f"/leads/{lead.pk}/",
                    )

                messages.success(request, f"Note successfully added for {lead.name}. Telecaller & Admin have been notified.")
            else:
                messages.warning(request, "Please enter a valid note or remark.")

        elif action == "update_schedule":
            schedule, _ = DoctorSchedule.objects.get_or_create(doctor=doctor, defaults={"hospital": doctor.hospital})
            schedule.opd_start_time = request.POST.get("opd_start_time", "09:00")
            schedule.opd_end_time = request.POST.get("opd_end_time", "17:00")
            schedule.slot_duration_minutes = int(request.POST.get("slot_duration_minutes", 30))
            schedule.is_available = (request.POST.get("is_available") == "1")
            schedule.off_days = request.POST.get("off_days", "Sunday")
            schedule.save()
            messages.success(request, "OPD Working Hours and schedule updated successfully.")
            return redirect(f"{reverse('dashboard:home')}?tab=calendar&subtab=hours")

        elif action in ["add_leave", "edit_leave"]:
            leave_id = request.POST.get("leave_id")
            start_date_str = request.POST.get("start_date")
            end_date_str = request.POST.get("end_date") or start_date_str
            reason = request.POST.get("reason", "").strip()
            is_full_day = (request.POST.get("is_full_day") == "1")
            start_time = request.POST.get("start_time") or None
            end_time = request.POST.get("end_time") or None

            if not start_date_str:
                messages.error(request, "Start date is required.")
                return redirect(f"{reverse('dashboard:home')}?tab=calendar&subtab=add_leave")

            try:
                def parse_flexible_date(d_str):
                    if not d_str:
                        return None
                    d_str = str(d_str).strip()
                    try:
                        return datetime.strptime(d_str, "%Y-%m-%d").date()
                    except ValueError:
                        pass
                    for fmt in ("%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y"):
                        try:
                            return datetime.strptime(d_str, fmt).date()
                        except ValueError:
                            pass
                    raise ValueError(f"Unknown date format: {d_str}")

                s_date = parse_flexible_date(start_date_str)
                e_date = parse_flexible_date(end_date_str)

                if s_date < today:
                    messages.error(request, "Leave date cannot be in the past.")
                    return redirect(f"{reverse('dashboard:home')}?tab=calendar&subtab=add_leave")
                if e_date < s_date:
                    messages.error(request, "End date cannot be earlier than start date.")
                    return redirect(f"{reverse('dashboard:home')}?tab=calendar&subtab=add_leave")

                # Validation: Cannot add full-day leave for today with past time
                # User must add partial leave with start_time > current time
                now_local = timezone.localtime()
                now_time = now_local.time()
                if s_date == today and is_full_day:
                    messages.error(request, f"Full day leave cannot be added for today at {now_local.strftime('%I:%M %p')}. Please uncheck 'Full Day Leave' and specify a time greater than current time.")
                    return redirect(f"{reverse('dashboard:home')}?tab=calendar&subtab=add_leave")

                if s_date == today and not is_full_day and start_time:
                    try:
                        parsed_st = datetime.strptime(start_time[:5], "%H:%M").time()
                        if parsed_st <= now_time:
                            messages.error(request, f"Leave start time ({parsed_st.strftime('%I:%M %p')}) must be greater than current time ({now_local.strftime('%I:%M %p')}).")
                            return redirect(f"{reverse('dashboard:home')}?tab=calendar&subtab=add_leave")
                    except Exception:
                        pass

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
                        DoctorLeave.objects.filter(doctor=doctor, start_date=s_date, end_date=e_date).exclude(pk=existing_leave.pk).delete()
                        messages.success(request, f"Leave record updated successfully ({s_date.strftime('%d-%m-%Y')} to {e_date.strftime('%d-%m-%Y')}).")
                        return redirect(f"{reverse('dashboard:home')}?tab=calendar&subtab=recorded_leaves")

                DoctorLeave.objects.create(
                    doctor=doctor,
                    hospital=doctor.hospital,
                    start_date=s_date,
                    end_date=e_date,
                    is_full_day=is_full_day,
                    start_time=start_time if not is_full_day else None,
                    end_time=end_time if not is_full_day else None,
                    reason=reason or "Personal Leave",
                    created_by=doctor
                )
                messages.success(request, f"Doctor leave scheduled from {s_date.strftime('%d-%m-%Y')} to {e_date.strftime('%d-%m-%Y')}.")
                return redirect(f"{reverse('dashboard:home')}?tab=calendar&subtab=recorded_leaves")
            except ValueError:
                messages.error(request, "Invalid date format provided.")
                return redirect(f"{reverse('dashboard:home')}?tab=calendar&subtab=add_leave")

        elif action == "delete_leave":
            leave_id = request.POST.get("leave_id")
            DoctorLeave.objects.filter(pk=leave_id, doctor=doctor).delete()
            messages.success(request, "Leave record removed.")
            return redirect(f"{reverse('dashboard:home')}?tab=calendar&subtab=recorded_leaves")

        # Determine redirection based on referring tab if provided
        ref_tab = request.POST.get('ref_tab', '')
        ref_subtab = request.POST.get('ref_subtab', '')
        if ref_tab:
            return redirect(f"{reverse('dashboard:home')}?tab={ref_tab}&subtab={ref_subtab}")

        return redirect("dashboard:doctor_home")
        
    # Doctor's appointments
    doctor_apts = Appointment.objects.filter(
        hospital=doctor.hospital
    ).filter(
        Q(doctor_user=doctor) | 
        Q(doctor_name__icontains=doctor.get_full_name() or doctor.username)
    ).select_related('lead').order_by('-appointment_date', 'appointment_time')
    
    # 1. Appointment Booked (Confirmed/Approved/Scheduled)
    # Today's Booked Appointments:
    today_apts = doctor_apts.filter(
        appointment_date=today
    ).exclude(
        status__in=[AppointmentStatus.CANCELLED, AppointmentStatus.COMPLETED]
    )
    
    # Upcoming Booked Appointments:
    upcoming_apts = doctor_apts.filter(
        appointment_date__gt=today
    ).exclude(
        status__in=[AppointmentStatus.CANCELLED, AppointmentStatus.COMPLETED]
    )
    
    # All Booked Appointments (Today + Upcoming):
    booked_apts = doctor_apts.filter(
        appointment_date__gte=today
    ).exclude(
        status__in=[AppointmentStatus.CANCELLED, AppointmentStatus.COMPLETED]
    )
    
    # 2. Completed Appointments
    completed_apts = doctor_apts.filter(status=AppointmentStatus.COMPLETED)
    
    # Calendar datasets
    schedule, _ = DoctorSchedule.objects.get_or_create(doctor=doctor, defaults={"hospital": doctor.hospital})
    leaves = DoctorLeave.objects.filter(doctor=doctor).order_by("-start_date")
    upcoming_leaves = leaves.filter(end_date__gte=today).order_by("start_date")
    
    # Summary KPI counts
    total_booked_count = booked_apts.count()
    today_count = today_apts.count()
    upcoming_count = upcoming_apts.count()
    completed_count = completed_apts.count()
    
    context = {
        'active': 'doctor_home',
        'today': today,
        'booked_apts': booked_apts,
        'today_apts': today_apts,
        'upcoming_apts': upcoming_apts,
        'completed_apts': completed_apts,
        'schedule': schedule,
        'leaves': leaves,
        'upcoming_leaves': upcoming_leaves,
        'total_booked_count': total_booked_count,
        'today_count': today_count,
        'upcoming_count': upcoming_count,
        'completed_count': completed_count,
    }
    return render(request, "hospital/dashboard/doctor_home.html", context)


@login_required
def doctor_appointments(request):
    """
    Dedicated Doctor Appointments view redirecting to Doctor Dashboard.
    """
    if request.user.role != User.Role.DOCTOR:
        messages.error(request, "Access restricted to doctors only.")
        return redirect("dashboard:home")
    return redirect("dashboard:doctor_home")


@login_required
def doctor_patient_review(request, lead_id):
    """
    Dedicated view for Doctors to review Patient details in clean read-only format,
    with action to add clinical notes / remarks (which notifies assigned telecaller & admin).
    Doctors cannot edit patient form fields or change lead stages/statuses.
    """
    if request.user.role != User.Role.DOCTOR:
        messages.error(request, "Access restricted to doctors only.")
        return redirect("dashboard:home")

    doctor = request.user
    today = timezone.localdate()
    from leads.models import Lead, Appointment, AppointmentStatus
    from notifications.models import Notification

    lead = get_object_or_404(Lead, pk=lead_id)

    # Check tenant access
    if doctor.hospital and lead.hospital and lead.hospital != doctor.hospital:
        messages.error(request, "Permission denied.")
        return redirect("dashboard:doctor_home")

    # Find the appointment linked to this lead and doctor
    appointment = Appointment.objects.filter(
        lead=lead
    ).filter(
        Q(doctor_user=doctor) | Q(doctor_name__icontains=doctor.get_full_name() or doctor.username)
    ).order_by('-appointment_date', '-id').first()

    if not appointment:
        appointment = Appointment.objects.filter(lead=lead).order_by('-appointment_date', '-id').first()

    if request.method == "POST":
        action = request.POST.get("action", "").strip()
        doctor_notes = request.POST.get("doctor_notes", "").strip() or request.POST.get("note", "").strip()

        if action in ["add_note", "doctor_remark"] or doctor_notes:
            if doctor_notes:
                from followups.models import Note, Activity, ActivityType
                doc_name = doctor.get_full_name() or doctor.username
                formatted_note = f"Doctor Note (Dr. {doc_name}): {doctor_notes}"
                
                Note.objects.create(
                    lead=lead,
                    created_by=doctor,
                    note=formatted_note
                )
                Activity.objects.create(
                    lead=lead,
                    created_by=doctor,
                    activity_type=ActivityType.NOTE,
                    description=formatted_note
                )
                
                cd = lead.custom_data or {}
                cd['last_doctor_remark'] = doctor_notes
                cd['doctor_remark'] = doctor_notes
                lead.custom_data = cd
                lead.save(update_fields=['custom_data'])

                if appointment:
                    appointment.doctor_notes = doctor_notes
                    appointment.save(update_fields=['doctor_notes'])

                # Notify Telecaller
                if lead.assigned_to and lead.assigned_to != doctor:
                    Notification.objects.create(
                        user=lead.assigned_to,
                        title=f"Doctor Added Remark: {lead.name}",
                        message=f"Dr. {doc_name} added a note on patient {lead.name} (#{lead.lead_code or lead.pk}): '{doctor_notes}'.",
                        link=f"/leads/{lead.pk}/",
                    )

                # Notify Hospital Admin(s)
                admin_users = User.objects.filter(
                    hospital=doctor.hospital,
                    role__in=[User.Role.ADMIN, User.Role.SUPER_ADMIN],
                    is_active=True
                ).exclude(pk=doctor.pk)
                if lead.assigned_to:
                    admin_users = admin_users.exclude(pk=lead.assigned_to.pk)

                for admin_u in admin_users:
                    Notification.objects.create(
                        user=admin_u,
                        title=f"Doctor Remark Added: {lead.name}",
                        message=f"Dr. {doc_name} added remark on patient {lead.name} (#{lead.lead_code or lead.pk}): '{doctor_notes}'.",
                        link=f"/leads/{lead.pk}/",
                    )

                messages.success(request, f"Note successfully added for {lead.name}. Telecaller & Admin notified.")
            else:
                messages.warning(request, "Please enter a valid note or remark.")
            return redirect("dashboard:doctor_patient_review", lead_id=lead.pk)

    # Prepare Initial Slot Data
    cd = lead.custom_data or {}
    raw_date = cd.get("appo_booked_date") or cd.get("appointment_date")
    current_date = appointment.appointment_date if (appointment and appointment.appointment_date) else None
    if not current_date and raw_date:
        from datetime import datetime
        try:
            current_date = datetime.strptime(str(raw_date)[:10], "%Y-%m-%d").date()
        except Exception:
            current_date = today
    if not current_date:
        current_date = today

    current_time = appointment.appointment_time if (appointment and appointment.appointment_time) else cd.get("appointment_time")

    current_date_display = current_date.strftime("%A, %d %B %Y")
    current_date_ymd = current_date.strftime("%Y-%m-%d")
    current_time_display = current_time.strftime("%I:%M %p") if hasattr(current_time, "strftime") else (str(current_time) if current_time else "Slot not set")
    current_time_hi = current_time.strftime("%H:%M") if hasattr(current_time, "strftime") else (str(current_time)[:5] if current_time else "10:00")

    doc_name = (appointment.doctor_name if appointment else "") or lead.custom_doctor or doctor.get_full_name() or doctor.username

    # Appointments & Consultation History for this lead
    all_appointments = Appointment.objects.filter(lead=lead).order_by("-appointment_date", "-id")

    context = {
        "active": "doctor_home",
        "lead": lead,
        "appointment": appointment,
        "doctor_name_str": doc_name,
        "current_date_display": current_date_display,
        "current_date_ymd": current_date_ymd,
        "current_time_display": current_time_display,
        "current_time_hi": current_time_hi,
        "today_ymd": today.strftime("%Y-%m-%d"),
        "all_appointments": all_appointments,
    }
    return render(request, "dashboard/doctor_patient_review.html", context)

    # Prepare Initial Slot Data
    cd = lead.custom_data or {}
    raw_date = cd.get("appo_booked_date") or cd.get("appointment_date")
    current_date = appointment.appointment_date if (appointment and appointment.appointment_date) else None
    if not current_date and raw_date:
        from datetime import datetime
        try:
            current_date = datetime.strptime(str(raw_date)[:10], "%Y-%m-%d").date()
        except Exception:
            current_date = today
    if not current_date:
        current_date = today

    current_time = appointment.appointment_time if (appointment and appointment.appointment_time) else cd.get("appointment_time")

    current_date_display = current_date.strftime("%A, %d %B %Y")
    current_date_ymd = current_date.strftime("%Y-%m-%d")
    current_time_display = current_time.strftime("%I:%M %p") if hasattr(current_time, "strftime") else (str(current_time) if current_time else "Slot not set")
    current_time_hi = current_time.strftime("%H:%M") if hasattr(current_time, "strftime") else (str(current_time)[:5] if current_time else "10:00")

    doc_name = (appointment.doctor_name if appointment else "") or lead.custom_doctor or doctor.get_full_name() or doctor.username

    # Appointments & Consultation History for this lead
    all_appointments = Appointment.objects.filter(lead=lead).order_by("-appointment_date", "-id")

    context = {
        "active": "doctor_appointments",
        "lead": lead,
        "appointment": appointment,
        "doctor_name_str": doc_name,
        "current_date_display": current_date_display,
        "current_date_ymd": current_date_ymd,
        "current_time_display": current_time_display,
        "current_time_hi": current_time_hi,
        "today_ymd": today.strftime("%Y-%m-%d"),
        "all_appointments": all_appointments,
    }
    return render(request, "dashboard/doctor_patient_review.html", context)




