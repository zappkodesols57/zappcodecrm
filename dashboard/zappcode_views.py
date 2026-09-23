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
    LeadTemperature,
    DealStatus,
    AdmissionStatus,
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
    business_options = [{"id": str(h.id), "name": h.name} for h in all_hospitals]

    # ── 2. Parse Selected Business Filters ─────────────────────────────────────
    raw_biz_list = [b.strip() for b in request.GET.getlist("business") if b.strip()]
    if not raw_biz_list and request.GET.get("business", "").strip():
        raw_biz_list = [request.GET.get("business").strip()]
    
    # Check session active_business_id if no explicit GET filter passed
    if not raw_biz_list:
        sess_biz = str(getattr(request, 'session', {}).get("active_business_id", "")).strip()
        if sess_biz and sess_biz != "all" and sess_biz != "0":
            raw_biz_list = [sess_biz]

    selected_business_ids = raw_biz_list
    is_all_selected_explicitly = "all" in selected_business_ids or "0" in selected_business_ids
    if not selected_business_ids or is_all_selected_explicitly:
        selected_business_ids = [b["id"] for b in business_options]

    is_all_businesses = (len(selected_business_ids) == len(business_options)) or is_all_selected_explicitly
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
        numeric_hosp_ids = [int(bid) for bid in selected_business_ids if bid.isdigit()]
        if numeric_hosp_ids:
            base_leads = base_leads.filter(hospital_id__in=numeric_hosp_ids)

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
        sc_val = request.GET.get("source_category").strip()
        if sc_val.isdigit():
            filtered_leads = filtered_leads.filter(source_category_id=int(sc_val))
        else:
            filtered_leads = filtered_leads.filter(source_category__name__iexact=sc_val)
    if request.GET.get("lead_source") or request.GET.get("lead_source_name"):
        ls_val = (request.GET.get("lead_source") or request.GET.get("lead_source_name")).strip()
        if ls_val.isdigit():
            filtered_leads = filtered_leads.filter(lead_source_id=int(ls_val))
        else:
            filtered_leads = filtered_leads.filter(Q(lead_source__name__iexact=ls_val) | Q(custom_data__lead_source__iexact=ls_val))
    if request.GET.get("course") or request.GET.get("course_name"):
        c_val = (request.GET.get("course") or request.GET.get("course_name")).strip()
        if c_val.isdigit():
            filtered_leads = filtered_leads.filter(course_id=int(c_val))
        else:
            filtered_leads = filtered_leads.filter(Q(course__name__iexact=c_val) | Q(custom_data__department__iexact=c_val) | Q(custom_data__course__iexact=c_val))
    if request.GET.get("stage") or request.GET.get("stage_name"):
        stg_val = (request.GET.get("stage") or request.GET.get("stage_name")).strip()
        if stg_val.isdigit():
            filtered_leads = filtered_leads.filter(stage_id=int(stg_val))
        else:
            filtered_leads = filtered_leads.filter(stage__name__iexact=stg_val)
    if request.GET.get("temperature"):
        filtered_leads = filtered_leads.filter(temperature=request.GET.get("temperature"))
    if request.GET.get("deal_status"):
        filtered_leads = filtered_leads.filter(deal_status=request.GET.get("deal_status"))
    if request.GET.get("assigned_to"):
        asg_val = request.GET.get("assigned_to").strip()
        if asg_val.isdigit():
            filtered_leads = filtered_leads.filter(assigned_to_id=int(asg_val))
        else:
            filtered_leads = filtered_leads.filter(
                Q(assigned_to__username__iexact=asg_val)
                | Q(assigned_to__first_name__iexact=asg_val)
                | Q(custom_data__assigned_to__iexact=asg_val)
            )

    # Date / Time Filter handling: today, this_month, all_time, or custom date_from / date_to
    time_filter = request.GET.get("time_filter", "").strip()
    date_from = request.GET.get("date_from", "").strip()
    date_to = request.GET.get("date_to", "").strip()

    if time_filter == "today":
        filtered_leads = filtered_leads.filter(inquiry_date=today)
    elif time_filter == "this_month" or time_filter == "month":
        filtered_leads = filtered_leads.filter(inquiry_date__year=today.year, inquiry_date__month=today.month)
    elif time_filter == "all_time" or time_filter == "all":
        pass  # No date restriction
    else:
        if date_from:
            filtered_leads = filtered_leads.filter(inquiry_date__gte=date_from)
        if date_to:
            filtered_leads = filtered_leads.filter(inquiry_date__lte=date_to)

    # ── Helper for Course vs Department Label Determination ────────────────────
    def get_category_meta_for_business(biz_name):
        b_lower = (biz_name or "").lower()
        if "hospital" in b_lower or "nelson" in b_lower or "clinic" in b_lower or "care" in b_lower or "medical" in b_lower:
            return {"title": "Department Distribution", "icon": "fa-hospital", "unit": "Department"}
        elif "academy" in b_lower or "zappcode" in b_lower or "institute" in b_lower or "school" in b_lower:
            return {"title": "Course Distribution", "icon": "fa-graduation-cap", "unit": "Course"}
        return {"title": "Course / Department Distribution", "icon": "fa-graduation-cap", "unit": "Specialization"}

    # Determine overall primary business category title
    if is_single_business and selected_businesses:
        primary_cat_meta = get_category_meta_for_business(selected_businesses[0]["name"])
    else:
        primary_cat_meta = {"title": "Course & Department Distribution", "icon": "fa-graduation-cap", "unit": "Course/Dept"}

    # ── 5. Overall Aggregated KPIs (Combined unified values) ────────────────────
    total_leads = filtered_leads.count()
    new_leads = filtered_leads.filter(inquiry_date__gte=today - timedelta(days=7)).count()
    
    uncontacted_filter = (
        Q(temperature=LeadTemperature.UNCONTACTED) &
        Q(followup_count=0) &
        Q(next_followup_date__isnull=True) &
        Q(deal_status__in=[DealStatus.OPEN, 'New', 'OPEN']) &
        Q(admission_status__in=[AdmissionStatus.NOT_APPLIED, '', None]) &
        Q(admission__isnull=True) &
        (Q(stage__isnull=True) | Q(stage__name__in=['New', 'Fresh', 'Uncontacted', 'new', 'fresh', 'uncontacted']))
    )
    uncontacted = filtered_leads.filter(uncontacted_filter).distinct().count()
    not_picked = filtered_leads.filter(temperature="NOT_PICKED").count()
    hot = filtered_leads.filter(temperature="HOT").count()
    warm = filtered_leads.filter(temperature="WARM").count()
    cold = filtered_leads.filter(temperature="COLD").count()
    
    lead_ids = filtered_leads.values_list("id", flat=True)
    followups_today = FollowUp.objects.filter(lead_id__in=lead_ids, followup_date=today).count()
    overdue = FollowUp.objects.filter(lead_id__in=lead_ids, followup_date__lt=today, followup_status="PENDING").count()
    
    admissions_filter = (
        Q(admission_status="ADMISSION_DONE") | Q(deal_status="WON") | Q(stage__name__icontains="admission") | Q(admission__isnull=False)
    )
    admissions_count = filtered_leads.filter(admissions_filter).distinct().count()
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
            b_admissions = b_leads.filter(admissions_filter).distinct().count()
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
                "uncontacted": b_leads.filter(uncontacted_filter).distinct().count(),
                "overdue": b_overdue,
                "admissions": b_admissions,
                "conversion_rate": b_conv,
                "revenue": b_rev,
            })

    # ── 7. Charts Data ─────────────────────────────────────────────────────────
    all_stages = list(LeadStage.objects.filter(is_active=True).order_by("order", "name"))
    funnel_stage_labels = [s.name for s in all_stages] if all_stages else ["New", "Contacted", "Interested", "Admission"]
    
    emp_lead_data = (
        filtered_leads.values("assigned_to__id", "assigned_to__first_name", "assigned_to__username")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )
    emp_labels = [r["assigned_to__first_name"] or r["assigned_to__username"] or "Unassigned" for r in emp_lead_data]
    emp_ids = [r["assigned_to__id"] for r in emp_lead_data]

    # Helper function to extract course/department counts from leads queryset
    def extract_category_counts(leads_qs, is_hospital_domain=False):
        from collections import Counter
        cat_counter = Counter()
        for lead_item in leads_qs.only("course__name", "custom_data"):
            c_name = None
            if is_hospital_domain:
                cd = lead_item.custom_data or {}
                c_name = cd.get("department") or cd.get("course") or (lead_item.course.name if lead_item.course else None)
            else:
                c_name = (lead_item.course.name if lead_item.course else None)
                if not c_name:
                    cd = lead_item.custom_data or {}
                    c_name = cd.get("course") or cd.get("department")
            if not c_name or str(c_name).strip() in ("", "-", "nan", "None", "null", "—"):
                c_name = "General Inquiry"
            cat_counter[str(c_name).strip()] += 1
        
        top_cats = cat_counter.most_common(8)
        c_labels = [k for k, v in top_cats]
        c_counts = [v for k, v in top_cats]
        return c_labels, c_counts

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

            b_meta = get_category_meta_for_business(b["name"])

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

            is_hosp = "hospital" in b["name"].lower() or "nelson" in b["name"].lower() or "clinic" in b["name"].lower()
            c_labels, c_counts = extract_category_counts(b_leads, is_hospital_domain=is_hosp)

            business_course_charts.append({
                "business_id": b["id"],
                "business_name": b["name"],
                "chart_title": b_meta["title"],
                "icon": b_meta["icon"],
                "unit": b_meta["unit"],
                "color": b["color"],
                "total": sum(c_counts),
                "labels": c_labels if c_labels else ["No Data"],
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
        is_hosp_single = is_single_business and ("hospital" in selected_businesses[0]["name"].lower() or "nelson" in selected_businesses[0]["name"].lower())
        c_labels, c_counts = extract_category_counts(filtered_leads, is_hospital_domain=is_hosp_single)

        business_course_charts = [{
            "business_id": "all",
            "business_name": primary_cat_meta["title"] if is_all_businesses else selected_businesses[0]["name"],
            "chart_title": primary_cat_meta["title"],
            "icon": primary_cat_meta["icon"],
            "unit": primary_cat_meta["unit"],
            "color": "#4f46e5",
            "total": sum(c_counts),
            "labels": c_labels if c_labels else ["No Data"],
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
        "primary_cat_meta": primary_cat_meta,
        "time_filter": time_filter,
        "date_from": date_from,
        "date_to": date_to,
        "request_get": request.GET,
        "funnel_chart_data": json.dumps({
            "labels": funnel_stage_labels,
            "datasets": funnel_datasets,
        }),
        "employee_chart_data": json.dumps({
            "labels": emp_labels,
            "datasets": employee_datasets,
            "emp_ids": emp_ids,
        }),
        "business_source_charts_json": json.dumps(business_source_charts),
        "business_course_charts_json": json.dumps(business_course_charts),
        "business_source_charts": business_source_charts,
        "business_course_charts": business_course_charts,
    }
    return render(request, "dashboard/management_home.html", context)
