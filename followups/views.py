import datetime
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.core.paginator import Paginator
from django.db.models import Q, Prefetch

from leads.models import Lead, DealStatus, Course, LeadStage
from .models import FollowUp, FollowUpStatus
from accounts.models import User, Hospital

from django.db.models import Subquery, OuterRef, Case, When, Value, CharField, DateField

def _filter_by_role(user, leads):
    # Strict tenant isolation: if user has a hospital/business assigned, only show that hospital's leads
    if user.hospital:
        leads = leads.filter(hospital=user.hospital)
    elif not (user.is_superuser or (user.role == User.Role.SUPER_ADMIN and not user.hospital)):
        # Default non-superadmin without hospital to academy (hospital__isnull=True)
        leads = leads.filter(hospital__isnull=True)
        
    if not user.can_view_all_leads:
        if user.can_view_team_leads:
            team = User.objects.filter(reports_to=user)
            leads = leads.filter(Q(assigned_to=user) | Q(assigned_to__in=team))
        elif user.can_view_assigned_leads:
            leads = leads.filter(assigned_to=user)
        else:
            leads = leads.none()
            
    return leads

def _board(request, leads, active, title, date_info=None):
    # Tenant context detection
    user = request.user
    hospital = user.hospital
    is_global_admin = user.is_superuser or (user.role == User.Role.SUPER_ADMIN and not user.hospital)
    
    selected_business_id = (
        request.GET.get("business", "").strip()
        or request.GET.get("hospital", "").strip()
        or str(request.session.get("active_business_id", "")).strip()
    )
    if is_global_admin and selected_business_id and selected_business_id.isdigit():
        hospital = Hospital.objects.filter(id=int(selected_business_id)).first()
        if hospital:
            leads = leads.filter(hospital=hospital)
            
    is_hospital_business = False
    if hospital:
        btype = (hospital.settings or {}).get("business_type", "")
        if not btype:
            name_lower = (hospital.name or "").lower()
            if "hospital" in name_lower or "clinic" in name_lower or "medical" in name_lower or "nelson" in name_lower:
                btype = "hospital"
        is_hospital_business = str(btype).strip().lower() == "hospital"
    elif user.industry == 'HOSPITAL':
        is_hospital_business = True

    # Search keyword filter
    q = request.GET.get('q', '').strip()
    if q:
        leads = leads.filter(
            Q(name__icontains=q) | 
            Q(mobile__icontains=q) | 
            Q(lead_code__icontains=q) |
            Q(city__icontains=q) |
            Q(course__name__icontains=q) |
            Q(custom_data__doctor__icontains=q) |
            Q(custom_data__department__icontains=q) |
            Q(assigned_to__first_name__icontains=q) |
            Q(assigned_to__last_name__icontains=q) |
            Q(assigned_to__username__icontains=q)
        )

    # Scoped filter options
    if is_hospital_business:
        team_members = User.objects.filter(
            is_active=True, is_approved=True,
            hospital=hospital,
            role__in=[User.Role.LEAD_ATTENDENT, User.Role.DOCTOR, User.Role.MANAGER, User.Role.ADMIN]
        ).order_by("first_name", "last_name", "username") if hospital else User.objects.none()
        courses = Course.objects.none()
        stages = LeadStage.objects.filter(is_active=True).filter(Q(hospital=hospital) | Q(business_type=LeadStage.BusinessType.HOSPITAL)).order_by("order", "name")
    else:
        team_members = User.objects.filter(
            is_active=True, is_approved=True,
            hospital=hospital,
            role__in=[User.Role.COUNSELLOR, User.Role.HR, User.Role.MANAGER]
        ).order_by("first_name", "username") if hospital else User.objects.filter(is_active=True, role__in=[User.Role.COUNSELLOR, User.Role.HR, User.Role.MANAGER]).order_by("first_name")
        courses = Course.objects.filter(is_active=True, hospital=hospital).order_by("name") if hospital else Course.objects.filter(is_active=True).order_by("name")
        stages = LeadStage.objects.filter(is_active=True, business_type=LeadStage.BusinessType.ACADEMY).order_by("order", "name")

    # Apply dropdown filters
    selected_user_id = request.GET.get("user_id", "").strip()
    if selected_user_id and selected_user_id.isdigit():
        leads = leads.filter(assigned_to_id=int(selected_user_id))

    selected_course_id = request.GET.get("course", "").strip()
    if selected_course_id and selected_course_id.isdigit():
        leads = leads.filter(course_id=int(selected_course_id))

    selected_stage_id = request.GET.get("stage", "").strip()
    if selected_stage_id and selected_stage_id.isdigit():
        leads = leads.filter(stage_id=int(selected_stage_id))

    selected_status = request.GET.get("followup_status", "").strip().upper()
    if selected_status:
        if selected_status == "PENDING":
            leads = leads.filter(latest_followup_st=FollowUpStatus.PENDING)
        elif selected_status == "RESCHEDULED":
            leads = leads.filter(latest_followup_st=FollowUpStatus.RESCHEDULED)
        elif selected_status in ("COMPLETED", "DONE"):
            leads = leads.filter(latest_followup_st=FollowUpStatus.COMPLETED)

    selected_admission_status = request.GET.get("admission_status", "").strip().upper()
    if selected_admission_status:
        leads = leads.filter(Q(admission_status=selected_admission_status) | Q(deal_status=selected_admission_status))

    selected_temperature = request.GET.get("temperature", "").strip().upper()
    if selected_temperature:
        leads = leads.filter(temperature=selected_temperature)

    # Date Range Filters
    date_from = request.GET.get("date_from", "").strip()
    date_to = request.GET.get("date_to", "").strip()

    if date_from:
        leads = leads.filter(
            Q(effective_followup_dt__gte=date_from) |
            (Q(effective_followup_dt__isnull=True) & Q(next_followup_date__gte=date_from))
        )
    if date_to:
        leads = leads.filter(
            Q(effective_followup_dt__lte=date_to) |
            (Q(effective_followup_dt__isnull=True) & Q(next_followup_date__lte=date_to))
        )

    # Distinct & Prefetch followups ordered by latest first
    leads = leads.distinct().select_related(
        "course", "stage", "assigned_to", "created_by"
    ).prefetch_related(
        Prefetch(
            "followups",
            queryset=FollowUp.objects.select_related("created_by").order_by("-followup_date", "-id"),
            to_attr="prefetched_followups"
        )
    )

    paginator = Paginator(leads, 25)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1) if hasattr(paginator, 'get_elided_page_range') else paginator.page_range

    for l in page_obj.object_list:
        cd = l.custom_data or {}
        l.hospital_doctor = cd.get('doctor', '')
        l.hospital_dept = cd.get('department', '')
        l.appt_status = cd.get('appointment_status', '')
        l.lead_priority = cd.get('priority', '')
        l.uhid_no = cd.get('uhid_id_no', '')
        l.total_bill = cd.get('total', '0')
        l.is_billing_filled = bool(cd.get('total') or cd.get('uhid_id_no') or cd.get('opd_bill'))

        # Rich latest follow-up object attachment & follow-up count
        fus = getattr(l, "prefetched_followups", [])
        l.followup_history_count = len(fus) or l.followup_count or 0
        if fus:
            l.latest_followup_obj = fus[0]
            l.followup_schedule_date = l.latest_followup_obj.followup_date or l.next_followup_date
            l.latest_followup_status = l.latest_followup_obj.get_followup_status_display()
            l.latest_followup_status_raw = l.latest_followup_obj.followup_status
            l.latest_remark = l.latest_followup_obj.comment or cd.get('remark_1') or cd.get('remark_2') or cd.get('remark_3') or cd.get('comments') or l.notes
        else:
            l.latest_followup_obj = None
            l.followup_schedule_date = cd.get('appo_booked_date') or cd.get('followup_date') or l.next_followup_date
            l.latest_followup_status = "Pending" if l.next_followup_date else "-"
            l.latest_followup_status_raw = "PENDING" if l.next_followup_date else ""
            l.latest_remark = cd.get('remark_1') or cd.get('remark_2') or cd.get('remark_3') or cd.get('comments') or l.notes
    
    query_params = request.GET.copy()
    if 'page' in query_params:
        del query_params['page']

    available_businesses = Hospital.objects.filter(is_active=True).order_by("name") if is_global_admin else []
        
    return render(request, "followups/board.html", {
        "active": active, 
        "title": title, 
        "page_obj": page_obj,
        "leads": page_obj,
        "page_range": page_range,
        "query_params": query_params.urlencode(),
        "total_count": paginator.count,
        "q": q,
        "date_from": date_from,
        "date_to": date_to,
        "date_info": date_info,
        "is_hospital_business": is_hospital_business,
        "current_hospital": hospital,
        "team_members": team_members,
        "courses": courses,
        "stages": stages,
        "selected_user_id": selected_user_id,
        "selected_course_id": selected_course_id,
        "selected_stage_id": selected_stage_id,
        "selected_status": selected_status,
        "selected_admission_status": selected_admission_status,
        "selected_temperature": selected_temperature,
        "is_global_admin": is_global_admin,
        "available_businesses": available_businesses,
    })


