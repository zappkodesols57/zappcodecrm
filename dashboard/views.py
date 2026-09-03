from django.core.paginator import Paginator
import json
from datetime import datetime, date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum, Q
from django.db.models.functions import TruncMonth
from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone

from leads.models import Lead, LeadSource, SourceCategory, Course, Campaign, LeadStage, Appointment, AppointmentStatus
from admissions.models import Admission
from payments.models import Payment, PaymentStatus
from accounts.models import User
from dashboard.models import DailyReport, TaskReminder
from notifications.models import Notification
from imports.models import ImportJob


@login_required
def home(request):
    from accounts.models import User
    # If user belongs to a specific hospital role, send them directly to their dedicated dashboard
    if request.user.hospital:
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

    if not request.user.can_view_all_leads:
        if request.user.can_view_team_leads:
            # View leads assigned to team members reporting to this user
            team = User.objects.filter(reports_to=request.user)
            leads = leads.filter(Q(assigned_to=request.user) | Q(assigned_to__in=team))
        elif request.user.can_view_assigned_leads:
            leads = leads.filter(assigned_to=request.user)
        else:
            # Can't view any leads
            leads = leads.none()

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
    total_leads = leads.count()
    
    # 1. Uncontacted: Today's new leads jo abhi tak contact/edit nahi hui (created today & uncontacted/no followups)
    uncontacted_today = leads.filter(
        Q(created_at__date=today) | Q(inquiry_date=today),
        temperature="UNCONTACTED",
        followup_count=0
    ).count()

    # 2. Contacted: Aaj ki contacted ya edited leads count
    from followups.models import FollowUp
    contacted_lead_ids = set(FollowUp.objects.filter(lead__in=leads, created_at__date=today).values_list("lead_id", flat=True))
    # also include leads edited/updated today that are not uncontacted
    edited_today_ids = set(leads.filter(updated_at__date=today).exclude(temperature="UNCONTACTED").values_list("id", flat=True))
    contacted_today = len(contacted_lead_ids.union(edited_today_ids))

    # 3. Booked: Aaj ki booked leads
    booked_today = leads.filter(
        Q(deal_status="WON") | 
        Q(admission_status="ADMISSION_DONE") |
        Q(admission__admission_date=today) |
        Q(custom_data__appo_booked_date=str(today)) |
        Q(custom_data__appointment_status__icontains="Booked")
    ).filter(
        Q(updated_at__date=today) | Q(created_at__date=today) | Q(admission__created_at__date=today)
    ).distinct().count()

    # 4. Overdue: Vo leads jinka followup time aaj ya aaj se pehle tha (<= today) and pending
    overdue_leads_count = leads.filter(
        next_followup_date__lte=today
    ).count()

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

    # 7. Dropdowns for filters
    if request.user.role in ('COUNSELLOR', 'HR'):
        active_leads_all = Lead.objects.filter(is_archived=False, assigned_to=request.user)
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
            "uncontacted": uncontacted_today,
            "contacted_today": contacted_today,
            "booked_today": booked_today,
            "overdue": overdue_leads_count,
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
        raise PermissionDenied("This dashboard is restricted to Business Admins & Managers.")

    today = timezone.localdate()
    user = request.user

    if user.hospital:
        base_leads = Lead.objects.filter(is_archived=False, hospital=user.hospital)
    else:
        base_leads = Lead.objects.filter(is_archived=False)

    # 1. Master lists for Filter Dropdowns (from single-pass over base_leads)
    raw_campaign_set = set()
    raw_source_set = set()
    raw_dept_set = set()
    raw_doc_set = set()
    raw_loc_set = set()
    db_years_set = set()

    for row in base_leads.values('location', 'campaign__name', 'lead_source__name', 'custom_data', 'inquiry_date', 'created_at'):
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

    # 3. Apply Filters (Default view is 'all_time' if specific dropdown slicers or dates are used, or 'today' if clean view)
    has_specific_dropdown = any([custom_start, custom_end, year_filter, month_filter, weekday_filter, campaign_filter, source_filter, department_filter, doctor_filter, location_filter, gender_filter, age_group_filter, payment_type_filter])
    
    if not time_filter:
        if has_specific_dropdown:
            time_filter = 'all_time'
        else:
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
        # No date boundary filter on base_leads -> displays all 1,298+ lifetime records
        filter_label = "All Time"
        period_title_prefix = "All Time"
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

    # Custom Date Range (Apply whenever start_date or end_date are provided)
    if custom_start or custom_end:
        filter_label = "Custom Date Range"
        period_title_prefix = "Period"
        if custom_start:
            base_leads = base_leads.filter(Q(inquiry_date__gte=custom_start) | (Q(inquiry_date__isnull=True) & Q(created_at__date__gte=custom_start)))
        if custom_end:
            base_leads = base_leads.filter(Q(inquiry_date__lte=custom_end) | (Q(inquiry_date__isnull=True) & Q(created_at__date__lte=custom_end)))
        if custom_start and custom_end:
            filter_label = f"{custom_start} to {custom_end}"

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

    # 4. Aggregations & Analytical Calculations for Nelson Hospital
    from collections import defaultdict

    # Base queryset for hospital tenant (inherits all applied filters: campaign, source, department, doctor, location, date, etc.)
    hospital_all_leads = base_leads

    # =========================================================================
    # CARD 1: TODAY'S / PERIOD NEW LEADS (Excel Imported + Organic + Direct Walk-in)
    # =========================================================================
    if time_filter == 'this_month':
        period_new_leads_qs = hospital_all_leads.filter(
            Q(created_at__range=(start_of_month, end_of_month)) |
            Q(inquiry_date__range=(start_date_month, end_date_month))
        ).distinct()
    elif time_filter == 'all_time':
        period_new_leads_qs = hospital_all_leads.distinct()
    else:
        period_new_leads_qs = hospital_all_leads.filter(
            Q(created_at__range=(start_of_today, end_of_today)) |
            Q(inquiry_date=today)
        ).distinct()

    todays_new_leads_count = period_new_leads_qs.count()

    # Explicit sub-counts for Today's / Period New Leads: Campaign, Organic, Walk-in
    todays_campaign_leads_count = 0
    todays_organic_leads_count = 0
    todays_walkin_leads_count = 0

    for l in period_new_leads_qs.select_related('lead_source', 'campaign', 'import_job'):
        src_name = (l.lead_source.name if l.lead_source else '') or (l.custom_data.get('lead_source') if l.custom_data else '') or ''
        src_name_l = src_name.lower()

        if 'walk-in' in src_name_l or 'direct' in src_name_l:
            todays_walkin_leads_count += 1
        elif 'organic' in src_name_l or 'website' in src_name_l or 'google' in src_name_l:
            todays_organic_leads_count += 1
        else:
            # Campaign, Instagram, Facebook, Meta, Excel import, etc.
            todays_campaign_leads_count += 1

    # Breakdown by Source Category: Excel Imported, Organic, Walk-in, Form Created
    card1_breakdown = defaultdict(lambda: {
        'total': 0, 'contacted': 0, 'not_contacted': 0, 'appointment_booked': 0, 'campaigns': set()
    })

    for l in period_new_leads_qs.select_related('lead_source', 'campaign', 'import_job'):
        src_name = (l.lead_source.name if l.lead_source else '') or (l.custom_data.get('lead_source') if l.custom_data else '') or ''
        src_name_l = src_name.lower()
        
        if l.import_job or l.import_source_file or 'import' in src_name_l:
            cat_name = "Excel / Ads Imported Leads"
        elif 'walk-in' in src_name_l or 'direct' in src_name_l:
            cat_name = "Walk-in Leads (Form Registered)"
        elif 'organic' in src_name_l or 'website' in src_name_l or 'google' in src_name_l:
            cat_name = "Organic Leads (Inquiries)"
        else:
            cat_name = f"Campaign Leads ({src_name or 'Meta/Social'})"

        card1_breakdown[cat_name]['total'] += 1
        c_name = l.campaign.name if l.campaign else (l.custom_data.get('campaign') if l.custom_data else 'General')
        if c_name:
            card1_breakdown[cat_name]['campaigns'].add(c_name)

        cd = l.custom_data or {}
        has_contact = bool(cd.get('remark_1') or cd.get('lead_calling_time') or (l.temperature in ['WARM', 'COLD'] and l.temperature != 'HOT'))
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
    call_not_done_base = hospital_all_leads.filter(
        deal_status__in=[DealStatus.OPEN, 'New', 'OPEN']
    )

    if time_filter == 'today':
        cnd_raw_qs = call_not_done_base.filter(
            Q(created_at__range=(start_of_today, end_of_today)) | Q(inquiry_date=today)
        ).distinct()
    elif time_filter == 'this_month':
        cnd_raw_qs = call_not_done_base.filter(
            Q(created_at__range=(start_of_month, end_of_month)) |
            Q(inquiry_date__range=(start_date_month, end_date_month))
        ).distinct()
    else: # all_time or other custom
        cnd_raw_qs = call_not_done_base.distinct()

    terminal_statuses = {'booked', 'completed', 'payment done', 'payment pending', 'cancelled', 'visited', 'admission done', 'won', 'lost'}
    cnd_filtered_leads = []
    today_s = today.strftime("%Y-%m-%d")
    today_a = today.strftime("%d-%m-%Y")

    for l in cnd_raw_qs.select_related('campaign', 'assigned_to', 'stage'):
        cd = l.custom_data or {}
        r1 = str(cd.get('remark_1') or '').strip()
        # Call Not Done rule: remark 1 must be empty/unentered
        if r1 and r1.lower() not in ('nan', 'none', '—', '-', ''):
            continue

        raw_apt = str(cd.get('appointment_status') or '').strip().lower()
        raw_ds = str(cd.get('deal_status') or '').strip().lower()
        disp_st = str(l.display_status or '').strip().lower()

        # If lead has any updated or resolved status, exclude from Call Not Done
        if raw_apt in terminal_statuses or raw_ds in terminal_statuses or disp_st in terminal_statuses:
            continue
        if 'book' in raw_apt or 'confirm' in raw_apt or 'won' in raw_apt or 'cancel' in raw_apt or 'lost' in raw_apt:
            continue
        if 'book' in disp_st or 'payment' in disp_st or 'cancel' in disp_st or 'lost' in disp_st:
            continue

        # If call was recorded today, exclude from pending Call Not Done
        if cd.get('calling_date_remark_1') in (today_s, today_a) or \
           cd.get('calling_date_remark_2') in (today_s, today_a) or \
           cd.get('calling_date_remark_3') in (today_s, today_a) or \
           cd.get('last_called_date') in (today_s, today_a):
            continue

        cnd_filtered_leads.append(l)

    call_not_done_count = len(cnd_filtered_leads)

    card2_breakdown = defaultdict(lambda: {'total': 0, 'unassigned': 0, 'hot': 0, 'uncontacted': 0})
    for l in cnd_filtered_leads[:200]:
        c_name = l.campaign.name if l.campaign else (l.custom_data.get('campaign') if l.custom_data else 'General / Direct')
        card2_breakdown[c_name]['total'] += 1
        if not l.assigned_to:
            card2_breakdown[c_name]['unassigned'] += 1
        if l.temperature == 'HOT':
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
    opd_status_q = (
        Q(custom_data__appointment_status__icontains='Book') |
        Q(custom_data__appointment_status__icontains='Confirm') |
        Q(custom_data__appointment_status__icontains='Approv') |
        Q(custom_data__appointment_status__icontains='Complete') |
        Q(custom_data__appointment_status__iexact='YES')
    )
    if time_filter == 'this_month':
        appts_booked_qs = hospital_all_leads.filter(
            opd_status_q
        ).filter(
            Q(created_at__range=(start_of_month, end_of_month)) |
            Q(inquiry_date__range=(start_date_month, end_date_month)) |
            Q(custom_data__appo_booked_date__startswith=today.strftime('%Y-%m'))
        ).distinct()
    elif time_filter == 'all_time':
        appts_booked_qs = hospital_all_leads.filter(
            opd_status_q
        ).distinct()
    else:
        appts_booked_qs = hospital_all_leads.filter(
            opd_status_q
        ).filter(
            Q(created_at__range=(start_of_today, end_of_today)) |
            Q(inquiry_date=today) |
            Q(custom_data__appo_booked_date=today_str) |
            Q(custom_data__appo_booked_date=today_alt_str) |
            Q(custom_data__appointment_date=today_str) |
            Q(custom_data__appointment_date=today_alt_str) |
            Q(custom_data__appointment_confirmed_at__startswith=today_str)
        ).distinct()

    appts_booked_count = appts_booked_qs.count()

    card3_breakdown = defaultdict(lambda: {'total': 0, 'completed': 0, 'scheduled': 0})
    for l in appts_booked_qs:
        cd = l.custom_data or {}
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
        Q(deal_status__in=[DealStatus.WON, DealStatus.LOST])
    )
    if time_filter == 'all_time':
        todays_followups_qs = hospital_all_leads.filter(
            next_followup_date__isnull=False
        ).exclude(booked_exclude_q).distinct()
    elif time_filter == 'this_month':
        todays_followups_qs = hospital_all_leads.filter(
            next_followup_date__range=(start_date_month, end_date_month)
        ).exclude(booked_exclude_q).distinct()
    else:
        todays_followups_qs = hospital_all_leads.filter(
            next_followup_date=today
        ).exclude(booked_exclude_q).distinct()

    todays_followups_count = todays_followups_qs.count()

    card4_breakdown = defaultdict(lambda: {'total': 0, 'pending': 0, 'done': 0})
    for l in todays_followups_qs.select_related('assigned_to'):
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
    ).distinct()

    todays_walkin_count = todays_walkin_qs.count()

    card5_breakdown = defaultdict(lambda: {'total': 0, 'dept': '', 'booked': 0})
    for l in todays_walkin_qs:
        cd = l.custom_data or {}
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

    fast_leads = base_leads.select_related('lead_source', 'campaign', 'assigned_to', 'stage')

    for l in fast_leads:
        cd = l.custom_data or {}

        # 1. Location
        loc = l.location or cd.get('location') or 'Not Mentioned'
        loc = loc.strip().title() if loc else 'Not Mentioned'
        location_dist[loc] = location_dist.get(loc, 0) + 1

        # 2. Month
        lead_date = l.created_at.date() if l.created_at else (l.inquiry_date or today)
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
        camp = (l.campaign.name if l.campaign else '') or cd.get('campaign') or 'Nelson General Campaign'
        camp = camp.strip()
        campaign_dist[camp] = campaign_dist.get(camp, 0) + 1

        # 6. Lead Source
        src = (l.lead_source.name if l.lead_source else '') or cd.get('lead_source') or 'Instagram'
        src = src.strip()
        source_dist[src] = source_dist.get(src, 0) + 1

        # 7. Appointment Status
        appo_st = cd.get('appointment_status') or 'NA'
        appo_st = str(appo_st).strip().upper()
        if not appo_st or appo_st in ['NAN', 'NONE']: appo_st = 'NA'
        appo_status_dist[appo_st] = appo_status_dist.get(appo_st, 0) + 1

        # 8. Final Lead Status / Temperature Chart
        prio_tag = l.custom_priority
        if prio_tag:
            fls = prio_tag.upper()
        else:
            fls = str(l.display_status or 'OPEN').upper()
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
        wd = str(cd.get('week_day') or (l.inquiry_date.strftime('%A') if l.inquiry_date else 'Thursday')).strip().title()
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
        "todays_followups": todays_followups_count,
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
        gender_filter, age_group_filter, payment_type_filter
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
        "time_filter": time_filter,
        "custom_start": custom_start,
        "custom_end": custom_end,
        "has_active_filters": has_active_filters,
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
    from django.db.models import Q
    from leads.models import Lead, DealStatus

    user = request.user
    card_type = request.GET.get('card_type', 'new_leads').strip() # 'new_leads', 'call_not_done', 'opd_booked', 'followups', 'walkin'
    mode = request.GET.get('mode', 'today').strip() # 'today', 'previous', 'next', 'all', 'custom'
    target_date_str = request.GET.get('target_date', '').strip()
    year_param = request.GET.get('year', '')
    month_param = request.GET.get('month', '')

    today = timezone.localdate()

    # Determine reference date
    if target_date_str:
        try:
            current_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
        except ValueError:
            current_date = today
    else:
        current_date = today

    if mode == 'previous':
        selected_date = current_date - timedelta(days=1)
    elif mode == 'next':
        selected_date = current_date + timedelta(days=1)
    elif mode == 'today':
        selected_date = today
    elif mode == 'custom':
        selected_date = current_date
    else: # mode == 'all'
        selected_date = None

    # Base tenant queryset
    hospital_qs = Lead.objects.filter(is_archived=False, hospital=user.hospital) if user.hospital else Lead.objects.filter(is_archived=False)

    start_dt = timezone.make_aware(datetime.combine(selected_date, datetime.min.time())) if selected_date else None
    end_dt = timezone.make_aware(datetime.combine(selected_date, datetime.max.time())) if selected_date else None
    sel_date_str = selected_date.isoformat() if selected_date else ""

    # Filter queryset based on card_type and selected time/mode
    if card_type == 'new_leads':
        if selected_date:
            leads_qs = hospital_qs.filter(
                Q(created_at__range=(start_dt, end_dt)) | Q(inquiry_date=selected_date)
            )
        else:
            leads_qs = hospital_qs

    elif card_type == 'call_not_done':
        # Uncontacted / Open pending calling queue (only leads whose remark 1 is empty, and not booked/called/closed)
        if user.role == User.Role.LEAD_ATTENDENT:
            c_base = hospital_qs.filter(
                Q(assigned_to=user) | Q(assigned_to__isnull=True) | Q(custom_data__lead_attendant__in=['Unassigned', '', None, 'nan'])
            ).filter(deal_status__in=[DealStatus.OPEN, 'New', 'OPEN'])
        else:
            c_base = hospital_qs.filter(deal_status__in=[DealStatus.OPEN, 'New', 'OPEN'])

        if selected_date:
            c_base = c_base.filter(
                Q(created_at__range=(start_dt, end_dt)) | Q(inquiry_date=selected_date)
            )

        # In-memory clean filter for Call Not Done: remark 1 must be empty and no terminal status / calling done
        terminal_statuses = {'booked', 'completed', 'payment done', 'payment pending', 'cancelled', 'visited', 'admission done', 'won', 'lost'}
        cnd_matched_ids = []
        today_s = today.strftime("%Y-%m-%d")
        today_a = today.strftime("%d-%m-%Y")
        
        for l in c_base:
            cd = l.custom_data or {}
            r1 = str(cd.get('remark_1') or '').strip()
            # Call Not Done rule: remark 1 must be empty
            if r1 and r1.lower() not in ('nan', 'none', '—', '-', ''):
                continue

            raw_apt = str(cd.get('appointment_status') or '').strip().lower()
            raw_ds = str(cd.get('deal_status') or '').strip().lower()
            disp_st = str(l.display_status or '').strip().lower()
            
            # If lead has any resolved status, remove from Call Not Done breakdown
            if raw_apt in terminal_statuses or raw_ds in terminal_statuses or disp_st in terminal_statuses:
                continue
            if 'book' in raw_apt or 'confirm' in raw_apt or 'won' in raw_apt or 'cancel' in raw_apt or 'lost' in raw_apt:
                continue
            if 'book' in disp_st or 'payment' in disp_st or 'cancel' in disp_st or 'lost' in disp_st:
                continue
            
            # If call was recorded today, remove from pending Call Not Done
            if cd.get('calling_date_remark_1') in (today_s, today_a) or \
               cd.get('calling_date_remark_2') in (today_s, today_a) or \
               cd.get('calling_date_remark_3') in (today_s, today_a) or \
               cd.get('last_called_date') in (today_s, today_a):
                continue
                
            cnd_matched_ids.append(l.id)

        leads_qs = hospital_qs.filter(id__in=cnd_matched_ids)

    elif card_type == 'opd_booked':
        b_base = hospital_qs.filter(
            Q(custom_data__appointment_status__icontains='Book') |
            Q(custom_data__appointment_status__icontains='Confirm') |
            Q(custom_data__appointment_status__icontains='Approv') |
            Q(custom_data__appointment_status__icontains='Complete') |
            Q(custom_data__appointment_status__iexact='YES')
        )
        if selected_date:
            sel_alt_str = selected_date.strftime("%d-%m-%Y")
            leads_qs = b_base.filter(
                Q(created_at__range=(start_dt, end_dt)) |
                Q(inquiry_date=selected_date) |
                Q(custom_data__appo_booked_date=sel_date_str) |
                Q(custom_data__appo_booked_date=sel_alt_str) |
                Q(custom_data__appointment_date=sel_date_str) |
                Q(custom_data__appointment_date=sel_alt_str) |
                Q(custom_data__appointment_confirmed_at__startswith=sel_date_str)
            )
        else:
            leads_qs = b_base

    elif card_type == 'followups':
        booked_exclude_modal = (
            Q(custom_data__appointment_status__icontains='Book') |
            Q(custom_data__appointment_status__icontains='Confirm') |
            Q(deal_status__in=[DealStatus.WON, DealStatus.LOST])
        )
        if selected_date:
            leads_qs = hospital_qs.filter(next_followup_date=selected_date).exclude(booked_exclude_modal)
        else:
            leads_qs = hospital_qs.filter(next_followup_date__isnull=False).exclude(booked_exclude_modal)

    elif card_type == 'walkin':
        w_base = hospital_qs.filter(
            Q(lead_source__name__icontains='walk') |
            Q(lead_source__name__icontains='hospital') |
            Q(custom_data__lead_source__icontains='walk') |
            Q(custom_data__lead_source__icontains='hospital')
        )
        if selected_date:
            leads_qs = w_base.filter(
                Q(created_at__range=(start_dt, end_dt)) | Q(inquiry_date=selected_date)
            )
        else:
            leads_qs = w_base
    else:
        leads_qs = hospital_qs

    leads_qs = leads_qs.distinct().select_related('assigned_to', 'campaign', 'lead_source')
    total_count = leads_qs.count()

    # Calculate Campaign-wise breakdown
    campaign_counts = defaultdict(int)
    for l in leads_qs:
        c_name = l.campaign.name if l.campaign else (l.custom_data.get('campaign') if l.custom_data else 'General / Direct')
        if not c_name or str(c_name).strip() in ['nan', 'None', '', '—', '-']:
            c_name = 'General / Direct'
        campaign_counts[c_name.strip()] += 1

    campaign_breakdown = [
        {"campaign_name": camp, "count": cnt}
        for camp, cnt in sorted(campaign_counts.items(), key=lambda x: x[1], reverse=True)
    ]

    # Build Lead Items (limit to top 250 for ultra fast responsive modal)
    lead_items = []
    for l in leads_qs[:250]:
        cd = l.custom_data or {}
        doc = cd.get('doctor') or (l.assigned_to.get_full_name() if l.assigned_to else 'Unassigned')
        dept = cd.get('department') or 'General OPD'
        raw_apt = cd.get('appointment_status')
        if raw_apt and str(raw_apt).strip().lower() not in ('nan', 'none', '', '-'):
            appt_st = str(raw_apt).strip()
        else:
            appt_st = l.display_status
        
        c_name = l.campaign.name if l.campaign else (cd.get('campaign') or 'General / Direct')
        if not c_name or str(c_name).strip() in ['nan', 'None', '', '—', '-']:
            c_name = 'General / Direct'

        mob_digits = Lead.clean_mobile(l.mobile)
        is_booked = l.is_booked
        appt_date = str(cd.get("appo_booked_date") or cd.get("appointment_date") or "").strip()
        appt_time = str(cd.get("appointment_time") or "").strip()

        status_str = l.display_status
        temp_str = l.custom_temperature or ""

        lead_items.append({
            "id": l.id,
            "name": l.name or "Anonymous Patient",
            "mobile": l.mobile or "-",
            "clean_mobile": mob_digits or "",
            "email": l.email or "-",
            "created_date": l.created_at.strftime('%d-%m-%Y') if l.created_at else str(l.inquiry_date or '-'),
            "inquiry_date": str(l.inquiry_date or '-'),
            "campaign": c_name.strip(),
            "lead_source": l.lead_source.name if l.lead_source else (cd.get('lead_source') or 'Hospital Form'),
            "status": status_str,
            "is_booked": is_booked,
            "temperature": temp_str,
            "appointment_status": appt_st or status_str,
            "doctor": doc,
            "appointment_date": appt_date,
            "appointment_time": appt_time,
            "whatsapp_message": l.whatsapp_message,
            "department": dept,
            "assigned_to": l.assigned_to.get_full_name() if l.assigned_to else "Unassigned",
            "next_followup": str(l.next_followup_date or '-'),
            "detail_url": f"/leads/{l.id}/",
            "edit_url": f"/leads/{l.id}/edit/",
        })

    # Pre-calculate calendar heatmap matrix
    # Shows count under each day for the target month
    cal_year = int(year_param) if year_param.isdigit() else (selected_date.year if selected_date else today.year)
    cal_month = int(month_param) if month_param.isdigit() else (selected_date.month if selected_date else today.month)
    _, days_in_month = calendar.monthrange(cal_year, cal_month)

    calendar_counts = {}
    for d in range(1, days_in_month + 1):
        day_date = date(cal_year, cal_month, d)
        day_str = day_date.strftime('%Y-%m-%d')
        d_start = timezone.make_aware(datetime.combine(day_date, datetime.min.time()))
        d_end = timezone.make_aware(datetime.combine(day_date, datetime.max.time()))

        if card_type == 'new_leads':
            cnt = hospital_qs.filter(Q(created_at__range=(d_start, d_end)) | Q(inquiry_date=day_date)).count()
        elif card_type == 'call_not_done':
            day_candidates = hospital_qs.filter(
                deal_status__in=[DealStatus.OPEN, 'New', 'OPEN']
            ).filter(
                Q(created_at__range=(d_start, d_end)) | Q(inquiry_date=day_date)
            )
            c_cnt = 0
            for l in day_candidates:
                cd = l.custom_data or {}
                r1 = str(cd.get('remark_1') or '').strip()
                if r1 and r1.lower() not in ('nan', 'none', '—', '-', ''):
                    continue
                raw_apt = str(cd.get('appointment_status') or '').strip().lower()
                raw_ds = str(cd.get('deal_status') or '').strip().lower()
                disp_st = str(l.display_status or '').strip().lower()
                if raw_apt in {'booked', 'completed', 'payment done', 'payment pending', 'cancelled', 'visited', 'admission done', 'won', 'lost'}:
                    continue
                if 'book' in raw_apt or 'confirm' in raw_apt or 'won' in raw_apt or 'cancel' in raw_apt or 'lost' in raw_apt:
                    continue
                if 'book' in disp_st or 'payment' in disp_st or 'cancel' in disp_st or 'lost' in disp_st:
                    continue
                if cd.get('calling_date_remark_1') in (day_str, day_date.strftime("%d-%m-%Y")) or \
                   cd.get('calling_date_remark_2') in (day_str, day_date.strftime("%d-%m-%Y")) or \
                   cd.get('calling_date_remark_3') in (day_str, day_date.strftime("%d-%m-%Y")) or \
                   cd.get('last_called_date') in (day_str, day_date.strftime("%d-%m-%Y")):
                    continue
                c_cnt += 1
            cnt = c_cnt
        elif card_type == 'opd_booked':
            cnt = hospital_qs.filter(
                Q(custom_data__appointment_status__icontains='Book') |
                Q(custom_data__appointment_status__icontains='Complete') |
                Q(custom_data__appointment_status__iexact='YES')
            ).filter(
                Q(created_at__range=(d_start, d_end)) |
                Q(inquiry_date=day_date) |
                Q(custom_data__appo_booked_date=day_str)
            ).count()
        elif card_type == 'followups':
            cnt = hospital_qs.filter(next_followup_date=day_date).count()
        elif card_type == 'walkin':
            cnt = hospital_qs.filter(
                Q(lead_source__name__icontains='walk-in') |
                Q(custom_data__lead_source__icontains='walk-in') |
                Q(custom_data__source__icontains='walk-in')
            ).filter(
                Q(created_at__range=(d_start, d_end)) | Q(inquiry_date=day_date)
            ).count()
        else:
            cnt = 0
        calendar_counts[day_str] = cnt

    hosp_name = user.hospital.name if (hasattr(user, 'hospital') and user.hospital) else "Nelson Mother & Child Care Hospital"
    agent_name = user.get_full_name() or user.username or "Patient Care Team"

    return JsonResponse({
        "status": "success",
        "card_type": card_type,
        "mode": mode,
        "selected_date": selected_date.strftime('%Y-%m-%d') if selected_date else "all",
        "selected_date_display": selected_date.strftime('%d %B %Y') if selected_date else "All Time Records",
        "total_count": total_count,
        "hospital_name": hosp_name,
        "agent_name": agent_name,
        "campaign_breakdown": campaign_breakdown,
        "lead_items": lead_items,
        "calendar_counts": calendar_counts,
        "cal_year": cal_year,
        "cal_month": cal_month,
        "cal_month_name": date(cal_year, cal_month, 1).strftime('%B %Y'),
    })


