from django.core.paginator import Paginator
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
from accounts.models import User


@login_required
def home(request):
    from django.db.models import Q
    from leads.models import SourceCategory, Course, LeadStage, LeadSource, Campaign
    from accounts.models import User
    
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

    gender_filter = request.GET.get('gender')
    if gender_filter:
        base_leads = base_leads.filter(nelson_data__gender__iexact=gender_filter)
        
    source_filter = request.GET.get('source')
    if source_filter:
        base_leads = base_leads.filter(lead_source__name__iexact=source_filter)
        
    priority_filter = request.GET.get('priority')
    if priority_filter:
        base_leads = base_leads.filter(nelson_data__priority__iexact=priority_filter)
        
    campaign_filter = request.GET.get('campaign')
    if campaign_filter:
        base_leads = base_leads.filter(campaign__name__iexact=campaign_filter)

    department_filter = request.GET.get('department')
    if department_filter:
        base_leads = base_leads.filter(nelson_data__department__iexact=department_filter)

    doctor_filter = request.GET.get('doctor')
    if doctor_filter:
        base_leads = base_leads.filter(nelson_data__doctor__iexact=doctor_filter)

    total_leads = base_leads.count()
    appts_booked = base_leads.filter(nelson_data__appo_book__iexact='YES').count()
    conv_rate = round(appts_booked / total_leads, 2) if total_leads > 0 else 0.0
    total_revenue = base_leads.aggregate(s=Sum('nelson_data__total'))['s'] or 0
    new_leads_month = base_leads.filter(created_at__year=today.year, created_at__month=today.month).count()

    def get_dist(field_name):
        qs = base_leads.values(field_name).annotate(c=Count('id'))
        dist = {}
        for row in qs:
            k = row[field_name]
            if not k: 
                continue
            k_str = str(k).strip()
            if not k_str or k_str.lower() in ['unknown', 'none', 'null', 'nan']:
                continue
            dist[k_str] = row['c']
        return dist

    import calendar
    month_wise_leads = {}
    current_year = today.year
    current_month = today.month
    
    months_to_fetch = []
    if current_month <= 2:
        months_to_fetch.extend([(current_year - 1, 11), (current_year - 1, 12)])
        
    for m in range(1, current_month + 1):
        months_to_fetch.append((current_year, m))
        
    for y, m in months_to_fetch:
        count = base_leads.filter(inquiry_date__year=y, inquiry_date__month=m).count()
        label = f"{calendar.month_abbr[m]} {str(y)[-2:]}" # e.g. "Nov 25"
        month_wise_leads[label] = count

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
        "funnel_data": {
            "Leads": base_leads.count(),
            "Contacted": base_leads.exclude(nelson_data__remark_1='').count(),
            "Follow-up": base_leads.exclude(nelson_data__remark_2='').count(),
            "Appointments": base_leads.filter(nelson_data__appo_book__iexact='YES').count(),
            "Visits Completed": base_leads.filter(nelson_data__done__iexact='YES').count(),
            "Active Patients": base_leads.exclude(nelson_data__uhid_id_no='').count(),
            "Revenue": float(total_revenue)
        },
        "month_wise_leads": month_wise_leads,
    }

    context = {
        "active": "superadmin_home",
        "today": today,
        "now": timezone.now(),
        "insights_json": json.dumps(insights),
        "insights": insights,
        "has_active_filters": any([gender_filter, source_filter, priority_filter, campaign_filter, department_filter, doctor_filter]),
    }
    return render(request, "dashboard/superadmin_home.html", context)

@login_required
def nelson_module_view(request, module_name):
    from django.core.exceptions import PermissionDenied
    from accounts.models import User
    from django.contrib import messages
    from django.shortcuts import redirect
    import json
    
    if request.user.role not in (User.Role.SUPER_ADMIN, User.Role.MANAGER):
        raise PermissionDenied("Restricted to Admin/Manager.")

    if module_name == 'hospital-profile':
        hospital = request.user.hospital
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

