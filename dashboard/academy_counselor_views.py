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
def academy_counselor_home(request):
    """
    Dedicated Academy Counselor Dashboard
    Features 6 specialized tabs with subtabs matching the student enrollment lifecycle.
    100% independent from other business types.
    """
    user = request.user
    today_date = timezone.localdate()
    start_of_today = timezone.make_aware(datetime.combine(today_date, datetime.min.time()))
    end_of_today = timezone.make_aware(datetime.combine(today_date, datetime.max.time()))

    # Base Queryset: Scoped strictly to this Academy business
    active_business = get_active_academy_business(user)
    business_id = getattr(active_business, 'id', None) or getattr(user, 'business_id', None)

    leads_qs = Lead.objects.filter(is_archived=False)
    if business_id:
        leads_qs = leads_qs.filter(hospital_id=business_id)
    else:
        leads_qs = leads_qs.none()

    # Scoping for Counselor vs Admin / Manager
    is_management = user.role in (User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER)
    if not is_management and not user.can_view_all_leads:
        assigned_filter = Q(assigned_to=user)
    else:
        assigned_filter = Q()

    user_scoped_leads = leads_qs.filter(assigned_filter) if assigned_filter else leads_qs

    # Current tab and subtab parameters from URL
    cur_tab = request.GET.get('tab', 'myleads').strip().lower()
    cur_subtab = request.GET.get('subtab', '').strip().lower()
    req_page = request.GET.get('page') or 1

    # =========================================================================
    # TAB 1: MY LEADS (Fresh, Uncontacted, Walk-in, Today's Inquiry)
    # =========================================================================
    # 1.1 Fresh Leads: Unassigned leads, meta ads, imports without counselor assignment
    fresh_leads_qs = leads_qs.filter(
        assigned_to__isnull=True
    ).exclude(
        deal_status=DealStatus.LOST
    ).exclude(
        stage__name__icontains='cancel'
    ).exclude(
        stage__name__icontains='lost'
    ).select_related('course', 'campaign', 'lead_source', 'stage').order_by('-created_at')
    fresh_leads_count = fresh_leads_qs.count()

    # 1.2 Uncontacted Leads: Assigned to user, but NO user remarks/follow-ups added yet
    cnd_candidates = user_scoped_leads.filter(assigned_to__isnull=False)
    if not is_management:
        cnd_candidates = cnd_candidates.filter(assigned_to=user)
    uncontacted_ids = filter_uncontacted_leads_ids(cnd_candidates, today=today_date)
    uncontacted_leads_qs = leads_qs.filter(id__in=uncontacted_ids).select_related('course', 'campaign', 'lead_source', 'stage', 'assigned_to').order_by('-created_at')
    uncontacted_leads_count = len(uncontacted_ids)

    # 1.3 Walk-in Leads: Originating from direct campus walk-in source
    walkin_leads_qs = user_scoped_leads.filter(
        Q(lead_source__name__icontains='walk-in') |
        Q(lead_source__name__icontains='walk in') |
        Q(custom_data__lead_source__icontains='walk') |
        Q(lead_type__icontains='walk')
    ).exclude(
        deal_status=DealStatus.LOST
    ).exclude(
        stage__name__icontains='cancel'
    ).select_related('course', 'campaign', 'lead_source', 'stage', 'assigned_to').order_by('-created_at')
    walkin_leads_count = walkin_leads_qs.count()

    # 1.4 Today's Inquiry: Total inquiries received or created today
    todays_inquiry_qs = user_scoped_leads.filter(
        Q(created_at__range=(start_of_today, end_of_today)) | Q(inquiry_date=today_date)
    ).select_related('course', 'campaign', 'lead_source', 'stage', 'assigned_to').order_by('-created_at')
    todays_inquiry_count = todays_inquiry_qs.count()

    total_myleads_count = uncontacted_leads_count + fresh_leads_count + walkin_leads_count

    # =========================================================================
    # TAB 2: FOLLOW-UPS (Stage: Follow up needed)
    # Strictly leads that are actively in follow-up pipeline
    # =========================================================================
    fu_base_qs = user_scoped_leads.exclude(
        deal_status__in=[DealStatus.WON, DealStatus.LOST]
    ).exclude(
        admission_status__in=[AdmissionStatus.WON, 'ADMISSION_DONE', 'CANCELLED']
    ).exclude(
        stage__name__icontains='cancel'
    ).exclude(
        stage__name__icontains='lost'
    ).exclude(
        stage__name__icontains='payment'
    ).exclude(
        stage__name__icontains='visit planned'
    ).exclude(
        stage__name__icontains='admission planned'
    ).filter(
        Q(stage__name__icontains='follow') |
        Q(next_followup_date__isnull=False) |
        Q(followups__next_followup_date__isnull=False)
    ).distinct().select_related('course', 'stage', 'assigned_to', 'lead_source').prefetch_related('followups')

    todays_fu_list = []
    overdue_fu_list = []
    upcoming_fu_list = []

    for l in list(fu_base_qs[:300]):
        sched_date = extract_lead_followup_date(l) or l.next_followup_date
        if not sched_date:
            continue
        l.scheduled_followup_date = sched_date
        cd = l.custom_data or {}
        l.followup_agenda = cd.get('followup_remark') or l.notes or 'General Counseling Call'

        if sched_date == today_date:
            todays_fu_list.append(l)
        elif sched_date < today_date:
            overdue_fu_list.append(l)
        elif sched_date > today_date:
            upcoming_fu_list.append(l)

    todays_fu_count = len(todays_fu_list)
    overdue_fu_count = len(overdue_fu_list)
    upcoming_fu_count = len(upcoming_fu_list)

    # 2.4 Completed History: Leads whose counseling follow-ups are completed
    completed_fu_qs = user_scoped_leads.filter(
        followups__followup_status__in=[FollowUpStatus.COMPLETED, 'DONE']
    ).exclude(
        deal_status=DealStatus.LOST
    ).exclude(
        stage__name__icontains='cancel'
    ).distinct().select_related('course', 'stage', 'assigned_to').order_by('-updated_at')
    completed_fu_count = completed_fu_qs.count()

    total_followups_count = todays_fu_count + overdue_fu_count + upcoming_fu_count

    # =========================================================================
    # TAB 3: VISIT PLANNED (Campus Visits & Demo Sessions)
    # Strictly leads in Visit Planned pipeline
    # =========================================================================
    visit_base_qs = user_scoped_leads.exclude(
        deal_status=DealStatus.LOST
    ).exclude(
        stage__name__icontains='cancel'
    ).exclude(
        stage__name__icontains='lost'
    ).filter(
        Q(stage__name__icontains='visit planned') |
        Q(custom_data__visit_date__isnull=False)
    ).distinct().select_related('course', 'stage', 'assigned_to', 'lead_source').order_by('-updated_at')

    todays_visits_list = []
    upcoming_visits_list = []
    overdue_visits_list = []
    visited_completed_list = []

    for l in list(visit_base_qs[:200]):
        cd = l.custom_data or {}
        v_date_str = cd.get('visit_date')
        v_date = None
        if v_date_str:
            for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y'):
                try:
                    v_date = datetime.strptime(str(v_date_str).strip()[:10], fmt).date()
                    break
                except Exception:
                    pass
        if not v_date:
            v_date = l.next_followup_date

        l.display_visit_date = v_date
        l.display_visit_slot = cd.get('visit_slot') or 'Scheduled Slot'
        l.display_visit_notes = cd.get('visit_remark') or 'Campus Demo'

        st_name = (l.stage.name if l.stage else '').strip().lower()
        is_visited_done = 'visited' in st_name or cd.get('visit_status') == 'DONE' or cd.get('visited') == True

        if is_visited_done:
            visited_completed_list.append(l)
        elif v_date:
            if v_date == today_date:
                todays_visits_list.append(l)
            elif v_date > today_date:
                upcoming_visits_list.append(l)
            elif v_date < today_date:
                overdue_visits_list.append(l)
        else:
            todays_visits_list.append(l)

    todays_visits_count = len(todays_visits_list)
    upcoming_visits_count = len(upcoming_visits_list)
    overdue_visits_count = len(overdue_visits_list)
    visited_completed_count = len(visited_completed_list)
    total_visits_count = todays_visits_count + upcoming_visits_count + overdue_visits_count

    # =========================================================================
    # TAB 4: ADMISSIONS (4.1 Admission Planned, 4.2 Admission Done)
    # =========================================================================
    # 4.1 Admission Planned: Expected future enrollment date
    adm_planned_base = user_scoped_leads.exclude(
        deal_status=DealStatus.LOST
    ).exclude(
        stage__name__icontains='cancel'
    ).exclude(
        stage__name__icontains='lost'
    ).filter(
        Q(stage__name__icontains='admission planned') |
        Q(custom_data__admission_date__isnull=False, deal_status=DealStatus.INTERESTED)
    ).exclude(
        deal_status=DealStatus.WON
    ).exclude(
        stage__name__icontains='admission & payment'
    ).distinct().select_related('course', 'stage', 'assigned_to', 'lead_source').order_by('-updated_at')

    adm_planned_list = []
    for l in list(adm_planned_base[:150]):
        cd = l.custom_data or {}
        l.expected_adm_date = cd.get('admission_date') or l.next_followup_date
        l.adm_remarks = cd.get('admission_remark') or cd.get('remark_1') or 'Seat commitment pending'
        adm_planned_list.append(l)
    adm_planned_count = len(adm_planned_list)

    # 4.2 Admission Done: Enrolled students (Won conversions)
    adm_done_base = user_scoped_leads.filter(
        Q(deal_status=DealStatus.WON) |
        Q(admission_status=AdmissionStatus.WON) |
        Q(stage__name__icontains='admission & payment') |
        Q(stage__name__icontains='admission done') |
        Q(custom_data__deal_status__iexact='WON')
    ).distinct().select_related('course', 'stage', 'assigned_to', 'lead_source').order_by('-updated_at')

    adm_done_list = []
    for l in list(adm_done_base[:150]):
        cd = l.custom_data or {}
        l.final_adm_date = cd.get('admission_date') or (l.updated_at.date() if l.updated_at else today_date)
        l.trainer_name = cd.get('trainer') or cd.get('selected_tutor') or 'Unassigned'
        l.batch_name = cd.get('batch') or cd.get('selected_batch') or 'Regular'
        l.batch_time = cd.get('batch_time') or cd.get('selected_batch_timing') or 'Morning'
        adm_done_list.append(l)
    adm_done_count = len(adm_done_list)
    total_admissions_count = adm_planned_count + adm_done_count

    # =========================================================================
    # TAB 5: PAYMENTS (5.1 Payment Pending, 5.2 Partial/Token Done, 5.3 Full Paid)
    # Dual tracking with Tab 4 for financial ledger & EMI installments
    # =========================================================================
    billing_candidates = user_scoped_leads.filter(
        Q(deal_status=DealStatus.WON) |
        Q(stage__name__icontains='admission & payment') |
        Q(stage__name__icontains='payment') |
        Q(custom_data__total__isnull=False) |
        Q(custom_data__total_paid__isnull=False)
    ).exclude(
        deal_status=DealStatus.LOST
    ).exclude(
        stage__name__icontains='cancel'
    ).distinct().select_related('course', 'stage', 'assigned_to').order_by('-updated_at')

    payment_pending_list = []
    partial_payment_list = []
    full_payment_list = []

    for l in list(billing_candidates[:200]):
        cd = l.custom_data or {}
        try:
            total_fee = float(cd.get('total') or cd.get('course_fee') or (l.course.base_price if l.course else 0) or 0)
        except (ValueError, TypeError):
            total_fee = 0.0

        try:
            token_paid = float(cd.get('token_amount') or cd.get('total_paid') or 0)
        except (ValueError, TypeError):
            token_paid = 0.0

        try:
            balance_due = float(cd.get('balance_amount') or max(0, total_fee - token_paid))
        except (ValueError, TypeError):
            balance_due = max(0, total_fee - token_paid)

        l.billed_total = total_fee
        l.billed_paid = token_paid
        l.billed_balance = balance_due
        l.billing_mode = cd.get('payment_option') or ('EMI' if cd.get('emi_months') else 'ONE_TIME')
        l.next_emi_date = cd.get('next_emi_date') or '—'

        if token_paid == 0 and total_fee > 0:
            payment_pending_list.append(l)
        elif balance_due > 0 and token_paid > 0:
            partial_payment_list.append(l)
        elif total_fee > 0 and balance_due <= 0:
            full_payment_list.append(l)
        elif l.deal_status == DealStatus.WON:
            full_payment_list.append(l)

    payment_pending_count = len(payment_pending_list)
    partial_payment_count = len(partial_payment_list)
    full_payment_count = len(full_payment_list)
    total_payments_count = payment_pending_count + partial_payment_count + full_payment_count

    # =========================================================================
    # TAB 6: LOST / CANCELLED LEADS (Cancelled, Dropped out, Freeze leads)
    # =========================================================================
    lost_qs = user_scoped_leads.filter(
        Q(deal_status=DealStatus.LOST) |
        Q(admission_status__in=[AdmissionStatus.LOST, 'CANCELLED', 'DROPOUT']) |
        Q(temperature=LeadTemperature.FREEZE) |
        Q(stage__name__icontains='cancel') |
        Q(stage__name__icontains='lost')
    ).distinct().select_related('course', 'stage', 'assigned_to', 'lead_source').order_by('-updated_at')

    lost_leads_list = []
    for l in list(lost_qs[:150]):
        cd = l.custom_data or {}
        l.lost_reason = cd.get('cancellation_reason') or l.notes or 'Not Interested / Fees Issue'
        l.lost_closing_date = l.updated_at.date() if l.updated_at else today_date
        lost_leads_list.append(l)
    lost_leads_count = lost_qs.count()

    # Pagination Helper (20 leads per page as requested)
    def paginate_dataset(dataset, page_num, per_page=20):
        if hasattr(dataset, 'count'):
            paginator = Paginator(dataset, per_page)
        else:
            paginator = Paginator(dataset, per_page)
        try:
            return paginator.page(page_num)
        except (EmptyPage, PageNotAnInteger):
            return paginator.page(1)

    # Paginate currently selected subtab data cleanly
    p_fresh = paginate_dataset(fresh_leads_qs, req_page if (cur_tab == 'myleads' and cur_subtab in ('fresh', '')) else 1)
    p_uncontacted = paginate_dataset(uncontacted_leads_qs, req_page if (cur_tab == 'myleads' and cur_subtab == 'uncontacted') else 1)
    p_walkin = paginate_dataset(walkin_leads_qs, req_page if (cur_tab == 'myleads' and cur_subtab == 'walkin') else 1)
    p_todays_inquiry = paginate_dataset(todays_inquiry_qs, req_page if (cur_tab == 'myleads' and cur_subtab == 'todays_inquiry') else 1)

    p_todays_fu = paginate_dataset(todays_fu_list, req_page if (cur_tab == 'followups' and cur_subtab in ('today_fu', '')) else 1)
    p_overdue_fu = paginate_dataset(overdue_fu_list, req_page if (cur_tab == 'followups' and cur_subtab == 'overdue_fu') else 1)
    p_upcoming_fu = paginate_dataset(upcoming_fu_list, req_page if (cur_tab == 'followups' and cur_subtab == 'upcoming_fu') else 1)
    p_completed_fu = paginate_dataset(completed_fu_qs, req_page if (cur_tab == 'followups' and cur_subtab == 'completed_fu') else 1)

    p_todays_visits = paginate_dataset(todays_visits_list, req_page if (cur_tab == 'visits' and cur_subtab in ('today_visits', '')) else 1)
    p_upcoming_visits = paginate_dataset(upcoming_visits_list, req_page if (cur_tab == 'visits' and cur_subtab == 'upcoming_visits') else 1)
    p_overdue_visits = paginate_dataset(overdue_visits_list, req_page if (cur_tab == 'visits' and cur_subtab == 'overdue_visits') else 1)
    p_visited_done = paginate_dataset(visited_completed_list, req_page if (cur_tab == 'visits' and cur_subtab == 'visited_done') else 1)

    p_adm_planned = paginate_dataset(adm_planned_list, req_page if (cur_tab == 'admissions' and cur_subtab in ('planned', '')) else 1)
    p_adm_done = paginate_dataset(adm_done_list, req_page if (cur_tab == 'admissions' and cur_subtab == 'done') else 1)

    p_payment_pending = paginate_dataset(payment_pending_list, req_page if (cur_tab == 'payments' and cur_subtab in ('pending', '')) else 1)
    p_partial_payment = paginate_dataset(partial_payment_list, req_page if (cur_tab == 'payments' and cur_subtab == 'partial') else 1)
    p_full_payment = paginate_dataset(full_payment_list, req_page if (cur_tab == 'payments' and cur_subtab == 'full') else 1)

    p_lost = paginate_dataset(lost_leads_list, req_page if (cur_tab == 'lost') else 1)

    # Calling target countdown for counselor
    daily_target = getattr(user, 'daily_call_target', 40) or 40
    user_today_fu_lead_ids = set(FollowUp.objects.filter(
        created_by=user,
        created_at__range=(start_of_today, end_of_today)
    ).values_list('lead_id', flat=True))
    calls_completed_today = len(user_today_fu_lead_ids)
    target_remaining = max(0, daily_target - calls_completed_today)

    context = {
        'active': 'counselor_dashboard',
        'is_management': is_management,
        'today_date': today_date,
        'cur_tab': cur_tab,
        'cur_subtab': cur_subtab,

        # Metrics / Target
        'daily_target': daily_target,
        'calls_completed_today': calls_completed_today,
        'target_remaining': target_remaining,
        'bulk_self_assign_limit': getattr(user, 'bulk_self_assign_limit', 25) or 25,

        # Tab 1: My Leads
        'total_myleads_count': total_myleads_count,
        'fresh_leads_count': fresh_leads_count,
        'uncontacted_leads_count': uncontacted_leads_count,
        'walkin_leads_count': walkin_leads_count,
        'todays_inquiry_count': todays_inquiry_count,
        'p_fresh': p_fresh,
        'p_uncontacted': p_uncontacted,
        'p_walkin': p_walkin,
        'p_todays_inquiry': p_todays_inquiry,

        # Tab 2: Follow-ups
        'total_followups_count': total_followups_count,
        'todays_fu_count': todays_fu_count,
        'overdue_fu_count': overdue_fu_count,
        'upcoming_fu_count': upcoming_fu_count,
        'completed_fu_count': completed_fu_count,
        'p_todays_fu': p_todays_fu,
        'p_overdue_fu': p_overdue_fu,
        'p_upcoming_fu': p_upcoming_fu,
        'p_completed_fu': p_completed_fu,

        # Tab 3: Visits Planned
        'total_visits_count': total_visits_count,
        'todays_visits_count': todays_visits_count,
        'upcoming_visits_count': upcoming_visits_count,
        'overdue_visits_count': overdue_visits_count,
        'visited_completed_count': visited_completed_count,
        'p_todays_visits': p_todays_visits,
        'p_upcoming_visits': p_upcoming_visits,
        'p_overdue_visits': p_overdue_visits,
        'p_visited_done': p_visited_done,

        # Tab 4: Admissions
        'total_admissions_count': total_admissions_count,
        'adm_planned_count': adm_planned_count,
        'adm_done_count': adm_done_count,
        'p_adm_planned': p_adm_planned,
        'p_adm_done': p_adm_done,

        # Tab 5: Payments
        'total_payments_count': total_payments_count,
        'payment_pending_count': payment_pending_count,
        'partial_payment_count': partial_payment_count,
        'full_payment_count': full_payment_count,
        'p_payment_pending': p_payment_pending,
        'p_partial_payment': p_partial_payment,
        'p_full_payment': p_full_payment,

        # Tab 6: Lost
        'lost_leads_count': lost_leads_count,
        'p_lost': p_lost,
    }

    return render(request, "academy/dashboard/counselor_home.html", context)


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
    if business_id:
        leads_base = leads_base.filter(hospital_id=business_id)
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

    # 7. Source Filter
    source_id = request.GET.get("lead_source") or request.GET.get("source")
    if source_id:
        if source_id.isdigit():
            leads = leads.filter(lead_source_id=int(source_id))
        else:
            leads = leads.filter(
                Q(lead_source__name__iexact=source_id) |
                Q(custom_data__lead_source__iexact=source_id)
            )

    # 8. Campaign Filter
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

    funnels = LeadStage.objects.filter(leads__in=leads).annotate(cnt=Count("leads")).order_by("order", "name")
    funnel_labels = [st.name for st in funnels]
    funnel_counts = [st.cnt for st in funnels]

    courses_stat = Course.objects.filter(leads__in=leads).annotate(cnt=Count("leads")).order_by("-cnt")[:8]
    course_labels = [c.name for c in courses_stat]
    course_counts = [c.cnt for c in courses_stat]

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
    if business_id:
        team_members = User.objects.filter(
            hospital_id=business_id, is_active=True
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

    pending_approvals_count = User.objects.filter(hospital_id=business_id, is_active=False).count() if business_id else 0

    # Business-Scoped Filter Dropdown Options
    # Courses
    business_courses = Course.objects.filter(hospital_id=business_id, is_active=True) if business_id else Course.objects.filter(hospital__isnull=True, is_active=True)
    if not business_courses.exists():
        business_courses = Course.objects.filter(is_active=True)

    # Trainers (Dedicated Trainer master + Course tutors + Staff trainers)
    from leads.models import Trainer
    business_trainers = []
    trainers_qs = Trainer.objects.filter(is_active=True)
    if business_id:
        trainers_qs = trainers_qs.filter(hospital_id=business_id)
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
    if business_id:
        counselors = User.objects.filter(
            hospital_id=business_id,
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
    lead_stage_ids = list(leads_base.filter(stage__isnull=False).values_list("stage_id", flat=True).distinct())
    stages = LeadStage.objects.filter(
        Q(is_active=True, business_type__in=[LeadStage.BusinessType.ACADEMY, LeadStage.BusinessType.ALL]) |
        Q(id__in=lead_stage_ids)
    )
    if business_id:
        stages = stages.filter(Q(hospital_id=business_id) | Q(hospital__isnull=True) | Q(id__in=lead_stage_ids))
    stages = stages.distinct().order_by("order", "name")

    # Campaigns (Business-specific)
    if business_id:
        campaigns = Campaign.objects.filter(hospital_id=business_id, is_active=True).order_by("name")
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

