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
    
    # Handle actions (Approval / Status change / Reschedule / Complete / Leave / Schedule)
    if request.method == "POST":
        action = request.POST.get('action')
        apt_id = request.POST.get('appointment_id')
        apt = None
        if apt_id and str(apt_id).isdigit():
            apt = Appointment.objects.filter(pk=int(apt_id), hospital=doctor.hospital).first()

        lead = apt.lead if apt else None
        time_str = apt.appointment_time.strftime('%I:%M %p') if apt and apt.appointment_time else 'Scheduled'
        date_str = apt.appointment_date.strftime('%d %b %Y') if apt else ''

        if action == "approve" and apt and lead:
            apt.status = AppointmentStatus.APPROVED
            apt.save(update_fields=['status'])

            # Update Lead custom data / deal status to reflect Booked appointment
            cd = lead.custom_data or {}
            cd['appointment_status'] = 'Booking Confirmed'
            cd['appo_booked_date'] = apt.appointment_date.strftime('%Y-%m-%d')
            if apt.appointment_time:
                cd['appointment_time'] = apt.appointment_time.strftime('%I:%M %p')
            cd['appointment_confirmed_at'] = timezone.now().strftime('%Y-%m-%d %H:%M')
            lead.custom_data = cd
            lead.next_followup_date = None
            booking_stage = LeadStage.objects.filter(name__iexact='Booking Confirmed').first() or \
                            LeadStage.objects.filter(name__iexact='Appointment Confirmed').first()
            if booking_stage:
                lead.stage = booking_stage
                lead.save(update_fields=['custom_data', 'next_followup_date', 'stage'])
            else:
                lead.save(update_fields=['custom_data', 'next_followup_date'])

            # Notify Lead Attendant
            if lead.assigned_to:
                Notification.objects.create(
                    user=lead.assigned_to,
                    title="Appointment Approved by Doctor",
                    message=f"Dr. {doctor.get_full_name() or doctor.username} confirmed appointment for patient {lead.name} on {date_str} at {time_str}.",
                    link=f"/leads/{lead.pk}/",
                )

            messages.success(request, f"Appointment for {lead.name} on {date_str} at {time_str} approved and confirmed!")

        elif (action == "reject" or action == "cancel") and apt and lead:
            reason = request.POST.get('reject_reason', '').strip() or request.POST.get('doctor_notes', '').strip() or 'Cancelled by Doctor'
            apt.status = AppointmentStatus.CANCELLED
            apt.doctor_notes = reason
            apt.save(update_fields=['status', 'doctor_notes'])

            # Update Lead custom data
            cd = lead.custom_data or {}
            cd['appointment_status'] = f"Doctor Cancelled: {reason}"
            cd['doctor_remark'] = reason
            lead.custom_data = cd
            lead.next_followup_date = timezone.localdate()
            lead.save(update_fields=['custom_data', 'next_followup_date'])

            if lead.assigned_to:
                Notification.objects.create(
                    user=lead.assigned_to,
                    title="Appointment Cancelled by Doctor",
                    message=f"Dr. {doctor.get_full_name() or doctor.username} cancelled appointment for {lead.name} ({date_str}). Reason: {reason}.",
                    link=f"/leads/{lead.pk}/",
                )

            messages.info(request, f"Appointment for {lead.name} marked cancelled. Lead attendant notified.")

        elif action == "change_slot" and apt and lead:
            new_date_str = request.POST.get('new_date', '').strip()
            new_time_str = request.POST.get('new_time', '').strip()
            remark = request.POST.get('doctor_remark', '').strip() or request.POST.get('doctor_notes', '').strip() or 'Rescheduled by doctor.'
            
            if new_date_str:
                new_date_obj = datetime.strptime(new_date_str, "%Y-%m-%d").date()
                
                # Check for doctor leave
                is_on_leave = DoctorLeave.objects.filter(
                    doctor=doctor,
                    start_date__lte=new_date_obj,
                    end_date__gte=new_date_obj
                ).exists()
                
                if is_on_leave:
                    messages.error(request, "Cannot reschedule to this date as you are marked on leave/off.")
                    return redirect("dashboard:doctor_home")
                    
                apt.appointment_date = new_date_obj
            if new_time_str:
                apt.appointment_time = new_time_str
            
            apt.status = AppointmentStatus.APPROVED
            apt.doctor_notes = remark
            apt.save(update_fields=['appointment_date', 'appointment_time', 'status', 'doctor_notes'])
            
            cd = lead.custom_data or {}
            if new_date_str:
                cd['appo_booked_date'] = new_date_str
            if new_time_str:
                cd['appointment_time'] = new_time_str
            cd['appointment_status'] = 'Booking Confirmed (Rescheduled)'
            cd['doctor_reschedule_remark'] = remark
            lead.custom_data = cd
            lead.next_followup_date = timezone.localdate()
            lead.save(update_fields=['custom_data', 'next_followup_date'])
            
            if lead.assigned_to:
                time_display = apt.appointment_time.strftime('%I:%M %p') if hasattr(apt.appointment_time, 'strftime') else str(apt.appointment_time)
                date_display = apt.appointment_date.strftime('%d %b %Y')
                Notification.objects.create(
                    user=lead.assigned_to,
                    title="Doctor Rescheduled Appointment Slot",
                    message=f"Dr. {doctor.get_full_name() or doctor.username} rescheduled slot for {lead.name}: {date_display} at {time_display}. Remark: '{remark}'.",
                    link=f"/leads/{lead.pk}/",
                )
            
            messages.success(request, f"Appointment slot updated for {lead.name}.")

        elif action == "update_status" and apt and lead:
            new_status = request.POST.get('new_status', '').strip()
            doctor_notes = request.POST.get('doctor_notes', '').strip()
            
            if new_status == "PENDING_APPROVAL":
                apt.status = AppointmentStatus.PENDING_APPROVAL
            elif new_status == "CONFIRMED" or new_status == "APPROVED":
                apt.status = AppointmentStatus.APPROVED
            elif new_status == "SCHEDULED":
                apt.status = AppointmentStatus.SCHEDULED
            elif new_status == "COMPLETED":
                apt.status = AppointmentStatus.COMPLETED
            elif new_status == "CANCELLED":
                apt.status = AppointmentStatus.CANCELLED
            elif new_status in AppointmentStatus.values:
                apt.status = new_status
                
            if doctor_notes:
                apt.doctor_notes = doctor_notes
            apt.save(update_fields=['status', 'doctor_notes'])
            
            cd = lead.custom_data or {}
            cd['appointment_status'] = apt.get_status_display()
            if doctor_notes:
                cd['doctor_remark'] = doctor_notes
            lead.custom_data = cd
            lead.save(update_fields=['custom_data'])
            
            if lead.assigned_to:
                Notification.objects.create(
                    user=lead.assigned_to,
                    title=f"Appointment Status Updated: {apt.get_status_display()}",
                    message=f"Dr. {doctor.get_full_name() or doctor.username} updated status for {lead.name} to '{apt.get_status_display()}'. Notes: '{doctor_notes}'.",
                    link=f"/leads/{lead.pk}/",
                )
            messages.success(request, f"Status updated to '{apt.get_status_display()}' for {lead.name}.")

        elif action == "complete" and apt and lead:
            doctor_notes = request.POST.get('doctor_notes', '').strip()
            add_next_apt = (request.POST.get('add_next_appointment') == '1')
            next_date_str = request.POST.get('next_appointment_date', '').strip()
            next_time_str = request.POST.get('next_appointment_time', '').strip()
            next_notes = request.POST.get('next_appointment_notes', '').strip() or doctor_notes

            # 1. Mark current appointment completed
            apt.status = AppointmentStatus.COMPLETED
            if doctor_notes:
                apt.doctor_notes = doctor_notes
            apt.save(update_fields=['status', 'doctor_notes'])
            
            cd = lead.custom_data or {}
            cd['appointment_status'] = 'Completed'
            if doctor_notes:
                cd['doctor_remark'] = doctor_notes
                cd['last_doctor_remark'] = doctor_notes
            lead.custom_data = cd
            completed_stage = LeadStage.objects.filter(name__iexact='Appointment Completed').first() or \
                              LeadStage.objects.filter(name__iexact='Completed').first()
            if completed_stage:
                lead.stage = completed_stage
                lead.save(update_fields=['custom_data', 'stage'])
            else:
                lead.save(update_fields=['custom_data'])

            # Log Activity
            from followups.models import Activity, ActivityType, FollowUp, FollowUpMode, FollowUpStatus
            Activity.objects.create(
                lead=lead,
                created_by=doctor,
                activity_type=ActivityType.NOTE,
                description=f"Dr. {doctor.get_full_name() or doctor.username} marked consultation completed. Remarks: {doctor_notes or 'No clinical remarks recorded.'}"
            )

            # 2. Check if Next Appointment is scheduled by Doctor
            if add_next_apt and next_date_str:
                try:
                    next_date = datetime.strptime(next_date_str, "%Y-%m-%d").date()
                except ValueError:
                    next_date = timezone.localdate()

                Appointment.objects.create(
                    lead=lead,
                    hospital=doctor.hospital,
                    doctor_name=doctor.get_full_name() or doctor.username,
                    doctor_user=doctor,
                    appointment_date=next_date,
                    appointment_time=next_time_str if next_time_str else None,
                    status=AppointmentStatus.SCHEDULED,
                    doctor_notes=next_notes,
                    notes=f"Next follow-up consultation set by Dr. {doctor.get_full_name() or doctor.username}.",
                    created_by=doctor
                )

                lead.next_followup_date = next_date
                cd['appo_booked_date'] = next_date_str
                if next_time_str:
                    cd['appointment_time'] = next_time_str
                cd['appointment_status'] = 'Doctor Scheduled Next Appointment (Pending Patient Confirmation)'
                cd['doctor_reschedule_remark'] = next_notes
                lead.custom_data = cd
                lead.save(update_fields=['next_followup_date', 'custom_data'])

                FollowUp.objects.create(
                    lead=lead,
                    followup_date=timezone.localdate(),
                    followup_mode=FollowUpMode.CALL,
                    followup_status=FollowUpStatus.PENDING,
                    comment=f"Dr. {doctor.get_full_name() or doctor.username} scheduled next consultation for {next_date.strftime('%d %b %Y')} {next_time_str or ''}. Remark: '{next_notes}'.",
                    created_by=doctor
                )

                if lead.assigned_to:
                    time_disp = next_time_str if next_time_str else "Slot not set"
                    Notification.objects.create(
                        user=lead.assigned_to,
                        title="Doctor Scheduled Next Appointment",
                        message=f"Dr. {doctor.get_full_name() or doctor.username} scheduled next consultation for patient {lead.name} on {next_date.strftime('%d %b %Y')} at {time_disp}. Clinical Remark: '{next_notes}'.",
                        link=f"/leads/{lead.pk}/",
                    )
                messages.success(request, f"Appointment completed and next consultation follow-up scheduled for {lead.name}!")
            else:
                if lead.assigned_to:
                    Notification.objects.create(
                        user=lead.assigned_to,
                        title="Appointment Completed - Enter Billing Details",
                        message=f"Dr. {doctor.get_full_name() or doctor.username} completed the appointment for patient {lead.name}. Remarks: '{doctor_notes or 'Completed'}'.",
                        link=f"/leads/{lead.pk}/edit/",
                    )
                messages.success(request, f"Appointment for {lead.name} marked completed.")

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
                s_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
                e_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()

                if s_date < today:
                    messages.error(request, "Leave date cannot be in the past.")
                    return redirect(f"{reverse('dashboard:home')}?tab=calendar&subtab=add_leave")
                if e_date < s_date:
                    messages.error(request, "End date cannot be earlier than start date.")
                    return redirect(f"{reverse('dashboard:home')}?tab=calendar&subtab=add_leave")

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
    
    # 1. Appointments Sub-datasets
    # Pending Requests: booking requested / doctor approval pending
    pending_apts = doctor_apts.filter(status=AppointmentStatus.PENDING_APPROVAL)
    
    # Today's Appointments: confirmed/approved/scheduled for today
    today_apts = doctor_apts.filter(
        appointment_date=today,
        status__in=[AppointmentStatus.APPROVED, AppointmentStatus.SCHEDULED]
    )
    
    # Upcoming Appointments: confirmed/approved/scheduled for future dates (> today)
    upcoming_apts = doctor_apts.filter(
        appointment_date__gt=today,
        status__in=[AppointmentStatus.APPROVED, AppointmentStatus.SCHEDULED]
    )
    
    # Admitted Appointments: appointments not completed and not cancelled (e.g. past appointment date or admitted in hospital)
    admitted_apts = doctor_apts.filter(
        appointment_date__lt=today
    ).exclude(
        status__in=[AppointmentStatus.CANCELLED, AppointmentStatus.COMPLETED, AppointmentStatus.PENDING_APPROVAL]
    )
    
    # 3. Completed Tab
    completed_apts = doctor_apts.filter(status=AppointmentStatus.COMPLETED)
    
    # 4. Cancelled Tab
    cancelled_apts = doctor_apts.filter(status=AppointmentStatus.CANCELLED)
    
    # Calendar datasets
    schedule, _ = DoctorSchedule.objects.get_or_create(doctor=doctor, defaults={"hospital": doctor.hospital})
    leaves = DoctorLeave.objects.filter(doctor=doctor).order_by("-start_date")
    upcoming_leaves = leaves.filter(end_date__gte=today).order_by("start_date")
    
    # Summary KPI counts
    total_count = doctor_apts.count()
    today_count = today_apts.count()
    pending_count = pending_apts.count()
    upcoming_count = upcoming_apts.count()
    admitted_count = admitted_apts.count()
    completed_count = completed_apts.count()
    cancelled_count = cancelled_apts.count()
    
    context = {
        'active': 'doctor_home',
        'today': today,
        'today_apts': today_apts,
        'pending_apts': pending_apts,
        'upcoming_apts': upcoming_apts,
        'admitted_apts': admitted_apts,
        'completed_apts': completed_apts,
        'cancelled_apts': cancelled_apts,
        'schedule': schedule,
        'leaves': leaves,
        'upcoming_leaves': upcoming_leaves,
        'total_count': total_count,
        'today_count': today_count,
        'pending_count': pending_count,
        'upcoming_count': upcoming_count,
        'admitted_count': admitted_count,
        'completed_count': completed_count,
        'cancelled_count': cancelled_count,
    }
    return render(request, "hospital/dashboard/doctor_home.html", context)