@login_required
def telecaller_home(request):
    from accounts.models import User
    from leads.models import Lead, LeadTemperature
    from dashboard.models import TaskReminder
    from followups.models import FollowUp
    from datetime import date
    
    if request.user.role != User.Role.LEAD_ATTENDENT or not request.user.hospital:
        messages.error(request, "Access denied.")
        return redirect("dashboard:home")
        
    user = request.user
    today_date = timezone.localdate()
    today_str = today_date.strftime("%Y-%m-%d")
    today_alt_str = today_date.strftime("%d-%m-%Y")
    
    # 1. Calls Today (Calls made by this user today)
    # Checked from remarks date or FollowUp objects logged by user
    calls_today_count = Lead.objects.filter(
        hospital=user.hospital,
        assigned_to=user,
    ).filter(
        Q(custom_data__calling_date_remark_1=today_str) |
        Q(custom_data__calling_date_remark_1=today_alt_str) |
        Q(custom_data__calling_date_remark_2=today_str) |
        Q(custom_data__calling_date_remark_2=today_alt_str) |
        Q(custom_data__calling_date_remark_3=today_str) |
        Q(custom_data__calling_date_remark_3=today_alt_str) |
        Q(followups__followup_date=today_date, followups__created_by=user)
    ).distinct().count()
    
    # 2. Appointments Booked Today by this user
    appts_today_count = Lead.objects.filter(
        hospital=user.hospital,
        assigned_to=user,
        custom_data__appointment_status="Booked",
    ).filter(
        Q(custom_data__appo_booked_date=today_str) |
        Q(custom_data__appo_booked_date=today_alt_str) |
        Q(updated_at__date=today_date)
    ).count()
    
    # 3. New Hot Leads Today Overall (Received in Hospital today)
    hot_leads_today_count = Lead.objects.filter(
        hospital=user.hospital,
        is_archived=False,
    ).filter(
        Q(inquiry_date=today_date) | Q(created_at__date=today_date)
    ).filter(
        Q(custom_data__priority__iexact="Hot") | Q(temperature=LeadTemperature.HOT)
    ).count()
    
    # 4. Overdue Follow-ups Remaining for this user
    overdue_followups_count = Lead.objects.filter(
        hospital=user.hospital,
        assigned_to=user,
        is_archived=False,
    ).exclude(
        deal_status__in=['WON', 'LOST']
    ).filter(
        Q(next_followup_date__lt=today_date) |
        Q(custom_data__calling_date_remark_1__lt=today_str, custom_data__calling_date_remark_1__gt="")
    ).count()
    
    # 5. My Recent Leads (Latest 10 entries assigned to this user)
    my_recent_leads = Lead.objects.filter(
        hospital=user.hospital,
        assigned_to=user,
        is_archived=False,
    ).select_related('stage').order_by('-updated_at')[:10]
    
    # 6. Today's Tasks & Reminders (Assigned by Manager/Admin or created by user for today)
    todays_tasks = TaskReminder.objects.filter(
        Q(user=user) | Q(user__hospital=user.hospital, user__role__in=['SUPER_ADMIN', 'MANAGER']),
        due_date=today_date,
    ).exclude(
        status=TaskReminder.Status.COMPLETED
    ).select_related('user', 'lead').order_by('-priority', 'due_time')
    
    context = {
        'active': 'telecaller_dashboard',
        'calls_today_count': calls_today_count,
        'appts_today_count': appts_today_count,
        'hot_leads_today_count': hot_leads_today_count,
        'overdue_followups_count': overdue_followups_count,
        'my_recent_leads': my_recent_leads,
        'todays_tasks': todays_tasks,
        'today_date': today_date,
    }
    return render(request, "dashboard/telecaller_home.html", context)

@login_required
def placeholder_view(request, module_name):
    # This acts as a dummy view for all incomplete telecaller modules
    return render(request, "dashboard/placeholder.html", {"active": module_name, "module_name": module_name.replace("_", " ").title()})

