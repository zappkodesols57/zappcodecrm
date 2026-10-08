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

def source_report(request):
    effective_hospital = _get_effective_hospital(request)
    rows = []
    for src in LeadSource.objects.all():
        leads_qs = Lead.objects.filter(lead_source=src, is_archived=False)
        if effective_hospital:
            leads_qs = leads_qs.filter(hospital=effective_hospital)
        total = leads_qs.count()
        if total == 0:
            continue
        interested = leads_qs.filter(temperature__in=["HOT", "WARM"]).count()
        visits = leads_qs.filter(stage__name__icontains="visit").count()
        admissions_qs = Admission.objects.filter(lead__lead_source=src)
        if effective_hospital:
            admissions_qs = admissions_qs.filter(lead__hospital=effective_hospital)
        admissions = admissions_qs.count()
        payments_qs = Payment.objects.filter(payment_status=PaymentStatus.SUCCESS, admission__lead__lead_source=src)
        if effective_hospital:
            payments_qs = payments_qs.filter(admission__lead__hospital=effective_hospital)
        revenue = payments_qs.aggregate(s=Sum("amount"))["s"] or 0
        rows.append({
            "source": src.name, "leads": total, "interested": interested, "visits": visits,
            "admissions": admissions, "conversion": round(admissions / total * 100, 1),
            "revenue": revenue,
        })
    rows.sort(key=lambda r: -r["leads"])
    return render(request, "dashboard/source_report.html", {
        "active": "reports_source",
        "rows": rows,
        "current_hospital": effective_hospital,
    })


@login_required


def campaign_report(request):
    effective_hospital = _get_effective_hospital(request)
    rows = []
    campaigns_qs = Campaign.objects.all()
    if effective_hospital:
        campaigns_qs = campaigns_qs.filter(hospital=effective_hospital)
    for camp in campaigns_qs:
        leads_qs = Lead.objects.filter(campaign=camp, is_archived=False)
        if effective_hospital:
            leads_qs = leads_qs.filter(hospital=effective_hospital)
        total = leads_qs.count()
        admissions_qs = Admission.objects.filter(lead__campaign=camp)
        if effective_hospital:
            admissions_qs = admissions_qs.filter(lead__hospital=effective_hospital)
        admissions = admissions_qs.count()
        payments_qs = Payment.objects.filter(payment_status=PaymentStatus.SUCCESS, admission__lead__campaign=camp)
        if effective_hospital:
            payments_qs = payments_qs.filter(admission__lead__hospital=effective_hospital)
        revenue = payments_qs.aggregate(s=Sum("amount"))["s"] or 0
        cost = float(camp.cost or 0)
        rows.append({
            "campaign": camp.name, "platform": camp.platform, "leads": total, "admissions": admissions,
            "revenue": revenue, "cost": cost,
            "cost_per_lead": round(cost / total, 2) if total else 0,
            "cost_per_admission": round(cost / admissions, 2) if admissions else 0,
            "conversion": round(admissions / total * 100, 1) if total else 0,
        })
    return render(request, "dashboard/campaign_report.html", {
        "active": "reports_campaign",
        "rows": rows,
        "current_hospital": effective_hospital,
    })


@login_required


def employee_report(request):
    from accounts.models import User
    effective_hospital = _get_effective_hospital(request)
    rows = []
    employees_qs = User.objects.filter(is_active_employee=True)
    if effective_hospital:
        employees_qs = employees_qs.filter(hospital=effective_hospital)
    for emp in employees_qs:
        leads_qs = Lead.objects.filter(assigned_to=emp, is_archived=False)
        if effective_hospital:
            leads_qs = leads_qs.filter(hospital=effective_hospital)
        total = leads_qs.count()
        if total == 0:
            continue
        admissions_qs = Admission.objects.filter(lead__assigned_to=emp)
        if effective_hospital:
            admissions_qs = admissions_qs.filter(lead__hospital=effective_hospital)
        admissions = admissions_qs.count()
        rows.append({
            "employee": emp.get_full_name() or emp.username, "leads": total,
            "admissions": admissions, "conversion": round(admissions / total * 100, 1),
        })
    rows.sort(key=lambda r: -r["leads"])
    return render(request, "dashboard/employee_report.html", {
        "active": "reports_employee",
        "rows": rows,
        "current_hospital": effective_hospital,
    })


@login_required


def employee_detail_activity(request, emp_id):
    from accounts.models import User
    from django.core.exceptions import PermissionDenied
    from datetime import datetime
    from django.shortcuts import get_object_or_404
    
    if request.user.role not in (User.Role.SUPER_ADMIN, User.Role.MANAGER):
        raise PermissionDenied("You do not have permission to view employee detailed activity.")
        
    employee = get_object_or_404(User, pk=emp_id)
    
    date_str = request.GET.get("date")
    if date_str:
        try:
            target_date = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
        except ValueError:
            target_date = timezone.localdate()
    else:
        target_date = timezone.localdate()
        
    from followups.models import FollowUp, Note
    followups = FollowUp.objects.filter(created_by=employee, followup_date=target_date).select_related("lead")
    notes = Note.objects.filter(created_by=employee, created_at__date=target_date).select_related("lead")
    
    entries = []
    for f in followups:
        entries.append({
            "type": "Follow-up",
            "time": f.created_at,
            "lead": f.lead,
            "details": f.get_followup_mode_display(),
            "status": f.get_followup_status_display(),
            "comment": f.comment,
        })
    for n in notes:
        entries.append({
            "type": "Note",
            "time": n.created_at,
            "lead": n.lead,
            "details": "Note added",
            "status": "—",
            "comment": n.note,
        })
    entries.sort(key=lambda x: x["time"], reverse=True)
    
    outgoing_calls = followups.filter(followup_mode="CALL_OUTGOING").count()
    incoming_calls = followups.filter(followup_mode="CALL_INCOMING").count()
    whatsapp = followups.filter(followup_mode="WHATSAPP").count()
    sms = followups.filter(followup_mode="SMS").count()
    email = followups.filter(followup_mode="EMAIL").count()
    
    stats = {
        "outgoing_calls": outgoing_calls,
        "incoming_calls": incoming_calls,
        "whatsapp": whatsapp,
        "sms": sms,
        "email": email,
        "notes": notes.count(),
        "total_entries": len(entries),
    }

    return render(request, "dashboard/employee_detail_activity.html", {
        "active": "reports_employee",
        "employee": employee,
        "target_date": target_date,
        "entries": entries,
        "stats": stats,
    })


@login_required


