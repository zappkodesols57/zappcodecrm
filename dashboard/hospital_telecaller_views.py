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
    
    hospital_leads = Lead.objects.filter(hospital=user.hospital, is_archived=False).select_related('hospital')

    # CARD 1: Today's New Leads Count (Fresh unassigned leads OR leads assigned to this user today)
    todays_new_leads_count = hospital_leads.filter(
        Q(assigned_to=user) | Q(assigned_to__isnull=True)
    ).filter(
        Q(created_at__range=(start_of_today, end_of_today)) | Q(inquiry_date=today_date)
    ).distinct().count()

    # CARD 2: Call Not Done Count (Pending calling queue assigned to this attendant)
    if user.role == User.Role.LEAD_ATTENDENT:
        cnd_candidates = hospital_leads.filter(assigned_to=user)
    else:
        cnd_candidates = hospital_leads.filter(assigned_to__isnull=False)

    # Strictly filter genuinely uncontacted leads (excludes any leads with remarks, comments, followups or notes)
    cnd_matched_ids = filter_uncontacted_leads_ids(cnd_candidates, today=today_date)
    call_not_done_count = len(cnd_matched_ids)

    # CARD 3: Today's OPD Booked & Consultation Booked by User (Initialized, populated by appointment loop below)
    todays_opd_booked_count = 0
    todays_consult_booked_count = 0
    todays_total_booked_count = 0

    # CARD 4: Today's Follow-ups for User (Strictly assigned to this user)
    booked_exclude_tele = (
        Q(custom_data__appointment_status__icontains='Book') |
        Q(custom_data__appointment_status__icontains='Confirm') |
        Q(deal_status__in=[DealStatus.WON, DealStatus.LOST]) |
        Q(admission_status='ADMISSION_DONE') |
        (Q(custom_data__total__isnull=False) & ~Q(custom_data__total__in=['0', '0.00', '', '0.0', 0, 0.0]))
    )

    user_fu_qs = hospital_leads.filter(
        assigned_to=user
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

    for l in list(user_fu_qs.prefetch_related('followups')[:150]):
        sched_date = extract_lead_followup_date(l)
        if not sched_date:
            continue

        cd = l.custom_data or {}
        latest_fu = l.followups.all()[0] if (hasattr(l, '_prefetched_objects_cache') and l.followups.all()) else None
        has_status_update = False
        if latest_fu and latest_fu.followup_status not in ('PENDING', 'CALL_BACK', 'RESCHEDULED') and latest_fu.followup_date and latest_fu.followup_date >= sched_date:
            has_status_update = True

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
            if not has_status_update:
                overdue_tele_followups_list.append(l)
        elif sched_date > today_date:
            if not has_status_update:
                upcoming_tele_followups_list.append(l)

    todays_followups_count = len(todays_tele_followups_list)
    overdue_followups_count = len(overdue_tele_followups_list)
    upcoming_followups_count = len(upcoming_tele_followups_list)

    # SECTION 4: Pending & Upcoming Follow-ups list for bottom table
    pending_and_upcoming_followups = (overdue_tele_followups_list + todays_tele_followups_list + upcoming_tele_followups_list)[:10]
    pending_and_upcoming_followups_count = (overdue_followups_count + todays_followups_count + upcoming_followups_count)

    # CARD 5: Today's Walk-in Leads (Assigned to user)
    todays_walkin_count = hospital_leads.filter(
        assigned_to=user
    ).filter(
        Q(lead_source__name__icontains='walk-in') |
        Q(custom_data__lead_source__icontains='walk-in') |
        Q(custom_data__source__icontains='walk-in') |
        Q(lead_type__icontains='walk')
    ).filter(
        Q(created_at__range=(start_of_today, end_of_today)) | Q(inquiry_date=today_date)
    ).distinct().count()

    # CARD 6: Today's Calling Target & Countdown (Unique leads interacted/updated today)
    daily_target = user.daily_call_target
    
    user_today_fu_lead_ids = set(FollowUp.objects.filter(
        created_by=user,
        created_at__range=(start_of_today, end_of_today)
    ).values_list('lead_id', flat=True)) | set(FollowUp.objects.filter(
        created_by=user,
        followup_date=today_date
    ).values_list('lead_id', flat=True))

    from followups.models import Note as LeadNote
    user_today_note_lead_ids = set(LeadNote.objects.filter(
        created_by=user,
        created_at__range=(start_of_today, end_of_today)
    ).values_list('lead_id', flat=True))

    user_today_apt_lead_ids = set(Appointment.objects.filter(
        lead__hospital=user.hospital,
        created_by=user,
        created_at__range=(start_of_today, end_of_today)
    ).values_list('lead_id', flat=True))

    user_today_updated_lead_ids = set(hospital_leads.filter(
        assigned_to=user,
        updated_at__range=(start_of_today, end_of_today)
    ).exclude(
        deal_status=DealStatus.OPEN,
        next_followup_date__isnull=True,
        followup_count=0
    ).values_list('id', flat=True))

    unique_touched_today_ids = user_today_fu_lead_ids | user_today_note_lead_ids | user_today_apt_lead_ids | user_today_updated_lead_ids
    calls_completed_today = len(unique_touched_today_ids)
    target_remaining = max(0, daily_target - calls_completed_today)

    # My Recent Leads (Latest 10 entries assigned to this user, newly updated first)
    my_recent_leads = list(hospital_leads.filter(
        assigned_to=user
    ).select_related('stage', 'campaign', 'lead_source').prefetch_related('appointments').order_by('-updated_at')[:10])

    # Today's Tasks & Reminders
    todays_tasks = list(TaskReminder.objects.filter(
        Q(user=user) | Q(user__hospital=user.hospital, user__role__in=['SUPER_ADMIN', 'MANAGER']),
        due_date=today_date,
    ).exclude(
        status=TaskReminder.Status.COMPLETED
    ).select_related('user', 'lead').order_by('due_time', '-created_at')[:10])

    # SECTION 3: APPOINTMENTS (4 SUB-TABS)
    # Helper to parse appointment date from lead custom_data or Appointment object
    def get_lead_appointment_info(l):
        cd = l.custom_data or {}
        apt_date_str = cd.get('appo_booked_date') or cd.get('appointment_date') or cd.get('booking_date')
        apt_time_str = cd.get('appointment_time') or cd.get('booking_time') or cd.get('time') or 'Scheduled Slot'
        apt_status_str = str(cd.get('appointment_status') or cd.get('deal_status') or (l.stage.name if l.stage else '')).strip()
        
        parsed_dt = None
        if apt_date_str:
            for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y'):
                try:
                    parsed_dt = datetime.strptime(str(apt_date_str).strip()[:10], fmt).date()
                    break
                except Exception:
                    pass

        # Check linked Appointment model instance (prefetched)
        if hasattr(l, '_prefetched_objects_cache') and 'appointments' in l._prefetched_objects_cache:
            apts = l.appointments.all()
            linked_apt = apts[0] if apts else None
        else:
            linked_apt = None

        if linked_apt:
            if not parsed_dt:
                parsed_dt = linked_apt.appointment_date
            if linked_apt.appointment_time:
                apt_time_str = linked_apt.appointment_time.strftime('%I:%M %p')
            if linked_apt.status:
                apt_status_str = linked_apt.get_status_display()

        # Determine OPD vs Consultation type
        is_opd = 'opd' in apt_status_str.lower() or 'opd' in str(cd.get('department') or cd.get('disease') or '').lower() or 'opd' in str(l.course.name if l.course else '').lower()
        apt_type = 'OPD' if is_opd else 'Consultation'

        return parsed_dt, apt_time_str, apt_status_str, apt_type

    # Base queryset for appointments belonging strictly to this telecaller
    appointment_candidate_leads = hospital_leads.filter(
        assigned_to=user
    ).exclude(
        deal_status__in=[DealStatus.LOST, DealStatus.WON]
    ).exclude(
        custom_data__deal_status__icontains='Lost'
    ).exclude(
        custom_data__deal_status__icontains='Won'
    ).exclude(
        custom_data__deal_status__icontains='Payment Done'
    ).exclude(
        custom_data__appointment_status__icontains='Lost'
    ).exclude(
        custom_data__appointment_status__icontains='Cancel'
    ).exclude(
        custom_data__appointment_status__icontains='Not Int'
    ).exclude(
        stage__name__icontains='Payment Done'
    ).exclude(
        Q(custom_data__total__isnull=False) & ~Q(custom_data__total__in=['0', '0.00', '', '0.0', 0, 0.0])
    ).filter(
        Q(appointments__isnull=False) |
        Q(custom_data__appo_booked_date__isnull=False) |
        Q(custom_data__appointment_date__isnull=False) |
        Q(custom_data__appointment_status__isnull=False) |
        Q(stage__name__icontains='book') |
        Q(stage__name__icontains='appoint')
    ).distinct().select_related('stage', 'campaign', 'lead_source', 'course').prefetch_related('appointments')

    todays_appointments_list = []
    upcoming_appointments_list = []
    admitted_appointments_list = []
    approval_pending_appointments_list = []

    for l in list(appointment_candidate_leads[:100]):
        p_dt, p_time, p_st, p_type = get_lead_appointment_info(l)
        if not p_dt and not (hasattr(l, '_prefetched_objects_cache') and l.appointments.all()) and not any(k in p_st.lower() for k in ['book', 'confirm', 'approv', 'await', 'sched']):
            continue

        l.appointment_scheduled_date = p_dt
        l.appointment_scheduled_time = p_time
        l.appointment_display_status = p_st
        l.appointment_type_label = p_type

        p_st_lower = p_st.lower()
        cd_l = l.custom_data or {}
        tot_l = 0.0
        try:
            tot_l = float(cd_l.get('total_paid') or cd_l.get('total') or cd_l.get('opd_bill') or cd_l.get('pharmacy_bill') or 0.0)
        except Exception:
            tot_l = 0.0

        is_paid = tot_l > 0 or l.deal_status == DealStatus.WON or 'payment done' in p_st_lower or 'payment done' in str(cd_l.get('deal_status') or '').lower() or (l.stage and 'payment done' in l.stage.name.lower())
        is_completed = 'complet' in p_st_lower or 'done' in p_st_lower or is_paid
        
        # If payment is already done, it belongs in the Billings -> Payment Done tab, not Appointments tab
        if is_paid:
            continue

        is_pending_approval = 'await' in p_st_lower or 'pending' in p_st_lower or 'pending approval' in p_st_lower
        is_confirmed_or_booked = any(k in p_st_lower for k in ['confirm', 'book', 'sched', 'approved', 'yes'])

        if is_pending_approval and not is_completed:
            approval_pending_appointments_list.append(l)
            continue

        if p_dt:
            if p_dt == today_date and not is_completed:
                todays_appointments_list.append(l)
            elif p_dt > today_date and not is_completed:
                upcoming_appointments_list.append(l)
            elif p_dt < today_date and not is_completed and is_confirmed_or_booked:
                admitted_appointments_list.append(l)
        elif is_confirmed_or_booked and not is_completed:
            todays_appointments_list.append(l)

    todays_appointments_count = len(todays_appointments_list)
    upcoming_appointments_count = len(upcoming_appointments_list)
    admitted_appointments_count = len(admitted_appointments_list)
    approval_pending_appointments_count = len(approval_pending_appointments_list)
    total_appointments_count = todays_appointments_count + upcoming_appointments_count + admitted_appointments_count + approval_pending_appointments_count

    todays_opd_booked_count = sum(1 for l in todays_appointments_list if getattr(l, 'appointment_type_label', 'Consultation') == 'OPD')
    todays_consult_booked_count = sum(1 for l in todays_appointments_list if getattr(l, 'appointment_type_label', 'Consultation') != 'OPD')
    todays_total_booked_count = todays_appointments_count

    upcoming_opd_leads = upcoming_appointments_list[:10]
    upcoming_opd_count = upcoming_appointments_count

    # SECTION: MY LEADS
    new_leads_qs = hospital_leads.filter(
        assigned_to__isnull=True
    ).exclude(
        deal_status=DealStatus.LOST
    ).exclude(
        custom_data__deal_status__icontains='Lost'
    ).select_related('stage', 'campaign', 'lead_source')

    if getattr(user, 'branch', None) and user.branch:
        new_leads_qs = new_leads_qs.filter(
            Q(custom_data__hospital_branch__iexact=user.branch.name) |
            Q(custom_data__branch__iexact=user.branch.name) |
            Q(custom_data__hospital_branch__isnull=True, custom_data__branch__isnull=True)
        )

    new_leads_count = new_leads_qs.count()
    from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
    new_leads_paginator = Paginator(new_leads_qs.order_by('-created_at'), 25)
    new_leads_page_num = request.GET.get('page') or 1
    try:
        new_leads_page = new_leads_paginator.page(new_leads_page_num)
    except (EmptyPage, PageNotAnInteger):
        new_leads_page = new_leads_paginator.page(1)
    new_leads_list = list(new_leads_page.object_list)

    # Assigned Leads (Call Not Done queue): Fresh assigned leads where NO calling remarks/followups have been logged yet
    if user.role == User.Role.LEAD_ATTENDENT:
        assigned_candidates = hospital_leads.filter(assigned_to=user)
    else:
        assigned_candidates = hospital_leads.filter(assigned_to__isnull=False)

    assigned_cnd_ids = filter_uncontacted_leads_ids(assigned_candidates, today=today_date)
    assigned_leads_qs = hospital_leads.filter(id__in=assigned_cnd_ids).select_related('stage', 'campaign', 'lead_source').order_by('-created_at')

    assigned_leads_count = len(assigned_cnd_ids)
    assigned_leads_paginator = Paginator(assigned_leads_qs, 25)
    assigned_leads_page_num = request.GET.get('page') if (request.GET.get('tab') == 'myleads' and request.GET.get('subtab') == 'assigned') else 1
    try:
        assigned_leads_page = assigned_leads_paginator.page(assigned_leads_page_num)
    except (EmptyPage, PageNotAnInteger):
        assigned_leads_page = assigned_leads_paginator.page(1)
    assigned_leads_list = list(assigned_leads_page.object_list)

    # Walk-in Leads: Leads originating from direct walk-in source for today assigned to user
    walkin_leads_qs = hospital_leads.filter(
        assigned_to=user
    ).filter(
        Q(lead_source__name__icontains='walk-in') |
        Q(custom_data__lead_source__icontains='walk-in') |
        Q(custom_data__source__icontains='walk-in') |
        Q(lead_type__icontains='walk')
    ).filter(
        Q(created_at__range=(start_of_today, end_of_today)) | Q(inquiry_date=today_date)
    ).select_related('stage', 'campaign', 'lead_source').order_by('-created_at')

    walkin_leads_count = walkin_leads_qs.count()
    walkin_leads_paginator = Paginator(walkin_leads_qs, 25)
    walkin_leads_page_num = request.GET.get('page') if (request.GET.get('tab') == 'myleads' and request.GET.get('subtab') == 'walkin') else 1
    try:
        walkin_leads_page = walkin_leads_paginator.page(walkin_leads_page_num)
    except (EmptyPage, PageNotAnInteger):
        walkin_leads_page = walkin_leads_paginator.page(1)
    walkin_leads_list = list(walkin_leads_page.object_list)

    # Today's All Leads: All leads received or created today with their current statuses & stages across hospital
    todays_all_leads_qs = hospital_leads.filter(
        Q(created_at__range=(start_of_today, end_of_today)) | Q(inquiry_date=today_date)
    ).select_related('stage', 'campaign', 'lead_source', 'assigned_to').prefetch_related('followups', 'appointments').order_by('-created_at')

    todays_all_leads_count = todays_all_leads_qs.count()
    todays_all_leads_paginator = Paginator(todays_all_leads_qs, 25)
    todays_all_leads_page_num = request.GET.get('page') if (request.GET.get('tab') == 'myleads' and request.GET.get('subtab') == 'todays_all') else 1
    try:
        todays_all_leads_page = todays_all_leads_paginator.page(todays_all_leads_page_num)
    except (EmptyPage, PageNotAnInteger):
        todays_all_leads_page = todays_all_leads_paginator.page(1)
    todays_all_leads_list = list(todays_all_leads_page.object_list)

    # Follow-ups -> Completed: Only leads that had calling follow-ups completed without converting to Booked/Paid/Lost
    completed_fu_qs = hospital_leads.filter(
        assigned_to=user
    ).exclude(
        booked_exclude_tele
    ).exclude(
        deal_status__in=[DealStatus.WON, DealStatus.LOST]
    ).exclude(
        custom_data__deal_status__icontains='Won'
    ).exclude(
        custom_data__deal_status__icontains='Payment'
    ).exclude(
        custom_data__deal_status__icontains='Lost'
    ).exclude(
        stage__name__icontains='Payment'
    ).exclude(
        custom_data__total__isnull=False
    ).filter(
        followups__followup_status__in=['COMPLETED', 'DONE']
    ).distinct().select_related('stage', 'campaign', 'lead_source').order_by('-updated_at')
    
    completed_followups_count = completed_fu_qs.count()
    completed_tele_followups_list = list(completed_fu_qs[:80])

    def safe_format_dt(dt):
        if not dt:
            return '—'
        if isinstance(dt, datetime):
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt, timezone.get_current_timezone())
            return timezone.localtime(dt).strftime('%d %b %Y, %I:%M %p')
        elif isinstance(dt, date):
            return dt.strftime('%d %b %Y')
        return str(dt)

    # 5. SECTION: BILLINGS (2 SUB-TABS: Payment Done vs Payment Pending for assigned leads)
    billing_candidate_leads = hospital_leads.filter(
        assigned_to=user
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
    ).filter(
        Q(stage__name__icontains='payment') |
        Q(stage__name__icontains='complete') |
        Q(stage__name__icontains='done') |
        Q(deal_status=DealStatus.WON) |
        Q(custom_data__deal_status__icontains='Won') |
        Q(custom_data__deal_status__icontains='Payment') |
        Q(custom_data__appointment_status__icontains='Complete') |
        Q(custom_data__appointment_status__icontains='Done') |
        Q(custom_data__appointment_status__icontains='Payment') |
        Q(custom_data__total__isnull=False) |
        Q(custom_data__total_paid__isnull=False)
    ).distinct().select_related('stage', 'campaign', 'lead_source', 'course').prefetch_related('appointments', 'admission__payments').order_by('-updated_at')

    payment_pending_list = []
    payment_done_list = []

    for l in list(billing_candidate_leads[:80]):
        cd = l.custom_data or {}
        st_name = (l.stage.name if l.stage else '').strip().lower()
        raw_ds = str(cd.get('deal_status') or '').strip().lower()
        raw_apt = str(cd.get('appointment_status') or '').strip().lower()
        
        tot = 0.0
        try:
            tot = float(cd.get('total_paid') or cd.get('total') or cd.get('opd_bill') or cd.get('pharmacy_bill') or 0.0)
        except (ValueError, TypeError):
            tot = 0.0

        has_payment_record = False
        if hasattr(l, 'admission') and l.admission:
            adm_payments = l.admission.payments.all() if (hasattr(l.admission, '_prefetched_objects_cache') and 'payments' in l.admission._prefetched_objects_cache) else []
            if adm_payments:
                succ = [p for p in adm_payments if p.payment_status == PaymentStatus.SUCCESS]
                if succ:
                    has_payment_record = True
                    tot = sum(float(p.amount) for p in succ)

        l.display_billed_amount = tot
        l.uhid_display = cd.get('uhid_id_no') or cd.get('uhid') or cd.get('ipd_no') or '—'
        l.opd_bill_amt = cd.get('opd_bill') or 0
        l.pharmacy_bill_amt = cd.get('pharmacy_bill') or 0
        l.investigation_amt = cd.get('investigation') or '—'

        apt_date_str = cd.get('appo_booked_date') or cd.get('appointment_date') or cd.get('booking_date')
        apt_time_str = cd.get('appointment_time') or cd.get('booking_time') or cd.get('time') or 'Scheduled Slot'
        p_dt = None
        if apt_date_str:
            for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y'):
                try:
                    p_dt = datetime.strptime(str(apt_date_str).strip()[:10], fmt).date()
                    break
                except Exception:
                    pass

        if hasattr(l, '_prefetched_objects_cache') and 'appointments' in l._prefetched_objects_cache:
            apts = l.appointments.all()
            linked_apt = apts[0] if apts else None
        else:
            linked_apt = None

        if linked_apt:
            if not p_dt:
                p_dt = linked_apt.appointment_date
            if linked_apt.appointment_time:
                apt_time_str = linked_apt.appointment_time.strftime('%I:%M %p')

        l.apt_scheduled_date = p_dt
        l.apt_scheduled_time = apt_time_str

        done_dt = None
        if linked_apt and linked_apt.status == AppointmentStatus.COMPLETED:
            done_dt = linked_apt.updated_at

        if not done_dt:
            done_date_raw = cd.get('appointment_done_date') or cd.get('opd_done_date') or cd.get('visit_date')
            if done_date_raw:
                for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%d-%m-%Y %H:%M:%S', '%d-%m-%Y'):
                    try:
                        done_dt = datetime.strptime(str(done_date_raw).strip(), fmt)
                        break
                    except Exception:
                        pass

        if not done_dt:
            done_dt = l.updated_at

        l.appointment_done_display = safe_format_dt(done_dt)

        is_opd_completed = (
            any(k in raw_apt for k in ['complete', 'done', 'visited', 'attended']) or
            any(k in raw_ds for k in ['complete', 'done', 'visited', 'attended']) or
            st_name in ['appointment completed', 'opd done', 'completed', 'consultation complete', 'visited', 'payment pending', 'payment done'] or
            cd.get('done') or
            (linked_apt and linked_apt.status == AppointmentStatus.COMPLETED)
        )

        is_paid = (
            tot > 0 or
            has_payment_record or
            'payment done' in raw_apt or
            'payment done' in raw_ds or
            'won' in raw_ds or
            st_name in ['payment done', 'won'] or
            l.deal_status == DealStatus.WON
        )

        if is_paid:
            payment_done_list.append(l)
        elif is_opd_completed or 'payment pending' in st_name or 'payment pending' in raw_apt or 'payment pending' in raw_ds:
            payment_pending_list.append(l)

    payment_pending_count = len(payment_pending_list)
    payment_done_count = billing_candidate_leads.filter(
        Q(custom_data__deal_status__icontains='Won') |
        Q(deal_status=DealStatus.WON) |
        Q(stage__name__icontains='Payment Done') |
        Q(custom_data__appointment_status__icontains='Payment Done') |
        Q(custom_data__total_paid__isnull=False) |
        Q(admission__payments__payment_status='SUCCESS')
    ).distinct().count() or len(payment_done_list)
    total_billings_count = payment_pending_count + payment_done_count

    # 6. Lost / Cancelled Leads (Strictly assigned to user)
    lost_qs = hospital_leads.filter(
        assigned_to=user
    ).filter(
        Q(deal_status=DealStatus.LOST) |
        Q(admission_status__in=['LOST', 'CANCELLED', 'DROPOUT']) |
        Q(temperature=LeadTemperature.FREEZE) |
        Q(stage__name__icontains='lost') |
        Q(stage__name__icontains='cancel') |
        Q(custom_data__deal_status__icontains='Lost') |
        Q(custom_data__deal_status__icontains='Cancel') |
        Q(custom_data__appointment_status__icontains='Cancel') |
        Q(custom_data__appointment_status__icontains='Not Int')
    ).distinct().select_related('stage', 'campaign', 'lead_source').prefetch_related('followups').order_by('-updated_at')

    lost_leads_count = lost_qs.count()
    lost_paginator = Paginator(lost_qs, 25)
    lost_page_num = request.GET.get('lost_page') or (request.GET.get('page') if request.GET.get('tab') == 'lost' else 1)
    try:
        lost_page = lost_paginator.page(lost_page_num)
    except (EmptyPage, PageNotAnInteger):
        lost_page = lost_paginator.page(1)

    lost_cancelled_leads_list = []
    for l in list(lost_page.object_list):
        closing_dt = l.updated_at or l.created_at
        fu_cnt = len(l.followups.all()) if (hasattr(l, '_prefetched_objects_cache') and 'followups' in l._prefetched_objects_cache) else l.followup_count
        l.display_followup_count = fu_cnt
        l.closing_datetime_display = safe_format_dt(closing_dt)
        lost_cancelled_leads_list.append(l)

    # SECTION 4: Paginate Follow-up lists
    from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger

    cur_tab = request.GET.get('tab', '')
    cur_subtab = request.GET.get('subtab', '')
    req_page = request.GET.get('page') or 1

    # Follow-ups pagination
    todays_fu_paginator = Paginator(todays_tele_followups_list, 25)
    upcoming_fu_paginator = Paginator(upcoming_tele_followups_list, 25)
    overdue_fu_paginator = Paginator(overdue_tele_followups_list, 25)
    
    try:
        todays_fu_page = todays_fu_paginator.page(req_page if (cur_tab == 'followups' and cur_subtab in ('today_fu', 'today', 'todays', '')) else 1)
    except (EmptyPage, PageNotAnInteger):
        todays_fu_page = todays_fu_paginator.page(1)
    todays_tele_followups_list = list(todays_fu_page.object_list)

    try:
        upcoming_fu_page = upcoming_fu_paginator.page(req_page if (cur_tab == 'followups' and cur_subtab in ('upcoming_fu', 'upcoming')) else 1)
    except (EmptyPage, PageNotAnInteger):
        upcoming_fu_page = upcoming_fu_paginator.page(1)
    upcoming_tele_followups_list = list(upcoming_fu_page.object_list)

    try:
        overdue_fu_page = overdue_fu_paginator.page(req_page if (cur_tab == 'followups' and cur_subtab in ('overdue_fu', 'overdue')) else 1)
    except (EmptyPage, PageNotAnInteger):
        overdue_fu_page = overdue_fu_paginator.page(1)
    overdue_tele_followups_list = list(overdue_fu_page.object_list)

    # Completed Follow-ups pagination
    completed_fu_paginator = Paginator(completed_fu_qs, 25)
    try:
        completed_fu_page = completed_fu_paginator.page(req_page if (cur_tab == 'followups' and cur_subtab in ('completed_fu', 'completed')) else 1)
    except (EmptyPage, PageNotAnInteger):
        completed_fu_page = completed_fu_paginator.page(1)
    completed_tele_followups_list = list(completed_fu_page.object_list)

    # Appointments pagination
    todays_apts_paginator = Paginator(todays_appointments_list, 25)
    try:
        todays_apts_page = todays_apts_paginator.page(req_page if (cur_tab == 'appointments' and cur_subtab in ('todays', 'today', '')) else 1)
    except (EmptyPage, PageNotAnInteger):
        todays_apts_page = todays_apts_paginator.page(1)
    todays_appointments_list = list(todays_apts_page.object_list)

    upcoming_apts_paginator = Paginator(upcoming_appointments_list, 25)
    try:
        upcoming_apts_page = upcoming_apts_paginator.page(req_page if (cur_tab == 'appointments' and cur_subtab == 'upcoming') else 1)
    except (EmptyPage, PageNotAnInteger):
        upcoming_apts_page = upcoming_apts_paginator.page(1)
    upcoming_appointments_list = list(upcoming_apts_page.object_list)

    admitted_apts_paginator = Paginator(admitted_appointments_list, 25)
    try:
        admitted_apts_page = admitted_apts_paginator.page(req_page if (cur_tab == 'appointments' and cur_subtab == 'admitted') else 1)
    except (EmptyPage, PageNotAnInteger):
        admitted_apts_page = admitted_apts_paginator.page(1)
    admitted_appointments_list = list(admitted_apts_page.object_list)

    approval_pending_apts_paginator = Paginator(approval_pending_appointments_list, 25)
    try:
        approval_pending_apts_page = approval_pending_apts_paginator.page(req_page if (cur_tab == 'appointments' and cur_subtab == 'pending') else 1)
    except (EmptyPage, PageNotAnInteger):
        approval_pending_apts_page = approval_pending_apts_paginator.page(1)
    approval_pending_appointments_list = list(approval_pending_apts_page.object_list)

    # Billings pagination
    payment_pending_paginator = Paginator(payment_pending_list, 25)
    try:
        payment_pending_page = payment_pending_paginator.page(req_page if (cur_tab == 'billings' and cur_subtab in ('pending', '')) else 1)
    except (EmptyPage, PageNotAnInteger):
        payment_pending_page = payment_pending_paginator.page(1)
    payment_pending_list = list(payment_pending_page.object_list)

    payment_done_paginator = Paginator(payment_done_list, 25)
    try:
        payment_done_page = payment_done_paginator.page(req_page if (cur_tab == 'billings' and cur_subtab == 'done') else 1)
    except (EmptyPage, PageNotAnInteger):
        payment_done_page = payment_done_paginator.page(1)
    payment_done_list = list(payment_done_page.object_list)

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
        'completed_followups_count': completed_followups_count,
        'todays_tele_followups_list': todays_tele_followups_list,
        'todays_fu_page': todays_fu_page,
        'upcoming_tele_followups_list': upcoming_tele_followups_list,
        'upcoming_fu_page': upcoming_fu_page,
        'overdue_tele_followups_list': overdue_tele_followups_list,
        'overdue_fu_page': overdue_fu_page,
        'completed_tele_followups_list': completed_tele_followups_list,
        'completed_fu_page': completed_fu_page,
        'lost_cancelled_leads_list': lost_cancelled_leads_list,
        'lost_leads_count': lost_leads_count,
        'lost_page': lost_page,
        'new_leads_list': new_leads_list,
        'new_leads_count': new_leads_count,
        'new_leads_page': new_leads_page,
        'can_self_assign': getattr(user, 'can_self_assign', True),
        'bulk_self_assign_limit': getattr(user, 'bulk_self_assign_limit', 25),
        'assigned_leads_list': assigned_leads_list,
        'assigned_leads_count': assigned_leads_count,
        'assigned_leads_page': assigned_leads_page,
        'walkin_leads_list': walkin_leads_list,
        'walkin_leads_count': walkin_leads_count,
        'walkin_leads_page': walkin_leads_page,
        'todays_all_leads_list': todays_all_leads_list,
        'todays_all_leads_count': todays_all_leads_count,
        'todays_all_leads_page': todays_all_leads_page,
        'todays_walkin_count': todays_walkin_count,
        'daily_target': daily_target,
        'calls_completed_today': calls_completed_today,
        'target_remaining': target_remaining,
        'my_recent_leads': my_recent_leads,
        'todays_tasks': todays_tasks,
        'upcoming_opd_leads': upcoming_opd_leads,
        'upcoming_opd_count': upcoming_opd_count,
        'todays_appointments_list': todays_appointments_list,
        'todays_apts_page': todays_apts_page,
        'todays_appointments_count': todays_appointments_count,
        'upcoming_appointments_list': upcoming_appointments_list,
        'upcoming_apts_page': upcoming_apts_page,
        'upcoming_appointments_count': upcoming_appointments_count,
        'admitted_appointments_list': admitted_appointments_list,
        'admitted_apts_page': admitted_apts_page,
        'admitted_appointments_count': admitted_appointments_count,
        'approval_pending_appointments_list': approval_pending_appointments_list,
        'approval_pending_apts_page': approval_pending_apts_page,
        'approval_pending_appointments_count': approval_pending_appointments_count,
        'total_appointments_count': total_appointments_count,
        'payment_done_list': payment_done_list,
        'payment_done_page': payment_done_page,
        'payment_done_count': payment_done_count,
        'payment_pending_list': payment_pending_list,
        'payment_pending_page': payment_pending_page,
        'payment_pending_count': payment_pending_count,
        'total_billings_count': total_billings_count,
        'pending_and_upcoming_followups': pending_and_upcoming_followups,
        'pending_and_upcoming_followups_count': pending_and_upcoming_followups_count,
        'today_date': today_date,
    }
    return render(request, "hospital/dashboard/telecaller_home.html", context)

