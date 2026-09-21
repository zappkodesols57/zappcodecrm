from django.core.paginator import Paginator
import json
from datetime import datetime, date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum, Q, Case, When, Value, IntegerField
from django.db.models.functions import TruncMonth
from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone

from leads.models import Lead, LeadSource, SourceCategory, Course, Campaign, LeadStage, Appointment, AppointmentStatus, DealStatus, LeadTemperature, AdmissionStatus
from admissions.models import Admission
from payments.models import Payment, PaymentStatus
from accounts.models import User, Hospital
from dashboard.models import DailyReport, TaskReminder
from notifications.models import Notification
from imports.models import ImportJob


def filter_uncontacted_leads_ids(c_base, today=None):
    """
    Returns list of lead IDs that are genuinely uncontacted / call not done:
    - Fresh/untouched leads with NO calling remarks or follow-ups added yet
    - Leads where follow-ups are pending / scheduled (next_followup_date set or PENDING follow-up status)
    Strictly excludes:
    - Leads with terminal deal status (WON, LOST) or admission done
    - Leads with payments billed (total > 0)
    - Leads with confirmed/completed appointments
    """
    if today is None:
        today = timezone.localdate()
    today_s = today.strftime("%Y-%m-%d")
    today_a = today.strftime("%d-%m-%Y")
    
    # 1. Broad DB exclusions (fast indexed filter, order_by() strips created_at filesort)
    q = c_base.order_by().filter(
        deal_status__in=[DealStatus.OPEN, 'New', 'OPEN']
    ).exclude(
        deal_status__in=[DealStatus.WON, DealStatus.LOST, 'WON', 'LOST', 'CLOSED']
    ).exclude(
        admission_status__in=[AdmissionStatus.ADMISSION_DONE, 'ADMISSION_DONE']
    ).exclude(
        admission__isnull=False
    )

    rows = list(q.values(
        'id', 'custom_data', 'stage__name', 'temperature', 'hospital__settings',
        'hospital__name', 'followup_count', 'next_followup_date', 'notes'
    ))

    terminal_statuses = {'booked', 'completed', 'payment done', 'payment pending', 'cancelled', 'visited', 'admission done', 'won', 'lost', 'not interested'}
    terminal_stages = {'admission done', 'complete', 'lost', 'cancelled', 'won'}

    def is_clean_val(v):
        if not v:
            return False
        s = str(v).strip()
        return bool(s and s.lower() not in ('nan', 'none', '—', '-', '', 'null', 'nil', 'na', 'n/a'))

    valid_hospital_candidates = []
    matched_ids = []

    for r in rows:
        cd = r['custom_data'] or {}
        st_name = (r['stage__name'] or '').strip().lower()
        temp_str = str(r['temperature'] or '').strip().upper()

        # Total billed check
        tot = 0.0
        try:
            tot = float(cd.get('total_paid') or cd.get('total') or 0.0)
        except (ValueError, TypeError):
            tot = 0.0
        if tot > 0:
            continue

        raw_apt = str(cd.get('appointment_status') or '').strip().lower()
        raw_ds = str(cd.get('deal_status') or '').strip().lower()

        if raw_apt in terminal_statuses or raw_ds in terminal_statuses:
            continue
        if any(k in raw_apt for k in ['book', 'confirm', 'payment done', 'completed', 'visit planned', 'visited']):
            continue

        is_hosp = False
        btype = (r['hospital__settings'] or {}).get("business_type")
        if btype:
            is_hosp = (str(btype).strip().lower() == "hospital")
        else:
            n_lower = (r['hospital__name'] or "").lower()
            is_hosp = any(k in n_lower for k in ["hospital", "clinic", "medical", "nelson", "health"])

        next_fu_d = r['next_followup_date']
        fu_cnt = r['followup_count'] or 0

        if not is_hosp:
            # ACADEMY TENANT UNCONTACTED RULES:
            if fu_cnt > 0 or next_fu_d is not None:
                continue
            if temp_str and temp_str != LeadTemperature.UNCONTACTED:
                continue
            if st_name and st_name not in ['new', 'fresh', 'uncontacted']:
                continue
            matched_ids.append(r['id'])
        else:
            # HOSPITAL TENANT CANDIDATE
            if st_name in terminal_stages:
                continue
            if temp_str == 'COLD' and not next_fu_d:
                continue

            r1 = cd.get('remark_1')
            r2 = cd.get('remark_2')
            r3 = cd.get('remark_3')
            f_rem = cd.get('followup_remark')
            comm = cd.get('comments')
            has_any_remark = any(is_clean_val(rk) for rk in [r1, r2, r3, f_rem, comm, r['notes']])

            valid_hospital_candidates.append((r, has_any_remark, next_fu_d))

    # Fast batch query for follow-up statuses only for hospital leads needing follow-up verification
    lead_ids_needing_fu = [r['id'] for r, has_remark, next_fu in valid_hospital_candidates if not next_fu]
    fu_status_map = {}
    if lead_ids_needing_fu:
        from django.db import connection
        with connection.cursor() as cursor:
            placeholders = ','.join(['%s'] * len(lead_ids_needing_fu))
            cursor.execute(f'SELECT lead_id, followup_status FROM followups_followup WHERE lead_id IN ({placeholders})', lead_ids_needing_fu)
            for lid, st in cursor.fetchall():
                if lid not in fu_status_map:
                    fu_status_map[lid] = []
                fu_status_map[lid].append(st)

    for r, has_any_remark, next_fu_d in valid_hospital_candidates:
        cd = r['custom_data'] or {}
        lid = r['id']
        fus = fu_status_map.get(lid, [])

        has_pending_followup = False
        if fus:
            if any(st in ['PENDING', 'CALL_BACK', 'RESCHEDULED'] for st in fus):
                has_pending_followup = True
        elif next_fu_d:
            has_pending_followup = True

        if not has_pending_followup:
            if cd.get('calling_date_remark_1') in (today_s, today_a) or \
               cd.get('calling_date_remark_2') in (today_s, today_a) or \
               cd.get('calling_date_remark_3') in (today_s, today_a) or \
               cd.get('last_called_date') in (today_s, today_a):
                continue

        is_untouched = (not has_any_remark and (not fus or (r['followup_count'] or 0) == 0))

        if is_untouched or has_pending_followup:
            matched_ids.append(lid)

    return matched_ids


@login_required
def welcome_view(request):
    from accounts.views import _role_redirect
    from accounts.models import User
    from leads.models import Lead, DealStatus, Campaign
    from followups.models import FollowUp, FollowUpStatus

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

    # 2. Base Queryset for Leads
    if user.hospital:
        hospital_leads = Lead.objects.filter(hospital=user.hospital, is_archived=False)
    else:
        hospital_leads = Lead.objects.filter(is_archived=False, hospital__isnull=True)

    today_leads_qs = hospital_leads.filter(
        Q(created_at__range=(start_of_today, end_of_today)) | Q(inquiry_date=today_date)
    ).distinct()

    if not user.can_view_all_leads and user.role in (User.Role.COUNSELLOR, User.Role.HR, User.Role.LEAD_ATTENDENT):
        today_leads_qs = today_leads_qs.filter(
            Q(assigned_to=user) | Q(created_by=user) | Q(assigned_to__isnull=True)
        )

    new_leads_count = today_leads_qs.count()

    # 3. Campaign Breakdown
    # Group by campaign name or source
    campaign_stats = []
    # Try grouped by campaign
    campaign_counts = (
        today_leads_qs.filter(campaign__isnull=False)
        .values("campaign__name")
        .annotate(count=Count("id"))
        .order_by("-count")[:5]
    )
    for c in campaign_counts:
        campaign_stats.append({
            "name": c["campaign__name"],
            "count": c["count"]
        })

    # If few or no tagged campaigns, check custom_data campaigns / form_name
    if not campaign_stats:
        from collections import defaultdict
        camp_map = defaultdict(int)
        for l in today_leads_qs:
            cd = l.custom_data or {}
            c_name = cd.get("campaign") or cd.get("form_name") or (l.lead_source.name if l.lead_source else None)
            if c_name and str(c_name).strip() not in ('nan', 'None', '', '—', '-'):
                camp_map[str(c_name).strip()] += 1
        for name, cnt in sorted(camp_map.items(), key=lambda x: x[1], reverse=True)[:5]:
            campaign_stats.append({
                "name": name,
                "count": cnt
            })

    # If still empty, group by lead source
    if not campaign_stats:
        source_counts = (
            today_leads_qs.filter(lead_source__isnull=False)
            .values("lead_source__name")
            .annotate(count=Count("id"))
            .order_by("-count")[:5]
        )
        for s in source_counts:
            campaign_stats.append({
                "name": s["lead_source__name"],
                "count": s["count"]
            })

    # 4. Pending Follow-ups for Today
    pending_followups_qs = FollowUp.objects.filter(
        followup_date=today_date,
        followup_status__in=[FollowUpStatus.PENDING, "PENDING", "pending"]
    )
    if user.hospital:
        pending_followups_qs = pending_followups_qs.filter(lead__hospital=user.hospital)
    else:
        pending_followups_qs = pending_followups_qs.filter(lead__hospital__isnull=True)
    if not user.can_view_all_leads and user.role in (User.Role.LEAD_ATTENDENT, User.Role.COUNSELLOR, User.Role.HR):
        pending_followups_qs = pending_followups_qs.filter(
            Q(lead__assigned_to=user) | Q(created_by=user)
        )
    pending_followups_count = pending_followups_qs.count()

    # 5. Determine target URL
    dest_resp = _role_redirect(user)
    next_url = dest_resp.url if hasattr(dest_resp, 'url') else "/dashboard/"

    user_name = user.get_full_name().strip() or user.username
    user_role = user.get_role_display() if hasattr(user, 'get_role_display') else str(user.role)
    hospital_name = user.hospital.name if user.hospital else ""

    return render(request, "dashboard/welcome.html", {
        "user_name": user_name,
        "user_role": user_role,
        "hospital_name": hospital_name,
        "greeting": greeting,
        "greeting_icon": greeting_icon,
        "greeting_style": greeting_style,
        "current_time_str": now.strftime("%I:%M %p"),
        "today_date": today_date.strftime("%A, %d %B %Y"),
        "new_leads_count": new_leads_count,
        "campaign_stats": campaign_stats,
        "pending_followups_count": pending_followups_count,
        "next_url": next_url,
    })