def submit_daily_report(request):
    from .forms import AcademyDailyReportForm, HospitalDailyReportForm, DailyReportForm
    from .models import DailyReport
    from followups.models import FollowUp
    from datetime import datetime, timedelta
    from admissions.models import Admission
    from payments.models import Payment, PaymentStatus
    from audit.models import AuditLog

    date_str = request.GET.get("date")
    if date_str:
        try:
            report_date = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
        except ValueError:
            report_date = timezone.localdate()
    else:
        report_date = timezone.localdate()

    # ── Check if already submitted today ──────────────────────────────────────
    report_instance = DailyReport.objects.filter(user=request.user, report_date=report_date).first()
    is_editing = request.GET.get('edit') == '1'

    if report_instance and not is_editing and request.method != "POST":
        # Already submitted → show confirmation page with option to edit
        return render(request, "dashboard/daily_report_done.html", {
            "active": "daily_report_submit",
            "report": report_instance,
            "report_date": report_date,
        })

    # ── Compute suggestions from today's actions ─────────────
    from followups.models import FollowUp, Note, Activity, ActivityType, FollowUpMode, FollowUpStatus
    import datetime as dt_module

    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(dt_module.datetime.combine(report_date, dt_module.time.min), tz)
    end_dt = timezone.make_aware(dt_module.datetime.combine(report_date, dt_module.time.max), tz)

    # 1. Total Leads Assigned (Captured + Assigned + Bulk Self Assigned + Created today)
    assigned_leads_qs = Lead.objects.filter(
        Q(assigned_to=request.user, inquiry_date=report_date) |
        Q(assigned_to=request.user, created_at__range=(start_dt, end_dt)) |
        Q(created_by=request.user, created_at__range=(start_dt, end_dt)) |
        Q(created_by=request.user, inquiry_date=report_date)
    )
    assigned_leads_ids = set(assigned_leads_qs.values_list('id', flat=True))

    assignment_activities_lead_ids = set(Activity.objects.filter(
        activity_type=ActivityType.ASSIGNMENT,
        created_at__range=(start_dt, end_dt)
    ).filter(
        Q(description__icontains=str(request.user.get_full_name() or request.user.username)) |
        Q(created_by=request.user)
    ).values_list('lead_id', flat=True))

    assignment_audit_lead_ids = set()
    for obj_id in AuditLog.objects.filter(
        action="ASSIGNMENT",
        created_at__range=(start_dt, end_dt),
        new_value__icontains=str(request.user.get_full_name() or request.user.username)
    ).values_list('object_id', flat=True):
        if obj_id and str(obj_id).isdigit():
            assignment_audit_lead_ids.add(int(obj_id))

    all_assigned_today_lead_ids = assigned_leads_ids | assignment_activities_lead_ids | assignment_audit_lead_ids
    leads_assigned_cnt = len(all_assigned_today_lead_ids)

    # 2. Appointments Booked (Confirmed / Scheduled / Completed appointments for today or created today)
    report_date_str = report_date.strftime("%Y-%m-%d")
    report_date_alt_str = report_date.strftime("%d-%m-%Y")
    
    appts_model_lead_ids = set(Appointment.objects.filter(
        Q(lead__assigned_to=request.user) | Q(created_by=request.user),
    ).filter(
        Q(appointment_date=report_date) | Q(created_at__range=(start_dt, end_dt))
    ).filter(
        status__in=[AppointmentStatus.APPROVED, AppointmentStatus.SCHEDULED, AppointmentStatus.COMPLETED]
    ).values_list('lead_id', flat=True))

    appts_leads_table_ids = set(Lead.objects.filter(
        assigned_to=request.user,
        is_archived=False,
    ).filter(
        Q(custom_data__appo_booked_date=report_date_str) |
        Q(custom_data__appo_booked_date=report_date_alt_str) |
        Q(custom_data__appointment_date=report_date_str) |
        Q(custom_data__appointment_date=report_date_alt_str) |
        Q(custom_data__appointment_confirmed_at__startswith=report_date_str)
    ).filter(
        Q(custom_data__appointment_status__icontains='Book') |
        Q(custom_data__appointment_status__icontains='Confirm') |
        Q(custom_data__appointment_status__icontains='Complete') |
        Q(custom_data__appointment_status__icontains='Done')
    ).values_list('id', flat=True))

    all_appts_lead_ids = appts_model_lead_ids | appts_leads_table_ids
    appointments_booked_cnt = len(all_appts_lead_ids)

    # 3. Payments Done Today (Leads whose stage became Payment Done or payment was received today)
    today_payment_lead_ids = set(Payment.objects.filter(
        Q(admission__lead__assigned_to=request.user) | Q(admission__assigned_counselor=request.user) | Q(admission__lead__created_by=request.user),
        payment_date=report_date,
        payment_status=PaymentStatus.SUCCESS
    ).values_list('admission__lead_id', flat=True))

    today_payment_stage_lead_ids = set(Lead.objects.filter(
        Q(assigned_to=request.user) | Q(created_by=request.user),
        updated_at__range=(start_dt, end_dt),
    ).filter(
        Q(stage__name__icontains="Payment") |
        Q(custom_data__deal_status__icontains="Payment") |
        Q(deal_status=DealStatus.WON)
    ).values_list('id', flat=True))

    all_payment_done_lead_ids = today_payment_lead_ids | today_payment_stage_lead_ids
    payments_done_cnt = len(all_payment_done_lead_ids)

    # 4. Follow-ups Taken Today (Unique leads with follow-up updates, remarks added, follow-ups added, stage updates today)
    user_fu_lead_ids = set(FollowUp.objects.filter(
        Q(created_by=request.user, followup_date=report_date) |
        Q(created_by=request.user, created_at__range=(start_dt, end_dt))
    ).values_list('lead_id', flat=True))

    user_note_lead_ids = set(Note.objects.filter(
        created_by=request.user,
        created_at__range=(start_dt, end_dt)
    ).values_list('lead_id', flat=True))

    user_activity_lead_ids = set(Activity.objects.filter(
        created_by=request.user,
        created_at__range=(start_dt, end_dt)
    ).values_list('lead_id', flat=True))

    user_audit_lead_ids = set()
    for obj_id in AuditLog.objects.filter(
        user=request.user,
        created_at__range=(start_dt, end_dt),
        model_name__iexact='Lead'
    ).values_list('object_id', flat=True):
        if obj_id and str(obj_id).isdigit():
            user_audit_lead_ids.add(int(obj_id))

    all_touched_today_lead_ids = user_fu_lead_ids | user_note_lead_ids | user_activity_lead_ids | user_audit_lead_ids
    follow_ups_taken_cnt = len(all_touched_today_lead_ids)
    calls_attended_cnt = follow_ups_taken_cnt

    # 5. Calls Breakdown
    day_followups = FollowUp.objects.filter(
        Q(created_by=request.user, followup_date=report_date) |
        Q(created_by=request.user, created_at__range=(start_dt, end_dt))
    )
    outgoing_calls_cnt = day_followups.filter(followup_mode=FollowUpMode.CALL_OUTGOING).count()
    incoming_calls_cnt = day_followups.filter(followup_mode=FollowUpMode.CALL_INCOMING).count()
    calls_not_connected_cnt = day_followups.filter(followup_status="NOT_CONNECTED").count()

    # 6. Interested Leads (Positive keywords added or temperature Hot/Warm updated today)
    leads_interested_cnt = Lead.objects.filter(
        Q(assigned_to=request.user) | Q(created_by=request.user),
        temperature__in=["WARM", "HOT"],
        updated_at__range=(start_dt, end_dt)
    ).count()

    # 7. Not Interested Leads (Negative remarks added or temperature Cold updated today)
    leads_cold_cnt = Lead.objects.filter(
        Q(assigned_to=request.user) | Q(created_by=request.user),
        temperature="COLD",
        updated_at__range=(start_dt, end_dt)
    ).count()

    # 8. Today's Lost / Cancelled Leads
    freeze_leads_cnt = Lead.objects.filter(
        Q(assigned_to=request.user) | Q(created_by=request.user),
        updated_at__range=(start_dt, end_dt)
    ).filter(
        Q(deal_status=DealStatus.LOST) |
        Q(temperature="FREEZE") |
        Q(custom_data__deal_status__icontains="Lost") |
        Q(custom_data__appointment_status__icontains="Cancel") |
        Q(custom_data__appointment_status__icontains="Reject")
    ).distinct().count()

    # 9. Today's Walk-in Leads
    leads_visited_cnt = Lead.objects.filter(
        Q(assigned_to=request.user) | Q(created_by=request.user),
        Q(inquiry_date=report_date) | Q(created_at__range=(start_dt, end_dt)) | Q(updated_at__range=(start_dt, end_dt))
    ).filter(
        Q(lead_source__name__icontains="walk") |
        Q(campaign__name__icontains="walk") |
        Q(custom_data__lead_source__icontains="walk") |
        Q(custom_data__campaign__icontains="walk") |
        Q(custom_data__appointment_status__icontains="Walk") |
        Q(custom_data__appointment_status__icontains="Visit")
    ).distinct().count()

    # Pending and Admissions calculations
    pending_followups_lead_ids = set(FollowUp.objects.filter(
        lead__assigned_to=request.user,
        followup_date__lte=report_date,
        followup_status=FollowUpStatus.PENDING
    ).values_list('lead_id', flat=True))

    pending_leads_table_ids = set(Lead.objects.filter(
        assigned_to=request.user,
        is_archived=False,
        next_followup_date__lte=report_date
    ).exclude(
        deal_status__in=["WON", "LOST"]
    ).values_list('id', flat=True))

    all_pending_leads_ids = pending_followups_lead_ids | pending_leads_table_ids
    pending_leads_cnt = len(all_pending_leads_ids)
    follow_ups_pending_cnt = pending_leads_cnt

    tomorrow_date = report_date + timedelta(days=1)
    tomorrow_fu_lead_ids = set(FollowUp.objects.filter(
        Q(lead__assigned_to=request.user) | Q(created_by=request.user),
        followup_date=tomorrow_date,
        followup_status=FollowUpStatus.PENDING
    ).values_list('lead_id', flat=True))

    tomorrow_leads_table_ids = set(Lead.objects.filter(
        assigned_to=request.user,
        is_archived=False,
        next_followup_date=tomorrow_date
    ).exclude(
        deal_status__in=["WON", "LOST"]
    ).values_list('id', flat=True))

    all_tomorrow_scheduled_lead_ids = tomorrow_fu_lead_ids | tomorrow_leads_table_ids
    tomorrow_followups_cnt = len(all_tomorrow_scheduled_lead_ids) + pending_leads_cnt

    today_adm_record_lead_ids = set(Admission.objects.filter(
        Q(lead__assigned_to=request.user) | Q(assigned_counselor=request.user),
        Q(admission_date=report_date) | Q(created_at__range=(start_dt, end_dt))
    ).values_list('lead_id', flat=True))

    today_adm_stage_lead_ids = set(Lead.objects.filter(
        assigned_to=request.user,
        updated_at__range=(start_dt, end_dt)
    ).filter(
        Q(stage__name__icontains="Admission") |
        Q(stage__name__icontains="Payment") |
        Q(admission_status="WON")
    ).values_list('id', flat=True))

    admissions_today_cnt = len(today_adm_record_lead_ids | today_adm_stage_lead_ids)
    fees_today_sum = Payment.objects.filter(
        Q(admission__lead__assigned_to=request.user) | Q(admission__assigned_counselor=request.user),
        payment_date=report_date,
        payment_status=PaymentStatus.SUCCESS
    ).aggregate(s=Sum("amount"))["s"] or 0

    # Login / Logout times from AuditLog
    first_login_log = AuditLog.objects.filter(
        user=request.user, 
        action="USER_LOGIN", 
        created_at__date=report_date
    ).order_by("created_at").first()
    
    if first_login_log:
        first_login_time = first_login_log.created_at
    elif request.user.last_login and request.user.last_login.date() == report_date:
        first_login_time = request.user.last_login
    else:
        earliest_log = AuditLog.objects.filter(user=request.user, created_at__date=report_date).order_by("created_at").first()
        first_login_time = earliest_log.created_at if earliest_log else timezone.now()

    last_logout_log = AuditLog.objects.filter(
        user=request.user, 
        action="USER_LOGOUT", 
        created_at__date=report_date
    ).order_by("-created_at").first()
    last_logout_time = last_logout_log.created_at if last_logout_log else None

    # ── Determine who this report will be sent to ─────────────
    # If user has reports_to set, send to them.
    # Default: send to Zappcode Super Admin(s).
    reports_to_user = request.user.reports_to
    recipients = []
    if reports_to_user and reports_to_user.is_active:
        recipients.append(reports_to_user)
    else:
        super_admins = User.objects.filter(role=User.Role.SUPER_ADMIN, is_active=True)
        if request.user.hospital:
            super_admins = super_admins.filter(hospital=request.user.hospital)
        else:
            super_admins = super_admins.filter(hospital__isnull=True)
            if not super_admins.exists():
                super_admins = User.objects.filter(role=User.Role.SUPER_ADMIN, is_active=True)
        for sa in super_admins:
            if sa != request.user and sa not in recipients:
                recipients.append(sa)

    # If user is a DOCTOR, calculate Doctor-specific appointment metrics
    if request.user.role == User.Role.DOCTOR:
        doctor_user = request.user
        doc_apts_qs = Appointment.objects.filter(
            Q(doctor_user=doctor_user) | 
            Q(doctor_name__icontains=doctor_user.get_full_name() or doctor_user.username)
        )
        if doctor_user.hospital:
            doc_apts_qs = doc_apts_qs.filter(hospital=doctor_user.hospital)

        # 1. Appointment requests received today (created_at on report_date)
        doc_req_received_cnt = doc_apts_qs.filter(created_at__date=report_date).count()

        # 2. Appointment accepted/approved today
        doc_accepted_cnt = doc_apts_qs.filter(
            Q(status__in=[AppointmentStatus.APPROVED, AppointmentStatus.SCHEDULED, AppointmentStatus.COMPLETED]),
            Q(updated_at__date=report_date) | Q(created_at__date=report_date)
        ).count()

        # 3. Today's scheduled appointments
        doc_today_scheduled_cnt = doc_apts_qs.filter(
            appointment_date=report_date
        ).exclude(status=AppointmentStatus.CANCELLED).count()

        # 4. Appointments cancelled (booked/requested but cancelled/rejected on report_date or for report_date)
        doc_cancelled_cnt = doc_apts_qs.filter(
            status=AppointmentStatus.CANCELLED
        ).filter(
            Q(appointment_date=report_date) | Q(updated_at__date=report_date)
        ).count()

        # 5. Appointments completed today
        doc_completed_cnt = doc_apts_qs.filter(
            status=AppointmentStatus.COMPLETED,
            appointment_date=report_date
        ).count()

        # 6. Tomorrow appointment scheduled
        tomorrow_date = report_date + timedelta(days=1)
        doc_tomorrow_scheduled_cnt = doc_apts_qs.filter(
            appointment_date=tomorrow_date
        ).exclude(status__in=[AppointmentStatus.CANCELLED, AppointmentStatus.PENDING_APPROVAL]).count()

        # Override counts for doctor suggestions
        leads_assigned_cnt = doc_req_received_cnt
        appointments_booked_cnt = doc_accepted_cnt
        pending_leads_cnt = doc_today_scheduled_cnt
        freeze_leads_cnt = doc_cancelled_cnt
        admissions_today_cnt = doc_completed_cnt
        tomorrow_followups_cnt = doc_tomorrow_scheduled_cnt

    suggestions = {
        "outgoing_calls": outgoing_calls_cnt,
        "incoming_calls": incoming_calls_cnt,
        "calls_attended": calls_attended_cnt,
        "calls_not_connected": calls_not_connected_cnt,
        "leads_assigned": leads_assigned_cnt,
        "appointments_booked": appointments_booked_cnt,
        "freeze_leads": freeze_leads_cnt,
        "follow_ups_taken": follow_ups_taken_cnt,
        "follow_ups_pending": follow_ups_pending_cnt,
        "pending_leads": pending_leads_cnt,
        "tomorrow_followups": tomorrow_followups_cnt,
        "leads_interested": leads_interested_cnt,
        "leads_cold": leads_cold_cnt,
        "leads_visited": leads_visited_cnt,
        "admissions_done": admissions_today_cnt,
        "payments_done": payments_done_cnt,
        "fees_collected": fees_today_sum,
        "mood": "Good",
        "first_login_time": first_login_time,
        "last_logout_time": last_logout_time,
    }

    # ── 1. BUSINESS TENANT CHECK & 2. ROLE CHECK ──────────────────────────────
    # Business Check first: 'hospital' vs 'academy' (Zappcode)
    # Then Role Check under the Business:
    user_business_type = request.user.business_type  # 'hospital' or 'academy'
    user_role = request.user.role

    if user_role == User.Role.DOCTOR:
        is_doctor_form = True
        is_hospital_form = False
        from .forms import DoctorDailyReportForm
        FormClass = DoctorDailyReportForm
        template_name = "dashboard/doctor_daily_report_form.html"
    elif user_business_type == "hospital" and user_role in (User.Role.LEAD_ATTENDENT, User.Role.ADMIN, User.Role.MANAGER):
        is_doctor_form = False
        is_hospital_form = True
        FormClass = HospitalDailyReportForm
        template_name = "hospital/dashboard/daily_report_form.html"
    else:
        # Zappcode Academy Business (Counsellor, HR, Manager, Admin, Super Admin)
        is_doctor_form = False
        is_hospital_form = False
        FormClass = AcademyDailyReportForm
        template_name = "academy/dashboard/reports_form.html"

    if request.method == "POST":
        from django.db import IntegrityError, transaction
        from notifications.models import Notification

        form = FormClass(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    cleaned = form.cleaned_data
                    
                    mood_val = cleaned.get("mood") or "Good"
                    mood_to_rating = {"Great": 5, "Good": 4, "Moderate": 3, "Tired": 2, "Exhausted": 1, "Sick": 1}
                    mood_rating_val = mood_to_rating.get(mood_val, cleaned.get("mood_rating") or 3)

                    # Store exact values entered/edited by user
                    report_data = {
                        "leads_assigned": cleaned.get("leads_assigned") if cleaned.get("leads_assigned") is not None else leads_assigned_cnt,
                        "appointments_booked": cleaned.get("appointments_booked") if cleaned.get("appointments_booked") is not None else appointments_booked_cnt,
                        "freeze_leads": cleaned.get("freeze_leads") if cleaned.get("freeze_leads") is not None else freeze_leads_cnt,
                        "calls_attended": cleaned.get("calls_attended") if cleaned.get("calls_attended") is not None else calls_attended_cnt,
                        "outgoing_calls": cleaned.get("outgoing_calls") if cleaned.get("outgoing_calls") is not None else outgoing_calls_cnt,
                        "incoming_calls": cleaned.get("incoming_calls") if cleaned.get("incoming_calls") is not None else incoming_calls_cnt,
                        "calls_not_connected": cleaned.get("calls_not_connected") if cleaned.get("calls_not_connected") is not None else calls_not_connected_cnt,
                        "follow_ups_taken": cleaned.get("follow_ups_taken") if cleaned.get("follow_ups_taken") is not None else follow_ups_taken_cnt,
                        "follow_ups_pending": cleaned.get("follow_ups_pending") if cleaned.get("follow_ups_pending") is not None else follow_ups_pending_cnt,
                        "pending_leads": cleaned.get("pending_leads") if cleaned.get("pending_leads") is not None else pending_leads_cnt,
                        "tomorrow_followups": cleaned.get("tomorrow_followups") if cleaned.get("tomorrow_followups") is not None else tomorrow_followups_cnt,
                        "leads_cold": cleaned.get("leads_cold") if cleaned.get("leads_cold") is not None else leads_cold_cnt,
                        "leads_interested": cleaned.get("leads_interested") if cleaned.get("leads_interested") is not None else leads_interested_cnt,
                        "leads_visited": cleaned.get("leads_visited") if cleaned.get("leads_visited") is not None else leads_visited_cnt,
                        "admissions_done": cleaned.get("admissions_done") if cleaned.get("admissions_done") is not None else admissions_today_cnt,
                        "payments_done": cleaned.get("payments_done") if cleaned.get("payments_done") is not None else payments_done_cnt,
                        "fees_collected": cleaned.get("fees_collected") if cleaned.get("fees_collected") is not None else fees_today_sum,
                        "key_highlight": cleaned.get("key_highlight") or "",
                        "challenges_faced": cleaned.get("challenges_faced") or "",
                        "tomorrow_priority": cleaned.get("tomorrow_priority") or "",
                        "other_updates": cleaned.get("other_updates") or "",
                        "mood": mood_val,
                        "mood_rating": mood_rating_val,
                        "first_login_at": first_login_time,
                        "last_logout_at": last_logout_time,
                    }
                    
                    report, created = DailyReport.objects.update_or_create(
                        user=request.user,
                        report_date=report_date,
                        defaults=report_data
                    )
                    
                    # Send Notifications to recipient (Reports To / Admin)
                    action_word = "submitted" if created else "updated"
                    user_display_name = request.user.get_full_name() or request.user.username
                    user_role_display = request.user.get_role_display()
                    submitter_info = f"{user_display_name} ({user_role_display})"

                    if request.user.role == User.Role.DOCTOR:
                        summary_text = (
                            f"Appointment Requests Received: {report.leads_assigned} | "
                            f"Appointments Accepted: {report.appointments_booked} | "
                            f"Today's Scheduled: {report.pending_leads} | "
                            f"Appointments Cancelled: {report.freeze_leads} | "
                            f"Appointments Completed: {report.admissions_done} | "
                            f"Tomorrow Scheduled: {report.tomorrow_followups}"
                        )
                    else:
                        summary_text = (
                            f"Assigned Leads: {report.leads_assigned} | Calls: {report.calls_attended} | "
                            f"Admissions Done: {report.admissions_done} | Payments: {report.payments_done} (₹{report.fees_collected}) | "
                            f"Pending Leads: {report.pending_leads} | Tomorrow FU: {report.tomorrow_followups}"
                        )

                    for r_user in recipients:
                        Notification.objects.create(
                            user=r_user,
                            title=f"EOD Report ({action_word.capitalize()}) - {submitter_info}",
                            message=(
                                f"Employee: {submitter_info}\n"
                                f"Date: {report_date.strftime('%d %b %Y')}\n"
                                f"{summary_text}\n"
                                f"Mood: {report.mood_display}"
                            ),
                            link="/dashboard/reports/daily/",
                        )

                messages.success(request, f"Daily EOD report for {report_date.strftime('%d-%m-%Y')} {'submitted' if created else 'updated'} successfully! ✅")
                return redirect("dashboard:submit_daily_report")
            except Exception as e:
                messages.error(request, f"Error saving report: {str(e)}")
                return redirect("dashboard:submit_daily_report")
    else:
        if report_instance:
            init_data = {
                "leads_assigned": report_instance.leads_assigned,
                "calls_attended": report_instance.calls_attended,
                "outgoing_calls": report_instance.outgoing_calls,
                "incoming_calls": report_instance.incoming_calls,
                "calls_not_connected": report_instance.calls_not_connected,
                "follow_ups_taken": report_instance.follow_ups_taken,
                "follow_ups_pending": report_instance.follow_ups_pending,
                "pending_leads": report_instance.pending_leads,
                "tomorrow_followups": report_instance.tomorrow_followups,
                "leads_cold": report_instance.leads_cold,
                "leads_interested": report_instance.leads_interested,
                "leads_visited": report_instance.leads_visited,
                "admissions_done": report_instance.admissions_done,
                "payments_done": report_instance.payments_done,
                "fees_collected": report_instance.fees_collected,
                "key_highlight": report_instance.key_highlight,
                "challenges_faced": report_instance.challenges_faced,
                "tomorrow_priority": report_instance.tomorrow_priority,
                "other_updates": report_instance.other_updates,
                "mood": report_instance.mood or "Good",
                "mood_rating": report_instance.mood_rating,
            }
            if is_hospital_form or is_doctor_form:
                init_data["appointments_booked"] = report_instance.appointments_booked
                init_data["freeze_leads"] = report_instance.freeze_leads
        else:
            init_data = {
                "leads_assigned": suggestions["leads_assigned"],
                "calls_attended": suggestions["calls_attended"],
                "outgoing_calls": suggestions["outgoing_calls"],
                "incoming_calls": suggestions["incoming_calls"],
                "calls_not_connected": suggestions["calls_not_connected"],
                "follow_ups_taken": suggestions["follow_ups_taken"],
                "follow_ups_pending": suggestions["follow_ups_pending"],
                "pending_leads": suggestions["pending_leads"],
                "tomorrow_followups": suggestions["tomorrow_followups"],
                "leads_interested": suggestions["leads_interested"],
                "leads_cold": suggestions["leads_cold"],
                "leads_visited": suggestions["leads_visited"],
                "admissions_done": suggestions["admissions_done"],
                "payments_done": suggestions["payments_done"],
                "fees_collected": suggestions["fees_collected"],
                "mood": suggestions["mood"],
            }
            if is_hospital_form or is_doctor_form:
                init_data["appointments_booked"] = suggestions["appointments_booked"]
                init_data["freeze_leads"] = suggestions["freeze_leads"]

        form = FormClass(initial=init_data)

    return render(request, template_name, {
        "active": "daily_report_submit",
        "form": form,
        "suggestions": suggestions,
        "report_date": report_date,
        "first_login_time": first_login_time,
        "reports_to_user": reports_to_user,
        "recipients": recipients,
        "existing": report_instance is not None,
    })


@login_required


def export_daily_activity_leads(request):
    """
    Export all unique leads touched or updated by the user today (or specified date) to an Excel (.xlsx) file.
    Includes:
    - Inquiry Date, Name, Contact, Alternate Mobile, Email, Course / Purpose, City, Location
    - Action by User today (from Timeline Activity / AuditLog)
    - Follow-ups Count, Follow-up Dates list, Remarks list
    - Latest Follow-up Date, Latest Follow-up Remark, Latest Follow-up Status
    - Lead Temperature, Lead Stage, Admission Status, Payment Status
    """
    import io
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from datetime import datetime
    from followups.models import FollowUp, Note, Activity, ActivityType
    from audit.models import AuditLog
    from admissions.models import Admission

    date_str = request.GET.get("date")
    if date_str:
        try:
            report_date = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
        except ValueError:
            report_date = timezone.localdate()
    else:
        report_date = timezone.localdate()

    # 1. Collect all unique lead IDs assigned to or touched by this user on report_date
    import datetime as dt_module
    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(dt_module.datetime.combine(report_date, dt_module.time.min), tz)
    end_dt = timezone.make_aware(dt_module.datetime.combine(report_date, dt_module.time.max), tz)

    assigned_leads_qs = Lead.objects.filter(
        Q(assigned_to=request.user, inquiry_date=report_date) |
        Q(assigned_to=request.user, created_at__range=(start_dt, end_dt)) |
        Q(created_by=request.user, created_at__range=(start_dt, end_dt)) |
        Q(created_by=request.user, inquiry_date=report_date)
    )
    assigned_lead_ids = set(assigned_leads_qs.values_list('id', flat=True))

    assignment_activities_lead_ids = set(Activity.objects.filter(
        activity_type=ActivityType.ASSIGNMENT,
        created_at__range=(start_dt, end_dt)
    ).filter(
        Q(description__icontains=str(request.user.get_full_name() or request.user.username)) |
        Q(created_by=request.user)
    ).values_list('lead_id', flat=True))

    assignment_audit_lead_ids = set()
    for obj_id in AuditLog.objects.filter(
        action="ASSIGNMENT",
        created_at__range=(start_dt, end_dt),
        new_value__icontains=str(request.user.get_full_name() or request.user.username)
    ).values_list('object_id', flat=True):
        if obj_id and str(obj_id).isdigit():
            assignment_audit_lead_ids.add(int(obj_id))

    user_fu_lead_ids = set(FollowUp.objects.filter(
        Q(created_by=request.user, followup_date=report_date) |
        Q(created_by=request.user, created_at__range=(start_dt, end_dt))
    ).values_list('lead_id', flat=True))

    user_note_lead_ids = set(Note.objects.filter(
        created_by=request.user,
        created_at__range=(start_dt, end_dt)
    ).values_list('lead_id', flat=True))

    user_activity_lead_ids = set(Activity.objects.filter(
        created_by=request.user,
        created_at__range=(start_dt, end_dt)
    ).values_list('lead_id', flat=True))

    user_audit_lead_ids = set()
    for obj_id in AuditLog.objects.filter(
        user=request.user,
        created_at__range=(start_dt, end_dt),
        model_name__iexact='Lead'
    ).values_list('object_id', flat=True):
        if obj_id and str(obj_id).isdigit():
            user_audit_lead_ids.add(int(obj_id))

    today_adm_record_lead_ids = set(Admission.objects.filter(
        Q(lead__assigned_to=request.user) | Q(assigned_counselor=request.user),
        Q(admission_date=report_date) | Q(created_at__range=(start_dt, end_dt))
    ).values_list('lead_id', flat=True))

    # All unique lead IDs
    all_target_lead_ids = (
        assigned_lead_ids |
        assignment_activities_lead_ids |
        assignment_audit_lead_ids |
        user_fu_lead_ids |
        user_note_lead_ids |
        user_activity_lead_ids |
        user_audit_lead_ids |
        today_adm_record_lead_ids
    )

    leads = Lead.objects.filter(id__in=all_target_lead_ids).select_related(
        'course', 'stage', 'assigned_to', 'created_by', 'admission'
    ).prefetch_related('followups', 'lead_notes', 'activities').order_by('-updated_at')

    # Create openpyxl workbook
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Today Activity Leads"

    # Styling definitions
    title_font = Font(name="Calibri", size=15, bold=True, color="1E3A8A")
    meta_font = Font(name="Calibri", size=10, italic=True, color="475569")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1E40AF", end_color="1E40AF", fill_type="solid")
    alt_row_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    regular_font = Font(name="Calibri", size=10, color="1E293B")
    border_thin = Side(border_style="thin", color="CBD5E1")
    cell_border = Border(left=border_thin, right=border_thin, top=border_thin, bottom=border_thin)
    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left_align = Alignment(horizontal="left", vertical="center", wrap_text=True)

    # Title & Metadata block
    ws.merge_cells("A1:P1")
    ws["A1"] = f"EOD Activity Leads Report - {request.user.get_full_name() or request.user.username}"
    ws["A1"].font = title_font
    ws["A1"].alignment = Alignment(horizontal="left", vertical="center")

    ws.merge_cells("A2:P2")
    ws["A2"] = f"Report Date: {report_date.strftime('%d-%b-%Y')}  |  Total Unique Leads: {leads.count()}  |  Generated on: {timezone.now().strftime('%d-%b-%Y %I:%M %p')}"
    ws["A2"].font = meta_font
    ws["A2"].alignment = Alignment(horizontal="left", vertical="center")

    headers = [
        "SR NO",
        "INQUIRY DATE",
        "LEAD CODE",
        "STUDENT / LEAD NAME",
        "CONTACT NUMBER",
        "COURSE / PURPOSE",
        "CITY / LOCATION",
        "ACTIONS BY USER TODAY (TIMELINE)",
        "TOTAL FOLLOW-UPS",
        "ALL FOLLOW-UP REMARKS & DATES",
        "LATEST FOLLOW-UP DATE",
        "LATEST FOLLOW-UP STATUS",
        "LATEST REMARK / NOTE",
        "LEAD TEMPERATURE",
        "LEAD STAGE",
        "ADMISSION / PAYMENT STATUS"
    ]

    # Write header row (Row 4)
    header_row_idx = 4
    for col_idx, header_text in enumerate(headers, start=1):
        cell = ws.cell(row=header_row_idx, column=col_idx, value=header_text)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center_align
        cell.border = cell_border
    ws.row_dimensions[header_row_idx].height = 28

    # Populate Data
    for row_num, lead in enumerate(leads, start=1):
        curr_row = header_row_idx + row_num

        # 1. Action by user today
        today_acts = lead.activities.filter(
            Q(created_by=request.user) | Q(created_at__range=(start_dt, end_dt)),
            created_at__range=(start_dt, end_dt)
        ).order_by('created_at')
        actions_list = [f"• {act.get_activity_type_display()}: {act.description}" for act in today_acts]
        if not actions_list:
            if lead.assigned_to == request.user:
                actions_list.append("• Assigned to user")
            if lead.created_by == request.user:
                actions_list.append("• Created/Captured by user")
        actions_str = "\n".join(actions_list) if actions_list else "Updated today"

        # 2. Follow-ups
        all_fus = list(lead.followups.all().order_by('followup_date', 'created_at'))
        fu_count = len(all_fus)
        all_fu_details = []
        for fu in all_fus:
            dt_str = fu.followup_date.strftime("%d/%m/%Y") if fu.followup_date else ""
            cm_str = f" - {fu.comment}" if fu.comment else ""
            all_fu_details.append(f"[{dt_str} | {fu.get_followup_mode_display()} | {fu.get_followup_status_display()}]{cm_str}")
        all_fu_str = "\n".join(all_fu_details) if all_fu_details else "No followups recorded"

        latest_fu = all_fus[-1] if all_fus else None
        latest_fu_date_str = latest_fu.followup_date.strftime("%d-%b-%Y") if (latest_fu and latest_fu.followup_date) else (lead.next_followup_date.strftime("%d-%b-%Y") if lead.next_followup_date else "-")
        latest_fu_status_str = latest_fu.get_followup_status_display() if latest_fu else "-"
        
        # Latest Note / Remark
        latest_note = lead.lead_notes.order_by('-created_at').first()
        latest_remark_str = latest_note.note if latest_note else (latest_fu.comment if (latest_fu and latest_fu.comment) else (lead.notes or "-"))

        # Course / City info
        course_name = lead.course.name if lead.course else (lead.custom_data.get('department') or lead.lead_type or "-")
        city_loc = ", ".join(filter(None, [lead.city, lead.location])) or "-"

        # Admission & Payment status
        adm_status_list = []
        if hasattr(lead, 'admission') and lead.admission:
            adm = lead.admission
            adm_status_list.append(f"Admitted ({adm.course.name if adm.course else ''})")
            if adm.collected > 0:
                adm_status_list.append(f"Paid: ₹{adm.collected:,.0f}")
            else:
                adm_status_list.append("Payment Pending")
        else:
            adm_status_list.append(lead.get_admission_status_display() or "Open")
            if lead.stage and "payment" in lead.stage.name.lower():
                adm_status_list.append(lead.stage.name)
        adm_payment_str = " | ".join(adm_status_list)

        row_data = [
            row_num,
            lead.inquiry_date.strftime("%d-%b-%Y") if lead.inquiry_date else "",
            lead.lead_code,
            lead.name,
            lead.mobile,
            course_name,
            city_loc,
            actions_str,
            fu_count,
            all_fu_str,
            latest_fu_date_str,
            latest_fu_status_str,
            latest_remark_str,
            lead.get_temperature_display() if hasattr(lead, 'get_temperature_display') else lead.temperature,
            lead.stage.name if lead.stage else "-",
            adm_payment_str
        ]

        is_even = (row_num % 2 == 0)
        for col_idx, val in enumerate(row_data, start=1):
            cell = ws.cell(row=curr_row, column=col_idx, value=val)
            cell.font = regular_font
            cell.border = cell_border
            if is_even:
                cell.fill = alt_row_fill
            if col_idx in (1, 2, 3, 5, 9, 11, 12, 14):
                cell.alignment = center_align
            else:
                cell.alignment = left_align

        ws.row_dimensions[curr_row].height = 45

    # Auto-adjust column widths
    col_widths = {
        1: 8,   # SR NO
        2: 14,  # INQUIRY DATE
        3: 16,  # LEAD CODE
        4: 22,  # NAME
        5: 16,  # CONTACT
        6: 22,  # COURSE
        7: 20,  # CITY
        8: 36,  # ACTIONS TODAY
        9: 14,  # FU COUNT
        10: 40, # ALL FU REMARKS
        11: 18, # LATEST FU DATE
        12: 18, # LATEST FU STATUS
        13: 30, # LATEST REMARK
        14: 16, # TEMPERATURE
        15: 18, # STAGE
        16: 26, # ADMISSION / PAYMENT STATUS
    }
    for col_idx, width in col_widths.items():
        col_letter = get_column_letter(col_idx)
        ws.column_dimensions[col_letter].width = width

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f"EOD_Activity_Leads_{request.user.username}_{report_date.strftime('%Y%m%d')}.xlsx"
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
def download_eod_report_pdf(request):
    """
    Download EOD Report as a clean, professionally formatted PDF.
    """
    import io
    try:
        from reportlab.lib.pagesizes import letter, A4
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    except ImportError:
        messages.error(request, "PDF generator is temporarily unavailable. Please try again or contact administrator.")
        return redirect("dashboard:submit_daily_report")

    from dashboard.models import DailyReport
    from datetime import datetime

    date_str = request.GET.get("date")
    if date_str:
        try:
            report_date = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
        except ValueError:
            report_date = timezone.localdate()
    else:
        report_date = timezone.localdate()

    report = DailyReport.objects.filter(user=request.user, report_date=report_date).first()
    if not report:
        messages.error(request, "No EOD report found for the selected date.")
        return redirect("dashboard:submit_daily_report")

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    story = []
    styles = getSampleStyleSheet()

    # Colors
    NAVY = colors.HexColor("#0f2744")
    DARK_BLUE = colors.HexColor("#1e3a8a")
    TEXT_DARK = colors.HexColor("#1e293b")
    TEXT_MUTED = colors.HexColor("#64748b")
    BORDER_COLOR = colors.HexColor("#e2e8f0")
    BG_LIGHT = colors.HexColor("#f8fafc")

    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=18,
        leading=22,
        textColor=NAVY,
    )

    sub_style = ParagraphStyle(
        'DocSub',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=14,
        textColor=TEXT_MUTED,
    )

    sec_header_style = ParagraphStyle(
        'SecHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=11,
        leading=14,
        textColor=NAVY,
    )

    cell_label_style = ParagraphStyle(
        'CellLabel',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=10,
        textColor=TEXT_MUTED,
    )

    cell_val_style = ParagraphStyle(
        'CellVal',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=15,
        textColor=TEXT_DARK,
    )

    body_text_style = ParagraphStyle(
        'BodyText',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=13,
        textColor=TEXT_DARK,
    )

    # Header section
    user_name = request.user.get_full_name() or request.user.username
    biz_name = request.user.hospital.name if request.user.hospital else "Hospital CRM"
    date_display = report_date.strftime("%A, %d %B %Y")

    header_table_data = [
        [
            Paragraph(f"<b>{biz_name}</b><br/><font size='14'>Daily EOD Report</font>", title_style),
            Paragraph(f"<b>Date:</b> {date_display}<br/><b>Staff:</b> {user_name} ({request.user.get_role_display()})", sub_style)
        ]
    ]
    header_table = Table(header_table_data, colWidths=[300, 220])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1.5, color=NAVY, spaceBefore=4, spaceAfter=14))

    # Metric Table (3 fields per row)
    metrics = [
        ("Total Leads Assigned", str(report.leads_assigned)),
        ("Appointments Booked", str(report.appointments_booked)),
        ("Payments Done", str(report.payments_done)),
        ("Follow-ups Taken", str(report.follow_ups_taken)),
        ("Interested Leads", str(report.leads_interested)),
        ("Not Interested Leads", str(report.leads_cold)),
        ("Today's Lost Leads", str(report.freeze_leads)),
        ("Today's Walk-in Leads", str(report.leads_visited)),
        ("Total Calls Attended", str(report.calls_attended)),
        ("Incoming Calls", str(report.incoming_calls)),
        ("Outgoing Calls", str(report.outgoing_calls)),
        ("Calls Not Connected", str(report.calls_not_connected)),
    ]

    grid_data = []
    for i in range(0, len(metrics), 3):
        row_cells = []
        for j in range(3):
            if i + j < len(metrics):
                lbl, val = metrics[i + j]
                content = [
                    Paragraph(lbl, cell_label_style),
                    Spacer(1, 2),
                    Paragraph(val, cell_val_style)
                ]
                row_cells.append(content)
            else:
                row_cells.append("")
        grid_data.append(row_cells)

    metric_table = Table(grid_data, colWidths=[173, 173, 174])
    metric_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), BG_LIGHT),
        ('BOX', (0, 0), (-1, -1), 1, BORDER_COLOR),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(metric_table)
    story.append(Spacer(1, 14))

    # Notes & Summary Section
    notes_data = [
        [Paragraph("<b>Today's Mood:</b>", cell_label_style), Paragraph(report.mood_display, body_text_style)],
        [Paragraph("<b>Remarks / Notes:</b>", cell_label_style), Paragraph(report.challenges_faced or "—", body_text_style)],
        [Paragraph("<b>Tomorrow's Plan:</b>", cell_label_style), Paragraph(report.tomorrow_priority or "—", body_text_style)],
    ]
    notes_table = Table(notes_data, colWidths=[130, 390])
    notes_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.white),
        ('BOX', (0, 0), (-1, -1), 1, BORDER_COLOR),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    story.append(notes_table)
    story.append(Spacer(1, 14))

    # Footer note
    footer_text = Paragraph(
        f"<font size='8' color='#94a3b8'>Generated automatically by CRM System on {timezone.now().strftime('%d-%b-%Y %I:%M %p')}</font>",
        ParagraphStyle('Footer', parent=styles['Normal'], alignment=TA_CENTER)
    )
    story.append(footer_text)

    doc.build(story)
    buffer.seek(0)

    pdf_filename = f"EOD_Report_{request.user.username}_{report_date.strftime('%Y%m%d')}.pdf"
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{pdf_filename}"'
    return response