@login_required
def telecaller_tab_data_api(request):
    """
    Lazy / Asynchronous AJAX Endpoint to load individual tab tables on-demand.
    Significantly improves performance and eliminates initial dashboard freeze.
    """
    from accounts.models import User
    from leads.models import Lead, LeadTemperature, DealStatus, Appointment, AppointmentStatus, PaymentStatus
    from followups.models import FollowUp
    from datetime import date, datetime
    from django.db.models import Q, Sum

    from django.http import JsonResponse
    from django.template.loader import render_to_string

    if not request.user.hospital:
        return JsonResponse({"status": "error", "message": "Access Denied"}, status=403)

    user = request.user
    today_date = timezone.localdate()
    start_of_today = timezone.make_aware(datetime.combine(today_date, datetime.min.time()))
    end_of_today = timezone.make_aware(datetime.combine(today_date, datetime.max.time()))
    tab = request.GET.get('tab', '').strip()
    hospital_leads = Lead.objects.filter(hospital=user.hospital, is_archived=False)

    def safe_format_dt(dt):
        if not dt:
            return '—'
        if isinstance(dt, datetime):
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt, timezone.get_current_timezone())
            return timezone.localtime(dt).strftime('%d %b %Y, %I:%M %p')
        elif isinstance(dt, date):
            return dt.strftime('%d %b %Y')
        return str(dt)

    leads = []

    if tab == 'assigned_leads':
        if user.role == User.Role.LEAD_ATTENDENT:
            assigned_candidates = hospital_leads.filter(assigned_to=user)
        else:
            assigned_candidates = hospital_leads.filter(assigned_to__isnull=False)

        assigned_cnd_ids = filter_uncontacted_leads_ids(assigned_candidates, today=today_date)
        qs = hospital_leads.filter(id__in=assigned_cnd_ids).select_related('stage', 'campaign', 'lead_source').order_by('-created_at')
        leads = list(qs[:100])

    elif tab == 'walkin_leads':
        qs = hospital_leads.filter(
            Q(lead_source__name__icontains='walk-in') |
            Q(custom_data__lead_source__icontains='walk-in') |
            Q(custom_data__source__icontains='walk-in') |
            Q(lead_type__icontains='walk')
        ).filter(
            Q(created_at__range=(start_of_today, end_of_today)) | Q(inquiry_date=today_date)
        ).select_related('stage', 'campaign', 'lead_source').order_by('-created_at')
        leads = list(qs[:100])

    elif tab == 'todays_all_leads':
        qs = hospital_leads.filter(
            Q(created_at__range=(start_of_today, end_of_today)) | Q(inquiry_date=today_date)
        ).select_related('stage', 'campaign', 'lead_source', 'assigned_to').prefetch_related('followups', 'appointments').order_by('-created_at')
        leads = list(qs[:100])

    elif tab in ['followups_todays', 'followups_upcoming', 'followups_overdue']:
        booked_exclude_tele = (
            Q(custom_data__appointment_status__icontains='Book') |
            Q(custom_data__appointment_status__icontains='Confirm') |
            Q(deal_status__in=[DealStatus.WON, DealStatus.LOST]) |
            Q(admission_status='ADMISSION_DONE') |
            (Q(custom_data__total__isnull=False) & ~Q(custom_data__total__in=['0', '0.00', '', '0.0', 0, 0.0]))
        )
        user_fu_qs = hospital_leads.filter(
            assigned_to=user
        ).exclude(
            booked_exclude_tele
        ).filter(
            Q(next_followup_date__isnull=False) |
            Q(followups__next_followup_date__isnull=False) |
            Q(custom_data__appointment_status__icontains='follow')
        ).distinct().select_related('stage', 'campaign', 'lead_source').prefetch_related('followups')

        for l in user_fu_qs:
            sched_date = extract_lead_followup_date(l)
            if not sched_date:
                continue

            latest_fu = l.followups.order_by('-id').first()
            has_status_update = False
            if latest_fu and latest_fu.followup_status not in ('PENDING', 'CALL_BACK', 'RESCHEDULED') and latest_fu.followup_date and latest_fu.followup_date >= sched_date:
                has_status_update = True

            l.is_overdue = bool(sched_date < today_date)

            if tab == 'followups_todays' and sched_date == today_date:
                leads.append(l)
            elif tab == 'followups_upcoming' and sched_date > today_date and not has_status_update:
                leads.append(l)
            elif tab == 'followups_overdue' and sched_date < today_date and not has_status_update:
                leads.append(l)

        leads = leads[:100]

    elif tab == 'followups_completed':
        completed_fu_qs = hospital_leads.filter(
            assigned_to=user
        ).filter(
            Q(followups__followup_status__in=['COMPLETED', 'DONE', 'INTERESTED']) |
            Q(stage__name__iexact='Payment Done') |
            Q(custom_data__appointment_status__icontains='Complete') |
            Q(custom_data__deal_status__icontains='Won')
        ).distinct().select_related('stage', 'campaign', 'lead_source').order_by('-updated_at')
        leads = list(completed_fu_qs[:100])

    elif tab in ['appointments_todays', 'appointments_upcoming', 'appointments_admitted', 'appointments_pending']:
        def get_lead_appointment_info(l):
            cd = l.custom_data or {}
            apt_date_str = cd.get('appo_booked_date') or cd.get('appointment_date') or cd.get('booking_date')
            apt_time_str = cd.get('appointment_time') or cd.get('booking_time') or cd.get('time') or 'Scheduled Slot'
            apt_status_str = str(cd.get('appointment_status') or cd.get('deal_status') or (l.stage.name if l.stage else '')).strip()
            
            parsed_dt = None
            if apt_date_str:
                for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y'):
                    try:
                        parsed_dt = datetime.strptime(str(apt_date_str).strip()[:10], fmt).date()
                        break
                    except Exception:
                        pass

            if hasattr(l, '_prefetched_objects_cache') and 'appointments' in l._prefetched_objects_cache:
                apts = sorted(l.appointments.all(), key=lambda x: (x.appointment_date, x.id), reverse=True)
                linked_apt = apts[0] if apts else None
            else:
                linked_apt = l.appointments.order_by('-appointment_date', '-id').first() if hasattr(l, 'appointments') else None

            if linked_apt:
                if not parsed_dt:
                    parsed_dt = linked_apt.appointment_date
                if linked_apt.appointment_time:
                    apt_time_str = linked_apt.appointment_time.strftime('%I:%M %p')
                if linked_apt.status:
                    apt_status_str = linked_apt.get_status_display()

            is_opd = 'opd' in apt_status_str.lower() or 'opd' in str(cd.get('department') or cd.get('disease') or '').lower() or 'opd' in str(l.course.name if l.course else '').lower()
            apt_type = 'OPD' if is_opd else 'Consultation'
            return parsed_dt, apt_time_str, apt_status_str, apt_type

        appointment_candidate_leads = hospital_leads.filter(
            assigned_to=user
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
        ).filter(
            Q(appointments__isnull=False) |
            Q(custom_data__appo_booked_date__isnull=False) |
            Q(custom_data__appointment_date__isnull=False) |
            Q(custom_data__appointment_status__isnull=False) |
            Q(stage__name__icontains='book') |
            Q(stage__name__icontains='appoint')
        ).distinct().select_related('stage', 'campaign', 'lead_source', 'course').prefetch_related('appointments')

        for l in appointment_candidate_leads[:200]:
            p_dt, p_time, p_st, p_type = get_lead_appointment_info(l)
            if not p_dt and not (hasattr(l, '_prefetched_objects_cache') and l.appointments.all()) and not any(k in p_st.lower() for k in ['book', 'confirm', 'approv', 'await', 'sched']):
                continue

            l.appointment_scheduled_date = p_dt
            l.appointment_scheduled_time = p_time
            l.appointment_display_status = p_st
            l.appointment_type_label = p_type

            p_st_lower = p_st.lower()
            is_completed = 'complet' in p_st_lower or 'done' in p_st_lower or l.deal_status == DealStatus.WON
            is_pending_approval = 'await' in p_st_lower or 'pending' in p_st_lower or 'pending approval' in p_st_lower
            is_confirmed_or_booked = any(k in p_st_lower for k in ['confirm', 'book', 'sched', 'approved', 'yes'])

            if tab == 'appointments_pending' and is_pending_approval and not is_completed:
                leads.append(l)
            elif tab == 'appointments_todays':
                if (p_dt == today_date and not is_completed) or (not p_dt and is_confirmed_or_booked and not is_completed):
                    leads.append(l)
            elif tab == 'appointments_upcoming' and p_dt and p_dt > today_date and not is_completed:
                leads.append(l)
            elif tab == 'appointments_admitted' and p_dt and p_dt < today_date and not is_completed and is_confirmed_or_booked:
                leads.append(l)

        leads = leads[:100]

    elif tab in ['billings_pending', 'billings_done']:
        billing_candidate_leads = hospital_leads.filter(
            assigned_to=user
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
        ).filter(
            Q(stage__name__icontains='payment') |
            Q(stage__name__icontains='complete') |
            Q(stage__name__icontains='done') |
            Q(deal_status=DealStatus.WON) |
            Q(custom_data__deal_status__icontains='Won') |
            Q(custom_data__deal_status__icontains='Payment') |
            Q(custom_data__appointment_status__icontains='Complete') |
            Q(custom_data__appointment_status__icontains='Done') |
            Q(custom_data__appointment_status__icontains='Payment') |
            Q(custom_data__total__isnull=False) |
            Q(custom_data__total_paid__isnull=False)
        ).distinct().select_related('stage', 'campaign', 'lead_source', 'course').prefetch_related('appointments', 'admission__payments').order_by('-updated_at')

        for l in list(billing_candidate_leads[:150]):
            cd = l.custom_data or {}
            st_name = (l.stage.name if l.stage else '').strip().lower()
            raw_ds = str(cd.get('deal_status') or '').strip().lower()
            raw_apt = str(cd.get('appointment_status') or '').strip().lower()
            
            tot = 0.0
            try:
                tot = float(cd.get('total_paid') or cd.get('total') or cd.get('opd_bill') or cd.get('pharmacy_bill') or 0.0)
            except (ValueError, TypeError):
                tot = 0.0

            has_payment_record = False
            if hasattr(l, 'admission') and l.admission and hasattr(l.admission, 'payments') and l.admission.payments.filter(payment_status=PaymentStatus.SUCCESS).exists():
                has_payment_record = True
                tot = float(l.admission.payments.filter(payment_status=PaymentStatus.SUCCESS).aggregate(s=Sum('amount'))['s'] or tot)

            l.display_billed_amount = tot
            l.opd_bill_amt = cd.get('opd_bill') or 0
            l.pharmacy_bill_amt = cd.get('pharmacy_bill') or 0

            done_dt = None
            if hasattr(l, 'appointments') and l.appointments.filter(status=AppointmentStatus.COMPLETED).exists():
                comp_apt = l.appointments.filter(status=AppointmentStatus.COMPLETED).order_by('-updated_at').first()
                done_dt = comp_apt.updated_at or comp_apt.appointment_date

            if not done_dt:
                raw_done = cd.get('appointment_done_date') or cd.get('opd_done_date') or cd.get('appointment_confirmed_at')
                if raw_done:
                    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%d-%m-%Y %H:%M:%S', '%d-%m-%Y'):
                        try:
                            done_dt = datetime.strptime(str(raw_done).strip()[:19], fmt)
                            break
                        except Exception:
                            pass

            if not done_dt:
                done_dt = l.updated_at

            l.appointment_done_display = safe_format_dt(done_dt)

            is_opd_completed = (
                any(k in raw_apt for k in ['complete', 'done', 'visited', 'attended']) or
                any(k in raw_ds for k in ['complete', 'done', 'visited', 'attended']) or
                st_name in ['appointment completed', 'opd done', 'completed', 'consultation complete', 'visited', 'payment pending', 'payment done'] or
                cd.get('done') or
                (hasattr(l, 'appointments') and l.appointments.filter(status=AppointmentStatus.COMPLETED).exists())
            )

            is_paid = (
                tot > 0 or
                has_payment_record or
                'payment done' in raw_apt or
                'payment done' in raw_ds or
                'won' in raw_ds or
                st_name in ['payment done', 'won'] or
                l.deal_status == DealStatus.WON
            )

            if tab == 'billings_done' and is_paid:
                leads.append(l)
            elif tab == 'billings_pending' and not is_paid and (is_opd_completed or 'payment pending' in st_name or 'payment pending' in raw_apt or 'payment pending' in raw_ds):
                leads.append(l)

        leads = leads[:100]

    elif tab == 'lost':
        lost_qs = hospital_leads.filter(
            assigned_to=user
        ).filter(
            Q(deal_status=DealStatus.LOST) |
            Q(admission_status__in=['LOST', 'CANCELLED', 'DROPOUT']) |
            Q(temperature=LeadTemperature.FREEZE) |
            Q(stage__name__icontains='lost') |
            Q(stage__name__icontains='cancel') |
            Q(custom_data__deal_status__icontains='Lost') |
            Q(custom_data__deal_status__icontains='Cancel') |
            Q(custom_data__appointment_status__icontains='Cancel') |
            Q(custom_data__appointment_status__icontains='Not Int')
        ).distinct().select_related('stage', 'campaign', 'lead_source').prefetch_related('followups').order_by('-updated_at')

        for l in list(lost_qs[:100]):
            closing_dt = l.updated_at or l.created_at
            fu_cnt = len(l.followups.all()) if (hasattr(l, '_prefetched_objects_cache') and 'followups' in l._prefetched_objects_cache) else l.followup_count
            l.display_followup_count = fu_cnt
            l.closing_datetime_display = safe_format_dt(closing_dt)
            leads.append(l)

    total_count = lost_qs.count() if tab == 'lost' and 'lost_qs' in locals() else len(leads)
    html_content = render_to_string("hospital/dashboard/_telecaller_tab_content.html", {
        "tab": tab,
        "leads": leads,
        "total_count": total_count,
        "today_date": today_date,
    }, request=request)

    return JsonResponse({
        "status": "success",
        "tab": tab,
        "count": len(leads),
        "html": html_content,
    })

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
        leads = leads.filter(Q(admission_status=AdmissionStatus.WON) | Q(admission_status='ADMISSION_DONE') | Q(deal_status=DealStatus.WON))
    elif converted_filter == 'no':
        leads = leads.exclude(Q(admission_status=AdmissionStatus.WON) | Q(admission_status='ADMISSION_DONE') | Q(deal_status=DealStatus.WON))

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

    filter_appointment_statuses = ["Booked", "Booking Done", "Pending Confirmation", "Awaiting Approval from Doctor", "Visited / OPD Done", "Cancelled", "Not Interested", "Payment Done"]
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

    filter_appointment_statuses = ["Booked", "Booking Done", "Pending Confirmation", "Awaiting Approval from Doctor", "Visited / OPD Done", "Cancelled", "Not Interested", "Payment Done"]
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