@login_required
def home(request):
    from accounts.models import User
    # If user belongs to a specific hospital role, send them directly to their dedicated dashboard
    if request.user.hospital and request.user.is_hospital_user:
        if request.user.role == User.Role.LEAD_ATTENDENT:
            return redirect("dashboard:telecaller_home")
        elif request.user.role == User.Role.DOCTOR:
            return redirect("dashboard:doctor_home")
        elif request.user.role == User.Role.SUPER_ADMIN:
            return redirect("dashboard:superadmin_home")
        elif request.user.role == User.Role.MANAGER:
            return redirect("dashboard:superadmin_home")

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

    # 5. Upcoming Follow-ups: next_followup_date >= today OR a FollowUp scheduled for today/future
    from followups.models import FollowUp as FollowUpModel
    upcoming_followup_lead_ids = set(
        leads.filter(next_followup_date__gte=today).values_list('id', flat=True)
    ) | set(
        leads.filter(
            followups__followup_date__gte=today
        ).values_list('id', flat=True)
    )
    upcoming_followups = len(upcoming_followup_lead_ids)

    # 6. Overdue Follow-ups: next_followup_date < today OR a past FollowUp with no future follow-up
    overdue_followup_lead_ids = set(
        leads.filter(next_followup_date__lt=today).values_list('id', flat=True)
    ) | set(
        leads.filter(
            followups__followup_date__lt=today
        ).exclude(id__in=upcoming_followup_lead_ids).values_list('id', flat=True)
    )
    overdue_followups = len(overdue_followup_lead_ids)

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

    # 4. Chart Data (Stage Funnel)
    stage_data = list(
        LeadStage.objects.filter(is_active=True).order_by("order")
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

    context = {
        "active": "dashboard",
        "kpis": {
            "total_leads": total_leads,
            "todays_new": todays_new_leads,
            "call_not_done": call_not_done,
            "admission_today": admission_today,
            "billing_today": billing_today,
            "billing_count_today": billing_count_today,
            "upcoming_followups": upcoming_followups,
            "overdue_followups": overdue_followups,
            "total_followups": upcoming_followups + overdue_followups,
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


@login_required
def superadmin_home(request):
    """
    Dedicated interactive analytics dashboard for Hospital Admins and Managers.
    Includes full KPI cards, payment breakdown (OPD, Pharmacy, IPD, Investigation),
    multi-dimension filters (Campaign, Source, Department, Doctor, Location, Age Group, Weekday, Year, Month),
    and interactive synchronized charts matching the Nelson Organic Leads Analytics system.
    """
    from accounts.models import User
    from leads.models import DealStatus
    from django.core.exceptions import PermissionDenied
    from django.db.models import Count, Sum
    import json
    import calendar

    if request.user.role not in (User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER):
        return redirect("dashboard:home")

    today = timezone.localdate()
    user = request.user

    if user.hospital:
        # Business admin / manager: strictly scoped to assigned business only. Never permit cross-business queries.
        selected_hospital_id = str(user.hospital.id)
        base_leads = Lead.objects.filter(is_archived=False, hospital=user.hospital)
    else:
        # Global Super Admin without hospital assignment
        raw_biz = request.GET.get("business")
        if raw_biz is not None:
            selected_hospital_id = raw_biz.strip()
        else:
            raw_hosp = request.GET.get("hospital")
            if raw_hosp is not None:
                selected_hospital_id = raw_hosp.strip()
            else:
                selected_hospital_id = str(request.session.get("active_business_id", "")).strip()

        if selected_hospital_id and selected_hospital_id.isdigit():
            base_leads = Lead.objects.filter(is_archived=False, hospital_id=int(selected_hospital_id))
        elif selected_hospital_id == "none":
            base_leads = Lead.objects.filter(is_archived=False, hospital__isnull=True)
        else:
            # All businesses / Global view: include all leads across all businesses
            selected_hospital_id = ""
            base_leads = Lead.objects.filter(is_archived=False)

    # 1. Master lists for Filter Dropdowns (Cached per business/hospital for instant load)
    from django.core.cache import cache
    cache_scope = selected_hospital_id if selected_hospital_id else (str(user.hospital_id) if user.hospital_id else "all")
    cache_key = f"dash_filters_v3_{cache_scope}"
    filter_cache_data = cache.get(cache_key)

    if not filter_cache_data:
        raw_campaign_set = set()
        raw_source_set = set()
        raw_dept_set = set()
        raw_doc_set = set()
        raw_loc_set = set()
        db_years_set = set()

        for row in base_leads.order_by().values('location', 'campaign__name', 'lead_source__name', 'custom_data', 'inquiry_date', 'created_at'):
            c_rel = row.get('campaign__name')
            if c_rel and c_rel != 'nan':
                raw_campaign_set.add(c_rel)
            s_rel = row.get('lead_source__name')
            if s_rel and s_rel != 'nan':
                raw_source_set.add(s_rel)
            loc_col = row.get('location')
            if loc_col and loc_col not in ['nan', 'Not Mentioned', '']:
                raw_loc_set.add(loc_col)

            inq_d = row.get('inquiry_date')
            if inq_d:
                db_years_set.add(inq_d.year)
            elif row.get('created_at'):
                db_years_set.add(row.get('created_at').year)

            cd = row.get('custom_data') or {}
            if isinstance(cd, dict):
                c_custom = cd.get('campaign')
                if c_custom and c_custom != 'nan':
                    raw_campaign_set.add(c_custom)
                s_custom = cd.get('lead_source')
                if s_custom and s_custom != 'nan':
                    raw_source_set.add(s_custom)
                d_custom = cd.get('department')
                if d_custom and d_custom != 'nan':
                    raw_dept_set.add(d_custom)
                loc_custom = cd.get('location')
                if loc_custom and loc_custom not in ['nan', 'Not Mentioned', '']:
                    raw_loc_set.add(loc_custom)
                d_entry = cd.get('doctor')
                if d_entry and str(d_entry).strip() not in ['nan', 'Not Mentioned', '']:
                    for single_d in str(d_entry).split(','):
                        d_clean = single_d.strip()
                        if d_clean and d_clean not in ['Not Mentioned', 'DOCOTOR', 'DOCTOR']:
                            raw_doc_set.add(d_clean)

        raw_campaigns = sorted(list(raw_campaign_set))
        raw_sources = sorted(list(raw_source_set))
        raw_departments = sorted(list(raw_dept_set))
        raw_doctors = sorted(list(raw_doc_set))
        raw_locations = sorted(list(raw_loc_set))

        raw_age_groups = ["Child", "Adult", "Old Age", "No Age Data"]
        raw_weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        raw_months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]

        if today.year not in db_years_set:
            db_years_set.add(today.year)
        available_years = sorted(list(db_years_set), reverse=True)

        filter_cache_data = {
            "raw_campaigns": raw_campaigns,
            "raw_sources": raw_sources,
            "raw_departments": raw_departments,
            "raw_doctors": raw_doctors,
            "raw_locations": raw_locations,
            "raw_age_groups": raw_age_groups,
            "raw_weekdays": raw_weekdays,
            "raw_months": raw_months,
            "available_years": available_years,
        }
        cache.set(cache_key, filter_cache_data, 3600)
    else:
        raw_campaigns = filter_cache_data["raw_campaigns"]
        raw_sources = filter_cache_data["raw_sources"]
        raw_departments = filter_cache_data["raw_departments"]
        raw_doctors = filter_cache_data["raw_doctors"]
        raw_locations = filter_cache_data["raw_locations"]
        raw_age_groups = filter_cache_data["raw_age_groups"]
        raw_weekdays = filter_cache_data["raw_weekdays"]
        raw_months = filter_cache_data["raw_months"]
        available_years = filter_cache_data["available_years"]

    # 2. Extract GET Filter Parameters
    time_filter = request.GET.get('time_filter', '').strip()
    custom_start = request.GET.get('start_date', '').strip()
    custom_end = request.GET.get('end_date', '').strip()
    year_filter = request.GET.get('year', '').strip()
    month_filter = request.GET.get('month', '').strip()
    weekday_filter = request.GET.get('weekday', '').strip()
    campaign_filter = request.GET.get('campaign', '').strip()
    source_filter = request.GET.get('source', '').strip()
    department_filter = request.GET.get('department', '').strip()
    doctor_filter = request.GET.get('doctor', '').strip()
    location_filter = request.GET.get('location', '').strip()
    gender_filter = request.GET.get('gender', '').strip()
    age_group_filter = request.GET.get('age_group', '').strip()
    payment_type_filter = request.GET.get('payment_type', '').strip() # 'all_paid', 'opd', 'pharmacy', 'ipd', 'investigation', 'unpaid'
    final_status_filter = request.GET.get('final_lead_status', '').strip()

    # 3. Apply Filters:
    # If any specific slicer filter is applied, the restrictive date filter (e.g. time_filter=today)
    # is automatically removed so the filter applies across ALL leads (all_time), unless custom dates are provided.
    has_specific_dropdown = any([year_filter, month_filter, weekday_filter, campaign_filter, source_filter, department_filter, doctor_filter, location_filter, gender_filter, age_group_filter, payment_type_filter, final_status_filter])
    
    if custom_start or custom_end:
        time_filter = 'custom'
    elif has_specific_dropdown:
        time_filter = 'all_time'
    elif not time_filter:
        time_filter = 'today'

    from datetime import datetime, date
    import calendar
    start_of_today = timezone.make_aware(datetime.combine(today, datetime.min.time()))
    end_of_today = timezone.make_aware(datetime.combine(today, datetime.max.time()))
    today_str = today.isoformat()

    start_of_month = timezone.make_aware(datetime(today.year, today.month, 1, 0, 0, 0))
    _, last_day = calendar.monthrange(today.year, today.month)
    end_of_month = timezone.make_aware(datetime(today.year, today.month, last_day, 23, 59, 59))
    start_date_month = date(today.year, today.month, 1)
    end_date_month = date(today.year, today.month, last_day)

    if year_filter:
        try:
            y_int = int(year_filter)
            base_leads = base_leads.filter(Q(inquiry_date__year=y_int) | Q(custom_data__year=str(y_int)))
        except ValueError:
            pass

    if month_filter:
        month_idx = None
        for i, m_name in enumerate(raw_months, 1):
            if m_name.lower() == month_filter.lower():
                month_idx = i
                break
        if month_idx:
            base_leads = base_leads.filter(Q(inquiry_date__month=month_idx) | Q(custom_data__month__iexact=month_filter))
        else:
            base_leads = base_leads.filter(custom_data__month__iexact=month_filter)

    if weekday_filter:
        base_leads = base_leads.filter(custom_data__week_day__iexact=weekday_filter)

    if campaign_filter:
        base_leads = base_leads.filter(Q(campaign__name__iexact=campaign_filter) | Q(custom_data__campaign__iexact=campaign_filter))

    if source_filter:
        base_leads = base_leads.filter(Q(lead_source__name__iexact=source_filter) | Q(custom_data__lead_source__iexact=source_filter))

    if department_filter:
        base_leads = base_leads.filter(custom_data__department__iexact=department_filter)

    if doctor_filter:
        base_leads = base_leads.filter(custom_data__doctor__icontains=doctor_filter)

    if location_filter:
        base_leads = base_leads.filter(Q(location__iexact=location_filter) | Q(custom_data__location__iexact=location_filter))

    if gender_filter:
        base_leads = base_leads.filter(custom_data__gender__iexact=gender_filter)

    if age_group_filter:
        base_leads = base_leads.filter(custom_data__age_group__iexact=age_group_filter)

    # Payment component filter
    if payment_type_filter == 'all_paid':
        base_leads = base_leads.filter(deal_status=DealStatus.WON)
    elif payment_type_filter == 'opd':
        base_leads = base_leads.filter(custom_data__opd_bill__gt='0')
    elif payment_type_filter == 'pharmacy':
        base_leads = base_leads.filter(custom_data__pharmacy_bill__gt='0')
    elif payment_type_filter == 'ipd':
        base_leads = base_leads.filter(custom_data__ipd_bill__gt='0')
    elif payment_type_filter == 'investigation':
        base_leads = base_leads.filter(custom_data__investigation_bill__gt='0')
    elif payment_type_filter == 'unpaid':
        base_leads = base_leads.exclude(deal_status=DealStatus.WON)

    # Final Lead Status (Doughnut Slicer) filter
    if final_status_filter:
        fls_norm = final_status_filter.strip().upper()
        if fls_norm == 'PAYMENT DONE':
            base_leads = base_leads.filter(
                Q(deal_status=DealStatus.WON) |
                Q(custom_data__total_paid__gt='0') |
                Q(custom_data__total__gt='0') |
                Q(custom_data__deal_status__icontains='won') |
                Q(custom_data__deal_status__icontains='Payment Done')
            )
        elif fls_norm == 'BOOKING CONFIRMED':
            base_leads = base_leads.filter(
                Q(custom_data__appointment_status__icontains='book') |
                Q(custom_data__appointment_status__icontains='confirm') |
                Q(custom_data__appointment_confirmed_at__isnull=False)
            )
        elif fls_norm == 'LOST':
            base_leads = base_leads.filter(
                Q(deal_status=DealStatus.LOST) |
                Q(custom_data__deal_status__icontains='Lost') |
                Q(custom_data__appointment_status__icontains='cancel') |
                Q(custom_data__appointment_status__icontains='lost')
            )
        else:
            base_leads = base_leads.filter(
                Q(custom_data__priority__iexact=final_status_filter) |
                Q(custom_data__appointment_status__iexact=final_status_filter) |
                Q(custom_data__deal_status__iexact=final_status_filter) |
                Q(temperature__iexact=final_status_filter)
            )

    # fu_leads_base contains all tenant leads matching applied slicers, without created_at restriction
    fu_leads_base = base_leads

    filter_label = "Today"
    period_title_prefix = "Today's"
    if time_filter == 'today':
        base_leads = base_leads.filter(
            Q(created_at__range=(start_of_today, end_of_today)) | Q(inquiry_date=today)
        )
        filter_label = f"Today ({today.strftime('%d %b %Y')})"
        period_title_prefix = "Today's"
    elif time_filter == 'this_month':
        base_leads = base_leads.filter(
            Q(created_at__range=(start_of_month, end_of_month)) |
            Q(inquiry_date__range=(start_date_month, end_date_month))
        )
        filter_label = f"This Month ({today.strftime('%B %Y')})"
        period_title_prefix = "This Month's"
    elif time_filter == 'all_time':
        # No date boundary filter on base_leads -> displays all lifetime records
        filter_label = "All Time"
        period_title_prefix = "All Time"
    elif time_filter == 'custom':
        filter_label = "Custom Date Range"
        period_title_prefix = "Period"
        if custom_start:
            base_leads = base_leads.filter(Q(inquiry_date__gte=custom_start) | (Q(inquiry_date__isnull=True) & Q(created_at__date__gte=custom_start)))
        if custom_end:
            base_leads = base_leads.filter(Q(inquiry_date__lte=custom_end) | (Q(inquiry_date__isnull=True) & Q(created_at__date__lte=custom_end)))
        if custom_start and custom_end:
            filter_label = f"{custom_start} to {custom_end}"
    elif time_filter.startswith('year_'):
        try:
            sel_year = int(time_filter.replace('year_', ''))
            base_leads = base_leads.filter(
                Q(inquiry_date__year=sel_year) | (Q(inquiry_date__isnull=True) & Q(created_at__year=sel_year))
            )
            filter_label = f"Year {sel_year}"
            period_title_prefix = f"Year {sel_year}"
        except ValueError:
            pass

    # 4. Aggregations & Analytical Calculations for Nelson Hospital
    from collections import defaultdict

    # Base queryset for hospital tenant (inherits all applied filters: campaign, source, department, doctor, location, date, etc.)
    hospital_all_leads = base_leads

    # =========================================================================
    # CARD 1: TODAY'S / PERIOD NEW LEADS (Excel Imported + Organic + Direct Walk-in)
    # =========================================================================
    if time_filter == 'today':
        period_new_leads_qs = hospital_all_leads.filter(
            Q(created_at__range=(start_of_today, end_of_today)) |
            Q(inquiry_date=today)
        ).distinct()
    elif time_filter == 'this_month':
        period_new_leads_qs = hospital_all_leads.filter(
            Q(created_at__range=(start_of_month, end_of_month)) |
            Q(inquiry_date__range=(start_date_month, end_date_month))
        ).distinct()
    else:
        # custom date range, all_time, year_*, etc. (already scoped in hospital_all_leads)
        period_new_leads_qs = hospital_all_leads.distinct()

    todays_new_leads_count = period_new_leads_qs.count()

    # Explicit sub-counts & Breakdown for Today's / Period New Leads in a single fast pass
    todays_campaign_leads_count = 0
    todays_organic_leads_count = 0
    todays_walkin_leads_count = 0

    card1_breakdown = defaultdict(lambda: {
        'total': 0, 'contacted': 0, 'not_contacted': 0, 'appointment_booked': 0, 'campaigns': set()
    })

    card1_rows = period_new_leads_qs.order_by().values(
        'lead_source__name', 'campaign__name', 'import_job_id', 'import_source_file',
        'custom_data', 'temperature'
    )

    for row in card1_rows:
        cd = row.get('custom_data') or {}
        src_name = row.get('lead_source__name') or cd.get('lead_source') or ''
        src_name_l = src_name.lower()

        if 'walk-in' in src_name_l or 'direct' in src_name_l:
            todays_walkin_leads_count += 1
            cat_name = "Walk-in Leads (Form Registered)"
        elif 'organic' in src_name_l or 'website' in src_name_l or 'google' in src_name_l:
            todays_organic_leads_count += 1
            cat_name = "Organic Leads (Inquiries)"
        elif row.get('import_job_id') or row.get('import_source_file') or 'import' in src_name_l:
            todays_campaign_leads_count += 1
            cat_name = "Excel / Ads Imported Leads"
        else:
            todays_campaign_leads_count += 1
            cat_name = f"Campaign Leads ({src_name or 'Meta/Social'})"

        card1_breakdown[cat_name]['total'] += 1
        c_name = row.get('campaign__name') or cd.get('campaign') or 'General'
        if c_name:
            card1_breakdown[cat_name]['campaigns'].add(c_name)

        temp_v = row.get('temperature')
        has_contact = bool(cd.get('remark_1') or cd.get('lead_calling_time') or (temp_v in ['WARM', 'COLD'] and temp_v != 'HOT'))
        is_appt = bool('book' in str(cd.get('appointment_status', '')).lower() or cd.get('appo_booked_date') or str(cd.get('appo_book', '')).lower() in ['yes', 'booked'])

        if is_appt:
            card1_breakdown[cat_name]['appointment_booked'] += 1
        elif has_contact:
            card1_breakdown[cat_name]['contacted'] += 1
        else:
            card1_breakdown[cat_name]['not_contacted'] += 1

    card1_breakdown_list = [
        {
            "category_name": cat,
            "total": stats["total"],
            "contacted": stats["contacted"],
            "not_contacted": stats["not_contacted"],
            "appointment_booked": stats["appointment_booked"],
            "campaigns_count": len(stats["campaigns"]),
        }
        for cat, stats in sorted(card1_breakdown.items(), key=lambda x: x[1]['total'], reverse=True)
    ]

    # =========================================================================
    # CARD 2: CALL NOT DONE LEADS (Uncontacted / Open Leads - Today / Selected Period)
    # Excludes any leads whose status has been updated (Booked, Payment Done, Cancelled, etc.) or called
    # =========================================================================
    call_not_done_base = hospital_all_leads

    if time_filter == 'today':
        cnd_raw_qs = call_not_done_base.filter(
            Q(created_at__range=(start_of_today, end_of_today)) | Q(inquiry_date=today)
        ).distinct()
    elif time_filter == 'this_month':
        cnd_raw_qs = call_not_done_base.filter(
            Q(created_at__range=(start_of_month, end_of_month)) |
            Q(inquiry_date__range=(start_date_month, end_date_month))
        ).distinct()
    else: # all_time or custom date range
        cnd_raw_qs = call_not_done_base.distinct()

    cnd_matched_ids = filter_uncontacted_leads_ids(cnd_raw_qs, today=today)
    call_not_done_count = len(cnd_matched_ids)

    card2_breakdown = defaultdict(lambda: {'total': 0, 'unassigned': 0, 'hot': 0, 'uncontacted': 0})
    if cnd_matched_ids:
        cnd_sample_rows = hospital_all_leads.filter(id__in=cnd_matched_ids[:200]).order_by().values(
            'campaign__name', 'custom_data', 'assigned_to_id', 'temperature'
        )
        for r in cnd_sample_rows:
            cd = r.get('custom_data') or {}
            c_name = r.get('campaign__name') or cd.get('campaign') or 'General / Direct'
            card2_breakdown[c_name]['total'] += 1
            if not r.get('assigned_to_id'):
                card2_breakdown[c_name]['unassigned'] += 1
            if r.get('temperature') == 'HOT':
                card2_breakdown[c_name]['hot'] += 1
            else:
                card2_breakdown[c_name]['uncontacted'] += 1

    card2_breakdown_list = [
        {
            "campaign_name": c,
            "total": stats["total"],
            "unassigned": stats["unassigned"],
            "hot": stats["hot"],
            "uncontacted": stats["uncontacted"],
        }
        for c, stats in sorted(card2_breakdown.items(), key=lambda x: x[1]['total'], reverse=True)
    ]

    # =========================================================================
    # CARD 3: TODAY'S / PERIOD OPD / APPOINTMENT BOOKED
    # =========================================================================
    today_alt_str = today.strftime("%d-%m-%Y")
    all_booked_status_q = (
        Q(admission_status="ADMISSION_DONE") |
        Q(deal_status=DealStatus.WON) |
        Q(admission__isnull=False) |
        Q(stage__name__icontains="admission") |
        Q(custom_data__appointment_status__iexact="OPD Booking") |
        Q(custom_data__appointment_status__icontains="OPD") |
        Q(custom_data__appointment_status__icontains="Book") |
        Q(custom_data__appointment_status__icontains="Confirm") |
        Q(custom_data__appointment_status__icontains="Approv") |
        Q(custom_data__appointment_status__icontains="Complete") |
        Q(custom_data__appointment_status__iexact="YES") |
        Q(custom_data__appointment_status__icontains="Payment Done") |
        (Q(custom_data__total__isnull=False) & ~Q(custom_data__total__in=["0", "0.00", "", "0.0", 0, 0.0]))
    )
    consult_status_q = (
        Q(custom_data__appointment_status__icontains='Consult') |
        Q(custom_data__department__icontains='Consult') |
        Q(notes__icontains='consult')
    )

    if time_filter == 'today':
        base_filter_q = (
            Q(created_at__range=(start_of_today, end_of_today)) |
            Q(inquiry_date=today) |
            Q(admission__admission_date=today) |
            Q(admission__created_at__range=(start_of_today, end_of_today)) |
            Q(custom_data__admission_date=today_str) |
            Q(custom_data__admission_date=today_alt_str) |
            Q(custom_data__appo_booked_date=today_str) |
            Q(custom_data__appo_booked_date=today_alt_str) |
            Q(custom_data__appointment_date=today_str) |
            Q(custom_data__appointment_date=today_alt_str) |
            Q(custom_data__appointment_confirmed_at__startswith=today_str)
        )
    elif time_filter == 'this_month':
        base_filter_q = (
            Q(created_at__range=(start_of_month, end_of_month)) |
            Q(inquiry_date__range=(start_date_month, end_date_month)) |
            Q(custom_data__appo_booked_date__startswith=today.strftime('%Y-%m'))
        )
    else: # all_time or custom date range
        base_filter_q = Q()

    appts_booked_qs = hospital_all_leads.filter(all_booked_status_q).filter(base_filter_q).order_by().distinct()
    appts_booked_count = appts_booked_qs.count()

    consultation_booked_count = appts_booked_qs.filter(consult_status_q).distinct().count()
    opd_booked_count = max(0, appts_booked_count - consultation_booked_count)

    card3_breakdown = defaultdict(lambda: {'total': 0, 'completed': 0, 'scheduled': 0})
    for l in appts_booked_qs.values('custom_data'):
        cd = l.get('custom_data') or {}
        doc_name = cd.get('doctor') or 'General OPD Consultation'
        card3_breakdown[doc_name]['total'] += 1
        st = str(cd.get('appointment_status', '')).lower()
        if 'complete' in st or 'visit' in st or 'done' in st:
            card3_breakdown[doc_name]['completed'] += 1
        else:
            card3_breakdown[doc_name]['scheduled'] += 1

    card3_breakdown_list = [
        {
            "doctor_name": doc,
            "total": stats["total"],
            "completed": stats["completed"],
            "scheduled": stats["scheduled"],
        }
        for doc, stats in sorted(card3_breakdown.items(), key=lambda x: x[1]['total'], reverse=True)
    ]

    # =========================================================================
    # CARD 4: TODAY'S / PERIOD FOLLOW-UPS (Pending / Due Follow-ups)
    # =========================================================================
    # Exclude won/lost as well as active Booked / Booking Confirmed OPD leads (which belong to OPD Card 3)
    booked_exclude_q = (
        Q(custom_data__appointment_status__icontains='Book') |
        Q(custom_data__appointment_status__icontains='Confirm') |
        Q(deal_status__in=[DealStatus.WON, DealStatus.LOST]) |
        Q(admission_status='ADMISSION_DONE') |
        (Q(custom_data__total__isnull=False) & ~Q(custom_data__total__in=['0', '0.00', '', '0.0', 0, 0.0]))
    )

    all_fu_leads_qs = fu_leads_base.exclude(booked_exclude_q).filter(
        Q(next_followup_date__isnull=False) |
        Q(followups__next_followup_date__isnull=False) |
        Q(custom_data__appointment_status__icontains='follow')
    ).order_by().distinct()

    todays_followups_list = []
    overdue_followups_list = []
    upcoming_followups_list = []
    this_month_followups_list = []
    custom_range_followups_list = []

    custom_s_d = None
    custom_e_d = None
    if custom_start:
        try:
            custom_s_d = datetime.strptime(custom_start, '%Y-%m-%d').date()
        except ValueError:
            pass
    if custom_end:
        try:
            custom_e_d = datetime.strptime(custom_end, '%Y-%m-%d').date()
        except ValueError:
            pass

    for l in all_fu_leads_qs.select_related('assigned_to', 'stage', 'campaign', 'lead_source'):
        sched_date = l.next_followup_date
        if not sched_date:
            latest_fu = l.followups.filter(next_followup_date__isnull=False).order_by('-id').first()
            if latest_fu:
                sched_date = latest_fu.next_followup_date
        if not sched_date:
            continue

        cd = l.custom_data or {}

        if sched_date == today:
            todays_followups_list.append(l)
        elif sched_date < today:
            overdue_followups_list.append(l)
        elif sched_date > today:
            upcoming_followups_list.append(l)

        # Month followups: scheduled in this month or active overdue from earlier
        if start_date_month <= sched_date <= end_date_month:
            this_month_followups_list.append(l)
        elif sched_date < start_date_month:
            this_month_followups_list.append(l)

        # Custom date range followups
        if custom_s_d and custom_e_d:
            if custom_s_d <= sched_date <= custom_e_d:
                custom_range_followups_list.append(l)
            elif sched_date < custom_s_d:
                custom_range_followups_list.append(l)
        elif custom_s_d and not custom_e_d:
            if sched_date >= custom_s_d:
                custom_range_followups_list.append(l)
        elif custom_e_d and not custom_s_d:
            if sched_date <= custom_e_d:
                custom_range_followups_list.append(l)

    todays_followups_count = len(todays_followups_list)
    overdue_followups_count = len(overdue_followups_list)
    upcoming_followups_count = len(upcoming_followups_list)

    if time_filter == 'today':
        period_followups_list = todays_followups_list
    elif time_filter == 'this_month':
        period_followups_list = this_month_followups_list
    elif time_filter == 'custom':
        period_followups_list = custom_range_followups_list
    elif time_filter == 'all_time':
        period_followups_list = overdue_followups_list + todays_followups_list + upcoming_followups_list
    else:
        period_followups_list = overdue_followups_list + todays_followups_list + upcoming_followups_list

    period_followups_count = len(period_followups_list)

    # For breakdown by attendant on period followups
    card4_breakdown = defaultdict(lambda: {'total': 0, 'pending': 0, 'done': 0})
    for l in period_followups_list:
        attendant = l.assigned_to.get_full_name() if l.assigned_to else "Unassigned Staff"
        card4_breakdown[attendant]['total'] += 1
        cd = l.custom_data or {}
        if cd.get('lead_calling_time') or (l.temperature in ['WARM', 'COLD'] and l.temperature != 'HOT'):
            card4_breakdown[attendant]['done'] += 1
        else:
            card4_breakdown[attendant]['pending'] += 1

    card4_breakdown_list = [
        {
            "attendant_name": att,
            "total": stats["total"],
            "pending": stats["pending"],
            "done": stats["done"],
        }
        for att, stats in sorted(card4_breakdown.items(), key=lambda x: x[1]['total'], reverse=True)
    ]


    # =========================================================================
    # CARD 5: TODAY'S WALK-IN LEADS
    # =========================================================================
    todays_walkin_qs = period_new_leads_qs.filter(
        Q(lead_source__name__icontains='walk-in') |
        Q(custom_data__lead_source__icontains='walk-in') |
        Q(custom_data__source__icontains='walk-in')
    ).order_by().distinct()

    todays_walkin_count = todays_walkin_qs.count()

    card5_breakdown = defaultdict(lambda: {'total': 0, 'dept': '', 'booked': 0})
    for l in todays_walkin_qs.values('custom_data'):
        cd = l.get('custom_data') or {}
        dept = cd.get('department') or 'General OPD'
        card5_breakdown[dept]['total'] += 1
        if 'book' in str(cd.get('appointment_status', '')).lower():
            card5_breakdown[dept]['booked'] += 1

    card5_breakdown_list = [
        {
            "department_name": dept,
            "total": stats["total"],
            "booked": stats["booked"],
        }
        for dept, stats in sorted(card5_breakdown.items(), key=lambda x: x[1]['total'], reverse=True)
    ]

    total_leads = base_leads.count()

    # Dynamic Distributions
    location_dist = {}
    month_dist = {}
    department_dist = {}
    doctor_dist = {}
    campaign_dist = {}
    source_dist = {}
    appo_status_dist = {}
    final_status_dist = {}
    year_dist = {}
    age_group_dist = {}
    gender_dist = {}
    weekday_dist = {}

    fast_leads = base_leads.order_by().values(
        'id', 'location', 'campaign__name', 'lead_source__name', 'custom_data',
        'inquiry_date', 'created_at', 'deal_status', 'temperature', 'stage__name'
    )

    for l in fast_leads:
        cd = l.get('custom_data') or {}

        # 1. Location
        loc = l.get('location') or cd.get('location') or 'Not Mentioned'
        loc = loc.strip().title() if loc else 'Not Mentioned'
        location_dist[loc] = location_dist.get(loc, 0) + 1

        # 2. Month
        lead_date = l.get('created_at').date() if l.get('created_at') else (l.get('inquiry_date') or today)
        m_name = cd.get('month') or lead_date.strftime('%B')
        m_name = m_name.strip().title()
        month_dist[m_name] = month_dist.get(m_name, 0) + 1

        # 3. Department
        dept = cd.get('department') or 'General OPD'
        dept = dept.strip().upper()
        department_dist[dept] = department_dist.get(dept, 0) + 1

        # 4. Doctor (support individual counts if multiple assigned)
        raw_doc_str = cd.get('doctor') or 'Not Mentioned'
        if raw_doc_str in ['nan', 'None', '', 'Not Mentioned', 'DOCOTOR', 'DOCTOR']:
            doctor_dist['Not Mentioned'] = doctor_dist.get('Not Mentioned', 0) + 1
        else:
            for s_doc in str(raw_doc_str).split(','):
                s_doc_clean = s_doc.strip().title()
                if s_doc_clean and s_doc_clean not in ['Not Mentioned', 'Docotor', 'Doctor']:
                    doctor_dist[s_doc_clean] = doctor_dist.get(s_doc_clean, 0) + 1

        # 5. Campaign
        camp = l.get('campaign__name') or cd.get('campaign') or 'Nelson General Campaign'
        camp = camp.strip()
        campaign_dist[camp] = campaign_dist.get(camp, 0) + 1

        # 6. Lead Source
        src = l.get('lead_source__name') or cd.get('lead_source') or 'Instagram'
        src = src.strip()
        source_dist[src] = source_dist.get(src, 0) + 1

        # 7. Appointment Status
        appo_st = cd.get('appointment_status') or 'NA'
        appo_st = str(appo_st).strip().upper()
        if not appo_st or appo_st in ['NAN', 'NONE']: appo_st = 'NA'
        appo_status_dist[appo_st] = appo_status_dist.get(appo_st, 0) + 1

        # 8. Final Lead Status / Temperature Chart (Ultra-fast in-memory)
        tot = 0.0
        try:
            tot = float(cd.get("total_paid") or cd.get("total") or 0.0)
        except (ValueError, TypeError):
            tot = 0.0
        raw_ds = str(cd.get("deal_status") or l.get('stage__name') or "").strip()
        prio_tag = cd.get("priority")
        if prio_tag and str(prio_tag).lower() not in ("nan", "none", ""):
            fls = str(prio_tag).upper()
        elif tot > 0 or l.get('deal_status') == DealStatus.WON or 'won' in raw_ds.lower():
            fls = "PAYMENT DONE"
        elif 'book' in appo_st.lower() or 'confirm' in appo_st.lower() or cd.get('appointment_confirmed_at'):
            fls = "BOOKING CONFIRMED"
        elif 'cancel' in appo_st.lower() or 'lost' in appo_st.lower() or l.get('deal_status') == DealStatus.LOST:
            fls = "LOST"
        elif appo_st and appo_st not in ['NAN', 'NONE', 'NA']:
            fls = appo_st
        elif raw_ds and raw_ds.upper() not in ['NAN', 'NONE']:
            fls = raw_ds.upper()
        else:
            fls = str(l.get('temperature') or 'OPEN').upper()
        final_status_dist[fls] = final_status_dist.get(fls, 0) + 1

        # 9. Year
        yr = str(cd.get('year') or (lead_date.year if lead_date else '2026')).strip()
        year_dist[yr] = year_dist.get(yr, 0) + 1

        # 10. Age Group & Gender
        ag = str(cd.get('age_group') or 'No Age Data').strip()
        age_group_dist[ag] = age_group_dist.get(ag, 0) + 1

        gen = str(cd.get('gender') or 'Not Mentioned').strip().title()
        gender_dist[gen] = gender_dist.get(gen, 0) + 1

        # 11. Weekday
        inq_d_val = l.get('inquiry_date')
        wd = str(cd.get('week_day') or (inq_d_val.strftime('%A') if inq_d_val else 'Thursday')).strip().title()
        weekday_dist[wd] = weekday_dist.get(wd, 0) + 1

    # Order locations and departments by highest count
    location_dist_sorted = dict(sorted(location_dist.items(), key=lambda item: item[1], reverse=True)[:15])
    department_dist_sorted = dict(sorted(department_dist.items(), key=lambda item: item[1], reverse=True)[:15])
    doctor_dist_sorted = dict(sorted(doctor_dist.items(), key=lambda item: item[1], reverse=True)[:12])
    campaign_dist_sorted = dict(sorted(campaign_dist.items(), key=lambda item: item[1], reverse=True)[:10])
    source_dist_sorted = dict(sorted(source_dist.items(), key=lambda item: item[1], reverse=True)[:10])

    insights = {
        "total_leads": total_leads,
        "todays_new_leads": todays_new_leads_count,
        "todays_campaign_leads": todays_campaign_leads_count,
        "todays_organic_leads": todays_organic_leads_count,
        "todays_walkin_leads": todays_walkin_leads_count,
        "call_not_done": call_not_done_count,
        "appointments_booked": appts_booked_count,
        "opd_booked": opd_booked_count,
        "consultation_booked": consultation_booked_count,
        "todays_followups": period_followups_count,
        "period_followups": period_followups_count,
        "overdue_followups": overdue_followups_count,
        "upcoming_followups": upcoming_followups_count,
        "todays_walkin": todays_walkin_count,
        "period_title_prefix": period_title_prefix,
        
        # Charts Data JSON Formatted (Passing full dictionaries for dynamic Top N & click slicing)
        "location_distribution": location_dist,
        "month_distribution": month_dist,
        "department_distribution": department_dist,
        "doctor_distribution": doctor_dist,
        "campaign_distribution": campaign_dist,
        "source_distribution": source_dist,
        "appointment_status_distribution": appo_status_dist,
        "final_lead_status_distribution": final_status_dist,
        "year_distribution": year_dist,
        "age_group_distribution": age_group_dist,
        "gender_distribution": gender_dist,
        "weekday_distribution": weekday_dist,
    }

    has_active_filters = any([
        time_filter not in ['today', ''], custom_start, custom_end, year_filter, month_filter, weekday_filter,
        campaign_filter, source_filter, department_filter, doctor_filter, location_filter,
        gender_filter, age_group_filter, payment_type_filter, final_status_filter
    ])

    context = {
        "active": "superadmin_home",
        "today": today,
        "today_str": today.isoformat(),
        "insights": insights,
        "insights_json": json.dumps(insights),
        "period_title_prefix": period_title_prefix,
        "todays_campaign_breakdown": card1_breakdown_list,
        "card1_breakdown": card1_breakdown_list,
        "card2_breakdown": card2_breakdown_list,
        "card3_breakdown": card3_breakdown_list,
        "card4_breakdown": card4_breakdown_list,
        "card5_breakdown": card5_breakdown_list,
        "filter_label": filter_label,
        
        # Filter options
        "campaigns": raw_campaigns,
        "lead_sources": raw_sources,
        "departments": raw_departments,
        "doctors": raw_doctors,
        "locations": raw_locations,
        "age_groups": raw_age_groups,
        "weekdays": raw_weekdays,
        "months": raw_months,
        "available_years": available_years,

        # Current Filter Values
        "current_campaign": campaign_filter,
        "current_source": source_filter,
        "current_department": department_filter,
        "current_doctor": doctor_filter,
        "current_location": location_filter,
        "current_gender": gender_filter,
        "current_age_group": age_group_filter,
        "current_year": year_filter,
        "current_month": month_filter,
        "current_weekday": weekday_filter,
        "current_payment_type": payment_type_filter,
        "current_final_status": final_status_filter,
        "time_filter": time_filter,
        "custom_start": custom_start,
        "custom_end": custom_end,
        "has_active_filters": has_active_filters,
        "selected_hospital_id": selected_hospital_id,
    }
    return render(request, "dashboard/nel_admin_home.html", context)


@login_required
def nel_card_drilldown_api(request):
    """
    Interactive API for Nelson Hospital Dashboard KPI Cards.
    Supports dynamic modes: 'today', 'previous' (yesterday/prev day), 'next' (tomorrow/future), 'all', 'custom'.
    Returns:
      - card_stats: metric count for requested date/mode
      - lead_items: leads list with real-time status, temperature, attendant, and direct links
      - calendar_counts: map of { 'YYYY-MM-DD': count } for the month calendar picker
    """
    from django.http import JsonResponse
    from datetime import datetime, date, timedelta
    import calendar
    from collections import defaultdict
    from django.utils import timezone
    from leads.models import Lead, DealStatus
    from accounts.models import User

    user = request.user
    card_type = request.GET.get('card_type', 'new_leads').strip() # 'new_leads', 'call_not_done', 'opd_booked', 'followups', 'walkin'
    mode = request.GET.get('mode', 'today').strip() # 'today', 'previous', 'next', 'all', 'custom', 'date_range'
    target_date_str = request.GET.get('target_date', '').strip()
    start_date_param = request.GET.get('start_date', '').strip()
    end_date_param = request.GET.get('end_date', '').strip()
    year_param = request.GET.get('year', '')
    month_param = request.GET.get('month', '')

    today = timezone.localdate()

    # Determine date range or reference date
    range_start_date = None
    range_end_date = None
    if start_date_param:
        try:
            range_start_date = datetime.strptime(start_date_param, '%Y-%m-%d').date()
        except ValueError:
            range_start_date = None

    if end_date_param:
        try:
            range_end_date = datetime.strptime(end_date_param, '%Y-%m-%d').date()
        except ValueError:
            range_end_date = None

    if target_date_str:
        try:
            current_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
        except ValueError:
            current_date = today
    else:
        current_date = today

    month_range_start = None
    month_range_end = None

    if mode == 'date_range' and (range_start_date or range_end_date):
        selected_date = None
        if range_start_date and not range_end_date:
            range_end_date = today
        elif range_end_date and not range_start_date:
            range_start_date = range_end_date
        
        if range_start_date and range_end_date and range_start_date > range_end_date:
            range_start_date, range_end_date = range_end_date, range_start_date
            
        r_start_dt = timezone.make_aware(datetime.combine(range_start_date, datetime.min.time()))
        r_end_dt = timezone.make_aware(datetime.combine(range_end_date, datetime.max.time()))
    elif mode == 'previous':
        selected_date = current_date - timedelta(days=1)
    elif mode == 'next':
        selected_date = current_date + timedelta(days=1)
    elif mode == 'today':
        selected_date = today
    elif mode == 'custom':
        selected_date = current_date
    elif mode == 'this_month':
        selected_date = None
        m_start_date = today.replace(day=1)
        _, last_day = calendar.monthrange(today.year, today.month)
        m_end_date = today.replace(day=last_day)
        month_range_start = timezone.make_aware(datetime.combine(m_start_date, datetime.min.time()))
        month_range_end = timezone.make_aware(datetime.combine(m_end_date, datetime.max.time()))
    else: # mode == 'all'
        selected_date = None

    # Base tenant queryset
    if user.hospital:
        hospital_qs = Lead.objects.filter(is_archived=False, hospital=user.hospital)
        selected_hospital_id = str(user.hospital.id)
        if not user.can_view_all_leads:
            if user.can_view_team_leads:
                team = User.objects.filter(reports_to=user)
                hospital_qs = hospital_qs.filter(Q(assigned_to=user) | Q(assigned_to__in=team))
            elif user.role == User.Role.MANAGER:
                team = User.objects.filter(reports_to=user)
                hospital_qs = hospital_qs.filter(Q(assigned_to=user) | Q(assigned_to__in=team) | Q(assigned_to__isnull=True))
            elif user.can_view_assigned_leads or user.role in (User.Role.COUNSELLOR, User.Role.HR, User.Role.LEAD_ATTENDENT):
                hospital_qs = hospital_qs.filter(Q(assigned_to=user) | Q(created_by=user) | Q(assigned_to__isnull=True))
    else:
        raw_biz = request.GET.get("business")
        if raw_biz is not None:
            selected_hospital_id = raw_biz.strip()
        else:
            raw_hosp = request.GET.get("hospital")
            if raw_hosp is not None:
                selected_hospital_id = raw_hosp.strip()
            else:
                selected_hospital_id = str(getattr(request, 'session', {}).get("active_business_id", "")).strip()

        if selected_hospital_id and selected_hospital_id.isdigit():
            hospital_qs = Lead.objects.filter(is_archived=False, hospital_id=int(selected_hospital_id))
        elif selected_hospital_id in ("zappcode", "none"):
            hospital_qs = Lead.objects.filter(is_archived=False, hospital__isnull=True)
        else:
            selected_hospital_id = ""
            hospital_qs = Lead.objects.filter(is_archived=False)

    # Extract Active Slicer Filters
    campaign_filter = request.GET.get('campaign', '').strip()
    source_filter = request.GET.get('source', '').strip()
    department_filter = request.GET.get('department', '').strip()
    doctor_filter = request.GET.get('doctor', '').strip()
    location_filter = request.GET.get('location', '').strip()
    gender_filter = request.GET.get('gender', '').strip()
    age_group_filter = request.GET.get('age_group', '').strip()
    payment_type_filter = request.GET.get('payment_type', '').strip()
    final_status_filter = request.GET.get('final_lead_status', '').strip()

    if campaign_filter:
        hospital_qs = hospital_qs.filter(Q(campaign__name__iexact=campaign_filter) | Q(custom_data__campaign__iexact=campaign_filter))
    if source_filter:
        hospital_qs = hospital_qs.filter(Q(lead_source__name__iexact=source_filter) | Q(custom_data__lead_source__iexact=source_filter))
    if department_filter:
        hospital_qs = hospital_qs.filter(custom_data__department__iexact=department_filter)
    if doctor_filter:
        hospital_qs = hospital_qs.filter(custom_data__doctor__icontains=doctor_filter)
    if location_filter:
        hospital_qs = hospital_qs.filter(Q(location__iexact=location_filter) | Q(custom_data__location__iexact=location_filter))
    if gender_filter:
        hospital_qs = hospital_qs.filter(custom_data__gender__iexact=gender_filter)
    if age_group_filter:
        hospital_qs = hospital_qs.filter(custom_data__age_group__iexact=age_group_filter)

    if payment_type_filter == 'all_paid':
        hospital_qs = hospital_qs.filter(deal_status=DealStatus.WON)
    elif payment_type_filter == 'opd':
        hospital_qs = hospital_qs.filter(custom_data__opd_bill__gt='0')
    elif payment_type_filter == 'pharmacy':
        hospital_qs = hospital_qs.filter(custom_data__pharmacy_bill__gt='0')
    elif payment_type_filter == 'ipd':
        hospital_qs = hospital_qs.filter(custom_data__ipd_bill__gt='0')
    elif payment_type_filter == 'investigation':
        hospital_qs = hospital_qs.filter(custom_data__investigation_bill__gt='0')
    elif payment_type_filter == 'unpaid':
        hospital_qs = hospital_qs.exclude(deal_status=DealStatus.WON)

    if final_status_filter:
        fls_norm = final_status_filter.strip().upper()
        if fls_norm == 'PAYMENT DONE':
            hospital_qs = hospital_qs.filter(
                Q(deal_status=DealStatus.WON) |
                Q(custom_data__total_paid__gt='0') |
                Q(custom_data__total__gt='0') |
                Q(custom_data__deal_status__icontains='won') |
                Q(custom_data__deal_status__icontains='Payment Done')
            )
        elif fls_norm == 'BOOKING CONFIRMED':
            hospital_qs = hospital_qs.filter(
                Q(custom_data__appointment_status__icontains='book') |
                Q(custom_data__appointment_status__icontains='confirm') |
                Q(custom_data__appointment_confirmed_at__isnull=False)
            )
        elif fls_norm == 'LOST':
            hospital_qs = hospital_qs.filter(
                Q(deal_status=DealStatus.LOST) |
                Q(custom_data__deal_status__icontains='Lost') |
                Q(custom_data__appointment_status__icontains='cancel') |
                Q(custom_data__appointment_status__icontains='lost')
            )
        else:
            hospital_qs = hospital_qs.filter(
                Q(custom_data__priority__iexact=final_status_filter) |
                Q(custom_data__appointment_status__iexact=final_status_filter) |
                Q(custom_data__deal_status__iexact=final_status_filter) |
                Q(temperature__iexact=final_status_filter)
            )

    start_dt = timezone.make_aware(datetime.combine(selected_date, datetime.min.time())) if selected_date else None
    end_dt = timezone.make_aware(datetime.combine(selected_date, datetime.max.time())) if selected_date else None
    sel_date_str = selected_date.isoformat() if selected_date else ""

    # Build base card queryset (without date restriction for calendar heatmap computation)
    if card_type == 'new_leads':
        base_card_qs = hospital_qs
    elif card_type == 'call_not_done':
        if user.role == User.Role.LEAD_ATTENDENT:
            c_base = hospital_qs.filter(
                Q(assigned_to=user) | Q(assigned_to__isnull=True) | Q(custom_data__lead_attendant__in=['Unassigned', '', None, 'nan'])
            )
        else:
            c_base = hospital_qs
        cnd_matched_ids = filter_uncontacted_leads_ids(c_base, today=today)
        base_card_qs = hospital_qs.filter(id__in=cnd_matched_ids)
    elif card_type == 'telecaller_opd_booked':
        status_q = Q(custom_data__appointment_status__iexact='OPD Booking') | \
                   Q(custom_data__appointment_status__icontains='OPD') | \
                   Q(custom_data__appointment_status__icontains='Book') | \
                   Q(custom_data__appointment_status__icontains='Confirm') | \
                   Q(custom_data__appointment_status__icontains='Complete') | \
                   Q(custom_data__appointment_status__icontains='Done') | \
                   Q(custom_data__appointment_status__icontains='Consult') | \
                   Q(custom_data__icontains='consult')
        appt_leads_ids = Appointment.objects.filter(
            lead__in=hospital_qs,
            status__in=[AppointmentStatus.APPROVED, AppointmentStatus.SCHEDULED, AppointmentStatus.COMPLETED, AppointmentStatus.PENDING_APPROVAL]
        ).values_list('lead_id', flat=True)
        base_card_qs = hospital_qs.filter(
            Q(id__in=appt_leads_ids) | status_q
        )
    elif card_type == 'opd_booked':
        base_card_qs = hospital_qs.filter(
            Q(admission_status="ADMISSION_DONE") |
            Q(deal_status=DealStatus.WON) |
            Q(admission__isnull=False) |
            Q(stage__name__icontains='admission') |
            Q(custom_data__appointment_status__icontains='Book') |
            Q(custom_data__appointment_status__icontains='Confirm') |
            Q(custom_data__appointment_status__icontains='Approv') |
            Q(custom_data__appointment_status__icontains='Complete') |
            Q(custom_data__appointment_status__iexact='YES') |
            (Q(custom_data__total__isnull=False) & ~Q(custom_data__total__in=["0", "0.00", "", "0.0", 0, 0.0]))
        )
    elif card_type == 'followups':
        booked_appointment_lead_ids = list(
            hospital_qs.filter(
                Q(custom_data__appointment_status__icontains='Book') |
                Q(custom_data__appointment_status__icontains='Confirm') |
                Q(admission_status='ADMISSION_DONE') |
                (Q(custom_data__total__isnull=False) & ~Q(custom_data__total__in=['0', '0.00', '', '0.0', 0, 0.0]))
            ).values_list('id', flat=True)
        )
        base_card_qs = hospital_qs.exclude(
            deal_status__in=[DealStatus.WON, DealStatus.LOST]
        ).exclude(id__in=booked_appointment_lead_ids).filter(
            Q(next_followup_date__isnull=False) |
            Q(followups__next_followup_date__isnull=False) |
            Q(custom_data__appointment_status__icontains='follow')
        )
        # --- In-memory categorization (mirrors superadmin_home Card 4 logic exactly) ---
        # Build active follow-up lead lists so that card total, campaign pills, and subtabs always match
        _fu_all_qs = base_card_qs.distinct().select_related('assigned_to', 'stage', 'campaign', 'lead_source')
        _fu_today_list = []
        _fu_overdue_list = []
        _fu_upcoming_list = []
        _fu_month_list = []
        _fu_range_list = []

        _this_month_start = today.replace(day=1)
        import calendar as _cal
        _, _last_day = _cal.monthrange(today.year, today.month)
        _this_month_end = today.replace(day=_last_day)

        _range_s_d = range_start_date  # may be None
        _range_e_d = range_end_date    # may be None

        for _fl in _fu_all_qs:
            _cd = _fl.custom_data or {}
            _sched = _fl.next_followup_date
            if not _sched:
                _lfu = _fl.followups.filter(next_followup_date__isnull=False).order_by('-id').first()
                if _lfu:
                    _sched = _lfu.next_followup_date
            if not _sched:
                continue

            # Categorise based purely on date vs today (no exclusion for updated leads in today bucket)
            if _sched == today:
                _fu_today_list.append(_fl)
            elif _sched < today:
                _fu_overdue_list.append(_fl)
            elif _sched > today:
                _fu_upcoming_list.append(_fl)

            # Month list
            if _this_month_start <= _sched <= _this_month_end:
                _fu_month_list.append(_fl)
            elif _sched < _this_month_start:  # overdue from before this month — include in month view
                _fu_month_list.append(_fl)

            # Date-range list
            if _range_s_d and _range_e_d:
                if _range_s_d <= _sched <= _range_e_d:
                    _fu_range_list.append(_fl)
                elif _sched < _range_s_d:  # overdue before range
                    _fu_range_list.append(_fl)
            elif _range_s_d and not _range_e_d:
                if _sched >= _range_s_d:
                    _fu_range_list.append(_fl)
            elif _range_e_d and not _range_s_d:
                if _sched <= _range_e_d:
                    _fu_range_list.append(_fl)

        _fu_all_active_list = _fu_overdue_list + _fu_today_list + _fu_upcoming_list
    elif card_type == 'walkin':
        base_card_qs = hospital_qs.filter(
            Q(lead_source__name__icontains='walk') |
            Q(lead_source__name__icontains='hospital') |
            Q(custom_data__lead_source__icontains='walk') |
            Q(custom_data__lead_source__icontains='hospital')
        )
    else:
        base_card_qs = hospital_qs

    # Filter queryset based on card_type and selected time/mode
    if mode == 'date_range' and range_start_date and range_end_date:
        if card_type in ('new_leads', 'call_not_done', 'walkin'):
            leads_qs = base_card_qs.filter(
                Q(created_at__range=(r_start_dt, r_end_dt)) |
                Q(inquiry_date__range=(range_start_date, range_end_date))
            )
        elif card_type == 'opd_booked':
            leads_qs = base_card_qs.filter(
                Q(created_at__range=(r_start_dt, r_end_dt)) |
                Q(inquiry_date__range=(range_start_date, range_end_date)) |
                Q(admission__admission_date__range=(range_start_date, range_end_date)) |
                Q(admission__created_at__range=(r_start_dt, r_end_dt))
            )
        elif card_type == 'followups':
            leads_qs = base_card_qs.filter(
                Q(next_followup_date__range=(range_start_date, range_end_date)) |
                Q(followups__followup_date__range=(range_start_date, range_end_date)) |
                Q(followups__next_followup_date__range=(range_start_date, range_end_date))
            )
        else:
            leads_qs = base_card_qs.filter(
                Q(created_at__range=(r_start_dt, r_end_dt)) |
                Q(inquiry_date__range=(range_start_date, range_end_date))
            )
    elif card_type in ('new_leads', 'call_not_done', 'walkin'):
        if selected_date:
            leads_qs = base_card_qs.filter(
                Q(created_at__range=(start_dt, end_dt)) | Q(inquiry_date=selected_date)
            )
        elif month_range_start and month_range_end:
            leads_qs = base_card_qs.filter(
                Q(created_at__range=(month_range_start, month_range_end)) |
                Q(inquiry_date__range=(m_start_date, m_end_date))
            )
        else:
            leads_qs = base_card_qs

    elif card_type == 'telecaller_opd_booked':
        target_date = selected_date if selected_date else today
        target_str = target_date.strftime("%Y-%m-%d")
        target_alt_str = target_date.strftime("%d-%m-%Y")
        start_dt_t = timezone.make_aware(datetime.combine(target_date, datetime.min.time()))
        end_dt_t = timezone.make_aware(datetime.combine(target_date, datetime.max.time()))
        leads_date_q = Q(created_at__range=(start_dt_t, end_dt_t)) | \
                       Q(inquiry_date=target_date) | \
                       Q(custom_data__appo_booked_date=target_str) | \
                       Q(custom_data__appo_booked_date=target_alt_str) | \
                       Q(custom_data__appointment_date=target_str) | \
                       Q(custom_data__appointment_date=target_alt_str) | \
                       Q(custom_data__appointment_confirmed_at__startswith=target_str)
        appt_date_q = Q(appointments__appointment_date=target_date) | \
                      Q(appointments__created_at__date=target_date)
        leads_qs = base_card_qs.filter(leads_date_q | appt_date_q).distinct()

    elif card_type == 'opd_booked':
        if selected_date:
            sel_alt_str = selected_date.strftime("%d-%m-%Y")
            leads_qs = base_card_qs.filter(
                Q(created_at__range=(start_dt, end_dt)) |
                Q(inquiry_date=selected_date) |
                Q(admission__admission_date=selected_date) |
                Q(admission__created_at__range=(start_dt, end_dt)) |
                Q(custom_data__admission_date=sel_date_str) |
                Q(custom_data__admission_date=sel_alt_str) |
                Q(custom_data__appo_booked_date=sel_date_str) |
                Q(custom_data__appo_booked_date=sel_alt_str) |
                Q(custom_data__appointment_date=sel_date_str) |
                Q(custom_data__appointment_date=sel_alt_str) |
                Q(custom_data__appointment_confirmed_at__startswith=sel_date_str)
            )
        elif month_range_start and month_range_end:
            leads_qs = base_card_qs.filter(
                Q(created_at__range=(month_range_start, month_range_end)) |
                Q(inquiry_date__range=(m_start_date, m_end_date)) |
                Q(custom_data__appo_booked_date__startswith=today.strftime('%Y-%m'))
            )
        else:
            leads_qs = base_card_qs

    elif card_type == 'followups':
        # Use the in-memory categorised lists built in the base_card_qs section above
        if mode == 'all':
            _target_fu_leads = _fu_all_active_list
        elif mode == 'date_range' and (range_start_date or range_end_date):
            _target_fu_leads = _fu_range_list
        elif mode == 'this_month':
            _target_fu_leads = _fu_month_list
        elif selected_date == today:
            _target_fu_leads = _fu_today_list
        elif selected_date and selected_date < today:
            # Specific past date: leads whose sched == that date (overdue if still active) or leads in overdue whose sched == that date
            _target_fu_leads = [l for l in _fu_all_active_list if l.next_followup_date == selected_date
                                  or (not l.next_followup_date and l.followups.filter(next_followup_date=selected_date).exists())]
        elif selected_date and selected_date > today:
            _target_fu_leads = [l for l in _fu_upcoming_list if l.next_followup_date == selected_date
                                  or (not l.next_followup_date and l.followups.filter(next_followup_date=selected_date).exists())]
        else:
            _target_fu_leads = _fu_all_active_list

        # De-duplicate in case a lead appeared in multiple lists
        seen_fu_ids = set()
        _deduped_fu_leads = []
        for _fl in _target_fu_leads:
            if _fl.id not in seen_fu_ids:
                seen_fu_ids.add(_fl.id)
                _deduped_fu_leads.append(_fl)
        _target_fu_leads = _deduped_fu_leads

        _fu_lead_id_set = {l.id for l in _target_fu_leads}
        leads_qs = base_card_qs.filter(id__in=_fu_lead_id_set).distinct().select_related(
            'assigned_to', 'campaign', 'lead_source', 'course', 'stage', 'hospital'
        )
    else:
        leads_qs = base_card_qs

    # Determine business mode: 'hospital', 'academy', or 'all'
    if user.hospital:
        h_type = (user.hospital.settings or {}).get("business_type", "")
        h_name_lower = (user.hospital.name or "").lower()
        business_mode = "hospital" if ("hospital" in str(h_type).lower() or any(k in h_name_lower for k in ["hospital", "clinic", "medical", "nelson", "health"])) else ("academy" if "academy" in str(h_type).lower() or "academy" in h_name_lower or "zappcode" in h_name_lower else "other")
    elif selected_hospital_id and selected_hospital_id.isdigit():
        h_obj = Hospital.objects.filter(id=int(selected_hospital_id)).first()
        if h_obj:
            h_type = (h_obj.settings or {}).get("business_type", "")
            h_name_lower = (h_obj.name or "").lower()
            business_mode = "hospital" if ("hospital" in str(h_type).lower() or any(k in h_name_lower for k in ["hospital", "clinic", "medical", "nelson", "health"])) else ("academy" if "academy" in str(h_type).lower() or "academy" in h_name_lower or "zappcode" in h_name_lower else "other")
        else:
            business_mode = "all"
    elif selected_hospital_id in ("zappcode", "none"):
        business_mode = "academy"
    else:
        # All businesses or superadmin global view
        business_mode = "all"

    # Only Hospital/Clinic businesses use 'Direct Hospital Visit'; all other businesses (Academy, Agency, Real Estate, etc.) use 'Direct Walk-in'
    default_direct_campaign = "Direct Hospital Visit" if business_mode == "hospital" else "Direct Walk-in"

    if card_type == 'followups':
        # Use in-memory count and campaign breakdown (avoids JOIN row multiplication)
        total_count = len(_target_fu_leads)

        # Campaign counts: computed in-memory from the exact set of target leads
        campaign_counts = defaultdict(int)
        for _fl in _target_fu_leads:
            _cd_tmp = _fl.custom_data or {}
            _c = _fl.campaign.name if _fl.campaign else (_cd_tmp.get('campaign') or '')
            _c = str(_c).strip()
            if not _c or _c.lower() in ['nan', 'none', 'null', '—', '-', '', 'general', 'general / direct', 'direct', 'general/direct']:
                _c = default_direct_campaign
            campaign_counts[_c] += 1

        campaign_breakdown = [
            {"campaign_name": camp, "count": cnt}
            for camp, cnt in sorted(campaign_counts.items(), key=lambda x: x[1], reverse=True)
        ]
    else:
        leads_qs = leads_qs.distinct().select_related('assigned_to', 'campaign', 'lead_source', 'course', 'stage', 'hospital')
        total_count = leads_qs.count()

        # Calculate Campaign-wise breakdown using DB leads to properly inspect campaign field and custom_data
        campaign_counts = defaultdict(int)
        for l in leads_qs:
            cd_tmp = l.custom_data or {}
            c = l.campaign.name if l.campaign else (cd_tmp.get('campaign') or '')
            c = str(c).strip()
            if not c or c.lower() in ['nan', 'none', 'null', '—', '-', '', 'general', 'general / direct', 'direct', 'general/direct']:
                c = default_direct_campaign
            campaign_counts[c] += 1

        campaign_breakdown = [
            {"campaign_name": camp, "count": cnt}
            for camp, cnt in sorted(campaign_counts.items(), key=lambda x: x[1], reverse=True)
        ]
    
    # Pre-fetch all followups and notes for the paginated leads
    if card_type == 'followups':
        # Build leads_page from the already-resolved in-memory list (already select_related)
        leads_page = list(leads_qs.order_by('-created_at', '-id')[:250])
        # Also build a quick id->sched_date map from the in-memory categorized lists for fu_cat assignment
        _fu_id_to_cat = {}
        for _fl in _fu_today_list:
            _fu_id_to_cat[_fl.id] = 'today'
        for _fl in _fu_overdue_list:
            if _fl.id not in _fu_id_to_cat:
                _fu_id_to_cat[_fl.id] = 'overdue'
        for _fl in _fu_upcoming_list:
            if _fl.id not in _fu_id_to_cat:
                _fu_id_to_cat[_fl.id] = 'upcoming'
    else:
        leads_page = list(leads_qs.order_by('-created_at', '-id')[:250])
    lead_ids = [l.id for l in leads_page]
    from followups.models import FollowUp, Note
    followups = FollowUp.objects.filter(lead_id__in=lead_ids).order_by('-created_at')
    notes = Note.objects.filter(lead_id__in=lead_ids).order_by('-created_at')
    
    lead_comments_map = defaultdict(list)
    for f in followups:
        if f.comment and str(f.comment).strip() not in ('', 'None', 'nan', '-'):
            lead_comments_map[f.lead_id].append(str(f.comment).strip())
    for n in notes:
        if n.note and str(n.note).strip() not in ('', 'None', 'nan', '-'):
            lead_comments_map[n.lead_id].append(str(n.note).strip())

    # Build Lead Items (limit to top 250 for ultra fast responsive modal)
    # Avoid calling l.display_status or l.is_booked properties which execute un-cached SQL queries per lead
    lead_items = []
    for l in leads_page:
        cd = l.custom_data or {}
        # Business Type Awareness (Hospital vs Academy)
        is_hosp_lead = False
        if l.hospital:
            btype = (l.hospital.settings or {}).get("business_type")
            if btype:
                is_hosp_lead = (str(btype).strip().lower() == "hospital")
            else:
                n_lower = (l.hospital.name or "").lower()
                is_hosp_lead = any(k in n_lower for k in ["hospital", "clinic", "medical", "nelson", "health"])
        elif business_mode == "hospital":
            is_hosp_lead = True
        
        doc = cd.get('doctor') or ('Not Assigned' if is_hosp_lead else '')
        dept = cd.get('department') or ('General OPD' if is_hosp_lead else '')
        lead_default_camp = 'Direct Hospital Visit' if is_hosp_lead else 'Direct Walk-in'
        c_name = l.campaign.name if l.campaign else (cd.get('campaign') or lead_default_camp)
        if not c_name or str(c_name).strip() in ['nan', 'None', '', '—', '-', 'general', 'general / direct', 'direct', 'general/direct']:
            c_name = lead_default_camp

        mob_digits = Lead.clean_mobile(l.mobile)
          # In-memory status & booked computation without N+1 queries
        raw_apt = str(cd.get("appointment_status") or "").strip()
        raw_ds = str(cd.get("deal_status") or (l.stage.name if l.stage else "")).strip()
        tot_billed = getattr(l, 'total_billed_amount', 0) or 0
        if tot_billed > 0 or l.deal_status == 'WON' or 'won' in raw_ds.lower():
            status_str = "Payment Done"
            is_booked = True
        elif raw_apt:
            status_str = raw_apt
            is_booked = bool("book" in raw_apt.lower() or "confirm" in raw_apt.lower() or "won" in raw_apt.lower() or "approv" in raw_apt.lower() or "complete" in raw_apt.lower())
        elif l.stage:
            status_str = l.stage.name
            is_booked = bool("admission" in status_str.lower() or "won" in status_str.lower())
        elif l.deal_status:
            status_str = l.deal_status
            is_booked = (l.deal_status == 'WON')
        else:
            status_str = "Open"
            is_booked = False

        appt_date = str(cd.get("appo_booked_date") or cd.get("appointment_date") or "").strip()
        appt_time = str(cd.get("appointment_time") or "").strip()

        # In-memory temperature computation
        temp_str = str(l.temperature or cd.get("temperature") or "").strip()
        if not temp_str:
            # Check remarks
            r_all = " ".join([str(cd.get(f'remark_{idx}') or '') for idx in range(1, 4)]).upper()
            if "HOT" in r_all:
                temp_str = "HOT"
            elif "WARM" in r_all:
                temp_str = "WARM"
            elif "COLD" in r_all:
                temp_str = "COLD"
            else:
                temp_str = "WARM"
        
        all_comments = []
        for i in range(1, 6):
            r = cd.get(f'remark_{i}')
            if r and str(r).strip() not in ('', 'None', 'nan', '-'):
                all_comments.append(str(r).strip())
        
        # Include internal notes and direct comments
        for extra_note in [l.notes, getattr(l, 'referral_notes', None), cd.get('comments')]:
            if extra_note and str(extra_note).strip() not in ('', 'None', 'nan', '-'):
                all_comments.append(str(extra_note).strip())
        all_comments.extend(lead_comments_map.get(l.id, []))

        course_name = l.course.name if l.course else (cd.get('course') or '')
        stage_name = l.stage.name if l.stage else (cd.get('stage') or '')
        admission_status_str = l.get_admission_status_display() if hasattr(l, 'get_admission_status_display') else str(l.admission_status or '')

        import urllib.parse
        entity_name = (l.hospital.name if l.hospital else "Zappcode Academy").strip()
        client_name = (l.name or "Student").strip()
        doc_or_course = (cd.get("doctor") or (l.course.name if l.course else "") or "our course / program").strip()
        
        if is_booked:
            date_part = f" for {appt_date}" if appt_date else ""
            time_part = f" at {appt_time}" if appt_time else ""
            wa_text = (
                f"Hello {client_name}, Greetings from {entity_name}!\n\n"
                f"Your registration / appointment for {doc_or_course} at {entity_name} is confirmed{date_part}{time_part}.\n\n"
                f"We look forward to connecting with you.\n\n"
                f"For any queries, feel free to reply here.\n\n"
                f"Warm Regards,\n{entity_name}"
            )
        else:
            wa_text = (
                f"Hello {client_name}, Greetings from {entity_name}!\n\n"
                f"Thank you for connecting with us. We are pleased to assist you with your inquiry.\n\n"
                f"Please let us know your preferred timing so we can assist you.\n\n"
                f"Warm Regards,\nCounseling Team - {entity_name}"
            )
        wa_msg_encoded = urllib.parse.quote(wa_text)

        source_fallback = 'Hospital Form' if is_hosp_lead else ('Meta Ads' if ('[Lead ID]' in (l.notes or '')) else 'Website / Inquiry')
        lead_source_name = l.lead_source.name if l.lead_source else (cd.get('lead_source') or source_fallback)

        # Categorize appointment/booking
        booking_cat = "none"
        if is_booked or "opd" in status_str.lower() or "consult" in status_str.lower() or card_type == "opd_booked":
            if "consult" in status_str.lower() or any("consult" in str(c).lower() for c in all_comments) or "consult" in str(cd.get("department") or "").lower():
                booking_cat = "consultation"
            else:
                booking_cat = "opd"

        # Determine Follow-up category ('today', 'overdue', 'upcoming', 'none')
        # Use the pre-built categorisation map when processing a followups card (avoids N+1 queries and mismatches)
        if card_type == 'followups':
            fu_cat = _fu_id_to_cat.get(l.id, 'none')
            sched_fu_date = l.next_followup_date
            if not sched_fu_date:
                _lfu2 = l.followups.filter(next_followup_date__isnull=False).order_by('-id').first()
                if _lfu2:
                    sched_fu_date = _lfu2.next_followup_date
            is_fu_updated = False  # Not used when fu_cat comes from map
        else:
            fu_cat = "none"
            sched_fu_date = l.next_followup_date
            if not sched_fu_date:
                latest_fu = l.followups.filter(next_followup_date__isnull=False).order_by('-id').first()
                if latest_fu:
                    sched_fu_date = latest_fu.next_followup_date

            has_fu_remark = any(bool(cd.get(k) and str(cd.get(k)).strip().lower() not in ('nan', 'none', '', '-')) for k in ['remark_1', 'remark_2', 'remark_3', 'followup_remark'])
            latest_fu_obj = l.followups.order_by('-id').first()
            has_fu_status_update = False
            if latest_fu_obj and sched_fu_date and latest_fu_obj.followup_status not in ('PENDING', 'CALL_BACK', 'RESCHEDULED') and latest_fu_obj.followup_date >= sched_fu_date:
                has_fu_status_update = True
            is_fu_updated = (has_fu_remark or has_fu_status_update)

            if sched_fu_date:
                if sched_fu_date == today:
                    fu_cat = "today"
                elif sched_fu_date < today:
                    fu_cat = "overdue" if not is_fu_updated else "none"
                elif sched_fu_date > today:
                    fu_cat = "upcoming" if not is_fu_updated else "none"

        lead_items.append({
            "id": l.id,
            "name": l.name or ("Anonymous Patient" if is_hosp_lead else "Anonymous Student"),
            "mobile": l.mobile or "-",
            "clean_mobile": mob_digits or "",
            "email": l.email or "-",
            "created_date": l.created_at.strftime('%d-%m-%Y') if l.created_at else str(l.inquiry_date or '-'),
            "inquiry_date": str(l.inquiry_date or '-'),
            "campaign": c_name.strip(),
            "lead_source": lead_source_name,
            "status": status_str,
            "is_booked": is_booked,
            "booking_category": booking_cat,
            "followup_category": fu_cat,
            "is_followup_updated": is_fu_updated,
            "temperature": temp_str,
            "appointment_status": status_str,
            "doctor": doc,
            "appointment_date": appt_date,
            "appointment_time": appt_time,
            "whatsapp_message": wa_msg_encoded,
            "department": dept,
            "course": course_name,
            "stage": stage_name,
            "admission_status": admission_status_str,
            "is_hospital": is_hosp_lead,
            "business_name": l.hospital.name if l.hospital else "Zappcode Academy",
            "assigned_to": l.assigned_to.get_full_name() if l.assigned_to else "Unassigned",
            "assigned_to_id": l.assigned_to_id,
            "next_followup": str(sched_fu_date or l.next_followup_date or '-'),
            "all_comments": all_comments,
            "detail_url": f"/leads/{l.id}/",
            "edit_url": f"/leads/{l.id}/edit/",
        })

    # Pre-calculate calendar heatmap matrix for the month based on base_card_qs
    cal_year = int(year_param) if (year_param and str(year_param).isdigit()) else (range_start_date.year if range_start_date else (selected_date.year if selected_date else today.year))
    cal_month = int(month_param) if (month_param and str(month_param).isdigit()) else (range_start_date.month if range_start_date else (selected_date.month if selected_date else today.month))
    _, days_in_month = calendar.monthrange(cal_year, cal_month)

    calendar_counts = {}
    cal_m_start = timezone.make_aware(datetime(cal_year, cal_month, 1, 0, 0, 0))
    _, last_d = calendar.monthrange(cal_year, cal_month)
    cal_m_end = timezone.make_aware(datetime(cal_year, cal_month, last_d, 23, 59, 59))

    from django.db.models.functions import TruncDate
    date_counts = (
        base_card_qs.filter(created_at__range=(cal_m_start, cal_m_end))
        .annotate(c_date=TruncDate('created_at'))
        .values('c_date')
        .annotate(cnt=Count('id'))
    )
    date_map = {row['c_date'].strftime('%Y-%m-%d'): row['cnt'] for row in date_counts if row.get('c_date')}

    # Also check inquiry_date if created_at didn't catch imported inquiry dates
    inq_counts = (
        base_card_qs.filter(inquiry_date__range=(date(cal_year, cal_month, 1), date(cal_year, cal_month, last_d)))
        .values('inquiry_date')
        .annotate(cnt=Count('id'))
    )
    for row in inq_counts:
        inq_d = row.get('inquiry_date')
        if inq_d:
            inq_str = inq_d.strftime('%Y-%m-%d')
            if inq_str not in date_map or date_map[inq_str] == 0:
                date_map[inq_str] = row['cnt']

    for d in range(1, days_in_month + 1):
        day_str = f"{cal_year:04d}-{cal_month:02d}-{d:02d}"
        calendar_counts[day_str] = date_map.get(day_str, 0)

    hosp_name = user.hospital.name if (hasattr(user, 'hospital') and user.hospital) else "Zappcode Academy"
    agent_name = user.get_full_name() or user.username or "Counseling & Admissions Team"

    if mode == 'date_range' and range_start_date and range_end_date:
        disp_title = f"{range_start_date.strftime('%d %b %Y')} to {range_end_date.strftime('%d %b %Y')}"
    elif mode == 'this_month':
        disp_title = today.strftime('%B %Y')
    elif selected_date:
        disp_title = selected_date.strftime('%d %B %Y')
    else:
        disp_title = "All Time Records"

    # Business mode was already determined above for campaign breakdown and lead attributes

    # Eligible assignable users for bulk assignment inside modal
    # Strictly for Admin / Superadmin. Format: Admins see clean Name only, Superadmin sees Name - Business
    is_admin_or_superadmin = bool(user.role in [User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER])
    users_list = []

    if is_admin_or_superadmin:
        if user.role == User.Role.SUPER_ADMIN:
            # Superadmin: Counsellors, HR, and Lead Attendants across businesses
            qs = User.objects.filter(
                role__in=[User.Role.COUNSELLOR, User.Role.HR, User.Role.LEAD_ATTENDENT],
                is_active=True,
                is_approved=True
            ).select_related('hospital').order_by('hospital__name', 'first_name', 'last_name', 'username')

            if selected_hospital_id and selected_hospital_id.isdigit():
                qs = qs.filter(hospital_id=int(selected_hospital_id))
            elif selected_hospital_id in ("zappcode", "none"):
                qs = qs.filter(hospital__isnull=True)

            for u in qs:
                u_name = u.get_full_name().strip() or u.username
                b_name = u.hospital.name if u.hospital else "Zappcode Academy"
                users_list.append({
                    "id": u.id,
                    "name": f"{u_name} - {b_name}",
                })
        else:
            if user.hospital:
                # Nelson Admin: ONLY Lead Attendants of Nelson Hospital
                qs = User.objects.filter(
                    hospital=user.hospital,
                    role=User.Role.LEAD_ATTENDENT,
                    is_active=True,
                    is_approved=True
                ).order_by('first_name', 'last_name', 'username')
            else:
                # Zappcode Admin: ONLY Counsellors (and HR) of Zappcode Academy
                qs = User.objects.filter(
                    hospital__isnull=True,
                    role__in=[User.Role.COUNSELLOR, User.Role.HR],
                    is_active=True,
                    is_approved=True
                ).order_by('first_name', 'last_name', 'username')

            for u in qs:
                u_name = u.get_full_name().strip() or u.username
                users_list.append({
                    "id": u.id,
                    "name": u_name,
                })

    return JsonResponse({
        "status": "success",
        "card_type": card_type,
        "mode": mode,
        "business_mode": business_mode,
        "selected_date": selected_date.strftime('%Y-%m-%d') if selected_date else ("range" if mode == 'date_range' else "all"),
        "selected_date_display": disp_title,
        "start_date": range_start_date.strftime('%Y-%m-%d') if range_start_date else "",
        "end_date": range_end_date.strftime('%Y-%m-%d') if range_end_date else "",
        "total_count": total_count,
        "hospital_name": hosp_name,
        "agent_name": agent_name,
        "campaign_breakdown": campaign_breakdown,
        "lead_items": lead_items,
        "calendar_counts": calendar_counts,
        "cal_year": cal_year,
        "cal_month": cal_month,
        "cal_month_name": date(cal_year, cal_month, 1).strftime('%B %Y'),
        "users": users_list,
        "user_role": user.role,
        "can_assign": is_admin_or_superadmin,
        "can_self_assign": bool(getattr(user, "can_self_assign", True)),
        "self_assign_limit": getattr(user, "bulk_self_assign_limit", 25),
    })


@login_required
def live_metrics_api(request):
    """
    Lightweight background polling endpoint for real-time CRM updates.
    Returns live counts for KPI cards, appointments, follow-ups, and latest lead ID/hash.
    Designed with fail-safe error handling so network blips never crash the UI.
    """
    from django.http import JsonResponse
    try:
        user = request.user
        hospital = user.hospital if hasattr(user, 'hospital') else None
        today = timezone.localdate()
        
        start_today = timezone.make_aware(datetime.combine(today, datetime.min.time()))
        end_today = timezone.make_aware(datetime.combine(today, datetime.max.time()))

        time_filter = request.GET.get('time_filter', 'today')
        start_of_month = timezone.make_aware(datetime(today.year, today.month, 1, 0, 0, 0))
        _, last_day = calendar.monthrange(today.year, today.month)
        end_of_month = timezone.make_aware(datetime(today.year, today.month, last_day, 23, 59, 59))
        start_date_month = date(today.year, today.month, 1)
        end_date_month = date(today.year, today.month, last_day)

        # Base Leads QuerySet
        leads_qs = Lead.objects.filter(is_archived=False)
        if hospital:
            leads_qs = leads_qs.filter(hospital=hospital)

        # Role-based restriction if telecaller
        if user.role == User.Role.LEAD_ATTENDENT and not getattr(user, 'can_view_all_leads', False):
            my_leads_qs = leads_qs.filter(assigned_to=user)
        else:
            my_leads_qs = leads_qs

        # Counts
        total_leads = my_leads_qs.count()
        today_leads = my_leads_qs.filter(
            Q(created_at__range=(start_today, end_today)) | Q(inquiry_date=today)
        ).count()

        # Follow-ups
        booked_exclude = (
            Q(custom_data__appointment_status__icontains='Book') |
            Q(custom_data__appointment_status__icontains='Confirm') |
            Q(deal_status__in=['WON', 'LOST'])
        )
        today_followups = my_leads_qs.filter(next_followup_date=today).exclude(booked_exclude).count()

        # Appointments
        apt_qs = Appointment.objects.all()
        if hospital:
            apt_qs = apt_qs.filter(hospital=hospital)
        if user.role == User.Role.DOCTOR:
            apt_qs = apt_qs.filter(doctor_user=user)

        today_apts = apt_qs.filter(appointment_date=today).count()
        awaiting_approval_count = apt_qs.filter(status=AppointmentStatus.SCHEDULED).count()
        confirmed_count = apt_qs.filter(status=AppointmentStatus.APPROVED).count()

        # Call Not Done (Exact same logic as dashboard calculate_insights)
        call_not_done_base = my_leads_qs
        if time_filter == 'today':
            cnd_raw_qs = call_not_done_base.filter(
                Q(created_at__range=(start_today, end_today)) | Q(inquiry_date=today)
            ).distinct()
        elif time_filter == 'this_month':
            cnd_raw_qs = call_not_done_base.filter(
                Q(created_at__range=(start_of_month, end_of_month)) |
                Q(inquiry_date__range=(start_date_month, end_date_month))
            ).distinct()
        else:
            cnd_raw_qs = call_not_done_base.distinct()

        cnd_matched_ids = filter_uncontacted_leads_ids(cnd_raw_qs, today=today)
        call_not_done_count = len(cnd_matched_ids)

        # Latest lead tracking to detect new entries
        latest_lead = my_leads_qs.order_by('-id').values('id', 'name', 'mobile', 'created_at').first()
        latest_lead_id = latest_lead['id'] if latest_lead else 0
        latest_lead_name = latest_lead['name'] if latest_lead else ''

        return JsonResponse({
            "status": "success",
            "timestamp": timezone.now().isoformat(),
            "latest_lead_id": latest_lead_id,
            "latest_lead_name": latest_lead_name,
            "metrics": {
                "total_leads": total_leads,
                "today_leads": today_leads,
                "today_followups": today_followups,
                "call_not_done": call_not_done_count,
                "today_appointments": today_apts,
                "awaiting_approval": awaiting_approval_count,
                "confirmed_appointments": confirmed_count,
            }
        })
    except Exception as e:
        return JsonResponse({
            "status": "retry",
            "error": str(e),
            "metrics": {}
        })


@login_required
def nelson_module_view(request, module_name):
    from django.core.exceptions import PermissionDenied
    from accounts.models import User, Hospital
    from django.contrib import messages
    from django.shortcuts import redirect
    from django.db.models import Q, Sum
    import json
    
    # Allow Super Admin, Admin, and Manager
    if request.user.role not in (User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER):
        raise PermissionDenied("Restricted to Admin/Manager.")

    # Determine effective hospital
    is_superadmin = (request.user.is_superuser or request.user.role == User.Role.SUPER_ADMIN) and not bool(request.user.hospital)
    if request.user.hospital:
        hospital = request.user.hospital
    else:
        # Superadmin: check session or query param
        selected_biz_id = (
            request.GET.get("business", "").strip()
            or request.GET.get("hospital", "").strip()
            or str(request.session.get("active_business_id", "")).strip()
        )
        if selected_biz_id and selected_biz_id.isdigit():
            hospital = Hospital.objects.filter(id=int(selected_biz_id)).first()
        else:
            hospital = None

    if module_name == 'hospital-profile':
        if not request.user.can_manage_hospital_profile:
            raise PermissionDenied('Permission denied for hospital profile.')
        if not hospital:
            messages.error(request, "No hospital associated with your account or selected.")
            return redirect('dashboard:home')
            
        if request.method == 'POST':
            hospital.name = request.POST.get('name', hospital.name)
            hospital.contact_email = request.POST.get('contact_email', hospital.contact_email)
            hospital.phone = request.POST.get('phone', hospital.phone)
            hospital.address = request.POST.get('address', hospital.address)
            hospital.registration_no = request.POST.get('registration_no', hospital.registration_no)
            
            if 'logo' in request.FILES:
                hospital.logo = request.FILES['logo']
                
            settings_data = {
                'facebook_url': request.POST.get('facebook_url', ''),
                'instagram_url': request.POST.get('instagram_url', ''),
                'whatsapp_number': request.POST.get('whatsapp_number', ''),
                'gst_number': request.POST.get('gst_number', ''),
                'bank_name': request.POST.get('bank_name', ''),
                'account_no': request.POST.get('account_no', ''),
                'ifsc_code': request.POST.get('ifsc_code', ''),
                'welcome_message': request.POST.get('welcome_message', ''),
            }
            hospital.settings = settings_data
            hospital.save()
            messages.success(request, "Hospital Profile updated successfully.")
            return redirect('dashboard:nelson_module', module_name='hospital-profile')
            
        return render(request, "dashboard/hospital_profile.html", {
            "title": "Hospital Profile", 
            "hospital": hospital, 
            "active": module_name
        })

    if module_name == 'campaign-management':
        if not request.user.can_manage_campaigns:
            raise PermissionDenied('Permission denied for campaign management.')
        from leads.models import Campaign, Lead, Appointment
        from imports.models import ImportJob
        
        if request.method == 'POST':
            action = request.POST.get('action')
            if action == 'create':
                name = request.POST.get('name', '').strip()
                platform = request.POST.get('platform', '').strip()
                campaign_id_code = request.POST.get('campaign_id_code', '').strip()
                ad_set = request.POST.get('ad_set', '').strip()
                ad_name = request.POST.get('ad_name', '').strip()
                raw_cost = request.POST.get('cost', '0').strip()
                landing_page = request.POST.get('landing_page', '').strip()
                start_date = request.POST.get('start_date') or None
                end_date = request.POST.get('end_date') or None
                
                # Validation: Cost must be non-negative integer
                try:
                    cost_val = int(float(raw_cost)) if raw_cost else 0
                    if cost_val < 0:
                        messages.error(request, "Campaign cost cannot be negative.")
                        return redirect('dashboard:nelson_module', module_name='campaign-management')
                except (ValueError, TypeError):
                    messages.error(request, "Please enter a valid whole integer amount for cost.")
                    return redirect('dashboard:nelson_module', module_name='campaign-management')

                # Validation: End Date must be after/on Start Date
                if start_date and end_date and end_date < start_date:
                    messages.error(request, "End Date must be greater than or equal to Start Date.")
                    return redirect('dashboard:nelson_module', module_name='campaign-management')

                if name:
                    Campaign.objects.create(
                        hospital=hospital,
                        name=name,
                        platform=platform,
                        campaign_id=campaign_id_code,
                        ad_set=ad_set,
                        ad_name=ad_name,
                        cost=cost_val,
                        landing_page=landing_page,
                        start_date=start_date,
                        end_date=end_date,
                        is_active=True
                    )
                    messages.success(request, f"Campaign '{name}' created successfully!")
                return redirect('dashboard:nelson_module', module_name='campaign-management')
                
            elif action == 'edit':
                cid = request.POST.get('campaign_id')
                camp = get_object_or_404(Campaign, pk=cid)
                if not is_superadmin and camp.hospital and camp.hospital != hospital:
                    messages.error(request, "Permission denied.")
                    return redirect('dashboard:nelson_module', module_name='campaign-management')
                    
                raw_cost = request.POST.get('cost', '').strip()
                start_date = request.POST.get('start_date') or None
                end_date = request.POST.get('end_date') or None

                # Validation: Cost must be non-negative integer
                try:
                    cost_val = int(float(raw_cost)) if raw_cost else 0
                    if cost_val < 0:
                        messages.error(request, "Campaign cost cannot be negative.")
                        return redirect('dashboard:nelson_module', module_name='campaign-management')
                except (ValueError, TypeError):
                    messages.error(request, "Please enter a valid whole integer amount for cost.")
                    return redirect('dashboard:nelson_module', module_name='campaign-management')

                # Validation: End Date must be after/on Start Date
                if start_date and end_date and end_date < start_date:
                    messages.error(request, "End Date must be greater than or equal to Start Date.")
                    return redirect('dashboard:nelson_module', module_name='campaign-management')

                camp.name = request.POST.get('name', camp.name).strip()
                camp.platform = request.POST.get('platform', camp.platform).strip()
                camp.campaign_id = request.POST.get('campaign_id_code', camp.campaign_id).strip()
                camp.ad_set = request.POST.get('ad_set', camp.ad_set).strip()
                camp.ad_name = request.POST.get('ad_name', camp.ad_name).strip()
                camp.cost = cost_val
                camp.landing_page = request.POST.get('landing_page', camp.landing_page).strip()
                camp.start_date = start_date
                camp.end_date = end_date
                camp.is_active = (request.POST.get('is_active') == 'on')
                camp.save()
                messages.success(request, f"Campaign '{camp.name}' updated successfully!")
                return redirect('dashboard:nelson_module', module_name='campaign-management')
                
            elif action == 'toggle':
                cid = request.POST.get('campaign_id')
                camp = get_object_or_404(Campaign, pk=cid)
                if not is_superadmin and camp.hospital and camp.hospital != hospital:
                    messages.error(request, "Permission denied.")
                    return redirect('dashboard:nelson_module', module_name='campaign-management')
                camp.is_active = not camp.is_active
                camp.save(update_fields=['is_active'])
                messages.success(request, f"Campaign '{camp.name}' status toggled to {'Active' if camp.is_active else 'Inactive'}.")
                return redirect('dashboard:nelson_module', module_name='campaign-management')
                
            elif action == 'delete':
                cid = request.POST.get('campaign_id')
                camp = get_object_or_404(Campaign, pk=cid)
                if not is_superadmin and camp.hospital and camp.hospital != hospital:
                    messages.error(request, "Permission denied.")
                    return redirect('dashboard:nelson_module', module_name='campaign-management')
                name = camp.name
                camp.delete()
                messages.success(request, f"Campaign '{name}' deleted.")
                return redirect('dashboard:nelson_module', module_name='campaign-management')

        # Load campaigns for this hospital
        if hospital:
            campaigns_qs = Campaign.objects.filter(Q(hospital=hospital) | Q(hospital__isnull=True)).order_by('-is_active', '-id')
            base_leads_qs = Lead.objects.filter(hospital=hospital, is_archived=False)
            base_jobs_qs = ImportJob.objects.filter(created_by__hospital=hospital)
        else:
            campaigns_qs = Campaign.objects.all().order_by('-is_active', '-id')
            base_leads_qs = Lead.objects.filter(is_archived=False)
            base_jobs_qs = ImportJob.objects.all()

        # Date Filtering Logic
        from datetime import datetime, timedelta
        from django.utils import timezone
        
        date_preset = request.GET.get('date_preset', 'today')
        start_date_str = request.GET.get('start_date', '')
        end_date_str = request.GET.get('end_date', '')
        
        today = timezone.localdate()
        filter_start = today
        filter_end = today
        preset_label = "Today"

        if date_preset == 'today':
            filter_start = today
            filter_end = today
            preset_label = today.strftime('%d-%m-%Y')
        elif date_preset == 'yesterday':
            yesterday = today - timedelta(days=1)
            filter_start = yesterday
            filter_end = yesterday
            preset_label = yesterday.strftime('%d-%m-%Y')
        elif date_preset == 'last_7d':
            filter_start = today - timedelta(days=7)
            filter_end = today
            preset_label = f"{filter_start.strftime('%d-%m-%Y')} to {filter_end.strftime('%d-%m-%Y')}"
        elif date_preset == 'this_month':
            filter_start = today.replace(day=1)
            filter_end = today
            preset_label = f"{filter_start.strftime('%d-%m-%Y')} to {filter_end.strftime('%d-%m-%Y')}"
        elif date_preset == 'all_time':
            filter_start = None
            filter_end = None
            preset_label = "All Time"
        elif date_preset == 'custom' and start_date_str:
            try:
                filter_start = datetime.strptime(start_date_str, '%Y-%m-%d').date()
                filter_end = datetime.strptime(end_date_str, '%Y-%m-%d').date() if end_date_str else filter_start
                preset_label = f"{filter_start.strftime('%d-%m-%Y')} to {filter_end.strftime('%d-%m-%Y')}"
            except ValueError:
                filter_start = today
                filter_end = today
                preset_label = today.strftime('%d-%m-%Y')

        # Leads in selected period (by created_at or inquiry_date)
        import datetime as dt_module
        if filter_start and filter_end:
            start_dt = timezone.make_aware(dt_module.datetime.combine(filter_start, dt_module.time.min))
            end_dt = timezone.make_aware(dt_module.datetime.combine(filter_end, dt_module.time.max))
            period_leads_qs = base_leads_qs.filter(
                Q(created_at__gte=start_dt, created_at__lte=end_dt) |
                Q(inquiry_date__gte=filter_start, inquiry_date__lte=filter_end)
            )
            period_jobs_qs = base_jobs_qs.filter(
                created_at__gte=start_dt, created_at__lte=end_dt
            )
        else:
            period_leads_qs = base_leads_qs
            period_jobs_qs = base_jobs_qs

        campaigns_data = []
        total_leads_count = 0
        total_period_leads = 0
        # Count campaigns that received leads today and in the active period
        start_today = timezone.make_aware(dt_module.datetime.combine(today, dt_module.time.min))
        end_today = timezone.make_aware(dt_module.datetime.combine(today, dt_module.time.max))
        today_leads_qs = base_leads_qs.filter(
            Q(created_at__gte=start_today, created_at__lte=end_today) |
            Q(inquiry_date=today)
        )
        today_active_campaigns_count = 0
        period_active_campaigns_count = 0

        for c in campaigns_qs:
            # All time leads for this campaign
            leads_all_cnt = base_leads_qs.filter(Q(campaign=c) | Q(custom_data__campaign=c.name)).count()
            # Period leads for this campaign
            leads_period_cnt = period_leads_qs.filter(Q(campaign=c) | Q(custom_data__campaign=c.name)).count()
            # Today's leads for this campaign
            leads_today_cnt = today_leads_qs.filter(Q(campaign=c) | Q(custom_data__campaign=c.name)).count()

            if leads_today_cnt > 0:
                today_active_campaigns_count += 1
            if leads_period_cnt > 0:
                period_active_campaigns_count += 1
            
            total_leads_count += leads_all_cnt
            total_period_leads += leads_period_cnt
            
            campaigns_data.append({
                "obj": c,
                "leads_count": leads_all_cnt,
                "period_leads_count": leads_period_cnt,
                "today_leads_count": leads_today_cnt,
            })
            
        total_appts = Appointment.objects.filter(hospital=hospital).count() if hospital else (Appointment.objects.all().count() if is_superadmin else 0)
        
        # Recent Import Jobs in selected period for WhatsApp report
        recent_jobs = period_jobs_qs.filter(imported_count__gt=0).order_by('-created_at')[:15]
        hospital_name = hospital.name if hospital else ("All Businesses (Global)" if is_superadmin else "Zappcode CRM")

        return render(request, "dashboard/campaign_management.html", {
            "title": "Campaign Management",
            "active": "campaign-management",
            "campaigns_data": campaigns_data,
            "total_campaigns": campaigns_qs.count(),
            "active_campaigns_count": campaigns_qs.filter(is_active=True).count(),
            "today_active_campaigns_count": today_active_campaigns_count,
            "period_active_campaigns_count": period_active_campaigns_count,
            "total_leads_generated": total_leads_count,
            "total_period_leads": total_period_leads,
            "total_appts_generated": total_appts,
            "date_preset": date_preset,
            "start_date": start_date_str,
            "end_date": end_date_str,
            "preset_label": preset_label,
            "recent_jobs": recent_jobs,
            "hospital_name": hospital_name,
            "today_date_str": today.strftime('%d-%m-%Y'),
        })

    elif module_name == 'financial-overview':
        if not request.user.can_view_financials:
            raise PermissionDenied('Permission denied for financial overview.')
        from leads.models import Campaign, Lead, Appointment
        from admissions.models import Admission
        from payments.models import Payment, PaymentStatus
        from decimal import Decimal

        leads_qs = Lead.objects.filter(is_archived=False)
        campaigns_qs = Campaign.objects.all()
        if hospital:
            leads_qs = leads_qs.filter(hospital=hospital)
            campaigns_qs = campaigns_qs.filter(Q(hospital=hospital) | Q(hospital__isnull=True))

        # 1. Total Campaign Costs
        total_campaign_cost = sum([float(c.cost or 0) for c in campaigns_qs])

        # 2. Revenue Calculation (Patient OPD/Pharmacy/Total Billing + Admissions Payments)
        total_billing_revenue = 0.0
        total_opd_revenue = 0.0
        total_pharmacy_revenue = 0.0
        
        financial_history = []
        for lead in leads_qs.order_by('-id')[:200]:
            cd = lead.custom_data or {}
            total_bill = float(cd.get('total') or 0.0)
            opd_bill = float(cd.get('opd_bill') or 0.0)
            pharm_bill = float(cd.get('pharmacy_bill') or 0.0)
            
            total_billing_revenue += total_bill
            total_opd_revenue += opd_bill
            total_pharmacy_revenue += pharm_bill

            if total_bill > 0 or opd_bill > 0 or pharm_bill > 0:
                financial_history.append({
                    "lead": lead,
                    "type": "Patient Billing",
                    "doctor": cd.get('doctor', '—'),
                    "department": cd.get('department', '—'),
                    "opd": opd_bill,
                    "pharmacy": pharm_bill,
                    "total": total_bill,
                    "date": lead.inquiry_date or lead.created_at.date(),
                    "status": "Paid" if total_bill > 0 else "Pending",
                    "appointment_status": cd.get('appointment_status', '—')
                })

        # Add any admissions direct payments if applicable
        admissions_qs = Admission.objects.filter(lead__in=leads_qs)
        payments_qs = Payment.objects.filter(admission__in=admissions_qs, payment_status=PaymentStatus.SUCCESS)
        admissions_revenue = float(payments_qs.aggregate(s=Sum('amount'))['s'] or 0.0)
        
        for p in payments_qs.select_related('admission__lead').order_by('-payment_date')[:50]:
            financial_history.append({
                "lead": p.admission.lead,
                "type": "Admission Payment",
                "doctor": "—",
                "department": p.admission.course.name if p.admission.course else "—",
                "opd": 0.0,
                "pharmacy": 0.0,
                "total": float(p.amount),
                "date": p.payment_date,
                "status": p.get_payment_status_display(),
                "appointment_status": "Admitted"
            })

        total_gross_revenue = total_billing_revenue + admissions_revenue
        net_profit = total_gross_revenue - total_campaign_cost
        roi_percentage = ((net_profit / total_campaign_cost) * 100) if total_campaign_cost > 0 else (100.0 if total_gross_revenue > 0 else 0.0)

        # 3. Campaign Financial Performance & ROI Breakdown
        campaigns_financial_data = []
        for c in campaigns_qs.order_by('-id'):
            c_leads = leads_qs.filter(Q(campaign=c) | Q(custom_data__campaign=c.name))
            c_leads_count = c_leads.count()
            c_cost = float(c.cost or 0.0)
            
            c_revenue = 0.0
            c_booked_count = 0
            for cl in c_leads:
                ccd = cl.custom_data or {}
                c_revenue += float(ccd.get('total') or 0.0)
                if 'Booked' in ccd.get('appointment_status', '') or 'Confirmed' in ccd.get('appointment_status', ''):
                    c_booked_count += 1
            
            c_profit = c_revenue - c_cost
            c_roi = ((c_profit / c_cost) * 100) if c_cost > 0 else (100.0 if c_revenue > 0 else 0.0)
            cost_per_lead = (c_cost / c_leads_count) if c_leads_count > 0 else 0.0

            campaigns_financial_data.append({
                "obj": c,
                "cost": c_cost,
                "leads_count": c_leads_count,
                "booked_count": c_booked_count,
                "revenue": c_revenue,
                "profit": c_profit,
                "roi": c_roi,
                "cpl": cost_per_lead,
                "start_date": c.start_date,
                "end_date": c.end_date,
                "is_active": c.is_active,
            })

        # 4. Chart Analytics Aggregations
        import json
        from collections import defaultdict
        from django.utils import timezone
        from accounts.models import User

        # User map for attendants
        user_map = {u.id: (u.get_full_name() or u.username) for u in User.objects.all()}

        # A) Revenue vs Campaign vs Cost data
        camp_labels = []
        camp_revenues = []
        camp_costs = []
        camp_profits = []
        for c_data in campaigns_financial_data:
            camp_labels.append(c_data["obj"].name)
            camp_revenues.append(round(c_data["revenue"], 2))
            camp_costs.append(round(c_data["cost"], 2))
            camp_profits.append(round(c_data["profit"], 2))

        # B) Attendant vs Revenue & Doctor vs Revenue & Timeline
        attendant_rev_map = defaultdict(float)
        doctor_rev_map = defaultdict(float)
        timeline_by_campaign = defaultdict(lambda: defaultdict(float))
        overall_timeline_rev = defaultdict(float)

        for lead in leads_qs:
            cd = lead.custom_data or {}
            rev = float(cd.get('total') or 0.0)
            if rev <= 0:
                continue

            # Date key: YYYY-MM
            lead_date = lead.inquiry_date or lead.created_at.date()
            month_key = lead_date.strftime("%Y-%m")

            # Campaign Name
            c_name = lead.campaign.name if lead.campaign else cd.get('campaign') or 'Direct / Organic'
            timeline_by_campaign[c_name][month_key] += rev
            overall_timeline_rev[month_key] += rev

            # Attendant
            att_name = user_map.get(lead.assigned_to_id) or cd.get('attendent_name') or cd.get('lead_attendent') or 'Unassigned'
            attendant_rev_map[att_name] += rev

            # Doctor
            doc_name = cd.get('doctor') or cd.get('doctor_name') or 'Not Mentioned'
            doctor_rev_map[doc_name] += rev

        # Campaign costs spread across start date months
        overall_timeline_cost = defaultdict(float)
        camp_cost_by_month = defaultdict(lambda: defaultdict(float))
        for c in campaigns_qs:
            c_cost = float(c.cost or 0.0)
            c_month = c.start_date.strftime("%Y-%m") if c.start_date else (c.created_at.strftime("%Y-%m") if hasattr(c, 'created_at') else None)
            if not c_month:
                c_month = timezone.now().strftime("%Y-%m")
            camp_cost_by_month[c.name][c_month] += c_cost
            overall_timeline_cost[c_month] += c_cost

        # Sorted unique timeline month labels
        all_months = sorted(set(list(overall_timeline_rev.keys()) + list(overall_timeline_cost.keys())))
        if not all_months:
            all_months = [timezone.now().strftime("%Y-%m")]

        timeline_data = {
            "all_months": all_months,
            "overall": {
                "revenue": [round(overall_timeline_rev.get(m, 0.0), 2) for m in all_months],
                "cost": [round(overall_timeline_cost.get(m, 0.0), 2) for m in all_months],
            },
            "by_campaign": {}
        }
        for c_name in camp_labels:
            timeline_data["by_campaign"][c_name] = {
                "revenue": [round(timeline_by_campaign[c_name].get(m, 0.0), 2) for m in all_months],
                "cost": [round(camp_cost_by_month[c_name].get(m, 0.0), 2) for m in all_months],
            }

        # Top 8 Attendants by revenue
        top_attendants = sorted(attendant_rev_map.items(), key=lambda x: x[1], reverse=True)[:8]
        attendant_chart = {
            "labels": [item[0] for item in top_attendants],
            "revenues": [round(item[1], 2) for item in top_attendants]
        }

        # Top 8 Doctors by revenue
        top_doctors = sorted(doctor_rev_map.items(), key=lambda x: x[1], reverse=True)[:8]
        doctor_chart = {
            "labels": [item[0] for item in top_doctors],
            "revenues": [round(item[1], 2) for item in top_doctors]
        }

        campaign_chart = {
            "labels": camp_labels,
            "revenues": camp_revenues,
            "costs": camp_costs,
            "profits": camp_profits
        }

        total_leads_overall = leads_qs.count()
        cost_per_lead_overall = (total_campaign_cost / total_leads_overall) if total_leads_overall > 0 else 0.0
        revenue_per_lead_overall = (total_gross_revenue / total_leads_overall) if total_leads_overall > 0 else 0.0
        hospital_name = hospital.name if hospital else ("All Businesses (Global)" if is_superadmin else "Zappcode CRM")

        return render(request, "dashboard/financial_overview.html", {
            "title": "Financial Overview",
            "active": "financial-overview",
            "total_gross_revenue": total_gross_revenue,
            "total_campaign_cost": total_campaign_cost,
            "net_profit": net_profit,
            "roi_percentage": roi_percentage,
            "total_opd_revenue": total_opd_revenue,
            "total_pharmacy_revenue": total_pharmacy_revenue,
            "total_leads_overall": total_leads_overall,
            "cost_per_lead_overall": cost_per_lead_overall,
            "revenue_per_lead_overall": revenue_per_lead_overall,
            "campaigns_financial_data": campaigns_financial_data,
            "financial_history": financial_history[:100],
            "total_campaigns_count": campaigns_qs.count(),
            "active_campaigns_count": campaigns_qs.filter(is_active=True).count(),
            "hospital_name": hospital_name,
            # JSON chart payloads
            "campaign_chart_json": json.dumps(campaign_chart),
            "timeline_data_json": json.dumps(timeline_data),
            "attendant_chart_json": json.dumps(attendant_chart),
            "doctor_chart_json": json.dumps(doctor_chart),
            "campaign_names": camp_labels,
        })
        
    titles = {
        'roles-permissions': 'Role & Permissions',
        'staff-management': 'Staff Management',
        'manager-management': 'Manager Management',
        'lead-assignment': 'Lead Assignment',
        'lead-configuration': 'Lead Configuration',
        'doctor-management': 'Doctor Management',
        'department-management': 'Department Management',
        'appointment-management': 'Appointment Management',
        'patient-management': 'Patient Management',
        'campaign-management': 'Campaign Management',
        'reports': 'Reports',
        'financial-overview': 'Financial Overview',
        'notifications': 'Notifications',
        'tasks': 'Tasks',
        'hospital-settings': 'Hospital Settings',
        'profile-security': 'Profile & Security',
    }
    title = titles.get(module_name, module_name.replace('-', ' ').title())
    return render(request, "dashboard/nelson_generic.html", {"title": title, "module_name": module_name, "active": module_name})


@login_required
def management_home(request):
    """Dedicated management dashboard for Managers and Super Admins matching home dashboard style."""
    from accounts.models import User
    from django.core.exceptions import PermissionDenied
    from followups.models import FollowUp, Note

    if request.user.role not in (User.Role.SUPER_ADMIN, User.Role.MANAGER):
        raise PermissionDenied("This dashboard is restricted to management accounts.")
        
    if request.user.hospital is not None:
        raise PermissionDenied("This dashboard is restricted to Zappcode management only.")

    today = timezone.localdate()
    all_leads = Lead.objects.filter(is_archived=False)

    # ─── Filter Handling ────────────────────────────────────────────────────────
    leads = all_leads
    q = request.GET.get("q", "").strip()
    if q:
        leads = leads.filter(
            Q(lead_code__icontains=q) | Q(name__icontains=q) | Q(mobile__icontains=q)
            | Q(email__icontains=q) | Q(city__icontains=q)
        )
    if request.GET.get("city"):
        leads = leads.filter(city__iexact=request.GET.get("city"))
    if request.GET.get("source_category"):
        leads = leads.filter(source_category_id=request.GET.get("source_category"))
    if request.GET.get("lead_source"):
        leads = leads.filter(lead_source_id=request.GET.get("lead_source"))
    if request.GET.get("course"):
        leads = leads.filter(course_id=request.GET.get("course"))
    if request.GET.get("stage"):
        leads = leads.filter(stage_id=request.GET.get("stage"))
    if request.GET.get("temperature"):
        leads = leads.filter(temperature=request.GET.get("temperature"))
    if request.GET.get("deal_status"):
        leads = leads.filter(deal_status=request.GET.get("deal_status"))
    if request.GET.get("assigned_to"):
        leads = leads.filter(assigned_to_id=request.GET.get("assigned_to"))

    # ─── KPIs ──────────────────────────────────────────────────────────────────
    total_leads = leads.count()
    new_leads = leads.filter(inquiry_date__gte=today - timedelta(days=7)).count()
    uncontacted = leads.filter(temperature="UNCONTACTED").count()
    not_picked = leads.filter(temperature="NOT_PICKED").count()
    hot = leads.filter(temperature="HOT").count()
    warm = leads.filter(temperature="WARM").count()
    cold = leads.filter(temperature="COLD").count()
    followups_today = FollowUp.objects.filter(followup_date=today).count()
    overdue = FollowUp.objects.filter(followup_date__lt=today, followup_status="PENDING").count()
    admissions_count = Admission.objects.count()
    visits_count = leads.filter(stage__name__icontains="visit").count()
    total_revenue = Payment.objects.filter(payment_status=PaymentStatus.SUCCESS).aggregate(s=Sum("amount"))["s"] or 0
    conversion_rate = round(admissions_count / total_leads * 100, 1) if total_leads else 0.0
    pending_approvals_count = User.objects.filter(is_approved=False).count()

    # ─── Team Activity Today ──────────────────────────────────────────────────
    team_members = User.objects.filter(is_active=True, is_approved=True, role__in=['COUNSELLOR', 'HR'])
    team_stats = []
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

    # ─── Charts Data ───────────────────────────────────────────────────────────
    source_data = leads.values("lead_source__name").annotate(count=Count("id")).order_by("-count")[:8]
    source_labels = [r["lead_source__name"] or "Unknown" for r in source_data]
    source_counts = [r["count"] for r in source_data]

    stage_data = leads.values("stage__name").annotate(count=Count("id")).order_by("-count")
    funnel_labels = [r["stage__name"] or "Unassigned" for r in stage_data]
    funnel_counts = [r["count"] for r in stage_data]

    emp_lead_data = leads.values("assigned_to__first_name", "assigned_to__username").annotate(count=Count("id")).order_by("-count")[:10]
    emp_labels = [r["assigned_to__first_name"] or r["assigned_to__username"] or "Unassigned" for r in emp_lead_data]
    emp_counts = [r["count"] for r in emp_lead_data]

    course_data = leads.values("course__name").annotate(count=Count("id")).order_by("-count")[:8]
    course_labels = [c["course__name"] or "Unspecified" for c in course_data]
    course_counts = [c["count"] for c in course_data]

    # Filter dropdown options
    used_sc_ids = all_leads.values_list("source_category_id", flat=True).distinct()
    used_ls_ids = all_leads.values_list("lead_source_id", flat=True).distinct()
    used_course_ids = all_leads.values_list("course_id", flat=True).distinct()
    used_stage_ids = all_leads.values_list("stage_id", flat=True).distinct()
    used_emp_ids = all_leads.values_list("assigned_to_id", flat=True).distinct()
    distinct_cities = sorted(list(set(all_leads.exclude(city="").values_list("city", flat=True))))

    context = {
        "active": "management_dashboard",
        "today": today,
        "kpis": {
            "total_leads": total_leads, "new_leads": new_leads,
            "uncontacted": uncontacted, "not_picked": not_picked,
            "hot": hot, "warm": warm, "cold": cold,
            "followups_today": followups_today, "overdue": overdue,
            "admissions": admissions_count, "conversion_rate": conversion_rate,
            "visits": visits_count, "total_revenue": total_revenue,
        },
        "pending_approvals_count": pending_approvals_count,
        "team_stats": team_stats,
        "source_categories": SourceCategory.objects.filter(id__in=used_sc_ids),
        "lead_sources": LeadSource.objects.filter(id__in=used_ls_ids),
        "courses": Course.objects.filter(id__in=used_course_ids),
        "stages": LeadStage.objects.filter(id__in=used_stage_ids),
        "employees": User.objects.filter(id__in=used_emp_ids),
        "cities": distinct_cities,
        "request_get": request.GET,
        "new_leads_date_from": (today - timedelta(days=7)).strftime("%Y-%m-%d"),
        "chart_data": json.dumps({
            "source": {"labels": source_labels, "counts": source_counts},
            "funnel": {"labels": funnel_labels, "counts": funnel_counts},
            "employee": {"labels": emp_labels, "counts": emp_counts},
            "course": {"labels": course_labels, "counts": course_counts},
        }),
    }
    return render(request, "dashboard/management_home.html", context)




def _get_effective_hospital(request):
    """
    Resolves the effective hospital/business for reports:
    1. If user has a hospital attached, returns that hospital.
    2. For Super Admin / Global Admin: checks GET 'business'/'hospital' or session 'active_business_id'.
    3. Returns (hospital_object, is_filtered_by_hospital).
    """
    user = request.user
    if user.hospital:
        return user.hospital
    selected_biz_id = (
        request.GET.get("business", "").strip()
        or request.GET.get("hospital", "").strip()
        or str(request.session.get("active_business_id", "")).strip()
    )
    if selected_biz_id and selected_biz_id.isdigit():
        return Hospital.objects.filter(id=int(selected_biz_id)).first()
    return None


@login_required
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
    day_followups = FollowUp.objects.filter(
        Q(created_by=request.user, followup_date=report_date) |
        Q(created_by=request.user, created_at__date=report_date)
    )
    
    # 1. Calls & Follow-ups / Touches
    outgoing_calls_cnt = day_followups.filter(followup_mode="CALL_OUTGOING").count()
    incoming_calls_cnt = day_followups.filter(followup_mode="CALL_INCOMING").count()
    calls_not_connected_cnt = day_followups.filter(followup_status="NOT_CONNECTED").count()
    follow_ups_taken_cnt = day_followups.count()

    leads_touched_today = Lead.objects.filter(
        Q(created_by=request.user, created_at__date=report_date) |
        Q(assigned_to=request.user, updated_at__date=report_date)
    ).distinct().count()
    calls_attended_cnt = max(follow_ups_taken_cnt, leads_touched_today, day_followups.filter(followup_mode__in=["CALL_OUTGOING", "CALL_INCOMING", "CALL"]).count())

    # 2. Leads Assigned to this user today (Captured by user + Assigned by admin/manager)
    leads_assigned_cnt = Lead.objects.filter(
        Q(assigned_to=request.user, inquiry_date=report_date) |
        Q(assigned_to=request.user, created_at__date=report_date) |
        Q(created_by=request.user, created_at__date=report_date)
    ).distinct().count()

    # 3. Appointments Booked / Approved today (for Hospital)
    from leads.models import Appointment, AppointmentStatus
    report_date_str = report_date.strftime("%Y-%m-%d")
    report_date_alt_str = report_date.strftime("%d-%m-%Y")
    
    appts_model_cnt = Appointment.objects.filter(
        lead__assigned_to=request.user,
    ).filter(
        Q(appointment_date=report_date) |
        Q(created_at__date=report_date)
    ).filter(
        status__in=[AppointmentStatus.APPROVED, AppointmentStatus.SCHEDULED, AppointmentStatus.PENDING_APPROVAL, AppointmentStatus.COMPLETED]
    ).values('lead').distinct().count()

    appts_leads_cnt = Lead.objects.filter(
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
    ).distinct().count()

    appointments_booked_cnt = max(appts_model_cnt, appts_leads_cnt)

    # 4. Freeze Leads (Cancelled / Not Interested / Cold)
    freeze_leads_cnt = Lead.objects.filter(
        assigned_to=request.user,
        updated_at__date=report_date
    ).filter(
        Q(temperature="COLD") | 
        Q(custom_data__appointment_status__icontains="Cancel") | 
        Q(custom_data__appointment_status__icontains="Reject") |
        Q(custom_data__appointment_status__icontains="Not Interested")
    ).count()

    # 5. Pending Leads (Today's pending followups & uncontacted/open assigned leads)
    pending_followups_cnt = FollowUp.objects.filter(
        lead__assigned_to=request.user,
        followup_date__lte=report_date,
        followup_status__in=["PENDING", "MISSED", "SCHEDULED"]
    ).values('lead').distinct().count()

    uncontacted_assigned_cnt = len(filter_uncontacted_leads_ids(
        Lead.objects.filter(assigned_to=request.user, is_archived=False),
        today=report_date
    ))

    follow_ups_pending_cnt = FollowUp.objects.filter(
        lead__assigned_to=request.user,
        followup_date__lte=report_date,
        followup_status__in=["PENDING", "MISSED", "SCHEDULED"]
    ).count()

    pending_leads_cnt = max(pending_followups_cnt + uncontacted_assigned_cnt, follow_ups_pending_cnt)

    # 6. Tomorrow's Follow-ups scheduled
    tomorrow_date = report_date + timedelta(days=1)
    tomorrow_fu_cnt = FollowUp.objects.filter(
        Q(lead__assigned_to=request.user) | Q(created_by=request.user),
        followup_date=tomorrow_date,
        followup_status__in=["PENDING", "SCHEDULED"]
    ).count()
    tomorrow_lead_cnt = Lead.objects.filter(
        assigned_to=request.user,
        next_followup_date=tomorrow_date
    ).count()
    tomorrow_followups_cnt = max(tomorrow_fu_cnt, tomorrow_lead_cnt)

    leads_interested_cnt = Lead.objects.filter(
        assigned_to=request.user,
        temperature__in=["WARM", "HOT"],
        updated_at__date=report_date
    ).count()

    leads_cold_cnt = Lead.objects.filter(
        assigned_to=request.user,
        temperature="COLD",
        updated_at__date=report_date
    ).count()

    leads_visited_cnt = Lead.objects.filter(
        assigned_to=request.user,
        updated_at__date=report_date
    ).filter(
        Q(custom_data__appointment_status__icontains="Visit") |
        Q(custom_data__appointment_status__icontains="Arrived") |
        Q(custom_data__appointment_status__icontains="Completed") |
        Q(custom_data__status__icontains="Visit")
    ).count()

    # 7. Login / Logout times from AuditLog
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
    
    # 8. Admissions Done today and Payments Done today
    adm_records_cnt = Admission.objects.filter(
        Q(lead__assigned_to=request.user) | Q(assigned_counselor=request.user),
        admission_date=report_date
    ).count()
    lead_adm_cnt = Lead.objects.filter(
        assigned_to=request.user
    ).filter(
        Q(admission_status="ADMITTED") | Q(stage__name__icontains="Admission")
    ).filter(
        Q(updated_at__date=report_date) | Q(admission__admission_date=report_date)
    ).distinct().count()
    admissions_today_cnt = max(adm_records_cnt, lead_adm_cnt)

    payments_today_qs = Payment.objects.filter(
        Q(admission__lead__assigned_to=request.user) | Q(admission__assigned_counselor=request.user),
        payment_date=report_date,
        payment_status=PaymentStatus.SUCCESS
    )
    payments_done_cnt = payments_today_qs.count()
    fees_today_sum = payments_today_qs.aggregate(s=Sum("amount"))["s"] or 0

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

    if user_business_type == "hospital" and user_role in (User.Role.LEAD_ATTENDENT, User.Role.DOCTOR, User.Role.ADMIN, User.Role.MANAGER):
        is_hospital_form = True
        FormClass = HospitalDailyReportForm
        template_name = "dashboard/hospital_daily_report_form.html"
    else:
        # Zappcode Academy Business (Counsellor, HR, Manager, Admin, Super Admin)
        is_hospital_form = False
        FormClass = AcademyDailyReportForm
        template_name = "dashboard/academy_reports_form.html"

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
                    for r_user in recipients:
                        Notification.objects.create(
                            user=r_user,
                            title=f"EOD Report ({action_word.capitalize()}) from {request.user.get_full_name() or request.user.username}",
                            message=(
                                f"{request.user.get_full_name() or request.user.username} {action_word} Daily EOD Report for {report_date.strftime('%d %b %Y')}.\n"
                                f"Assigned Leads: {report.leads_assigned} | Calls: {report.calls_attended} | "
                                f"Admissions Done: {report.admissions_done} | Payments: {report.payments_done} (₹{report.fees_collected}) | "
                                f"Pending Leads: {report.pending_leads} | Tomorrow FU: {report.tomorrow_followups} | "
                                f"Mood: {report.mood_display}"
                            ),
                            link="/dashboard/reports/admin/",
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
            if is_hospital_form:
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
            if is_hospital_form:
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
    
    if date_from_str:
        try:
            date_from = datetime.strptime(date_from_str.strip(), "%Y-%m-%d").date()
            reports = reports.filter(report_date__gte=date_from)
        except ValueError:
            pass
            
    if date_to_str:
        try:
            date_to = datetime.strptime(date_to_str.strip(), "%Y-%m-%d").date()
            reports = reports.filter(report_date__lte=date_to)
        except ValueError:
            pass
            
    # Check for Excel export
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
def telecaller_home(request):
    from accounts.models import User
    from leads.models import Lead, LeadTemperature, DealStatus, Appointment, AppointmentStatus
    from dashboard.models import TaskReminder
    from followups.models import FollowUp
    from datetime import date
    
    if request.user.role != User.Role.LEAD_ATTENDENT or not request.user.hospital:
        messages.error(request, "Access denied.")
        return redirect("dashboard:home")
        
    user = request.user
    today_date = timezone.localdate()
    start_of_today = timezone.make_aware(datetime.combine(today_date, datetime.min.time()))
    end_of_today = timezone.make_aware(datetime.combine(today_date, datetime.max.time()))
    today_str = today_date.strftime("%Y-%m-%d")
    today_alt_str = today_date.strftime("%d-%m-%Y")
    
    hospital_leads = Lead.objects.filter(hospital=user.hospital, is_archived=False)

    # CARD 1: Today's New Leads Count (Hospital wide received today)
    todays_new_leads_count = hospital_leads.filter(
        Q(created_at__range=(start_of_today, end_of_today)) | Q(inquiry_date=today_date)
    ).distinct().count()

    # CARD 2: Call Not Done Count (Pending calling queue for user / unassigned)
    if user.role == User.Role.LEAD_ATTENDENT:
        cnd_candidates = hospital_leads.filter(
            Q(assigned_to=user) | Q(assigned_to__isnull=True) | Q(custom_data__lead_attendant__in=['Unassigned', '', None, 'nan'])
        )
    else:
        cnd_candidates = hospital_leads

    call_not_done_count = len(filter_uncontacted_leads_ids(cnd_candidates, today=today_date))

    # CARD 3: Today's OPD Booked & Consultation Booked by User
    appts_model_cnt = Appointment.objects.filter(
        lead__hospital=user.hospital,
        lead__assigned_to=user,
    ).filter(
        Q(appointment_date=today_date) |
        Q(created_at__date=today_date)
    ).filter(
        status__in=[AppointmentStatus.APPROVED, AppointmentStatus.SCHEDULED, AppointmentStatus.COMPLETED, AppointmentStatus.PENDING_APPROVAL]
    ).values('lead').distinct().count()

    tele_user_leads_today = hospital_leads.filter(
        assigned_to=user
    ).filter(
        Q(created_at__range=(start_of_today, end_of_today)) |
        Q(inquiry_date=today_date) |
        Q(custom_data__appo_booked_date=today_str) |
        Q(custom_data__appo_booked_date=today_alt_str) |
        Q(custom_data__appointment_date=today_str) |
        Q(custom_data__appointment_date=today_alt_str) |
        Q(custom_data__appointment_confirmed_at__startswith=today_str)
    )

    opd_user_leads_cnt = tele_user_leads_today.filter(
        Q(custom_data__appointment_status__iexact='OPD Booking') |
        Q(custom_data__appointment_status__icontains='OPD') |
        Q(custom_data__appointment_status__icontains='Book') |
        Q(custom_data__appointment_status__icontains='Confirm') |
        Q(custom_data__appointment_status__icontains='Complete') |
        Q(custom_data__appointment_status__icontains='Done')
    ).exclude(
        Q(custom_data__appointment_status__icontains='Consult') | Q(custom_data__icontains='consult')
    ).distinct().count()

    todays_opd_booked_count = max(appts_model_cnt, opd_user_leads_cnt)

    todays_consult_booked_count = tele_user_leads_today.filter(
        Q(custom_data__appointment_status__icontains='Consult') | Q(custom_data__icontains='consult')
    ).distinct().count()

    # CARD 4: Today's Follow-ups for User (Assigned to user or created by user)
    booked_exclude_tele = (
        Q(custom_data__appointment_status__icontains='Book') |
        Q(custom_data__appointment_status__icontains='Confirm') |
        Q(deal_status__in=[DealStatus.WON, DealStatus.LOST]) |
        Q(admission_status='ADMISSION_DONE') |
        (Q(custom_data__total__isnull=False) & ~Q(custom_data__total__in=['0', '0.00', '', '0.0', 0, 0.0]))
    )

    user_fu_qs = hospital_leads.filter(
        Q(assigned_to=user) | Q(created_by=user)
    ).exclude(
        booked_exclude_tele
    ).filter(
        Q(next_followup_date__isnull=False) |
        Q(followups__next_followup_date__isnull=False) |
        Q(custom_data__appointment_status__icontains='follow')
    ).distinct().select_related('stage', 'campaign', 'lead_source')

    todays_tele_followups_list = []
    overdue_tele_followups_list = []
    upcoming_tele_followups_list = []

    for l in user_fu_qs:
        sched_date = l.next_followup_date
        if not sched_date:
            latest_fu = l.followups.filter(next_followup_date__isnull=False).order_by('-id').first()
            if latest_fu:
                sched_date = latest_fu.next_followup_date
        if not sched_date:
            continue

        cd = l.custom_data or {}
        has_remark = any(bool(cd.get(k) and str(cd.get(k)).strip().lower() not in ('nan', 'none', '', '-')) for k in ['remark_1', 'remark_2', 'remark_3', 'followup_remark'])
        latest_fu = l.followups.order_by('-id').first()
        has_status_update = False
        if latest_fu and latest_fu.followup_status not in ('PENDING', 'CALL_BACK', 'RESCHEDULED') and latest_fu.followup_date >= sched_date:
            has_status_update = True
        is_updated = (has_remark or has_status_update)

        # Categorize for template display
        f_type = 'Call Follow-up'
        if cd.get('pharmacy_bill') or cd.get('opd_bill') or cd.get('total') or getattr(l, 'custom_deal_status', '') == 'Payment Pending':
            f_type = 'Billing Follow-up'
        elif any(k in str(cd.get('appointment_status') or '').lower() for k in ['appo', 'reschedule', 'slot']):
            f_type = 'Appointment Follow-up'
        elif cd.get('remark_1') or cd.get('last_called_date'):
            f_type = 'Calling Follow-up'
        l.followup_category = f_type
        l.is_overdue = bool(sched_date < today_date)

        if sched_date == today_date:
            todays_tele_followups_list.append(l)
        elif sched_date < today_date:
            if not is_updated:
                overdue_tele_followups_list.append(l)
        elif sched_date > today_date:
            if not is_updated:
                upcoming_tele_followups_list.append(l)

    todays_followups_count = len(todays_tele_followups_list)
    overdue_followups_count = len(overdue_tele_followups_list)
    upcoming_followups_count = len(upcoming_tele_followups_list)

    # SECTION 4: Pending & Upcoming Follow-ups list for bottom table
    pending_and_upcoming_followups = (overdue_tele_followups_list + todays_tele_followups_list + upcoming_tele_followups_list)[:10]
    pending_and_upcoming_followups_count = (overdue_followups_count + todays_followups_count + upcoming_followups_count)


    # CARD 5: Today's Walk-in Leads
    todays_walkin_count = hospital_leads.filter(
        Q(lead_source__name__icontains='walk-in') |
        Q(custom_data__lead_source__icontains='walk-in') |
        Q(custom_data__source__icontains='walk-in')
    ).filter(
        Q(created_at__range=(start_of_today, end_of_today)) | Q(inquiry_date=today_date)
    ).distinct().count()

    # CARD 6: Today's Calling Target & Countdown
    daily_target = user.daily_call_target
    calls_completed_today = hospital_leads.filter(
        assigned_to=user
    ).filter(
        Q(custom_data__calling_date_remark_1=today_str) |
        Q(custom_data__calling_date_remark_1=today_alt_str) |
        Q(custom_data__calling_date_remark_2=today_str) |
        Q(custom_data__calling_date_remark_2=today_alt_str) |
        Q(custom_data__calling_date_remark_3=today_str) |
        Q(custom_data__calling_date_remark_3=today_alt_str) |
        Q(custom_data__last_called_date=today_str) |
        Q(custom_data__last_called_date=today_alt_str) |
        Q(followups__followup_date=today_date, followups__created_by=user)
    ).distinct().count()
    target_remaining = max(0, daily_target - calls_completed_today)

    # My Recent Leads (Latest 10 entries assigned to this user, newly updated first)
    my_recent_leads = hospital_leads.filter(
        assigned_to=user
    ).select_related('stage', 'campaign', 'lead_source').prefetch_related('appointments').order_by('-updated_at')[:10]

    # Today's Tasks & Reminders
    todays_tasks = TaskReminder.objects.filter(
        Q(user=user) | Q(user__hospital=user.hospital, user__role__in=['SUPER_ADMIN', 'MANAGER']),
        due_date=today_date,
    ).exclude(
        status=TaskReminder.Status.COMPLETED
    ).select_related('user', 'lead').annotate(
        priority_order=Case(
            When(priority=TaskReminder.Priority.URGENT, then=Value(1)),
            When(priority=TaskReminder.Priority.HIGH, then=Value(2)),
            When(priority=TaskReminder.Priority.MEDIUM, then=Value(3)),
            When(priority=TaskReminder.Priority.LOW, then=Value(4)),
            default=Value(5),
            output_field=IntegerField(),
        ),
        status_order=Case(
            When(status=TaskReminder.Status.PENDING, then=Value(1)),
            When(status=TaskReminder.Status.IN_PROGRESS, then=Value(2)),
            When(status=TaskReminder.Status.COMPLETED, then=Value(3)),
            When(status=TaskReminder.Status.CANCELLED, then=Value(4)),
            default=Value(5),
            output_field=IntegerField(),
        ),
    ).order_by('priority_order', 'status_order', 'due_time', '-created_at')

    # SECTION 3: Upcoming OPD / Appointments (Booked for dates ahead of today)
    # 1. Filter only candidate leads that actually have appointment booking dates in future
    upcoming_opd_candidates = hospital_leads.filter(
        assigned_to=user
    ).filter(
        Q(custom_data__appo_booked_date__gt=today_str) |
        Q(custom_data__appointment_date__gt=today_str)
    ).select_related('stage', 'campaign', 'lead_source')
    
    upcoming_opd_leads_list = []
    for l in upcoming_opd_candidates:
        cd = l.custom_data or {}
        apt_st = str(cd.get('appointment_status') or cd.get('deal_status') or '').strip()
        bk_date_str = cd.get('appo_booked_date') or cd.get('appointment_date')
        
        # Check if booking status is active
        is_booked = any(k in apt_st.lower() for k in ['book', 'confirm', 'yes', 'scheduled', 'approved', 'awaiting'])
        if is_booked and bk_date_str:
            try:
                bk_dt = datetime.strptime(str(bk_date_str).strip()[:10], '%Y-%m-%d').date()
                if bk_dt > today_date:
                    l.booking_scheduled_date = bk_dt
                    l.booking_scheduled_time = cd.get('appointment_time') or '-'
                    upcoming_opd_leads_list.append(l)
            except Exception:
                pass
                
    # Also check Appointment objects linked to this user's leads in future
    apt_objs = Appointment.objects.filter(
        lead__hospital=user.hospital,
        lead__assigned_to=user,
        appointment_date__gt=today_date,
    ).exclude(
        status__in=[AppointmentStatus.CANCELLED, AppointmentStatus.COMPLETED]
    ).select_related('lead')
    
    seen_lead_ids = {l.id for l in upcoming_opd_leads_list}
    for apt in apt_objs:
        if apt.lead and apt.lead.id not in seen_lead_ids:
            lead_obj = apt.lead
            lead_obj.booking_scheduled_date = apt.appointment_date
            lead_obj.booking_scheduled_time = apt.appointment_time.strftime('%I:%M %p') if apt.appointment_time else '-'
            upcoming_opd_leads_list.append(lead_obj)
            seen_lead_ids.add(apt.lead.id)

    upcoming_opd_count = len(upcoming_opd_leads_list)
    upcoming_opd_leads = sorted(upcoming_opd_leads_list, key=lambda x: getattr(x, 'booking_scheduled_date', today_date))[:10]

    context = {
        'active': 'telecaller_dashboard',
        'todays_new_leads_count': todays_new_leads_count,
        'call_not_done_count': call_not_done_count,
        'todays_opd_booked_count': todays_opd_booked_count,
        'todays_consult_booked_count': todays_consult_booked_count,
        'todays_total_booked_count': (todays_opd_booked_count + todays_consult_booked_count),
        'todays_followups_count': todays_followups_count,
        'overdue_followups_count': overdue_followups_count,
        'upcoming_followups_count': upcoming_followups_count,
        'todays_walkin_count': todays_walkin_count,
        'daily_target': daily_target,
        'calls_completed_today': calls_completed_today,
        'target_remaining': target_remaining,
        'my_recent_leads': my_recent_leads,
        'todays_tasks': todays_tasks,
        'upcoming_opd_leads': upcoming_opd_leads,
        'upcoming_opd_count': upcoming_opd_count,
        'pending_and_upcoming_followups': pending_and_upcoming_followups,
        'pending_and_upcoming_followups_count': pending_and_upcoming_followups_count,
        'today_date': today_date,
    }
    return render(request, "dashboard/nel_telecaller_home.html", context)

@login_required
def placeholder_view(request, module_name):
    # This acts as a dummy view for all incomplete telecaller modules
    return render(request, "dashboard/placeholder.html", {"active": module_name, "module_name": module_name.replace("_", " ").title()})

@login_required
def telecaller_search(request):
    from accounts.models import User
    from leads.models import Lead, DealStatus, AdmissionStatus, MasterGroup, HospitalDepartment, HospitalDoctor
    from django.db.models import Q
    import csv
    from django.http import HttpResponse
    from django.core.paginator import Paginator
    from datetime import datetime

    if request.user.role != User.Role.LEAD_ATTENDENT or not request.user.hospital:
        messages.error(request, "Access denied.")
        return redirect("dashboard:home")

    leads = Lead.objects.filter(hospital=request.user.hospital).order_by('-inquiry_date')

    from leads.models import LeadStage
    
    # Get Multi-select & single-value filter parameters
    q = request.GET.get('q', '').strip()
    selected_campaigns = request.GET.getlist("campaign")
    selected_sources = request.GET.getlist("lead_source")
    selected_departments = request.GET.getlist("department")
    selected_doctors = request.GET.getlist("doctor")
    selected_assigned = request.GET.getlist("assigned_to") or request.GET.getlist("assigned")
    selected_deal_statuses = request.GET.getlist("deal_status") or request.GET.getlist("status")
    selected_appointment_statuses = request.GET.getlist("appointment_status")
    selected_priorities = request.GET.getlist("priority")
    selected_temperatures = request.GET.getlist("temperature")
    selected_locations = request.GET.getlist("location")

    # Search
    if q:
        leads = leads.filter(Q(name__icontains=q) | Q(mobile__icontains=q) | Q(lead_code__icontains=q))

    # Date Filter
    def _parse_date(val):
        if not val:
            return None
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(val.strip(), fmt).date()
            except ValueError:
                continue
        return None

    date_from = _parse_date(request.GET.get("date_from") or request.GET.get("date"))
    date_to = _parse_date(request.GET.get("date_to"))
    if date_from:
        leads = leads.filter(inquiry_date__gte=date_from)
    if date_to:
        leads = leads.filter(inquiry_date__lte=date_to)

    # 1. Campaigns
    if selected_campaigns:
        camp_q = Q()
        for c_val in selected_campaigns:
            if c_val:
                camp_q |= Q(custom_data__campaign__iexact=c_val) | Q(campaign__name__iexact=c_val)
                if c_val.isdigit():
                    camp_q |= Q(campaign_id=int(c_val))
        leads = leads.filter(camp_q)

    # 2. Lead Sources
    if selected_sources:
        src_q = Q()
        for s_val in selected_sources:
            if s_val:
                src_q |= Q(custom_data__lead_source__iexact=s_val) | Q(lead_source__name__iexact=s_val)
                if s_val.isdigit():
                    src_q |= Q(lead_source_id=int(s_val))
        leads = leads.filter(src_q)

    # 3. Department
    if selected_departments:
        dept_q = Q()
        for d_val in selected_departments:
            if d_val:
                dept_q |= Q(custom_data__department__icontains=d_val) | Q(custom_data__disease__icontains=d_val)
        leads = leads.filter(dept_q)
    elif request.GET.get('disease'):
        leads = leads.filter(custom_data__disease__icontains=request.GET.get('disease').strip())

    # 4. Doctor
    if selected_doctors:
        doc_q = Q()
        for doc_val in selected_doctors:
            if doc_val:
                doc_q |= Q(custom_data__doctor__icontains=doc_val)
        leads = leads.filter(doc_q)
    elif request.GET.get('doctor'):
        leads = leads.filter(custom_data__doctor__icontains=request.GET.get('doctor').strip())

    # 5. Assigned To
    if selected_assigned:
        emp_q = Q()
        for emp_val in selected_assigned:
            if emp_val == "unassigned" or emp_val.lower() in ['unassigned', 'new']:
                emp_q |= Q(assigned_to__isnull=True)
            elif emp_val == "assigned":
                emp_q |= Q(assigned_to__isnull=False)
            elif emp_val == "my_leads":
                emp_q |= Q(assigned_to=request.user)
            elif emp_val and emp_val.isdigit():
                emp_q |= Q(assigned_to_id=int(emp_val))
        leads = leads.filter(emp_q)

    # 6. Status & Deal Status
    if selected_deal_statuses:
        st_q = Q()
        for ds_val in selected_deal_statuses:
            if ds_val.lower() == 'assigned':
                st_q |= Q(assigned_to__isnull=False)
            elif ds_val.lower() in ['unassigned', 'new']:
                st_q |= Q(assigned_to__isnull=True)
            else:
                st_q |= Q(stage__name__iexact=ds_val) | Q(deal_status__iexact=ds_val) | Q(custom_data__deal_status__iexact=ds_val)
        leads = leads.filter(st_q)

    # 7. Appointment Status
    if selected_appointment_statuses:
        apt_q = Q()
        for apt_val in selected_appointment_statuses:
            if apt_val:
                apt_q |= Q(custom_data__appointment_status__icontains=apt_val)
        leads = leads.filter(apt_q)

    # 8. Priority & Temperature
    if selected_priorities or selected_temperatures:
        prio_q = Q()
        for p_val in (selected_priorities + selected_temperatures):
            if p_val:
                prio_q |= Q(custom_data__priority__iexact=p_val) | Q(temperature__iexact=p_val)
        leads = leads.filter(prio_q)

    # 9. Location / City
    if selected_locations:
        loc_q = Q()
        for loc_val in selected_locations:
            if loc_val:
                loc_q |= Q(location__iexact=loc_val) | Q(city__iexact=loc_val) | Q(custom_data__location__iexact=loc_val)
        leads = leads.filter(loc_q)

    # Conversion status
    converted_filter = request.GET.get('converted', '')
    if converted_filter == 'yes':
        leads = leads.filter(admission_status=AdmissionStatus.ADMISSION_DONE)
    elif converted_filter == 'no':
        leads = leads.exclude(admission_status=AdmissionStatus.ADMISSION_DONE)

    # Handle Export (Excel & PDF)
    export_format = request.GET.get('export', '').lower()
    if export_format in ('1', 'excel', 'xlsx', 'csv'):
        import pandas as pd
        rows = []
        is_hospital = bool(user.hospital or user.role == 'LEAD_ATTENDENT')
        for lead in leads:
            cd = lead.custom_data or {}
            row_dict = {
                "Lead Code": lead.lead_code,
                "Patient Name": lead.name,
                "Mobile": lead.mobile,
                "Doctor": cd.get('doctor', ''),
                "Department": cd.get('department', '') or cd.get('disease', ''),
            }
            if not is_hospital:
                row_dict["Priority"] = cd.get('priority', '') or lead.get_temperature_display()
            row_dict.update({
                "Lead Status": cd.get('deal_status', '') or lead.get_deal_status_display(),
                "Appointment Status": cd.get('appointment_status', ''),
                "Inquiry Date": str(lead.inquiry_date) if lead.inquiry_date else '',
                "Assigned Staff": lead.assigned_to.get_full_name() if lead.assigned_to else 'Unassigned',
                "Location": lead.location or lead.city or '',
            })
            rows.append(row_dict)
        df = pd.DataFrame(rows)
        response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        response['Content-Disposition'] = f'attachment; filename="leads_export_{timezone.now().strftime("%Y%m%d_%H%M")}.xlsx"'
        df.to_excel(response, index=False, sheet_name="Leads")
        return response
    elif export_format == "pdf":
        return render(request, "leads/leads_print_pdf.html", {
            "leads": leads[:500],
            "total_count": leads.count(),
            "now": timezone.now(),
            "active_filters_count": active_filters_count,
        })
    # Sorting logic
    sort_by = request.GET.get("sort", "-created_at")
    sort_mapping = {
        "-created_at": "-created_at",
        "created_at": "created_at",
        "-updated_at": "-updated_at",
        "updated_at": "updated_at",
        "name_asc": "name",
        "name_desc": "-name",
        "-inquiry_date": "-inquiry_date",
        "inquiry_date": "inquiry_date",
    }
    order_field = sort_mapping.get(sort_by, "-created_at")
    leads = leads.order_by(order_field)

    paginator = Paginator(leads, 25)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1) if hasattr(paginator, 'get_elided_page_range') else paginator.page_range

    query_params = request.GET.copy()
    if 'page' in query_params:
        del query_params['page']

    # Filter choices extraction for Nelson Hospital
    filter_departments = list(HospitalDepartment.objects.filter(hospital=request.user.hospital, is_active=True).values_list("name", flat=True))
    filter_doctors = list(HospitalDoctor.objects.filter(hospital=request.user.hospital, is_active=True).values_list("name", flat=True))
    if not filter_departments:
        filter_departments = list(MasterGroup.get_active_choices("Departments").filter(hospital=request.user.hospital).values_list("name", flat=True))
    if not filter_doctors:
        filter_doctors = list(MasterGroup.get_active_choices("Doctors").filter(hospital=request.user.hospital).values_list("name", flat=True))
    if not filter_departments:
        filter_departments = ["Gynaecology", "Paediatrics", "NICU / PICU", "Obstetrics", "General OPD"]

    hospital_campaigns = MasterGroup.get_active_choices("Campaigns").filter(hospital=request.user.hospital)
    hospital_sources = MasterGroup.get_active_choices("Lead Sources").filter(hospital=request.user.hospital)
    hospital_statuses = MasterGroup.get_active_choices("Deal Statuses").filter(hospital=request.user.hospital)
    employees = User.objects.filter(hospital=request.user.hospital, is_active=True, is_approved=True)

    filter_appointment_statuses = ["Booked", "Booking Done", "Pending Confirmation", "Awaiting Doctor Approval", "Visited / OPD Done", "Cancelled", "Not Interested", "Payment Done"]
    filter_priorities = ["Hot", "Warm", "Cold"]
    filter_locations = sorted(list(set(Lead.objects.filter(hospital=request.user.hospital).exclude(location="").values_list("location", flat=True))))

    active_filters_count = (
        len(selected_campaigns) + len(selected_sources) + len(selected_departments) +
        len(selected_doctors) + len(selected_assigned) + len(selected_deal_statuses) +
        len(selected_appointment_statuses) + len(selected_priorities) + len(selected_temperatures) +
        len(selected_locations) + (1 if (date_from or date_to) else 0)
    )

    context = {
        'page_obj': page_obj,
        'leads': page_obj,
        'page_range': page_range,
        'total_count': paginator.count,
        'query_params': query_params.urlencode(),
        'q': q,
        'hospital_campaigns': hospital_campaigns,
        'hospital_sources': hospital_sources,
        'hospital_statuses': hospital_statuses,
        'employees': employees,
        'filter_departments': filter_departments,
        'filter_doctors': filter_doctors,
        'filter_appointment_statuses': filter_appointment_statuses,
        'filter_priorities': filter_priorities,
        'filter_locations': filter_locations,
        'selected_campaigns': selected_campaigns,
        'selected_sources': selected_sources,
        'selected_departments': selected_departments,
        'selected_doctors': selected_doctors,
        'selected_assigned': selected_assigned,
        'selected_deal_statuses': selected_deal_statuses,
        'selected_appointment_statuses': selected_appointment_statuses,
        'selected_priorities': selected_priorities,
        'selected_temperatures': selected_temperatures,
        'selected_locations': selected_locations,
        'date_from_val': request.GET.get('date_from', '') or request.GET.get('date', ''),
        'date_to_val': request.GET.get('date_to', ''),
        'current_sort': sort_by,
        'active_filters_count': active_filters_count,
        'request_get': request.GET,
        'active': 'search_filter',
    }
    return render(request, "dashboard/telecaller_search.html", context)

@login_required
def doctor_home(request):
    from accounts.models import User
    from leads.models import Appointment, AppointmentStatus, DoctorSchedule, DoctorLeave
    
    if request.user.role != User.Role.DOCTOR or not request.user.hospital:
        messages.error(request, "Doctor access required.")
        return redirect("dashboard:home")
        
    doctor = request.user
    today = timezone.localdate()
    
    # Handle actions (Approval / Status change / Doctor Notes)
    if request.method == "POST":
        action = request.POST.get('action')
        apt_id = request.POST.get('appointment_id')
        apt = get_object_or_404(Appointment, pk=apt_id, hospital=doctor.hospital)
        from notifications.models import Notification
        
        time_str = apt.appointment_time.strftime('%I:%M %p') if apt.appointment_time else 'Scheduled'
        date_str = apt.appointment_date.strftime('%d %b %Y')
        lead = apt.lead

        if action == "approve":
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
            lead.next_followup_date = None # Lead is now confirmed booked OPD, remove from generic follow-ups
            lead.save(update_fields=['custom_data', 'next_followup_date'])

            # Notify Lead Attendant
            if lead.assigned_to:
                Notification.objects.create(
                    user=lead.assigned_to,
                    title="Appointment Approved by Doctor",
                    message=f"Dr. {doctor.get_full_name() or doctor.username} confirmed and booked appointment for patient {lead.name} on {date_str} at {time_str}.",
                    link=f"/leads/{lead.pk}/",
                )

            messages.success(request, f"Appointment for {lead.name} on {date_str} at {time_str} approved and Booking Confirmed! Notification sent to Lead Attendant.")

        elif action == "reject" or action == "cancel":
            reason = request.POST.get('reject_reason', '').strip() or request.POST.get('doctor_notes', '').strip() or 'Doctor unavailable / slot full'
            apt.status = AppointmentStatus.CANCELLED
            apt.doctor_notes = reason
            apt.save(update_fields=['status', 'doctor_notes'])

            # Update Lead custom data & set next follow-up so lead attendant can reschedule
            cd = lead.custom_data or {}
            cd['appointment_status'] = f"Doctor Rejected: {reason}"
            lead.custom_data = cd
            lead.next_followup_date = timezone.localdate() # Shift to follow-ups for immediate action
            lead.save(update_fields=['custom_data', 'next_followup_date'])

            # Notify Lead Attendant
            if lead.assigned_to:
                Notification.objects.create(
                    user=lead.assigned_to,
                    title="Appointment Rejected by Doctor",
                    message=f"Dr. {doctor.get_full_name() or doctor.username} rejected appointment for {lead.name} ({date_str}). Reason: {reason}. Lead moved to your Follow-ups list.",
                    link=f"/leads/{lead.pk}/",
                )

            messages.info(request, f"Appointment for {lead.name} rejected with reason '{reason}'. Lead Attendant notified.")

        elif action == "change_slot":
            new_date_str = request.POST.get('new_date', '').strip()
            new_time_str = request.POST.get('new_time', '').strip()
            remark = request.POST.get('doctor_remark', '').strip() or request.POST.get('doctor_notes', '').strip() or 'Doctor requested to reschedule to this new slot.'
            
            if new_date_str:
                from datetime import datetime
                new_date_obj = datetime.strptime(new_date_str, "%Y-%m-%d").date()
                
                # Check for doctor leave
                from leads.models import DoctorLeave
                is_on_leave = DoctorLeave.objects.filter(
                    doctor=doctor,
                    start_date__lte=new_date_obj,
                    end_date__gte=new_date_obj
                ).exists()
                
                if is_on_leave:
                    messages.error(request, "Cannot reschedule/update appointment to this date as you are marked on leave/vacation.")
                    return redirect("dashboard:doctor_home")
                    
                apt.appointment_date = new_date_obj
            if new_time_str:
                apt.appointment_time = new_time_str
            
            apt.status = AppointmentStatus.SCHEDULED
            apt.doctor_notes = remark
            apt.save(update_fields=['appointment_date', 'appointment_time', 'status', 'doctor_notes'])
            
            # Update Lead custom data & move to Telecaller's today follow-ups for patient confirmation
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
            
            # Send Notification to Telecaller
            if lead.assigned_to:
                time_display = apt.appointment_time.strftime('%I:%M %p') if hasattr(apt.appointment_time, 'strftime') else str(apt.appointment_time)
                date_display = apt.appointment_date.strftime('%d %b %Y')
                Notification.objects.create(
                    user=lead.assigned_to,
                    title="Doctor Changed Slot - Please Confirm by Patient",
                    message=f"Dr. {doctor.get_full_name() or doctor.username} assigned a new slot for {lead.name}: {date_display} at {time_display}. Remark: '{remark}'. Please call patient to confirm.",
                    link=f"/leads/{lead.pk}/",
                )
            
            messages.success(request, f"Appointment slot updated for {lead.name}. Telecaller has been notified to call patient and confirm.")

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
            apt.status = AppointmentStatus.COMPLETED
            apt.doctor_notes = request.POST.get('doctor_notes', '')
            apt.save(update_fields=['status', 'doctor_notes'])
            
            # Sync Lead custom_data status to Completed as well
            cd = lead.custom_data or {}
            cd['appointment_status'] = 'Completed'
            lead.custom_data = cd
            lead.save(update_fields=['custom_data'])

            # Send Notification to Telecaller (Lead Attendant) to enter billing & UHID details
            if lead.assigned_to:
                Notification.objects.create(
                    user=lead.assigned_to,
                    title="Appointment Completed - Enter Billing Details",
                    message=f"Dr. {doctor.get_full_name() or doctor.username} completed the appointment for patient {lead.name}. Please enter UHID & Billing details in your Billing Follow-ups list.",
                    link=f"/leads/{lead.pk}/edit/",
                )
            
            messages.success(request, f"Appointment for {lead.name} marked completed. Notification sent to Telecaller for billing follow-up.")

        return redirect("dashboard:doctor_home")
        
    # Doctor's appointments
    doctor_apts = Appointment.objects.filter(
        hospital=doctor.hospital
    ).filter(
        Q(doctor_user=doctor) | 
        Q(doctor_name__icontains=doctor.get_full_name() or doctor.username)
    ).select_related('lead').order_by('-appointment_date', 'appointment_time')
    
    today_apts = doctor_apts.filter(appointment_date=today).exclude(
        status__in=[AppointmentStatus.CANCELLED, AppointmentStatus.COMPLETED, AppointmentStatus.PENDING_APPROVAL]
    )
    pending_apts = doctor_apts.filter(status=AppointmentStatus.PENDING_APPROVAL)
    upcoming_apts = doctor_apts.filter(appointment_date__gt=today).exclude(
        status__in=[AppointmentStatus.CANCELLED, AppointmentStatus.COMPLETED, AppointmentStatus.PENDING_APPROVAL]
    )
    completed_today_apts = doctor_apts.filter(appointment_date=today, status=AppointmentStatus.COMPLETED)
    
    schedule, _ = DoctorSchedule.objects.get_or_create(doctor=doctor, defaults={"hospital": doctor.hospital})
    leaves = DoctorLeave.objects.filter(doctor=doctor, end_date__gte=today).order_by("start_date")
    
    context = {
        'active': 'doctor_home',
        'today': today,
        'today_apts': today_apts,
        'pending_apts': pending_apts,
        'upcoming_apts': upcoming_apts,
        'all_apts': doctor_apts[:50],
        'schedule': schedule,
        'leaves': leaves,
        'total_count': doctor_apts.count(),
        'today_count': today_apts.count(),
        'pending_count': pending_apts.count(),
    }
    return render(request, "dashboard/nel_doctor_home.html", context)


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
            apt.status = AppointmentStatus.COMPLETED
            apt.doctor_notes = request.POST.get('doctor_notes', '')
            apt.save(update_fields=['status', 'doctor_notes'])
            
            cd = lead.custom_data or {}
            cd['appointment_status'] = 'Completed'
            lead.custom_data = cd
            lead.save(update_fields=['custom_data'])

            if lead.assigned_to:
                Notification.objects.create(
                    user=lead.assigned_to,
                    title="Appointment Completed - Enter Billing Details",
                    message=f"Dr. {doctor.get_full_name() or doctor.username} completed the appointment for patient {lead.name}. Please enter UHID & Billing details in your Billing Follow-ups list.",
                    link=f"/leads/{lead.pk}/edit/",
                )
            messages.success(request, f"Appointment for {lead.name} marked completed. Billing Follow-up unlocked.")

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
def telecaller_appointments(request):
    from accounts.models import User
    from leads.models import Appointment
    
    if request.user.role != User.Role.LEAD_ATTENDENT or not request.user.hospital:
        messages.error(request, "Access denied.")
        return redirect("dashboard:home")
        
    # Mark appointment logic
    if request.method == "POST":
        apt_id = request.POST.get('appointment_id')
        action = request.POST.get('action')
        apt = get_object_or_404(Appointment, pk=apt_id, hospital=request.user.hospital)
        if action in ['COMPLETED', 'CANCELLED', 'NO_SHOW', 'APPROVED']:
            apt.status = action
            apt.save(update_fields=['status'])
            messages.success(request, f"Appointment marked as {action.capitalize()}.")
        return redirect('dashboard:telecaller_appointments')

    # Get appointments for this hospital
    appointments = Appointment.objects.filter(hospital=request.user.hospital).select_related('lead').order_by('-appointment_date', '-appointment_time')
    
    context = {
        'appointments': appointments,
        'active': 'apt_management',
    }
    return render(request, "dashboard/telecaller_appointments.html", context)

@login_required
def telecaller_my_leads(request):
    from accounts.models import User
    from leads.models import Lead, MasterGroup, HospitalDepartment, HospitalDoctor
    from django.db.models import Q
    from django.core.paginator import Paginator
    from datetime import datetime
    
    if request.user.role != User.Role.LEAD_ATTENDENT or not request.user.hospital:
        messages.error(request, "Access denied.")
        return redirect("dashboard:home")
        
    # Get leads strictly assigned to the current user, ordered by most recently updated
    leads = Lead.objects.filter(assigned_to=request.user).order_by('-updated_at')
    
    # Search logic
    q = request.GET.get('q', '').strip()
    if q:
        leads = leads.filter(Q(name__icontains=q) | Q(mobile__icontains=q) | Q(lead_code__icontains=q))

    # Multi-select & single-value filter parameters
    selected_campaigns = request.GET.getlist("campaign")
    selected_sources = request.GET.getlist("lead_source")
    selected_departments = request.GET.getlist("department")
    selected_doctors = request.GET.getlist("doctor")
    selected_deal_statuses = request.GET.getlist("deal_status") or request.GET.getlist("status")
    selected_stages = request.GET.getlist("stage")
    selected_assigned = request.GET.getlist("assigned_to")
    selected_appointment_statuses = request.GET.getlist("appointment_status")
    selected_priorities = request.GET.getlist("priority")
    selected_temperatures = request.GET.getlist("temperature")
    selected_locations = request.GET.getlist("location")

    def _parse_date(val):
        if not val:
            return None
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(val.strip(), fmt).date()
            except ValueError:
                continue
        return None

    date_from = _parse_date(request.GET.get("date_from") or request.GET.get("date"))
    date_to = _parse_date(request.GET.get("date_to"))
    if date_from:
        leads = leads.filter(inquiry_date__gte=date_from)
    if date_to:
        leads = leads.filter(inquiry_date__lte=date_to)

    if selected_campaigns:
        camp_q = Q()
        for c_val in selected_campaigns:
            if c_val:
                camp_q |= Q(custom_data__campaign__iexact=c_val) | Q(campaign__name__iexact=c_val)
                if c_val.isdigit():
                    camp_q |= Q(campaign_id=int(c_val))
        leads = leads.filter(camp_q)

    if selected_sources:
        src_q = Q()
        for s_val in selected_sources:
            if s_val:
                src_q |= Q(custom_data__lead_source__iexact=s_val) | Q(lead_source__name__iexact=s_val)
                if s_val.isdigit():
                    src_q |= Q(lead_source_id=int(s_val))
        leads = leads.filter(src_q)

    if selected_departments:
        dept_q = Q()
        for d_val in selected_departments:
            if d_val:
                dept_q |= Q(custom_data__department__icontains=d_val) | Q(custom_data__disease__icontains=d_val)
        leads = leads.filter(dept_q)

    if selected_doctors:
        doc_q = Q()
        for doc_val in selected_doctors:
            if doc_val:
                doc_q |= Q(custom_data__doctor__icontains=doc_val)
        leads = leads.filter(doc_q)

    if selected_deal_statuses:
        st_q = Q()
        for ds_val in selected_deal_statuses:
            if not ds_val:
                continue
            v_up = ds_val.strip().upper()
            if 'PAYMENT DONE' in v_up or v_up in ('WON', 'ADMISSION DONE', 'ADMISSION'):
                sub_q = Q(deal_status='WON') | Q(custom_data__total_paid__gt='0') | Q(custom_data__total__gt='0') | Q(custom_data__deal_status__icontains='Payment Done') | Q(custom_data__deal_status__icontains='Won')
            elif any(k in v_up for k in ('BOOKING CONFIRMED', 'BOOKING APPROVAL', 'AWAITING APPROVAL', 'BOOKED')):
                sub_q = Q(custom_data__appointment_status__icontains='Book') | Q(custom_data__appointment_status__icontains='Confirm') | Q(custom_data__appointment_status__icontains='Approv') | Q(custom_data__appointment_status__icontains='Await') | Q(custom_data__appointment_status__iexact='YES') | Q(custom_data__appo_booked_date__isnull=False)
            elif 'PAYMENT PENDING' in v_up or 'BILLING PENDING' in v_up:
                sub_q = Q(custom_data__appointment_status__icontains='Complet') | Q(custom_data__appointment_status__icontains='Done') | Q(custom_data__appointment_status__icontains='Visit')
            elif 'FOLLOW' in v_up:
                sub_q = Q(custom_data__appointment_status__icontains='Follow') | Q(next_followup_date__isnull=False) | Q(custom_data__deal_status__icontains='Follow')
            elif 'NOT INT' in v_up or 'NOT INTERESTED' in v_up:
                sub_q = Q(custom_data__appointment_status__icontains='Not Int') | Q(custom_data__deal_status__icontains='Not Int')
            elif 'CANCEL' in v_up:
                sub_q = Q(custom_data__appointment_status__icontains='Cancel') | Q(custom_data__deal_status__icontains='Cancel')
            elif v_up == 'LOST':
                sub_q = Q(deal_status='LOST') | Q(custom_data__deal_status__icontains='Lost')
            elif 'ASSIGNED' in v_up:
                sub_q = Q(assigned_to__isnull=False) | Q(custom_data__deal_status__iexact='Assigned')
            else:
                sub_q = Q(deal_status__iexact=ds_val) | Q(custom_data__deal_status__iexact=ds_val) | Q(stage__name__iexact=ds_val)
            st_q |= sub_q
        leads = leads.filter(st_q)

    if selected_stages:
        stg_q = Q()
        for stg_val in selected_stages:
            if stg_val:
                if stg_val.isdigit():
                    stg_q |= Q(stage_id=int(stg_val))
                else:
                    stg_q |= Q(stage__name__iexact=stg_val)
        leads = leads.filter(stg_q)

    if selected_appointment_statuses:
        apt_q = Q()
        for apt_val in selected_appointment_statuses:
            if apt_val:
                apt_q |= Q(custom_data__appointment_status__icontains=apt_val)
        leads = leads.filter(apt_q)

    if selected_priorities or selected_temperatures:
        prio_q = Q()
        for p_val in (selected_priorities + selected_temperatures):
            if p_val:
                prio_q |= Q(custom_data__priority__iexact=p_val) | Q(temperature__iexact=p_val)
        leads = leads.filter(prio_q)

    if selected_locations:
        loc_q = Q()
        for loc_val in selected_locations:
            if loc_val:
                loc_q |= Q(location__iexact=loc_val) | Q(city__iexact=loc_val) | Q(custom_data__location__iexact=loc_val)
        leads = leads.filter(loc_q)

    # Dynamic Sorting Logic
    sort_by = request.GET.get("sort", "-created_at")
    sort_mapping = {
        "-created_at": "-created_at",
        "created_at": "created_at",
        "-updated_at": "-updated_at",
        "updated_at": "updated_at",
        "name_asc": "name",
        "name_desc": "-name",
        "-inquiry_date": "-inquiry_date",
        "inquiry_date": "inquiry_date",
    }
    order_field = sort_mapping.get(sort_by, "-created_at")
    leads = leads.order_by(order_field)

    # Handle Export (Excel & PDF)
    export_format = request.GET.get('export', '').lower()
    if export_format in ('1', 'excel', 'xlsx', 'csv'):
        import pandas as pd
        rows = []
        for lead in leads:
            cd = lead.custom_data or {}
            rows.append({
                "Lead Code": lead.lead_code,
                "Patient Name": lead.name,
                "Mobile": lead.mobile,
                "Doctor": cd.get('doctor', ''),
                "Department": cd.get('department', '') or cd.get('disease', ''),
                "Priority": cd.get('priority', '') or lead.get_temperature_display(),
                "Lead Status": cd.get('deal_status', '') or lead.get_deal_status_display(),
                "Appointment Status": cd.get('appointment_status', ''),
                "Inquiry Date": str(lead.inquiry_date) if lead.inquiry_date else '',
                "Location": lead.location or lead.city or '',
            })
        df = pd.DataFrame(rows)
        response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        response['Content-Disposition'] = f'attachment; filename="my_leads_export_{timezone.now().strftime("%Y%m%d_%H%M")}.xlsx"'
        df.to_excel(response, index=False, sheet_name="My Leads")
        return response
    elif export_format == "pdf":
        return render(request, "leads/leads_print_pdf.html", {
            "leads": leads[:500],
            "total_count": leads.count(),
            "now": timezone.now(),
            "active_filters_count": len(selected_campaigns) + len(selected_sources) + (1 if (date_from or date_to) else 0),
        })

    paginator = Paginator(leads, 25)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1) if hasattr(paginator, 'get_elided_page_range') else paginator.page_range
    
    query_params = request.GET.copy()
    if 'page' in query_params:
        del query_params['page']

    # Filter choices for Nelson Hospital
    filter_departments = list(HospitalDepartment.objects.filter(hospital=request.user.hospital, is_active=True).values_list("name", flat=True))
    filter_doctors = list(HospitalDoctor.objects.filter(hospital=request.user.hospital, is_active=True).values_list("name", flat=True))
    if not filter_departments:
        filter_departments = list(MasterGroup.get_active_choices("Departments").filter(hospital=request.user.hospital).values_list("name", flat=True))
    if not filter_doctors:
        filter_doctors = list(MasterGroup.get_active_choices("Doctors").filter(hospital=request.user.hospital).values_list("name", flat=True))
    if not filter_departments:
        filter_departments = ["Gynaecology", "Paediatrics", "NICU / PICU", "Obstetrics", "General OPD"]

    hospital_campaigns = MasterGroup.get_active_choices("Campaigns").filter(hospital=request.user.hospital)
    hospital_sources = MasterGroup.get_active_choices("Lead Sources").filter(hospital=request.user.hospital)
    hospital_statuses = MasterGroup.get_active_choices("Deal Statuses").filter(hospital=request.user.hospital)

    filter_appointment_statuses = ["Booked", "Booking Done", "Pending Confirmation", "Awaiting Doctor Approval", "Visited / OPD Done", "Cancelled", "Not Interested", "Payment Done"]
    filter_priorities = ["Hot", "Warm", "Cold"]
    filter_locations = sorted(list(set(Lead.objects.filter(hospital=request.user.hospital).exclude(location="").values_list("location", flat=True))))

    active_filters_count = (
        len(selected_campaigns) + len(selected_sources) + len(selected_departments) +
        len(selected_doctors) + len(selected_deal_statuses) + len(selected_stages) +
        len(selected_assigned) + len(selected_appointment_statuses) +
        len(selected_priorities) + len(selected_temperatures) +
        len(selected_locations) + (1 if (date_from or date_to) else 0)
    )

    context = {
        'page_obj': page_obj,
        'leads': page_obj,
        'page_range': page_range,
        'q': q,
        'query_params': query_params.urlencode(),
        'total_count': paginator.count,
        'hospital_campaigns': hospital_campaigns,
        'hospital_sources': hospital_sources,
        'hospital_statuses': hospital_statuses,
        'filter_departments': filter_departments,
        'filter_doctors': filter_doctors,
        'filter_appointment_statuses': filter_appointment_statuses,
        'filter_priorities': filter_priorities,
        'filter_locations': filter_locations,
        # Selected filter values — required by leads_side_filter.html for checked state
        'selected_campaigns': selected_campaigns,
        'selected_sources': selected_sources,
        'selected_departments': selected_departments,
        'selected_doctors': selected_doctors,
        'selected_deal_statuses': selected_deal_statuses,
        'selected_stages': selected_stages,
        'selected_assigned': selected_assigned,
        'selected_appointment_statuses': selected_appointment_statuses,
        'selected_priorities': selected_priorities,
        'selected_temperatures': selected_temperatures,
        'selected_locations': selected_locations,
        'date_from_val': request.GET.get('date_from', '') or request.GET.get('date', ''),
        'date_to_val': request.GET.get('date_to', ''),
        'current_sort': sort_by,
        'active_filters_count': active_filters_count,
        'request_get': request.GET,
        'active': 'my_leads',
    }
    return render(request, "dashboard/telecaller_my_leads.html", context)

from accounts.models import HospitalRolePermission
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

@login_required
def roles_permissions_view(request):
    if not (request.user.role == 'SUPER_ADMIN' and request.user.can_manage_users):
        raise PermissionDenied("You do not have permission to manage roles and permissions.")
        
    hospital = request.user.hospital
    if not hospital:
        messages.error(request, "No hospital context found.")
        return redirect("dashboard:home")

    available_permissions = [
        {"key": "view_admin_dashboard", "label": "View Admin Dashboard", "type": "data"},
        {"key": "view_reports", "label": "View Team Reports & EOD Reports", "type": "data"},
        {"key": "manage_campaigns", "label": "Manage Campaigns & Meta Ads", "type": "action"},
        {"key": "view_financials", "label": "View Financial Overview", "type": "data"},
        {"key": "manage_hospital_profile", "label": "Manage Hospital Profile", "type": "action"},
        {"key": "view_all_leads", "label": "View All Hospital Leads", "type": "data"},
        {"key": "view_team_leads", "label": "View Team Leads", "type": "data"},
        {"key": "view_assigned_leads", "label": "View Only Own/Assigned Leads", "type": "data"},
        {"key": "add_leads", "label": "Add New Leads", "type": "action"},
        {"key": "edit_any_lead", "label": "Edit Any Lead", "type": "action"},
        {"key": "edit_own_leads", "label": "Edit Own/Assigned Leads", "type": "action"},
        {"key": "delete_leads", "label": "Delete Leads", "type": "action"},
        {"key": "assign_leads", "label": "Assign/Transfer Leads", "type": "action"},
        {"key": "import_export", "label": "Import / Export Data", "type": "action"},
        {"key": "manage_users", "label": "Manage Staff & Users", "type": "action"},
        {"key": "manage_masters", "label": "Manage Masters", "type": "action"},
    ]

    # Pre-fetch all role configurations for this hospital
    role_permissions = {
        rp.role: rp.permissions
        for rp in HospitalRolePermission.objects.filter(hospital=hospital)
    }
    
    users = User.objects.filter(hospital=hospital).exclude(id=request.user.id).order_by("first_name", "last_name")

    if request.method == "POST":
        action = request.POST.get("action")
        
        if action == "save_role_permissions":
            role_key = request.POST.get("role")
            if role_key in [r[0] for r in User.Role.choices]:
                # Extract boolean perms from POST
                perms = {}
                for p in available_permissions:
                    # If checkbox is checked, it will be in POST
                    perms[p["key"]] = request.POST.get(f"perm_{p['key']}") == "on"
                
                rp, created = HospitalRolePermission.objects.get_or_create(
                    hospital=hospital, role=role_key,
                    defaults={"permissions": perms}
                )
                if not created:
                    rp.permissions = perms
                    rp.save()
                    
                messages.success(request, f"Permissions updated successfully for {role_key} role.")
            else:
                messages.error(request, "Invalid role selected.")
                
        elif action == "save_user_permissions":
            user_id = request.POST.get("user_id")
            target_user = get_object_or_404(User, id=user_id, hospital=hospital)
            
            perms = {}
            # We want to clear the dict if 'reset' is checked
            if request.POST.get("reset_to_default") == "on":
                target_user.custom_permissions = {}
                messages.success(request, f"Permissions reset to default for {target_user.get_full_name()}.")
            else:
                for p in available_permissions:
                    # To store an override, we only store if it differs from default?
                    # Or we store everything if they explicitly hit save. Let's store all explicit overrides.
                    perms[p["key"]] = request.POST.get(f"perm_{p['key']}") == "on"
                target_user.custom_permissions = perms
                messages.success(request, f"Custom permissions saved for {target_user.get_full_name()}.")
            
            target_user.save()
            
        return redirect("dashboard:roles_permissions")

    context = {
        "active": "roles_permissions",
        "roles": [r for r in User.Role.choices if r[0] in ('MANAGER', 'LEAD_ATTENDENT', 'DOCTOR')],
        "available_permissions": available_permissions,
        "role_permissions": role_permissions,
        "role_permissions_json": json.dumps(role_permissions),
        "users": users,
        "users_json": json.dumps({
            u.id: {
                "name": u.get_full_name() or u.username,
                "role": u.role,
                "custom_permissions": u.custom_permissions
            } for u in users
        })
    }
    return render(request, "dashboard/nelson/roles_permissions.html", context)


@login_required
def telecaller_new_enquiries(request):
    from accounts.models import User
    from leads.models import Lead, MasterGroup, HospitalDepartment, HospitalDoctor
    from django.db.models import Q
    from django.core.paginator import Paginator
    from datetime import datetime
    
    if request.user.role != User.Role.LEAD_ATTENDENT or not request.user.hospital:
        messages.error(request, "Access denied.")
        return redirect("dashboard:home")
        
    leads = Lead.objects.filter(
        hospital=request.user.hospital,
        is_archived=False,
        assigned_to__isnull=True,  # STRICTLY UNASSIGNED: Disappears once assigned to anyone
    ).filter(
        Q(temperature='UNCONTACTED') | Q(stage__name__icontains='new') | Q(stage__name__icontains='fresh')
    ).select_related('lead_source', 'assigned_to', 'stage').defer('notes').order_by('-created_at')
    
    q = request.GET.get('q', '').strip()
    if q:
        leads = leads.filter(
            Q(name__icontains=q) | Q(mobile__icontains=q) | 
            Q(city__icontains=q) | Q(email__icontains=q)
        )

    # Multi-select & single-value filter parameters
    selected_campaigns = request.GET.getlist("campaign")
    selected_sources = request.GET.getlist("lead_source")
    selected_departments = request.GET.getlist("department")
    selected_doctors = request.GET.getlist("doctor")
    selected_priorities = request.GET.getlist("priority")
    selected_temperatures = request.GET.getlist("temperature")
    selected_locations = request.GET.getlist("location")

    def _parse_date(val):
        if not val:
            return None
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(val.strip(), fmt).date()
            except ValueError:
                continue
        return None

    date_from = _parse_date(request.GET.get("date_from") or request.GET.get("date"))
    date_to = _parse_date(request.GET.get("date_to"))
    if date_from:
        leads = leads.filter(inquiry_date__gte=date_from)
    if date_to:
        leads = leads.filter(inquiry_date__lte=date_to)

    if selected_campaigns:
        camp_q = Q()
        for c_val in selected_campaigns:
            if c_val:
                camp_q |= Q(custom_data__campaign__iexact=c_val) | Q(campaign__name__iexact=c_val)
                if c_val.isdigit():
                    camp_q |= Q(campaign_id=int(c_val))
        leads = leads.filter(camp_q)

    if selected_sources:
        src_q = Q()
        for s_val in selected_sources:
            if s_val:
                src_q |= Q(custom_data__lead_source__iexact=s_val) | Q(lead_source__name__iexact=s_val)
                if s_val.isdigit():
                    src_q |= Q(lead_source_id=int(s_val))
        leads = leads.filter(src_q)

    if selected_departments:
        dept_q = Q()
        for d_val in selected_departments:
            if d_val:
                dept_q |= Q(custom_data__department__icontains=d_val) | Q(custom_data__disease__icontains=d_val)
        leads = leads.filter(dept_q)

    if selected_doctors:
        doc_q = Q()
        for doc_val in selected_doctors:
            if doc_val:
                doc_q |= Q(custom_data__doctor__icontains=doc_val)
        leads = leads.filter(doc_q)

    if selected_priorities or selected_temperatures:
        prio_q = Q()
        for p_val in (selected_priorities + selected_temperatures):
            if p_val:
                prio_q |= Q(custom_data__priority__iexact=p_val) | Q(temperature__iexact=p_val)
    if selected_locations:
        loc_q = Q()
        for loc_val in selected_locations:
            if loc_val:
                loc_q |= Q(location__iexact=loc_val) | Q(city__iexact=loc_val) | Q(custom_data__location__iexact=loc_val)
        leads = leads.filter(loc_q)

    # Dynamic Sorting Logic
    sort_by = request.GET.get("sort", "-created_at")
    sort_mapping = {
        "-created_at": "-created_at",
        "created_at": "created_at",
        "-updated_at": "-updated_at",
        "updated_at": "updated_at",
        "name_asc": "name",
        "name_desc": "-name",
        "-inquiry_date": "-inquiry_date",
        "inquiry_date": "inquiry_date",
    }
    order_field = sort_mapping.get(sort_by, "-created_at")
    leads = leads.order_by(order_field)

    # Calculate active filters count
    active_filters_count = (
        len(selected_campaigns) + len(selected_sources) + len(selected_departments) +
        len(selected_doctors) + len(selected_priorities) +
        len(selected_temperatures) + len(selected_locations) +
        (1 if (date_from or date_to) else 0) + (1 if q else 0)
    )

    # Handle Export (Excel & PDF)
    export_format = request.GET.get('export', '').lower()
    if export_format in ('1', 'excel', 'xlsx', 'csv'):
        import pandas as pd
        rows = []
        for lead in leads:
            cd = lead.custom_data or {}
            lead_stat = cd.get('deal_status', '') or (lead.stage.name if lead.stage else '')
            if not lead_stat:
                lead_stat = "New" if (not lead.assigned_to_id and (not lead.temperature or lead.temperature == 'UNCONTACTED')) else lead.get_temperature_display()
            
            rows.append({
                "Lead Code": lead.lead_code,
                "Patient Name": lead.name,
                "Mobile": lead.mobile,
                "Department": cd.get('department', '') or cd.get('disease', '') or 'General OPD',
                "Doctor": cd.get('doctor', '') or '-',
                "Lead Source": cd.get('lead_source', '') or (lead.lead_source.name if lead.lead_source else '-'),
                "Campaign": cd.get('campaign', '') or (lead.campaign.name if lead.campaign else '-'),
                "Lead Status": lead_stat,
                "Appointment Status": cd.get('appointment_status', '') or '-',
                "Inquiry Date": str(lead.inquiry_date) if lead.inquiry_date else '',
                "City / Location": lead.location or lead.city or '',
                "Assigned Staff": lead.assigned_to.get_full_name() if lead.assigned_to else "Unassigned",
            })
        df = pd.DataFrame(rows)
        response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        response['Content-Disposition'] = f'attachment; filename="new_enquiries_export_{timezone.now().strftime("%Y%m%d_%H%M")}.xlsx"'
        df.to_excel(response, index=False, sheet_name="New Enquiries")
        return response
    elif export_format == "pdf":
        return render(request, "leads/leads_print_pdf.html", {
            "leads": leads[:500],
            "total_count": leads.count(),
            "now": timezone.now(),
            "active_filters_count": len(selected_campaigns) + len(selected_sources) + (1 if (date_from or date_to) else 0),
        })

    paginator = Paginator(leads, 24)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1) if hasattr(paginator, 'get_elided_page_range') else paginator.page_range
    
    query_params = request.GET.copy()
    if 'page' in query_params:
        del query_params['page']

    # Filter choices for Nelson Hospital
    filter_departments = list(HospitalDepartment.objects.filter(hospital=request.user.hospital, is_active=True).values_list("name", flat=True))
    filter_doctors = list(HospitalDoctor.objects.filter(hospital=request.user.hospital, is_active=True).values_list("name", flat=True))
    if not filter_departments:
        filter_departments = list(MasterGroup.get_active_choices("Departments").filter(hospital=request.user.hospital).values_list("name", flat=True))
    if not filter_doctors:
        filter_doctors = list(MasterGroup.get_active_choices("Doctors").filter(hospital=request.user.hospital).values_list("name", flat=True))
    if not filter_departments:
        filter_departments = ["Gynaecology", "Paediatrics", "NICU / PICU", "Obstetrics", "General OPD"]

    hospital_campaigns = MasterGroup.get_active_choices("Campaigns").filter(hospital=request.user.hospital)
    hospital_sources = MasterGroup.get_active_choices("Lead Sources").filter(hospital=request.user.hospital)

    filter_priorities = ["Hot", "Warm", "Cold"]
    filter_locations = sorted(list(set(Lead.objects.filter(hospital=request.user.hospital).exclude(location="").values_list("location", flat=True))))

    active_filters_count = (
        len(selected_campaigns) + len(selected_sources) + len(selected_departments) +
        len(selected_doctors) + len(selected_priorities) + len(selected_temperatures) +
        len(selected_locations) + (1 if (date_from or date_to) else 0)
    )
        
    context = {
        'leads': page_obj,
        'page_obj': page_obj,
        'page_range': page_range,
        'query_params': query_params.urlencode(),
        'total_count': paginator.count,
        'q': q,
        'hospital_campaigns': hospital_campaigns,
        'hospital_sources': hospital_sources,
        'filter_departments': filter_departments,
        'filter_doctors': filter_doctors,
        'filter_priorities': filter_priorities,
        'filter_locations': filter_locations,
        'selected_campaigns': selected_campaigns,
        'selected_sources': selected_sources,
        'selected_departments': selected_departments,
        'selected_doctors': selected_doctors,
        'selected_priorities': selected_priorities,
        'selected_temperatures': selected_temperatures,
        'date_from_val': request.GET.get('date_from', '') or request.GET.get('date', ''),
        'date_to_val': request.GET.get('date_to', ''),
        'active_filters_count': active_filters_count,
        'request_get': request.GET,
        'active': 'new_enquiries',
    }
    return render(request, "dashboard/telecaller_new_enquiries.html", context)