@login_required


def management_daily_reports(request):
    from accounts.models import User
    from django.core.exceptions import PermissionDenied
    from .models import DailyReport
    from datetime import datetime
    import pandas as pd
    from django.http import HttpResponse
    
    user = request.user
    if user.role not in (User.Role.SUPER_ADMIN, User.Role.MANAGER, User.Role.ADMIN) and not user.is_superuser:
        raise PermissionDenied("You do not have permission to view this report log.")
        
    reports = DailyReport.objects.select_related("user").all()
    
    effective_hospital = _get_effective_hospital(request)
    if effective_hospital:
        reports = reports.filter(user__hospital=effective_hospital)
        
    # If user is a MANAGER and not Super Admin, show reports of users who report to this manager + themselves
    if user.role == User.Role.MANAGER and not user.is_superuser:
        team_members = User.objects.filter(Q(reports_to=user) | Q(pk=user.pk))
        reports = reports.filter(user__in=team_members)
    
    # Apply Filters
    emp_id = request.GET.get("employee")
    if emp_id:
        reports = reports.filter(user_id=emp_id)
        
    date_from_str = request.GET.get("date_from")
    date_to_str = request.GET.get("date_to")
    
    has_date_filter = False
    if date_from_str:
        try:
            date_from = datetime.strptime(date_from_str.strip(), "%Y-%m-%d").date()
            reports = reports.filter(report_date__gte=date_from)
            has_date_filter = True
        except ValueError:
            pass
            
    if date_to_str:
        try:
            date_to = datetime.strptime(date_to_str.strip(), "%Y-%m-%d").date()
            reports = reports.filter(report_date__lte=date_to)
            has_date_filter = True
        except ValueError:
            pass

    # Option 3: By default (when no specific date filter is applied), show only the latest 1 entry per user
    if not has_date_filter and not emp_id:
        from django.db.models import Max
        latest_report_ids = DailyReport.objects.filter(
            id__in=reports.values_list('id', flat=True)
        ).values('user_id').annotate(max_id=Max('id')).values_list('max_id', flat=True)
        reports = reports.filter(id__in=latest_report_ids).order_by('-report_date', '-id')
    else:
        reports = reports.order_by('-report_date', '-id')

    if "export" in request.GET:
        rows = []
        for r in reports:
            rows.append({
                "Date": r.report_date.strftime("%d-%m-%Y"),
                "Employee": r.user.get_full_name() or r.user.username,
                "Role": r.user.get_role_display(),
                "Reports To": (r.user.reports_to.get_full_name() or r.user.reports_to.username) if r.user.reports_to else "Admin",
                "First Login": r.first_login_at.strftime("%I:%M %p") if r.first_login_at else "—",
                "Last Logout": r.last_logout_at.strftime("%I:%M %p") if r.last_logout_at else "—",
                "Leads Assigned": r.leads_assigned,
                "Calls / Touches": r.calls_attended,
                "Admissions Done": r.admissions_done,
                "Payments Done": r.payments_done,
                "Fees Collected (₹)": float(r.fees_collected),
                "Pending Leads": r.pending_leads,
                "Tomorrow Follow-ups": r.tomorrow_followups,
                "Follow-ups Taken": r.follow_ups_taken,
                "Appointments Booked": r.appointments_booked,
                "Freeze Leads": r.freeze_leads,
                "Key Highlight": r.key_highlight,
                "Challenges Faced": r.challenges_faced,
                "Tomorrow Priority": r.tomorrow_priority,
                "Mood": r.mood_display,
            })
        df = pd.DataFrame(rows)
        response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        response["Content-Disposition"] = f'attachment; filename="daily_reports_{timezone.now().strftime("%Y%m%d_%H%M")}.xlsx"'
        df.to_excel(response, index=False, sheet_name="Daily Reports")
        return response
        
    # Get active/approved employees for filter dropdown
    employees = User.objects.filter(is_active=True, is_approved=True)
    if effective_hospital:
        employees = employees.filter(hospital=effective_hospital)
    if user.role == User.Role.MANAGER and not user.is_superuser:
        employees = employees.filter(Q(reports_to=user) | Q(pk=user.pk))
    
    return render(request, "dashboard/daily_reports_list.html", {
        "active": "reports_daily",
        "reports": reports,
        "employees": employees,
        "request_get": request.GET,
        "current_hospital": effective_hospital,
    })