@login_required
def doctor_appointments(request):
    """
    Dedicated Doctor Appointments management page.
    Doctor can review pending booking requests, change slots, approve, reject, update status, and complete appointments.
    """
    if request.user.role != User.Role.DOCTOR:
        messages.error(request, "Access restricted to doctors only.")
        return redirect("dashboard:home")

    doctor = request.user
    today = timezone.localdate()

    if request.method == "POST":
        apt_id = request.POST.get('appointment_id')
        action = request.POST.get('action')
        apt = get_object_or_404(Appointment, pk=apt_id, hospital=doctor.hospital)
        lead = apt.lead
        time_str = apt.appointment_time.strftime('%I:%M %p') if apt.appointment_time else 'Slot not fixed'
        date_str = apt.appointment_date.strftime('%d %b %Y')

        if action == "approve":
            apt.status = AppointmentStatus.APPROVED
            apt.save(update_fields=['status'])
            cd = lead.custom_data or {}
            cd['appointment_status'] = 'Booking Confirmed'
            cd['appo_booked_date'] = apt.appointment_date.strftime('%Y-%m-%d')
            if apt.appointment_time:
                cd['appointment_time'] = apt.appointment_time.strftime('%I:%M %p')
            cd['appointment_confirmed_at'] = timezone.now().strftime('%Y-%m-%d %H:%M')
            lead.custom_data = cd
            lead.next_followup_date = None
            booking_stage = LeadStage.objects.filter(name__iexact='Booking Confirmed').first() or \
                            LeadStage.objects.filter(name__iexact='Appointment Confirmed').first()
            if booking_stage:
                lead.stage = booking_stage
                lead.save(update_fields=['custom_data', 'next_followup_date', 'stage'])
            else:
                lead.save(update_fields=['custom_data', 'next_followup_date'])

            if lead.assigned_to:
                Notification.objects.create(
                    user=lead.assigned_to,
                    title="Appointment Approved by Doctor",
                    message=f"Dr. {doctor.get_full_name() or doctor.username} confirmed and booked appointment for patient {lead.name} on {date_str} at {time_str}.",
                    link=f"/leads/{lead.pk}/",
                )
            messages.success(request, f"Appointment for {lead.name} on {date_str} at {time_str} approved and Booking Confirmed!")

        elif action == "reject" or action == "cancel":
            reason = request.POST.get('reject_reason', '').strip() or request.POST.get('doctor_notes', '').strip() or 'Doctor unavailable / slot full'
            apt.status = AppointmentStatus.CANCELLED
            apt.doctor_notes = reason
            apt.save(update_fields=['status', 'doctor_notes'])
            cd = lead.custom_data or {}
            cd['appointment_status'] = f"Doctor Rejected: {reason}"
            lead.custom_data = cd
            lead.next_followup_date = timezone.localdate()
            lead.save(update_fields=['custom_data', 'next_followup_date'])

            if lead.assigned_to:
                Notification.objects.create(
                    user=lead.assigned_to,
                    title="Appointment Rejected by Doctor",
                    message=f"Dr. {doctor.get_full_name() or doctor.username} rejected appointment for {lead.name} ({date_str}). Reason: {reason}. Lead moved to your Follow-ups list.",
                    link=f"/leads/{lead.pk}/",
                )
            messages.info(request, f"Appointment for {lead.name} rejected. Telecaller notified.")

        elif action == "change_slot":
            new_date_str = request.POST.get('new_date', '').strip()
            new_time_str = request.POST.get('new_time', '').strip()
            remark = request.POST.get('doctor_remark', '').strip() or request.POST.get('doctor_notes', '').strip() or 'Doctor requested to reschedule to this new slot.'
            
            if new_date_str:
                from datetime import datetime
                apt.appointment_date = datetime.strptime(new_date_str, "%Y-%m-%d").date()
            if new_time_str:
                apt.appointment_time = new_time_str
            
            apt.status = AppointmentStatus.SCHEDULED
            apt.doctor_notes = remark
            apt.save(update_fields=['appointment_date', 'appointment_time', 'status', 'doctor_notes'])
            
            cd = lead.custom_data or {}
            if new_date_str:
                cd['appo_booked_date'] = new_date_str
            if new_time_str:
                cd['appointment_time'] = new_time_str
            cd['appointment_status'] = 'Slot Changed by Doctor (Pending Patient Confirmation)'
            cd['doctor_reschedule_remark'] = remark
            lead.custom_data = cd
            lead.next_followup_date = timezone.localdate()
            lead.save(update_fields=['custom_data', 'next_followup_date'])
            
            if lead.assigned_to:
                time_display = apt.appointment_time.strftime('%I:%M %p') if hasattr(apt.appointment_time, 'strftime') else str(apt.appointment_time)
                date_display = apt.appointment_date.strftime('%d %b %Y')
                Notification.objects.create(
                    user=lead.assigned_to,
                    title="Doctor Changed Slot - Please Confirm by Patient",
                    message=f"Dr. {doctor.get_full_name() or doctor.username} assigned a new slot for {lead.name}: {date_display} at {time_display}. Remark: '{remark}'. Please call patient to confirm.",
                    link=f"/leads/{lead.pk}/",
                )
            messages.success(request, f"Appointment slot updated for {lead.name}. Telecaller notified.")

        elif action == "update_status":
            new_status = request.POST.get('new_status', '').strip()
            doctor_notes = request.POST.get('doctor_notes', '').strip()
            if new_status in AppointmentStatus.values:
                apt.status = new_status
                if doctor_notes:
                    apt.doctor_notes = doctor_notes
                apt.save(update_fields=['status', 'doctor_notes'])
                
                cd = lead.custom_data or {}
                cd['appointment_status'] = apt.get_status_display()
                if doctor_notes:
                    cd['doctor_remark'] = doctor_notes
                lead.custom_data = cd
                lead.save(update_fields=['custom_data'])
                
                if lead.assigned_to:
                    Notification.objects.create(
                        user=lead.assigned_to,
                        title=f"Appointment Status: {apt.get_status_display()}",
                        message=f"Dr. {doctor.get_full_name() or doctor.username} updated appointment status for {lead.name} to '{apt.get_status_display()}'. Notes: '{doctor_notes}'.",
                        link=f"/leads/{lead.pk}/",
                    )
                messages.success(request, f"Status updated to '{apt.get_status_display()}' for patient {lead.name}.")

        elif action == "complete":
            doctor_notes = request.POST.get('doctor_notes', '').strip()
            add_next_apt = (request.POST.get('add_next_appointment') == '1')
            next_date_str = request.POST.get('next_appointment_date', '').strip()
            next_time_str = request.POST.get('next_appointment_time', '').strip()
            next_notes = request.POST.get('next_appointment_notes', '').strip() or doctor_notes

            # 1. Mark current appointment completed
            apt.status = AppointmentStatus.COMPLETED
            if doctor_notes:
                apt.doctor_notes = doctor_notes
            apt.save(update_fields=['status', 'doctor_notes'])
            
            # Sync Lead custom_data status
            cd = lead.custom_data or {}
            cd['appointment_status'] = 'Completed'
            if doctor_notes:
                cd['doctor_remark'] = doctor_notes
                cd['last_doctor_remark'] = doctor_notes
            lead.custom_data = cd
            lead.save(update_fields=['custom_data'])

            # Log Activity
            from followups.models import Activity, ActivityType, FollowUp, FollowUpMode, FollowUpStatus
            Activity.objects.create(
                lead=lead,
                created_by=doctor,
                activity_type=ActivityType.NOTE,
                description=f"Dr. {doctor.get_full_name() or doctor.username} marked consultation completed. Remarks: {doctor_notes or 'No clinical remarks recorded.'}"
            )

            # 2. Check if Next Appointment is scheduled by Doctor
            if add_next_apt and next_date_str:
                from datetime import datetime
                try:
                    next_date = datetime.strptime(next_date_str, "%Y-%m-%d").date()
                except ValueError:
                    next_date = timezone.localdate()

                # Create next appointment record in SCHEDULED status
                new_apt = Appointment.objects.create(
                    lead=lead,
                    hospital=doctor.hospital,
                    doctor_name=doctor.get_full_name() or doctor.username,
                    doctor_user=doctor,
                    appointment_date=next_date,
                    appointment_time=next_time_str if next_time_str else None,
                    status=AppointmentStatus.SCHEDULED,
                    doctor_notes=next_notes,
                    notes=f"Next follow-up consultation set by Dr. {doctor.get_full_name() or doctor.username}.",
                    created_by=doctor
                )

                # Set next follow up on lead
                lead.next_followup_date = next_date
                cd['appo_booked_date'] = next_date_str
                if next_time_str:
                    cd['appointment_time'] = next_time_str
                cd['appointment_status'] = 'Doctor Scheduled Next Appointment (Pending Patient Confirmation)'
                cd['doctor_reschedule_remark'] = next_notes
                lead.custom_data = cd
                lead.save(update_fields=['next_followup_date', 'custom_data'])

                # Create FollowUp entry assigned to lead attendant
                FollowUp.objects.create(
                    lead=lead,
                    followup_date=timezone.localdate(),
                    followup_mode=FollowUpMode.CALL,
                    followup_status=FollowUpStatus.PENDING,
                    comment=f"Dr. {doctor.get_full_name() or doctor.username} scheduled next consultation for {next_date.strftime('%d %b %Y')} {next_time_str or ''}. Remark: '{next_notes}'. Please call patient to confirm slot.",
                    created_by=doctor
                )

                # Send Notification to Telecaller (Lead Attendant)
                if lead.assigned_to:
                    time_disp = next_time_str if next_time_str else "Slot not set"
                    Notification.objects.create(
                        user=lead.assigned_to,
                        title="Doctor Scheduled Next Appointment - Confirm with Patient",
                        message=f"Dr. {doctor.get_full_name() or doctor.username} scheduled next consultation for patient {lead.name} on {next_date.strftime('%d %b %Y')} at {time_disp}. Clinical Remark: '{next_notes}'. Please call and confirm with patient.",
                        link=f"/leads/{lead.pk}/",
                    )
                messages.success(request, f"Appointment completed and next consultation follow-up scheduled for {lead.name} on {next_date.strftime('%d %b %Y')}! Telecaller notified.")
            else:
                # Send Notification to Telecaller (Lead Attendant) to enter billing & UHID details
                if lead.assigned_to:
                    Notification.objects.create(
                        user=lead.assigned_to,
                        title="Appointment Completed - Enter Billing Details",
                        message=f"Dr. {doctor.get_full_name() or doctor.username} completed the appointment for patient {lead.name}. Remarks: '{doctor_notes or 'Completed'}'. Please enter UHID & Billing details in your Billing Follow-ups list.",
                        link=f"/leads/{lead.pk}/edit/",
                    )
                messages.success(request, f"Appointment for {lead.name} marked completed. Doctor remarks saved to patient history.")

        return redirect("dashboard:doctor_appointments")

    # Base query for doctor's appointments
    doctor_apts = Appointment.objects.filter(
        hospital=doctor.hospital
    ).filter(
        Q(doctor_user=doctor) | 
        Q(doctor_name__icontains=doctor.get_full_name() or doctor.username)
    ).select_related('lead', 'lead__assigned_to').order_by('-appointment_date', '-appointment_time')

    # Status tab filtering
    tab = request.GET.get('tab', 'requests').strip()
    q = request.GET.get('q', '').strip()

    if q:
        doctor_apts = doctor_apts.filter(
            Q(lead__name__icontains=q) | 
            Q(lead__mobile__icontains=q) |
            Q(lead__lead_code__icontains=q) |
            Q(doctor_notes__icontains=q)
        )

    pending_apts = doctor_apts.filter(status=AppointmentStatus.PENDING_APPROVAL)
    today_apts = doctor_apts.filter(appointment_date=today).exclude(
        status__in=[AppointmentStatus.CANCELLED, AppointmentStatus.COMPLETED, AppointmentStatus.PENDING_APPROVAL]
    )
    upcoming_apts = doctor_apts.filter(appointment_date__gt=today).exclude(
        status__in=[AppointmentStatus.CANCELLED, AppointmentStatus.COMPLETED, AppointmentStatus.PENDING_APPROVAL]
    )
    completed_apts = doctor_apts.filter(status=AppointmentStatus.COMPLETED)
    cancelled_apts = doctor_apts.filter(status=AppointmentStatus.CANCELLED)

    if tab == 'requests':
        displayed_apts = pending_apts
    elif tab == 'today':
        displayed_apts = today_apts
    elif tab == 'upcoming':
        displayed_apts = upcoming_apts
    elif tab == 'completed':
        displayed_apts = completed_apts
    elif tab == 'cancelled':
        displayed_apts = cancelled_apts
    else:
        displayed_apts = doctor_apts

    context = {
        'active': 'doctor_appointments',
        'tab': tab,
        'q': q,
        'displayed_apts': displayed_apts,
        'pending_count': pending_apts.count(),
        'today_count': today_apts.count(),
        'upcoming_count': upcoming_apts.count(),
        'completed_count': completed_apts.count(),
        'cancelled_count': cancelled_apts.count(),
        'total_count': doctor_apts.count(),
        'today': today,
    }
    return render(request, "dashboard/doctor_appointments.html", context)