@login_required
def nelson_module_view(request, module_name):
    from django.core.exceptions import PermissionDenied
    from accounts.models import User
    from django.contrib import messages
    from django.shortcuts import redirect
    import json
    
    if request.user.role not in (User.Role.SUPER_ADMIN, User.Role.MANAGER):
        raise PermissionDenied("Restricted to Admin/Manager.")

    hospital = request.user.hospital

    if module_name == 'hospital-profile':
        if not request.user.can_manage_hospital_profile:
            raise PermissionDenied('Permission denied for hospital profile.')
        if not hospital:
            messages.error(request, "No hospital associated with your account.")
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
        from leads.models import Campaign, Lead, Appointment
        hospital = request.user.hospital
        
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
                if camp.hospital and camp.hospital != hospital:
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
                camp.is_active = not camp.is_active
                camp.save(update_fields=['is_active'])
                messages.success(request, f"Campaign '{camp.name}' status toggled to {'Active' if camp.is_active else 'Inactive'}.")
                return redirect('dashboard:nelson_module', module_name='campaign-management')
                
            elif action == 'delete':
                cid = request.POST.get('campaign_id')
                camp = get_object_or_404(Campaign, pk=cid)
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

        for c in campaigns_qs:
            # All time leads for this campaign
            leads_all_cnt = base_leads_qs.filter(Q(campaign=c) | Q(custom_data__campaign=c.name)).count()
            # Period leads for this campaign
            leads_period_cnt = period_leads_qs.filter(Q(campaign=c) | Q(custom_data__campaign=c.name)).count()
            
            total_leads_count += leads_all_cnt
            total_period_leads += leads_period_cnt
            
            campaigns_data.append({
                "obj": c,
                "leads_count": leads_all_cnt,
                "period_leads_count": leads_period_cnt,
            })
            
        total_appts = Appointment.objects.filter(hospital=hospital).count() if hospital else 0
        
        # Recent Import Jobs in selected period for WhatsApp report
        recent_jobs = period_jobs_qs.filter(imported_count__gt=0).order_by('-created_at')[:15]
        hospital_name = hospital.name if hospital else "Zappcode CRM"

        return render(request, "dashboard/campaign_management.html", {
            "title": "Campaign Management",
            "active": "campaign-management",
            "campaigns_data": campaigns_data,
            "total_campaigns": campaigns_qs.count(),
            "active_campaigns_count": campaigns_qs.filter(is_active=True).count(),
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

        total_leads_overall = leads_qs.count()
        cost_per_lead_overall = (total_campaign_cost / total_leads_overall) if total_leads_overall > 0 else 0.0
        revenue_per_lead_overall = (total_gross_revenue / total_leads_overall) if total_leads_overall > 0 else 0.0

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




@login_required
def source_report(request):
    rows = []
    for src in LeadSource.objects.all():
        leads_qs = Lead.objects.filter(lead_source=src, is_archived=False)
        total = leads_qs.count()
        if total == 0:
            continue
        interested = leads_qs.filter(temperature__in=["HOT", "WARM"]).count()
        visits = leads_qs.filter(stage__name__icontains="visit").count()
        admissions_qs = Admission.objects.filter(lead__lead_source=src)
        admissions = admissions_qs.count()
        revenue = Payment.objects.filter(payment_status=PaymentStatus.SUCCESS, admission__lead__lead_source=src).aggregate(s=Sum("amount"))["s"] or 0
        rows.append({
            "source": src.name, "leads": total, "interested": interested, "visits": visits,
            "admissions": admissions, "conversion": round(admissions / total * 100, 1),
            "revenue": revenue,
        })
    rows.sort(key=lambda r: -r["leads"])
    return render(request, "dashboard/source_report.html", {"active": "reports_source", "rows": rows})


@login_required
def campaign_report(request):
    rows = []
    for camp in Campaign.objects.all():
        leads_qs = Lead.objects.filter(campaign=camp, is_archived=False)
        total = leads_qs.count()
        admissions_qs = Admission.objects.filter(lead__campaign=camp)
        admissions = admissions_qs.count()
        revenue = Payment.objects.filter(payment_status=PaymentStatus.SUCCESS, admission__lead__campaign=camp).aggregate(s=Sum("amount"))["s"] or 0
        cost = float(camp.cost or 0)
        rows.append({
            "campaign": camp.name, "platform": camp.platform, "leads": total, "admissions": admissions,
            "revenue": revenue, "cost": cost,
            "cost_per_lead": round(cost / total, 2) if total else 0,
            "cost_per_admission": round(cost / admissions, 2) if admissions else 0,
            "conversion": round(admissions / total * 100, 1) if total else 0,
        })
    return render(request, "dashboard/campaign_report.html", {"active": "reports_campaign", "rows": rows})


@login_required
def employee_report(request):
    from accounts.models import User
    rows = []
    for emp in User.objects.filter(is_active_employee=True):
        leads_qs = Lead.objects.filter(assigned_to=emp, is_archived=False)
        total = leads_qs.count()
        if total == 0:
            continue
        followups = emp.followup_set.count() if hasattr(emp, "followup_set") else 0
        admissions = Admission.objects.filter(lead__assigned_to=emp).count()
        rows.append({
            "employee": emp.get_full_name() or emp.username, "leads": total,
            "admissions": admissions, "conversion": round(admissions / total * 100, 1),
        })
    rows.sort(key=lambda r: -r["leads"])
    return render(request, "dashboard/employee_report.html", {"active": "reports_employee", "rows": rows})


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
    from datetime import datetime

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
    day_followups = FollowUp.objects.filter(created_by=request.user, followup_date=report_date)
    
    # 1. Calls & Follow-ups
    outgoing_calls_cnt = day_followups.filter(followup_mode="CALL_OUTGOING").count()
    incoming_calls_cnt = day_followups.filter(followup_mode="CALL_INCOMING").count()
    calls_attended_cnt = day_followups.filter(followup_mode__in=["CALL_OUTGOING", "CALL_INCOMING"]).count()
    calls_not_connected_cnt = day_followups.filter(followup_status="NOT_CONNECTED").count()
    follow_ups_taken_cnt = day_followups.count()

    # 2. Leads Assigned to this user today
    leads_assigned_cnt = Lead.objects.filter(assigned_to=request.user, inquiry_date=report_date).count()

    # 3. Appointments Booked / Approved today
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

    # 5. Pending Follow-ups, Interested, Cold, Visited
    follow_ups_pending_cnt = FollowUp.objects.filter(
        lead__assigned_to=request.user,
        followup_date__lte=report_date,
        followup_status__in=["PENDING", "MISSED", "SCHEDULED"]
    ).count()

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

    # 6. Login / Logout times from AuditLog (with smart fallback to activity timestamps)
    from audit.models import AuditLog
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
    
    # 7. Auto-calculate academic metrics: Admissions Done today and Fees Payments Collected today
    from admissions.models import Admission
    from payments.models import Payment, PaymentStatus
    admissions_today_cnt = Admission.objects.filter(lead__assigned_to=request.user, admission_date=report_date).count()
    fees_today_sum = Payment.objects.filter(
        admission__lead__assigned_to=request.user,
        payment_date=report_date,
        payment_status=PaymentStatus.SUCCESS
    ).aggregate(s=Sum("amount"))["s"] or 0

    # Determine who this report will be sent to
    reports_to_user = request.user.reports_to
    admin_qs = User.objects.filter(role__in=['SUPER_ADMIN', 'ADMIN', 'MANAGER'], is_active=True)
    if request.user.hospital:
        admin_qs = admin_qs.filter(hospital=request.user.hospital)
    
    recipients = []
    if reports_to_user and reports_to_user.is_active:
        recipients.append(reports_to_user)
    for adm in admin_qs:
        if adm not in recipients and adm != request.user:
            recipients.append(adm)

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
        "leads_interested": leads_interested_cnt,
        "leads_cold": leads_cold_cnt,
        "leads_visited": leads_visited_cnt,
        "admissions_done": admissions_today_cnt,
        "fees_collected": fees_today_sum,
        "first_login_time": first_login_time,
        "last_logout_time": last_logout_time,
    }

    FormClass = HospitalDailyReportForm if request.user.hospital else AcademyDailyReportForm
    template_name = "dashboard/hospital_daily_report_form.html" if request.user.hospital else "dashboard/academy_reports_form.html"

    if request.method == "POST":
        from django.db import IntegrityError, transaction
        from notifications.models import Notification

        form = FormClass(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    cleaned = form.cleaned_data
                    
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
                        "leads_cold": cleaned.get("leads_cold") if cleaned.get("leads_cold") is not None else leads_cold_cnt,
                        "leads_interested": cleaned.get("leads_interested") if cleaned.get("leads_interested") is not None else leads_interested_cnt,
                        "leads_visited": cleaned.get("leads_visited") if cleaned.get("leads_visited") is not None else leads_visited_cnt,
                        "admissions_done": cleaned.get("admissions_done") if cleaned.get("admissions_done") is not None else admissions_today_cnt,
                        "fees_collected": cleaned.get("fees_collected") if cleaned.get("fees_collected") is not None else fees_today_sum,
                        "key_highlight": cleaned.get("key_highlight") or "",
                        "challenges_faced": cleaned.get("challenges_faced") or "",
                        "tomorrow_priority": cleaned.get("tomorrow_priority") or "",
                        "other_updates": cleaned.get("other_updates") or "",
                        "mood_rating": cleaned.get("mood_rating") or 3,
                        "first_login_at": first_login_time,
                        "last_logout_at": last_logout_time,
                    }
                    
                    report, created = DailyReport.objects.update_or_create(
                        user=request.user,
                        report_date=report_date,
                        defaults=report_data
                    )
                    
                    # Send Notifications to recipient (Reports To / Admin)
                    target_recipients = recipients if recipients else admin_qs
                    action_word = "submitted" if created else "updated"
                    for r_user in target_recipients:
                        Notification.objects.create(
                            user=r_user,
                            title=f"EOD Report ({action_word.capitalize()}) from {request.user.get_full_name() or request.user.username}",
                            message=f"{request.user.get_full_name() or request.user.username} {action_word} Daily EOD Report for {report_date.strftime('%d %b %Y')}. (Assigned: {report.leads_assigned}, Appts: {report.appointments_booked}, Calls: {report.calls_attended})",
                            link="/dashboard/reports/admin/",
                        )

                messages.success(request, f"Daily report for {report_date.strftime('%d-%m-%Y')} {'submitted' if created else 'updated'} successfully! ✅")
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
                "leads_cold": report_instance.leads_cold,
                "leads_interested": report_instance.leads_interested,
                "leads_visited": report_instance.leads_visited,
                "admissions_done": report_instance.admissions_done,
                "fees_collected": report_instance.fees_collected,
                "key_highlight": report_instance.key_highlight,
                "challenges_faced": report_instance.challenges_faced,
                "tomorrow_priority": report_instance.tomorrow_priority,
                "other_updates": report_instance.other_updates,
                "mood_rating": report_instance.mood_rating,
            }
            if request.user.hospital:
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
                "leads_interested": suggestions["leads_interested"],
                "leads_cold": suggestions["leads_cold"],
                "leads_visited": suggestions["leads_visited"],
                "admissions_done": suggestions["admissions_done"],
                "fees_collected": suggestions["fees_collected"],
            }
            if request.user.hospital:
                init_data["appointments_booked"] = suggestions["appointments_booked"]
                init_data["freeze_leads"] = suggestions["freeze_leads"]

        form = FormClass(initial=init_data)

    return render(request, template_name, {
        "active": "daily_report_submit",
        "form": form,
        "suggestions": suggestions,
        "report_date": report_date,
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
    
    if user.hospital:
        reports = reports.filter(user__hospital=user.hospital)
        
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
                "Calls Attended": r.calls_attended,
                "Outgoing Calls": r.outgoing_calls,
                "Incoming Calls": r.incoming_calls,
                "Follow-ups Taken": r.follow_ups_taken,
                "Appointments Booked": r.appointments_booked,
                "Freeze Leads": r.freeze_leads,
                "Interested Leads": r.leads_interested,
                "Key Highlight": r.key_highlight,
                "Challenges Faced": r.challenges_faced,
                "Tomorrow Priority": r.tomorrow_priority,
                "Mood Rating": dict(r.MOOD_CHOICES).get(r.mood_rating, r.mood_rating),
            })
        df = pd.DataFrame(rows)
        response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        response["Content-Disposition"] = f'attachment; filename="daily_reports_{timezone.now().strftime("%Y%m%d_%H%M")}.xlsx"'
        df.to_excel(response, index=False, sheet_name="Daily Reports")
        return response
        
    # Get active/approved employees for filter dropdown
    employees = User.objects.filter(is_active=True, is_approved=True)
    if user.hospital:
        employees = employees.filter(hospital=user.hospital)
    if user.role == User.Role.MANAGER and not user.is_superuser:
        employees = employees.filter(Q(reports_to=user) | Q(pk=user.pk))
    
    return render(request, "dashboard/daily_reports_list.html", {
        "active": "reports_daily",
        "reports": reports,
        "employees": employees,
        "request_get": request.GET,
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
        ).filter(deal_status__in=[DealStatus.OPEN, 'New', 'OPEN'])
    else:
        cnd_candidates = hospital_leads.filter(deal_status__in=[DealStatus.OPEN, 'New', 'OPEN'])

    # Filter out leads that have already been booked/paid/cancelled or called (or remark 1 has been entered)
    terminal_statuses = {'booked', 'completed', 'payment done', 'cancelled', 'visited', 'admission done', 'won', 'lost'}
    call_not_done_count = 0
    for l in cnd_candidates:
        cd = l.custom_data or {}
        r1 = str(cd.get('remark_1') or '').strip()
        # Call Not Done rule: remark 1 must be empty/unentered
        if r1 and r1.lower() not in ('nan', 'none', '—', '-', ''):
            continue

        appt_st = str(cd.get('appointment_status') or '').strip().lower()
        if appt_st in terminal_statuses:
            continue
        # If call was recorded today, it is completed for today
        if cd.get('calling_date_remark_1') in (today_str, today_alt_str) or \
           cd.get('calling_date_remark_2') in (today_str, today_alt_str) or \
           cd.get('calling_date_remark_3') in (today_str, today_alt_str) or \
           cd.get('last_called_date') in (today_str, today_alt_str):
            continue
        call_not_done_count += 1

    # CARD 3: Today's OPD Booked by User
    appts_model_cnt = Appointment.objects.filter(
        lead__hospital=user.hospital,
        lead__assigned_to=user,
    ).filter(
        Q(appointment_date=today_date) |
        Q(created_at__date=today_date)
    ).filter(
        status__in=[AppointmentStatus.APPROVED, AppointmentStatus.SCHEDULED, AppointmentStatus.COMPLETED, AppointmentStatus.PENDING_APPROVAL]
    ).values('lead').distinct().count()

    appts_leads_cnt = hospital_leads.filter(
        assigned_to=user
    ).filter(
        Q(custom_data__appo_booked_date=today_str) |
        Q(custom_data__appo_booked_date=today_alt_str) |
        Q(custom_data__appointment_date=today_str) |
        Q(custom_data__appointment_date=today_alt_str) |
        Q(custom_data__appointment_confirmed_at__startswith=today_str)
    ).filter(
        Q(custom_data__appointment_status__icontains='Book') |
        Q(custom_data__appointment_status__icontains='Confirm') |
        Q(custom_data__appointment_status__icontains='Complete') |
        Q(custom_data__appointment_status__icontains='Done')
    ).distinct().count()

    todays_opd_booked_count = max(appts_model_cnt, appts_leads_cnt)

    # CARD 4: Today's Follow-ups for User
    booked_exclude_tele = (
        Q(custom_data__appointment_status__icontains='Book') |
        Q(custom_data__appointment_status__icontains='Confirm') |
        Q(deal_status__in=[DealStatus.WON, DealStatus.LOST])
    )
    todays_followups_count = hospital_leads.filter(
        assigned_to=user,
        next_followup_date=today_date
    ).exclude(
        booked_exclude_tele
    ).distinct().count()

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
    ).select_related('stage', 'campaign', 'lead_source').order_by('-updated_at')[:10]

    # Today's Tasks & Reminders
    todays_tasks = TaskReminder.objects.filter(
        Q(user=user) | Q(user__hospital=user.hospital, user__role__in=['SUPER_ADMIN', 'MANAGER']),
        due_date=today_date,
    ).exclude(
        status=TaskReminder.Status.COMPLETED
    ).select_related('user', 'lead').order_by('-priority', 'due_time')

    # SECTION 3: Upcoming OPD / Appointments (Booked for dates ahead of today)
    # 1. Leads with upcoming appo_booked_date / appointment_date
    upcoming_opd_candidates = hospital_leads.filter(
        assigned_to=user
    ).select_related('stage', 'campaign', 'lead_source')
    
    upcoming_opd_leads_list = []
    for l in upcoming_opd_candidates:
        cd = l.custom_data or {}
        apt_st = str(cd.get('appointment_status') or l.custom_deal_status or '').strip()
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

    # SECTION 4: Pending & Upcoming Follow-ups (Billing, Call, Appointments followups)
    # Includes all scheduled followups (pending overdue, today, and future)
    followups_qs = hospital_leads.filter(
        assigned_to=user,
        next_followup_date__isnull=False
    ).exclude(
        booked_exclude_tele
    ).select_related('stage', 'campaign', 'lead_source').order_by('next_followup_date')

    pending_followups_list = []
    for l in followups_qs:
        cd = l.custom_data or {}
        # Identify followup category (Billing, Call, Appointment, General)
        f_type = 'Call Follow-up'
        if cd.get('pharmacy_bill') or cd.get('opd_bill') or cd.get('total') or l.custom_deal_status == 'Payment Pending':
            f_type = 'Billing Follow-up'
        elif any(k in str(cd.get('appointment_status') or '').lower() for k in ['appo', 'reschedule', 'slot']):
            f_type = 'Appointment Follow-up'
        elif cd.get('remark_1') or cd.get('last_called_date'):
            f_type = 'Calling Follow-up'
            
        l.followup_category = f_type
        l.is_overdue = bool(l.next_followup_date and l.next_followup_date < today_date)
        pending_followups_list.append(l)

    pending_and_upcoming_followups_count = len(pending_followups_list)
    pending_and_upcoming_followups = pending_followups_list[:10]

    context = {
        'active': 'telecaller_dashboard',
        'todays_new_leads_count': todays_new_leads_count,
        'call_not_done_count': call_not_done_count,
        'todays_opd_booked_count': todays_opd_booked_count,
        'todays_followups_count': todays_followups_count,
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
                apt.appointment_date = datetime.strptime(new_date_str, "%Y-%m-%d").date()
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
            if ds_val:
                st_q |= Q(stage__name__iexact=ds_val) | Q(deal_status__iexact=ds_val) | Q(custom_data__deal_status__iexact=ds_val)
        leads = leads.filter(st_q)

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
        len(selected_doctors) + len(selected_deal_statuses) +
        len(selected_appointment_statuses) + len(selected_priorities) + len(selected_temperatures) +
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
        'selected_campaigns': selected_campaigns,
        'selected_sources': selected_sources,
        'selected_departments': selected_departments,
        'selected_doctors': selected_doctors,
        'selected_priorities': selected_priorities,
        'selected_temperatures': selected_temperatures,
        'selected_locations': selected_locations,
        'date_from_val': request.GET.get('date_from', '') or request.GET.get('date', ''),
        'date_to_val': request.GET.get('date_to', ''),
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
        
    context = {
        'page_obj': page_obj,
        'leads': page_obj,
        'page_range': page_range,
        'total_calls_logged': total_calls_logged,
        'query_params': query_params.urlencode(),
        'q': q,
        'call_date': call_date,
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
    
    # 1. Fetch Task Reports submitted to Admin
    task_reports_qs = TaskReminder.objects.filter(is_reported_to_admin=True)
    if hospital:
        task_reports_qs = task_reports_qs.filter(user__hospital=hospital)
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
