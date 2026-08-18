import json
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum, Q
from django.db.models.functions import TruncMonth
from django.shortcuts import render, redirect
from django.utils import timezone

from leads.models import Lead, LeadSource, SourceCategory, Course, Campaign, LeadStage
from admissions.models import Admission
from payments.models import Payment, PaymentStatus


@login_required
def home(request):
    from django.db.models import Q
    from leads.models import SourceCategory, Course, LeadStage, LeadSource, Campaign
    from accounts.models import User
    
    today = timezone.localdate()
    leads = Lead.objects.filter(is_archived=False)

    if request.user.role in ('COUNSELLOR', 'HR'):
        leads = leads.filter(assigned_to=request.user)

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

    # 2. Compute KPIs based on filtered leads
    total_leads = leads.count()
    new_leads = leads.filter(inquiry_date__gte=today - timedelta(days=7)).count()
    uncontacted = leads.filter(temperature="UNCONTACTED").count()
    not_picked = leads.filter(temperature="NOT_PICKED").count()
    hot = leads.filter(temperature="HOT").count()
    warm = leads.filter(temperature="WARM").count()
    cold = leads.filter(temperature="COLD").count()
    followups_today = leads.filter(next_followup_date=today).count()
    overdue = leads.filter(next_followup_date__lt=today).count()
    visits = leads.filter(stage__name__icontains="visit").count()
    
    admissions_qs = Admission.objects.filter(lead__in=leads)
    admissions = admissions_qs.count()
    conversion_rate = round((admissions / total_leads * 100), 1) if total_leads else 0
    revenue = Payment.objects.filter(payment_status=PaymentStatus.SUCCESS, admission__lead__in=leads).aggregate(s=Sum("amount"))["s"] or 0
    total_fee_value = admissions_qs.aggregate(s=Sum("final_fee"))["s"] or 0
    pending_revenue = float(total_fee_value) - float(revenue)

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
            "total_leads": total_leads, "new_leads": new_leads,
            "uncontacted": uncontacted, "not_picked": not_picked,
            "hot": hot, "warm": warm, "cold": cold,
            "followups_today": followups_today, "overdue": overdue, "visits": visits,
            "admissions": admissions, "conversion_rate": conversion_rate,
            "revenue": revenue, "pending_revenue": pending_revenue,
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
    """Dedicated specialized dashboard for Hospital Super Admins."""
    from accounts.models import User
    from django.core.exceptions import PermissionDenied
    from django.db.models import Count, Sum
    import json

    if request.user.role not in (User.Role.SUPER_ADMIN, User.Role.MANAGER):
        raise PermissionDenied("This dashboard is restricted to Super Admins.")
    
    # If the user is a tenant, they must have a hospital.
    # Zappcode admins (no hospital) are also allowed to view this as an aggregated dashboard.

    today = timezone.localdate()
    user = request.user

    if user.hospital:
        base_leads = Lead.objects.filter(is_archived=False, hospital=user.hospital)
    else:
        base_leads = Lead.objects.filter(is_archived=False)

    total_leads = base_leads.count()
    appts_booked = base_leads.filter(nelson_data__appo_book__iexact='YES').count()
    conv_rate = round(appts_booked / total_leads, 2) if total_leads > 0 else 0.0
    total_revenue = base_leads.aggregate(s=Sum('nelson_data__total'))['s'] or 0
    new_leads_month = base_leads.filter(created_at__year=today.year, created_at__month=today.month).count()

    def get_dist(field_name, default_key='Unknown'):
        qs = base_leads.values(field_name).annotate(c=Count('id'))
        dist = {}
        for row in qs:
            k = row[field_name]
            k = str(k).strip() if k else default_key
            if not k: k = default_key
            dist[k] = row['c']
        return dist

    insights = {
        "total_leads": total_leads,
        "appointments_booked": appts_booked,
        "conversion_rate": conv_rate,
        "total_revenue": float(total_revenue),
        "new_leads_this_month": new_leads_month,
        "gender_distribution": get_dist('nelson_data__gender'),
        "source_distribution": get_dist('lead_source__name'),
        "priority_distribution": get_dist('nelson_data__priority'),
        "campaign_distribution": get_dist('campaign__name'),
    }

    context = {
        "active": "superadmin_home",
        "today": today,
        "now": timezone.now(),
        "insights_json": json.dumps(insights),
        "insights": insights
    }
    return render(request, "dashboard/superadmin_home.html", context)