@login_required


def task_list_view(request):
    user = request.user
    
    # Get user's tasks or hospital admin view
    if user.hospital and (user.role == 'SUPER_ADMIN' or user.role == 'MANAGER'):
        # Admin can view all hospital tasks or filter
        tasks = TaskReminder.objects.filter(user__hospital=user.hospital)
    else:
        tasks = TaskReminder.objects.filter(user=user)
        
    # Filter by Status
    status_filter = request.GET.get('status', '').strip()
    if status_filter:
        tasks = tasks.filter(status=status_filter)
        
    # Filter by Priority
    priority_filter = request.GET.get('priority', '').strip()
    if priority_filter:
        tasks = tasks.filter(priority=priority_filter)
        
    # Search query
    q = request.GET.get('q', '').strip()
    if q:
        tasks = tasks.filter(
            Q(title__icontains=q) |
            Q(description__icontains=q) |
            Q(lead__name__icontains=q) |
            Q(lead__mobile__icontains=q)
        )
        
    # Stats
    total_tasks = tasks.count()
    pending_tasks = tasks.filter(status=TaskReminder.Status.PENDING).count()
    completed_tasks = tasks.filter(status=TaskReminder.Status.COMPLETED).count()
    urgent_tasks = tasks.filter(priority__in=[TaskReminder.Priority.HIGH, TaskReminder.Priority.URGENT], status__in=[TaskReminder.Status.PENDING, TaskReminder.Status.IN_PROGRESS]).count()
    
    # Sorting order:
    # 1. Latest due_date first (-due_date)
    # 2. Priority: Urgent (1) -> High (2) -> Medium (3) -> Low (4)
    # 3. Status: Pending (1) -> In Progress (2) -> Completed (3) -> Cancelled (4)
    # 4. Due time (due_time) & recently created (-created_at)
    priority_order = Case(
        When(priority=TaskReminder.Priority.URGENT, then=Value(1)),
        When(priority=TaskReminder.Priority.HIGH, then=Value(2)),
        When(priority=TaskReminder.Priority.MEDIUM, then=Value(3)),
        When(priority=TaskReminder.Priority.LOW, then=Value(4)),
        default=Value(5),
        output_field=IntegerField(),
    )
    status_order = Case(
        When(status=TaskReminder.Status.PENDING, then=Value(1)),
        When(status=TaskReminder.Status.IN_PROGRESS, then=Value(2)),
        When(status=TaskReminder.Status.COMPLETED, then=Value(3)),
        When(status=TaskReminder.Status.CANCELLED, then=Value(4)),
        default=Value(5),
        output_field=IntegerField(),
    )
    tasks = tasks.annotate(
        priority_order=priority_order,
        status_order=status_order,
    ).order_by('-due_date', 'priority_order', 'status_order', 'due_time', '-created_at')

    # Pagination with dynamic page_size
    page_size = request.GET.get('page_size', '20').strip()
    try:
        page_size = int(page_size)
        if page_size not in [10, 20, 50, 100]:
            page_size = 20
    except ValueError:
        page_size = 20

    paginator = Paginator(tasks, page_size)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1) if hasattr(paginator, 'get_elided_page_range') else paginator.page_range

    query_params = request.GET.copy()
    if 'page' in query_params:
        del query_params['page']
        
    # Leads for dropdown search/selection in modal
    user_leads = Lead.objects.filter(is_archived=False)
    if user.hospital:
        user_leads = user_leads.filter(hospital=user.hospital)
    if user.role == 'LEAD_ATTENDENT':
        user_leads = user_leads.filter(assigned_to=user)
    user_leads = user_leads.order_by('-updated_at')[:50]

    # Allowed assignment roles for tasks
    allowed_roles = [User.Role.MANAGER, User.Role.DOCTOR, User.Role.LEAD_ATTENDENT]

    # Eligible assignable users for Admin/Manager
    assignable_users = User.objects.filter(is_active=True, is_approved=True, role__in=allowed_roles)
    if user.hospital:
        assignable_users = assignable_users.filter(hospital=user.hospital)
    assignable_users = assignable_users.order_by('role', 'first_name', 'username')

    # Available role choices for filter buttons
    role_choices = [(r.value, r.label) for r in User.Role if r in allowed_roles]

    context = {
        'page_obj': page_obj,
        'tasks': page_obj,
        'page_range': page_range,
        'query_params': query_params.urlencode(),
        'total_tasks': total_tasks,
        'pending_tasks': pending_tasks,
        'completed_tasks': completed_tasks,
        'urgent_tasks': urgent_tasks,
        'status_filter': status_filter,
        'priority_filter': priority_filter,
        'q': q,
        'user_leads': user_leads,
        'assignable_users': assignable_users,
        'role_choices': role_choices,
        'page_size': page_size,
        'active': 'tasks',
    }
    return render(request, "dashboard/tasks.html", context)