@login_required
def telecaller_search(request):
    from accounts.models import User
    from leads.models import Lead, DealStatus, AdmissionStatus
    from django.db.models import Q
    import csv
    from django.http import HttpResponse
    from django.core.paginator import Paginator

    if request.user.role != User.Role.LEAD_ATTENDENT or not request.user.hospital:
        messages.error(request, "Access denied.")
        return redirect("dashboard:home")

    leads = Lead.objects.filter(hospital=request.user.hospital).order_by('-inquiry_date')

    from leads.models import LeadStage
    
    # Get Filter Parameters
    q = request.GET.get('q', '').strip()
    date_filter = request.GET.get('date', '')
    status_filter = request.GET.get('status', '').strip()
    assigned_filter = request.GET.get('assigned', '').strip()
    appointment_filter = request.GET.get('appointment_status', '').strip()
    converted_filter = request.GET.get('converted', '')
    doctor_filter = request.GET.get('doctor', '').strip()
    disease_filter = request.GET.get('disease', '').strip()
    priority_filter = request.GET.get('priority', '').strip()

    # Apply Filters
    if q:
        leads = leads.filter(Q(name__icontains=q) | Q(mobile__icontains=q) | Q(lead_code__icontains=q))
    if date_filter:
        leads = leads.filter(inquiry_date=date_filter)
    
    # Status / Assigned Filter logic
    if status_filter:
        if status_filter.lower() == 'assigned':
            leads = leads.filter(assigned_to__isnull=False)
        elif status_filter.lower() in ['unassigned', 'new']:
            leads = leads.filter(assigned_to__isnull=True)
        else:
            leads = leads.filter(Q(stage__name__iexact=status_filter) | Q(deal_status__iexact=status_filter))
            
    if assigned_filter:
        if assigned_filter == 'assigned':
            leads = leads.filter(assigned_to__isnull=False)
        elif assigned_filter == 'unassigned':
            leads = leads.filter(assigned_to__isnull=True)
        elif assigned_filter == 'my_leads':
            leads = leads.filter(assigned_to=request.user)
        else:
            # Specific user ID
            leads = leads.filter(assigned_to_id=assigned_filter)
    if appointment_filter:
        leads = leads.filter(custom_data__appointment_status=appointment_filter)
    if converted_filter == 'yes':
        leads = leads.filter(admission_status=AdmissionStatus.ADMISSION_DONE)
    elif converted_filter == 'no':
        leads = leads.exclude(admission_status=AdmissionStatus.ADMISSION_DONE)
    
    # Custom Data JSON Filters
    if doctor_filter:
        leads = leads.filter(custom_data__doctor__icontains=doctor_filter)
    if disease_filter:
        leads = leads.filter(custom_data__disease__icontains=disease_filter)
    if priority_filter:
        leads = leads.filter(custom_data__priority=priority_filter)

    # Handle Export
    if 'export' in request.GET:
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="leads_search_export.csv"'
        writer = csv.writer(response)
        writer.writerow(['Patient Name', 'Mobile', 'Doctor', 'Disease', 'Priority', 'Status', 'Inquiry Date', 'Assigned To'])
        for lead in leads:
            doctor = lead.custom_data.get('doctor', '') if lead.custom_data else ''
            disease = lead.custom_data.get('disease', '') if lead.custom_data else ''
            priority = lead.custom_data.get('priority', '') if lead.custom_data else ''
            assigned = lead.assigned_to.get_full_name() if lead.assigned_to else 'Unassigned'
            writer.writerow([lead.name, lead.mobile, doctor, disease, priority, lead.get_deal_status_display(), lead.inquiry_date, assigned])
        return response

    paginator = Paginator(leads, 25)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1) if hasattr(paginator, 'get_elided_page_range') else paginator.page_range

    query_params = request.GET.copy()
    if 'page' in query_params:
        del query_params['page']

    all_stages = LeadStage.objects.filter(is_active=True).order_by('order')
    
    context = {
        'page_obj': page_obj,
        'leads': page_obj,
        'page_range': page_range,
        'total_count': paginator.count,
        'query_params': query_params.urlencode(),
        'q': q,
        'date_filter': date_filter,
        'status_filter': status_filter,
        'assigned_filter': assigned_filter,
        'appointment_filter': appointment_filter,
        'converted_filter': converted_filter,
        'doctor_filter': doctor_filter,
        'disease_filter': disease_filter,
        'priority_filter': priority_filter,
        'all_stages': all_stages,
        'active': 'search_filter',
    }
    return render(request, "dashboard/telecaller_search.html", context)

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
        if action in ['COMPLETED', 'CANCELLED', 'NO_SHOW']:
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
    from leads.models import Lead
    from django.db.models import Q
    from django.core.paginator import Paginator
    
    if request.user.role != User.Role.LEAD_ATTENDENT or not request.user.hospital:
        messages.error(request, "Access denied.")
        return redirect("dashboard:home")
        
    # Get leads strictly assigned to the current user, ordered by most recently updated
    leads = Lead.objects.filter(assigned_to=request.user).order_by('-updated_at')
    
    # Search logic
    q = request.GET.get('q', '').strip()
    if q:
        leads = leads.filter(Q(name__icontains=q) | Q(mobile__icontains=q) | Q(lead_code__icontains=q))
        
    paginator = Paginator(leads, 25)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    
    # Calculate custom dynamic range for nice scroller
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1) if hasattr(paginator, 'get_elided_page_range') else paginator.page_range
    
    query_params = request.GET.copy()
    if 'page' in query_params:
        del query_params['page']
        
    context = {
        'page_obj': page_obj,
        'leads': page_obj,
        'page_range': page_range,
        'q': q,
        'query_params': query_params.urlencode(),
        'total_count': paginator.count,
        'active': 'my_leads',
    }
    return render(request, "dashboard/telecaller_my_leads.html", context)

from accounts.models import HospitalRolePermission
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

