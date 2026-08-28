import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, Sum
from django.shortcuts import render
from django.utils import timezone

from accounts.models import Hospital, User
from admissions.models import Admission
from followups.models import FollowUp, Note
from leads.models import (
    Course,
    Lead,
    LeadSource,
    LeadStage,
    SourceCategory,
)
from payments.models import Payment, PaymentStatus


BUSINESS_COLOR_PALETTE = [
    {"primary": "#6366f1", "bg": "rgba(99, 102, 241, 0.12)", "border": "#6366f1", "label": "Indigo"},
    {"primary": "#0ea5e9", "bg": "rgba(14, 165, 233, 0.12)", "border": "#0ea5e9", "label": "Sky"},
    {"primary": "#10b981", "bg": "rgba(16, 185, 129, 0.12)", "border": "#10b981", "label": "Emerald"},
    {"primary": "#f59e0b", "bg": "rgba(245, 158, 11, 0.12)", "border": "#f59e0b", "label": "Amber"},
    {"primary": "#ec4899", "bg": "rgba(236, 72, 153, 0.12)", "border": "#ec4899", "label": "Pink"},
    {"primary": "#8b5cf6", "bg": "rgba(139, 92, 246, 0.12)", "border": "#8b5cf6", "label": "Purple"},
    {"primary": "#14b8a6", "bg": "rgba(20, 184, 166, 0.12)", "border": "#14b8a6", "label": "Teal"},
    {"primary": "#f43f5e", "bg": "rgba(244, 63, 94, 0.12)", "border": "#f43f5e", "label": "Rose"},
]