@login_required


def task_create_view(request):
    if request.method == "POST":
        title = request.POST.get('title', '').strip()
        description = request.POST.get('description', '').strip()
        priority = request.POST.get('priority', TaskReminder.Priority.MEDIUM)
        lead_id = request.POST.get('lead_id')
        sync_to_followup = bool(request.POST.get('sync_to_followup'))
        
        # Timeline handling
        timeline_option = request.POST.get('timeline_option', 'today_eod')
        today = timezone.localdate()
        
        if timeline_option == 'today_eod':
            due_date = today
            due_time = "18:30:00"
        elif timeline_option == 'tomorrow_eod':
            due_date = today + timedelta(days=1)
            due_time = "18:30:00"
        else: # custom
            due_date = request.POST.get('custom_due_date') or today
            due_time = request.POST.get('custom_due_time') or None

        lead = None
        if lead_id:
            try:
                lead = Lead.objects.get(pk=lead_id)
            except Lead.DoesNotExist:
                lead = None

        # User assignment handling (Multiple selection supported)
        selected_user_ids = request.POST.getlist('assigned_users')
        target_users = []
        if selected_user_ids:
            target_users = list(User.objects.filter(id__in=selected_user_ids, is_active=True))
        
        if not target_users:
            target_users = [request.user]

        created_count = 0
        for target_user in target_users:
            TaskReminder.objects.create(
                user=target_user,
                title=title,
                description=description,
                due_date=due_date,
                due_time=due_time if due_time else None,
                priority=priority,
                lead=lead,
                sync_to_followup=sync_to_followup,
                status=TaskReminder.Status.PENDING,
            )
            created_count += 1
            
            # Send Notification if assigned to someone else
            if target_user != request.user:
                try:
                    from notifications.models import Notification
                    Notification.objects.create(
                        recipient=target_user,
                        title="New Task Assigned",
                        message=f"{request.user.get_full_name() or request.user.username} assigned you task: '{title}'",
                        notification_type="SYSTEM",
                        link_url="/dashboard/tasks/"
                    )
                except Exception:
                    pass

        # If synced to followup, update lead's next followup
        if sync_to_followup and lead:
            lead.next_followup_date = due_date
            if due_time:
                lead.next_followup_time = due_time
            lead.save(update_fields=['next_followup_date', 'next_followup_time'])
            
        if created_count > 1:
            messages.success(request, f"Task '{title}' created and assigned to {created_count} users successfully!")
        else:
            messages.success(request, f"Task '{title}' created successfully!")
    return redirect("dashboard:tasks")