@login_required
def telecaller_today_team_activity(request):
    """
    Lead Management Section for Lead Attendants:
    Shows leads contacted today by other team members/users in the hospital.
    """
    from accounts.models import User
    from leads.models import Lead
    from followups.models import FollowUp
    from django.db.models import Q
    from django.core.paginator import Paginator
    from django.utils import timezone

    if request.user.role != User.Role.LEAD_ATTENDENT or not request.user.hospital:
        messages.error(request, "Access denied.")
        return redirect("dashboard:home")

    today = timezone.localdate()
    
    # Query followups made or created today in the same hospital
    followups = FollowUp.objects.filter(
        lead__hospital=request.user.hospital
    ).filter(
        Q(followup_date=today) | Q(created_at__date=today)
    ).select_related('lead', 'created_by', 'lead__lead_source', 'lead__campaign').order_by('-created_at', '-id')

    # Filter: Other users vs specific user
    user_filter = request.GET.get('user_id', '').strip()
    if user_filter:
        followups = followups.filter(created_by_id=user_filter)
    else:
        # Default: show activities done by other users
        include_me = request.GET.get('include_me', '0')
        if include_me != '1':
            followups = followups.exclude(created_by=request.user)

    # Search logic (patient name, phone, code, comment)
    q = request.GET.get('q', '').strip()
    if q:
        followups = followups.filter(
            Q(lead__name__icontains=q) |
            Q(lead__mobile__icontains=q) |
            Q(lead__lead_code__icontains=q) |
            Q(comment__icontains=q)
        )

    # Filter by Follow-up Mode / Outcome
    status_filter = request.GET.get('status', '').strip()
    if status_filter:
        followups = followups.filter(followup_status=status_filter)

    mode_filter = request.GET.get('mode', '').strip()
    if mode_filter:
        if mode_filter == 'CALL':
            followups = followups.filter(followup_mode__in=['CALL', 'CALL_OUTGOING', 'CALL_INCOMING'])
        else:
            followups = followups.filter(followup_mode=mode_filter)

    # Active team members in hospital for filter dropdown
    team_members = User.objects.filter(
        hospital=request.user.hospital,
        is_active=True
    ).exclude(id=request.user.id).order_by('first_name', 'username')

    # Dynamic Page Size
    page_size = request.GET.get('page_size', '20').strip()
    try:
        page_size = int(page_size)
        if page_size not in [10, 20, 50, 100]:
            page_size = 20
    except ValueError:
        page_size = 20

    total_count = followups.count()
    paginator = Paginator(followups, page_size)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1) if hasattr(paginator, 'get_elided_page_range') else paginator.page_range

    query_params = request.GET.copy()
    if 'page' in query_params:
        del query_params['page']

    context = {
        'followups': page_obj,
        'page_obj': page_obj,
        'page_range': page_range,
        'query_params': query_params.urlencode(),
        'page_size': page_size,
        'total_count': total_count,
        'today': today,
        'team_members': team_members,
        'selected_user_id': user_filter,
        'q': q,
        'status_filter': status_filter,
        'mode_filter': mode_filter,
        'include_me': request.GET.get('include_me', '0'),
        'active': 'team_today_activity',
    }
    return render(request, "dashboard/telecaller_today_team_activity.html", context)