@login_required
def roles_permissions_view(request):
    if not request.user.can_manage_users:
        raise PermissionDenied("You do not have permission to manage roles and permissions.")
        
    hospital = request.user.hospital
    if not hospital:
        messages.error(request, "No hospital context found.")
        return redirect("dashboard:home")

    available_permissions = [
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
    from leads.models import Lead
    from django.db.models import Q
    from django.core.paginator import Paginator
    
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
        
    paginator = Paginator(leads, 24)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1) if hasattr(paginator, 'get_elided_page_range') else paginator.page_range
    
    query_params = request.GET.copy()
    if 'page' in query_params:
        del query_params['page']
        
    context = {
        'leads': page_obj,
        'page_obj': page_obj,
        'page_range': page_range,
        'query_params': query_params.urlencode(),
        'total_count': paginator.count,
        'q': q,
        'active': 'new_enquiries',
    }
    return render(request, "dashboard/telecaller_new_enquiries.html", context)

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
    
    # Pagination
    paginator = Paginator(tasks, 20)
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
        'active': 'tasks',
    }
    return render(request, "dashboard/tasks.html", context)


@login_required
def task_create_view(request):
    if request.method == "POST":
        title = request.POST.get('title', '').strip()
        description = request.POST.get('description', '').strip()
        due_date = request.POST.get('due_date') or timezone.localdate()
        due_time = request.POST.get('due_time') or None
        priority = request.POST.get('priority', TaskReminder.Priority.MEDIUM)
        lead_id = request.POST.get('lead_id')
        sync_to_followup = bool(request.POST.get('sync_to_followup'))
        
        lead = None
        if lead_id:
            try:
                lead = Lead.objects.get(pk=lead_id)
            except Lead.DoesNotExist:
                lead = None
                
        task = TaskReminder.objects.create(
            user=request.user,
            title=title,
            description=description,
            due_date=due_date,
            due_time=due_time if due_time else None,
            priority=priority,
            lead=lead,
            sync_to_followup=sync_to_followup,
            status=TaskReminder.Status.PENDING,
        )
        
        # If synced to followup, update lead's next followup
        if sync_to_followup and lead:
            lead.next_followup_date = due_date
            if due_time:
                lead.next_followup_time = due_time
            lead.save(update_fields=['next_followup_date', 'next_followup_time'])
            
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
            
    # Filter leads that have any telecaller remarks or call logs
    leads = leads.filter(
        Q(custom_data__remark_1__isnull=False, custom_data__remark_1__gt="") |
        Q(custom_data__remark_2__isnull=False, custom_data__remark_2__gt="") |
        Q(custom_data__remark_3__isnull=False, custom_data__remark_3__gt="") |
        Q(custom_data__calling_date_remark_1__isnull=False, custom_data__calling_date_remark_1__gt="") |
        Q(custom_data__calling_date_remark_2__isnull=False, custom_data__calling_date_remark_2__gt="") |
        Q(custom_data__calling_date_remark_3__isnull=False, custom_data__calling_date_remark_3__gt="") |
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
        leads = leads.filter(
            Q(custom_data__calling_date_remark_1=call_date) |
            Q(custom_data__calling_date_remark_2=call_date) |
            Q(custom_data__calling_date_remark_3=call_date) |
            Q(followups__followup_date=call_date)
        )
        
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
        
    # Search / User filter for tasks
    task_user_filter = request.GET.get('user', '').strip()
    if task_user_filter:
        task_reports_qs = task_reports_qs.filter(user__username=task_user_filter)
        
    date_filter = request.GET.get('date', '').strip()
    if date_filter:
        task_reports_qs = task_reports_qs.filter(reported_at__date=date_filter)
        
    task_reports = task_reports_qs.select_related('user', 'lead').order_by('-reported_at')
    
    # 2. Daily Calling & EOD Reports submitted by Employees
    daily_reports_qs = DailyReport.objects.all()
    if hospital:
        daily_reports_qs = daily_reports_qs.filter(user__hospital=hospital)
    if task_user_filter:
        daily_reports_qs = daily_reports_qs.filter(user__username=task_user_filter)
    if date_filter:
        daily_reports_qs = daily_reports_qs.filter(report_date=date_filter)
    daily_reports = daily_reports_qs.select_related('user').order_by('-report_date')
    
    # Stats
    total_task_reports = task_reports_qs.count()
    total_daily_reports = daily_reports_qs.count()
    
    # Telecallers / Employees for filter dropdown
    employees = User.objects.filter(is_active=True)
    if hospital:
        employees = employees.filter(hospital=hospital)
        
    # Pagination for Task Reports
    paginator = Paginator(task_reports, 15)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1) if hasattr(paginator, 'get_elided_page_range') else paginator.page_range
    
    query_params = request.GET.copy()
    if 'page' in query_params:
        del query_params['page']

    context = {
        'active': 'reports',
        'task_reports': page_obj,
        'page_obj': page_obj,
        'page_range': page_range,
        'daily_reports': daily_reports[:10],
        'total_task_reports': total_task_reports,
        'total_daily_reports': total_daily_reports,
        'employees': employees,
        'selected_user': task_user_filter,
        'selected_date': date_filter,
        'query_params': query_params.urlencode(),
    }
    return render(request, "dashboard/admin_reports.html", context)