@login_required


def task_update_status(request, pk):
    task = get_object_or_404(TaskReminder, pk=pk)
    if task.user != request.user and not (request.user.hospital and request.user.role in ['SUPER_ADMIN', 'MANAGER']):
        messages.error(request, "Unauthorized action.")
        return redirect("dashboard:tasks")
        
    new_status = request.POST.get('status')
    if new_status in TaskReminder.Status.values:
        task.status = new_status
        task.save(update_fields=['status'])
        messages.success(request, f"Task status updated to {task.get_status_display()}.")
    return redirect("dashboard:tasks")


@login_required


def task_send_report_to_admin(request):
    if request.method == "POST":
        report_notes = request.POST.get('report_notes', '').strip()
        selected_task_ids = request.POST.getlist('task_ids')
        
        user = request.user
        tasks_to_report = TaskReminder.objects.filter(user=user)
        if selected_task_ids:
            tasks_to_report = tasks_to_report.filter(id__in=selected_task_ids)
            
        tasks_count = tasks_to_report.count()
        tasks_to_report.update(
            is_reported_to_admin=True,
            admin_report_notes=report_notes,
            reported_at=timezone.now()
        )
        
        # Send Notification to Admin / SuperAdmin
        from notifications.models import Notification
        admins = User.objects.filter(role__in=['SUPER_ADMIN', 'ADMIN', 'MANAGER'])
        if user.hospital:
            admins = admins.filter(hospital=user.hospital)
            
        for admin_user in admins:
            Notification.objects.create(
                user=admin_user,
                title=f"Task Report from {user.get_full_name() or user.username}",
                message=f"{user.get_full_name() or user.username} submitted a Task & Reminder summary report ({tasks_count} tasks). Notes: {report_notes[:200]}",
                link="/dashboard/reports/admin/",
            )
            
        messages.success(request, f"Successfully submitted task report ({tasks_count} tasks) to Administration!")
    return redirect("dashboard:tasks")