def _get_base_annotated_leads():
    """
    Annotates leads with their strictly latest follow-up status and date.
    """
    latest_fu = FollowUp.objects.filter(lead=OuterRef('pk')).order_by('-followup_date', '-id')
    latest_status_sq = Subquery(latest_fu.values('followup_status')[:1])
    latest_date_sq = Subquery(latest_fu.values('followup_date')[:1])
    
    leads = Lead.objects.filter(is_archived=False).annotate(
        latest_followup_st=latest_status_sq,
        latest_followup_dt=latest_date_sq,
    ).annotate(
        effective_followup_dt=Case(
            When(latest_followup_dt__isnull=False, then='latest_followup_dt'),
            default='next_followup_date',
            output_field=DateField()
        ),
        effective_followup_st=Case(
            When(latest_followup_st__isnull=False, then='latest_followup_st'),
            When(next_followup_date__isnull=False, then=Value('PENDING')),
            default=Value(''),
            output_field=CharField()
        )
    )
    return leads


@login_required
def today(request):
    """
    Todays Follow-ups: Strictly leads where the latest follow-up is NOT completed
    (Pending/Rescheduled/etc.) and the follow-up date == today.
    """
    d = timezone.localdate()
    
    leads = _get_base_annotated_leads().filter(
        effective_followup_dt=d
    ).exclude(
        effective_followup_st=FollowUpStatus.COMPLETED
    ).order_by("next_followup_date", "-updated_at")
    
    leads = _filter_by_role(request.user, leads)
    return _board(request, leads, "fu_today", "Today's Follow-ups", d)


