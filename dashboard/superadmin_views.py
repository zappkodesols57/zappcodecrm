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

@login_required
def superadmin_home(request):
    """
    Dedicated interactive analytics dashboard for SuperAdmins, Admins, and Managers.
    Includes top multi-dimension filter bar (Industry, Business, Date, Stage, Deal Status, Temperature),
    5 core KPI cards (New Leads/Unassigned, Call Not Done, Won Leads, Follow-ups Breakdown, Total Revenue),
    clean 2-color theme, and synchronized interactive charts.
    """
    from accounts.models import User, Hospital
    from leads.models import Lead, DealStatus, LeadStage, LeadTemperature
    from django.core.exceptions import PermissionDenied
    from django.db.models import Count, Sum
    import json
    import calendar
    from datetime import datetime, date
    from collections import defaultdict

    if not request.user.is_authenticated or not hasattr(request.user, 'role') or request.user.role not in (User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER):
        return redirect("dashboard:home")

    # If this is a Hospital Admin/Manager accessing /dashboard/superadmin/, route cleanly to /dashboard/hospital/admin/
    if request.user.hospital and request.path.rstrip('/') == '/dashboard/superadmin':
        query = request.GET.urlencode()
        return redirect(f"{reverse('dashboard:hospital_admin_home')}?{query}" if query else "dashboard:hospital_admin_home")

    today = timezone.localdate()
    user = request.user

    # 1. Base Business & Tenant Scope
    raw_biz = request.GET.get("business", "").strip()
    raw_hosp = request.GET.get("hospital", "").strip()
    industry_filter = request.GET.get("industry", "").strip()
    session_biz = str(getattr(request, 'session', {}).get("active_business_id", "")).strip()

    if user.hospital:
        # Tenant Admin / Manager: strictly restricted to their assigned business
        effective_hospital = user.hospital
        selected_hospital_id = str(user.hospital.id)
        base_leads = Lead.objects.filter(is_archived=False, hospital=user.hospital)
        if user.role == User.Role.MANAGER and user.branch:
            b_name = user.branch.name
            base_leads = base_leads.filter(
                Q(custom_data__hospital_branch__iexact=b_name) |
                Q(custom_data__branch__iexact=b_name) |
                Q(custom_data__dyn_hospital_branch__iexact=b_name) |
                Q(custom_data__dyn_branch__iexact=b_name) |
                Q(assigned_to__branch=user.branch)
            )
    else:
        selected_hospital_id = raw_biz or raw_hosp or session_biz
        if selected_hospital_id and selected_hospital_id.isdigit():
            effective_hospital = Hospital.objects.filter(id=int(selected_hospital_id)).first()
            base_leads = Lead.objects.filter(is_archived=False, hospital_id=int(selected_hospital_id))
        elif selected_hospital_id in ("none", "zappcode"):
            effective_hospital = None
            base_leads = Lead.objects.filter(is_archived=False, hospital__isnull=True)
        else:
            effective_hospital = None
            selected_hospital_id = ""
            base_leads = Lead.objects.filter(is_archived=False)

        # Apply industry filter for global SuperAdmin
        if industry_filter:
            base_leads = base_leads.filter(hospital__industry=industry_filter)

    # Master lists for dropdowns - strictly scoped to business/industry
    all_businesses = Hospital.objects.filter(is_active=True).order_by("name")
    industry_choices = Hospital.Industry.choices
    
    # Telecallers strictly for this hospital/business
    if effective_hospital:
        telecaller_users = User.objects.filter(
            hospital=effective_hospital,
            role__in=[User.Role.LEAD_ATTENDENT, User.Role.COUNSELLOR, User.Role.HR],
            is_active=True
        ).order_by('first_name', 'username')
    else:
        telecaller_users = User.objects.filter(
            role__in=[User.Role.LEAD_ATTENDENT, User.Role.COUNSELLOR, User.Role.HR],
            is_active=True
        ).order_by('first_name', 'username')

    # Stages choices
    stage_choices = list(LeadStage.objects.filter(is_active=True).values_list('name', flat=True).distinct().order_by('order', 'name'))
    
    deal_status_choices = [
        ("NEW", "New"),
        ("OPEN", "Open"),
        ("PENDING", "Pending"),
        ("WON", "Won"),
        ("LOST", "Lost"),
    ]
    temperature_choices = [
        ("HOT", "Hot"),
        ("WARM", "Warm"),
        ("COLD", "Cold"),
        ("FREEZE", "Freeze"),
    ]

    # Dynamic filter caches strictly for this business
    from django.core.cache import cache
    cache_scope = f"{selected_hospital_id or 'all'}_{industry_filter or 'all'}"
    cache_key = f"dash_filters_v5_{cache_scope}"
    filter_cache_data = cache.get(cache_key)

    if not filter_cache_data:
        raw_campaign_set = set()
        raw_source_set = set()
        raw_dept_set = set()
        raw_doc_set = set()
        raw_loc_set = set()
        db_years_set = set()

        # Gather choices from Lead database
        for row in base_leads.order_by().values('location', 'campaign__name', 'lead_source__name', 'custom_data', 'inquiry_date', 'created_at'):
            c_rel = row.get('campaign__name')
            if c_rel and str(c_rel).strip().lower() not in ['nan', 'none', '']:
                raw_campaign_set.add(str(c_rel).strip())
            s_rel = row.get('lead_source__name')
            if s_rel and str(s_rel).strip().lower() not in ['nan', 'none', '']:
                raw_source_set.add(str(s_rel).strip())
            loc_col = row.get('location')
            if loc_col:
                loc_s = str(loc_col).strip()
                if loc_s.lower() not in ['nan', 'none', 'not mentioned', ''] and not any(u in loc_s.lower() for u in ['http:', 'https:', 'www.', 'facebook.com', 'fb.com', 'instagram.com', 'youtube.com']):
                    raw_loc_set.add(loc_s.title())

            inq_d = row.get('inquiry_date')
            if inq_d:
                db_years_set.add(inq_d.year)
            elif row.get('created_at'):
                db_years_set.add(row.get('created_at').year)

            cd = row.get('custom_data') or {}
            if isinstance(cd, dict):
                c_custom = cd.get('campaign')
                if c_custom and str(c_custom).strip().lower() not in ['nan', 'none', '']:
                    raw_campaign_set.add(str(c_custom).strip())
                s_custom = cd.get('lead_source')
                if s_custom and str(s_custom).strip().lower() not in ['nan', 'none', '']:
                    raw_source_set.add(str(s_custom).strip())
                d_custom = cd.get('department')
                if d_custom and str(d_custom).strip().lower() not in ['nan', 'none', '']:
                    raw_dept_set.add(str(d_custom).strip().upper())
                loc_custom = cd.get('location')
                if loc_custom:
                    loc_cs = str(loc_custom).strip()
                    if loc_cs.lower() not in ['nan', 'none', 'not mentioned', ''] and not any(u in loc_cs.lower() for u in ['http:', 'https:', 'www.', 'facebook.com', 'fb.com', 'instagram.com', 'youtube.com']):
                        raw_loc_set.add(loc_cs.title())
                d_entry = cd.get('doctor')
                if d_entry and str(d_entry).strip().lower() not in ['nan', 'none', 'not mentioned', 'docotor', 'doctor', '']:
                    for single_d in str(d_entry).split(','):
                        d_clean = single_d.strip().title()
                        if d_clean and d_clean.lower() not in ['not mentioned', 'docotor', 'doctor', 'nan', 'none']:
                            raw_doc_set.add(d_clean)

        # Include official HospitalDepartment and HospitalDoctor records if applicable
        from leads.models import HospitalDepartment, HospitalDoctor
        if effective_hospital:
            for dept_obj in HospitalDepartment.objects.filter(hospital=effective_hospital, is_active=True):
                if dept_obj.name:
                    raw_dept_set.add(dept_obj.name.strip().upper())
            for doc_obj in HospitalDoctor.objects.filter(hospital=effective_hospital, is_active=True):
                if doc_obj.name:
                    raw_doc_set.add(doc_obj.name.strip().title())

        raw_campaigns = sorted(list(raw_campaign_set))
        raw_sources = sorted(list(raw_source_set))
        raw_departments = sorted(list(raw_dept_set))
        raw_doctors = sorted(list(raw_doc_set))
        raw_locations = sorted(list(raw_loc_set))
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
            "raw_weekdays": raw_weekdays,
            "raw_months": raw_months,
            "available_years": available_years,
        }
        cache.set(cache_key, filter_cache_data, 600)
    else:
        raw_campaigns = filter_cache_data["raw_campaigns"]
        raw_sources = filter_cache_data["raw_sources"]
        raw_departments = filter_cache_data["raw_departments"]
        raw_doctors = filter_cache_data["raw_doctors"]
        raw_locations = filter_cache_data["raw_locations"]
        raw_weekdays = filter_cache_data["raw_weekdays"]
        raw_months = filter_cache_data["raw_months"]
        available_years = filter_cache_data["available_years"]

    # 2. Extract GET Filter Parameters
    search_query = request.GET.get('q', '').strip()
    time_filter = request.GET.get('time_filter', '').strip()
    custom_start = request.GET.get('start_date', '').strip()
    custom_end = request.GET.get('end_date', '').strip()
    stage_filter = request.GET.get('stage', '').strip()
    deal_status_filter = request.GET.get('deal_status', '').strip()
    telecaller_filter = request.GET.get('telecaller', '').strip()
    temperature_filter = request.GET.get('temperature', '').strip()
    campaign_filter = request.GET.get('campaign', '').strip()
    source_filter = request.GET.get('source', '').strip()
    department_filter = request.GET.get('department', '').strip()
    doctor_filter = request.GET.get('doctor', '').strip()
    location_filter = request.GET.get('location', '').strip()
    final_status_filter = request.GET.get('final_lead_status', '').strip()

    # Search Query
    if search_query:
        base_leads = base_leads.filter(
            Q(name__icontains=search_query) |
            Q(mobile__icontains=search_query) |
            Q(email__icontains=search_query) |
            Q(lead_code__icontains=search_query) |
            Q(custom_data__icontains=search_query)
        )

    # 3. Apply Multi-Dimension Filters:
    # Telecaller / Staff filter
    if telecaller_filter:
        if telecaller_filter.isdigit():
            base_leads = base_leads.filter(assigned_to_id=int(telecaller_filter))
        elif telecaller_filter.lower() == 'unassigned':
            base_leads = base_leads.filter(assigned_to__isnull=True)
        else:
            base_leads = base_leads.filter(
                Q(assigned_to__username__iexact=telecaller_filter) |
                Q(assigned_to__first_name__icontains=telecaller_filter) |
                Q(assigned_to__last_name__icontains=telecaller_filter)
            )

    # Apply Stage Filter
    if stage_filter:
        base_leads = base_leads.filter(Q(stage__name__iexact=stage_filter) | Q(custom_data__stage__iexact=stage_filter))

    # Apply Deal Status Filter
    if deal_status_filter:
        ds_up = deal_status_filter.upper()
        if ds_up == 'WON':
            base_leads = base_leads.filter(
                Q(deal_status=DealStatus.WON) |
                Q(custom_data__total_paid__gt='0') |
                Q(custom_data__total__gt='0') |
                Q(custom_data__deal_status__icontains='won') |
                Q(admission_status='WON') |
                Q(admission_status='ADMISSION_DONE')
            )
        elif ds_up == 'LOST':
            base_leads = base_leads.filter(
                Q(deal_status=DealStatus.LOST) |
                Q(temperature=LeadTemperature.FREEZE) |
                Q(custom_data__deal_status__icontains='lost') |
                Q(custom_data__appointment_status__icontains='lost') |
                Q(custom_data__appointment_status__icontains='cancel')
            )
        elif ds_up == 'NEW':
            base_leads = base_leads.filter(
                Q(assigned_to__isnull=True) | Q(deal_status='New') | Q(stage__name__iexact='New')
            )
        elif ds_up == 'PENDING':
            base_leads = base_leads.filter(
                Q(next_followup_date__isnull=False) |
                Q(custom_data__appointment_status__icontains='follow') |
                Q(custom_data__appointment_status__icontains='book')
            ).exclude(deal_status__in=[DealStatus.WON, DealStatus.LOST])
        elif ds_up == 'OPEN':
            base_leads = base_leads.filter(
                deal_status__in=[DealStatus.OPEN, 'Open', 'OPEN']
            ).exclude(deal_status__in=[DealStatus.WON, DealStatus.LOST])
        else:
            base_leads = base_leads.filter(
                Q(deal_status__iexact=deal_status_filter) |
                Q(custom_data__deal_status__iexact=deal_status_filter)
            )

    # Apply Temperature Filter
    if temperature_filter:
        base_leads = base_leads.filter(
            Q(temperature__iexact=temperature_filter) |
            Q(custom_data__temperature__iexact=temperature_filter) |
            Q(custom_data__priority__iexact=temperature_filter)
        )

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

    # Final Lead Status slicer
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

    # 4. Quick Date Filter & Date Range Logic
    if custom_start or custom_end:
        time_filter = 'custom'
    elif not time_filter:
        time_filter = 'today'

    start_of_today = timezone.make_aware(datetime.combine(today, datetime.min.time()))
    end_of_today = timezone.make_aware(datetime.combine(today, datetime.max.time()))
    today_str = today.isoformat()

    start_of_month = timezone.make_aware(datetime(today.year, today.month, 1, 0, 0, 0))
    _, last_day = calendar.monthrange(today.year, today.month)
    end_of_month = timezone.make_aware(datetime(today.year, today.month, last_day, 23, 59, 59))
    start_date_month = date(today.year, today.month, 1)
    end_date_month = date(today.year, today.month, last_day)

    date_range_start_dt = None
    date_range_end_dt = None
    filter_label = "Today"

    if time_filter == 'today':
        base_leads = base_leads.filter(
            Q(created_at__range=(start_of_today, end_of_today)) | Q(inquiry_date=today)
        )
        date_range_start_dt = start_of_today
        date_range_end_dt = end_of_today
        filter_label = f"Today ({today.strftime('%d %b %Y')})"
    elif time_filter in ('last_7_days', '7days'):
        d7 = today - timedelta(days=7)
        d7_start = timezone.make_aware(datetime.combine(d7, datetime.min.time()))
        base_leads = base_leads.filter(
            Q(created_at__range=(d7_start, end_of_today)) | Q(inquiry_date__range=(d7, today))
        )
        date_range_start_dt = d7_start
        date_range_end_dt = end_of_today
        filter_label = f"Last 7 Days ({d7.strftime('%d %b')} - {today.strftime('%d %b')})"
    elif time_filter in ('last_30_days', '30days'):
        d30 = today - timedelta(days=30)
        d30_start = timezone.make_aware(datetime.combine(d30, datetime.min.time()))
        base_leads = base_leads.filter(
            Q(created_at__range=(d30_start, end_of_today)) | Q(inquiry_date__range=(d30, today))
        )
        date_range_start_dt = d30_start
        date_range_end_dt = end_of_today
        filter_label = f"Last 30 Days ({d30.strftime('%d %b')} - {today.strftime('%d %b')})"
    elif time_filter == 'this_month':
        base_leads = base_leads.filter(
            Q(created_at__range=(start_of_month, end_of_month)) |
            Q(inquiry_date__range=(start_date_month, end_date_month))
        )
        date_range_start_dt = start_of_month
        date_range_end_dt = end_of_month
        filter_label = f"This Month ({today.strftime('%B %Y')})"
    elif time_filter in ('all_time', 'all'):
        filter_label = "All Time"
    elif time_filter == 'custom':
        filter_label = "Custom Range"
        if custom_start:
            base_leads = base_leads.filter(Q(inquiry_date__gte=custom_start) | (Q(inquiry_date__isnull=True) & Q(created_at__date__gte=custom_start)))
            try:
                cs_d = datetime.strptime(custom_start, '%Y-%m-%d').date()
                date_range_start_dt = timezone.make_aware(datetime.combine(cs_d, datetime.min.time()))
            except ValueError:
                pass
        if custom_end:
            base_leads = base_leads.filter(Q(inquiry_date__lte=custom_end) | (Q(inquiry_date__isnull=True) & Q(created_at__date__lte=custom_end)))
            try:
                ce_d = datetime.strptime(custom_end, '%Y-%m-%d').date()
                date_range_end_dt = timezone.make_aware(datetime.combine(ce_d, datetime.max.time()))
            except ValueError:
                pass
        if custom_start and custom_end:
            filter_label = f"{custom_start} to {custom_end}"

    hospital_all_leads = base_leads

    # =========================================================================
    # 10 CORE REFINED KPIs (Calculated strictly on the filtered hospital dataset)
    # =========================================================================
    
    # 1. New Leads: All leads count in filtered date range / filters
    new_leads_count = hospital_all_leads.count()
    unassigned_count = hospital_all_leads.filter(assigned_to__isnull=True).count()
    assigned_leads_count = max(0, new_leads_count - unassigned_count)

    # 2. Call Not Done: Fresh or assigned leads untouched by users (exclude system remarks)
    cnd_matched_ids = filter_uncontacted_leads_ids(hospital_all_leads, today=today)
    call_not_done_count = len(cnd_matched_ids)

    # 3. Appointment Booked: Leads where stage or appointment status is confirmed/booked in date range
    appo_booked_qs = hospital_all_leads.filter(
        Q(custom_data__appointment_status__icontains='confirm') |
        Q(custom_data__appointment_status__icontains='book') |
        Q(custom_data__appointment_confirmed_at__isnull=False) |
        Q(stage__name__icontains='confirm') |
        Q(stage__name__icontains='booked')
    ).exclude(
        deal_status=DealStatus.LOST
    ).exclude(
        custom_data__deal_status__icontains='lost'
    ).exclude(
        custom_data__appointment_status__icontains='cancel'
    )
    appointment_booked_count = appo_booked_qs.distinct().count()

    # 4. Walk-in Leads: Leads where source or lead type is Walk-in
    walkin_qs = hospital_all_leads.filter(
        Q(lead_source__name__icontains='walk') |
        Q(custom_data__lead_source__icontains='walk') |
        Q(lead_type__icontains='walk') |
        Q(custom_data__lead_type__icontains='walk')
    )
    walkin_leads_count = walkin_qs.distinct().count()

    # 5. Appointments Scheduled: Count of appointments scheduled in the date range
    if date_range_start_dt and date_range_end_dt:
        appo_sched_qs = Appointment.objects.filter(
            lead__in=hospital_all_leads,
            appointment_date__range=(date_range_start_dt.date(), date_range_end_dt.date())
        ).exclude(status=AppointmentStatus.CANCELLED)
        appo_sched_count = appo_sched_qs.count()
        # If no Appointment model rows, fall back to leads marked as scheduled
        if appo_sched_count == 0:
            appo_sched_count = hospital_all_leads.filter(
                Q(custom_data__appo_booked_date__isnull=False) |
                Q(custom_data__appointment_date__isnull=False) |
                Q(custom_data__appointment_status__icontains='schedul')
            ).count()
    else:
        appo_sched_count = Appointment.objects.filter(
            lead__in=hospital_all_leads
        ).exclude(status=AppointmentStatus.CANCELLED).count()
        if appo_sched_count == 0:
            appo_sched_count = hospital_all_leads.filter(
                Q(custom_data__appo_booked_date__isnull=False) |
                Q(custom_data__appointment_date__isnull=False) |
                Q(custom_data__appointment_status__icontains='schedul')
            ).count()

    # 6. Admitted Patients: Count of admitted patients in date range
    admitted_qs = hospital_all_leads.filter(
        Q(admission_status__in=['ADMISSION_DONE', 'ADMITTED', 'WON']) |
        Q(custom_data__admission_status__icontains='admit') |
        Q(custom_data__appointment_status__icontains='admit') |
        Q(stage__name__icontains='admit') |
        Q(admission__isnull=False)
    )
    admitted_patients_count = admitted_qs.distinct().count()

    # 7. Won Leads: Payment completed / Won deals
    won_leads_qs = hospital_all_leads.filter(
        Q(deal_status=DealStatus.WON) |
        Q(custom_data__total_paid__gt='0') |
        Q(custom_data__total__gt='0') |
        Q(admission_status='WON') |
        Q(admission_status='ADMISSION_DONE') |
        Q(custom_data__deal_status__icontains='won') |
        Q(custom_data__deal_status__icontains='Payment Done') |
        Q(custom_data__appointment_status__icontains='Complete') |
        Q(custom_data__appointment_status__icontains='Visit Done') |
        Q(custom_data__appointment_status__icontains='Payment Done')
    ).exclude(
        deal_status=DealStatus.LOST
    ).exclude(
        custom_data__deal_status__icontains='lost'
    ).distinct()
    won_leads_count = won_leads_qs.count()

    # 8. Follow-ups Pending: Leads with pending follow-up status
    fu_pending_qs = hospital_all_leads.exclude(
        deal_status__in=[DealStatus.WON, DealStatus.LOST]
    ).filter(
        Q(followups__followup_status__in=['PENDING', 'RESCHEDULED']) |
        (Q(next_followup_date__isnull=False) & ~Q(stage__name__icontains='lost')) |
        Q(custom_data__appointment_status__icontains='follow')
    ).distinct()
    followups_pending_count = fu_pending_qs.count()

    # 9. Total Revenue & 10. Lost Leads
    total_revenue = 0.0
    lost_leads_count = hospital_all_leads.filter(
        Q(deal_status=DealStatus.LOST) |
        Q(temperature=LeadTemperature.FREEZE) |
        Q(stage__name__icontains='lost') |
        Q(custom_data__deal_status__icontains='lost') |
        Q(custom_data__appointment_status__icontains='lost') |
        Q(custom_data__appointment_status__icontains='cancel')
    ).distinct().count()

    # Dynamic Distributions & Revenue Calculation in single pass
    location_dist = {}
    month_dist = {}
    department_dist = {}
    doctor_dist = {}
    campaign_dist = {}
    source_dist = {}
    appo_status_dist = {}
    final_status_dist = {}
    year_dist = {}
    stage_dist = {}
    temperature_dist = {}

    fast_leads = hospital_all_leads.order_by().values(
        'id', 'location', 'campaign__name', 'lead_source__name', 'custom_data',
        'inquiry_date', 'created_at', 'deal_status', 'temperature', 'stage__name'
    )

    for l in fast_leads:
        cd = l.get('custom_data') or {}

        # 1. Revenue
        tot = 0.0
        try:
            val = cd.get("total_paid") or cd.get("total") or 0.0
            tot = float(val)
            if tot > 10000000:
                tot = 0.0
        except (ValueError, TypeError):
            tot = 0.0
        total_revenue += tot

        # 2. Location
        loc_raw = l.get('location') or cd.get('location') or 'Not Mentioned'
        loc_str = str(loc_raw).strip()
        if not loc_str or loc_str.lower() in ['nan', 'none', 'not mentioned', ''] or any(u in loc_str.lower() for u in ['http:', 'https:', 'www.', 'facebook.com', 'fb.com', 'instagram.com', 'youtube.com']):
            loc = 'Not Mentioned'
        else:
            loc = loc_str.title()
        location_dist[loc] = location_dist.get(loc, 0) + 1

        # 3. Month
        lead_date = l.get('created_at').date() if l.get('created_at') else (l.get('inquiry_date') or today)
        m_name = cd.get('month') or lead_date.strftime('%B')
        m_name = m_name.strip().title()
        month_dist[m_name] = month_dist.get(m_name, 0) + 1

        # 4. Department
        dept = cd.get('department') or 'General OPD'
        dept = dept.strip().upper()
        department_dist[dept] = department_dist.get(dept, 0) + 1

        # 5. Doctor
        raw_doc_str = cd.get('doctor') or 'Not Mentioned'
        if raw_doc_str in ['nan', 'None', '', 'Not Mentioned', 'DOCOTOR', 'DOCTOR']:
            doctor_dist['Not Mentioned'] = doctor_dist.get('Not Mentioned', 0) + 1
        else:
            for s_doc in str(raw_doc_str).split(','):
                s_doc_clean = s_doc.strip().title()
                if s_doc_clean and s_doc_clean not in ['Not Mentioned', 'Docotor', 'Doctor']:
                    doctor_dist[s_doc_clean] = doctor_dist.get(s_doc_clean, 0) + 1

        # 6. Campaign
        camp = l.get('campaign__name') or cd.get('campaign') or 'General Campaign'
        camp = camp.strip()
        campaign_dist[camp] = campaign_dist.get(camp, 0) + 1

        # 7. Lead Source
        src = l.get('lead_source__name') or cd.get('lead_source') or 'Inquiry'
        src = src.strip()
        source_dist[src] = source_dist.get(src, 0) + 1

        # 8. Stage & Temperature
        stg = (l.get('stage__name') or 'New').strip().title()
        stage_dist[stg] = stage_dist.get(stg, 0) + 1

        temp = (l.get('temperature') or 'HOT').strip().upper()
        temperature_dist[temp] = temperature_dist.get(temp, 0) + 1

        # 9. Appointment Status
        appo_st = cd.get('appointment_status') or 'NA'
        appo_st = str(appo_st).strip().upper()
        if not appo_st or appo_st in ['NAN', 'NONE']: appo_st = 'NA'
        appo_status_dist[appo_st] = appo_status_dist.get(appo_st, 0) + 1

        # 10. Final Lead Status
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

        # 11. Year
        yr = str(cd.get('year') or (lead_date.year if lead_date else '2026')).strip()
        year_dist[yr] = year_dist.get(yr, 0) + 1

    # Currency formatter for Indian system (Crores, Lakhs, Thousands)
    def format_indian_currency(amt):
        try:
            amt = float(amt)
        except (ValueError, TypeError):
            return "₹0"
        
        abs_amt = abs(amt)
        sign = "-" if amt < 0 else ""
        
        if abs_amt >= 10000000:  # 1 Crore+
            cr_val = abs_amt / 10000000
            return f"{sign}₹{cr_val:.2f} Cr" if cr_val % 1 != 0 else f"{sign}₹{int(cr_val)} Cr"
        elif abs_amt >= 100000:   # 1 Lakh+
            lakh_val = abs_amt / 100000
            return f"{sign}₹{lakh_val:.2f} L" if lakh_val % 1 != 0 else f"{sign}₹{int(lakh_val)} L"
        elif abs_amt >= 1000:     # 1 Thousand+
            k_val = abs_amt / 1000
            return f"{sign}₹{k_val:.1f} K" if k_val % 1 != 0 else f"{sign}₹{int(k_val)} K"
        else:
            return f"{sign}₹{int(abs_amt):,}"

    formatted_revenue = format_indian_currency(total_revenue)

    insights = {
        # 10 Requested KPIs
        "new_leads": new_leads_count,
        "total_leads": new_leads_count,
        "unassigned_leads": unassigned_count,
        "assigned_leads": assigned_leads_count,
        "call_not_done": call_not_done_count,
        "appointment_booked": appointment_booked_count,
        "walkin_leads": walkin_leads_count,
        "appointments_scheduled": appo_sched_count,
        "admitted_patients": admitted_patients_count,
        "won_leads": won_leads_count,
        "followups_pending": followups_pending_count,
        "total_revenue": total_revenue,
        "formatted_revenue": formatted_revenue,
        "lost_leads": lost_leads_count,

        # Charts Data JSON Formatted
        "location_distribution": location_dist,
        "month_distribution": month_dist,
        "department_distribution": department_dist,
        "doctor_distribution": doctor_dist,
        "campaign_distribution": campaign_dist,
        "source_distribution": source_dist,
        "stage_distribution": stage_dist,
        "temperature_distribution": temperature_dist,
        "appointment_status_distribution": appo_status_dist,
        "final_lead_status_distribution": final_status_dist,
        "year_distribution": year_dist,
    }

    has_active_filters = any([
        time_filter not in ['today', ''], custom_start, custom_end, search_query,
        industry_filter, selected_hospital_id, stage_filter, deal_status_filter, telecaller_filter,
        temperature_filter, campaign_filter, source_filter, department_filter,
        doctor_filter, location_filter, final_status_filter
    ])

    context = {
        "active": "superadmin_home",
        "today": today,
        "today_str": today.isoformat(),
        "insights": insights,
        "insights_json": json.dumps(insights),
        "filter_label": filter_label,

        # Master Dropdown options
        "businesses": all_businesses,
        "industries": industry_choices,
        "telecaller_users": telecaller_users,
        "stages": stage_choices,
        "deal_statuses": deal_status_choices,
        "temperatures": temperature_choices,
        "campaigns": raw_campaigns,
        "lead_sources": raw_sources,
        "departments": raw_departments,
        "doctors": raw_doctors,
        "locations": raw_locations,
        "months": raw_months,
        "available_years": available_years,

        # Current Selected Filter Values
        "search_query": search_query,
        "current_industry": industry_filter,
        "selected_hospital_id": selected_hospital_id,
        "current_stage": stage_filter,
        "current_deal_status": deal_status_filter,
        "current_telecaller": telecaller_filter,
        "current_temperature": temperature_filter,
        "current_campaign": campaign_filter,
        "current_source": source_filter,
        "current_department": department_filter,
        "current_doctor": doctor_filter,
        "current_location": location_filter,
        "current_final_status": final_status_filter,
        "time_filter": time_filter,
        "custom_start": custom_start,
        "custom_end": custom_end,
        "has_active_filters": has_active_filters,
    }

    # Separate template for Hospital Admin vs Global Super Admin
    if user.hospital or (effective_hospital and user.role != User.Role.SUPER_ADMIN):
        template_name = "hospital/dashboard/admin_home.html"
    else:
        template_name = "dashboard/superadmin_home.html"

    return render(request, template_name, context)


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
                # Counsellors / HR / Telecallers: see assigned leads, leads created by them, or fresh unassigned leads in their business
                # Also allow seeing new leads for current business so drilldown counts and cards match perfectly
                if card_type in ('new_leads', 'walkin'):
                    hospital_qs = hospital_qs.filter(Q(assigned_to=user) | Q(created_by=user) | Q(assigned_to__isnull=True) | Q(created_at__date=today) | Q(inquiry_date=today))
                else:
                    hospital_qs = hospital_qs.filter(Q(assigned_to=user) | Q(created_by=user) | Q(assigned_to__isnull=True))
    else:
        raw_biz = request.GET.get("business", "").strip()
        raw_hosp = request.GET.get("hospital", "").strip()
        session_biz = str(getattr(request, 'session', {}).get("active_business_id", "")).strip()
        selected_hospital_id = raw_biz or raw_hosp or session_biz

        if selected_hospital_id and selected_hospital_id.isdigit():
            hospital_qs = Lead.objects.filter(is_archived=False, hospital_id=int(selected_hospital_id))
        elif selected_hospital_id in ("zappcode", "none"):
            hospital_qs = Lead.objects.filter(is_archived=False, hospital__isnull=True)
        else:
            selected_hospital_id = ""
            hospital_qs = Lead.objects.filter(is_archived=False)
            
        if not user.can_view_all_leads:
            if user.can_view_team_leads:
                team = User.objects.filter(reports_to=user)
                hospital_qs = hospital_qs.filter(Q(assigned_to=user) | Q(assigned_to__in=team))
            elif user.role == User.Role.MANAGER:
                team = User.objects.filter(reports_to=user)
                hospital_qs = hospital_qs.filter(Q(assigned_to=user) | Q(assigned_to__in=team) | Q(assigned_to__isnull=True))
            elif user.can_view_assigned_leads or user.role in (User.Role.COUNSELLOR, User.Role.HR, User.Role.LEAD_ATTENDENT):
                if card_type in ('new_leads', 'walkin'):
                    hospital_qs = hospital_qs.filter(Q(assigned_to=user) | Q(created_by=user) | Q(assigned_to__isnull=True) | Q(created_at__date=today) | Q(inquiry_date=today))
                else:
                    hospital_qs = hospital_qs.filter(Q(assigned_to=user) | Q(created_by=user) | Q(assigned_to__isnull=True))

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

    # Cache optimization: for heavy queries (e.g. all time or month queries), check cache
    from django.core.cache import cache
    cache_key = None
    if mode in ('all', 'this_month') and not (source_filter or department_filter or doctor_filter or location_filter or gender_filter or age_group_filter or payment_type_filter or final_status_filter):
        cache_key = f"drilldown_api_{user.id}_{card_type}_{mode}_{selected_hospital_id}_{campaign_filter}_{year_param}_{month_param}"
        cached_response = cache.get(cache_key)
        if cached_response:
            return JsonResponse(cached_response)

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
        uncontacted_q = (
            Q(stage__name__icontains="new") |
            Q(deal_status="OPEN", followup_count=0) |
            Q(temperature="UNCONTACTED")
        )
        if user.role == User.Role.LEAD_ATTENDENT:
            base_card_qs = hospital_qs.filter(uncontacted_q).filter(
                Q(assigned_to=user) | Q(assigned_to__isnull=True) | Q(custom_data__lead_attendant__in=['Unassigned', '', None, 'nan'])
            ).exclude(
                deal_status__in=[DealStatus.WON, DealStatus.LOST, 'WON', 'LOST', 'CLOSED']
            ).exclude(
                admission_status__in=[AdmissionStatus.WON, 'ADMISSION_DONE', 'WON']
            ).exclude(
                admission__isnull=False
            )
        else:
            base_card_qs = hospital_qs.filter(uncontacted_q).exclude(
                deal_status__in=[DealStatus.WON, DealStatus.LOST, 'WON', 'LOST', 'CLOSED']
            ).exclude(
                admission_status__in=[AdmissionStatus.WON, 'ADMISSION_DONE', 'WON']
            ).exclude(
                admission__isnull=False
            )
    elif card_type == 'telecaller_opd_booked':
        card_leads_qs = hospital_qs.filter(assigned_to=user) if user.role == User.Role.LEAD_ATTENDENT else hospital_qs
        status_q = Q(custom_data__appointment_status__iexact='OPD Booking') | \
                   Q(custom_data__appointment_status__icontains='OPD') | \
                   Q(custom_data__appointment_status__icontains='Book') | \
                   Q(custom_data__appointment_status__icontains='Confirm') | \
                   Q(custom_data__appointment_status__icontains='Complete') | \
                   Q(custom_data__appointment_status__icontains='Done') | \
                   Q(custom_data__appointment_status__icontains='Consult') | \
                   Q(custom_data__icontains='consult')
        appt_leads_ids = Appointment.objects.filter(
            lead__in=card_leads_qs,
            status__in=[AppointmentStatus.APPROVED, AppointmentStatus.SCHEDULED, AppointmentStatus.COMPLETED, AppointmentStatus.PENDING_APPROVAL]
        ).exclude(
            lead__deal_status=DealStatus.LOST
        ).values_list('lead_id', flat=True)
        base_card_qs = card_leads_qs.filter(
            Q(id__in=appt_leads_ids) | status_q
        ).exclude(
            deal_status=DealStatus.LOST
        ).exclude(
            custom_data__deal_status__icontains='Lost'
        ).exclude(
            custom_data__appointment_status__icontains='Lost'
        ).exclude(
            custom_data__appointment_status__icontains='Cancel'
        ).exclude(
            custom_data__appointment_status__icontains='Not Int'
        )
    elif card_type in ('opd_booked', 'won_leads'):
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
        ).exclude(
            deal_status=DealStatus.LOST
        ).exclude(
            custom_data__deal_status__icontains='Lost'
        ).exclude(
            custom_data__appointment_status__icontains='Lost'
        ).exclude(
            custom_data__appointment_status__icontains='Cancel'
        ).exclude(
            custom_data__appointment_status__icontains='Not Int'
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

        _sched_date_map = {}
        for _fl in _fu_all_qs:
            _sched = extract_lead_followup_date(_fl)
            if not _sched:
                continue
            _sched_date_map[_fl.id] = _sched

            # Categorise based purely on date vs today
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
        if mode == 'all':
            leads_qs = base_card_qs
        elif mode == 'date_range' and (range_start_date or range_end_date):
            r_s = range_start_date or range_end_date
            r_e = range_end_date or range_start_date
            r_start_dt = timezone.make_aware(datetime.combine(r_s, datetime.min.time()))
            r_end_dt = timezone.make_aware(datetime.combine(r_e, datetime.max.time()))
            leads_date_q = Q(created_at__range=(r_start_dt, r_end_dt)) | \
                           Q(inquiry_date__range=(r_s, r_e)) | \
                           Q(appointments__appointment_date__range=(r_s, r_e))
            leads_qs = base_card_qs.filter(leads_date_q).distinct()
        elif mode == 'this_month' and month_range_start and month_range_end:
            leads_date_q = Q(created_at__range=(month_range_start, month_range_end)) | \
                           Q(inquiry_date__range=(m_start_date, m_end_date)) | \
                           Q(appointments__appointment_date__range=(m_start_date, m_end_date)) | \
                           Q(custom_data__appo_booked_date__startswith=today.strftime('%Y-%m'))
            leads_qs = base_card_qs.filter(leads_date_q).distinct()
        else:
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

    elif card_type in ('opd_booked', 'won_leads'):
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
            _target_fu_leads = [l for l in _fu_all_active_list if _sched_date_map.get(l.id) == selected_date]
        elif selected_date and selected_date > today:
            _target_fu_leads = [l for l in _fu_upcoming_list if _sched_date_map.get(l.id) == selected_date]
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
    
    # Server-side campaign parameter targeting:
    # If a specific campaign is requested in query params (or clicked in the UI), target leads directly for that campaign
    # so that all leads belonging to that campaign load smoothly without being cut off by the top 250 slice.
    leads_target_qs = leads_qs
    if campaign_filter and card_type != 'followups':
        cf_norm = campaign_filter.lower().strip()
        direct_keywords = ['nan', 'general / direct', 'direct hospital visit', 'direct walk-in', 'direct walk in', 'walk-in', 'general', 'direct', 'none', '', default_direct_campaign.lower()]
        if cf_norm in direct_keywords:
            leads_target_qs = leads_qs.filter(
                Q(campaign__isnull=True) |
                Q(campaign__name__iexact=campaign_filter) |
                Q(campaign__name__in=['', 'None', 'nan', 'null', '—', '-', 'general', 'General / Direct', 'Direct Hospital Visit', 'Direct Walk-in', default_direct_campaign]) |
                Q(custom_data__campaign__isnull=True) |
                Q(custom_data__campaign__in=['', 'None', 'nan', 'null', '—', '-', 'general', 'general / direct', 'direct', 'general/direct', default_direct_campaign]) |
                Q(custom_data__campaign__iexact=campaign_filter)
            )
        else:
            leads_target_qs = leads_qs.filter(
                Q(campaign__name__iexact=campaign_filter) |
                Q(custom_data__campaign__iexact=campaign_filter)
            )

    # Pre-fetch all followups and notes for the paginated leads
    if card_type == 'followups':
        if campaign_filter:
            cf_norm = campaign_filter.lower().strip()
            direct_keywords = ['nan', 'general / direct', 'direct hospital visit', 'direct walk-in', 'direct walk in', 'walk-in', 'general', 'direct', 'none', '']
            def _matches_cf(_lead):
                _c = _lead.campaign.name if _lead.campaign else ((_lead.custom_data or {}).get('campaign') or '')
                _c_norm = str(_c).lower().strip()
                if cf_norm in direct_keywords:
                    return _c_norm in direct_keywords or _c_norm == default_direct_campaign.lower()
                return _c_norm == cf_norm
            _target_fu_filtered = [l for l in _target_fu_leads if _matches_cf(l)]
            leads_page = _target_fu_filtered[:250]
        else:
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
        leads_page = list(leads_target_qs.order_by('-created_at', '-id')[:250])
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
        c_name = l.campaign.name if l.campaign else (cd.get('campaign') or '')
        if not c_name or str(c_name).strip().lower() in ['nan', 'none', '', '—', '-', 'null']:
            c_name = 'nan'

        source_val = l.lead_source.name if l.lead_source else (cd.get('lead_source') or cd.get('source') or '')
        if not source_val or str(source_val).strip().lower() in ['nan', 'none', '', '—', '-', 'null']:
            lead_source_name = 'nan'
        else:
            lead_source_name = str(source_val).strip()

        mob_digits = Lead.clean_mobile(l.mobile)
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
            r_all = " ".join([str(cd.get(f'remark_{idx}') or '') for idx in range(1, 4)]).upper()
            if "HOT" in r_all:
                temp_str = "HOT"
            elif "WARM" in r_all:
                temp_str = "WARM"
            elif "COLD" in r_all:
                temp_str = "COLD"
            else:
                temp_str = "WARM"
        
        raw_comments = []
        for i in range(1, 6):
            r = cd.get(f'remark_{i}')
            if r and str(r).strip() not in ('', 'None', 'nan', '-'):
                raw_comments.append(str(r).strip())
        
        for extra_note in [cd.get('comments'), l.notes, getattr(l, 'referral_notes', None)]:
            if extra_note and str(extra_note).strip() not in ('', 'None', 'nan', '-'):
                raw_comments.append(str(extra_note).strip())
        raw_comments.extend(lead_comments_map.get(l.id, []))

        all_comments = []
        seen_comments = set()
        for c in raw_comments:
            c_str = str(c).strip()
            c_key = c_str.lower()
            if c_key and c_key not in seen_comments:
                seen_comments.add(c_key)
                all_comments.append(c_str)

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
            "lead_code": l.lead_code or str(l.id),
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

    date_map = defaultdict(int)

    if card_type in ('telecaller_opd_booked', 'opd_booked'):
        cal_m_prefix = f"{cal_year:04d}-{cal_month:02d}-"
        cal_m_alt_suffix = f"-{cal_month:02d}-{cal_year:04d}"

        # 1. Appointment model counts for this month
        appt_qs_cal = Appointment.objects.filter(
            lead__in=base_card_qs,
            appointment_date__year=cal_year,
            appointment_date__month=cal_month,
            status__in=[AppointmentStatus.APPROVED, AppointmentStatus.SCHEDULED, AppointmentStatus.COMPLETED, AppointmentStatus.PENDING_APPROVAL]
        ).exclude(lead__deal_status=DealStatus.LOST).values('appointment_date').annotate(cnt=Count('lead_id', distinct=True))
        for row in appt_qs_cal:
            if row.get('appointment_date'):
                d_s = row['appointment_date'].strftime('%Y-%m-%d')
                date_map[d_s] = row['cnt']

        # 2. Leads custom_data & created_at / inquiry_date for this month
        day_lead_map = defaultdict(set)
        for l in base_card_qs:
            cd = l.custom_data or {}
            b_dates = [
                cd.get('appo_booked_date'),
                cd.get('appointment_date'),
                str(l.inquiry_date) if l.inquiry_date else None,
                l.created_at.strftime('%Y-%m-%d') if l.created_at else None
            ]
            for bd in b_dates:
                if not bd:
                    continue
                b_str = str(bd).strip()[:10]
                if b_str.startswith(cal_m_prefix) and len(b_str) == 10:
                    day_lead_map[b_str].add(l.id)
                elif b_str.endswith(cal_m_alt_suffix) and len(b_str) == 10:
                    d_p = b_str[:2]
                    day_lead_map[f"{cal_year:04d}-{cal_month:02d}-{d_p}"].add(l.id)

        for d_str, id_set in day_lead_map.items():
            date_map[d_str] = max(date_map.get(d_str, 0), len(id_set))
    elif card_type == 'followups':
        cal_m_prefix = f"{cal_year:04d}-{cal_month:02d}-"
        for _fl in _fu_all_qs:
            _d = _sched_date_map.get(_fl.id)
            if _d:
                _d_str = _d.strftime('%Y-%m-%d')
                if _d_str.startswith(cal_m_prefix):
                    date_map[_d_str] += 1
    else:
        from django.db.models.functions import TruncDate
        date_counts = (
            base_card_qs.filter(created_at__range=(cal_m_start, cal_m_end))
            .annotate(c_date=TruncDate('created_at'))
            .values('c_date')
            .annotate(cnt=Count('id'))
        )
        for row in date_counts:
            if row.get('c_date'):
                date_map[row['c_date'].strftime('%Y-%m-%d')] = row['cnt']

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

    res_payload = {
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
    }
    if cache_key:
        cache.set(cache_key, res_payload, 180)  # 3 minutes cache for fast drilldown loading
    return JsonResponse(res_payload)


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

        # Role-based restriction if telecaller / counsellor / hr
        if user.role in (User.Role.LEAD_ATTENDENT, User.Role.COUNSELLOR, User.Role.HR) and not getattr(user, 'can_view_all_leads', False):
            my_leads_qs = leads_qs.filter(Q(assigned_to=user) | Q(assigned_to__isnull=True))
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
            phone_val = request.POST.get('phone', '').strip()
            if phone_val:
                import re
                raw_digits = re.sub(r"\D", "", phone_val)
                if len(raw_digits) == 12 and raw_digits.startswith("91"):
                    raw_digits = raw_digits[2:]
                elif len(raw_digits) == 11 and raw_digits.startswith("0"):
                    raw_digits = raw_digits[1:]
                if len(raw_digits) < 10 or len(raw_digits) > 11:
                    messages.error(request, "Please enter a valid 10-11 digit contact phone number.")
                    return redirect('dashboard:nelson_module', module_name='hospital-profile')
                phone_val = raw_digits

            hospital.name = request.POST.get('name', hospital.name)
            hospital.contact_email = request.POST.get('contact_email', hospital.contact_email)
            hospital.phone = phone_val
            hospital.address = request.POST.get('address', hospital.address)
            hospital.registration_no = request.POST.get('registration_no', hospital.registration_no)
            
            if 'logo' in request.FILES:
                hospital.logo = request.FILES['logo']
                
            wa_num = request.POST.get('whatsapp_number', '').strip()
            if wa_num:
                import re
                wa_digits = re.sub(r"\D", "", wa_num)
                if len(wa_digits) == 12 and wa_digits.startswith("91"):
                    wa_digits = wa_digits[2:]
                elif len(wa_digits) == 11 and wa_digits.startswith("0"):
                    wa_digits = wa_digits[1:]
                if len(wa_digits) != 10:
                    messages.error(request, "Please enter a valid 10-digit WhatsApp number.")
                    return redirect('dashboard:nelson_module', module_name='hospital-profile')
                wa_num = wa_digits

            settings_data = {
                'facebook_url': request.POST.get('facebook_url', ''),
                'instagram_url': request.POST.get('instagram_url', ''),
                'whatsapp_number': wa_num,
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
            
        return render(request, "hospital/dashboard/profile.html", {
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

                # Sync LeadCustomField options if present
                from leads.models import LeadCustomField
                target_h = camp.hospital or hospital
                if target_h:
                    cf_camp = LeadCustomField.objects.filter(hospital=target_h, name="campaign").first()
                    if cf_camp:
                        active_camps = list(Campaign.objects.filter(hospital=target_h, is_active=True).values_list("name", flat=True))
                        cf_camp.options = ", ".join(active_camps)
                        cf_camp.save(update_fields=["options"])

                messages.success(request, f"Campaign '{camp.name}' status toggled to {'Active' if camp.is_active else 'Inactive'}.")
                return redirect('dashboard:nelson_module', module_name='campaign-management')
                
            elif action == 'delete':
                cid = request.POST.get('campaign_id')
                camp = get_object_or_404(Campaign, pk=cid)
                if not is_superadmin and camp.hospital and camp.hospital != hospital:
                    messages.error(request, "Permission denied.")
                    return redirect('dashboard:nelson_module', module_name='campaign-management')
                name = camp.name
                target_h = camp.hospital or hospital
                camp.delete()

                # Sync LeadCustomField options if present
                from leads.models import LeadCustomField
                if target_h:
                    cf_camp = LeadCustomField.objects.filter(hospital=target_h, name="campaign").first()
                    if cf_camp:
                        active_camps = list(Campaign.objects.filter(hospital=target_h, is_active=True).values_list("name", flat=True))
                        cf_camp.options = ", ".join(active_camps)
                        cf_camp.save(update_fields=["options"])

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

        # Sort campaigns by leads count descending (primary: active period leads, secondary: all-time leads)
        sort_by = request.GET.get('sort', 'leads_desc')
        if sort_by == 'period_leads_desc' or (date_preset != 'all_time' and sort_by != 'all_time_desc'):
            campaigns_data.sort(key=lambda x: (x["period_leads_count"], x["leads_count"]), reverse=True)
        else:
            campaigns_data.sort(key=lambda x: (x["leads_count"], x["period_leads_count"]), reverse=True)

        # Calculate platform summary counts across all campaigns
        selected_platform = request.GET.get('platform', 'all').strip()
        platform_stats = {
            "all": {"name": "All Platforms", "count": 0, "campaigns_count": 0},
            "meta": {"name": "Meta Ads", "count": 0, "campaigns_count": 0},
            "google": {"name": "Google Ads", "count": 0, "campaigns_count": 0},
            "justdial": {"name": "Justdial", "count": 0, "campaigns_count": 0},
            "practo": {"name": "Practo", "count": 0, "campaigns_count": 0},
            "offline": {"name": "Offline / General", "count": 0, "campaigns_count": 0},
        }

        for c_item in campaigns_data:
            p_val = (c_item["obj"].platform or "").lower()
            lead_cnt = c_item["period_leads_count"] if date_preset != 'all_time' else c_item["leads_count"]
            
            platform_stats["all"]["count"] += lead_cnt
            platform_stats["all"]["campaigns_count"] += 1

            if "meta" in p_val or "facebook" in p_val or "instagram" in p_val:
                platform_stats["meta"]["count"] += lead_cnt
                platform_stats["meta"]["campaigns_count"] += 1
            elif "google" in p_val:
                platform_stats["google"]["count"] += lead_cnt
                platform_stats["google"]["campaigns_count"] += 1
            elif "justdial" in p_val or "just dial" in p_val:
                platform_stats["justdial"]["count"] += lead_cnt
                platform_stats["justdial"]["campaigns_count"] += 1
            elif "practo" in p_val:
                platform_stats["practo"]["count"] += lead_cnt
                platform_stats["practo"]["campaigns_count"] += 1
            else:
                platform_stats["offline"]["count"] += lead_cnt
                platform_stats["offline"]["campaigns_count"] += 1

        # Filter campaigns_data by selected platform if specified
        filtered_campaigns_data = []
        if selected_platform == "meta":
            filtered_campaigns_data = [c for c in campaigns_data if ("meta" in (c["obj"].platform or "").lower() or "facebook" in (c["obj"].platform or "").lower() or "instagram" in (c["obj"].platform or "").lower())]
        elif selected_platform == "google":
            filtered_campaigns_data = [c for c in campaigns_data if "google" in (c["obj"].platform or "").lower()]
        elif selected_platform == "justdial":
            filtered_campaigns_data = [c for c in campaigns_data if ("justdial" in (c["obj"].platform or "").lower() or "just dial" in (c["obj"].platform or "").lower())]
        elif selected_platform == "practo":
            filtered_campaigns_data = [c for c in campaigns_data if "practo" in (c["obj"].platform or "").lower()]
        elif selected_platform == "offline":
            filtered_campaigns_data = [c for c in campaigns_data if not any(x in (c["obj"].platform or "").lower() for x in ["meta", "facebook", "instagram", "google", "justdial", "just dial", "practo"])]
        else:
            selected_platform = "all"
            filtered_campaigns_data = campaigns_data

        # Pagination for campaigns table (10 items per page)
        from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
        page = request.GET.get('page', 1)
        paginator = Paginator(filtered_campaigns_data, 10)
        try:
            campaigns_page = paginator.page(page)
        except PageNotAnInteger:
            campaigns_page = paginator.page(1)
        except EmptyPage:
            campaigns_page = paginator.page(paginator.num_pages)

        total_appts = Appointment.objects.filter(hospital=hospital).count() if hospital else (Appointment.objects.all().count() if is_superadmin else 0)
        
        # Recent Import Jobs in selected period for WhatsApp report
        recent_jobs = period_jobs_qs.filter(imported_count__gt=0).order_by('-created_at')[:15]
        hospital_name = hospital.name if hospital else ("All Businesses (Global)" if is_superadmin else "Zappcode CRM")

        return render(request, "dashboard/campaign_management.html", {
            "title": "Campaign Management",
            "active": "campaign-management",
            "campaigns_data": campaigns_page,
            "paginator": paginator,
            "page_obj": campaigns_page,
            "is_paginated": campaigns_page.has_other_pages(),
            "total_campaigns": len(filtered_campaigns_data),
            "all_campaigns_count": len(campaigns_data),
            "platform_stats": platform_stats,
            "selected_platform": selected_platform,
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
            "sort_by": sort_by,
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
    return render(request, "hospital/dashboard/generic.html", {"title": title, "module_name": module_name, "active": module_name})


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

    # Business / Tenant scoping for Global Super Admin
    is_global_admin = request.user.is_superuser or (
        request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital
    )
    selected_hospital_id = ""
    if is_global_admin:
        raw_biz = request.GET.get("business", "").strip()
        raw_hosp = request.GET.get("hospital", "").strip()
        session_biz = str(getattr(request, 'session', {}).get("active_business_id", "")).strip()
        selected_hospital_id = raw_biz or raw_hosp or session_biz
    elif request.user.hospital:
        selected_hospital_id = str(request.user.hospital.id)

    all_leads = Lead.objects.filter(is_archived=False)
    if selected_hospital_id and selected_hospital_id.isdigit():
        all_leads = all_leads.filter(hospital_id=int(selected_hospital_id))
    elif selected_hospital_id in ("none", "zappcode"):
        all_leads = all_leads.filter(hospital__isnull=True)

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
    followups_today = FollowUp.objects.filter(lead__in=all_leads, followup_date=today).count()
    overdue = FollowUp.objects.filter(lead__in=all_leads, followup_date__lt=today, followup_status="PENDING").count()
    admissions_count = Admission.objects.filter(lead__in=all_leads).count() if (selected_hospital_id and (selected_hospital_id.isdigit() or selected_hospital_id in ("none", "zappcode"))) else Admission.objects.count()
    visits_count = leads.filter(stage__name__icontains="visit").count()
    payments_qs = Payment.objects.filter(payment_status=PaymentStatus.SUCCESS)
    if selected_hospital_id and selected_hospital_id.isdigit():
        payments_qs = payments_qs.filter(lead__hospital_id=int(selected_hospital_id))
    elif selected_hospital_id in ("none", "zappcode"):
        payments_qs = payments_qs.filter(lead__hospital__isnull=True)
    total_revenue = payments_qs.aggregate(s=Sum("amount"))["s"] or 0
    conversion_rate = round(admissions_count / total_leads * 100, 1) if total_leads else 0.0
    pending_approvals_count = User.objects.filter(is_approved=False).count()

    # ─── Team Activity Today ──────────────────────────────────────────────────
    team_members = User.objects.filter(is_active=True, is_approved=True, role__in=['COUNSELLOR', 'HR', 'LEAD_ATTENDENT'])
    if selected_hospital_id and selected_hospital_id.isdigit():
        team_members = team_members.filter(hospital_id=int(selected_hospital_id))
    elif selected_hospital_id in ("none", "zappcode"):
        team_members = team_members.filter(hospital__isnull=True)
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






def placeholder_view(request, module_name):
    # This acts as a dummy view for all incomplete telecaller modules
    return render(request, "dashboard/placeholder.html", {"active": module_name, "module_name": module_name.replace("_", " ").title()})

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
    return render(request, "hospital/dashboard/roles_permissions.html", context)