@login_required
def doctor_patient_review(request, lead_id):
    """
    Dedicated view for Doctors to review Patient details in clean table format,
    with an editable option to change slot, confirm appointment or confirm slot change.
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
        return redirect("dashboard:doctor_appointments")

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
        slot_modified = request.POST.get("slot_modified", "0").strip() == "1"
        doctor_notes = request.POST.get("doctor_notes", "").strip()

        if action == "reject":
            reason = request.POST.get("reject_reason", "").strip() or doctor_notes or "Doctor unavailable / slot full"
            if appointment:
                appointment.status = AppointmentStatus.CANCELLED
                appointment.doctor_notes = reason
                appointment.save(update_fields=["status", "doctor_notes"])

            cd = lead.custom_data or {}
            cd["appointment_status"] = f"Doctor Rejected: {reason}"
            lead.custom_data = cd
            lead.next_followup_date = timezone.localdate()
            lead.save(update_fields=["custom_data", "next_followup_date"])

            if lead.assigned_to:
                Notification.objects.create(
                    user=lead.assigned_to,
                    title="Appointment Rejected by Doctor",
                    message=f"Dr. {doctor.get_full_name() or doctor.username} rejected appointment request for {lead.name}. Reason: {reason}. Lead moved to your Follow-ups list.",
                    link=f"/leads/{lead.pk}/",
                )
        if action == "complete":
            # 1. Mark current appointment as COMPLETED
            if appointment:
                appointment.status = AppointmentStatus.COMPLETED
                if doctor_notes:
                    appointment.doctor_notes = doctor_notes
                appointment.save(update_fields=["status", "doctor_notes"])
            else:
                appointment = Appointment.objects.create(
                    lead=lead,
                    hospital=doctor.hospital,
                    doctor_name=doctor.get_full_name() or doctor.username,
                    doctor_user=doctor,
                    appointment_date=today,
                    status=AppointmentStatus.COMPLETED,
                    doctor_notes=doctor_notes,
                )

            cd = lead.custom_data or {}
            cd["appointment_status"] = "Completed"
            if doctor_notes:
                cd["doctor_remark"] = doctor_notes
                cd["last_doctor_remark"] = doctor_notes
            lead.custom_data = cd
            lead.save(update_fields=["custom_data"])

            # Log Activity
            from followups.models import Activity, ActivityType, FollowUp, FollowUpMode, FollowUpStatus
            Activity.objects.create(
                lead=lead,
                created_by=doctor,
                activity_type=ActivityType.NOTE,
                description=f"Dr. {doctor.get_full_name() or doctor.username} marked consultation completed. Remarks: {doctor_notes or 'No clinical remarks recorded.'}"
            )

            # Check if Next Appointment is scheduled by Doctor
            add_next_apt = request.POST.get("add_next_appointment") == "1"
            next_date_str = request.POST.get("next_appointment_date", "").strip()
            next_time_str = request.POST.get("next_appointment_time", "").strip()
            next_notes = request.POST.get("next_appointment_notes", "").strip()

            if add_next_apt and next_date_str:
                from datetime import datetime
                try:
                    next_date = datetime.strptime(next_date_str, "%Y-%m-%d").date()
                except ValueError:
                    next_date = timezone.localdate()

                # Create next appointment in SCHEDULED status
                new_apt = Appointment.objects.create(
                    lead=lead,
                    hospital=doctor.hospital,
                    doctor_name=doctor.get_full_name() or doctor.username,
                    doctor_user=doctor,
                    appointment_date=next_date,
                    appointment_time=next_time_str if next_time_str else None,
                    status=AppointmentStatus.SCHEDULED,
                    doctor_notes=next_notes,
                    notes=f"Next follow-up consultation set by Dr. {doctor.get_full_name() or doctor.username}.",
                    created_by=doctor
                )

                cd["appo_booked_date"] = next_date.strftime("%Y-%m-%d")
                if next_time_str:
                    cd["appointment_time"] = next_time_str
                cd["appointment_status"] = "Follow-up Scheduled by Doctor (Pending Confirmation)"
                cd["doctor_reschedule_remark"] = next_notes
                lead.custom_data = cd
                lead.next_followup_date = next_date
                lead.save(update_fields=["custom_data", "next_followup_date"])

                # Create FollowUp record & notify telecaller
                if lead.assigned_to:
                    time_display_str = f" at {next_time_str}" if next_time_str else ""
                    FollowUp.objects.create(
                        lead=lead,
                        followup_date=timezone.localdate(),
                        followup_mode=FollowUpMode.CALL,
                        followup_status=FollowUpStatus.PENDING,
                        comment=f"Dr. {doctor.get_full_name() or doctor.username} completed consultation and set next appointment for {next_date.strftime('%d %b %Y')}{time_display_str}. Remarks: '{next_notes}'. Please confirm with patient.",
                        next_followup_date=next_date,
                        created_by=doctor
                    )

                    Notification.objects.create(
                        user=lead.assigned_to,
                        title="Patient Consultation Completed & Next Follow-up Scheduled",
                        message=f"Dr. {doctor.get_full_name() or doctor.username} completed consultation for {lead.name} and scheduled next appointment on {next_date.strftime('%d %b %Y')}{time_display_str}. Please confirm with patient.",
                        link=f"/leads/{lead.pk}/",
                    )
                messages.success(request, f"Consultation marked as Completed & Next appointment scheduled for {lead.name}! ✅")
            else:
                messages.success(request, f"Consultation for {lead.name} marked as Completed! Clinical remarks saved. ✅")

            return redirect("dashboard:doctor_appointments")

        if action == "schedule_next":
            # Schedule next appointment from Completed page
            next_date_str = request.POST.get("next_appointment_date", "").strip()
            next_time_str = request.POST.get("next_appointment_time", "").strip()
            next_notes = request.POST.get("next_appointment_notes", "").strip()

            if next_date_str:
                from datetime import datetime
                try:
                    next_date = datetime.strptime(next_date_str, "%Y-%m-%d").date()
                except ValueError:
                    next_date = timezone.localdate()

                Appointment.objects.create(
                    lead=lead,
                    hospital=doctor.hospital,
                    doctor_name=doctor.get_full_name() or doctor.username,
                    doctor_user=doctor,
                    appointment_date=next_date,
                    appointment_time=next_time_str if next_time_str else None,
                    status=AppointmentStatus.SCHEDULED,
                    doctor_notes=next_notes,
                    notes=f"Next follow-up consultation set by Dr. {doctor.get_full_name() or doctor.username}.",
                    created_by=doctor
                )

                cd = lead.custom_data or {}
                cd["appo_booked_date"] = next_date.strftime("%Y-%m-%d")
                if next_time_str:
                    cd["appointment_time"] = next_time_str
                cd["appointment_status"] = "Follow-up Scheduled by Doctor (Pending Confirmation)"
                cd["doctor_reschedule_remark"] = next_notes
                lead.custom_data = cd
                lead.next_followup_date = next_date
                lead.save(update_fields=["custom_data", "next_followup_date"])

                from followups.models import FollowUp, FollowUpMode, FollowUpStatus
                if lead.assigned_to:
                    time_display_str = f" at {next_time_str}" if next_time_str else ""
                    FollowUp.objects.create(
                        lead=lead,
                        followup_date=timezone.localdate(),
                        followup_mode=FollowUpMode.CALL,
                        followup_status=FollowUpStatus.PENDING,
                        comment=f"Dr. {doctor.get_full_name() or doctor.username} scheduled next follow-up appointment for {next_date.strftime('%d %b %Y')}{time_display_str}. Remarks: '{next_notes}'. Please confirm with patient.",
                        next_followup_date=next_date,
                        created_by=doctor
                    )

                    Notification.objects.create(
                        user=lead.assigned_to,
                        title="Next Follow-up Appointment Scheduled by Doctor",
                        message=f"Dr. {doctor.get_full_name() or doctor.username} scheduled next appointment for {lead.name} on {next_date.strftime('%d %b %Y')}{time_display_str}. Please confirm with patient.",
                        link=f"/leads/{lead.pk}/",
                    )
                messages.success(request, f"Next appointment scheduled for {lead.name}! Telecaller notified. ✅")
                return redirect("dashboard:doctor_appointments")

        # Confirm Appointment or Confirm Slot Change
        new_date_str = request.POST.get("new_date", "").strip()
        new_time_str = request.POST.get("new_time", "").strip()
        doctor_remark = request.POST.get("doctor_remark", "").strip()

        if slot_modified and (new_date_str or new_time_str):
            # SLOT CHANGED FLOW
            from datetime import datetime
            new_date = datetime.strptime(new_date_str, "%Y-%m-%d").date() if new_date_str else (appointment.appointment_date if appointment else today)
            new_time = new_time_str if new_time_str else (appointment.appointment_time if appointment else None)
            reschedule_note = doctor_remark or doctor_notes or "Doctor rescheduled appointment slot."

            if appointment:
                appointment.appointment_date = new_date
                appointment.appointment_time = new_time
                appointment.status = AppointmentStatus.SCHEDULED
                appointment.doctor_notes = reschedule_note
                appointment.save(update_fields=["appointment_date", "appointment_time", "status", "doctor_notes"])
            else:
                appointment = Appointment.objects.create(
                    lead=lead,
                    hospital=doctor.hospital,
                    doctor_name=doctor.get_full_name() or doctor.username,
                    doctor_user=doctor,
                    appointment_date=new_date,
                    appointment_time=new_time,
                    status=AppointmentStatus.SCHEDULED,
                    doctor_notes=reschedule_note,
                )

            cd = lead.custom_data or {}
            cd["appo_booked_date"] = new_date.strftime("%Y-%m-%d")
            if new_time:
                cd["appointment_time"] = str(new_time)
            cd["appointment_status"] = "Slot Changed by Doctor (Pending Patient Confirmation)"
            cd["doctor_reschedule_remark"] = reschedule_note
            lead.custom_data = cd
            lead.next_followup_date = timezone.localdate()
            lead.save(update_fields=["custom_data", "next_followup_date"])

            if lead.assigned_to:
                time_disp = appointment.appointment_time.strftime("%I:%M %p") if hasattr(appointment.appointment_time, "strftime") else str(appointment.appointment_time or "Slot Not Set")
                Notification.objects.create(
                    user=lead.assigned_to,
                    title="Doctor Changed Slot - Please Confirm with Patient",
                    message=f"Dr. {doctor.get_full_name() or doctor.username} updated slot for {lead.name}: {new_date.strftime('%d %b %Y')} at {time_disp}. Remark: '{reschedule_note}'. Please call patient to confirm.",
                    link=f"/leads/{lead.pk}/",
                )
            messages.success(request, f"Appointment slot changed and confirmed for {lead.name}! Telecaller notified.")
        else:
            # SLOT NOT CHANGED -> CONFIRM / APPROVE APPOINTMENT
            if appointment:
                appointment.status = AppointmentStatus.APPROVED
                if doctor_notes:
                    appointment.doctor_notes = doctor_notes
                appointment.save(update_fields=["status", "doctor_notes"])
                apt_date = appointment.appointment_date
                apt_time = appointment.appointment_time
            else:
                cd = lead.custom_data or {}
                raw_d = cd.get("appo_booked_date")
                from datetime import datetime
                apt_date = datetime.strptime(raw_d, "%Y-%m-%d").date() if raw_d else today
                apt_time = cd.get("appointment_time") or "10:00"
                appointment = Appointment.objects.create(
                    lead=lead,
                    hospital=doctor.hospital,
                    doctor_name=doctor.get_full_name() or doctor.username,
                    doctor_user=doctor,
                    appointment_date=apt_date,
                    appointment_time=apt_time,
                    status=AppointmentStatus.APPROVED,
                    doctor_notes=doctor_notes,
                )

            cd = lead.custom_data or {}
            cd["appointment_status"] = "Booking Confirmed"
            cd["appo_booked_date"] = apt_date.strftime("%Y-%m-%d")
            if apt_time:
                cd["appointment_time"] = apt_time.strftime("%I:%M %p") if hasattr(apt_time, "strftime") else str(apt_time)
            cd["appointment_confirmed_at"] = timezone.now().strftime("%Y-%m-%d %H:%M")
            if doctor_notes:
                cd["doctor_remark"] = doctor_notes
            lead.custom_data = cd
            lead.next_followup_date = None
            lead.save(update_fields=["custom_data", "next_followup_date"])

            if lead.assigned_to:
                time_disp = appointment.appointment_time.strftime("%I:%M %p") if hasattr(appointment.appointment_time, "strftime") else str(appointment.appointment_time or "Slot Not Set")
                Notification.objects.create(
                    user=lead.assigned_to,
                    title="Appointment Approved by Doctor",
                    message=f"Dr. {doctor.get_full_name() or doctor.username} confirmed appointment for patient {lead.name} on {apt_date.strftime('%d %b %Y')} at {time_disp}.",
                    link=f"/leads/{lead.pk}/",
                )
            messages.success(request, f"Appointment for {lead.name} confirmed successfully! Booking locked. ✅")

        return redirect("dashboard:doctor_appointments")

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




