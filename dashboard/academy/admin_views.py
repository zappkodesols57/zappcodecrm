import json
from datetime import datetime, date, timedelta

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Count, Sum, Q
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from leads.models import (
    Lead, LeadSource, SourceCategory, Course, Campaign,
    LeadStage, DealStatus, LeadTemperature, AdmissionStatus
)
from followups.models import FollowUp, FollowUpStatus
from payments.models import Payment, PaymentStatus
from dashboard.helpers import filter_uncontacted_leads_ids, extract_lead_followup_date


def get_active_academy_business(user):
    """
    Helper to resolve the active Academy business tenant for the current user/request.
    Guarantees strict tenant isolation by business / business_id.
    """
    from accounts.models import Hospital
    if hasattr(user, 'business') and user.business:
        return user.business
    # Fallback to default academy business tenant
    return Hospital.objects.filter(industry=Hospital.Industry.ACADEMY, is_active=True).first()


@login_required


@login_required
def academy_admin_home(request):
    """
    Dedicated Academy Admin & Manager Analytics Dashboard.
    Completely isolated from SuperAdmin and Hospital codebases.
    Renders templates/academy/dashboard/admin_home.html.
    """
    from accounts.models import User, Hospital
    from leads.models import Lead, LeadSource, SourceCategory, Course, Campaign, LeadStage, DealStatus
    from payments.models import Payment, PaymentStatus
    from followups.models import FollowUp, Note

    user = request.user
    today = timezone.localdate()
    today_start = timezone.make_aware(datetime.combine(today, datetime.min.time()))
    today_end = timezone.make_aware(datetime.combine(today, datetime.max.time()))

    # Business Tenant Scoping (Strict isolation for Academy businesses)
    active_business = get_active_academy_business(user)
    business_id = getattr(active_business, 'id', None) or getattr(user, 'business_id', None)

    # Base Queryset strictly scoped to this Academy business
    leads_base = Lead.objects.filter(is_archived=False)
    if active_business:
        leads_base = leads_base.filter(hospital=active_business)
    elif business_id:
        leads_base = leads_base.filter(hospital__id=business_id)
    else:
        leads_base = leads_base.none()
    leads = leads_base

    # 1. Date Range Filter (Inquiry Date)
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

    # 2. Course Filter
    course_id = request.GET.get("course")
    if course_id:
        if course_id.isdigit():
            leads = leads.filter(course_id=int(course_id))
        else:
            leads = leads.filter(Q(course__name__iexact=course_id) | Q(custom_data__course__icontains=course_id))

    # 3. Trainer Filter
    trainer_val = request.GET.get("trainer", "").strip()
    if trainer_val:
        leads = leads.filter(
            Q(course__tutor__iexact=trainer_val) |
            Q(course__batches__trainer__name__iexact=trainer_val) |
            Q(custom_data__trainer__iexact=trainer_val) |
            Q(custom_data__selected_tutor__iexact=trainer_val) |
            Q(custom_data__tutor__iexact=trainer_val) |
            Q(admission__tutor__iexact=trainer_val)
        ).distinct()

    # 4. Counselor Filter
    counselor_id = request.GET.get("counselor") or request.GET.get("assigned_to")
    if counselor_id:
        if counselor_id == "unassigned":
            leads = leads.filter(assigned_to__isnull=True)
        elif counselor_id.isdigit():
            leads = leads.filter(Q(assigned_to_id=int(counselor_id)) | Q(admission__assigned_counselor_id=int(counselor_id)))

    # 5. Stages Filter
    stage_id = request.GET.get("stage")
    if stage_id:
        if stage_id.isdigit():
            target_stage = LeadStage.objects.filter(id=int(stage_id)).first()
            if target_stage:
                # Match both exact stage_id and any stage with the same name (handles cross-business stage copies)
                leads = leads.filter(Q(stage_id=int(stage_id)) | Q(stage__name__iexact=target_stage.name))
            else:
                leads = leads.filter(stage_id=int(stage_id))
        else:
            leads = leads.filter(stage__name__iexact=stage_id)

    # 6. Status Filter (Admission Status / Deal Status)
    status_val = request.GET.get("status") or request.GET.get("admission_status") or request.GET.get("deal_status")
    if status_val:
        s_up = status_val.strip().upper()
        if s_up == "WON":
            leads = leads.filter(
                Q(deal_status=DealStatus.WON) |
                Q(admission_status__in=["WON", "ADMISSION_DONE"]) |
                Q(stage__name__icontains="admission") |
                Q(stage__name__icontains="won")
            )
        elif s_up == "LOST":
            leads = leads.filter(
                Q(deal_status=DealStatus.LOST) |
                Q(admission_status__in=["LOST", "CANCELLED"]) |
                Q(stage__name__icontains="lost") |
                Q(stage__name__icontains="cancel") |
                Q(temperature=LeadTemperature.FREEZE)
            )
        elif s_up == "HOLD":
            leads = leads.filter(
                Q(admission_status="HOLD") |
                Q(stage__name__icontains="hold")
            )
        elif s_up == "OPEN":
            leads = leads.filter(
                Q(deal_status=DealStatus.OPEN) |
                Q(admission_status="OPEN")
            ).exclude(
                Q(deal_status=DealStatus.WON) | Q(deal_status=DealStatus.LOST)
            )
        else:
            leads = leads.filter(
                Q(admission_status__iexact=status_val) |
                Q(deal_status__iexact=status_val) |
                Q(custom_data__admission_status__iexact=status_val) |
                Q(custom_data__deal_status__iexact=status_val)
            )

    # 7. Source Type Filter (Academy: strictly Paid vs Organic)
    source_type = request.GET.get("source_type", "").strip().lower()
    paid_source_names = ['Meta Ads', 'Facebook Ads', 'Google Ads', 'Instagram', 'WhatsApp']
    organic_source_names = ['Google My Business', 'Website', 'Direct Walk-in']

    if source_type == 'paid':
        paid_q = (
            Q(lead_source__name__in=paid_source_names) |
            Q(custom_data__lead_source__in=paid_source_names) |
            Q(ad_platform__isnull=False) & ~Q(ad_platform='')
        )
        for p_kw in ['meta', 'facebook', 'google ad', 'instagram', 'whatsapp', 'paid']:
            paid_q |= (
                Q(lead_source__name__icontains=p_kw) |
                Q(custom_data__lead_source__icontains=p_kw) |
                Q(custom_data__source__icontains=p_kw) |
                Q(campaign__name__icontains=p_kw) |
                Q(custom_data__campaign__icontains=p_kw)
            )
        leads = leads.filter(paid_q)
    elif source_type == 'organic':
        org_q = (
            Q(lead_source__name__in=organic_source_names) |
            Q(custom_data__lead_source__in=organic_source_names)
        )
        for o_kw in ['website', 'my business', 'gmb', 'walk-in', 'walkin', 'organic']:
            org_q |= (
                Q(lead_source__name__icontains=o_kw) |
                Q(custom_data__lead_source__icontains=o_kw) |
                Q(custom_data__source__icontains=o_kw)
            )
        leads = leads.filter(org_q)

    # 8. Source Filter
    source_id = request.GET.get("lead_source") or request.GET.get("source")
    if source_id:
        if source_id.isdigit():
            leads = leads.filter(lead_source_id=int(source_id))
        else:
            leads = leads.filter(
                Q(lead_source__name__iexact=source_id) |
                Q(custom_data__lead_source__iexact=source_id)
            )

    # 9. Campaign Filter
    campaign_id = request.GET.get("campaign")
    if campaign_id:
        if campaign_id.isdigit():
            leads = leads.filter(campaign_id=int(campaign_id))
        else:
            leads = leads.filter(
                Q(campaign__name__iexact=campaign_id) |
                Q(custom_data__campaign__iexact=campaign_id) |
                Q(raw_source_metadata__campaign_name__iexact=campaign_id)
            )

    # Additional standard filters (Search query, City, Temperature)
    q = request.GET.get("q", "").strip()
    if q:
        leads = leads.filter(
            Q(lead_code__icontains=q) |
            Q(name__icontains=q) |
            Q(mobile__icontains=q) |
            Q(email__icontains=q) |
            Q(city__icontains=q) |
            Q(course__name__icontains=q)
        )

    city = request.GET.get("city")
    if city:
        leads = leads.filter(city__iexact=city)

    temp = request.GET.get("temperature")
    if temp:
        leads = leads.filter(temperature=temp)

    # Count active filters
    filter_keys = ["date_from", "date_to", "course", "trainer", "counselor", "assigned_to", "stage", "status", "admission_status", "deal_status", "lead_source", "source", "campaign", "city", "temperature", "q"]
    active_filters_count = sum(1 for k in filter_keys if request.GET.get(k))

    # 10 Refined Academy KPIs (Matching Hospital Admin standard, strictly scoped to filtered leads)
    total_leads = leads.count()
    unassigned_leads = leads.filter(assigned_to__isnull=True).count()
    assigned_leads = total_leads - unassigned_leads

    # 2. Call Not Done (Untouched Leads)
    cnd_lead_ids = filter_uncontacted_leads_ids(leads, today=today)
    call_not_done = len(cnd_lead_ids)

    # 3. Admissions Booked / Confirmed
    admissions_booked = leads.filter(
        Q(deal_status=DealStatus.WON) | 
        Q(admission_status="ADMISSION_DONE") | 
        Q(stage__name__icontains="admission") |
        Q(admission__isnull=False)
    ).distinct().count()

    # 4. Walk-in Leads (Direct Institute Visits)
    walkin_leads = leads.filter(
        Q(lead_source__name__icontains='walk') |
        Q(custom_data__lead_source__icontains='walk') |
        Q(custom_data__channel__icontains='walk')
    ).distinct().count()

    # 5. Demo / Counseling Scheduled
    demo_scheduled = leads.filter(
        Q(stage__name__icontains='demo') |
        Q(stage__name__icontains='counsel') |
        Q(stage__name__icontains='visit') |
        Q(custom_data__appointment_status__icontains='scheduled') |
        Q(next_followup_date=today)
    ).distinct().count()

    # 6. Enrolled Students
    enrolled_students = leads.filter(
        Q(admission__isnull=False) |
        Q(admission_status="ADMISSION_DONE")
    ).distinct().count()

    # 7. Won Leads (Payment / Enrollment Complete)
    won_leads = leads.filter(
        Q(deal_status=DealStatus.WON) |
        Q(admission_status__in=["WON", "ADMISSION_DONE"])
    ).distinct().count()

    # 8. Follow-ups Pending
    followups_pending = leads.filter(
        Q(next_followup_date__lte=today) |
        Q(followups__followup_date__lte=today, followups__followup_status__in=["PENDING", "pending", FollowUpStatus.PENDING])
    ).exclude(
        Q(deal_status=DealStatus.WON) | Q(deal_status=DealStatus.LOST) | Q(admission_status="LOST")
    ).distinct().count()

    # 9. Realized Revenue
    payments_total = Payment.objects.filter(
        payment_status=PaymentStatus.SUCCESS,
        admission__lead__in=leads
    ).aggregate(s=Sum("amount"))["s"] or 0

    if payments_total == 0:
        for l in leads.filter(custom_data__isnull=False):
            cd = l.custom_data or {}
            val = cd.get("total_paid") or cd.get("total") or 0.0
            try:
                payments_total += float(val)
            except (ValueError, TypeError):
                pass

    if payments_total >= 10000000:
        formatted_revenue = f"₹{payments_total / 10000000:.2f} Cr"
    elif payments_total >= 100000:
        formatted_revenue = f"₹{payments_total / 100000:.2f} L"
    elif payments_total >= 1000:
        formatted_revenue = f"₹{payments_total / 1000:.1f} K"
    else:
        formatted_revenue = f"₹{payments_total:,.0f}"

    # 10. Lost Leads
    lost_leads = leads.filter(
        Q(deal_status=DealStatus.LOST) |
        Q(admission_status__in=["LOST", "CANCELLED"]) |
        Q(temperature=LeadTemperature.FREEZE) |
        Q(stage__name__icontains="lost") |
        Q(stage__name__icontains="cancel")
    ).distinct().count()

    # Charts data
    sources = LeadSource.objects.filter(leads__in=leads).annotate(cnt=Count("leads")).order_by("-cnt")[:8]
    source_labels = [s.name for s in sources]
    source_counts = [s.cnt for s in sources]

    # Aggregate Stage Funnel by stage name (strictly Academy pipeline, exclude hospital-specific terms)
    stage_data = (
        leads.exclude(
            Q(stage__name__icontains="Appointment") |
            Q(stage__name__icontains="Doctor") |
            Q(stage__name__icontains="OPD") |
            Q(stage__name__icontains="Consultation") |
            Q(stage__name__icontains="Patient")
        )
        .values("stage__name")
        .annotate(cnt=Count("id"))
        .order_by("-cnt")
    )
    funnel_labels = [r["stage__name"] or "Unassigned" for r in stage_data]
    funnel_counts = [r["cnt"] for r in stage_data]

    courses_stat = Course.objects.filter(leads__in=leads).annotate(cnt=Count("leads")).order_by("-cnt")[:8]
    course_labels = [c.name for c in courses_stat]
    course_counts = [c.cnt for c in courses_stat]

    # Campaign vs Leads Count (Strictly filtered leads)
    campaign_counts_map = {}
    for l_item in leads.values("campaign__name", "custom_data"):
        c_name = l_item.get("campaign__name") or (l_item.get("custom_data") or {}).get("campaign") or "Direct / General"
        c_name = str(c_name).strip()
        if c_name:
            campaign_counts_map[c_name] = campaign_counts_map.get(c_name, 0) + 1
    sorted_campaigns = sorted(campaign_counts_map.items(), key=lambda x: x[1], reverse=True)[:10]
    campaign_labels = [item[0] for item in sorted_campaigns]
    campaign_counts = [item[1] for item in sorted_campaigns]

    # Monthly Trend (Inquiry Date-based)
    # If a specific date range is filtered, show trend across that range; otherwise show monthly trend across recent months
    from collections import defaultdict
    from django.db.models.functions import TruncMonth

    if date_from or date_to:
        # Date range specified: Show daily trend between date_from and date_to
        start_d = date_from or (date_to - timedelta(days=29) if date_to else today - timedelta(days=29))
        end_d = date_to or today
        if (end_d - start_d).days > 60:
            # Multi-month range: group by month
            month_counts = list(
                leads.filter(inquiry_date__isnull=False)
                .annotate(m=TruncMonth("inquiry_date"))
                .values("m")
                .annotate(cnt=Count("id"))
                .order_by("m")
            )
            trend_labels = [row["m"].strftime("%b %Y") for row in month_counts if row.get("m")]
            trend_counts = [row["cnt"] for row in month_counts if row.get("m")]
        else:
            # Up to 60 days: group by day
            day_counts = {
                row["inquiry_date"]: row["cnt"]
                for row in leads.filter(inquiry_date__gte=start_d, inquiry_date__lte=end_d)
                .values("inquiry_date")
                .annotate(cnt=Count("id"))
            }
            trend_labels = []
            trend_counts = []
            curr_d = start_d
            while curr_d <= end_d:
                trend_labels.append(curr_d.strftime("%d %b"))
                trend_counts.append(day_counts.get(curr_d, 0))
                curr_d += timedelta(days=1)
    else:
        # Default view: Monthly Trend across the past 6 to 12 months with actual inquiry dates
        six_months_ago = (today.replace(day=1) - timedelta(days=180)).replace(day=1)
        # Check if inquiry dates exist in past 6 months, else past 12 months
        month_rows = list(
            leads.filter(inquiry_date__gte=six_months_ago)
            .annotate(m=TruncMonth("inquiry_date"))
            .values("m")
            .annotate(cnt=Count("id"))
            .order_by("m")
        )
        if len(month_rows) < 3:
            # Fallback to past 12 months if data spans longer
            twelve_months_ago = (today.replace(day=1) - timedelta(days=365)).replace(day=1)
            month_rows = list(
                leads.filter(inquiry_date__gte=twelve_months_ago)
                .annotate(m=TruncMonth("inquiry_date"))
                .values("m")
                .annotate(cnt=Count("id"))
                .order_by("m")
            )
        
        # Build month sequence up to current month
        month_map = {row["m"].strftime("%b %Y"): row["cnt"] for row in month_rows if row.get("m")}
        trend_labels = []
        trend_counts = []
        if month_rows:
            first_m = month_rows[0]["m"]
            curr_m = first_m
            while curr_m <= today:
                m_label = curr_m.strftime("%b %Y")
                if m_label not in trend_labels:
                    trend_labels.append(m_label)
                    trend_counts.append(month_map.get(m_label, 0))
                # Next month
                if curr_m.month == 12:
                    curr_m = curr_m.replace(year=curr_m.year + 1, month=1)
                else:
                    curr_m = curr_m.replace(month=curr_m.month + 1)
        else:
            # If no recent leads found, show all distinct months available in queryset
            all_months = list(
                leads.filter(inquiry_date__isnull=False)
                .annotate(m=TruncMonth("inquiry_date"))
                .values("m")
                .annotate(cnt=Count("id"))
                .order_by("m")
            )
            trend_labels = [r["m"].strftime("%b %Y") for r in all_months if r.get("m")]
            trend_counts = [r["cnt"] for r in all_months if r.get("m")]

    # Team Activity Today (Strictly scoped to current business)
    if active_business:
        team_members = User.objects.filter(
            hospital=active_business, is_active=True
        ).exclude(role=User.Role.SUPER_ADMIN).order_by("first_name", "username")
    else:
        team_members = User.objects.filter(
            hospital__isnull=True, is_active=True
        ).exclude(role=User.Role.SUPER_ADMIN).order_by("first_name", "username")

    team_stats = []
    for member in team_members:
        member_fu = FollowUp.objects.filter(created_by=member, created_at__date=today)
        out_calls = member_fu.filter(followup_mode="CALL_OUTGOING").count()
        in_calls = member_fu.filter(followup_mode="CALL_INCOMING").count()
        whatsapp = member_fu.filter(followup_mode="WHATSAPP").count()
        sms = member_fu.filter(followup_mode="SMS").count()
        email = member_fu.filter(followup_mode="EMAIL").count()
        notes_cnt = Note.objects.filter(created_by=member, created_at__date=today).count()
        team_stats.append({
            "member": member,
            "outgoing_calls": out_calls,
            "incoming_calls": in_calls,
            "whatsapp": whatsapp,
            "sms": sms,
            "email": email,
            "total_entries": member_fu.count() + notes_cnt,
        })

    pending_approvals_count = User.objects.filter(hospital=active_business, is_active=False).count() if active_business else 0

    # Business-Scoped Filter Dropdown Options
    # Courses
    business_courses = Course.objects.filter(hospital=active_business, is_active=True) if active_business else Course.objects.filter(hospital__isnull=True, is_active=True)
    if not business_courses.exists():
        business_courses = Course.objects.filter(is_active=True)

    # Trainers (Dedicated Trainer master + Course tutors + Staff trainers)
    from leads.models import Trainer
    business_trainers = []
    trainers_qs = Trainer.objects.filter(is_active=True)
    if active_business:
        trainers_qs = trainers_qs.filter(hospital=active_business)
    else:
        trainers_qs = trainers_qs.filter(hospital__isnull=True)
    for tr in trainers_qs.order_by("name"):
        if tr.name.strip() and tr.name.strip() not in business_trainers:
            business_trainers.append(tr.name.strip())

    course_tutors = business_courses.exclude(tutor="").values_list("tutor", flat=True).distinct()
    for t in course_tutors:
        if t and t.strip() and t.strip() not in business_trainers:
            business_trainers.append(t.strip())

    # Counselors (Users with COUNSELLOR role in this business)
    if active_business:
        counselors = User.objects.filter(
            hospital=active_business,
            is_active=True,
            role=User.Role.COUNSELLOR
        ).order_by("first_name", "username")
    else:
        counselors = User.objects.filter(
            hospital__isnull=True,
            is_active=True,
            role=User.Role.COUNSELLOR
        ).order_by("first_name", "username")

    # Stages (Academy/Shared stages + any stage currently assigned to this business's leads)
    if active_business:
        raw_stages = LeadStage.objects.filter(hospital=active_business, is_active=True).order_by("order", "name")
    else:
        raw_stages = LeadStage.objects.filter(
            is_active=True,
            business_type__in=[LeadStage.BusinessType.ACADEMY, LeadStage.BusinessType.ALL]
        ).order_by("order", "name")
    seen_stage_names = set()
    stages = []
    for st in raw_stages:
        key = (st.name or "").strip().lower()
        if key and key not in seen_stage_names:
            seen_stage_names.add(key)
            stages.append(st)

    # Campaigns (Business-specific)
    if active_business:
        campaigns = Campaign.objects.filter(hospital=active_business, is_active=True).order_by("name")
    else:
        campaigns = Campaign.objects.filter(hospital__isnull=True, is_active=True).order_by("name")

    # Lead Sources
    lead_sources = LeadSource.objects.filter(is_active=True).order_by("name")

    # Status choices
    status_choices = [
        ("OPEN", "Open"),
        ("HOLD", "Hold"),
        ("WON", "Won (Admitted)"),
        ("LOST", "Lost / Cancelled"),
    ]

    # Paginated Filtered Results (Bottom Table)
    paginator = Paginator(leads.select_related("course", "stage", "assigned_to", "lead_source", "campaign").order_by("-inquiry_date", "-created_at"), 15)
    page_number = request.GET.get("page")
    try:
        filtered_leads_page = paginator.page(page_number)
    except PageNotAnInteger:
        filtered_leads_page = paginator.page(1)
    except EmptyPage:
        filtered_leads_page = paginator.page(paginator.num_pages)

    # Attach trainer name and batch to page leads
    for l in filtered_leads_page:
        cd = l.custom_data or {}
        l.display_trainer = cd.get("trainer") or cd.get("selected_tutor") or cd.get("tutor") or (l.course.tutor if l.course else "") or "—"

    # Preserve active filter parameters across pagination
    req_params = request.GET.copy()
    if 'page' in req_params:
        del req_params['page']
    filter_query_string = req_params.urlencode()

    context = {
        "active": "dashboard",
        "active_business": active_business,
        "kpis": {
            "total_leads": total_leads,
            "unassigned_leads": unassigned_leads,
            "assigned_leads": assigned_leads,
            "call_not_done": call_not_done,
            "admissions_booked": admissions_booked,
            "walkin_leads": walkin_leads,
            "demo_scheduled": demo_scheduled,
            "enrolled_students": enrolled_students,
            "won_leads": won_leads,
            "followups_pending": followups_pending,
            "total_revenue": payments_total,
            "formatted_revenue": formatted_revenue,
            "lost_leads": lost_leads,
            "uncontacted": call_not_done,
            "contacted_today": total_leads - call_not_done,
            "booked_today": admissions_booked,
            "overdue": followups_pending,
        },
        "chart_data": json.dumps({
            "source": {"labels": source_labels, "counts": source_counts},
            "funnel": {"labels": funnel_labels, "counts": funnel_counts},
            "trend": {"labels": trend_labels, "counts": trend_counts},
            "course": {"labels": course_labels, "counts": course_counts},
            "campaign": {"labels": campaign_labels, "counts": campaign_counts},
        }),
        "source_categories": SourceCategory.objects.all(),
        "lead_sources": lead_sources,
        "courses": business_courses,
        "trainers": business_trainers,
        "counselors": counselors,
        "stages": stages,
        "campaigns": campaigns,
        "status_choices": status_choices,
        "employees": team_members,
        "cities": leads.exclude(city="").values_list("city", flat=True).distinct()[:20],
        "request_get": request.GET,
        "active_filters_count": active_filters_count,
        "team_stats": team_stats,
        "pending_approvals_count": pending_approvals_count,
        "filtered_leads": filtered_leads_page,
        "total_filtered_count": total_leads,
    }

    return render(request, "academy/dashboard/admin_home.html", context)