@login_required


def call_history_view(request):
    from django.core.paginator import Paginator
    user = request.user
    
    # 1. Get Base Leads for hospital / user
    leads = Lead.objects.filter(is_archived=False)
    if user.hospital:
        leads = leads.filter(hospital=user.hospital)
        
    if user.role == 'LEAD_ATTENDENT':
        leads = leads.filter(assigned_to=user)
    elif not user.can_view_all_leads:
        if user.can_view_team_leads:
            team = User.objects.filter(reports_to=user)
            leads = leads.filter(Q(assigned_to=user) | Q(assigned_to__in=team))
        elif user.can_view_assigned_leads:
            leads = leads.filter(assigned_to=user)
            
    # Filter leads that have any telecaller remarks, call logs, or recorded interactions
    leads = leads.filter(
        Q(custom_data__remark_1__isnull=False, custom_data__remark_1__gt="") |
        Q(custom_data__remark_2__isnull=False, custom_data__remark_2__gt="") |
        Q(custom_data__remark_3__isnull=False, custom_data__remark_3__gt="") |
        Q(custom_data__calling_date_remark_1__isnull=False, custom_data__calling_date_remark_1__gt="") |
        Q(custom_data__calling_date_remark_2__isnull=False, custom_data__calling_date_remark_2__gt="") |
        Q(custom_data__calling_date_remark_3__isnull=False, custom_data__calling_date_remark_3__gt="") |
        Q(custom_data__last_called_date__isnull=False, custom_data__last_called_date__gt="") |
        Q(followups__isnull=False)
    ).distinct().select_related('assigned_to', 'stage').order_by('-updated_at')
    
    # Search Query
    q = request.GET.get('q', '').strip()
    if q:
        leads = leads.filter(
            Q(name__icontains=q) |
            Q(mobile__icontains=q) |
            Q(lead_code__icontains=q) |
            Q(custom_data__remark_1__icontains=q) |
            Q(custom_data__remark_2__icontains=q) |
            Q(custom_data__remark_3__icontains=q) |
            Q(custom_data__doctor__icontains=q) |
            Q(custom_data__department__icontains=q)
        )
        
    # Date Filter
    call_date = request.GET.get('call_date', '').strip()
    if call_date:
        call_date_alt = ""
        try:
            from datetime import datetime as dt
            dt_obj = dt.strptime(call_date, "%Y-%m-%d")
            call_date_alt = dt_obj.strftime("%d-%m-%Y")
        except Exception:
            pass

        date_q = (
            Q(custom_data__calling_date_remark_1=call_date) |
            Q(custom_data__calling_date_remark_2=call_date) |
            Q(custom_data__calling_date_remark_3=call_date) |
            Q(custom_data__last_called_date=call_date) |
            Q(followups__followup_date=call_date)
        )
        if call_date_alt:
            date_q |= (
                Q(custom_data__calling_date_remark_1=call_date_alt) |
                Q(custom_data__calling_date_remark_2=call_date_alt) |
                Q(custom_data__calling_date_remark_3=call_date_alt) |
                Q(custom_data__last_called_date=call_date_alt)
            )
        leads = leads.filter(date_q)
        
    # Call Status / Appointment filter
    call_status = request.GET.get('call_status', '').strip()
    if call_status:
        if call_status.lower() == 'done':
            leads = leads.filter(
                Q(custom_data__appointment_status__iexact='Done') |
                Q(custom_data__appointment_status__iexact='Completed') |
                Q(custom_data__appointment_status__icontains='Payment Done') |
                Q(admission_status='ADMISSION_DONE') |
                Q(stage__name__iexact='Payment Done') |
                Q(stage__name__iexact='Visited') |
                Q(stage__name__iexact='Admission')
            )
        elif call_status.lower() == 'booked':
            leads = leads.filter(
                Q(custom_data__appointment_status__iexact='Booked') |
                Q(custom_data__appointment_status__icontains='Booking')
            )
        elif call_status.lower() == 'cancelled':
            leads = leads.filter(
                Q(custom_data__appointment_status__icontains='Cancel')
            )
        elif call_status.lower() == 'not booked':
            leads = leads.filter(
                Q(custom_data__appointment_status__iexact='Not Booked') |
                Q(custom_data__appointment_status__icontains='Follow-up') |
                Q(custom_data__appointment_status__icontains='WARM') |
                Q(custom_data__appointment_status__icontains='Not Interested') |
                Q(custom_data__appointment_status__isnull=True) |
                Q(custom_data__appointment_status='')
            )
        else:
            leads = leads.filter(custom_data__appointment_status=call_status)
        
    # Stats
    total_calls_logged = leads.count()
    
    # Pagination
    paginator = Paginator(leads, 25)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1) if hasattr(paginator, 'get_elided_page_range') else paginator.page_range
    
    query_params = request.GET.copy()
    if 'page' in query_params:
        del query_params['page']
        
    today = timezone.localdate()
    context = {
        'page_obj': page_obj,
        'leads': page_obj,
        'page_range': page_range,
        'total_calls_logged': total_calls_logged,
        'query_params': query_params.urlencode(),
        'q': q,
        'call_date': call_date,
        'today': today,
        'today_str': today.strftime("%Y-%m-%d"),
        'call_status': call_status,
        'active': 'call_history',
    }
    return render(request, "dashboard/call_history.html", context)