@login_required
def upcoming(request):
    """
    Upcoming Follow-ups: Strictly leads where the latest follow-up is NOT completed
    (Pending/Rescheduled/etc.) and the follow-up date > today.
    """
    d = timezone.localdate()
    
    leads = _get_base_annotated_leads().filter(
        effective_followup_dt__gt=d
    ).exclude(
        effective_followup_st=FollowUpStatus.COMPLETED
    ).order_by("effective_followup_dt", "-updated_at")
    
    leads = _filter_by_role(request.user, leads)
    return _board(request, leads, "fu_upcoming", "Upcoming Follow-ups", d)


@login_required
def overdue(request):
    """
    Overdue Follow-ups: Strictly leads where the latest follow-up is NOT completed
    (Pending/Rescheduled/etc.) and the follow-up date < today.
    """
    d = timezone.localdate()
    
    leads = _get_base_annotated_leads().filter(
        effective_followup_dt__lt=d
    ).exclude(
        effective_followup_st=FollowUpStatus.COMPLETED
    ).order_by("effective_followup_dt", "-updated_at")
    
    leads = _filter_by_role(request.user, leads)
    return _board(request, leads, "fu_overdue", "Overdue Follow-ups", d)


@login_required
def completed(request):
    """
    Completed Follow-ups: Strictly leads whose latest follow-up is COMPLETED.
    """
    leads = _get_base_annotated_leads().filter(
        effective_followup_st=FollowUpStatus.COMPLETED
    ).order_by("-updated_at")
    
    leads = _filter_by_role(request.user, leads)
    return _board(request, leads, "fu_completed", "Completed Follow-ups", None)


@login_required
def billing_followup(request):
    """
    Dedicated view for Telecallers/Staff to track leads whose appointment has been marked Completed by Doctor.
    Telecallers can open the lead form (where UHID & Billing section is now unlocked) to enter billing/UHID details.
    """
    from leads.models import Appointment, AppointmentStatus
    
    completed_lead_ids = Appointment.objects.filter(
        status=AppointmentStatus.COMPLETED
    ).values_list('lead_id', flat=True)
    
    leads = Lead.objects.filter(
        is_archived=False
    ).filter(
        Q(id__in=completed_lead_ids) | 
        Q(custom_data__appointment_status__icontains="Completed")
    ).order_by("-updated_at")
    
    leads = _filter_by_role(request.user, leads)
    return _board(request, leads, "fu_billing", "Billing Follow-ups", None)


@login_required
def complete(request, pk):
    lead = get_object_or_404(Lead, pk=pk)
    if request.method == "POST":
        FollowUp.objects.create(
            lead=lead, followup_date=timezone.localdate(), followup_mode="CALL",
            followup_status=FollowUpStatus.COMPLETED, comment=request.POST.get("comment", "Marked complete"),
            created_by=request.user,
        )
        update_kwargs = {"next_followup_date": None, "next_followup_time": None}
        if lead.assigned_to is None:
            update_kwargs["assigned_to"] = request.user
        Lead.objects.filter(pk=pk).update(**update_kwargs)
        messages.success(request, "Follow-up marked complete.")
    return redirect(request.META.get("HTTP_REFERER", "/"))

