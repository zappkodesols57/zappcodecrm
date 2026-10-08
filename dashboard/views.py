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


# Re-export helper functions
from dashboard.helpers import filter_uncontacted_leads_ids, extract_lead_followup_date, _get_effective_hospital

# Re-export SuperAdmin / Executive Management Views
from dashboard.superadmin_views import (
    superadmin_home,
    nel_card_drilldown_api,
    live_metrics_api,
    nelson_module_view,
    management_home,
    roles_permissions_view,
    placeholder_view,
)

# Re-export Hospital Telecaller Views
from dashboard.hospital_telecaller_views import (
    telecaller_home,
    telecaller_tab_data_api,
    telecaller_search,
    telecaller_appointments,
    telecaller_my_leads,
    telecaller_new_enquiries,
    telecaller_today_team_activity,
)

# Re-export Hospital Doctor Views
from dashboard.hospital_doctor_views import (
    doctor_home,
    doctor_appointments,
    doctor_patient_review,
)

# Re-export Reports and Tasks Views
from dashboard.reports_and_tasks_views import (
    source_report,
    campaign_report,
    employee_report,
    employee_detail_activity,
    submit_daily_report,
    export_daily_activity_leads,
    download_eod_report_pdf,
    management_daily_reports,
    task_list_view,
    task_create_view,
    task_update_status,
    task_send_report_to_admin,
    call_history_view,
    admin_reports_view,
)

