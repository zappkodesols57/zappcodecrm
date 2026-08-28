from django.core.paginator import Paginator
import json
from datetime import timedelta

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
    """Dedicated specialized dashboard for Hospital Super Admins."""
    from accounts.models import User
    from django.core.exceptions import PermissionDenied
    from django.db.models import Count, Sum
    import json

    if request.user.role not in (User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER):
        raise PermissionDenied("This dashboard is restricted to Business Admins & Managers.")
    
    # If the user is a tenant, they must have a hospital.
    # Zappcode admins (no hospital) are also allowed to view this as an aggregated dashboard.

    today = timezone.localdate()
    user = request.user

    if user.hospital:
        base_leads = Lead.objects.filter(is_archived=False, hospital=user.hospital)
    else:
        base_leads = Lead.objects.filter(is_archived=False)

    # Dynamic Date Range & Time Period Filters
    # Options: 'all', 'today', 'this_month', 'year_YYYY' (e.g. 2026, 2025, 2024...), 'custom'
    time_filter = request.GET.get('time_filter', '').strip()
    custom_start = request.GET.get('start_date', '').strip()
    custom_end = request.GET.get('end_date', '').strip()
    
    # Get all distinct years available in data
    from django.db.models.functions import ExtractYear
    db_years = list(base_leads.exclude(inquiry_date__isnull=True).annotate(y=ExtractYear('inquiry_date')).values_list('y', flat=True).distinct())
    if today.year not in db_years:
        db_years.append(today.year)
    available_years = sorted(list(set([y for y in db_years if y])), reverse=True)

    filter_label = "All Time"
    if time_filter == 'today':
        base_leads = base_leads.filter(Q(inquiry_date=today) | (Q(inquiry_date__isnull=True) & Q(created_at__date=today)))
        filter_label = f"Today ({today.strftime('%d %b %Y')})"
    elif time_filter == 'this_month':
        base_leads = base_leads.filter(
            Q(inquiry_date__year=today.year, inquiry_date__month=today.month) |
            (Q(inquiry_date__isnull=True) & Q(created_at__year=today.year, created_at__month=today.month))
        )
        filter_label = f"This Month ({today.strftime('%B %Y')})"
    elif time_filter.startswith('year_'):
        try:
            sel_year = int(time_filter.replace('year_', ''))
            base_leads = base_leads.filter(
                Q(inquiry_date__year=sel_year) |
                (Q(inquiry_date__isnull=True) & Q(created_at__year=sel_year))
            )
            filter_label = f"Year {sel_year}"
        except ValueError:
            pass
    elif time_filter == 'custom':
        filter_label = "Custom Range"
        if custom_start:
            base_leads = base_leads.filter(Q(inquiry_date__gte=custom_start) | (Q(inquiry_date__isnull=True) & Q(created_at__date__gte=custom_start)))
        if custom_end:
            # Ensure custom_end cannot exceed today
            if custom_end > today.isoformat():
                custom_end = today.isoformat()
            base_leads = base_leads.filter(Q(inquiry_date__lte=custom_end) | (Q(inquiry_date__isnull=True) & Q(created_at__date__lte=custom_end)))
        if custom_start and custom_end:
            filter_label = f"{custom_start} to {custom_end}"
        elif custom_start:
            filter_label = f"From {custom_start}"
        elif custom_end:
            filter_label = f"Up to {custom_end}"

    gender_filter = request.GET.get('gender')
    if gender_filter:
        base_leads = base_leads.filter(Q(custom_data__gender__iexact=gender_filter) | Q(nelson_data__gender__iexact=gender_filter))
        
    source_filter = request.GET.get('source')
    if source_filter:
        base_leads = base_leads.filter(lead_source__name__iexact=source_filter)
        
    priority_filter = request.GET.get('priority')
    if priority_filter:
        base_leads = base_leads.filter(Q(custom_data__priority__iexact=priority_filter) | Q(nelson_data__priority__iexact=priority_filter))
        
    campaign_filter = request.GET.get('campaign')
    if campaign_filter:
        base_leads = base_leads.filter(campaign__name__iexact=campaign_filter)

    department_filter = request.GET.get('department')
    if department_filter:
        base_leads = base_leads.filter(Q(custom_data__department__iexact=department_filter) | Q(nelson_data__department__iexact=department_filter))

    doctor_filter = request.GET.get('doctor')
    if doctor_filter:
        base_leads = base_leads.filter(Q(custom_data__doctor__iexact=doctor_filter) | Q(nelson_data__doctor__iexact=doctor_filter))

    total_leads = base_leads.count()
    appts_booked = base_leads.filter(
        Q(custom_data__appointment_status__icontains='Booked') |
        Q(custom_data__appointment_status__icontains='Complete') |
        Q(nelson_data__appo_book__iexact='YES')
    ).count()
    conv_rate = round(appts_booked / total_leads, 2) if total_leads > 0 else 0.0

    # Calculate total revenue from both custom_data and nelson_data
    total_rev_calc = 0.0
    for l in base_leads:
        cd = l.custom_data or {}
        tot = cd.get('total')
        if tot:
            try:
                total_rev_calc += float(tot)
            except (ValueError, TypeError):
                pass
    nelson_rev = float(base_leads.aggregate(s=Sum('nelson_data__total'))['s'] or 0.0)
    total_revenue = total_rev_calc + nelson_rev
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
        "today_str": today.isoformat(),
        "now": timezone.now(),
        "insights_json": json.dumps(insights),
        "insights": insights,
        "time_filter": time_filter,
        "custom_start": custom_start,
        "custom_end": custom_end,
        "available_years": available_years,
        "filter_label": filter_label,
        "has_active_filters": any([gender_filter, source_filter, priority_filter, campaign_filter, department_filter, doctor_filter, time_filter, custom_start, custom_end]),
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
                cost = request.POST.get('cost', 0) or 0
                landing_page = request.POST.get('landing_page', '').strip()
                start_date = request.POST.get('start_date') or None
                end_date = request.POST.get('end_date') or None
                
                if name:
                    Campaign.objects.create(
                        hospital=hospital,
                        name=name,
                        platform=platform,
                        campaign_id=campaign_id_code,
                        ad_set=ad_set,
                        ad_name=ad_name,
                        cost=cost,
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
                    
                camp.name = request.POST.get('name', camp.name).strip()
                camp.platform = request.POST.get('platform', camp.platform).strip()
                camp.campaign_id = request.POST.get('campaign_id_code', camp.campaign_id).strip()
                camp.ad_set = request.POST.get('ad_set', camp.ad_set).strip()
                camp.ad_name = request.POST.get('ad_name', camp.ad_name).strip()
                camp.cost = request.POST.get('cost', camp.cost) or 0
                camp.landing_page = request.POST.get('landing_page', camp.landing_page).strip()
                camp.start_date = request.POST.get('start_date') or None
                camp.end_date = request.POST.get('end_date') or None
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
        else:
            campaigns_qs = Campaign.objects.all().order_by('-is_active', '-id')

        campaigns_data = []
        total_leads_count = 0
        for c in campaigns_qs:
            leads_cnt = Lead.objects.filter(Q(campaign=c) | Q(custom_data__campaign=c.name)).count()
            total_leads_count += leads_cnt
            campaigns_data.append({
                "obj": c,
                "leads_count": leads_cnt
            })
            
        total_appts = Appointment.objects.filter(hospital=hospital).count() if hospital else 0

        return render(request, "dashboard/campaign_management.html", {
            "title": "Campaign Management",
            "active": "campaign-management",
            "campaigns_data": campaigns_data,
            "total_campaigns": campaigns_qs.count(),
            "active_campaigns_count": campaigns_qs.filter(is_active=True).count(),
            "total_leads_generated": total_leads_count,
            "total_appts_generated": total_appts,
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

    if report_instance:
        # Already submitted → show locked confirmation page, no form
        return render(request, "dashboard/daily_report_done.html", {
            "active": "daily_report_submit",
            "report": report_instance,
            "report_date": report_date,
        })

    # ── Not yet submitted → compute suggestions from today's actions ─────────────
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
    appointments_booked_cnt = Appointment.objects.filter(
        lead__assigned_to=request.user,
        appointment_date=report_date
    ).filter(status__in=[AppointmentStatus.APPROVED, AppointmentStatus.SCHEDULED, AppointmentStatus.PENDING_APPROVAL]).count()

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

    # 5. Login / Logout times from AuditLog (with smart fallback to activity timestamps)
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
        # Check earliest action of the day
        earliest_log = AuditLog.objects.filter(user=request.user, created_at__date=report_date).order_by("created_at").first()
        first_login_time = earliest_log.created_at if earliest_log else timezone.now()

    last_logout_log = AuditLog.objects.filter(
        user=request.user, 
        action="USER_LOGOUT", 
        created_at__date=report_date
    ).order_by("-created_at").first()
    last_logout_time = last_logout_log.created_at if last_logout_log else None
    
    # 6. Auto-calculate academic metrics: Admissions Done today and Fees Payments Collected today
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
        if DailyReport.objects.filter(user=request.user, report_date=report_date).exists():
            messages.warning(request, "Report already submitted for today.")
            return redirect("dashboard:submit_daily_report")

        form = FormClass(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    cleaned = form.cleaned_data
                    report = DailyReport.objects.create(
                        user=request.user,
                        report_date=report_date,
                        leads_assigned=cleaned.get("leads_assigned", leads_assigned_cnt),
                        appointments_booked=cleaned.get("appointments_booked", appointments_booked_cnt),
                        freeze_leads=cleaned.get("freeze_leads", freeze_leads_cnt),
                        calls_attended=cleaned.get("calls_attended", 0),
                        outgoing_calls=cleaned.get("outgoing_calls", 0),
                        incoming_calls=cleaned.get("incoming_calls", 0),
                        calls_not_connected=cleaned.get("calls_not_connected", 0),
                        follow_ups_taken=cleaned.get("follow_ups_taken", follow_ups_taken_cnt),
                        follow_ups_pending=cleaned.get("follow_ups_pending", 0),
                        leads_cold=cleaned.get("leads_cold", 0),
                        leads_interested=cleaned.get("leads_interested", 0),
                        leads_visited=cleaned.get("leads_visited", 0),
                        admissions_done=cleaned.get("admissions_done", admissions_today_cnt),
                        fees_collected=cleaned.get("fees_collected", fees_today_sum),
                        key_highlight=cleaned.get("key_highlight", ""),
                        challenges_faced=cleaned.get("challenges_faced", ""),
                        tomorrow_priority=cleaned.get("tomorrow_priority", ""),
                        other_updates=cleaned.get("other_updates", ""),
                        mood_rating=cleaned.get("mood_rating", 3),
                        first_login_at=first_login_time,
                        last_logout_at=last_logout_time,
                    )
                    
                    # Send Notifications to recipient (Reports To / Admin)
                    target_recipients = recipients if recipients else admin_qs
                    for r_user in target_recipients:
                        Notification.objects.create(
                            user=r_user,
                            title=f"EOD Report from {request.user.get_full_name() or request.user.username}",
                            message=f"{request.user.get_full_name() or request.user.username} submitted Daily EOD Report for {report_date.strftime('%d %b %Y')}. (Assigned: {report.leads_assigned}, Admissions: {report.admissions_done}, Calls: {report.calls_attended})",
                            link="/dashboard/reports/admin/",
                        )

                messages.success(request, f"Daily report for {report_date.strftime('%d-%m-%Y')} submitted and sent to Administration/Manager successfully! ✅")
                return redirect("dashboard:submit_daily_report")
            except IntegrityError:
                messages.warning(request, "Report already submitted for today.")
                return redirect("dashboard:submit_daily_report")
    else:
        init_data = {
            "leads_assigned": suggestions["leads_assigned"],
            "calls_attended": suggestions["calls_attended"],
            "outgoing_calls": suggestions["outgoing_calls"],
            "incoming_calls": suggestions["incoming_calls"],
            "calls_not_connected": suggestions["calls_not_connected"],
            "follow_ups_taken": suggestions["follow_ups_taken"],
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
    from leads.models import Lead, LeadTemperature
    from dashboard.models import TaskReminder
    from followups.models import FollowUp
    from datetime import date
    
    if request.user.role != User.Role.LEAD_ATTENDENT or not request.user.hospital:
        messages.error(request, "Access denied.")
        return redirect("dashboard:home")
        
    user = request.user
    today_date = timezone.localdate()
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_str = today_date.strftime("%Y-%m-%d")
    today_alt_str = today_date.strftime("%d-%m-%Y")
    
    # 1. Calls Today (Leads edited/called/interacted by this user today)
    calls_today_count = Lead.objects.filter(
        hospital=user.hospital,
        assigned_to=user,
    ).filter(
        Q(updated_at__gte=today_start) |
        Q(custom_data__last_called_date=today_str) |
        Q(custom_data__calling_date_remark_1=today_str) |
        Q(custom_data__calling_date_remark_1=today_alt_str) |
        Q(custom_data__calling_date_remark_2=today_str) |
        Q(custom_data__calling_date_remark_2=today_alt_str) |
        Q(custom_data__calling_date_remark_3=today_str) |
        Q(custom_data__calling_date_remark_3=today_alt_str) |
        Q(followups__followup_date=today_date, followups__created_by=user)
    ).distinct().count()
    
    # 2. Appointments Booked / Confirmed by Doctors for this user's leads
    from leads.models import Appointment, AppointmentStatus
    appts_today_count = Appointment.objects.filter(
        lead__hospital=user.hospital,
        lead__assigned_to=user,
        status__in=[AppointmentStatus.APPROVED, AppointmentStatus.SCHEDULED, AppointmentStatus.COMPLETED]
    ).values('lead').distinct().count()
    
    if appts_today_count == 0:
        appts_today_count = Lead.objects.filter(
            hospital=user.hospital,
            assigned_to=user,
        ).filter(
            Q(custom_data__appointment_status__in=['Booked', 'Completed', 'Payment Done']) |
            Q(custom_data__appointment_confirmed_at__startswith=today_str)
        ).count()
    
    # 3. New Hot Leads Added/Imported Today (Received in Hospital today)
    hot_leads_today_count = Lead.objects.filter(
        hospital=user.hospital,
        is_archived=False,
    ).filter(
        Q(inquiry_date=today_date) |
        Q(created_at__gte=today_start)
    ).count()
    
    # 4. Overdue & Pending Follow-ups (Pending tasks/follow-ups due today or overdue)
    pending_followups_count = Lead.objects.filter(
        hospital=user.hospital,
        assigned_to=user,
        is_archived=False,
    ).exclude(
        deal_status__in=['WON', 'LOST']
    ).filter(
        Q(next_followup_date__lte=today_date) |
        Q(custom_data__calling_date_remark_1__lte=today_str, custom_data__calling_date_remark_1__gt="") |
        Q(custom_data__calling_date_remark_2__lte=today_str, custom_data__calling_date_remark_2__gt="") |
        Q(custom_data__calling_date_remark_3__lte=today_str, custom_data__calling_date_remark_3__gt="")
    ).distinct().count()
    
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
        'pending_followups_count': pending_followups_count,
        'overdue_followups_count': pending_followups_count,
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
                "Assigned Staff": lead.assigned_to.get_full_name() if lead.assigned_to else 'Unassigned',
                "Location": lead.location or lead.city or '',
            })
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
            cd['appointment_status'] = 'Booked'
            cd['appointment_confirmed_at'] = timezone.now().strftime('%Y-%m-%d %H:%M')
            lead.custom_data = cd
            lead.save(update_fields=['custom_data'])

            # Notify Lead Attendant
            if lead.assigned_to:
                Notification.objects.create(
                    user=lead.assigned_to,
                    title="Appointment Approved by Doctor",
                    message=f"Dr. {doctor.get_full_name() or doctor.username} confirmed and booked appointment for patient {lead.name} on {date_str} at {time_str}.",
                    link=f"/leads/{lead.pk}/",
                )

            messages.success(request, f"Appointment for {lead.name} on {date_str} at {time_str} approved and Booked! Notification sent to Lead Attendant.")

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
    
    today_apts = doctor_apts.filter(appointment_date=today)
    pending_apts = doctor_apts.filter(status=AppointmentStatus.PENDING_APPROVAL)
    upcoming_apts = doctor_apts.filter(appointment_date__gt=today).exclude(status=AppointmentStatus.CANCELLED)
    
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
    return render(request, "dashboard/doctor_home.html", context)


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
            cd['appointment_status'] = 'Booked'
            cd['appointment_confirmed_at'] = timezone.now().strftime('%Y-%m-%d %H:%M')
            lead.custom_data = cd
            lead.save(update_fields=['custom_data'])

            if lead.assigned_to:
                Notification.objects.create(
                    user=lead.assigned_to,
                    title="Appointment Approved by Doctor",
                    message=f"Dr. {doctor.get_full_name() or doctor.username} confirmed and booked appointment for patient {lead.name} on {date_str} at {time_str}.",
                    link=f"/leads/{lead.pk}/",
                )
            messages.success(request, f"Appointment for {lead.name} on {date_str} at {time_str} approved and Booked!")

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
    today_apts = doctor_apts.filter(appointment_date=today).exclude(status=AppointmentStatus.CANCELLED)
    upcoming_apts = doctor_apts.filter(appointment_date__gt=today).exclude(status=AppointmentStatus.CANCELLED)
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
                "Priority": cd.get('priority', '') or lead.get_temperature_display(),
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
    
    # Query followups made today in the same hospital by other users (excluding current user or optionally all team members)
    followups = FollowUp.objects.filter(
        lead__hospital=request.user.hospital,
        followup_date=today
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