@login_required
def management_home(request):
    """
    Dedicated Multi-Tenant & Multi-Business Dashboard for Zappcode Super Admins and Managers.
    - Default behavior: "All Businesses" (Aggregated unified view with single combined charts & metrics).
    - Single business selected: Shows data specifically for that business.
    - Custom subset (2+ specific businesses selected, but not all): Activates Comparison Mode with side-by-side breakdowns.
    """
    if request.user.role not in (User.Role.SUPER_ADMIN, User.Role.MANAGER):
        raise PermissionDenied("This dashboard is restricted to management accounts.")

    if request.user.hospital is not None:
        raise PermissionDenied("This dashboard is restricted to Zappcode management only.")

    today = timezone.localdate()
    
    # ── 1. Fetch All Active Businesses (Tenants) ───────────────────────────────
    all_hospitals = list(Hospital.objects.filter(is_active=True).order_by("name"))
    business_options = [{"id": "zappcode", "name": "Zappcode / General Academy"}]
    for h in all_hospitals:
        business_options.append({"id": str(h.id), "name": h.name})

    # ── 2. Parse Selected Business Filters ─────────────────────────────────────
    selected_business_ids = request.GET.getlist("business")
    if not selected_business_ids and request.GET.get("business"):
        selected_business_ids = [request.GET.get("business")]

    # If nothing selected, or 'all' passed, default to all businesses
    is_all_selected_explicitly = "all" in selected_business_ids
    if not selected_business_ids or is_all_selected_explicitly:
        selected_business_ids = [b["id"] for b in business_options]

    is_all_businesses = (len(selected_business_ids) == len(business_options))
    is_single_business = (len(selected_business_ids) == 1)
    
    # Check explicit compare mode flag
    compare_param = request.GET.get("compare")
    if compare_param is not None:
        is_comparison_mode = (compare_param == "1" and len(selected_business_ids) > 1)
    else:
        # Auto-enable comparison mode if 2 or more businesses are selected
        is_comparison_mode = len(selected_business_ids) > 1 and not is_all_businesses

    # Map selected business objects with colors
    selected_businesses = []
    color_idx = 0
    for b_id in selected_business_ids:
        b_name = next((b["name"] for b in business_options if b["id"] == b_id), None)
        if b_name:
            color = BUSINESS_COLOR_PALETTE[color_idx % len(BUSINESS_COLOR_PALETTE)]
            selected_businesses.append({
                "id": b_id,
                "name": b_name,
                "color": color["primary"],
                "bg": color["bg"],
                "border": color["border"],
            })
            color_idx += 1

    # ── 3. Base Queryset Filtered by Selected Businesses ────────────────────────
    base_leads = Lead.objects.filter(is_archived=False)
    
    if not is_all_businesses:
        hosp_filter = Q()
        if "zappcode" in selected_business_ids:
            hosp_filter |= Q(hospital__isnull=True)
        numeric_hosp_ids = [int(bid) for bid in selected_business_ids if bid != "zappcode" and bid.isdigit()]
        if numeric_hosp_ids:
            hosp_filter |= Q(hospital_id__in=numeric_hosp_ids)
        base_leads = base_leads.filter(hosp_filter)

    # ── 4. Apply Additional Dashboard Filters ──────────────────────────────────
    filtered_leads = base_leads
    q = request.GET.get("q", "").strip()
    if q:
        filtered_leads = filtered_leads.filter(
            Q(lead_code__icontains=q) | Q(name__icontains=q) | Q(mobile__icontains=q)
            | Q(email__icontains=q) | Q(city__icontains=q)
        )
    if request.GET.get("city"):
        filtered_leads = filtered_leads.filter(city__iexact=request.GET.get("city"))
    if request.GET.get("source_category"):
        filtered_leads = filtered_leads.filter(source_category_id=request.GET.get("source_category"))
    if request.GET.get("lead_source"):
        filtered_leads = filtered_leads.filter(lead_source_id=request.GET.get("lead_source"))
    if request.GET.get("course"):
        filtered_leads = filtered_leads.filter(course_id=request.GET.get("course"))
    if request.GET.get("stage"):
        filtered_leads = filtered_leads.filter(stage_id=request.GET.get("stage"))
    if request.GET.get("temperature"):
        filtered_leads = filtered_leads.filter(temperature=request.GET.get("temperature"))
    if request.GET.get("deal_status"):
        filtered_leads = filtered_leads.filter(deal_status=request.GET.get("deal_status"))
    if request.GET.get("assigned_to"):
        filtered_leads = filtered_leads.filter(assigned_to_id=request.GET.get("assigned_to"))
    if request.GET.get("date_from"):
        filtered_leads = filtered_leads.filter(inquiry_date__gte=request.GET.get("date_from"))
    if request.GET.get("date_to"):
        filtered_leads = filtered_leads.filter(inquiry_date__lte=request.GET.get("date_to"))

    # ── 5. Overall Aggregated KPIs (Combined unified values) ────────────────────
    total_leads = filtered_leads.count()
    new_leads = filtered_leads.filter(inquiry_date__gte=today - timedelta(days=7)).count()
    uncontacted = filtered_leads.filter(temperature="UNCONTACTED").count()
    not_picked = filtered_leads.filter(temperature="NOT_PICKED").count()
    hot = filtered_leads.filter(temperature="HOT").count()
    warm = filtered_leads.filter(temperature="WARM").count()
    cold = filtered_leads.filter(temperature="COLD").count()
    
    lead_ids = filtered_leads.values_list("id", flat=True)
    followups_today = FollowUp.objects.filter(lead_id__in=lead_ids, followup_date=today).count()
    overdue = FollowUp.objects.filter(lead_id__in=lead_ids, followup_date__lt=today, followup_status="PENDING").count()
    
    admissions_count = Admission.objects.filter(lead_id__in=lead_ids).count()
    visits_count = filtered_leads.filter(Q(stage__name__icontains="visit") | Q(custom_data__appointment_status__icontains="Visit")).count()
    total_revenue = Payment.objects.filter(admission__lead_id__in=lead_ids, payment_status=PaymentStatus.SUCCESS).aggregate(s=Sum("amount"))["s"] or 0
    conversion_rate = round(admissions_count / total_leads * 100, 1) if total_leads else 0.0
    pending_approvals_count = User.objects.filter(is_approved=False).count()

    overall_kpis = {
        "total_leads": total_leads, "new_leads": new_leads,
        "uncontacted": uncontacted, "not_picked": not_picked,
        "hot": hot, "warm": warm, "cold": cold,
        "followups_today": followups_today, "overdue": overdue,
        "admissions": admissions_count, "conversion_rate": conversion_rate,
        "visits": visits_count, "total_revenue": total_revenue,
    }

    # ── 6. Business-by-Business KPI Breakdown (Only when is_comparison_mode) ───
    business_comparisons = []
    if is_comparison_mode:
        for b in selected_businesses:
            if b["id"] == "zappcode":
                b_leads = filtered_leads.filter(hospital__isnull=True)
            else:
                b_leads = filtered_leads.filter(hospital_id=int(b["id"]))

            b_total = b_leads.count()
            b_lead_ids = b_leads.values_list("id", flat=True)
            b_admissions = Admission.objects.filter(lead_id__in=b_lead_ids).count()
            b_rev = Payment.objects.filter(admission__lead_id__in=b_lead_ids, payment_status=PaymentStatus.SUCCESS).aggregate(s=Sum("amount"))["s"] or 0
            b_conv = round(b_admissions / b_total * 100, 1) if b_total else 0.0

            b_overdue = FollowUp.objects.filter(lead_id__in=b_lead_ids, followup_date__lt=today, followup_status="PENDING").count()

            business_comparisons.append({
                "id": b["id"],
                "name": b["name"],
                "color": b["color"],
                "bg": b["bg"],
                "border": b["border"],
                "total_leads": b_total,
                "uncontacted": b_leads.filter(temperature="UNCONTACTED").count(),
                "overdue": b_overdue,
                "admissions": b_admissions,
                "conversion_rate": b_conv,
                "revenue": b_rev,
            })

    # ── 7. Charts Data ─────────────────────────────────────────────────────────
    all_stages = list(LeadStage.objects.filter(is_active=True).order_by("order", "name"))
    funnel_stage_labels = [s.name for s in all_stages] if all_stages else ["New", "Contacted", "Interested", "Admission"]
    
    emp_lead_data = filtered_leads.values("assigned_to__first_name", "assigned_to__username").annotate(count=Count("id")).order_by("-count")[:10]
    emp_labels = [r["assigned_to__first_name"] or r["assigned_to__username"] or "Unassigned" for r in emp_lead_data]

    if is_comparison_mode:
        # A. Comparison Funnel (Multi-bar)
        funnel_datasets = []
        for b in selected_businesses:
            if b["id"] == "zappcode":
                b_leads = filtered_leads.filter(hospital__isnull=True)
            else:
                b_leads = filtered_leads.filter(hospital_id=int(b["id"]))

            stage_counts_map = dict(b_leads.values("stage__name").annotate(c=Count("id")).values_list("stage__name", "c"))
            b_counts = [stage_counts_map.get(sname, 0) for sname in funnel_stage_labels]

            funnel_datasets.append({
                "label": b["name"],
                "data": b_counts,
                "backgroundColor": b["color"],
                "borderColor": b["color"],
                "borderWidth": 1,
            })

        # B. Comparison Employee
        employee_datasets = []
        for b in selected_businesses:
            if b["id"] == "zappcode":
                b_leads = filtered_leads.filter(hospital__isnull=True)
            else:
                b_leads = filtered_leads.filter(hospital_id=int(b["id"]))

            emp_map = dict(b_leads.values("assigned_to__first_name", "assigned_to__username").annotate(c=Count("id")).values_list("assigned_to__first_name", "c"))
            emp_counts = [emp_map.get(lbl, 0) for lbl in emp_labels]

            employee_datasets.append({
                "label": b["name"],
                "data": emp_counts,
                "backgroundColor": b["color"],
            })

        # C & D. Individual Charts for each business
        business_source_charts = []
        business_course_charts = []
        for b in selected_businesses:
            if b["id"] == "zappcode":
                b_leads = filtered_leads.filter(hospital__isnull=True)
            else:
                b_leads = filtered_leads.filter(hospital_id=int(b["id"]))

            s_data = b_leads.values("lead_source__name").annotate(count=Count("id")).order_by("-count")[:6]
            s_labels = [r["lead_source__name"] or "Unknown" for r in s_data]
            s_counts = [r["count"] for r in s_data]
            business_source_charts.append({
                "business_id": b["id"],
                "business_name": b["name"],
                "color": b["color"],
                "total": sum(s_counts),
                "labels": s_labels if s_labels else ["No Source Data"],
                "counts": s_counts if s_counts else [0],
            })

            c_data = b_leads.values("course__name").annotate(count=Count("id")).order_by("-count")[:6]
            c_labels = [c["course__name"] or "General Inquiry" for c in c_data]
            c_counts = [c["count"] for c in c_data]
            business_course_charts.append({
                "business_id": b["id"],
                "business_name": b["name"],
                "color": b["color"],
                "total": sum(c_counts),
                "labels": c_labels if c_labels else ["No Category Data"],
                "counts": c_counts if c_counts else [0],
            })

    else:
        # SINGLE OR COMBINED ALL: Unified standard charts
        # A. Funnel (Unified Single Dataset)
        stage_counts_map = dict(filtered_leads.values("stage__name").annotate(c=Count("id")).values_list("stage__name", "c"))
        combined_funnel_counts = [stage_counts_map.get(sname, 0) for sname in funnel_stage_labels]
        funnel_datasets = [{
            "label": "All Leads" if is_all_businesses else selected_businesses[0]["name"],
            "data": combined_funnel_counts,
            "backgroundColor": "#4f46e5",
        }]

        # B. Employee (Unified Single Dataset)
        emp_counts = [r["count"] for r in emp_lead_data]
        employee_datasets = [{
            "label": "All Leads" if is_all_businesses else selected_businesses[0]["name"],
            "data": emp_counts,
            "backgroundColor": "#10b981",
        }]

        # C. Unified Source Doughnut
        s_data = filtered_leads.values("lead_source__name").annotate(count=Count("id")).order_by("-count")[:8]
        s_labels = [r["lead_source__name"] or "Unknown" for r in s_data]
        s_counts = [r["count"] for r in s_data]
        business_source_charts = [{
            "business_id": "all",
            "business_name": "Unified Source Distribution (All Businesses)" if is_all_businesses else selected_businesses[0]["name"],
            "color": "#6366f1",
            "total": sum(s_counts),
            "labels": s_labels if s_labels else ["No Source Data"],
            "counts": s_counts if s_counts else [0],
        }]

        # D. Unified Course / Department Pie
        c_data = filtered_leads.values("course__name").annotate(count=Count("id")).order_by("-count")[:8]
        c_labels = [c["course__name"] or "General Inquiry" for c in c_data]
        c_counts = [c["count"] for c in c_data]
        business_course_charts = [{
            "business_id": "all",
            "business_name": "Unified Course / Department Distribution" if is_all_businesses else selected_businesses[0]["name"],
            "color": "#4f46e5",
            "total": sum(c_counts),
            "labels": c_labels if c_labels else ["No Category Data"],
            "counts": c_counts if c_counts else [0],
        }]

    # ── 8. Team Activity Today ────────────────────────────────────────────────
    team_members = User.objects.filter(is_active=True, is_approved=True, role__in=['COUNSELLOR', 'HR', 'LEAD_ATTENDENT', 'MANAGER'])
    if not is_all_businesses:
        if "zappcode" in selected_business_ids and len(selected_business_ids) == 1:
            team_members = team_members.filter(hospital__isnull=True)
        elif numeric_hosp_ids and "zappcode" not in selected_business_ids:
            team_members = team_members.filter(hospital_id__in=numeric_hosp_ids)

    team_stats = []
    for member in team_members[:15]:
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
            "business_name": member.hospital.name if member.hospital else "Zappcode",
        })

    # ── 9. Filter Dropdowns ────────────────────────────────────────────────────
    used_sc_ids = base_leads.values_list("source_category_id", flat=True).distinct()
    used_ls_ids = base_leads.values_list("lead_source_id", flat=True).distinct()
    used_course_ids = base_leads.values_list("course_id", flat=True).distinct()
    used_stage_ids = base_leads.values_list("stage_id", flat=True).distinct()
    used_emp_ids = base_leads.values_list("assigned_to_id", flat=True).distinct()
    distinct_cities = sorted(list(set(base_leads.exclude(city="").values_list("city", flat=True))))

    context = {
        "active": "management_dashboard",
        "today": today,
        "kpis": overall_kpis,
        "pending_approvals_count": pending_approvals_count,
        "business_options": business_options,
        "selected_business_ids": selected_business_ids,
        "selected_businesses": selected_businesses,
        "is_all_businesses": is_all_businesses,
        "is_single_business": is_single_business,
        "is_comparison_mode": is_comparison_mode,
        "business_comparisons": business_comparisons,
        "team_stats": team_stats,
        "source_categories": SourceCategory.objects.filter(id__in=used_sc_ids),
        "lead_sources": LeadSource.objects.filter(id__in=used_ls_ids),
        "courses": Course.objects.filter(id__in=used_course_ids),
        "stages": LeadStage.objects.filter(id__in=used_stage_ids),
        "employees": User.objects.filter(id__in=used_emp_ids),
        "cities": distinct_cities,
        "request_get": request.GET,
        "funnel_chart_data": json.dumps({
            "labels": funnel_stage_labels,
            "datasets": funnel_datasets,
        }),
        "employee_chart_data": json.dumps({
            "labels": emp_labels,
            "datasets": employee_datasets,
        }),
        "business_source_charts_json": json.dumps(business_source_charts),
        "business_course_charts_json": json.dumps(business_course_charts),
        "business_source_charts": business_source_charts,
        "business_course_charts": business_course_charts,
    }
    return render(request, "dashboard/management_home.html", context)