def welcome_view(request):
    from accounts.views import _role_redirect
    from accounts.models import User, Hospital
    from leads.models import Lead, DealStatus, Campaign, Appointment, AppointmentStatus
    from followups.models import FollowUp, FollowUpStatus
    from django.db.models import Count

    user = request.user
    today_date = timezone.localdate()
    now = timezone.localtime()
    start_of_today = timezone.make_aware(datetime.combine(today_date, datetime.min.time()))
    end_of_today = timezone.make_aware(datetime.combine(today_date, datetime.max.time()))

    # 1. Determine Time-based Greeting & Icon
    current_hour = now.hour
    if 5 <= current_hour < 12:
        greeting = "Good Morning"
        greeting_icon = "fa-sun"
        greeting_style = "color: #f59e0b;"
    elif 12 <= current_hour < 17:
        greeting = "Good Afternoon"
        greeting_icon = "fa-cloud-sun"
        greeting_style = "color: #f97316;"
    else:
        greeting = "Good Evening"
        greeting_icon = "fa-moon"
        greeting_style = "color: #818cf8;"

    # Helper function to extract campaign breakdown and walk-in count for any queryset of leads
    def extract_campaign_and_walkin_stats(qs):
        from collections import defaultdict
        camp_map = defaultdict(int)
        
        # 1. Direct indexed DB aggregation for campaign foreign keys
        campaign_counts = (
            qs.filter(campaign__isnull=False)
            .values("campaign__name")
            .annotate(count=Count("id"))
            .order_by("-count")[:6]
        )
        for c in campaign_counts:
            c_name = c["campaign__name"]
            if c_name and str(c_name).strip() not in ('nan', 'None', '', '—', '-'):
                camp_map[str(c_name).strip()] += c["count"]

        # 2. Fast DB counts for walkin & meta
        walkin_count = qs.filter(
            Q(lead_source__name__icontains='walk') |
            Q(lead_type__icontains='walk')
        ).count()
        meta_count = qs.filter(
            Q(lead_source__name__icontains='meta') |
            Q(lead_source__name__icontains='facebook') |
            Q(lead_source__name__icontains='instagram')
        ).count()

        campaign_stats = []
        for name, cnt in sorted(camp_map.items(), key=lambda x: x[1], reverse=True)[:6]:
            campaign_stats.append({
                "name": name,
                "count": cnt
            })

        if not campaign_stats:
            source_counts = (
                qs.filter(lead_source__isnull=False)
                .values("lead_source__name")
                .annotate(count=Count("id"))
                .order_by("-count")[:6]
            )
            for s in source_counts:
                campaign_stats.append({
                    "name": s["lead_source__name"],
                    "count": s["count"]
                })

        return {
            "total_count": qs.count(),
            "campaign_stats": campaign_stats,
            "walkin_count": walkin_count,
            "meta_count": meta_count
        }

    # Role categorization flags
    is_super_admin = (user.role == User.Role.SUPER_ADMIN and not user.hospital)
    is_doctor = (user.role == User.Role.DOCTOR)
    is_lead_attendant = (user.role == User.Role.LEAD_ATTENDENT)
    is_counsellor = (user.role in (User.Role.COUNSELLOR, User.Role.HR))
    is_business_admin = (user.role in (User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER) and bool(user.hospital))

    # Data structures to pass to template
    businesses_data = []
    role_metrics = {}

    all_active_leads_today = Lead.objects.filter(is_archived=False).filter(
        Q(created_at__range=(start_of_today, end_of_today)) | Q(inquiry_date=today_date)
    ).distinct()

    if is_super_admin:
        # Multi-business breakdown: Nelson Hospital, Zappcode Academy, and any dynamic business/tenant
        hospitals = Hospital.objects.filter(is_active=True).order_by("id")
        for h in hospitals:
            h_leads_today = all_active_leads_today.filter(hospital=h)
            b_stats = extract_campaign_and_walkin_stats(h_leads_today)
            businesses_data.append({
                "id": h.id,
                "name": h.name,
                "total_count": b_stats["total_count"],
                "walkin_count": b_stats["walkin_count"],
                "meta_count": b_stats["meta_count"],
                "campaign_stats": b_stats["campaign_stats"],
                "is_nelson": "nelson" in h.name.lower(),
                "is_zappcode": "zappcode" in h.name.lower() or "academy" in h.name.lower(),
            })

    elif is_doctor:
        # Doctor role: Today's Appointments & Pending Approvals
        doc_user_apts = Appointment.objects.filter(
            Q(doctor_user=user) | Q(doctor_name__iexact=user.get_full_name().strip()) | Q(doctor_name__iexact=user.username)
        )
        today_appointments_count = doc_user_apts.filter(appointment_date=today_date).count()
        pending_approval_count = doc_user_apts.filter(status=AppointmentStatus.PENDING_APPROVAL).count()
        scheduled_today_count = doc_user_apts.filter(
            appointment_date=today_date,
            status__in=[AppointmentStatus.APPROVED, AppointmentStatus.SCHEDULED]
        ).count()

        role_metrics = {
            "today_appointments_count": today_appointments_count,
            "pending_approval_count": pending_approval_count,
            "scheduled_today_count": scheduled_today_count,
        }

    elif is_lead_attendant or is_counsellor:
        # Tenant specific leads for Lead Attendants / Counsellors
        user_h_leads = all_active_leads_today.filter(hospital=user.hospital) if user.hospital else all_active_leads_today

        fresh_leads_qs = user_h_leads.filter(assigned_to__isnull=True)
        my_assigned_leads_qs = user_h_leads.filter(assigned_to=user)

        fresh_stats = extract_campaign_and_walkin_stats(fresh_leads_qs)
        assigned_stats = extract_campaign_and_walkin_stats(my_assigned_leads_qs)
        combined_stats = extract_campaign_and_walkin_stats(user_h_leads.filter(Q(assigned_to=user) | Q(assigned_to__isnull=True)))

        # Pending Follow-ups for this user today
        pending_fu_count = FollowUp.objects.filter(
            followup_date=today_date,
            followup_status__in=[FollowUpStatus.PENDING, "PENDING", "pending"],
            lead__assigned_to=user
        ).count()

        role_metrics = {
            "fresh_leads_count": fresh_stats["total_count"],
            "assigned_leads_count": assigned_stats["total_count"],
            "total_relevant_count": fresh_stats["total_count"] + assigned_stats["total_count"],
            "campaign_stats": combined_stats["campaign_stats"],
            "walkin_count": combined_stats["walkin_count"],
            "pending_followups_count": pending_fu_count,
        }

    else:
        # Business Admin / Manager / Generic tenant user
        user_h_leads = all_active_leads_today.filter(hospital=user.hospital) if user.hospital else all_active_leads_today
        b_stats = extract_campaign_and_walkin_stats(user_h_leads)
        pending_fu_count = FollowUp.objects.filter(
            followup_date=today_date,
            followup_status__in=[FollowUpStatus.PENDING, "PENDING", "pending"]
        )
        if user.hospital:
            pending_fu_count = pending_fu_count.filter(lead__hospital=user.hospital)
        role_metrics = {
            "total_count": b_stats["total_count"],
            "campaign_stats": b_stats["campaign_stats"],
            "walkin_count": b_stats["walkin_count"],
            "meta_count": b_stats["meta_count"],
            "pending_followups_count": pending_fu_count.count()
        }

    # Determine target URL
    dest_resp = _role_redirect(user)
    next_url = dest_resp.url if hasattr(dest_resp, 'url') else "/dashboard/"

    user_name = user.get_full_name().strip() or user.username
    user_role = user.get_role_display() if hasattr(user, 'get_role_display') else str(user.role)
    hospital_name = user.hospital.name if user.hospital else ("Global Super Admin" if is_super_admin else "")

    return render(request, "dashboard/welcome.html", {
        "user_name": user_name,
        "user_role": user_role,
        "hospital_name": hospital_name,
        "greeting": greeting,
        "greeting_icon": greeting_icon,
        "greeting_style": greeting_style,
        "current_time_str": now.strftime("%I:%M %p"),
        "today_date": today_date.strftime("%A, %d %B %Y"),
        "is_super_admin": is_super_admin,
        "is_doctor": is_doctor,
        "is_lead_attendant": is_lead_attendant,
        "is_counsellor": is_counsellor,
        "is_business_admin": is_business_admin,
        "businesses_data": businesses_data,
        "role_metrics": role_metrics,
        "next_url": next_url,
    })