def nelson_module_view(request, module_name):
    from django.core.exceptions import PermissionDenied
    if 'nelson' not in request.user.username.lower():
        raise PermissionDenied("Restricted to Nelson admin.")
        
    titles = {
        'hospital-profile': 'Hospital Profile',
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
    from .forms import DailyReportForm
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

    if report_instance:
        # Already submitted → show locked confirmation page, no form
        return render(request, "dashboard/daily_report_done.html", {
            "active": "daily_report_submit",
            "report": report_instance,
            "report_date": report_date,
        })

    # ── Not yet submitted → show form ─────────────────────────────────────────
    day_followups = FollowUp.objects.filter(created_by=request.user, followup_date=report_date)
    suggestions = {
        "outgoing_calls": day_followups.filter(followup_mode="CALL_OUTGOING").count(),
        "incoming_calls": day_followups.filter(followup_mode="CALL_INCOMING").count(),
        "calls_attended": day_followups.filter(followup_mode__in=["CALL_OUTGOING", "CALL_INCOMING"]).count(),
        "calls_not_connected": day_followups.filter(followup_status="NOT_CONNECTED").count(),
    }

    if request.method == "POST":
        from django.db import IntegrityError, transaction
        # Re-check in case two tabs were open
        if DailyReport.objects.filter(user=request.user, report_date=report_date).exists():
            messages.warning(request, "Report already submitted for today.")
            return redirect("dashboard:submit_daily_report")

        form = DailyReportForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    cleaned = form.cleaned_data
                    DailyReport.objects.create(
                        user=request.user,
                        report_date=report_date,
                        calls_attended=cleaned.get("calls_attended", 0),
                        outgoing_calls=cleaned.get("outgoing_calls", 0),
                        incoming_calls=cleaned.get("incoming_calls", 0),
                        calls_not_connected=cleaned.get("calls_not_connected", 0),
                        leads_cold=cleaned.get("leads_cold", 0),
                        leads_interested=cleaned.get("leads_interested", 0),
                        leads_visited=cleaned.get("leads_visited", 0),
                        admissions_done=cleaned.get("admissions_done", 0),
                        follow_ups_pending=cleaned.get("follow_ups_pending", 0),
                        key_highlight=cleaned.get("key_highlight", ""),
                        challenges_faced=cleaned.get("challenges_faced", ""),
                        tomorrow_priority=cleaned.get("tomorrow_priority", ""),
                        other_updates=cleaned.get("other_updates", ""),
                        mood_rating=cleaned.get("mood_rating", 3),
                    )
                messages.success(request, f"Daily report for {report_date.strftime('%d-%m-%Y')} submitted successfully! ✅")
                return redirect("dashboard:submit_daily_report")
            except IntegrityError:
                messages.warning(request, "Report already submitted for today.")
                return redirect("dashboard:submit_daily_report")
    else:
        form = DailyReportForm(initial={
            "calls_attended": suggestions["calls_attended"],
            "outgoing_calls": suggestions["outgoing_calls"],
            "incoming_calls": suggestions["incoming_calls"],
            "calls_not_connected": suggestions["calls_not_connected"],
        })

    return render(request, "dashboard/daily_report_form.html", {
        "active": "daily_report_submit",
        "form": form,
        "report_date": report_date,
        "suggestions": suggestions,
    })


@login_required
def management_daily_reports(request):
    from accounts.models import User
    from django.core.exceptions import PermissionDenied
    from .models import DailyReport
    from datetime import datetime
    import pandas as pd
    from django.http import HttpResponse
    
    if request.user.role not in (User.Role.SUPER_ADMIN, User.Role.MANAGER):
        raise PermissionDenied("You do not have permission to view this report log.")
        
    reports = DailyReport.objects.select_related("user").all()
    
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
                "Total Calls Attended": r.calls_attended,
                "Outgoing Calls": r.outgoing_calls,
                "Incoming Calls": r.incoming_calls,
                "Calls Not Connected": r.calls_not_connected,
                "Interested Leads": r.leads_interested,
                "Cold Leads": r.leads_cold,
                "Leads Visited": r.leads_visited,
                "Admissions Done": r.admissions_done,
                "Follow-ups Pending": r.follow_ups_pending,
                "Key Highlight": r.key_highlight,
                "Challenges Faced": r.challenges_faced,
                "Tomorrow's Priority": r.tomorrow_priority,
                "Other Updates": r.other_updates,
                "Mood Rating": dict(r.MOOD_CHOICES).get(r.mood_rating, r.mood_rating),
            })
        df = pd.DataFrame(rows)
        response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        response["Content-Disposition"] = f'attachment; filename="daily_reports_{timezone.now().strftime("%Y%m%d_%H%M")}.xlsx"'
        df.to_excel(response, index=False, sheet_name="Daily Reports")
        return response
        
    # Get active/approved employees for filter dropdown
    employees = User.objects.filter(is_active=True, is_approved=True, role__in=['COUNSELLOR', 'HR', User.Role.MANAGER])
    
    return render(request, "dashboard/daily_reports_list.html", {
        "active": "reports_daily",
        "reports": reports,
        "employees": employees,
        "request_get": request.GET,
    })