@login_required


def admin_reports_view(request):
    user = request.user
    if user.role not in ['SUPER_ADMIN', 'MANAGER', 'ADMIN'] and not user.is_superuser:
        messages.error(request, "Access restricted to Administration and Management.")
        return redirect("dashboard:home")
        
    # Strict Business Boundary Check:
    # 1. If user is bound to a specific hospital/business, FORCE that hospital only.
    #    They MUST NOT be able to override via GET params or session to peek into other businesses.
    # 2. Only Super Admin / Global Admin (without user.hospital) can switch businesses.
    if user.hospital:
        effective_hospital = user.hospital
    elif user.is_superuser or user.role == User.Role.SUPER_ADMIN:
        selected_hospital_id = (
            request.GET.get("business", "").strip()
            or request.GET.get("hospital", "").strip()
            or str(request.session.get("active_business_id", "")).strip()
        )
        if selected_hospital_id and selected_hospital_id.isdigit():
            effective_hospital = Hospital.objects.filter(id=int(selected_hospital_id)).first()
        else:
            effective_hospital = None
    else:
        effective_hospital = None
        
    today = timezone.localdate()
    from datetime import datetime, time
    from audit.models import AuditLog
    
    # Filters from GET request
    task_user_filter = request.GET.get('user', '').strip()
    role_filter = request.GET.get('role', '').strip()
    date_filter = request.GET.get('date', '').strip()
    
    # Determine effective target date (default to today if empty, or parsed date if provided)
    if date_filter:
        try:
            target_date = datetime.strptime(date_filter, "%Y-%m-%d").date()
        except ValueError:
            target_date = today
            date_filter = today.strftime("%Y-%m-%d")
    else:
        target_date = today
        date_filter = today.strftime("%Y-%m-%d")
        
    start_target_date = timezone.make_aware(datetime.combine(target_date, time.min))
    end_target_date = timezone.make_aware(datetime.combine(target_date, time.max))

    # Base Employees for this specific business/hospital only
    employees_qs = User.objects.filter(is_active=True)
    if effective_hospital:
        employees_qs = employees_qs.filter(hospital=effective_hospital)
    elif not user.is_superuser and user.role != User.Role.SUPER_ADMIN:
        # Fallback security shield: If user has no hospital and is not superadmin, empty queryset
        employees_qs = employees_qs.none()

    # Manager restriction: Only see subordinates or self within the same business
    if user.role == User.Role.MANAGER and not user.is_superuser:
        employees_qs = employees_qs.filter(Q(reports_to=user) | Q(pk=user.pk))
        
    # All employees for dropdown (strictly scoped to this business)
    all_employees = employees_qs.order_by('first_name', 'username')
    
    # Filtered employees according to role and user filters
    filtered_employees = employees_qs
    if role_filter:
        filtered_employees = filtered_employees.filter(role=role_filter)
    if task_user_filter:
        filtered_employees = filtered_employees.filter(username=task_user_filter)

    # 1. Fetch Task Reports submitted to Admin (Strictly Scoped to Business Employees)
    task_reports_qs = TaskReminder.objects.filter(is_reported_to_admin=True, user__in=employees_qs)
        
    if role_filter:
        task_reports_qs = task_reports_qs.filter(user__role=role_filter)
    if task_user_filter:
        task_reports_qs = task_reports_qs.filter(user__username=task_user_filter)
    if date_filter:
        task_reports_qs = task_reports_qs.filter(reported_at__date=target_date)
        
    task_reports = task_reports_qs.select_related('user', 'lead').order_by('-reported_at')
    
    # 2. Daily Calling & EOD Reports (Strictly Scoped to Business Employees)
    daily_reports_qs = DailyReport.objects.filter(user__in=employees_qs)
        
    if role_filter:
        daily_reports_qs = daily_reports_qs.filter(user__role=role_filter)
    if task_user_filter:
        daily_reports_qs = daily_reports_qs.filter(user__username=task_user_filter)
    if date_filter:
        daily_reports_qs = daily_reports_qs.filter(report_date=target_date)
        
    daily_reports = daily_reports_qs.select_related('user', 'user__reports_to').order_by('-report_date', '-created_at')
    
    # 3. Staff Attendance & Login/Logout Activity for Selected Date
    staff_attendance = []
    logged_in_count = 0
    total_staff_count = filtered_employees.count()
    
    for emp in filtered_employees:
        emp_logs = AuditLog.objects.filter(user=emp, created_at__range=(start_target_date, end_target_date)).order_by('created_at')
        first_login_log = emp_logs.filter(action='USER_LOGIN').first()
        last_login_log = emp_logs.filter(action='USER_LOGIN').last()
        last_logout_log = emp_logs.filter(action='USER_LOGOUT').last()
        
        # Calculate first login time on target date
        first_login = None
        if first_login_log:
            first_login = first_login_log.created_at
        elif emp.last_login and start_target_date <= emp.last_login <= end_target_date:
            first_login = emp.last_login
        elif emp_logs.exists():
            first_login = emp_logs.first().created_at
            
        last_logout = last_logout_log.created_at if last_logout_log else None
        is_logged_in = bool(first_login)
        if is_logged_in:
            logged_in_count += 1
            
        # Determine session status
        if target_date == today:
            if last_logout and (not last_login_log or last_logout >= last_login_log.created_at):
                session_status = 'Logged Out'
            elif is_logged_in:
                session_status = 'Active Now'
            else:
                session_status = 'Absent / Inactive'
        else:
            if last_logout:
                session_status = 'Logged Out'
            elif is_logged_in:
                session_status = 'Completed Session'
            else:
                session_status = 'Absent'

        # Check if EOD report submitted on this target date
        eod_report = DailyReport.objects.filter(user=emp, report_date=target_date).first()
        
        # Activity summary for this date (strictly scoped to lead under this business)
        leads_assigned_on_date = Lead.objects.filter(assigned_to=emp, inquiry_date=target_date)
        if effective_hospital:
            leads_assigned_on_date = leads_assigned_on_date.filter(hospital=effective_hospital)
        leads_assigned_count = leads_assigned_on_date.count()
        
        staff_attendance.append({
            'user': emp,
            'is_logged_in': is_logged_in,
            'first_login': first_login,
            'last_logout': last_logout,
            'eod_report': eod_report,
            'leads_assigned_today': leads_assigned_count,
            'session_status': session_status,
        })
        
    # Sort staff attendance: logged in first, then by role, then username
    staff_attendance.sort(key=lambda x: (not x['is_logged_in'], x['user'].role, x['user'].username))

    # Metric stats for cards (reflects filtered date & role/user criteria)
    total_task_reports = task_reports_qs.count()
    total_daily_reports = daily_reports_qs.count()

    # Pagination for Task Reports
    paginator = Paginator(task_reports, 15)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1) if hasattr(paginator, 'get_elided_page_range') else paginator.page_range
    
    query_params = request.GET.copy()
    if 'page' in query_params:
        del query_params['page']

    # Dynamically scope allowed role choices to current business/tenant if configured
    if effective_hospital and hasattr(effective_hospital, 'get_allowed_roles'):
        allowed_keys = effective_hospital.get_allowed_roles()
        all_choices = [
            (User.Role.ADMIN, "Admin"),
            (User.Role.MANAGER, "Manager"),
            (User.Role.LEAD_ATTENDENT, "Lead Attendant"),
            (User.Role.DOCTOR, "Doctor"),
            (User.Role.COUNSELLOR, "Counsellor"),
            (User.Role.HR, "HR"),
        ]
        role_choices = [c for c in all_choices if c[0] in allowed_keys]
    else:
        role_choices = [
            (User.Role.ADMIN, "Admin"),
            (User.Role.MANAGER, "Manager"),
            (User.Role.LEAD_ATTENDENT, "Lead Attendant"),
            (User.Role.DOCTOR, "Doctor"),
            (User.Role.COUNSELLOR, "Counsellor"),
            (User.Role.HR, "HR"),
        ]

    context = {
        'active': 'reports',
        'task_reports': page_obj,
        'page_obj': page_obj,
        'page_range': page_range,
        'query_params': query_params.urlencode(),
        'total_task_reports': total_task_reports,
        'total_daily_reports': total_daily_reports,
        'employees': all_employees,
        'role_choices': role_choices,
        'staff_attendance': staff_attendance,
        'today_logged_in_count': logged_in_count,
        'total_staff_count': total_staff_count,
        'today_date': target_date,
        'today_max_date': today.strftime("%Y-%m-%d"),
        'is_today': (target_date == today),
        'selected_user': task_user_filter,
        'selected_role': role_filter,
        'selected_date': date_filter,
        'current_hospital': effective_hospital,
    }
    return render(request, "dashboard/admin_reports.html", context)