@login_required


def home(request):
    from accounts.models import User
    # Super Admin goes to the Super Admin analytics dashboard
    if request.user.role == User.Role.SUPER_ADMIN:
        return redirect("dashboard:superadmin_home")
    if request.user.hospital and request.user.industry == 'HOSPITAL':
        if request.user.role == User.Role.LEAD_ATTENDENT:
            query = request.GET.urlencode()
            return redirect(f"{reverse('dashboard:telecaller_home')}?{query}" if query else "dashboard:telecaller_home")
        elif request.user.role == User.Role.DOCTOR:
            query = request.GET.urlencode()
            return redirect(f"{reverse('dashboard:doctor_home')}?{query}" if query else "dashboard:doctor_home")
        elif request.user.role in (User.Role.ADMIN, User.Role.MANAGER):
            return redirect("dashboard:hospital_admin_home")

    from django.db.models import Q
    from leads.models import SourceCategory, Course, LeadStage, LeadSource, Campaign
    
    today = timezone.localdate()
    leads = Lead.objects.filter(is_archived=False)

    # --- Business-Tenant Scoping ---
    # Any user assigned to a business sees only that business's leads.
    # Global Super Admin (no hospital) can see all leads across businesses.
    is_global_admin = request.user.is_superuser or (
        request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital
    )

    if request.user.hospital:
        # Tenant user: always scoped to their business
        leads = leads.filter(hospital=request.user.hospital)
        if not request.user.can_view_all_leads:
            if request.user.can_view_team_leads:
                team = User.objects.filter(reports_to=request.user)
                leads = leads.filter(Q(assigned_to=request.user) | Q(assigned_to__in=team))
            elif request.user.role == User.Role.MANAGER:
                team = User.objects.filter(reports_to=request.user)
                leads = leads.filter(Q(assigned_to=request.user) | Q(assigned_to__in=team) | Q(assigned_to__isnull=True))
            elif request.user.can_view_assigned_leads or request.user.role in (User.Role.COUNSELLOR, User.Role.HR, User.Role.LEAD_ATTENDENT):
                # Counsellors / HR: see assigned leads, leads created by them, or fresh unassigned leads in their business
                leads = leads.filter(Q(assigned_to=request.user) | Q(created_by=request.user) | Q(assigned_to__isnull=True))
            else:
                leads = leads.none()
    elif is_global_admin:
        # Global Super Admin: check if active_business is selected in session or URL
        selected_hospital_id = (
            request.GET.get("business", "").strip()
            or request.GET.get("hospital", "").strip()
            or str(request.session.get("active_business_id", "")).strip()
        )
        if selected_hospital_id and selected_hospital_id.isdigit():
            leads = leads.filter(hospital_id=int(selected_hospital_id))
        elif selected_hospital_id == "none":
            leads = leads.filter(hospital__isnull=True)
    else:
        # User without a hospital assigned (e.g. Academy user / Counsellor / HR / Staff)
        leads = leads.filter(hospital__isnull=True)
        if not request.user.can_view_all_leads:
            if request.user.can_view_team_leads:
                team = User.objects.filter(reports_to=request.user)
                leads = leads.filter(Q(assigned_to=request.user) | Q(assigned_to__in=team))
            elif request.user.role == User.Role.MANAGER:
                team = User.objects.filter(reports_to=request.user)
                leads = leads.filter(Q(assigned_to=request.user) | Q(assigned_to__in=team) | Q(assigned_to__isnull=True))
            elif request.user.can_view_assigned_leads or request.user.role in (User.Role.COUNSELLOR, User.Role.HR, User.Role.LEAD_ATTENDENT):
                leads = leads.filter(Q(assigned_to=request.user) | Q(created_by=request.user) | Q(assigned_to__isnull=True))

    # 1. Apply Filters
    q = request.GET.get("q", "").strip()
    if q:
        leads = leads.filter(
            Q(lead_code__icontains=q) | Q(name__icontains=q) | Q(mobile__icontains=q)
            | Q(email__icontains=q) | Q(city__icontains=q) | Q(course__name__icontains=q)
            | Q(lead_source__name__icontains=q) | Q(campaign__name__icontains=q)
        )

    for field in ["source_category", "lead_source", "campaign", "course", "stage", "assigned_to"]:
        val = request.GET.get(field)
        if val:
            leads = leads.filter(**{f"{field}_id": val})

    for field in ["temperature", "deal_status", "admission_status"]:
        val = request.GET.get(field)
        if val:
            leads = leads.filter(**{field: val})

    city = request.GET.get("city")
    if city:
        leads = leads.filter(city__iexact=city)

    def _parse_date_input(val):
        if not val:
            return None
        from datetime import datetime
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(val.strip(), fmt).date()
            except ValueError:
                continue
        return None

    date_from = _parse_date_input(request.GET.get("date_from"))
    date_to = _parse_date_input(request.GET.get("date_to"))
    if date_from:
        leads = leads.filter(inquiry_date__gte=date_from)
    if date_to:
        leads = leads.filter(inquiry_date__lte=date_to)

    # 2. Compute KPIs based on user filtered leads matching exact requirements
    import datetime
    today_start = timezone.make_aware(datetime.datetime.combine(today, datetime.time.min))
    today_end = timezone.make_aware(datetime.datetime.combine(today, datetime.time.max))

    total_leads = leads.count()
    
    # 1. Today's New Leads: Leads created or inquired today
    todays_new_leads = leads.filter(
        Q(created_at__range=(today_start, today_end)) | Q(inquiry_date=today)
    ).count()

    # 2. Call Not Done: Leads pending initial call / 0 follow-ups / uncontacted
    cnd_lead_ids = filter_uncontacted_leads_ids(leads, today=today)
    call_not_done = len(cnd_lead_ids)

    # 3. Admission Today: Admissions enrolled / done / won today (Matching OPD & Admissions Drilldown Modal)
    sel_date_str = today.strftime("%Y-%m-%d")
    sel_alt_str = today.strftime("%d-%m-%Y")

    admitted_or_booked_leads = leads.filter(
        Q(admission_status="ADMISSION_DONE") | 
        Q(deal_status="WON") | 
        Q(admission__isnull=False) |
        Q(stage__name__icontains="admission") |
        Q(custom_data__appointment_status__icontains="Book") |
        Q(custom_data__appointment_status__icontains="Confirm") |
        Q(custom_data__appointment_status__icontains="Approv") |
        Q(custom_data__appointment_status__icontains="Complete") |
        Q(custom_data__appointment_status__iexact="YES") |
        (Q(custom_data__total__isnull=False) & ~Q(custom_data__total__in=["0", "0.00", "", "0.0", 0, 0.0]))
    ).filter(
        Q(created_at__range=(today_start, today_end)) | 
        Q(updated_at__range=(today_start, today_end)) | 
        Q(inquiry_date=today) |
        Q(admission__admission_date=today) |
        Q(admission__created_at__range=(today_start, today_end)) |
        Q(custom_data__admission_date=sel_date_str) |
        Q(custom_data__admission_date=sel_alt_str) |
        Q(custom_data__appo_booked_date=sel_date_str) |
        Q(custom_data__appo_booked_date=sel_alt_str) |
        Q(custom_data__appointment_date=sel_date_str) |
        Q(custom_data__appointment_date=sel_alt_str) |
        Q(custom_data__appointment_confirmed_at__startswith=sel_date_str)
    ).distinct()

    admission_today = admitted_or_booked_leads.count()

    # 4. Billing Done Today: Payments received today
    billing_today = Payment.objects.filter(
        payment_status=PaymentStatus.SUCCESS,
        created_at__range=(today_start, today_end),
        admission__lead__in=leads
    ).aggregate(s=Sum("amount"))["s"] or 0

    billing_count_today = Payment.objects.filter(
        payment_status=PaymentStatus.SUCCESS,
        created_at__range=(today_start, today_end),
        admission__lead__in=leads
    ).count()

    # Fallback to custom_data billing for hospital tenant leads
    if billing_count_today == 0:
        cd_billed_leads = admitted_or_booked_leads.filter(
            Q(custom_data__total__isnull=False) & ~Q(custom_data__total__in=["0", "0.00", "", "0.0", 0, 0.0])
        )
        billing_count_today = cd_billed_leads.count()
        if billing_today == 0 and billing_count_today > 0:
            custom_total_sum = 0.0
            for bl in cd_billed_leads:
                try:
                    custom_total_sum += float(bl.custom_data.get("total") or 0.0)
                except (ValueError, TypeError):
                    pass
            billing_today = custom_total_sum

    # 5. Follow-ups Scheduled for TODAY ONLY
    from followups.models import FollowUp as FollowUpModel
    todays_followup_lead_ids = set(
        leads.filter(next_followup_date=today).values_list('id', flat=True)
    ) | set(
        leads.filter(
            followups__followup_date=today
        ).values_list('id', flat=True)
    )
    todays_followups = len(todays_followup_lead_ids)

    # 5b. Upcoming & Overdue Follow-ups (for breakdown/filters)
    upcoming_followup_lead_ids = set(
        leads.filter(next_followup_date__gt=today).values_list('id', flat=True)
    ) | set(
        leads.filter(
            followups__followup_date__gt=today
        ).values_list('id', flat=True)
    )
    upcoming_followups = len(upcoming_followup_lead_ids)

    overdue_followup_lead_ids = set(
        leads.filter(next_followup_date__lt=today).values_list('id', flat=True)
    ) | set(
        leads.filter(
            followups__followup_date__lt=today
        ).exclude(id__in=todays_followup_lead_ids | upcoming_followup_lead_ids).values_list('id', flat=True)
    )
    overdue_followups = len(overdue_followup_lead_ids)

    # 6. Today's Visit Planned (Stage contains 'Visit' or appointment scheduled for today)
    visit_planned_leads = leads.filter(
        Q(stage__name__icontains="visit") |
        Q(custom_data__deal_status__icontains="visit") |
        Q(custom_data__appointment_status__icontains="visit") |
        Q(custom_data__visit_date=sel_date_str) |
        Q(custom_data__visit_date=sel_alt_str)
    ).filter(
        Q(next_followup_date=today) |
        Q(custom_data__visit_date=sel_date_str) |
        Q(custom_data__visit_date=sel_alt_str) |
        Q(followups__followup_date=today) |
        Q(updated_at__range=(today_start, today_end)) |
        Q(inquiry_date=today)
    ).distinct()
    visit_planned_today = visit_planned_leads.count()

    # Also keep legacy metrics for backward compatibility if needed
    admissions_qs = Admission.objects.filter(lead__in=leads)
    admissions = admissions_qs.count()
    conversion_rate = round((admissions / total_leads * 100), 1) if total_leads else 0
    revenue = Payment.objects.filter(payment_status=PaymentStatus.SUCCESS, admission__lead__in=leads).aggregate(s=Sum("amount"))["s"] or 0

    # 3. Chart Data (Source Distribution)
    source_data = list(
        leads.values("lead_source__name").annotate(count=Count("id")).order_by("-count")[:8]
    )
    source_labels = [s["lead_source__name"] or "Unspecified" for s in source_data]
    source_counts = [s["count"] for s in source_data]

    # 4. Chart Data (Stage Funnel) - strictly scoped to user's business_id
    current_business_id = request.user.hospital_id or getattr(request.user, "business_id", None)
    if current_business_id:
        stage_data = list(
            LeadStage.objects.filter(is_active=True, hospital_id=current_business_id).order_by("order", "name")
        )
    else:
        # Global Super Admin without hospital filter: check selected hospital or fallback
        if selected_hospital_id and selected_hospital_id.isdigit():
            stage_data = list(
                LeadStage.objects.filter(is_active=True, hospital_id=int(selected_hospital_id)).order_by("order", "name")
            )
        else:
            stage_data = list(
                LeadStage.objects.filter(is_active=True).order_by("order", "name")
            )
    funnel_labels = [s.name for s in stage_data]
    funnel_counts = []
    for s in stage_data:
        funnel_counts.append(leads.filter(stage=s).count())

    # 5. Chart Data (Monthly Trend of Inquiry Dates - Timezone and DB-safe)
    since_date = today.replace(day=1) - timedelta(days=150)
    trend_leads = leads.filter(inquiry_date__gte=since_date).values_list("inquiry_date", flat=True)
    
    from collections import defaultdict
    trend_map = defaultdict(int)
    for idate in trend_leads:
        trend_map[idate.strftime("%b %Y")] += 1
        
    trend_labels = []
    trend_counts = []
    curr = since_date
    while curr <= today:
        m_str = curr.strftime("%b %Y")
        if m_str not in trend_labels:
            trend_labels.append(m_str)
        curr += timedelta(days=15)
        
    for m in trend_labels:
        trend_counts.append(trend_map[m])

    # 6. Chart Data (Course-wise distribution)
    course_data = list(leads.values("course__name").annotate(count=Count("id")).order_by("-count")[:8])
    course_labels = [c["course__name"] or "Unspecified" for c in course_data]
    course_counts = [c["count"] for c in course_data]

    # 7. Dropdowns for filters - scoped to current user's business
    if request.user.hospital:
        active_leads_all = Lead.objects.filter(is_archived=False, hospital=request.user.hospital)
        if request.user.role in ('COUNSELLOR', 'HR'):
            active_leads_all = active_leads_all.filter(
                Q(assigned_to=request.user) | Q(created_by=request.user) | Q(assigned_to__isnull=True)
            )
    else:
        active_leads_all = Lead.objects.filter(is_archived=False)

    used_sc_ids = active_leads_all.values_list("source_category_id", flat=True).distinct()
    used_ls_ids = active_leads_all.values_list("lead_source_id", flat=True).distinct()
    used_camp_ids = active_leads_all.values_list("campaign_id", flat=True).distinct()
    used_course_ids = active_leads_all.values_list("course_id", flat=True).distinct()
    used_stage_ids = active_leads_all.values_list("stage_id", flat=True).distinct()
    used_emp_ids = active_leads_all.values_list("assigned_to_id", flat=True).distinct()
    distinct_cities = sorted(list(set(active_leads_all.exclude(city="").values_list("city", flat=True))))

    # 8. Team Activity Statistics for Managers & Super Admins
    team_stats = []
    pending_approvals_count = 0
    if request.user.role in (User.Role.SUPER_ADMIN, User.Role.MANAGER):
        pending_approvals_count = User.objects.filter(is_approved=False).count()
        team_members = User.objects.filter(
            is_active=True, 
            is_approved=True, 
            role__in=['COUNSELLOR', 'HR']
        )
        from followups.models import FollowUp, Note
        for member in team_members:
            member_fu = FollowUp.objects.filter(created_by=member, followup_date=today)
            outgoing_calls = member_fu.filter(followup_mode="CALL_OUTGOING").count()
            incoming_calls = member_fu.filter(followup_mode="CALL_INCOMING").count()
            whatsapp = member_fu.filter(followup_mode="WHATSAPP").count()
            sms = member_fu.filter(followup_mode="SMS").count()
            email = member_fu.filter(followup_mode="EMAIL").count()
            notes_count = Note.objects.filter(created_by=member, created_at__date=today).count()
            total_entries = member_fu.count() + notes_count
            
            team_stats.append({
                "member": member,
                "outgoing_calls": outgoing_calls,
                "incoming_calls": incoming_calls,
                "whatsapp": whatsapp,
                "sms": sms,
                "email": email,
                "total_entries": total_entries,
            })

    # 9. Recent Activity Leads (sorted by updated_at descending, excluding lost/cancelled leads)
    recent_leads = leads.exclude(
        Q(deal_status=DealStatus.LOST) |
        Q(admission_status__in=['LOST', 'CANCELLED']) |
        Q(stage__name__icontains='lost') |
        Q(stage__name__icontains='cancel') |
        Q(temperature='FREEZE')
    ).select_related("stage", "assigned_to", "course", "lead_source").order_by("-updated_at")[:25]

    context = {
        "active": "dashboard",
        "recent_leads": recent_leads,
        "kpis": {
            "total_leads": total_leads,
            "todays_new": todays_new_leads,
            "call_not_done": call_not_done,
            "admission_today": admission_today,
            "billing_today": billing_today,
            "billing_count_today": billing_count_today,
            "todays_followups": todays_followups,
            "visit_planned_today": visit_planned_today,
            "upcoming_followups": upcoming_followups,
            "overdue_followups": overdue_followups,
            "total_followups": todays_followups,
            "uncontacted": todays_new_leads,
            "contacted_today": total_leads - call_not_done,
            "booked_today": admission_today,
            "overdue": overdue_followups,
            "admissions": admissions,
            "conversion_rate": conversion_rate,
            "revenue": revenue,
        },
        "chart_data": json.dumps({
            "source": {"labels": source_labels, "counts": source_counts},
            "funnel": {"labels": funnel_labels, "counts": funnel_counts},
            "trend": {"labels": trend_labels, "counts": trend_counts},
            "course": {"labels": course_labels, "counts": course_counts},
        }),
        "source_categories": SourceCategory.objects.filter(id__in=used_sc_ids),
        "lead_sources": LeadSource.objects.filter(id__in=used_ls_ids),
        "campaigns": Campaign.objects.filter(id__in=used_camp_ids),
        "courses": Course.objects.filter(id__in=used_course_ids),
        "stages": LeadStage.objects.filter(id__in=used_stage_ids),
        "employees": User.objects.filter(id__in=used_emp_ids),
        "cities": distinct_cities,
        "request_get": request.GET,
        "new_leads_date_from": (today - timedelta(days=7)).strftime("%Y-%m-%d"),
        "team_stats": team_stats,
        "pending_approvals_count": pending_approvals_count,
    }
    return render(request, "dashboard/home.html", context)