from .models import TaskReminder
from leads.models import Lead

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
        
    hospital = user.hospital
    selected_hospital_id = (
        request.GET.get("business", "").strip()
        or request.GET.get("hospital", "").strip()
        or str(request.session.get("active_business_id", "")).strip()
    )
    
    # 1. Fetch Task Reports submitted to Admin
    task_reports_qs = TaskReminder.objects.filter(is_reported_to_admin=True)
    if hospital:
        task_reports_qs = task_reports_qs.filter(user__hospital=hospital)
    elif selected_hospital_id and selected_hospital_id.isdigit():
        task_reports_qs = task_reports_qs.filter(user__hospital_id=int(selected_hospital_id))
    if user.role == User.Role.MANAGER and not user.is_superuser:
        task_reports_qs = task_reports_qs.filter(Q(user__reports_to=user) | Q(user=user))
        
    # Search / User filter for tasks
    task_user_filter = request.GET.get('user', '').strip()
    if task_user_filter:
        task_reports_qs = task_reports_qs.filter(user__username=task_user_filter)
        
    date_filter = request.GET.get('date', '').strip()
    if date_filter:
        task_reports_qs = task_reports_qs.filter(reported_at__date=date_filter)
        
    task_reports = task_reports_qs.select_related('user', 'lead').order_by('-reported_at')
    
    # 2. Daily Calling & EOD Reports submitted by Employees / Attendants
    daily_reports_qs = DailyReport.objects.all()
    if hospital:
        daily_reports_qs = daily_reports_qs.filter(user__hospital=hospital)
    elif selected_hospital_id and selected_hospital_id.isdigit():
        daily_reports_qs = daily_reports_qs.filter(user__hospital_id=int(selected_hospital_id))
    if user.role == User.Role.MANAGER and not user.is_superuser:
        daily_reports_qs = daily_reports_qs.filter(Q(user__reports_to=user) | Q(user=user))
    if task_user_filter:
        daily_reports_qs = daily_reports_qs.filter(user__username=task_user_filter)
    if date_filter:
        daily_reports_qs = daily_reports_qs.filter(report_date=date_filter)
    daily_reports = daily_reports_qs.select_related('user', 'user__reports_to').order_by('-report_date', '-created_at')
    
    # Stats
    total_task_reports = task_reports_qs.count()
    total_daily_reports = daily_reports_qs.count()
    
    # Telecallers / Employees for filter dropdown
    employees = User.objects.filter(is_active=True)
    if hospital:
        employees = employees.filter(hospital=hospital)
    elif selected_hospital_id and selected_hospital_id.isdigit():
        employees = employees.filter(hospital_id=int(selected_hospital_id))
    if user.role == User.Role.MANAGER and not user.is_superuser:
        employees = employees.filter(Q(reports_to=user) | Q(pk=user.pk))
        
    # 3. Live Daily Attendance & Login/Logout Activity for Today (All Staff)
    today = timezone.localdate()
    from datetime import datetime, time
    start_today = timezone.make_aware(datetime.combine(today, time.min))
    end_today = timezone.make_aware(datetime.combine(today, time.max))
    from audit.models import AuditLog
    
    staff_attendance = []
    today_logged_in_count = 0
    
    for emp in employees:
        emp_logs = AuditLog.objects.filter(user=emp, created_at__range=(start_today, end_today)).order_by('created_at')
        first_login_log = emp_logs.filter(action='USER_LOGIN').first()
        last_login_log = emp_logs.filter(action='USER_LOGIN').last()
        last_logout_log = emp_logs.filter(action='USER_LOGOUT').last()
        
        # Calculate first login time
        first_login = None
        if first_login_log:
            first_login = first_login_log.created_at
        elif emp.last_login and start_today <= emp.last_login <= end_today:
            first_login = emp.last_login
        elif emp_logs.exists():
            first_login = emp_logs.first().created_at
            
        last_logout = last_logout_log.created_at if last_logout_log else None
        is_logged_in_today = bool(first_login)
        if is_logged_in_today:
            today_logged_in_count += 1
            
        # Determine accurate live session status
        if last_logout and (not last_login_log or last_logout >= last_login_log.created_at):
            session_status = 'Logged Out'
        elif is_logged_in_today:
            session_status = 'Active / In Session'
        else:
            session_status = 'Not Logged In Today'

        # Check if EOD report submitted today
        has_eod = DailyReport.objects.filter(user=emp, report_date=today).first()
        
        # Activity summary
        leads_assigned_today = Lead.objects.filter(assigned_to=emp, inquiry_date=today).count()
        
        staff_attendance.append({
            'user': emp,
            'is_logged_in': is_logged_in_today,
            'first_login': first_login,
            'last_logout': last_logout,
            'eod_report': has_eod,
            'leads_assigned_today': leads_assigned_today,
            'session_status': session_status,
        })
        
    # Sort staff attendance: logged in first, then by role
    staff_attendance.sort(key=lambda x: (not x['is_logged_in'], x['user'].role, x['user'].username))

    # Pagination for Daily Reports
    paginator = Paginator(daily_reports, 15)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1) if hasattr(paginator, 'get_elided_page_range') else paginator.page_range
    
    query_params = request.GET.copy()
    if 'page' in query_params:
        del query_params['page']

    context = {
        'active': 'reports',
        'task_reports': task_reports[:10],
        'daily_reports': page_obj,
        'page_obj': page_obj,
        'page_range': page_range,
        'query_params': query_params.urlencode(),
        'total_task_reports': total_task_reports,
        'total_daily_reports': total_daily_reports,
        'employees': employees,
        'staff_attendance': staff_attendance,
        'today_logged_in_count': today_logged_in_count,
        'total_staff_count': employees.count(),
        'today_date': today,
        'selected_user': task_user_filter,
        'selected_date': date_filter,
    }
    return render(request, "dashboard/admin_reports.html", context)
