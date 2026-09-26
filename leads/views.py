import json
# CRM Views - Auto-reloaded for schema sync
from collections import defaultdict
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import models
from django.db.models import Q, F, Max
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone

from followups.models import FollowUp, Note, Activity, ActivityType, FollowUpMode, FollowUpStatus
from admissions.models import Admission
from accounts.models import User, Hospital
from .models import (
    Lead, SourceCategory, LeadSource, Campaign, Course, LeadStage, Tag, 
    MasterGroup, MasterItem, HospitalBranch, HospitalDepartment, HospitalDoctor, 
    HospitalDisease, DoctorBranchAvailability, DealStatus, LeadTemperature,
    AdmissionStatus,
)
from .forms import (
    LeadForm, HospitalLeadForm, SourceCategoryForm, LeadSourceForm, CampaignForm, CourseForm, LeadStageForm,
)


def _can_edit_lead(user, lead):
    """
    Business-tenant aware edit permission check.
    - Global Super Admin (no hospital): can edit any lead.
    - Tenant user: can only edit leads in their own business (hospital).
    - Within-business:
        - Super Admin / Admin / Manager: can edit any lead in their business.
        - Counsellor / HR: can ONLY edit leads assigned to themselves (lead.assigned_to == user) or unassigned leads.
    """
    is_global_admin = user.is_superuser or (user.role == User.Role.SUPER_ADMIN and not user.hospital)

    if is_global_admin:
        return True

    # Business-tenant check: lead must belong to the same business
    if user.hospital:
        if lead.hospital != user.hospital:
            return False
    else:
        if lead.hospital is not None:
            return False

    # Within-Business Edit Permission Check
    if user.role in (User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER) or user.can_edit_any_lead:
        return True
    if user.role == User.Role.LEAD_ATTENDENT:
        return True
    if user.role in (User.Role.COUNSELLOR, User.Role.HR):
        # Counsellors / HRs can only edit leads assigned to themselves or unassigned leads
        return (lead.assigned_to == user or lead.assigned_to is None)
    if user.can_edit_own_leads and (lead.assigned_to == user or lead.assigned_to is None):
        return True
    if lead.assigned_to is None:
        return True
    return False

def _can_access_lead(user, lead):
    """
    Business-tenant aware access permission check (VIEW permission).
    - Global Super Admin (no hospital): can access any lead.
    - Tenant user: can view leads in their own business (hospital).
    - Within-business:
        - All Counsellors, HRs, Managers, Admins can VIEW team leads within their business tenant.
    """
    is_global_admin = user.is_superuser or (user.role == User.Role.SUPER_ADMIN and not user.hospital)

    if is_global_admin:
        return True

    # Business-tenant check
    if user.hospital:
        if lead.hospital != user.hospital:
            return False
    else:
        if lead.hospital is not None:
            return False

    # Doctor within same business can view patient leads
    if user.role == User.Role.DOCTOR:
        return True

    # In Academy/CRM, Counsellors, HRs, Managers, Admins within the tenant have VIEW permission
    if user.role in (User.Role.COUNSELLOR, User.Role.HR, User.Role.MANAGER, User.Role.ADMIN, User.Role.SUPER_ADMIN):
        return True

    # Within-Business Access Permission Check
    if user.can_view_all_leads or user.can_view_team_leads:
        return True
    if user.can_view_assigned_leads and lead.assigned_to == user:
        return True
    if lead.assigned_to is None:
        return True
    return False


FK_FILTER_FIELDS = [
    "source_category", "lead_source", "campaign", "course", "stage", "assigned_to", "import_job",
]
CHAR_FILTER_FIELDS = [
    "temperature", "deal_status", "admission_status",
]


def get_filtered_leads(request, base_qs=None):
    """
    Applies all tenant scoping, search, sidebar filters, date filters, quick filters,
    and sorting to a leads queryset based on request GET parameters.
    Used by lead_list and lead_detail queue navigation to keep filter state across prev/next iteration.
    """
    if base_qs is not None:
        leads = base_qs
    else:
        leads = Lead.objects.select_related(
            "course", "stage", "lead_source", "source_category", "campaign", "assigned_to"
        ).filter(is_archived=False)

    # --- Business-Tenant Scoping ---
    is_global_admin = request.user.is_superuser or (
        request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital
    )

    # Selected hospital filter for Super Admin
    selected_hospital_id = (
        request.GET.get("business", "").strip()
        or request.GET.get("hospital", "").strip()
        or str(request.session.get("active_business_id", "")).strip()
    )

    if request.user.hospital:
        # Tenant user: always scoped to their business
        leads = leads.filter(hospital=request.user.hospital)
        # Branch-level isolation for Branch Managers / Attendants
        if request.user.role == User.Role.MANAGER and request.user.branch:
            b_name = request.user.branch.name
            branch_team = User.objects.filter(hospital=request.user.hospital, branch=request.user.branch)
            leads = leads.filter(
                Q(custom_data__hospital_branch__iexact=b_name) |
                Q(custom_data__branch__iexact=b_name) |
                Q(custom_data__dyn_hospital_branch__iexact=b_name) |
                Q(custom_data__dyn_branch__iexact=b_name) |
                Q(assigned_to__in=branch_team) |
                Q(created_by__in=branch_team)
            )
        elif not request.user.can_view_all_leads:
            if request.user.can_view_team_leads:
                team = User.objects.filter(reports_to=request.user)
                leads = leads.filter(Q(assigned_to=request.user) | Q(assigned_to__in=team))
            elif request.user.role == User.Role.MANAGER:
                team = User.objects.filter(reports_to=request.user)
                leads = leads.filter(Q(assigned_to=request.user) | Q(assigned_to__in=team) | Q(assigned_to__isnull=True))
            elif request.user.can_view_assigned_leads or request.user.role in (User.Role.COUNSELLOR, User.Role.HR, User.Role.LEAD_ATTENDENT):
                # Counsellors / Staff see their own assigned leads, created by them, or unassigned leads they can capture
                leads = leads.filter(Q(assigned_to=request.user) | Q(created_by=request.user) | Q(assigned_to__isnull=True))
            else:
                leads = leads.none()
    elif is_global_admin:
        if selected_hospital_id:
            if selected_hospital_id == "none":
                leads = leads.filter(hospital__isnull=True)
            elif selected_hospital_id.isdigit():
                leads = leads.filter(hospital_id=int(selected_hospital_id))
    else:
        # Academy user without hospital assignment
        leads = leads.filter(hospital__isnull=True)
        if not request.user.can_view_all_leads:
            if request.user.can_view_team_leads:
                team = User.objects.filter(reports_to=request.user)
                leads = leads.filter(Q(assigned_to=request.user) | Q(assigned_to__in=team))
            elif request.user.role == User.Role.MANAGER:
                team = User.objects.filter(reports_to=request.user)
                leads = leads.filter(Q(assigned_to=request.user) | Q(assigned_to__in=team) | Q(assigned_to__isnull=True))
            elif request.user.can_view_assigned_leads or request.user.role in (User.Role.COUNSELLOR, User.Role.HR, User.Role.LEAD_ATTENDENT):
                leads = leads.filter(Q(assigned_to=request.user) | Q(created_by=request.user) | Q(assigned_to__isnull=True))

    q = request.GET.get("q", "").strip()
    if q:
        leads = leads.filter(
            Q(lead_code__icontains=q) | Q(name__icontains=q) | Q(mobile__icontains=q)
            | Q(email__icontains=q) | Q(city__icontains=q) | Q(course__name__icontains=q)
            | Q(lead_source__name__icontains=q) | Q(campaign__name__icontains=q)
        )

    # Multi-select & single-value filter extraction
    selected_campaigns = request.GET.getlist("campaign")
    selected_sources = request.GET.getlist("lead_source")
    selected_courses = request.GET.getlist("course")
    selected_departments = request.GET.getlist("department")
    selected_doctors = request.GET.getlist("doctor")
    selected_assigned = request.GET.getlist("assigned_to")
    selected_deal_statuses = request.GET.getlist("deal_status")
    selected_admission_statuses = request.GET.getlist("admission_status")
    selected_appointment_statuses = request.GET.getlist("appointment_status")
    selected_priorities = request.GET.getlist("priority")
    selected_temperatures = request.GET.getlist("temperature")
    selected_locations = request.GET.getlist("location")
    selected_stages = request.GET.getlist("stage")

    # 1. Campaigns filter
    if selected_campaigns:
        camp_q = Q()
        for c_val in selected_campaigns:
            if c_val:
                camp_q |= Q(custom_data__campaign__iexact=c_val) | Q(campaign__name__iexact=c_val)
                if c_val.isdigit():
                    camp_q |= Q(campaign_id=int(c_val))
        leads = leads.filter(camp_q)

    # 2. Lead Source filter
    if selected_sources:
        src_q = Q()
        for s_val in selected_sources:
            if s_val:
                src_q |= Q(custom_data__lead_source__iexact=s_val) | Q(lead_source__name__iexact=s_val)
                if s_val.isdigit():
                    src_q |= Q(lead_source_id=int(s_val))
        leads = leads.filter(src_q)

    # 2b. Course filter (Academy)
    if selected_courses:
        crs_q = Q()
        for crs_val in selected_courses:
            if crs_val:
                if str(crs_val).isdigit():
                    crs_q |= Q(course_id=int(crs_val))
                else:
                    crs_q |= Q(course__name__iexact=crs_val) | Q(custom_data__course__icontains=crs_val)
        leads = leads.filter(crs_q)

    # 3. Department filter
    if selected_departments:
        dept_q = Q()
        for d_val in selected_departments:
            if d_val:
                dept_q |= Q(custom_data__department__icontains=d_val)
        leads = leads.filter(dept_q)

    # 4. Doctor filter
    if selected_doctors:
        doc_q = Q()
        for doc_val in selected_doctors:
            if doc_val:
                doc_q |= Q(custom_data__doctor__icontains=doc_val)
        leads = leads.filter(doc_q)

    # 5. Assigned To User filter
    if selected_assigned:
        emp_q = Q()
        for emp_val in selected_assigned:
            if emp_val == "unassigned":
                emp_q |= Q(assigned_to__isnull=True)
            elif emp_val and emp_val.isdigit():
                uid = int(emp_val)
                emp_q |= Q(assigned_to_id=uid) | Q(created_by_id=uid)
        leads = leads.filter(emp_q)

    # 6. Deal Status / Stage filter
    if selected_deal_statuses:
        st_q = Q()
        for ds_val in selected_deal_statuses:
            if not ds_val:
                continue
            v = ds_val.strip()
            v_up = v.upper()

            if 'PAYMENT DONE' in v_up or v_up in ('WON', 'ADMISSION DONE', 'ADMISSION'):
                sub_q = (
                    Q(deal_status=DealStatus.WON) |
                    Q(custom_data__total_paid__gt='0') |
                    Q(custom_data__total__gt='0') |
                    Q(custom_data__deal_status__icontains='Payment Done') |
                    Q(custom_data__deal_status__icontains='Won') |
                    Q(custom_data__deal_status__icontains='Admission Done')
                )
            elif any(k in v_up for k in ('BOOKING CONFIRMED', 'BOOKING APPROVAL', 'AWAITING APPROVAL', 'BOOKED')):
                sub_q = (
                    Q(custom_data__appointment_status__icontains='Book') |
                    Q(custom_data__appointment_status__icontains='Confirm') |
                    Q(custom_data__appointment_status__icontains='Approv') |
                    Q(custom_data__appointment_status__icontains='Await') |
                    Q(custom_data__appointment_status__iexact='YES') |
                    Q(custom_data__appo_booked_date__isnull=False)
                )
            elif 'PAYMENT PENDING' in v_up or 'BILLING PENDING' in v_up:
                sub_q = (
                    Q(custom_data__appointment_status__icontains='Complet') |
                    Q(custom_data__appointment_status__icontains='Done') |
                    Q(custom_data__appointment_status__icontains='Visit')
                )
            elif 'FOLLOW' in v_up:
                sub_q = (
                    Q(custom_data__appointment_status__icontains='Follow') |
                    Q(next_followup_date__isnull=False) |
                    Q(custom_data__deal_status__icontains='Follow')
                )
            elif 'NOT INT' in v_up or 'NOT INTERESTED' in v_up:
                sub_q = (
                    Q(custom_data__appointment_status__icontains='Not Int') |
                    Q(custom_data__deal_status__icontains='Not Int') |
                    Q(deal_status=DealStatus.LOST, custom_data__appointment_status__icontains='Not Int')
                )
            elif 'CANCEL' in v_up:
                sub_q = (
                    Q(custom_data__appointment_status__icontains='Cancel') |
                    Q(custom_data__deal_status__icontains='Cancel')
                )
            elif v_up == 'LOST':
                sub_q = (
                    Q(deal_status=DealStatus.LOST) |
                    Q(custom_data__deal_status__icontains='Lost')
                )
            elif 'ASSIGNED' in v_up:
                sub_q = (
                    Q(assigned_to__isnull=False) |
                    Q(custom_data__deal_status__iexact='Assigned')
                )
            elif v_up == 'NEW':
                today_date = timezone.localdate()
                sub_q = (
                    Q(assigned_to__isnull=True) &
                    (Q(created_at__date=today_date) | Q(inquiry_date=today_date))
                )
            elif v_up == 'OPEN':
                today_date = timezone.localdate()
                sub_q = Q(assigned_to__isnull=True) & ~(
                    Q(created_at__date=today_date) | Q(inquiry_date=today_date)
                )
            else:
                sub_q = (
                    Q(deal_status__iexact=ds_val) |
                    Q(custom_data__deal_status__iexact=ds_val) |
                    Q(stage__name__iexact=ds_val)
                )

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

    # 7. Appointment Status filter (Hospital)
    if selected_appointment_statuses:
        apt_q = Q()
        for apt_val in selected_appointment_statuses:
            if apt_val:
                v_l = apt_val.lower().strip()
                if 'book' in v_l:
                    apt_q |= (
                        Q(custom_data__appointment_status__icontains='Book') |
                        Q(custom_data__appointment_status__icontains='Booking') |
                        Q(custom_data__appointment_status__iexact='YES') |
                        Q(custom_data__appo_booked_date__isnull=False)
                    )
                else:
                    apt_q |= Q(custom_data__appointment_status__icontains=apt_val)
        leads = leads.filter(apt_q)

    # 7b. Admission Status filter (Academy)
    if selected_admission_statuses:
        adm_q = Q()
        for adm_val in selected_admission_statuses:
            if adm_val:
                adm_q |= Q(admission_status__iexact=adm_val) | Q(custom_data__admission_status__iexact=adm_val)
        leads = leads.filter(adm_q)

    # 8. Priority & Temperature filter
    if selected_priorities or selected_temperatures:
        prio_q = Q()
        for p_val in (selected_priorities + selected_temperatures):
            if p_val:
                prio_q |= Q(custom_data__priority__iexact=p_val) | Q(temperature__iexact=p_val)
        leads = leads.filter(prio_q)
        
        p_vals_upper = [str(x).upper() for x in (selected_priorities + selected_temperatures)]
        if any(x in ['HOT', 'WARM', 'COLD', 'FREEZE'] for x in p_vals_upper):
            leads = leads.exclude(
                Q(deal_status=DealStatus.WON) |
                Q(deal_status=DealStatus.LOST) |
                Q(custom_data__appointment_status__icontains='Book') |
                Q(custom_data__appointment_status__icontains='Complete') |
                Q(custom_data__appointment_status__icontains='Cancel') |
                Q(custom_data__appointment_status__iexact='YES') |
                Q(custom_data__deal_status__icontains='Won') |
                Q(custom_data__deal_status__icontains='Payment Done') |
                Q(custom_data__total_paid__gt='0')
            )

    # 9. Location / City filter
    if selected_locations:
        loc_q = Q()
        for loc_val in selected_locations:
            if loc_val:
                loc_q |= Q(location__iexact=loc_val) | Q(city__iexact=loc_val) | Q(custom_data__location__iexact=loc_val)
        leads = leads.filter(loc_q)

    city = request.GET.get("city")
    if city and not selected_locations:
        leads = leads.filter(city__iexact=city)

    import_job_id = request.GET.get("import_job")
    if import_job_id:
        from imports.models import ImportJob
        selected_import_job = ImportJob.objects.filter(pk=import_job_id).first()
        if selected_import_job:
            leads = leads.filter(
                Q(import_job_id=selected_import_job.pk) | 
                Q(import_source_file__iexact=selected_import_job.original_filename)
            )

    def _parse_date_input(val):
        if not val:
            return None
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(val.strip(), fmt).date()
            except ValueError:
                continue
        return None

    date_from = _parse_date_input(request.GET.get("date_from") or request.GET.get("date"))
    date_to = _parse_date_input(request.GET.get("date_to"))
    if date_from:
        leads = leads.filter(inquiry_date__gte=date_from)
    if date_to:
        leads = leads.filter(inquiry_date__lte=date_to)

    followup_filter = request.GET.get("followup")
    quick_filter = request.GET.get("filter")
    today = timezone.localdate()
    
    if quick_filter == "todays_new":
        leads = leads.filter(
            Q(created_at__date=today) | Q(inquiry_date=today)
        )
    elif quick_filter == "call_not_done":
        leads = leads.filter(
            deal_status__in=[DealStatus.OPEN, 'New', 'OPEN'],
            admission_status__in=[AdmissionStatus.NOT_APPLIED, '', None],
            admission__isnull=True,
            temperature=LeadTemperature.UNCONTACTED,
            followup_count=0,
            next_followup_date__isnull=True,
        ).filter(
            Q(stage__isnull=True) | Q(stage__name__in=['New', 'Fresh', 'Uncontacted', 'new', 'fresh', 'uncontacted'])
        ).distinct()
    elif quick_filter == "admission_today":
        leads = leads.filter(
            Q(admission_status="ADMISSION_DONE") | Q(deal_status="WON") | Q(admission__admission_date=today) | Q(custom_data__admission_date=str(today))
        ).distinct()
    elif quick_filter == "billing_today":
        leads = leads.filter(
            Q(admission__payments__payment_status='SUCCESS', admission__payments__created_at__date=today) |
            (Q(custom_data__total__isnull=False) & ~Q(custom_data__total__in=["0", "0.00", "", "0.0", 0, 0.0]) & Q(updated_at__date=today))
        ).distinct()
    elif quick_filter == "upcoming_followups" or followup_filter == "upcoming":
        leads = leads.filter(
            Q(next_followup_date__gte=today) | Q(followups__followup_date__gte=today)
        ).distinct()

    if followup_filter == "overdue" or quick_filter == "overdue":
        leads = leads.filter(
            Q(next_followup_date__lt=today) | Q(followups__followup_date__lt=today)
        ).exclude(
            Q(next_followup_date__gte=today) | Q(followups__followup_date__gte=today)
        ).distinct()
    elif followup_filter == "today":
        leads = leads.filter(
            Q(next_followup_date=today) | Q(followups__followup_date=today)
        ).distinct()

    appo_book = request.GET.get("appo_book")
    if appo_book == "YES":
        leads = leads.filter(
            Q(custom_data__appo_booked_date__isnull=False) |
            Q(custom_data__appointment_status__icontains="Booked") |
            Q(custom_data__appointment_status__icontains="Complete") |
            Q(custom_data__appo_book__iexact="YES")
        )

    has_revenue = request.GET.get("has_revenue")
    if has_revenue == "1":
        leads = leads.filter(
            (Q(custom_data__total__isnull=False) & ~Q(custom_data__total__in=["0", "0.00", "", "0.0", 0, 0.0])) |
            Q(admission__payments__payment_status='SUCCESS', admission__payments__amount__gt=0) |
            Q(deal_status='WON') |
            Q(admission_status='ADMISSION_DONE')
        ).distinct()

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
    return leads


@login_required
def lead_list(request):
    leads = get_filtered_leads(request)

    selected_hospital_id = (
        request.GET.get("business", "").strip()
        or request.GET.get("hospital", "").strip()
        or str(request.session.get("active_business_id", "")).strip()
    )
    is_global_admin = request.user.is_superuser or (
        request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital
    )
    q = request.GET.get("q", "").strip()
    selected_campaigns = request.GET.getlist("campaign")
    selected_sources = request.GET.getlist("lead_source")
    selected_courses = request.GET.getlist("course")
    selected_departments = request.GET.getlist("department")
    selected_doctors = request.GET.getlist("doctor")
    selected_assigned = request.GET.getlist("assigned_to")
    selected_deal_statuses = request.GET.getlist("deal_status")
    selected_admission_statuses = request.GET.getlist("admission_status")
    selected_appointment_statuses = request.GET.getlist("appointment_status")
    selected_priorities = request.GET.getlist("priority")
    selected_temperatures = request.GET.getlist("temperature")
    selected_locations = request.GET.getlist("location")
    selected_stages = request.GET.getlist("stage")

    sort_by = request.GET.get("sort", "-created_at")
    date_from = request.GET.get("date_from") or request.GET.get("date")
    date_to = request.GET.get("date_to")

    import_job_id = request.GET.get("import_job")
    selected_import_job = None
    if import_job_id:
        from imports.models import ImportJob
        selected_import_job = ImportJob.objects.filter(pk=import_job_id).first()

    # Calculate active filters count
    active_filters_count = (
        len(selected_campaigns) + len(selected_sources) + len(selected_departments) +
        len(selected_doctors) + len(selected_assigned) + len(selected_deal_statuses) +
        len(selected_appointment_statuses) + len(selected_priorities) + len(selected_temperatures) +
        len(selected_locations) + len(selected_stages) +
        (1 if (date_from or date_to) else 0)
    )

    # Export filtered leads (Excel / CSV / PDF)
    export_format = request.GET.get("export", "").lower()
    if export_format in ("excel", "xlsx", "1", "csv"):
        import pandas as pd
        is_hospital = bool(request.user.hospital)
        
        def _build_lead_export_row(l):
            cd = l.custom_data or {}
            if is_hospital:
                return {
                    "Lead ID": l.lead_code,
                    "Patient Name": l.name,
                    "Mobile": l.mobile,
                    "Email": l.email or "",
                    "Location / City": l.location or l.city or cd.get("location", ""),
                    "Department": cd.get("department", "") or cd.get("disease", ""),
                    "Doctor": cd.get("doctor", ""),
                    "Lead Source": cd.get("lead_source", "") or (l.lead_source.name if l.lead_source else ""),
                    "Campaign": cd.get("campaign", "") or (l.campaign.name if l.campaign else ""),
                    "Lead Status": cd.get("deal_status", "") or (l.stage.name if l.stage else ""),
                    "Appointment Status": cd.get("appointment_status", ""),
                    "Inquiry Date": str(l.inquiry_date) if l.inquiry_date else "",
                    "Assigned Staff": l.assigned_to.get_full_name() if l.assigned_to else "Unassigned",
                    "Created At": l.effective_created_formatted or (l.created_at.strftime("%Y-%m-%d %H:%M") if l.created_at else ""),
                }
            else:
                return {
                    "Lead ID": l.lead_code,
                    "Name": l.name,
                    "Mobile": l.mobile,
                    "Email": l.email or "",
                    "City": l.city or "",
                    "Course": l.course.name if l.course else "",
                    "Lead Source": l.lead_source.name if l.lead_source else "",
                    "Campaign": l.campaign.name if l.campaign else "",
                    "Stage": l.stage.name if l.stage else "",
                    "Deal Status": l.get_deal_status_display(),
                    "Inquiry Date": str(l.inquiry_date) if l.inquiry_date else "",
                    "Assigned To": l.assigned_to.get_full_name() if l.assigned_to else "Unassigned",
                    "Created At": l.effective_created_formatted or (l.created_at.strftime("%Y-%m-%d %H:%M") if l.created_at else ""),
                }

        rows = [_build_lead_export_row(l) for l in leads]
        df = pd.DataFrame(rows)
        response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        response["Content-Disposition"] = f'attachment; filename="filtered_leads_{timezone.now().strftime("%Y%m%d_%H%M")}.xlsx"'
        df.to_excel(response, index=False, sheet_name="Filtered Leads")
        return response

    elif export_format == "pdf":
        return render(request, "leads/leads_print_pdf.html", {
            "leads": leads[:500],
            "total_count": leads.count(),
            "now": timezone.now(),
            "active_filters_count": active_filters_count,
        })

    paginator = Paginator(leads, 25)
    page = paginator.get_page(request.GET.get("page"))

    query_params = request.GET.copy()
    if 'page' in query_params:
        del query_params['page']

    # Filter dropdown options scoped to current user's business
    active_leads = Lead.objects.filter(is_archived=False)
    if request.user.hospital:
        active_leads = active_leads.filter(hospital=request.user.hospital)
        if request.user.role in ('COUNSELLOR', 'HR'):
            active_leads = active_leads.filter(
                Q(assigned_to=request.user) | Q(created_by=request.user) | Q(assigned_to__isnull=True)
            )
    elif is_global_admin and selected_hospital_id and selected_hospital_id.isdigit():
        active_leads = active_leads.filter(hospital_id=int(selected_hospital_id))
    
    used_sc_ids = active_leads.values_list("source_category_id", flat=True).distinct()
    used_ls_ids = active_leads.values_list("lead_source_id", flat=True).distinct()
    used_camp_ids = active_leads.values_list("campaign_id", flat=True).distinct()
    used_course_ids = active_leads.values_list("course_id", flat=True).distinct()
    used_stage_ids = active_leads.values_list("stage_id", flat=True).distinct()
    used_emp_ids = active_leads.values_list("assigned_to_id", flat=True).distinct()

    distinct_cities = sorted(list(set(active_leads.exclude(city="").values_list("city", flat=True))))
    distinct_locations = sorted(list(set(active_leads.exclude(location="").values_list("location", flat=True))))
    
    # Determine whether current view should show hospital or academy style layout
    target_hospital = None
    if request.user.hospital:
        target_hospital = request.user.hospital
    elif is_global_admin and selected_hospital_id and selected_hospital_id.isdigit():
        target_hospital = Hospital.objects.filter(id=int(selected_hospital_id)).first()

    is_viewing_hospital = False
    if request.user.hospital:
        is_viewing_hospital = request.user.is_hospital_user
    elif target_hospital:
        is_viewing_hospital = "hospital" in target_hospital.name.lower() or "clinic" in target_hospital.name.lower() or "nelson" in target_hospital.name.lower()
    else:
        is_viewing_hospital = False

    # Extract departments, doctors, and appointment statuses only for hospital views
    # And courses / admission statuses only for academy views
    if is_viewing_hospital:
        courses_qs = Course.objects.none()
        adm_status_choices = []
        if target_hospital:
            filter_departments = list(HospitalDepartment.objects.filter(hospital=target_hospital, is_active=True).values_list("name", flat=True))
            filter_doctors = list(HospitalDoctor.objects.filter(hospital=target_hospital, is_active=True).values_list("name", flat=True))
            if not filter_departments:
                filter_departments = list(MasterGroup.get_active_choices("Departments").filter(hospital=target_hospital).values_list("name", flat=True))
            if not filter_doctors:
                filter_doctors = list(MasterGroup.get_active_choices("Doctors").filter(hospital=target_hospital).values_list("name", flat=True))
            if not filter_doctors:
                filter_doctors = list(User.objects.filter(hospital=target_hospital, role=User.Role.DOCTOR, is_active=True).values_list("first_name", flat=True))
        filter_appointment_statuses = ["Booked", "Booking Done", "Pending Confirmation", "Awaiting Doctor Approval", "Visited / OPD Done", "Cancelled", "Not Interested", "Payment Done"]
    else:
        courses_qs = Course.objects.filter(id__in=used_course_ids)
        adm_status_choices = AdmissionStatus.choices
        filter_departments = []
        filter_doctors = []
        filter_appointment_statuses = []

    filter_priorities = ["Hot", "Warm", "Cold"]

    # Businesses dropdown is ONLY for global superadmin (no user.hospital)
    available_businesses = Hospital.objects.filter(is_active=True).order_by("name") if (is_global_admin and not request.user.hospital) else Hospital.objects.none()

    context = {
        "query_params": query_params.urlencode(),
        "active": "leads_all",
        "page_obj": page,
        "total_count": leads.count(),
        "q": q,
        "selected_import_job": selected_import_job,
        "source_categories": SourceCategory.objects.filter(id__in=used_sc_ids),
        "lead_sources": LeadSource.objects.filter(id__in=used_ls_ids),
        "campaigns": Campaign.objects.filter(id__in=used_camp_ids),
        "courses": courses_qs,
        "stages": LeadStage.objects.filter(id__in=used_stage_ids),
        "cities": distinct_cities,
        "locations": distinct_locations,
        "filter_locations": distinct_locations or distinct_cities,
        "filter_departments": filter_departments,
        "filter_doctors": filter_doctors,
        "filter_appointment_statuses": filter_appointment_statuses,
        "admission_status_choices": adm_status_choices,
        "filter_priorities": filter_priorities,
        "deal_status_choices": DealStatus.choices,
        "selected_campaigns": selected_campaigns,
        "selected_sources": selected_sources,
        "selected_courses": selected_courses,
        "selected_departments": selected_departments,
        "selected_doctors": selected_doctors,
        "selected_assigned": selected_assigned,
        "selected_deal_statuses": selected_deal_statuses,
        "selected_admission_statuses": selected_admission_statuses,
        "selected_appointment_statuses": selected_appointment_statuses,
        "selected_priorities": selected_priorities,
        "selected_temperatures": selected_temperatures,
        "selected_locations": selected_locations,
        "selected_stages": selected_stages,
        "selected_hospital_id": selected_hospital_id,
        "businesses": available_businesses,
        "date_from_val": request.GET.get("date_from", "") or request.GET.get("date", ""),
        "date_to_val": request.GET.get("date_to", ""),
        "current_sort": sort_by,
        "active_filters_count": active_filters_count,
        "request_get": request.GET,
    }

    if target_hospital:
        # For hospitals, leads are assigned to Lead Attendants (or Counsellors/HR if configured), never to Doctors or Admins
        context["employees"] = User.objects.filter(
            hospital=target_hospital,
            role__in=[User.Role.LEAD_ATTENDENT, User.Role.COUNSELLOR, User.Role.HR],
            is_active=True,
            is_approved=True
        ).order_by("first_name", "last_name", "username")
        context["hospital_campaigns"] = MasterGroup.get_active_choices("Campaigns").filter(hospital=target_hospital)
        context["hospital_sources"] = MasterGroup.get_active_choices("Lead Sources").filter(hospital=target_hospital)
        context["hospital_statuses"] = MasterGroup.get_active_choices("Deal Statuses").filter(hospital=target_hospital)
        hospital_deal_statuses = list(context["hospital_statuses"].values_list("name", flat=True))
        if hospital_deal_statuses:
            context["bulk_stages"] = [{"id": s, "name": s} for s in hospital_deal_statuses]
        else:
            context["bulk_stages"] = [{"id": s.id, "name": s.name} for s in LeadStage.objects.filter(is_active=True)]
    else:
        # For Academy / generic, assign to Counsellors and HR (and Lead Attendants if present)
        context["employees"] = User.objects.filter(
            role__in=[User.Role.COUNSELLOR, User.Role.HR, User.Role.LEAD_ATTENDENT],
            is_active=True,
            is_approved=True
        ).order_by("first_name", "last_name", "username")
        context["bulk_stages"] = [{"id": s.id, "name": s.name} for s in LeadStage.objects.filter(is_active=True)]

    template_name = "leads/nel_lead_list.html" if is_viewing_hospital else "leads/zapp_lead_list.html"
    return render(request, template_name, context)


@login_required
def my_leads(request):
    """
    My Leads View for Zappcode Academy:
    Shows leads captured by or assigned to the current user (Counsellor, HR, etc.)
    with comprehensive header filters (Date: Today, Yesterday, Months, Years, Custom;
    Course, Stage, Admission Status).
    """
    from datetime import date, timedelta
    from django.db.models.functions import TruncMonth, TruncYear

    # Restrict to Academy tenant
    hospital = request.user.hospital
    leads = Lead.objects.filter(is_archived=False)
    if hospital:
        leads = leads.filter(hospital=hospital)
    
    # Strictly leads assigned to the logged in user
    leads = leads.filter(assigned_to=request.user)

    # Search keyword
    q = request.GET.get("q", "").strip()
    if q:
        leads = leads.filter(
            Q(lead_code__icontains=q) | Q(name__icontains=q) | Q(mobile__icontains=q)
            | Q(email__icontains=q) | Q(city__icontains=q) | Q(course__name__icontains=q)
        )

    # Available distinct years and months with data for dropdowns
    available_years_raw = Lead.objects.filter(
        hospital=hospital if hospital else None, inquiry_date__isnull=False
    ).dates("inquiry_date", "year", order="DESC") if hospital else Lead.objects.filter(inquiry_date__isnull=False).dates("inquiry_date", "year", order="DESC")
    available_years = [d.year for d in available_years_raw]

    available_months_raw = Lead.objects.filter(
        hospital=hospital if hospital else None, inquiry_date__isnull=False
    ).dates("inquiry_date", "month", order="DESC") if hospital else Lead.objects.filter(inquiry_date__isnull=False).dates("inquiry_date", "month", order="DESC")
    available_months = [
        {"value": d.strftime("%Y-%m"), "label": d.strftime("%B %Y"), "year": d.year, "month": d.month}
        for d in available_months_raw
    ]

    # Filters extraction
    today = timezone.localdate()
    yesterday = today - timedelta(days=1)
    
    date_preset = request.GET.get("date_preset", "").strip()
    selected_month = request.GET.get("month", "").strip()
    selected_year = request.GET.get("year", "").strip()
    date_from = request.GET.get("date_from", "").strip()
    date_to = request.GET.get("date_to", "").strip()

    active_date_label = "All Time"

    if date_preset == "today":
        leads = leads.filter(Q(inquiry_date=today) | Q(created_at__date=today))
        active_date_label = f"Today ({today.strftime('%d %b %Y')})"
    elif date_preset == "yesterday":
        leads = leads.filter(Q(inquiry_date=yesterday) | Q(created_at__date=yesterday))
        active_date_label = f"Yesterday ({yesterday.strftime('%d %b %Y')})"
    elif selected_month:
        try:
            parts = selected_month.split("-")
            y, m = int(parts[0]), int(parts[1])
            leads = leads.filter(inquiry_date__year=y, inquiry_date__month=m)
            active_date_label = datetime(y, m, 1).strftime("%B %Y")
        except Exception:
            pass
    elif selected_year:
        try:
            y = int(selected_year)
            leads = leads.filter(inquiry_date__year=y)
            active_date_label = f"Year {y}"
        except Exception:
            pass
    elif date_from or date_to:
        if date_from:
            try:
                df = datetime.strptime(date_from, "%Y-%m-%d").date()
                leads = leads.filter(inquiry_date__gte=df)
            except ValueError:
                pass
        if date_to:
            try:
                dt = datetime.strptime(date_to, "%Y-%m-%d").date()
                leads = leads.filter(inquiry_date__lte=dt)
            except ValueError:
                pass
        active_date_label = f"Custom: {date_from or 'Start'} to {date_to or 'End'}"

    # Dropdown filters: Course, Stage, Admission Status
    selected_courses = [c for c in request.GET.getlist("course") if c.isdigit()]
    selected_stages = [s for s in request.GET.getlist("stage") if s.isdigit()]
    selected_admission_statuses = [a.strip() for a in request.GET.getlist("admission_status") if a.strip()]

    if selected_courses:
        leads = leads.filter(course__id__in=selected_courses)
    if selected_stages:
        leads = leads.filter(stage__id__in=selected_stages)
    if selected_admission_statuses:
        leads = leads.filter(admission_status__in=selected_admission_statuses)

    # Sort
    sort_by = request.GET.get("sort", "-updated_at")
    leads = leads.select_related("course", "stage", "assigned_to", "lead_source").order_by(sort_by)

    # Filter counts for badges
    active_filters_count = (
        (1 if date_preset or selected_month or selected_year or date_from or date_to else 0)
        + len(selected_courses) + len(selected_stages) + len(selected_admission_statuses)
        + (1 if q else 0)
    )

    # Pagination
    paginator = Paginator(leads, 25)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    query_params = request.GET.copy()
    query_params.pop("page", None)

    # Options for dropdowns
    courses = Course.objects.filter(is_active=True).order_by("name")
    stages = LeadStage.objects.filter(is_active=True).order_by("order", "name")
    admission_status_choices = AdmissionStatus.choices

    context = {
        "active": "my_leads",
        "page_obj": page_obj,
        "total_count": paginator.count,
        "q": q,
        "courses": courses,
        "stages": stages,
        "admission_status_choices": admission_status_choices,
        "available_years": available_years,
        "available_months": available_months,
        "date_preset": date_preset,
        "selected_month": selected_month,
        "selected_year": selected_year,
        "date_from": date_from,
        "date_to": date_to,
        "active_date_label": active_date_label,
        "selected_courses": selected_courses,
        "selected_stages": selected_stages,
        "selected_admission_statuses": selected_admission_statuses,
        "current_sort": sort_by,
        "active_filters_count": active_filters_count,
        "query_params": query_params.urlencode(),
        "today_str": today.strftime("%Y-%m-%d"),
    }
    return render(request, "leads/zapp_my_leads.html", context)


@login_required
def team_history(request):
    """
    Teams History View for Zappcode Academy:
    Shows leads created, assigned, or worked on by team members (Counsellors & HRs).
    By default displays All Time team leads with instant filters for Today, Yesterday, Months, Years, Custom Dates.
    Header displays user-wise lead count summary cards. Clicking a user filters to their leads.
    """
    from datetime import date, timedelta
    from followups.models import FollowUp, Note, Activity
    from django.db.models import Prefetch

    from accounts.models import Hospital

    is_global_admin = request.user.is_superuser or (
        request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital
    )

    hospital = request.user.hospital
    selected_business_id = (
        request.GET.get("business", "").strip()
        or request.GET.get("hospital", "").strip()
        or str(request.session.get("active_business_id", "")).strip()
    )
    if is_global_admin:
        if selected_business_id and selected_business_id.isdigit():
            hospital = Hospital.objects.filter(id=int(selected_business_id)).first()
        elif not hospital:
            # Default to Zappcode Academy for Academy views
            hospital = Hospital.objects.filter(name__icontains="zappcode").first() or Hospital.objects.first()

    today = timezone.localdate()
    yesterday = today - timedelta(days=1)

    # Determine team member roles based on business type
    # Hospital businesses use LEAD_ATTENDENT & DOCTOR; academies use COUNSELLOR, HR, MANAGER
    is_hospital_business = False
    if hospital:
        btype = (hospital.settings or {}).get("business_type", "")
        if not btype:
            name_lower = (hospital.name or "").lower()
            if "hospital" in name_lower or "clinic" in name_lower or "medical" in name_lower or "nelson" in name_lower:
                btype = "hospital"
        is_hospital_business = str(btype).strip().lower() == "hospital"

    if is_hospital_business:
        team_roles = [User.Role.LEAD_ATTENDENT, User.Role.DOCTOR, User.Role.MANAGER, User.Role.ADMIN]
    else:
        team_roles = [User.Role.COUNSELLOR, User.Role.HR, User.Role.MANAGER]

    # Get team members strictly scoped to the current hospital/business
    if hospital:
        team_members = User.objects.filter(
            is_active=True, is_approved=True,
            role__in=team_roles,
            hospital=hospital,
        ).order_by("first_name", "username")
    else:
        # No hospital context — show no one to avoid cross-tenant leakage
        team_members = User.objects.none()

    # Base queryset for team leads
    leads = Lead.objects.filter(is_archived=False)
    if hospital:
        leads = leads.filter(hospital=hospital)

    # Search keyword
    q = request.GET.get("q", "").strip()
    if q:
        leads = leads.filter(
            Q(lead_code__icontains=q) | Q(name__icontains=q) | Q(mobile__icontains=q)
            | Q(email__icontains=q) | Q(city__icontains=q) | Q(course__name__icontains=q)
            | Q(assigned_to__first_name__icontains=q) | Q(assigned_to__last_name__icontains=q)
            | Q(assigned_to__username__icontains=q) | Q(created_by__first_name__icontains=q)
            | Q(created_by__last_name__icontains=q) | Q(created_by__username__icontains=q)
        )

    # Date preset (Default to "all" (All Time) so team leads are immediately visible)
    date_preset = request.GET.get("date_preset", "").strip()
    selected_month = request.GET.get("month", "").strip()
    selected_year = request.GET.get("year", "").strip()
    date_from = request.GET.get("date_from", "").strip()
    date_to = request.GET.get("date_to", "").strip()

    active_date_label = "All Time"

    if date_preset == "today":
        leads = leads.filter(
            Q(inquiry_date=today) | Q(created_at__date=today) | Q(updated_at__date=today)
            | Q(followups__followup_date=today) | Q(followups__created_at__date=today)
            | Q(lead_notes__created_at__date=today) | Q(activities__created_at__date=today)
        )
        active_date_label = f"Today ({today.strftime('%d %b %Y')})"
    elif date_preset == "yesterday":
        leads = leads.filter(
            Q(inquiry_date=yesterday) | Q(created_at__date=yesterday) | Q(updated_at__date=yesterday)
            | Q(followups__followup_date=yesterday) | Q(followups__created_at__date=yesterday)
            | Q(lead_notes__created_at__date=yesterday) | Q(activities__created_at__date=yesterday)
        )
        active_date_label = f"Yesterday ({yesterday.strftime('%d %b %Y')})"
    elif selected_month:
        try:
            parts = selected_month.split("-")
            y, m = int(parts[0]), int(parts[1])
            leads = leads.filter(
                Q(inquiry_date__year=y, inquiry_date__month=m)
                | Q(created_at__year=y, created_at__month=m)
                | Q(updated_at__year=y, updated_at__month=m)
                | Q(followups__followup_date__year=y, followups__followup_date__month=m)
                | Q(followups__created_at__year=y, followups__created_at__month=m)
                | Q(lead_notes__created_at__year=y, lead_notes__created_at__month=m)
                | Q(activities__created_at__year=y, activities__created_at__month=m)
            )
            active_date_label = datetime(y, m, 1).strftime("%B %Y")
        except Exception:
            pass
    elif selected_year:
        try:
            y = int(selected_year)
            leads = leads.filter(
                Q(inquiry_date__year=y) | Q(created_at__year=y) | Q(updated_at__year=y)
                | Q(followups__followup_date__year=y) | Q(followups__created_at__year=y)
                | Q(lead_notes__created_at__year=y) | Q(activities__created_at__year=y)
            )
            active_date_label = f"Year {y}"
        except Exception:
            pass
    elif date_from or date_to:
        if date_from:
            try:
                df = datetime.strptime(date_from, "%Y-%m-%d").date()
                leads = leads.filter(
                    Q(inquiry_date__gte=df) | Q(created_at__date__gte=df) | Q(updated_at__date__gte=df)
                    | Q(followups__followup_date__gte=df) | Q(followups__created_at__date__gte=df)
                    | Q(lead_notes__created_at__date__gte=df) | Q(activities__created_at__date__gte=df)
                )
            except ValueError:
                pass
        if date_to:
            try:
                dt = datetime.strptime(date_to, "%Y-%m-%d").date()
                leads = leads.filter(
                    Q(inquiry_date__lte=dt) | Q(created_at__date__lte=dt) | Q(updated_at__date__lte=dt)
                    | Q(followups__followup_date__lte=dt) | Q(followups__created_at__date__lte=dt)
                    | Q(lead_notes__created_at__date__lte=dt) | Q(activities__created_at__date__lte=dt)
                )
            except ValueError:
                pass
        active_date_label = f"Custom: {date_from or 'Start'} to {date_to or 'End'}"

    # Apply distinct to avoid inflated cartesian product counts from multi-table joins (followups, notes, activities)
    leads = leads.distinct()

    # For hospital businesses: no course/stage/admission filters needed
    # (these are Zappcode Academy-specific)

    # Build separate attendant and doctor querysets for hospital filter dropdowns
    attendants = User.objects.none()
    doctors = User.objects.none()
    if hospital and is_hospital_business:
        attendants = User.objects.filter(
            is_active=True, is_approved=True,
            role=User.Role.LEAD_ATTENDENT,
            hospital=hospital,
        ).order_by("first_name", "last_name")
        doctors = User.objects.filter(
            is_active=True, is_approved=True,
            role=User.Role.DOCTOR,
            hospital=hospital,
        ).order_by("first_name", "last_name")

    # Calculate all_members_count across leads before single user selection
    all_members_count = leads.distinct().count()
    user_counts = []
    for member in team_members:
        if is_hospital_business and member.role == User.Role.DOCTOR:
            doc_name = member.get_full_name() or member.username
            c = leads.filter(Q(assigned_to=member) | Q(custom_data__doctor__icontains=doc_name)).distinct().count()
        else:
            c = leads.filter(assigned_to=member).distinct().count()
        user_counts.append({"user": member, "count": c, "is_self": member == request.user})
    user_counts.sort(key=lambda x: x["count"], reverse=True)

    # Filter by selected Lead Attendant (assigned_to)
    selected_attendant_id = request.GET.get("attendant_id", "").strip()
    selected_attendant = None
    if selected_attendant_id and selected_attendant_id.isdigit():
        selected_attendant = attendants.filter(id=int(selected_attendant_id)).first()
        if selected_attendant:
            leads = leads.filter(assigned_to=selected_attendant)

    # Filter by selected Doctor (stored as name string in custom_data__doctor)
    selected_doctor_id = request.GET.get("doctor_id", "").strip()
    selected_doctor = None
    if selected_doctor_id and selected_doctor_id.isdigit():
        selected_doctor = doctors.filter(id=int(selected_doctor_id)).first()
        if selected_doctor:
            doctor_name = selected_doctor.get_full_name() or selected_doctor.username
            leads = leads.filter(custom_data__doctor__icontains=doctor_name)

    # Filter by selected team member (for both Academy and Hospital)
    selected_user_id = request.GET.get("user_id", "").strip()
    selected_user = None
    if selected_user_id and selected_user_id.isdigit():
        selected_user = team_members.filter(id=int(selected_user_id)).first()
        if selected_user:
            if is_hospital_business and selected_user.role == User.Role.DOCTOR:
                dname = selected_user.get_full_name() or selected_user.username
                leads = leads.filter(Q(assigned_to=selected_user) | Q(custom_data__doctor__icontains=dname))
            else:
                leads = leads.filter(assigned_to=selected_user)

    # Course filter (Zappcode Academy)
    selected_courses = request.GET.getlist("course")
    if selected_courses:
        valid_course_ids = [int(c) for c in selected_courses if str(c).isdigit()]
        if valid_course_ids:
            leads = leads.filter(course_id__in=valid_course_ids)

    # Stage filter (Zappcode Academy)
    selected_stages = request.GET.getlist("stage")
    if selected_stages:
        valid_stage_ids = [int(s) for s in selected_stages if str(s).isdigit()]
        if valid_stage_ids:
            leads = leads.filter(stage_id__in=valid_stage_ids)

    # Admission status filter (Zappcode Academy)
    selected_admission_statuses = request.GET.getlist("admission_status")
    if selected_admission_statuses:
        valid_statuses = [st for st in selected_admission_statuses if st]
        if valid_statuses:
            leads = leads.filter(admission_status__in=valid_statuses)

    # Available distinct years and months with data for dropdowns
    available_years_raw = Lead.objects.filter(
        hospital=hospital if hospital else None, inquiry_date__isnull=False
    ).dates("inquiry_date", "year", order="DESC") if hospital else Lead.objects.filter(inquiry_date__isnull=False).dates("inquiry_date", "year", order="DESC")
    available_years = [d.year for d in available_years_raw]

    available_months_raw = Lead.objects.filter(
        hospital=hospital if hospital else None, inquiry_date__isnull=False
    ).dates("inquiry_date", "month", order="DESC") if hospital else Lead.objects.filter(inquiry_date__isnull=False).dates("inquiry_date", "month", order="DESC")
    available_months = [
        {"value": d.strftime("%Y-%m"), "label": d.strftime("%B %Y"), "year": d.year, "month": d.month}
        for d in available_months_raw
    ]

    # Sort & Prefetch for complete update insights
    sort_by = request.GET.get("sort", "-updated_at")
    leads = leads.distinct().select_related(
        "course", "stage", "assigned_to", "created_by", "lead_source"
    ).prefetch_related(
        Prefetch("followups", queryset=FollowUp.objects.select_related("created_by").order_by("-followup_date", "-id"), to_attr="prefetched_followups"),
        Prefetch("activities", queryset=Activity.objects.select_related("created_by").order_by("-created_at", "-id"), to_attr="prefetched_activities"),
        Prefetch("lead_notes", queryset=Note.objects.select_related("created_by").order_by("-created_at", "-id"), to_attr="prefetched_notes"),
    ).order_by(sort_by)

    # Filter counts for badges
    active_filters_count = (
        (1 if (date_preset and date_preset != "all") or selected_month or selected_year or date_from or date_to else 0)
        + (1 if selected_attendant else 0)
        + (1 if selected_doctor else 0)
        + (1 if selected_user else 0)
        + (1 if selected_courses else 0)
        + (1 if selected_stages else 0)
        + (1 if selected_admission_statuses else 0)
        + (1 if q else 0)
    )

    # Pagination
    paginator = Paginator(leads, 25)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # Attach rich activity objects to each lead on current page
    for lead in page_obj:
        fus = getattr(lead, "prefetched_followups", [])
        lead.latest_followup_obj = fus[0] if fus else None
        nts = getattr(lead, "prefetched_notes", [])
        lead.latest_note_obj = nts[0] if nts else None
        acts = getattr(lead, "prefetched_activities", [])
        lead.latest_activity_obj = acts[0] if acts else None

    query_params = request.GET.copy()
    query_params.pop("page", None)

    courses = Course.objects.filter(is_active=True).order_by("name")
    stages = LeadStage.objects.filter(is_active=True).order_by("order", "name")
    admission_status_choices = AdmissionStatus.choices

    available_businesses = Hospital.objects.filter(is_active=True).order_by("name") if is_global_admin else []

    context = {
        "active": "team_history",
        "page_obj": page_obj,
        "total_count": paginator.count,
        "all_members_count": all_members_count,
        "q": q,
        # Hospital-specific filters
        "attendants": attendants,
        "doctors": doctors,
        "selected_attendant": selected_attendant,
        "selected_doctor": selected_doctor,
        # Academy & Hospital member filters
        "courses": Course.objects.filter(is_active=True).order_by("name"),
        "stages": LeadStage.objects.filter(is_active=True).order_by("order", "name"),
        "admission_status_choices": AdmissionStatus.choices,
        "team_members": team_members,
        "user_counts": user_counts,
        "selected_user": selected_user,
        "available_years": available_years,
        "available_months": available_months,
        "date_preset": date_preset,
        "selected_month": selected_month,
        "selected_year": selected_year,
        "date_from": date_from,
        "date_to": date_to,
        "active_date_label": active_date_label,
        "selected_courses": selected_courses,
        "selected_stages": selected_stages,
        "selected_admission_statuses": selected_admission_statuses,
        "current_sort": sort_by,
        "active_filters_count": active_filters_count,
        "query_params": query_params.urlencode(),
        "today_str": today.strftime("%Y-%m-%d"),
        "is_global_admin": is_global_admin,
        "available_businesses": available_businesses,
        "current_hospital": hospital,
        "is_hospital_business": is_hospital_business,
    }
    template = "leads/nel_team_history.html" if is_hospital_business else "leads/zapp_team_history.html"
    return render(request, template, context)


@login_required
def user_performance_analysis(request, user_id):
    """
    Dedicated User Performance Analysis view:
    Shows clean charts and KPIs for an individual team member:
    - Lead Temperature Distribution (Hot, Warm, Cold, Freeze, Uncontacted, etc.)
    - Deal / Admission / Appointment Status Breakdown (Won/Confirmed, Open, Lost, Hold)
    - Follow-ups Performance & Compliance (Completed, Pending, Rescheduled, Overdue)
    - Day-wise / Month-wise Lead Trend Analysis
    - Key Metrics: Total Leads, Won/Converted, Conversion Rate, Followups done, etc.
    Supported Period filters: Today, Last 7 Days, This Month, Last Month, All Time, Custom range.
    """
    import json
    from datetime import date, timedelta, datetime
    from followups.models import FollowUp, Activity, Note
    from accounts.models import Hospital

    target_user = get_object_or_404(User, id=user_id)

    # Permission check: superadmin, admin, manager, or user viewing own analytics
    is_global_admin = request.user.is_superuser or (
        request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital
    )
    if not (
        is_global_admin
        or request.user.role in [User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER]
        or request.user.id == target_user.id
    ):
        messages.error(request, "You do not have permission to view this performance analysis.")
        return redirect("leads:team_history")

    hospital = target_user.hospital or request.user.hospital
    is_hospital_business = False
    if hospital:
        btype = (hospital.settings or {}).get("business_type", "")
        if not btype:
            name_lower = (hospital.name or "").lower()
            if "hospital" in name_lower or "clinic" in name_lower or "medical" in name_lower or "nelson" in name_lower:
                btype = "hospital"
        is_hospital_business = str(btype).strip().lower() == "hospital"

    today = timezone.localdate()

    # Base queryset for this user's leads
    if is_hospital_business and target_user.role == User.Role.DOCTOR:
        dname = target_user.get_full_name() or target_user.username
        leads = Lead.objects.filter(is_archived=False).filter(
            Q(assigned_to=target_user) | Q(custom_data__doctor__icontains=dname)
        )
    else:
        leads = Lead.objects.filter(is_archived=False, assigned_to=target_user)

    if hospital:
        leads = leads.filter(hospital=hospital)

    # Date filter preset (Default to 'all' so historical imported data is immediately shown on charts)
    period = request.GET.get("period", "all").strip()
    date_from_str = request.GET.get("date_from", "").strip()
    date_to_str = request.GET.get("date_to", "").strip()

    start_date = None
    end_date = None
    period_label = "All Time"

    if period == "today":
        start_date = today
        end_date = today
        period_label = f"Today ({today.strftime('%d %b %Y')})"
    elif period == "7days":
        start_date = today - timedelta(days=6)
        end_date = today
        period_label = f"Last 7 Days ({start_date.strftime('%d %b')} - {end_date.strftime('%d %b %Y')})"
    elif period == "month":
        start_date = today.replace(day=1)
        end_date = today
        period_label = f"This Month ({today.strftime('%B %Y')})"
    elif period == "last_month":
        first_this_month = today.replace(day=1)
        end_last_month = first_this_month - timedelta(days=1)
        start_date = end_last_month.replace(day=1)
        end_date = end_last_month
        period_label = f"Last Month ({start_date.strftime('%B %Y')})"
    elif period == "all":
        period_label = "All Time"
    elif date_from_str or date_to_str:
        period = "custom"
        try:
            if date_from_str:
                start_date = datetime.strptime(date_from_str, "%Y-%m-%d").date()
            if date_to_str:
                end_date = datetime.strptime(date_to_str, "%Y-%m-%d").date()
            period_label = f"Custom: {start_date or 'Start'} to {end_date or 'End'}"
        except ValueError:
            pass

    # Apply date bounds to leads (checking inquiry_date or created_at)
    if start_date:
        leads = leads.filter(
            Q(inquiry_date__gte=start_date) | Q(created_at__date__gte=start_date)
        )
    if end_date:
        leads = leads.filter(
            Q(inquiry_date__lte=end_date) | Q(created_at__date__lte=end_date)
        )

    # 1. Key Metrics (KPIs)
    total_leads_count = leads.count()
    won_leads_count = leads.filter(
        Q(deal_status=DealStatus.WON)
        | Q(admission_status=AdmissionStatus.ADMISSION_DONE)
        | Q(deal_status="BOOKING CONFIRMED")
        | Q(deal_status="PAYMENT DONE")
    ).distinct().count()

    lost_leads_count = leads.filter(
        Q(deal_status=DealStatus.LOST) | Q(admission_status=AdmissionStatus.CANCELLED)
    ).count()

    open_leads_count = leads.filter(
        deal_status=DealStatus.OPEN
    ).exclude(admission_status=AdmissionStatus.ADMISSION_DONE).count()

    conversion_rate = round((won_leads_count / total_leads_count * 100), 1) if total_leads_count > 0 else 0.0

    # Follow-ups counts for target user
    user_followups = FollowUp.objects.filter(created_by=target_user)
    if start_date:
        user_followups = user_followups.filter(followup_date__gte=start_date)
    if end_date:
        user_followups = user_followups.filter(followup_date__lte=end_date)

    total_followups_count = user_followups.count()
    completed_followups_count = user_followups.filter(followup_status__in=["COMPLETED", "DONE"]).count()
    pending_followups_count = user_followups.filter(followup_status="PENDING").count()
    followup_completion_rate = round((completed_followups_count / total_followups_count * 100), 1) if total_followups_count > 0 else 0.0

    # 2. Temperature Distribution Data
    temp_counts = {
        "Hot": leads.filter(temperature=LeadTemperature.HOT).count(),
        "Warm": leads.filter(temperature=LeadTemperature.WARM).count(),
        "Cold": leads.filter(temperature=LeadTemperature.COLD).count(),
        "Freeze": leads.filter(temperature=LeadTemperature.FREEZE).count(),
        "Uncontacted": leads.filter(temperature=LeadTemperature.UNCONTACTED).count(),
    }
    # Include not picked if present
    not_picked_count = leads.filter(temperature=LeadTemperature.NOT_PICKED).count()
    if not_picked_count > 0:
        temp_counts["Not Picked"] = not_picked_count

    # 3. Status Distribution Data
    if is_hospital_business:
        status_counts = {
            "Open": leads.filter(deal_status=DealStatus.OPEN).count(),
            "Appointments Booked": leads.filter(deal_status="BOOKING CONFIRMED").count(),
            "Payment Done": leads.filter(deal_status="PAYMENT DONE").count(),
            "Hold": leads.filter(deal_status=DealStatus.HOLD).count(),
            "Lost / Cancelled": leads.filter(deal_status=DealStatus.LOST).count(),
        }
    else:
        status_counts = {
            "Open Leads": leads.filter(deal_status=DealStatus.OPEN).exclude(admission_status=AdmissionStatus.ADMISSION_DONE).count(),
            "Admissions Done": leads.filter(admission_status=AdmissionStatus.ADMISSION_DONE).count(),
            "Interested / Applied": leads.filter(admission_status__in=[AdmissionStatus.INTERESTED, AdmissionStatus.APPLIED]).count(),
            "On Hold": leads.filter(deal_status=DealStatus.HOLD).count(),
            "Lost": leads.filter(deal_status=DealStatus.LOST).count(),
        }

    # 4. Day-wise or Month-wise Performance Trend
    trend_labels = []
    trend_counts = []
    trend_won_counts = []

    if period in ["today", "7days", "month", "last_month"] or (start_date and end_date and (end_date - start_date).days <= 35):
        # Day-wise trend for short range
        calc_start = start_date or (today - timedelta(days=29))
        calc_end = end_date or today
        curr = calc_start
        while curr <= calc_end:
            label = curr.strftime("%d %b")
            trend_labels.append(label)
            day_leads = leads.filter(Q(inquiry_date=curr) | Q(created_at__date=curr))
            trend_counts.append(day_leads.count())
            day_won = day_leads.filter(
                Q(deal_status=DealStatus.WON)
                | Q(admission_status=AdmissionStatus.ADMISSION_DONE)
                | Q(deal_status="BOOKING CONFIRMED")
                | Q(deal_status="PAYMENT DONE")
            ).count()
            trend_won_counts.append(day_won)
            curr += timedelta(days=1)
    else:
        # Month-wise trend (All Time / Custom long range)
        from dateutil.relativedelta import relativedelta
        # Discover actual distinct months with leads for this user or last 12 months
        distinct_months = list(leads.filter(inquiry_date__isnull=False).dates("inquiry_date", "month", order="ASC"))
        if not distinct_months:
            curr_m = today.replace(day=1) - relativedelta(months=5)
            while curr_m <= today.replace(day=1):
                distinct_months.append(curr_m)
                curr_m += relativedelta(months=1)

        for curr_m in distinct_months:
            label = curr_m.strftime("%b %Y")
            trend_labels.append(label)
            m_leads = leads.filter(
                Q(inquiry_date__year=curr_m.year, inquiry_date__month=curr_m.month)
                | Q(created_at__year=curr_m.year, created_at__month=curr_m.month)
            )
            trend_counts.append(m_leads.count())
            m_won = m_leads.filter(
                Q(deal_status=DealStatus.WON)
                | Q(admission_status=AdmissionStatus.ADMISSION_DONE)
                | Q(deal_status="BOOKING CONFIRMED")
                | Q(deal_status="PAYMENT DONE")
            ).count()
            trend_won_counts.append(m_won)

    # 5. Follow-ups Status Breakdown Chart Data
    fu_status_counts = {
        "Completed": user_followups.filter(followup_status__in=["COMPLETED", "DONE"]).count(),
        "Pending": user_followups.filter(followup_status="PENDING").count(),
        "Rescheduled": user_followups.filter(followup_status="RESCHEDULED").count(),
        "Interested": user_followups.filter(followup_status="INTERESTED").count(),
        "Not Connected / DNP": user_followups.filter(followup_status__in=["DNP", "NOT_CONNECTED"]).count(),
    }

    # Chart datasets in clean JSON
    chart_data = {
        "temperature": {
            "labels": list(temp_counts.keys()),
            "counts": list(temp_counts.values()),
        },
        "status": {
            "labels": list(status_counts.keys()),
            "counts": list(status_counts.values()),
        },
        "trend": {
            "labels": trend_labels,
            "leads": trend_counts,
            "won": trend_won_counts,
        },
        "followups": {
            "labels": list(fu_status_counts.keys()),
            "counts": list(fu_status_counts.values()),
        }
    }

    # Recent 10 leads assigned to user
    recent_leads = leads.order_by("-updated_at")[:10]

    context = {
        "active": "team_history",
        "target_user": target_user,
        "current_hospital": hospital,
        "is_hospital_business": is_hospital_business,
        "period": period,
        "period_label": period_label,
        "date_from": date_from_str,
        "date_to": date_to_str,
        # KPIs
        "total_leads_count": total_leads_count,
        "won_leads_count": won_leads_count,
        "lost_leads_count": lost_leads_count,
        "open_leads_count": open_leads_count,
        "conversion_rate": conversion_rate,
        "total_followups_count": total_followups_count,
        "completed_followups_count": completed_followups_count,
        "pending_followups_count": pending_followups_count,
        "followup_completion_rate": followup_completion_rate,
        # Chart JSON
        "chart_data_json": json.dumps(chart_data),
        "recent_leads": recent_leads,
    }
    return render(request, "leads/user_performance_analysis.html", context)


@login_required
def lead_add(request):
    if request.user.role == User.Role.DOCTOR or not request.user.can_add_leads:
        messages.error(request, "Doctors cannot create new leads.")
        return redirect("dashboard:doctor_home")
        
    duplicates = None
    is_hospital = request.user.is_hospital_user
    FormClass = HospitalLeadForm if is_hospital else LeadForm
    template = "leads/nel_lead_form.html" if is_hospital else "leads/zapp_lead_form.html"
    
    if request.method == "POST":
        form = FormClass(request.POST, user=request.user)
        force = request.POST.get("force_create") == "1"
        if form.is_valid():
            if not force:
                mobile = form.cleaned_data.get("mobile")
                duplicates = Lead.objects.filter(mobile=mobile, is_archived=False)
                if duplicates.exists():
                    return render(request, template, {
                        "active": "leads_add", "form": form, "mode": "Add", "duplicates": duplicates
                    })
            lead = form.save(commit=False)
            lead.created_by = request.user
            if request.user.hospital:
                lead.hospital = request.user.hospital
                
            # If creator is a Lead Attendant, Counsellor, or employee without can_assign_leads permission, auto-assign to themselves
            if request.user.role in (User.Role.LEAD_ATTENDENT, User.Role.COUNSELLOR, User.Role.HR) or not request.user.can_assign_leads:
                if request.user.role not in (User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER):
                    lead.assigned_to = request.user
                elif not lead.assigned_to:
                    lead.assigned_to = request.user
            
            # Ensure defaults
            from leads.models import LeadStage, LeadSource, SourceCategory, Appointment, AppointmentStatus
            from notifications.models import Notification
            
            # Process Zappcode conditional form fields
            custom_fup = request.POST.get("custom_followup_date")
            custom_adm = request.POST.get("custom_admission_date")
            custom_reason = request.POST.get("custom_cancellation_reason")
            
            if custom_fup:
                from datetime import datetime
                try:
                    lead.next_followup_date = datetime.strptime(custom_fup, "%Y-%m-%d").date()
                except ValueError:
                    pass
            
            custom_dict = lead.custom_data or {}
            if custom_adm:
                custom_dict["admission_date"] = custom_adm
            if custom_reason:
                custom_dict["cancellation_reason"] = custom_reason
            lead.custom_data = custom_dict

            # Safeguard: Ensure stage is never null
            if not getattr(lead, "stage_id", None):
                default_stg = None
                if lead.assigned_to:
                    default_stg = LeadStage.objects.filter(name__iexact="Assigned").first()
                if not default_stg:
                    default_stg = (
                        LeadStage.objects.filter(name__iexact="New").first()
                        or LeadStage.objects.filter(order=1).first()
                        or LeadStage.objects.first()
                    )
                lead.stage = default_stg

            lead.save()
            form.save_m2m()
            messages.success(request, f"Lead #{lead.lead_code or lead.pk} ({lead.name}) saved successfully! ✅")

            # 1. Send Notification to assigned Telecaller / Staff member
            if lead.assigned_to and lead.assigned_to != request.user:
                Notification.objects.create(
                    user=lead.assigned_to,
                    title="New Lead Assigned",
                    message=f"Patient lead '{lead.name}' ({lead.mobile}) has been assigned to you by {request.user.get_full_name() or request.user.username}.",
                    link=f"/leads/{lead.pk}/",
                )

            # 2. Notify Doctor if appointment was booked or doctor selected
            doc_name = (lead.custom_data or {}).get('doctor')
            apt = Appointment.objects.filter(lead=lead).order_by('-id').first()
            if apt and apt.doctor_user and apt.doctor_user != request.user:
                time_str = apt.appointment_time.strftime('%I:%M %p') if apt.appointment_time else 'Slot pending'
                Notification.objects.create(
                    user=apt.doctor_user,
                    title="New Appointment Scheduled",
                    message=f"Patient {lead.name} appointment booked for {apt.appointment_date.strftime('%d %b %Y')} at {time_str}.",
                    link="/dashboard/doctor/",
                )
            elif doc_name:
                import re
                clean_doc_name = re.sub(r'^(dr\.?|doctor)\s+', '', doc_name, flags=re.IGNORECASE).strip()
                doc_user = User.objects.filter(role=User.Role.DOCTOR, hospital=request.user.hospital).filter(
                    Q(first_name__icontains=clean_doc_name) | Q(last_name__icontains=clean_doc_name) | Q(username__icontains=clean_doc_name)
                ).first()
                if doc_user and doc_user != request.user:
                    Notification.objects.create(
                        user=doc_user,
                        title="New Patient Lead Allocated",
                        message=f"Patient {lead.name} ({lead.mobile}) has been registered under your consultation by {request.user.get_full_name() or request.user.username}.",
                        link="/dashboard/doctor/",
                    )

            if request.user.role == User.Role.LEAD_ATTENDENT:
                return redirect("dashboard:telecaller_my_leads")
            return redirect("leads:lead_list")
        else:
            messages.error(request, "Could not save lead. Please check the highlighted fields below.")
    else:
        from django.utils import timezone
        form = FormClass(initial={"inquiry_date": timezone.localdate()}, user=request.user)
        
    return render(request, template, {
        "active": "leads_add", "form": form, "mode": "Add", "duplicates": duplicates,
    })

@login_required
def _get_lead_or_redirect(request, pk):
    """Helper to safely fetch a lead or redirect with a user-friendly warning message."""
    lead = Lead.objects.select_related(
        "course", "stage", "lead_source", "source_category", "campaign",
        "assigned_to", "assigned_manager", "original_lead_source", "original_source_category", "original_campaign",
    ).filter(pk=pk).first()
    if not lead:
        messages.warning(request, f"⚠️ Lead #{pk} not found or may have been removed.")
        return None
    return lead


@login_required
def lead_edit(request, pk):
    lead = _get_lead_or_redirect(request, pk)
    if not lead:
        if request.user.role == User.Role.DOCTOR:
            return redirect("dashboard:doctor_appointments")
        if request.user.role == User.Role.LEAD_ATTENDENT:
            return redirect("dashboard:telecaller_my_leads")
        return redirect("leads:lead_list")

    is_doctor = (request.user.role == User.Role.DOCTOR)
    is_view_only = is_doctor or (not _can_edit_lead(request.user, lead) and _can_access_lead(request.user, lead))

    if not is_view_only and not _can_edit_lead(request.user, lead):
        messages.error(request, "You do not have permission to edit this lead.")
        if is_doctor:
            return redirect("dashboard:doctor_appointments")
        return redirect("leads:lead_list")
        
    # Determine form/template based on hospital's business_type (not name string)
    lead_hospital_settings = (lead.hospital.settings or {}) if lead.hospital else {}
    lead_btype = lead_hospital_settings.get("business_type", "hospital")
    is_lead_hospital_type = (lead_btype == "hospital")
    FormClass = HospitalLeadForm if is_lead_hospital_type else LeadForm
    template = "leads/nel_lead_form.html" if is_lead_hospital_type else "leads/zapp_lead_form.html"
    
    if request.method == "POST":
        if is_view_only:
            messages.error(request, "Doctors and view-only users cannot modify lead details.")
            if is_doctor:
                return redirect("dashboard:doctor_appointments")
            return redirect("leads:lead_detail", pk=pk)

        form = FormClass(request.POST, instance=lead, user=request.user)
        if form.is_valid():
            saved_lead = form.save(commit=False)
            
            # If user is a Telecaller (Lead Attendant), automatically assign lead to them if not already assigned
            if request.user.role == User.Role.LEAD_ATTENDENT:
                saved_lead.assigned_to = request.user
            elif saved_lead.assigned_to is None:
                saved_lead.assigned_to = request.user
                
            # Check if telecaller filled calling remarks or call dates
            cd = saved_lead.custom_data if saved_lead.custom_data else {}
            has_call_remarks = bool(cd.get('calling_remarks') or cd.get('remarks') or cd.get('last_call_remark'))
            has_call_dates = bool(cd.get('last_calling_date') or cd.get('last_called_on'))
            has_call_interaction = has_call_remarks or has_call_dates
            
            try:
                is_won = saved_lead.deal_status == DealStatus.WON or saved_lead.admission_status == AdmissionStatus.ADMISSION_DONE or bool(cd.get('total') and float(cd.get('total') or 0) > 0)
                if is_lead_hospital_type:
                    # --- HOSPITAL LEAD STAGE RESOLUTION ---
                    if is_won:
                        won_stage = LeadStage.objects.filter(name__iexact='Payment Done').first() or \
                                    LeadStage.objects.filter(name__iexact='Appointment Completed').first()
                        if won_stage:
                            saved_lead.stage = won_stage
                        saved_lead.deal_status = DealStatus.WON
                        saved_lead.admission_status = AdmissionStatus.ADMISSION_DONE
                        cd['deal_status'] = 'Won (Payment Done)'
                        cd['appointment_status'] = cd.get('appointment_status') or 'Payment Done'
                        saved_lead.custom_data = cd
                    elif cd.get('appointment_status'):
                        apt_st = cd.get('appointment_status')
                        cd['deal_status'] = apt_st
                        stage_match = LeadStage.objects.filter(name__iexact=apt_st).first()
                        if not stage_match:
                            apt_upper = apt_st.upper()
                            if 'APPROV' in apt_upper or 'AWAIT' in apt_upper:
                                stage_match = LeadStage.objects.filter(name__iexact='Awaiting Approval from Doctor').first()
                            elif 'CONFIRM' in apt_upper or 'BOOK' in apt_upper:
                                stage_match = LeadStage.objects.filter(name__iexact='Booking Confirmed').first()
                            elif 'COMPLET' in apt_upper:
                                stage_match = LeadStage.objects.filter(name__iexact='Appointment Completed').first()
                            elif 'PENDING' in apt_upper and 'PAYMENT' in apt_upper:
                                stage_match = LeadStage.objects.filter(name__iexact='Payment Pending').first()
                            elif 'CANCEL' in apt_upper or 'LOST' in apt_upper or 'NOT INT' in apt_upper:
                                stage_match = LeadStage.objects.filter(name__iexact='Lost').first()
                            elif 'FOLLOW' in apt_upper:
                                stage_match = LeadStage.objects.filter(name__iexact='Follow-up').first()
                        if stage_match:
                            saved_lead.stage = stage_match
                        elif has_call_interaction:
                            contacted_stage = LeadStage.objects.filter(name__iexact='Contacted').first() or LeadStage.objects.filter(name__iexact='Assigned').first()
                            if contacted_stage:
                                saved_lead.stage = contacted_stage
                    elif has_call_interaction:
                        contacted_stage = LeadStage.objects.filter(name__iexact='Contacted').first() or LeadStage.objects.create(name='Contacted', order=3)
                        saved_lead.stage = contacted_stage
                        if saved_lead.temperature == LeadTemperature.UNCONTACTED:
                            saved_lead.temperature = LeadTemperature.WARM
                    else:
                        assigned_stage = LeadStage.objects.filter(name__iexact='Assigned').first() or LeadStage.objects.create(name='Assigned', order=2)
                        if not saved_lead.stage or saved_lead.stage.name.lower() in ['new', 'fresh', 'uncontacted']:
                            saved_lead.stage = assigned_stage
                else:
                    # --- ACADEMY / OTHER BUSINESS LEAD STAGE RESOLUTION ---
                    if is_won:
                        won_stage = LeadStage.objects.filter(name__iexact='Admission Done').first() or \
                                    LeadStage.objects.filter(name__iexact='Admission').first() or \
                                    LeadStage.objects.filter(name__iexact='Payment Done').first()
                        if won_stage:
                            saved_lead.stage = won_stage
                        saved_lead.deal_status = DealStatus.WON
                        saved_lead.admission_status = AdmissionStatus.ADMISSION_DONE
                        cd['deal_status'] = 'Won (Admission Done)'
                        saved_lead.custom_data = cd
                    elif cd.get('deal_status') or cd.get('stage'):
                        deal_st = cd.get('deal_status') or cd.get('stage')
                        stage_match = LeadStage.objects.filter(name__iexact=deal_st).first()
                        if stage_match:
                            saved_lead.stage = stage_match
                        elif has_call_interaction:
                            contacted_stage = LeadStage.objects.filter(name__iexact='Contacted').first() or LeadStage.objects.filter(name__iexact='Assigned').first()
                            if contacted_stage:
                                saved_lead.stage = contacted_stage
                    elif has_call_interaction:
                        contacted_stage = LeadStage.objects.filter(name__iexact='Contacted').first() or LeadStage.objects.create(name='Contacted', order=3)
                        saved_lead.stage = contacted_stage
                        if saved_lead.temperature == LeadTemperature.UNCONTACTED:
                            saved_lead.temperature = LeadTemperature.WARM
                    else:
                        assigned_stage = LeadStage.objects.filter(name__iexact='Assigned').first() or LeadStage.objects.create(name='Assigned', order=2)
                        if not saved_lead.stage or saved_lead.stage.name.lower() in ['new', 'fresh', 'uncontacted']:
                            saved_lead.stage = assigned_stage
            except Exception:
                pass
                
            # Process Zappcode conditional form fields
            custom_fup = request.POST.get("custom_followup_date")
            custom_adm = request.POST.get("custom_admission_date")
            custom_reason = request.POST.get("custom_cancellation_reason")
            
            if custom_fup:
                from datetime import datetime
                try:
                    saved_lead.next_followup_date = datetime.strptime(custom_fup, "%Y-%m-%d").date()
                except ValueError:
                    pass
            
            if custom_adm:
                cd["admission_date"] = custom_adm
            if custom_reason:
                cd["cancellation_reason"] = custom_reason
            saved_lead.custom_data = cd

            prev_assigned = lead.assigned_to
            saved_lead.save()
            if hasattr(form, 'save_m2m'):
                form.save_m2m()

            # 1. Send notification to newly assigned Telecaller if assigned_to changed
            from notifications.models import Notification
            from leads.models import Appointment
            if saved_lead.assigned_to and saved_lead.assigned_to != request.user and saved_lead.assigned_to != prev_assigned:
                Notification.objects.create(
                    user=saved_lead.assigned_to,
                    title="Lead Assigned to You",
                    message=f"Lead '{saved_lead.name}' ({saved_lead.mobile}) has been assigned to you by {request.user.get_full_name() or request.user.username}.",
                    link=f"/leads/{saved_lead.pk}/",
                )

            # 2. Appointment Synchronization & Doctor Notification
            from datetime import datetime
            raw_appo_date = cd.get("appo_booked_date") or cd.get("appointment_date")
            raw_appo_time = cd.get("appointment_time")
            doctor_name = cd.get("doctor", "").strip()
            apt_st_raw = (cd.get("appointment_status") or "").strip()

            if raw_appo_date and is_lead_hospital_type:
                try:
                    parsed_apt_date = datetime.strptime(str(raw_appo_date).strip(), "%Y-%m-%d").date()
                except ValueError:
                    parsed_apt_date = None

                if parsed_apt_date:
                    # Find doctor user
                    doc_user = None
                    if doctor_name:
                        doc_user = User.objects.filter(
                            hospital=saved_lead.hospital,
                            role=User.Role.DOCTOR
                        ).filter(
                            Q(first_name__icontains=doctor_name) |
                            Q(username__icontains=doctor_name) |
                            Q(last_name__icontains=doctor_name)
                        ).first()

                    # Find existing latest appointment
                    existing_apt = Appointment.objects.filter(lead=saved_lead).order_by('-id').first()
                    
                    # Determine if slot changed from previous appointment
                    slot_is_same = False
                    if existing_apt:
                        date_same = (existing_apt.appointment_date == parsed_apt_date)
                        time_same = True
                        if raw_appo_time and existing_apt.appointment_time:
                            time_same = (str(existing_apt.appointment_time)[:5] == str(raw_appo_time)[:5])
                        slot_is_same = (date_same and time_same)

                    # Check if telecaller is confirming slot set by doctor
                    if any(k in apt_st_raw.lower() for k in ['confirm', 'book', 'yes', 'schedul']):
                        if slot_is_same and existing_apt:
                            # Slot kept exactly as doctor setup -> auto-approve without asking doctor for re-approval
                            existing_apt.status = AppointmentStatus.APPROVED
                            existing_apt.save(update_fields=['status'])
                            cd['appointment_status'] = 'Booking Confirmed'
                            cd['appointment_confirmed_at'] = timezone.now().strftime('%Y-%m-%d %H:%M')
                            saved_lead.custom_data = cd
                            saved_lead.save(update_fields=['custom_data'])

                            if doc_user and doc_user != request.user:
                                time_str = existing_apt.appointment_time.strftime('%I:%M %p') if existing_apt.appointment_time else 'Slot not fixed'
                                Notification.objects.create(
                                    user=doc_user,
                                    title="Next Appointment Confirmed from Patient",
                                    message=f"Telecaller confirmed patient {saved_lead.name}'s appointment for {parsed_apt_date.strftime('%d %b %Y')} at {time_str}. Confirmed in your appointments tab.",
                                    link="/dashboard/doctor/",
                                )
                        else:
                            # Slot was changed or is new -> needs Doctor Approval confirmation
                            if existing_apt and existing_apt.status != AppointmentStatus.COMPLETED:
                                existing_apt.appointment_date = parsed_apt_date
                                if raw_appo_time:
                                    existing_apt.appointment_time = raw_appo_time
                                if doc_user:
                                    existing_apt.doctor_user = doc_user
                                if doctor_name:
                                    existing_apt.doctor_name = doctor_name
                                existing_apt.status = AppointmentStatus.PENDING_APPROVAL
                                existing_apt.save(update_fields=['appointment_date', 'appointment_time', 'doctor_user', 'doctor_name', 'status'])
                            else:
                                existing_apt = Appointment.objects.create(
                                    lead=saved_lead,
                                    hospital=saved_lead.hospital,
                                    doctor_name=doctor_name or "Consulting Doctor",
                                    doctor_user=doc_user,
                                    appointment_date=parsed_apt_date,
                                    appointment_time=raw_appo_time if raw_appo_time else None,
                                    status=AppointmentStatus.PENDING_APPROVAL,
                                    created_by=request.user
                                )

                            if doc_user and doc_user != request.user:
                                time_str = existing_apt.appointment_time.strftime('%I:%M %p') if existing_apt.appointment_time else 'Slot not fixed'
                                Notification.objects.create(
                                    user=doc_user,
                                    title="New Appointment Request / Slot Changed",
                                    message=f"Telecaller updated/requested appointment for patient {saved_lead.name} on {parsed_apt_date.strftime('%d %b %Y')} at {time_str}. Please review and approve.",
                                    link="/dashboard/doctor/",
                                )
            else:
                apt = Appointment.objects.filter(lead=saved_lead).order_by('-id').first()
                if apt and apt.doctor_user and apt.doctor_user != request.user:
                    time_str = apt.appointment_time.strftime('%I:%M %p') if apt.appointment_time else 'Slot not fixed'
                    Notification.objects.create(
                        user=apt.doctor_user,
                        title="Appointment Update",
                        message=f"Patient {saved_lead.name} appointment details updated.",
                        link="/dashboard/doctor/",
                    )

            messages.success(request, f"Lead #{saved_lead.lead_code or saved_lead.pk} ({saved_lead.name}) updated and assigned successfully! ✅")
            
            # Smart Redirect: Return to previous list page if specified, otherwise role-based redirect
            return_url = request.POST.get("return_to") or request.GET.get("return_to") or request.GET.get("next")
            if return_url:
                return redirect(return_url)

            if request.user.role == User.Role.LEAD_ATTENDENT:
                return redirect("dashboard:telecaller_my_leads")
            elif request.user.hospital:
                return redirect("leads:lead_list")
            return redirect("leads:lead_detail", pk=lead.pk)
    else:
        form = FormClass(instance=lead, user=request.user)

    # Check if appointment is confirmed/approved/scheduled/completed or payment done
    from leads.models import Appointment, AppointmentStatus
    cd = lead.custom_data or {}
    apt_status_str = str(cd.get('appointment_status', '')).upper()
    deal_status_str = str(cd.get('deal_status', '')).upper()
    has_completed_apt = Appointment.objects.filter(lead=lead, status=AppointmentStatus.COMPLETED).exists()
    is_payment_done = 'PAYMENT' in apt_status_str or 'PAYMENT' in deal_status_str or bool(cd.get('total') and float(cd.get('total') or 0) > 0)
    is_appointment_completed = 'COMPLET' in apt_status_str or 'DONE' in apt_status_str or 'VISIT' in apt_status_str or has_completed_apt or is_payment_done

    # Get latest active/confirmed appointment for this lead
    latest_appointment = Appointment.objects.filter(lead=lead).order_by('-appointment_date', '-id').first()
    
    # If actual Appointment object exists and has been approved/completed by doctor
    is_appointment_confirmed = False
    if latest_appointment and latest_appointment.status in [AppointmentStatus.APPROVED, AppointmentStatus.COMPLETED]:
        is_appointment_confirmed = True
    elif cd.get('appointment_confirmed_at') and any(k in apt_status_str for k in ['CONFIRM', 'COMPLET', 'VISIT']):
        is_appointment_confirmed = True

    current_apt_status = cd.get("appointment_status") or ""
    if current_apt_status in ['nan', 'None', 'null', 'NULL', '']:
        current_apt_status = ""

    saved_initial = {
        "hospital_branch": cd.get("hospital_branch") or cd.get("branch") or "",
        "department": cd.get("department") or "",
        "doctor": cd.get("doctor") or (latest_appointment.doctor_name if latest_appointment else ""),
        "disease": cd.get("disease") or "",
        "appointment_status": current_apt_status,
        "appo_booked_date": str(latest_appointment.appointment_date) if latest_appointment and latest_appointment.appointment_date else (cd.get("appo_booked_date") or ""),
        "appointment_time": latest_appointment.appointment_time.strftime("%H:%M") if latest_appointment and latest_appointment.appointment_time else (cd.get("appointment_time") or ""),
    }

    # Calculate grand total paid across all bills
    billing_history = cd.get('billing_history', [])
    history_total = sum(float(b.get('total') or 0) for b in billing_history if isinstance(b, dict))
    single_total = float(cd.get('total_paid') or cd.get('total') or 0)
    grand_total_paid = max(history_total, single_total) if billing_history else single_total

    # Compute safe cancel_url based on return_to or HTTP_REFERER
    cancel_url = request.GET.get("return_to") or request.GET.get("next")
    if not cancel_url:
        ref = request.META.get('HTTP_REFERER', '')
        if ref and f"/leads/{lead.pk}/edit/" not in ref:
            cancel_url = ref
    if not cancel_url:
        if is_doctor:
            cancel_url = "/dashboard/doctor/appointments/"
        elif request.user.role == User.Role.LEAD_ATTENDENT:
            cancel_url = "/dashboard/telecaller/my-leads/"
        elif request.user.hospital:
            cancel_url = "/leads/"
        else:
            cancel_url = f"/leads/{lead.pk}/"

    # Determine appointment state flags
    is_appointment_pending = False
    is_appointment_scheduled = False
    if latest_appointment:
        if latest_appointment.status == AppointmentStatus.PENDING_APPROVAL:
            is_appointment_pending = True
        elif latest_appointment.status == AppointmentStatus.SCHEDULED:
            is_appointment_scheduled = True

    return render(request, template, {
        "active": "leads_all",
        "form": form,
        "mode": "Edit",
        "obj": lead,
        "cancel_url": cancel_url,
        "is_view_only": is_view_only,
        "is_doctor": is_doctor,
        "is_appointment_completed": is_appointment_completed,
        "is_appointment_confirmed": is_appointment_confirmed,
        "is_appointment_pending": is_appointment_pending,
        "is_appointment_scheduled": is_appointment_scheduled,
        "is_payment_done": is_payment_done,
        "grand_total_paid": grand_total_paid,
        "billing_history_list": billing_history,
        "latest_appointment": latest_appointment,
        "all_appointments": Appointment.objects.filter(lead=lead).order_by("-appointment_date", "-id"),
        "saved_initial": saved_initial,
    })


@login_required
def lead_detail(request, pk):
    lead = _get_lead_or_redirect(request, pk)
    if not lead:
        if request.user.role == User.Role.LEAD_ATTENDENT:
            return redirect("dashboard:telecaller_my_leads")
        return redirect("leads:lead_list")

    if not _can_access_lead(request.user, lead):
        messages.error(request, "You do not have permission to access this lead.")
        return redirect("leads:lead_list")

    timeline = lead.activities.all()[:200]
    followups = lead.followups.select_related("created_by").all()[:50]
    admission = getattr(lead, "admission", None)
    
    # Retrieve active/approved users for the assignment form
    if lead.hospital:
        employees = User.objects.filter(is_active=True, is_approved=True, hospital=lead.hospital, role__in=['COUNSELLOR', 'HR', User.Role.MANAGER, User.Role.LEAD_ATTENDENT])
        managers = User.objects.filter(is_active=True, is_approved=True, hospital=lead.hospital, role__in=[User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER])
    else:
        employees = User.objects.filter(is_active=True, is_approved=True, role__in=['COUNSELLOR', 'HR', User.Role.MANAGER, User.Role.LEAD_ATTENDENT])
        managers = User.objects.filter(is_active=True, is_approved=True, role__in=[User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER])
    
    latest_appointment = None
    custom_field_data = []

    # Check lead's own organization business_type (Hospital vs Academy)
    lead_hospital_settings = (lead.hospital.settings or {}) if lead.hospital else {}
    lead_btype = lead_hospital_settings.get("business_type")
    if not lead_btype and lead.hospital:
        name_lower = (lead.hospital.name or "").lower()
        lead_btype = "hospital" if any(k in name_lower for k in ["hospital", "clinic", "medical", "nelson"]) else "academy"
    
    is_lead_hospital = bool(lead.hospital and lead_btype == "hospital")

    if is_lead_hospital:
        from leads.models import Appointment, LeadCustomField
        appointments_history = list(Appointment.objects.filter(lead=lead).order_by('-appointment_date', '-id'))
        latest_appointment = appointments_history[0] if appointments_history else None
        cfs = LeadCustomField.objects.filter(hospital=lead.hospital, is_active=True).order_by("order")
        cd = lead.custom_data or {}
        for cf in cfs:
            if cf.name in cd and cd[cf.name] != "":
                custom_field_data.append({"label": cf.label, "value": cd[cf.name]})
    else:
        appointments_history = []
        
    can_edit = _can_edit_lead(request.user, lead)
    is_owner = (lead.assigned_to == request.user or lead.assigned_to is None)
    can_convert = is_owner or request.user.can_edit_any_lead or request.user.role in [User.Role.SUPER_ADMIN, User.Role.MANAGER]

    # --- Return URL (Back Button) & Queue Navigation ---
    return_to = request.GET.get("return_to", "").strip()
    if not return_to:
        referer = request.META.get("HTTP_REFERER", "")
        if referer:
            from urllib.parse import urlparse
            ref_path = urlparse(referer).path
            # Only use referer if it's not another lead detail page
            if "/leads/" in referer and "/lead/" not in ref_path:
                return_to = referer
            elif "/dashboard/" in referer or "/followups/" in referer or "/admissions/" in referer or "/payments/" in referer:
                return_to = referer

    # Fallback return_url if none provided
    if return_to:
        return_url = return_to
    else:
        # Default fallback to leads list
        fallback_params = request.GET.copy()
        if "return_to" in fallback_params:
            del fallback_params["return_to"]
        qs_str = fallback_params.urlencode()
        return_url = f"/leads/{'?' + qs_str if qs_str else ''}"

    # --- Queue Navigation (Prev / Next Lead) ---
    # Retrieve the leads list scoped to all current request filters (or tenant defaults if no filter applied)
    filtered_leads_qs = get_filtered_leads(request)

    lead_ids = list(filtered_leads_qs.values_list("id", flat=True))
    prev_lead = None
    next_lead = None

    if lead.id in lead_ids:
        cur_idx = lead_ids.index(lead.id)
        if cur_idx > 0:
            prev_id = lead_ids[cur_idx - 1]
            prev_lead = Lead.objects.filter(id=prev_id).first()
        if cur_idx < len(lead_ids) - 1:
            next_id = lead_ids[cur_idx + 1]
            next_lead = Lead.objects.filter(id=next_id).first()
    else:
        # Fallback if lead is not in current filtered list
        prev_lead = filtered_leads_qs.filter(
            Q(created_at__gt=lead.created_at) | Q(created_at=lead.created_at, id__gt=lead.id)
        ).order_by('created_at', 'id').first()
        next_lead = filtered_leads_qs.filter(
            Q(created_at__lt=lead.created_at) | Q(created_at=lead.created_at, id__lt=lead.id)
        ).order_by('-created_at', '-id').first()

    query_params = request.GET.copy()
    if return_url and "return_to" not in query_params:
        query_params["return_to"] = return_url
    filter_querystring = query_params.urlencode()

    stages = LeadStage.objects.filter(is_active=True).order_by("order", "name")
    temperatures = LeadTemperature.choices
    deal_statuses = DealStatus.choices
    admission_statuses = AdmissionStatus.choices

    courses_qs = Course.objects.filter(is_active=True, hospital=lead.hospital) if lead.hospital else Course.objects.filter(is_active=True)
    course_data = {
        str(c.id): {
            "name": c.name,
            "base_price": float(c.base_price),
            "max_discount": float(c.max_discount),
        }
        for c in courses_qs
    }

    template = "leads/nel_lead_detail.html" if is_lead_hospital else "leads/zapp_lead_detail.html"
    return render(request, template, {
        "active": "leads_all", "lead": lead, "timeline": timeline, "followups": followups, "admission": admission,
        "is_lead_hospital": is_lead_hospital,
        "return_url": return_url,
        "prev_lead": prev_lead,
        "next_lead": next_lead,
        "filter_querystring": filter_querystring,
        "latest_appointment": latest_appointment,
        "appointments_history": appointments_history,
        "custom_field_data": custom_field_data,
        "followup_modes": FollowUpMode.choices, "followup_statuses": FollowUpStatus.choices,
        "stages": stages,
        "courses": courses_qs,
        "course_data_json": json.dumps(course_data),
        "temperatures": temperatures,
        "deal_statuses": deal_statuses,
        "admission_statuses": admission_statuses,
        "today": timezone.localdate(),
        "employees": employees,
        "managers": managers,
        "can_edit": can_edit,
        "can_convert": can_convert,
    })


def _can_archive_lead(user):
    if user.is_superuser:
        return True
    if user.role in [User.Role.SUPER_ADMIN, User.Role.ADMIN]:
        return True
    if user.hospital and user.role in [User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER]:
        return True
    return False


@login_required
def lead_archive(request, pk):
    lead = _get_lead_or_redirect(request, pk)
    if not lead:
        return redirect("leads:lead_list")
    if not _can_access_lead(request.user, lead):
        messages.error(request, "You do not have permission to access this lead.")
        return redirect("leads:lead_list")
    if not _can_archive_lead(request.user):
        messages.error(request, "Only Hospital Admins and Zappcode Admins can archive or restore leads.")
        return redirect("leads:lead_detail", pk=pk)
    lead.is_archived = not lead.is_archived
    lead.save(update_fields=["is_archived"])
    messages.success(request, f"Lead #{lead.lead_code or lead.pk} ({lead.name}) {'archived' if lead.is_archived else 'restored successfully'}.")
    next_url = request.GET.get('next') or request.POST.get('next')
    if next_url:
        return redirect(next_url)
    return redirect("leads:lead_detail", pk=pk)


@login_required
def add_note(request, pk):
    lead = _get_lead_or_redirect(request, pk)
    if not lead:
        return redirect("leads:lead_list")
    if not _can_access_lead(request.user, lead):
        messages.error(request, "You do not have permission to access this lead.")
        return redirect("leads:lead_list")
    if request.method == "POST" and request.POST.get("note", "").strip():
        note_text = request.POST["note"].strip()
        Note.objects.create(lead=lead, note=note_text, created_by=request.user)
        if lead.assigned_to is None:
            lead.assigned_to = request.user
        if not lead.stage or lead.stage.name.lower() in ['new', 'fresh', 'uncontacted']:
            contacted_stage = LeadStage.objects.filter(name__iexact='Contacted').first() or LeadStage.objects.filter(name__iexact='Assigned').first()
            if contacted_stage:
                lead.stage = contacted_stage
        
        # Recalculate dynamic temperature and sync priority
        new_temp = lead.custom_temperature
        if new_temp:
            lead.temperature = new_temp.upper()
            cd = lead.custom_data or {}
            cd['priority'] = new_temp
            lead.custom_data = cd

        lead.save()
        messages.success(request, "Note added.")
    return redirect("leads:lead_detail", pk=pk)


@login_required
def add_followup(request, pk):
    lead = _get_lead_or_redirect(request, pk)
    if not lead:
        return redirect("leads:lead_list")
    if not _can_access_lead(request.user, lead):
        messages.error(request, "You do not have permission to access this lead.")
        return redirect("leads:lead_list")
    if request.method == "POST":
        today = timezone.localdate()
        fu_date_raw = request.POST.get("followup_date")
        next_fu_date_raw = request.POST.get("next_followup_date")

        fu_date = today
        if fu_date_raw:
            try:
                parsed_fu = datetime.strptime(fu_date_raw, "%Y-%m-%d").date()
                if parsed_fu < today:
                    messages.error(request, "Follow-up date cannot be in the past.")
                    return redirect("leads:lead_detail", pk=pk)
                fu_date = parsed_fu
            except ValueError:
                fu_date = today

        next_fu_date = None
        if next_fu_date_raw:
            try:
                parsed_next = datetime.strptime(next_fu_date_raw, "%Y-%m-%d").date()
                if parsed_next < today:
                    messages.error(request, "Next follow-up date cannot be in the past.")
                    return redirect("leads:lead_detail", pk=pk)
                next_fu_date = parsed_next
            except ValueError:
                next_fu_date = None

        fu_time_raw = request.POST.get("followup_time") or None
        fu_time = None
        if fu_time_raw:
            try:
                fu_time = datetime.strptime(fu_time_raw, "%H:%M").time()
                if fu_date == today:
                    now_time = timezone.localtime().time()
                    if fu_time < now_time:
                        messages.error(request, f"Follow-up time cannot be in the past (Current time is {now_time.strftime('%I:%M %p')}). Please select an upcoming time.")
                        return redirect("leads:lead_detail", pk=pk)
            except ValueError:
                fu_time = None

        # If no explicit next_followup_date set, but followup_date is today or future,
        # treat followup_date as the next_followup_date so dashboard shows it correctly
        if next_fu_date is None and fu_date >= today:
            next_fu_date = fu_date

        FollowUp.objects.create(
            lead=lead,
            followup_date=fu_date,
            followup_time=fu_time,
            followup_mode=request.POST.get("followup_mode", FollowUpMode.CALL),
            followup_status=request.POST.get("followup_status", FollowUpStatus.COMPLETED),
            comment=request.POST.get("comment", ""),
            next_followup_date=next_fu_date,
            next_followup_time=request.POST.get("next_followup_time") or None,
            created_by=request.user,
        )
        if lead.assigned_to is None:
            lead.assigned_to = request.user
        if not lead.stage or lead.stage.name.lower() in ['new', 'fresh', 'uncontacted']:
            fu_stage = LeadStage.objects.filter(name__iexact='Follow-up').first() or LeadStage.objects.filter(name__iexact='Contacted').first() or LeadStage.objects.filter(name__iexact='Assigned').first()
            if fu_stage:
                lead.stage = fu_stage
        if lead.temperature == LeadTemperature.UNCONTACTED or lead.temperature == 'UNCONTACTED':
            lead.temperature = LeadTemperature.WARM
        lead.save()
        messages.success(request, "Follow-up recorded.")
    return redirect("leads:lead_detail", pk=pk)


@login_required
def update_followup_status(request, pk, fu_id):
    lead = _get_lead_or_redirect(request, pk)
    if not lead:
        return redirect("leads:lead_list")
    if not _can_access_lead(request.user, lead):
        messages.error(request, "You do not have permission to access this lead.")
        return redirect("leads:lead_list")
    
    fu = get_object_or_404(FollowUp, id=fu_id, lead=lead)
    if request.method == "POST":
        if fu.followup_status == FollowUpStatus.DONE:
            messages.warning(request, "This follow-up is already marked as Done and cannot be updated.")
            return redirect("leads:lead_detail", pk=pk)

        new_status = request.POST.get("followup_status")
        update_note = request.POST.get("update_note", "").strip()
        next_date_str = request.POST.get("next_followup_date", "").strip()
        next_time_str = request.POST.get("next_followup_time", "").strip()

        if new_status and new_status in FollowUpStatus.values:
            old_status_display = fu.get_followup_status_display()
            fu.followup_status = new_status
            if update_note:
                existing_comment = (fu.comment or "").strip()
                author_name = request.user.get_full_name() or request.user.username
                note_entry = f"[Update by {author_name}]: {update_note}"
                fu.comment = f"{existing_comment}\n{note_entry}".strip() if existing_comment else note_entry
            
            if new_status == FollowUpStatus.DONE:
                fu.next_followup_date = None
                fu.next_followup_time = None
            else:
                if next_date_str:
                    try:
                        fu.next_followup_date = datetime.strptime(next_date_str, "%Y-%m-%d").date()
                    except (ValueError, TypeError):
                        pass
                if next_time_str:
                    try:
                        fu.next_followup_time = datetime.strptime(next_time_str, "%H:%M").time()
                    except (ValueError, TypeError):
                        pass

            fu.save()
            
            # Log Activity so timeline shows the status update
            status_display = "Follow-up Done" if new_status == FollowUpStatus.DONE else fu.get_followup_status_display()
            Activity.objects.create(
                lead=lead,
                activity_type=ActivityType.FOLLOWUP,
                description=f"Follow-up status updated from {old_status_display} to {status_display}" + (f" - Note: {update_note}" if update_note else ""),
                created_by=request.user,
            )
            messages.success(request, f"Follow-up status updated to {status_display}.")
        else:
            messages.warning(request, "Please select a valid follow-up status.")
    return redirect("leads:lead_detail", pk=pk)


@login_required
def convert_admission(request, pk):
    lead = _get_lead_or_redirect(request, pk)
    if not lead:
        return redirect("leads:lead_list")
    if not _can_access_lead(request.user, lead):
        messages.error(request, "You do not have permission to access this lead.")
        return redirect("leads:lead_list")

    # Only lead owner (assigned counsellor) or users with Manager/Admin role can convert lead to admission
    is_owner = (lead.assigned_to == request.user or lead.assigned_to is None)
    is_admin_or_manager = (request.user.role in [User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER] or request.user.can_edit_any_lead)
    if not is_owner and not is_admin_or_manager:
        messages.error(request, "You cannot convert another team member's lead to admission. Only the assigned counselor, manager, or admin can perform admission conversion.")
        return redirect("leads:lead_detail", pk=pk)

    if hasattr(lead, "admission"):
        messages.info(request, "This lead already has an admission record.")
        return redirect("leads:lead_detail", pk=pk)
    if request.method == "POST":
        course_id = request.POST.get("course")
        selected_course = lead.course
        if course_id and course_id.isdigit():
            c_obj = Course.objects.filter(id=int(course_id)).first()
            if c_obj:
                selected_course = c_obj
                lead.course = c_obj
                lead.save(update_fields=["course"])

        total_fee = float(request.POST.get("total_fee") or 0)
        discount = float(request.POST.get("discount") or 0)
        max_discount = float(selected_course.max_discount) if selected_course else 0.0
        extra_reason = request.POST.get("extra_discount_reason", "").strip()

        # Backend validation
        if discount > max_discount and not extra_reason:
            messages.error(request, "Reason for extra discount is required since the discount exceeds the course maximum allowed discount limit.")
            return redirect("leads:lead_detail", pk=pk)

        payment_option = request.POST.get("payment_option", "ONE_TIME_UPI").strip()
        payment_plan = "EMI" if payment_option == "EMI" else "FULL"
        
        emi_months = int(request.POST.get("emi_months") or 0) if payment_option == "EMI" else 0
        monthly_emi_amount = float(request.POST.get("monthly_emi_amount") or 0) if payment_option == "EMI" else 0.0
        date_of_joining = request.POST.get("date_of_joining") or None
        tutor = request.POST.get("tutor", "").strip()
        batch = request.POST.get("batch", "").strip()
        admission_date = request.POST.get("admission_date") or timezone.localdate()

        # Update Lead stage to Admission dynamically when converted
        admission_stage = LeadStage.objects.filter(name__icontains="admission", is_active=True).first()
        if admission_stage:
            lead.stage = admission_stage
            lead.deal_status = "WON"
            lead.admission_status = "ADMISSION_DONE"
            lead.save(update_fields=["stage", "deal_status", "admission_status"])

        adm = Admission.objects.create(
            lead=lead,
            student_name=lead.name,
            course=selected_course,
            admission_date=admission_date,
            total_fee=total_fee,
            discount=discount,
            max_allowed_discount=max_discount,
            extra_discount_reason=extra_reason if discount > max_discount else "",
            payment_plan=payment_plan,
            payment_option=payment_option,
            emi_months=emi_months,
            monthly_emi_amount=monthly_emi_amount,
            date_of_joining=date_of_joining,
            tutor=tutor,
            batch=batch,
            assigned_counselor=lead.assigned_to,
        )

        # Automatically schedule EMI installments if EMI plan selected
        if payment_option == "EMI" and emi_months > 0 and monthly_emi_amount > 0:
            from admissions.models import Installment
            from dateutil.relativedelta import relativedelta
            import datetime
            base_date = datetime.date.fromisoformat(str(admission_date)) if isinstance(admission_date, str) else admission_date
            for m in range(1, emi_months + 1):
                due_d = base_date + relativedelta(months=m)
                Installment.objects.create(
                    admission=adm,
                    amount=monthly_emi_amount,
                    due_date=due_d,
                )

        messages.success(request, f"Lead converted to admission successfully ({selected_course.name if selected_course else 'General'}).")
        return redirect("admissions:list")
    return redirect("leads:lead_detail", pk=pk)


@login_required
def lead_self_assign(request, pk):
    is_ajax = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or request.headers.get("accept") == "application/json"
        or "application/json" in request.headers.get("accept", "")
    )

    lead = _get_lead_or_redirect(request, pk)
    if not lead:
        if is_ajax:
            return JsonResponse({"status": "error", "message": "Lead not found."}, status=404)
        return redirect("leads:lead_list")
    
    if not getattr(request.user, "can_self_assign", True):
        err_msg = "You do not have permission to self-assign leads. Please contact your administrator."
        if is_ajax:
            return JsonResponse({"status": "error", "message": err_msg}, status=403)
        messages.error(request, err_msg)
        return redirect("leads:lead_detail", pk=pk)

    # Allow user to capture / self-assign if lead is unassigned or if user has access
    if lead.assigned_to and lead.assigned_to != request.user and not request.user.can_assign_leads:
        warn_msg = f"Lead is already assigned to {lead.assigned_to.get_full_name() or lead.assigned_to.username}."
        if is_ajax:
            return JsonResponse({"status": "warning", "message": warn_msg}, status=400)
        messages.warning(request, warn_msg)
        return redirect("leads:lead_detail", pk=pk)

    user_full_name = request.user.get_full_name() or request.user.username
    lead.assigned_to = request.user
    if isinstance(lead.custom_data, dict):
        lead.custom_data['lead_attendant'] = user_full_name

    if not lead.stage or lead.stage.name.lower() in ['new', 'fresh', 'uncontacted']:
        assigned_stage = LeadStage.objects.filter(name__iexact='Assigned').first() or LeadStage.objects.filter(name__iexact='Contacted').first()
        if assigned_stage:
            lead.stage = assigned_stage
    lead.save()

    Activity.objects.create(
        lead=lead,
        created_by=request.user,
        activity_type="ASSIGNMENT",
        description=f"Lead captured / self-assigned by {user_full_name}.",
    )
    success_msg = f"🎉 Lead #{lead.lead_code or lead.pk} ({lead.name}) successfully captured and assigned to you!"

    if is_ajax:
        return JsonResponse({
            "status": "success",
            "message": success_msg,
            "lead_id": lead.pk,
            "assigned_to": user_full_name,
            "assigned_to_id": request.user.pk
        })

    messages.success(request, success_msg)
    
    next_url = request.GET.get("next") or request.POST.get("next")
    if next_url:
        from django.utils.http import url_has_allowed_host_and_scheme
        if url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            return redirect(next_url)
    return redirect("leads:lead_detail", pk=pk)


@login_required
def assign_lead(request, pk):
    lead = _get_lead_or_redirect(request, pk)
    if not lead:
        return redirect("leads:lead_list")
    if not request.user.can_assign_leads:
        messages.error(request, "You do not have permission to assign leads.")
        return redirect("leads:lead_detail", pk=pk)
        
    if request.method == "POST":
        assignee_id = request.POST.get("assigned_to")
        manager_id = request.POST.get("assigned_manager")
        
        old_assigned = lead.assigned_to
        old_mgr = lead.assigned_manager

        if assignee_id:
            assignee = User.objects.filter(pk=assignee_id, is_active=True).first()
            lead.assigned_to = assignee
            if not lead.stage or lead.stage.name.lower() in ['new', 'fresh', 'uncontacted']:
                assigned_stage = LeadStage.objects.filter(name__iexact='Assigned').first() or LeadStage.objects.filter(name__iexact='Contacted').first()
                if assigned_stage:
                    lead.stage = assigned_stage
        else:
            lead.assigned_to = None

        if manager_id:
            mgr = User.objects.filter(pk=manager_id, is_active=True).first()
            lead.assigned_manager = mgr
        else:
            lead.assigned_manager = None

        lead.save()

        # Log Activity
        desc_parts = []
        if old_assigned != lead.assigned_to:
            new_name = lead.assigned_to.get_full_name() or lead.assigned_to.username if lead.assigned_to else "Unassigned"
            old_name = old_assigned.get_full_name() or old_assigned.username if old_assigned else "Unassigned"
            desc_parts.append(f"Owner changed from '{old_name}' to '{new_name}'")
        if old_mgr != lead.assigned_manager:
            new_mgr_name = lead.assigned_manager.get_full_name() or lead.assigned_manager.username if lead.assigned_manager else "None"
            old_mgr_name = old_mgr.get_full_name() or old_mgr.username if old_mgr else "None"
            desc_parts.append(f"Manager changed from '{old_mgr_name}' to '{new_mgr_name}'")
            
        if desc_parts:
            Activity.objects.create(
                lead=lead,
                created_by=request.user,
                activity_type="ASSIGNMENT",
                description=f"Lead reassigned by {request.user.get_full_name() or request.user.username}: {', '.join(desc_parts)}.",
            )

        if lead.assigned_to and lead.assigned_to != request.user and lead.assigned_to != old_assigned:
            from notifications.models import Notification
            assigner_name = request.user.get_full_name() or request.user.username
            Notification.objects.create(
                user=lead.assigned_to,
                title="Lead Assigned to You",
                message=f"Lead '{lead.name}' ({lead.mobile}) has been assigned to you by {assigner_name}.",
                link=f"/leads/{lead.pk}/",
            )

        messages.success(request, f"Lead assignment updated successfully.")
            
    return redirect("leads:lead_detail", pk=pk)


@login_required
def lead_quick_update_stage(request, pk):
    """Allows assigned counselor or manager/admin to quickly switch lead stage/status directly from detail page."""
    lead = _get_lead_or_redirect(request, pk)
    if not lead:
        return redirect("leads:lead_list")
    
    can_edit = _can_edit_lead(request.user, lead)
    if not can_edit:
        messages.error(request, "Only the assigned counselor or manager can change this lead's stage and status.")
        return redirect("leads:lead_detail", pk=pk)

    if request.method == "POST":
        new_stage_id = request.POST.get("stage")
        new_temp = request.POST.get("temperature")
        new_admission_status = request.POST.get("admission_status")
        new_deal_status = request.POST.get("deal_status")

        if new_stage_id and new_stage_id.isdigit():
            stg = LeadStage.objects.filter(id=int(new_stage_id), is_active=True).first()
            if stg:
                lead.stage = stg

        if new_temp in LeadTemperature.values:
            lead.temperature = new_temp

        if new_admission_status in AdmissionStatus.values:
            lead.admission_status = new_admission_status

        if new_deal_status in DealStatus.values:
            lead.deal_status = new_deal_status

        lead.save()
        messages.success(request, f"Lead stage and status updated successfully!")

    return redirect("leads:lead_detail", pk=pk)


@login_required
def bulk_action(request):
    is_ajax = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or request.headers.get("accept") == "application/json"
        or "application/json" in request.headers.get("accept", "")
        or request.content_type == "application/json"
        or request.POST.get("format") == "json"
    )

    def respond(status_type, message_text, extra_data=None):
        if is_ajax:
            resp = {"status": status_type, "message": message_text}
            if extra_data:
                resp.update(extra_data)
            return JsonResponse(resp, status=200 if status_type == "success" else 400)
        if status_type == "success":
            messages.success(request, message_text)
        elif status_type == "warning":
            messages.warning(request, message_text)
        elif status_type == "info":
            messages.info(request, message_text)
        else:
            messages.error(request, message_text)
        return redirect("leads:lead_list")

    if request.method != "POST":
        return respond("error", "Invalid request method.")

    # Support JSON payload or Form data
    ids = request.POST.getlist("selected")
    action = request.POST.get("bulk_action")
    if not ids and request.content_type == "application/json":
        try:
            import json
            body_data = json.loads(request.body.decode("utf-8") or "{}")
            ids = body_data.get("selected") or body_data.get("lead_ids") or []
            action = body_data.get("bulk_action") or action
        except Exception:
            pass

    leads = Lead.objects.filter(pk__in=ids)
    if not leads.exists():
        return respond("warning", "No leads selected.")

    if action == "self_assign":
        if request.user.role in [User.Role.SUPER_ADMIN, User.Role.ADMIN] or not getattr(request.user, "can_self_assign", True):
            return respond("error", "Admins and Superadmins cannot self-assign leads. Please assign leads to team members.")

        max_limit = getattr(request.user, "bulk_self_assign_limit", 25)
        selected_count = leads.count()
        if selected_count > max_limit:
            return respond(
                "error",
                f"Bulk Self-Assign Limit exceeded! Your maximum allowed limit is {max_limit} leads at a time, but you selected {selected_count} leads."
            )
        
        # Determine candidate leads (unassigned or already assigned to self or accessible)
        # Non-admin users cannot take away leads already assigned to someone else
        eligible_leads = leads
        is_admin_user = request.user.role in [User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER] or request.user.can_assign_leads
        if not is_admin_user:
            already_assigned_other = leads.filter(assigned_to__isnull=False).exclude(assigned_to=request.user)
            if already_assigned_other.exists():
                warning_msg = f"{already_assigned_other.count()} lead(s) are already assigned to other team members and were skipped."
                if not is_ajax:
                    messages.warning(request, warning_msg)
            eligible_leads = leads.filter(Q(assigned_to__isnull=True) | Q(assigned_to=request.user))
        
        assigned_stage = LeadStage.objects.filter(name__iexact='Assigned').first() or LeadStage.objects.filter(name__iexact='Contacted').first()
        updated_count = 0
        for lead in eligible_leads:
            lead.assigned_to = request.user
            if assigned_stage and (not lead.stage or lead.stage.name.lower() in ['new', 'fresh', 'uncontacted']):
                lead.stage = assigned_stage
            lead.save(update_fields=["assigned_to", "stage", "updated_at"])
            Activity.objects.create(
                lead=lead,
                created_by=request.user,
                activity_type="ASSIGNMENT",
                description=f"Bulk self-assigned by {request.user.get_full_name() or request.user.username}.",
            )
            updated_count += 1
            
        if updated_count > 0:
            return respond("success", f"🎉 Successfully self-assigned {updated_count} lead(s) to yourself!", {"updated_count": updated_count})
        else:
            return respond("info", "No eligible unassigned leads to assign.")

    elif action == "assign":
        is_admin_user = request.user.role in [User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER] or request.user.can_assign_leads
        if not is_admin_user:
            return respond("error", "You don't have permission to assign leads to other team members.")
        
        emp_id = request.POST.get("assign_to")
        from_user = request.POST.get("from_user", "").strip()
        
        eligible_leads = leads
        if from_user:
            if from_user == "unassigned":
                eligible_leads = leads.filter(assigned_to__isnull=True)
            elif from_user.isdigit():
                eligible_leads = leads.filter(assigned_to_id=int(from_user))
                
        assignee = User.objects.filter(pk=emp_id, is_active=True).first() if emp_id else None
        assignee_name = assignee.get_full_name() or assignee.username if assignee else "Unassigned"
        
        updated_count = 0
        assigned_stage = LeadStage.objects.filter(name__iexact='Assigned').first() or LeadStage.objects.filter(name__iexact='Contacted').first()
        for lead in eligible_leads:
            prev_user = lead.assigned_to
            lead.assigned_to = assignee
            if assignee and assigned_stage and (not lead.stage or lead.stage.name.lower() in ['new', 'fresh', 'uncontacted']):
                lead.stage = assigned_stage
            lead.save(update_fields=["assigned_to", "stage", "updated_at"])
            Activity.objects.create(
                lead=lead,
                created_by=request.user,
                activity_type="ASSIGNMENT",
                description=f"Bulk assigned to '{assignee_name}' (was '{prev_user}') by {request.user.get_full_name() or request.user.username}.",
            )
            updated_count += 1

        # Send notification to assignee if assigned by someone else
        if assignee and assignee != request.user and updated_count > 0:
            from notifications.models import Notification
            assigner_name = request.user.get_full_name() or request.user.username
            Notification.objects.create(
                user=assignee,
                title=f"{updated_count} New Leads Assigned",
                message=f"{assigner_name} has assigned {updated_count} lead(s) to you.",
                link="/leads/my-leads/",
            )
            
        return respond("success", f"🎉 {updated_count} lead(s) successfully assigned to {assignee_name}.", {"updated_count": updated_count})

    elif action == "stage":
        stage_id = request.POST.get("stage")
        from_stage = request.POST.get("from_stage", "").strip()
        
        eligible_leads = leads
        if from_stage:
            if from_stage.isdigit():
                eligible_leads = leads.filter(stage_id=int(from_stage))
            else:
                eligible_leads = leads.filter(Q(stage__name__iexact=from_stage) | Q(deal_status__iexact=from_stage))
        
        target_stage = LeadStage.objects.filter(pk=stage_id).first() if stage_id and str(stage_id).isdigit() else None
        target_stage_name = target_stage.name if target_stage else (stage_id or "Updated")
        
        updated_count = 0
        for lead in eligible_leads:
            prev_stage = str(lead.stage or lead.deal_status)
            if target_stage:
                lead.stage = target_stage
                lead.save(update_fields=["stage", "updated_at"])
            else:
                # Custom deal status / stage name
                lead.deal_status = stage_id
                if not lead.custom_data:
                    lead.custom_data = {}
                lead.custom_data["deal_status"] = stage_id
                lead.save(update_fields=["deal_status", "custom_data", "updated_at"])
            Activity.objects.create(
                lead=lead,
                created_by=request.user,
                activity_type="STAGE_CHANGE",
                description=f"Bulk stage changed from '{prev_stage}' to '{target_stage_name}' by {request.user.get_full_name() or request.user.username}.",
            )
            updated_count += 1
            
        messages.success(request, f"🎉 {updated_count} lead(s) updated to stage '{target_stage_name}'.")
        return redirect("leads:lead_list")

    elif action == "archive":
        if not _can_archive_lead(request.user):
            messages.error(request, "Only Hospital Admins and Zappcode Super Admins can archive leads.")
            return redirect("leads:lead_list")
        leads.update(is_archived=True)
        messages.success(request, f"{leads.count()} lead(s) archived.")
    return redirect("leads:lead_list")


@login_required
def archived_leads(request):
    """
    Dedicated view for listing archived patient leads with search, pagination, and restore capabilities.
    Scoped to current hospital tenant or global view for SuperAdmin.
    """
    if not _can_archive_lead(request.user):
        messages.error(request, "You do not have permission to view archived leads.")
        return redirect("leads:lead_list")

    is_global_admin = request.user.is_superuser or (
        request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital
    )

    leads_qs = Lead.objects.filter(is_archived=True).select_related(
        "hospital", "assigned_to", "stage", "campaign"
    ).order_by("-updated_at")

    if request.user.hospital:
        leads_qs = leads_qs.filter(hospital=request.user.hospital)
        current_hospital = request.user.hospital
    else:
        current_hospital = None

    q = request.GET.get("q", "").strip()
    if q:
        leads_qs = leads_qs.filter(
            Q(name__icontains=q)
            | Q(mobile__icontains=q)
            | Q(email__icontains=q)
            | Q(lead_code__icontains=q)
        )

    total_archived = leads_qs.count()
    paginator = Paginator(leads_qs, 25)
    page_num = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_num)

    context = {
        "active": "archived_leads",
        "leads": page_obj,
        "total_archived": total_archived,
        "q_archived": q,
        "current_hospital": current_hospital,
        "is_global_admin": is_global_admin,
    }
    return render(request, "leads/archived_leads.html", context)


@login_required
def duplicates(request):
    leads_qs = Lead.objects.select_related("stage", "assigned_to", "lead_source").filter(is_archived=False)
    if request.user.hospital:
        leads_qs = leads_qs.filter(hospital=request.user.hospital)
    elif not (request.user.is_superuser or request.user.role == User.Role.SUPER_ADMIN):
        leads_qs = leads_qs.none()
    all_leads = list(leads_qs)
    groups = defaultdict(list)
    for l in all_leads:
        digits = Lead.clean_mobile(l.mobile)
        if digits:
            groups[digits].append(l)
    dup_groups = [g for g in groups.values() if len(g) > 1]
    dup_groups.sort(key=lambda g: -len(g))
    return render(request, "leads/duplicates.html", {"active": "leads_dup", "dup_groups": dup_groups})


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def masters(request):
    if request.method == "POST":
        kind = request.POST.get("kind")
        form_cls = {
            "source_category": SourceCategoryForm, "lead_source": LeadSourceForm,
            "campaign": CampaignForm, "stage": LeadStageForm,
        }.get(kind)
        if form_cls:
            form = form_cls(request.POST)
            if form.is_valid():
                form.save()
                messages.success(request, "Saved.")
            else:
                messages.error(request, f"Could not save: {form.errors.as_text()}")
        return redirect("leads:masters")

    return render(request, "leads/masters.html", {
        "active": "settings",
        "source_categories": SourceCategory.objects.all(),
        "lead_sources": LeadSource.objects.select_related("category").all(),
        "campaigns": Campaign.objects.all(),
        "stages": LeadStage.objects.all(),
        "sc_form": SourceCategoryForm(), "ls_form": LeadSourceForm(),
        "camp_form": CampaignForm(), "stage_form": LeadStageForm(),
    })


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def course_master(request):
    user_hospital = request.user.hospital
    active_biz_id = request.GET.get("business", "").strip() or str(request.session.get("active_business_id", "")).strip()
    show_archived = request.GET.get("archived") == "1"

    target_biz = None
    if user_hospital:
        target_biz = user_hospital
    elif active_biz_id and active_biz_id.isdigit():
        target_biz = Hospital.objects.filter(id=int(active_biz_id), is_active=True).first()

    if request.method == "POST":
        form = CourseForm(request.POST)
        if form.is_valid():
            new_course = form.save(commit=False)
            if not new_course.hospital and target_biz:
                new_course.hospital = target_biz
            new_course.save()
            messages.success(request, f"New course '{new_course.name}' added successfully.")
            return redirect("leads:course_master")
        else:
            messages.error(request, f"Could not add course: {form.errors.as_text()}")
        return redirect("leads:course_master")

    courses = Course.objects.all().order_by("name")
    
    # Filter by business if tenant user or business filter selected
    if target_biz:
        courses = courses.filter(models.Q(hospital=target_biz) | models.Q(hospital__isnull=True))
    
    # Filter by archived status
    if show_archived:
        courses = courses.filter(is_archived=True)
    else:
        courses = courses.filter(is_archived=False)

    all_businesses = Hospital.objects.filter(is_active=True).order_by("name")
    form = CourseForm()
    if target_biz:
        form.fields["hospital"].initial = target_biz.id

    return render(request, "leads/course_master.html", {
        "active": "course_master",
        "courses": courses,
        "form": form,
        "target_biz": target_biz,
        "all_businesses": all_businesses,
        "show_archived": show_archived,
        "archived_count": Course.objects.filter(is_archived=True).count(),
        "active_count": Course.objects.filter(is_archived=False).count(),
    })


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def course_archive(request, pk):
    course = get_object_or_404(Course, pk=pk)
    course.is_archived = not course.is_archived
    course.save(update_fields=["is_archived"])
    action_text = "archived" if course.is_archived else "restored"
    messages.success(request, f"Course '{course.name}' {action_text} successfully.")
    return redirect("leads:course_master")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def master_toggle(request, kind, pk):
    model = {
        "source_category": SourceCategory, "lead_source": LeadSource,
        "campaign": Campaign, "course": Course, "stage": LeadStage,
    }.get(kind)
    if model:
        obj = get_object_or_404(model, pk=pk)
        obj.is_active = not obj.is_active
        obj.save(update_fields=["is_active"])
    if kind == "course":
        return redirect("leads:course_master")
    return redirect("leads:masters")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def course_edit(request, pk):
    course = get_object_or_404(Course, pk=pk)
    if request.method == "POST":
        form = CourseForm(request.POST, instance=course)
        if form.is_valid():
            form.save()
            messages.success(request, f"Course '{course.name}' updated successfully.")
            return redirect("leads:course_master")
        else:
            messages.error(request, f"Could not update: {form.errors.as_text()}")
    else:
        form = CourseForm(instance=course)
    return render(request, "leads/course_edit.html", {
        "form": form, "course": course, "active": "course_master"
    })


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def course_excel_import(request):
    """
    Import Courses from Excel / CSV:
    If a course already exists (by name, case-insensitive), update its fields:
    base_price, max_discount, tutor, batch, batch_time, is_active.
    If it doesn't exist, create it.
    """
    if request.method == "POST" and request.FILES.get("course_excel"):
        import openpyxl
        import io
        excel_file = request.FILES["course_excel"]
        hospital = request.user.hospital

        try:
            filename = excel_file.name.lower()
            wb = openpyxl.load_workbook(excel_file, data_only=True)
            sheet = wb.active

            rows = list(sheet.iter_rows(values_only=True))
            if not rows:
                messages.error(request, "The uploaded Excel file is empty.")
                return redirect("leads:course_master")

            header = [str(col).strip().lower() if col is not None else "" for col in rows[0]]

            # Helper to find column index by matching aliases
            def find_col(aliases):
                for idx, col_name in enumerate(header):
                    clean = col_name.replace("_", " ").replace("-", " ")
                    for alias in aliases:
                        if alias.lower() in clean:
                            return idx
                return None

            idx_name = find_col(["course name", "course", "name", "subject", "title"])
            idx_price = find_col(["base price", "price", "fee", "fees", "cost", "total fee"])
            idx_discount = find_col(["max allowed discount", "max discount", "discount", "allowed discount"])
            idx_tutor = find_col(["tuitor", "tutor", "trainer", "faculty", "instructor", "teacher"])
            idx_batch = find_col(["batch name", "batch code", "batch"])
            idx_time = find_col(["batch time", "timing", "batch timing", "time", "schedule"])
            idx_active = find_col(["status", "is active", "active"])

            if idx_name is None:
                messages.error(request, "Could not find a 'Course Name' column in the uploaded Excel file.")
                return redirect("leads:course_master")

            created_count = 0
            updated_count = 0

            for row in rows[1:]:
                if not row or not any(row):
                    continue

                raw_name = str(row[idx_name]).strip() if idx_name < len(row) and row[idx_name] is not None else ""
                if not raw_name or raw_name.lower() in ("none", "nan", ""):
                    continue

                # Parse price
                base_price = 0
                if idx_price is not None and idx_price < len(row) and row[idx_price] is not None:
                    try:
                        base_price = int(float(str(row[idx_price]).replace("₹", "").replace(",", "").strip()))
                    except (ValueError, TypeError):
                        base_price = 0

                # Parse max discount
                max_discount = 0
                if idx_discount is not None and idx_discount < len(row) and row[idx_discount] is not None:
                    try:
                        max_discount = int(float(str(row[idx_discount]).replace("₹", "").replace(",", "").strip()))
                    except (ValueError, TypeError):
                        max_discount = 0

                # Tutor
                tutor = ""
                if idx_tutor is not None and idx_tutor < len(row) and row[idx_tutor] is not None:
                    tutor = str(row[idx_tutor]).strip()
                    if tutor.lower() in ("none", "nan"):
                        tutor = ""

                # Batch
                batch = ""
                if idx_batch is not None and idx_batch < len(row) and row[idx_batch] is not None:
                    batch = str(row[idx_batch]).strip()
                    if batch.lower() in ("none", "nan"):
                        batch = ""

                # Batch Time
                batch_time = ""
                if idx_time is not None and idx_time < len(row) and row[idx_time] is not None:
                    batch_time = str(row[idx_time]).strip()
                    if batch_time.lower() in ("none", "nan"):
                        batch_time = ""

                # Active status
                is_active = True
                if idx_active is not None and idx_active < len(row) and row[idx_active] is not None:
                    val_str = str(row[idx_active]).strip().lower()
                    if val_str in ("0", "false", "no", "inactive", "disabled"):
                        is_active = False

                # Query existing course
                course_qs = Course.objects.filter(name__iexact=raw_name)
                if hospital:
                    course_obj = course_qs.filter(hospital=hospital).first() or course_qs.first()
                else:
                    course_obj = course_qs.first()

                if course_obj:
                    # Update existing course with new information
                    course_obj.base_price = base_price
                    course_obj.max_discount = max_discount
                    if tutor:
                        course_obj.tutor = tutor
                    if batch:
                        course_obj.batch = batch
                    if batch_time:
                        course_obj.batch_time = batch_time
                    course_obj.is_active = is_active
                    course_obj.save()
                    updated_count += 1
                else:
                    # Create new course
                    Course.objects.create(
                        hospital=hospital,
                        name=raw_name,
                        base_price=base_price,
                        max_discount=max_discount,
                        tutor=tutor,
                        batch=batch,
                        batch_time=batch_time,
                        is_active=is_active,
                    )
                    created_count += 1

            messages.success(
                request,
                f"Course Excel Import completed: {created_count} new course(s) created, {updated_count} course(s) updated."
            )
        except Exception as e:
            messages.error(request, f"Failed to import courses from Excel: {str(e)}")

    return redirect("leads:course_master")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def course_sample_download(request):
    """Generate and return sample Excel file template for course import."""
    import openpyxl
    from django.http import HttpResponse

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Courses"

    headers = ["Course Name", "Base Price", "Max Allowed Discount", "Tutor", "Batch Name", "Batch Time", "Status"]
    ws.append(headers)

    sample_rows = [
        ["Full Stack Python Developer", 65000, 15000, "Rahul Sharma", "Python Morning Batch A", "10:00 AM - 12:00 PM", "Active"],
        ["Data Science & Machine Learning", 75000, 20000, "Pooja Verma", "Weekend DS Batch", "02:00 PM - 05:00 PM", "Active"],
        ["Data Analytics Master", 45000, 12000, "Amit Patel", "Batch DA-2", "04:00 PM - 06:00 PM", "Active"],
        ["Digital Marketing & SEO", 35000, 8000, "Sneha Joshi", "Fastrack Batch", "11:00 AM - 01:00 PM", "Active"],
    ]
    for row in sample_rows:
        ws.append(row)

    # Style header row
    for col in ws.iter_cols(min_row=1, max_row=1):
        for cell in col:
            cell.font = openpyxl.styles.Font(bold=True, color="FFFFFF")
            cell.fill = openpyxl.styles.PatternFill(start_color="4F46E5", end_color="4F46E5", fill_type="solid")

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = 'attachment; filename="Course_Master_Sample.xlsx"'
    wb.save(response)
    return response


# ---------------------------------------------------------------------------
# Universal Master Management (Master & Sub-Master System)
# ---------------------------------------------------------------------------

def _get_business_for_custom_fields(request):
    """Resolve target hospital for Custom Lead Form view & operations."""
    user = request.user
    if user.hospital:
        return user.hospital
    # For Super Admin: check GET parameter, session, or default
    biz_val = (
        request.GET.get("business", "").strip()
        or request.POST.get("business", "").strip()
        or str(request.session.get("active_business_id", "")).strip()
    )
    if biz_val.lower() == "default":
        return "default"
    if biz_val and biz_val.isdigit():
        target = Hospital.objects.filter(id=int(biz_val), is_active=True).first()
        if target:
            return target
    # Default to Nelson Hospital or first active hospital
    return Hospital.objects.filter(is_active=True).order_by("id").first()


def _ensure_business_core_fields(h):
    """Initializes core lead form fields for a business if not yet initialized."""
    from leads.models import LeadCustomField, HospitalDisease, HospitalBranch, Course, LeadStage
    
    # ── Universal Default Lead Form Fields (for new businesses / system template) ──
    default_lead_form_fields = [
        ('lead_id', 'Lead ID', 'TEXT', 1, False, True, 'Auto-generated ID (System)', '', True),
        ('name', 'Full Name', 'TEXT', 2, True, True, 'Enter full name', '', True),
        ('mobile', 'Phone Number', 'TEXT', 3, True, True, 'Phone with country code (e.g. +91 9876543210)', '', True),
        ('email', 'Email Address', 'TEXT', 4, False, True, 'e.g. client@example.com', '', True),
        ('industry_category', 'Industry / Category', 'DROPDOWN', 5, False, True, 'Select Industry', 'Healthcare, Academy, Travel, E-commerce, Other', True),
        ('lead_source', 'Lead Source', 'DROPDOWN', 6, False, True, 'Select Lead Source', 'Website, Social Media, Referral, Ad Campaign, Walk-in, Call, Event, Partner', True),
        ('stage', 'Lead Status / Pipeline Stage', 'DROPDOWN', 7, True, True, 'Select Status', 'New, Contacted, Qualified, Proposal Sent, Negotiation, Converted, Lost', True),
        ('assigned_to', 'Lead Owner / Assigned To', 'DROPDOWN', 8, False, True, 'Select Owner / Counselor', 'Dynamic user list', True),
        ('lead_score', 'Lead Score', 'NUMBER', 9, False, True, 'e.g. 85', '', True),
        ('city', 'City', 'TEXT', 10, False, True, 'Enter city name', '', True),
        ('country', 'Country', 'DROPDOWN', 11, False, True, 'Select Country', 'India, United States, United Kingdom, Canada, Australia, UAE, Other', True),
        ('preferred_contact_method', 'Preferred Contact Method', 'DROPDOWN', 12, False, True, 'Select Contact Method', 'Call, Email, WhatsApp, SMS', True),
        ('preferred_language', 'Preferred Language', 'DROPDOWN', 13, False, True, 'Select Language', 'English, Hindi, Marathi, Gujarati, Spanish, French, Other', True),
        ('budget_range', 'Budget Range', 'DROPDOWN', 14, False, True, 'Select Budget', 'Under ₹25,000, ₹25,000 - ₹50,000, ₹50,000 - ₹1,00,000, ₹1,00,000 - ₹2,50,000, Above ₹2,50,000', True),
        ('notes', 'Notes / Remarks', 'TEXTAREA', 15, False, True, 'Free text notes, conversation summary, or remarks...', '', True),
        ('tags', 'Tags', 'TEXT', 16, False, True, 'Tags for segmentation (comma-separated)', '', True),
        ('created_at_date', 'Created Date', 'DATE', 17, False, True, 'System date (auto)', '', True),
        ('last_contacted_date', 'Last Contacted Date', 'DATE', 18, False, True, 'Date of last interaction', '', True),
        ('next_followup_date', 'Next Follow-up Date', 'DATE', 19, False, True, 'Next scheduled follow-up date', '', True),
        ('utm_source_campaign', 'UTM Source / Campaign', 'TEXT', 20, False, True, 'Marketing attribution tracking', '', True),
        ('marketing_consent', 'Marketing Consent (Opt-in)', 'CHECKBOX', 21, False, True, 'Compliance opt-in confirmed', '', True),
    ]

    # If initializing the default template (hospital is None / "default")
    if h is None or h == "default":
        for f_name, f_lbl, f_type, f_ord, f_req, f_act, f_ph, f_opt, f_sys in default_lead_form_fields:
            if not LeadCustomField.objects.filter(hospital__isnull=True, name=f_name).exists():
                LeadCustomField.objects.create(
                    hospital=None, name=f_name, label=f_lbl, field_type=f_type,
                    order=f_ord, is_required=f_req, is_active=f_act,
                    placeholder=f_ph, options=f_opt, is_system=f_sys
                )
        return

    is_academy = "academy" in h.name.lower() or "zappcode" in h.name.lower()
    is_nelson = "nelson" in h.name.lower() or "hospital" in h.name.lower()

    if is_academy:
        core_field_defs = [
            ('name', 'Student Name', 'TEXT', 1, True, True, 'Enter full student name', ''),
            ('mobile', 'Mobile Number', 'TEXT', 2, True, True, '10-digit mobile number', ''),
            ('alternate_mobile', 'Alternate Mobile', 'TEXT', 3, False, True, '10-digit alternate mobile', ''),
            ('email', 'Email Address', 'TEXT', 4, False, True, 'e.g. student@gmail.com', ''),
            ('state', 'State', 'DROPDOWN', 5, True, True, 'Select State', 'Maharashtra, Madhya Pradesh, Gujarat, Karnataka, Delhi, Other'),
            ('city', 'City', 'DROPDOWN', 6, True, True, 'Select City', 'Nagpur, Pune, Mumbai, Nashik, Aurangabad, Wardha, Amravati, Chandrapur, Bhandara, Gondia, Other'),
            ('location', 'Area / Location', 'TEXT', 7, False, True, 'Area or locality in city', ''),
            ('education', 'Education Category', 'DROPDOWN', 8, True, True, 'Select Education Category', 'Engineering, Medical, Management, Arts & Commerce, Polytechnic / Diploma, School Student, Other'),
            ('qualification', 'Qualification / Degree', 'DROPDOWN', 9, True, True, 'Select Qualification', 'B.Tech / B.E, BCA, MCA, B.Sc, M.Sc, Diploma, 12th Standard, Other'),
            ('graduation_year', 'Graduation / Passing Year', 'TEXT', 10, False, True, 'e.g. 2025, 2026', ''),
            ('course', 'Course Interested', 'DROPDOWN', 11, True, True, 'Select Course', 'Data Analytics, Data Science, Full Stack Python, Full Stack Java, Digital Marketing, AI & Machine Learning, Software Testing'),
            ('temperature', 'Lead Temperature', 'DROPDOWN', 12, False, True, 'Select Temperature', 'HOT, WARM, COLD'),
            ('stage', 'Lead Stage', 'DROPDOWN', 13, True, True, 'Select Stage', 'New, Contacted, Follow Up, Demo Attended, Interested, Admission Confirmed, Lost / Dropped'),
            ('deal_status', 'Deal Status', 'DROPDOWN', 14, False, True, 'Select Deal Status', 'OPEN, WON, LOST, ON_HOLD'),
            ('admission_status', 'Admission Status', 'DROPDOWN', 15, True, True, 'Select Admission Status', 'NOT_APPLIED, APPLIED, INTERESTED, ADMISSION_DONE, CANCELLED'),
            ('inquiry_date', 'Inquiry Date', 'DATE', 16, True, True, 'Select inquiry date', ''),
            ('lead_source', 'Lead Source', 'DROPDOWN', 17, False, True, 'Select Source', 'Meta Ads, Google Ads, Direct Walk-in, College Visit, JustDial, Website Form, Referral, Student Referral'),
            ('campaign', 'Campaign', 'DROPDOWN', 18, False, True, 'Select Campaign', 'ZA Meta Campaign 2026, Summer Batch Campaign, Python Masters, B2B Zappkode'),
        ]
    elif is_nelson:
        core_field_defs = [
            ('name', 'Patient Name', 'TEXT', 1, True, True, 'Enter full patient name', ''),
            ('mobile', 'Mobile Number', 'TEXT', 2, True, True, '10-digit mobile number', ''),
            ('age', 'Age', 'NUMBER', 3, False, True, 'e.g. 35', ''),
            ('gender', 'Gender', 'DROPDOWN', 4, False, True, 'Select Gender', 'Male, Female, Other'),
            ('comments', 'Comments / Notes', 'TEXTAREA', 5, False, True, 'Enter patient notes...', ''),
            ('location', 'Location', 'DROPDOWN', 6, False, True, 'Select Location', 'Nagpur, Wardha, Hinganghat, Chandrapur, Amravati, Bhandara, Yavatmal, Gondia'),
            ('doctor', 'Doctor', 'DROPDOWN', 7, False, True, 'Select Doctor', 'Dr. Pradeep Patil, Dr. Rahul Sharma, Dr. Priya Deshmukh, Dr. Amit Verma'),
            ('department', 'Department', 'DROPDOWN', 8, False, True, 'Select Department', 'Cardiology, Neurology, Orthopedics, Pediatrics, Oncology, Gynecology, General Medicine'),
            ('lead_source', 'Lead Source', 'DROPDOWN', 9, False, True, 'Select Lead Source', 'Google Ads, Facebook / Instagram, Walk-in, Doctor Referral, Website, Newspaper, Camp / Event'),
            ('appointment_status', 'Appointment Status', 'DROPDOWN', 10, False, True, 'Select Status', 'Interested, Booked, Visited, Follow-up Needed, Cancelled / Rescheduled, Not Interested'),
            ('campaign', 'Campaign', 'DROPDOWN', 11, False, True, 'Select Campaign', 'Summer Health Checkup, Cardiology Camp, Free OPD Camp, Digital Awareness 2026'),
            ('hospital_branch', 'Hospital Branch', 'DROPDOWN', 12, True, True, 'Select Branch', 'Dhantoli, Main Branch'),
            ('disease', 'Disease', 'DROPDOWN', 13, False, True, 'Select Disease', ''),
        ]
    else:
        # Any other / new business automatically inherits the Default Custom Lead Form!
        core_field_defs = [
            (f_name, f_lbl, f_type, f_ord, f_req, f_act, f_ph, f_opt)
            for f_name, f_lbl, f_type, f_ord, f_req, f_act, f_ph, f_opt, _ in default_lead_form_fields
        ]

    for f_name, f_lbl, f_type, f_ord, f_req, f_act, f_ph, f_opt in core_field_defs:
        if not LeadCustomField.objects.filter(hospital=h, name=f_name).exists():
            LeadCustomField.objects.create(
                hospital=h, name=f_name, label=f_lbl, field_type=f_type,
                order=f_ord, is_required=f_req, is_active=f_act,
                placeholder=f_ph, options=f_opt, is_system=True
            )

    # Sync dynamic choices from other tables if available
    if is_academy:
        cf_course = LeadCustomField.objects.filter(hospital=h, name="course").first()
        if cf_course:
            course_names = list(Course.objects.filter(is_active=True, hospital=h).values_list("name", flat=True)[:15]) or list(Course.objects.filter(is_active=True).values_list("name", flat=True)[:15])
            if course_names:
                cf_course.options = ", ".join(course_names)
                cf_course.save(update_fields=["options"])

        cf_camp = LeadCustomField.objects.filter(hospital=h, name="campaign").first()
        if cf_camp:
            academy_camps = list(Campaign.objects.filter(hospital=h, is_active=True).values_list("name", flat=True))
            if academy_camps:
                cf_camp.options = ", ".join(academy_camps)
                cf_camp.save(update_fields=["options"])

    elif is_nelson:
        cf_disease = LeadCustomField.objects.filter(hospital=h, name="disease").first()
        if cf_disease:
            dis_names = list(HospitalDisease.objects.filter(hospital=h, is_active=True).values_list("name", flat=True))
            if dis_names:
                cf_disease.options = ", ".join(dis_names)
                cf_disease.save(update_fields=["options"])

        cf_branch = LeadCustomField.objects.filter(hospital=h, name="hospital_branch").first()
        if cf_branch:
            branch_names = list(HospitalBranch.objects.filter(hospital=h, is_active=True).values_list("name", flat=True))
            if branch_names:
                cf_branch.options = ", ".join(branch_names)
                cf_branch.save(update_fields=["options"])

        cf_camp = LeadCustomField.objects.filter(hospital=h, name="campaign").first()
        if cf_camp:
            hosp_camps = list(Campaign.objects.filter(hospital=h, is_active=True).values_list("name", flat=True))
            if hosp_camps:
                cf_camp.options = ", ".join(hosp_camps)
                cf_camp.save(update_fields=["options"])


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def universal_master_list(request):
    from leads.models import LeadCustomField
    from accounts.models import Hospital

    is_global_admin = request.user.is_superuser or (
        request.user.role == User.Role.SUPER_ADMIN and not request.user.hospital
    )

    # Ensure default universal fields are seeded
    _ensure_business_core_fields("default")

    h = _get_business_for_custom_fields(request)
    if h != "default":
        _ensure_business_core_fields(h)

    available_businesses = list(Hospital.objects.filter(is_active=True).order_by("id")) if is_global_admin else []
    if request.user.hospital and not available_businesses:
        available_businesses = [request.user.hospital]

    if h == "default":
        all_fields_qs = LeadCustomField.objects.filter(hospital__isnull=True).order_by('order', 'id')
        current_hospital = None
        is_default_tab = True
    else:
        all_fields_qs = LeadCustomField.objects.filter(hospital=h).order_by('order', 'id') if h else LeadCustomField.objects.all().order_by('order', 'id')
        current_hospital = h
        is_default_tab = False

    # Fetch Positive & Negative remark Master Groups and Items
    from leads.models import MasterGroup, MasterItem
    pos_group, _ = MasterGroup.objects.get_or_create(
        name="Positive Remarks", 
        defaults={"description": "Positive call remarks & notes that shift lead temperature UP to Hot / Warm"}
    )
    neg_group, _ = MasterGroup.objects.get_or_create(
        name="Negative Remarks", 
        defaults={"description": "Negative call remarks & notes that shift lead temperature DOWN to Warm / Cold / Freeze"}
    )

    pos_items = MasterItem.objects.filter(group=pos_group, hospital=current_hospital).order_by("order", "name")
    neg_items = MasterItem.objects.filter(group=neg_group, hospital=current_hospital).order_by("order", "name")

    # Fetch Archived Leads based on selected business scope
    from leads.models import Lead
    from django.core.paginator import Paginator

    if is_default_tab:
        archived_leads_qs = Lead.objects.filter(hospital__isnull=True, is_archived=True)
        total_archived = Lead.objects.filter(is_archived=True).count()
    elif current_hospital:
        archived_leads_qs = Lead.objects.filter(hospital=current_hospital, is_archived=True)
        total_archived = Lead.objects.filter(hospital=current_hospital, is_archived=True).count()
    else:
        archived_leads_qs = Lead.objects.filter(is_archived=True)
        total_archived = Lead.objects.filter(is_archived=True).count()

    archived_leads_qs = archived_leads_qs.select_related('hospital', 'assigned_to', 'stage', 'campaign').order_by('-updated_at')

    q_archived = request.GET.get('q_archived', '').strip()
    if q_archived:
        archived_leads_qs = archived_leads_qs.filter(
            Q(name__icontains=q_archived) |
            Q(mobile__icontains=q_archived) |
            Q(email__icontains=q_archived) |
            Q(lead_code__icontains=q_archived)
        )

    archived_paginator = Paginator(archived_leads_qs, 25)
    archived_page_num = request.GET.get('archived_page', 1)
    archived_page_obj = archived_paginator.get_page(archived_page_num)

    # If business-specific has no items yet, fallback/copy defaults or show list
    active_main_tab = request.GET.get("tab", "fields")

    return render(request, "leads/universal_masters.html", {
        "active": "universal_masters",
        "active_main_tab": active_main_tab,
        "all_fields": all_fields_qs,
        "current_hospital": current_hospital,
        "is_default_tab": is_default_tab,
        "available_businesses": available_businesses,
        "is_global_admin": is_global_admin,
        "pos_group": pos_group,
        "neg_group": neg_group,
        "pos_items": pos_items,
        "neg_items": neg_items,
        "archived_leads": archived_page_obj,
        "archived_count": total_archived,
        "q_archived": q_archived,
    })


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def custom_field_add(request):
    from leads.models import LeadCustomField
    from django.utils.text import slugify
    from django.db.models import Max, F
    
    target_hospital = _get_business_for_custom_fields(request)
    target_hospital_obj = None if target_hospital == "default" else target_hospital

    if request.method == "POST":
        label = request.POST.get("label", "").strip()
        field_type = request.POST.get("field_type", "TEXT")
        options = request.POST.get("options", "").strip()
        placeholder = request.POST.get("placeholder", "").strip()
        help_text = request.POST.get("help_text", "").strip()
        is_required = request.POST.get("is_required") == "on"
        order_raw = request.POST.get("order", "").strip()
        biz_param = request.POST.get("business", "").strip() or ("default" if target_hospital == "default" else (str(target_hospital.id) if target_hospital else ""))

        qs = LeadCustomField.objects.filter(hospital=target_hospital_obj)

        try:
            order_val = int(order_raw) if order_raw else None
        except ValueError:
            order_val = None

        if order_val is None or order_val <= 0:
            max_order = qs.aggregate(m=Max('order'))['m'] or 0
            order = max_order + 1
        else:
            order = order_val
            qs.filter(order__gte=order).update(order=F('order') + 1)

        if label:
            name = slugify(label).replace("-", "_")
            base_name = name
            count = 1
            while LeadCustomField.objects.filter(hospital=target_hospital_obj, name=name).exists():
                name = f"{base_name}_{count}"
                count += 1

            LeadCustomField.objects.create(
                hospital=target_hospital_obj,
                name=name,
                label=label,
                field_type=field_type,
                options=options,
                placeholder=placeholder,
                help_text=help_text,
                is_required=is_required,
                order=order,
                is_active=True,
            )
            messages.success(request, f"New custom form field '{label}' added at position #{order} successfully.")
            redirect_url = f"/leads/universal-masters/?tab=custom_fields&business={biz_param}" if biz_param else "/leads/universal-masters/?tab=custom_fields"
            return redirect(redirect_url)
        messages.error(request, "Field label is required.")
    
    biz_param = request.GET.get("business", "").strip() or ("default" if target_hospital == "default" else (str(target_hospital.id) if target_hospital else ""))
    redirect_url = f"/leads/universal-masters/?tab=custom_fields&business={biz_param}" if biz_param else "/leads/universal-masters/?tab=custom_fields"
    return redirect(redirect_url)


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def custom_field_edit(request, pk):
    from leads.models import LeadCustomField
    field = get_object_or_404(LeadCustomField, pk=pk)
    
    is_superadmin = request.user.is_superuser or request.user.role == User.Role.SUPER_ADMIN
    if not is_superadmin and field.hospital != request.user.hospital:
        messages.error(request, "Permission denied.")
        return redirect("/leads/universal-masters/?tab=custom_fields")
        
    biz_param = request.POST.get("business", "").strip() or (str(field.hospital.id) if field.hospital else "default")

    if request.method == "POST":
        label = request.POST.get("label", "").strip()
        field_type = request.POST.get("field_type", "TEXT")
        options = request.POST.get("options", "").strip()
        placeholder = request.POST.get("placeholder", "").strip()
        help_text = request.POST.get("help_text", "").strip()
        is_required = request.POST.get("is_required") == "on"
        is_active = request.POST.get("is_active") == "on"
        order = request.POST.get("order", 0)
        try:
            order = int(order)
        except ValueError:
            order = 0

        if label:
            old_order = field.order
            field.label = label
            field.field_type = field_type
            field.options = options
            field.placeholder = placeholder
            field.help_text = help_text
            field.is_required = is_required
            field.is_active = is_active
            
            all_fields = list(LeadCustomField.objects.filter(hospital=field.hospital).order_by('order', 'id'))
            
            if order > 0 and order != old_order:
                all_fields = [f for f in all_fields if f.pk != field.pk]
                insert_idx = max(0, min(order - 1, len(all_fields)))
                all_fields.insert(insert_idx, field)
                
                for idx, f in enumerate(all_fields):
                    f.order = idx + 1
                    if f.pk == field.pk:
                        field.order = idx + 1
                        field.save()
                    else:
                        f.save(update_fields=['order'])
            else:
                field.save()

            FIELD_TO_GROUP_MAP = {
                "appointment_status": "Appointment Statuses",
                "lead_source": "Lead Sources",
                "campaign": "Campaigns",
                "location": "Locations",
                "gender": "Genders",
                "priority": "Priorities",
                "deal_status": "Deal Statuses",
            }
            if field.name in FIELD_TO_GROUP_MAP and field.field_type == LeadCustomField.FieldType.DROPDOWN:
                from leads.models import MasterGroup, MasterItem
                group_name = FIELD_TO_GROUP_MAP[field.name]
                mg, _ = MasterGroup.objects.get_or_create(name=group_name)
                new_opts = [o.strip() for o in options.split(",") if o.strip()]
                if new_opts:
                    mg.items.filter(hospital=field.hospital).exclude(name__in=new_opts).delete()
                    for idx, opt_name in enumerate(new_opts):
                        MasterItem.objects.update_or_create(
                            group=mg,
                            hospital=field.hospital,
                            name=opt_name,
                            defaults={"order": idx + 1, "is_active": True}
                        )
                
            messages.success(request, f"Form field '{label}' updated successfully.")
        redirect_url = f"/leads/universal-masters/?business={biz_param}" if biz_param else "/leads/universal-masters/"
        return redirect(redirect_url)
    redirect_url = f"/leads/universal-masters/?business={biz_param}" if biz_param else "/leads/universal-masters/"
    return redirect(redirect_url)


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def custom_field_toggle(request, pk):
    from leads.models import LeadCustomField
    field = get_object_or_404(LeadCustomField, pk=pk)
    
    is_superadmin = request.user.is_superuser or request.user.role == User.Role.SUPER_ADMIN
    if not is_superadmin and field.hospital != request.user.hospital:
        messages.error(request, "Permission denied.")
        return redirect("/leads/universal-masters/?tab=custom_fields")
        
    field.is_active = not field.is_active
    field.save(update_fields=["is_active"])
    messages.success(request, f"Field '{field.label}' is now {'Active' if field.is_active else 'Hidden'}.")
    biz_param = request.GET.get("business", "").strip() or (str(field.hospital.id) if field.hospital else "default")
    redirect_url = f"/leads/universal-masters/?tab=custom_fields&business={biz_param}" if biz_param else "/leads/universal-masters/?tab=custom_fields"
    return redirect(redirect_url)


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def custom_field_delete(request, pk):
    from leads.models import LeadCustomField
    field = get_object_or_404(LeadCustomField, pk=pk)
    
    is_superadmin = request.user.is_superuser or request.user.role == User.Role.SUPER_ADMIN
    if not is_superadmin and field.hospital != request.user.hospital:
        messages.error(request, "Permission denied.")
        return redirect("/leads/universal-masters/?tab=custom_fields")
        
    label = field.label
    biz_param = request.POST.get("business", "").strip() or (str(field.hospital.id) if field.hospital else "default")
    field.delete()
    messages.success(request, f"Custom form field '{label}' removed from lead form.")
    redirect_url = f"/leads/universal-masters/?tab=custom_fields&business={biz_param}" if biz_param else "/leads/universal-masters/?tab=custom_fields"
    return redirect(redirect_url)


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def master_group_add(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        if name:
            group, created = MasterGroup.objects.get_or_create(name=name, defaults={"description": description})
            if created:
                messages.success(request, f"Master Category '{name}' created successfully.")
            else:
                messages.warning(request, f"Master Category '{name}' already exists.")
            return redirect(f"/leads/universal-masters/?group_id={group.pk}")
        messages.error(request, "Category name is required.")
    return redirect("leads:universal_masters")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def master_group_edit(request, pk):
    group = get_object_or_404(MasterGroup, pk=pk)
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        is_active = request.POST.get("is_active") == "on"
        if name:
            group.name = name
            group.description = description
            group.is_active = is_active
            group.save()
            messages.success(request, f"Master Category '{group.name}' updated.")
        return redirect(f"/leads/universal-masters/?group_id={group.pk}")
    return redirect("leads:universal_masters")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def master_group_delete(request, pk):
    group = get_object_or_404(MasterGroup, pk=pk)
    if request.method == "POST":
        name = group.name
        group.delete()
        messages.success(request, f"Master Category '{name}' and its sub-items deleted.")
    return redirect("leads:universal_masters")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def master_item_add(request):
    if request.method == "POST":
        group_id = request.POST.get("group_id")
        group = get_object_or_404(MasterGroup, pk=group_id)
        name = request.POST.get("name", "").strip()
        code = request.POST.get("code", "").strip()
        order = request.POST.get("order", 0)
        biz_param = request.POST.get("business", "").strip()
        from_view = request.POST.get("from_view", "").strip()
        
        target_hospital = None
        is_superadmin = request.user.is_superuser or request.user.role == User.Role.SUPER_ADMIN
        if is_superadmin:
            if biz_param and biz_param != "default":
                from accounts.models import Hospital
                target_hospital = Hospital.objects.filter(id=biz_param).first()
        else:
            target_hospital = request.user.hospital

        try:
            order = int(order)
        except ValueError:
            order = 0

        if name:
            item, created = MasterItem.objects.get_or_create(
                group=group, name=name, hospital=target_hospital, 
                defaults={"code": code, "order": order, "is_active": True}
            )
            if created:
                messages.success(request, f"Remark keyword '{name}' added successfully.")
            else:
                messages.warning(request, f"Keyword '{name}' already exists in {group.name}.")
        else:
            messages.error(request, "Keyword / Remark text is required.")

        if from_view == "hospital_config":
            return redirect("/leads/hospital-configuration/?tab=temperature")
        
        redirect_url = f"/leads/universal-masters/?tab=temperature&business={biz_param}" if biz_param else "/leads/universal-masters/?tab=temperature"
        return redirect(redirect_url)
    return redirect("leads:universal_masters")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def master_item_edit(request, pk):
    item = get_object_or_404(MasterItem, pk=pk)
    is_superadmin = request.user.is_superuser or request.user.role == User.Role.SUPER_ADMIN
    if not is_superadmin and item.hospital != request.user.hospital:
        messages.error(request, "You do not have permission to edit this item.")
        return redirect("leads:universal_masters")
        
    biz_param = request.POST.get("business", "").strip()
    from_view = request.POST.get("from_view", "").strip()

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        code = request.POST.get("code", "").strip()
        order = request.POST.get("order", 0)
        is_active = request.POST.get("is_active") == "on"
        try:
            order = int(order)
        except ValueError:
            order = 0

        if name:
            item.name = name
            item.code = code
            item.order = order
            item.is_active = is_active
            item.save()
            messages.success(request, f"Remark keyword '{item.name}' updated.")

        if from_view == "hospital_config":
            return redirect("/leads/hospital-configuration/?tab=temperature")
        
        redirect_url = f"/leads/universal-masters/?tab=temperature&business={biz_param}" if biz_param else "/leads/universal-masters/?tab=temperature"
        return redirect(redirect_url)
    return redirect("leads:universal_masters")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def master_item_toggle(request, pk):
    item = get_object_or_404(MasterItem, pk=pk)
    is_superadmin = request.user.is_superuser or request.user.role == User.Role.SUPER_ADMIN
    if not is_superadmin and item.hospital != request.user.hospital:
        messages.error(request, "You do not have permission to modify this item.")
        return redirect("leads:universal_masters")
        
    biz_param = request.GET.get("business", "").strip()
    from_view = request.GET.get("from_view", "").strip()

    item.is_active = not item.is_active
    item.save(update_fields=["is_active"])
    messages.success(request, f"Status for '{item.name}' changed to {'Active' if item.is_active else 'Inactive'}.")
    
    if from_view == "hospital_config":
        return redirect("/leads/hospital-configuration/?tab=temperature")
    redirect_url = f"/leads/universal-masters/?tab=temperature&business={biz_param}" if biz_param else "/leads/universal-masters/?tab=temperature"
    return redirect(redirect_url)


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def master_item_delete(request, pk):
    item = get_object_or_404(MasterItem, pk=pk)
    is_superadmin = request.user.is_superuser or request.user.role == User.Role.SUPER_ADMIN
    if not is_superadmin and item.hospital != request.user.hospital:
        messages.error(request, "You do not have permission to delete this item.")
        return redirect("leads:universal_masters")
        
    biz_param = request.POST.get("business", "").strip() or request.GET.get("business", "").strip()
    from_view = request.POST.get("from_view", "").strip() or request.GET.get("from_view", "").strip()

    name = item.name
    item.delete()
    messages.success(request, f"Remark keyword '{name}' deleted.")
    
    if from_view == "hospital_config":
        return redirect("/leads/hospital-configuration/?tab=temperature")
    redirect_url = f"/leads/universal-masters/?tab=temperature&business={biz_param}" if biz_param else "/leads/universal-masters/?tab=temperature"
    return redirect(redirect_url)


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def universal_master_import(request):
    if request.method == "POST" and request.FILES.get("import_file"):
        import pandas as pd
        excel_file = request.FILES["import_file"]
        
        try:
            if excel_file.name.endswith('.csv'):
                df = pd.read_csv(excel_file)
            else:
                df = pd.read_excel(excel_file)
                
            items_created = 0
            
            for column in df.columns:
                group_name = str(column).strip()
                if not group_name or group_name.lower() == 'unnamed':
                    continue
                    
                group, _ = MasterGroup.objects.get_or_create(name=group_name)
                
                for value in df[column].dropna():
                    item_name = str(value).strip()
                    if item_name:
                        item, created = MasterItem.objects.get_or_create(
                            group=group, 
                            name=item_name, 
                            hospital=request.user.hospital,
                            defaults={"is_active": True}
                        )
                        if created:
                            items_created += 1
                            
            messages.success(request, f"Successfully imported {items_created} items from file.")
        except Exception as e:
            messages.error(request, f"Error processing file: {str(e)}")
            
    return redirect("leads:universal_masters")




@login_required
def book_appointment(request, pk):
    from django.core.exceptions import PermissionDenied
    from django.contrib import messages
    from leads.models import Lead, Appointment, AppointmentStatus
    
    lead = get_object_or_404(Lead, pk=pk)
    
    if not _can_access_lead(request.user, lead):
        raise PermissionDenied("You do not have access to this lead.")
        
    if request.method == "POST":
        doctor_name = request.POST.get('doctor_name')
        appointment_date = request.POST.get('appointment_date')
        appointment_time = request.POST.get('appointment_time') or None
        notes = request.POST.get('notes', '')
        
        if doctor_name and appointment_date:
            Appointment.objects.create(
                lead=lead,
                hospital=request.user.hospital,
                doctor_name=doctor_name,
                appointment_date=appointment_date,
                appointment_time=appointment_time,
                notes=notes,
                created_by=request.user
            )
            messages.success(request, "Appointment booked successfully.")
        else:
            messages.error(request, "Doctor name and date are required.")
            
    return redirect('leads:lead_detail', pk=pk)

@login_required
def check_duplicate_mobile(request):
    from django.http import JsonResponse
    raw_mobile = request.GET.get("mobile", "").strip()
    digits = Lead.clean_mobile(raw_mobile)
    if not digits or len(digits) < 8:
        return JsonResponse({"exists": False})
    
    existing = Lead.objects.filter(is_archived=False)
    for lead in existing.only("id", "lead_code", "name", "mobile", "assigned_to"):
        if Lead.clean_mobile(lead.mobile) == digits:
            assigned_name = lead.assigned_to.get_full_name() if lead.assigned_to else (lead.assigned_to.username if lead.assigned_to else "Unassigned")
            return JsonResponse({
                "exists": True,
                "lead_id": lead.pk,
                "lead_code": lead.lead_code,
                "name": lead.name,
                "assigned_to": assigned_name
            })
    return JsonResponse({"exists": False})


@login_required
def check_duplicate_uhid(request):
    from django.http import JsonResponse
    from django.db.models import Q
    raw_uhid = request.GET.get("uhid", "").strip()
    exclude_lead_id = request.GET.get("exclude_id", "").strip()
    if not raw_uhid:
        return JsonResponse({"exists": False})

    qs = Lead.objects.filter(is_archived=False)
    if request.user.hospital:
        qs = qs.filter(hospital=request.user.hospital)

    if exclude_lead_id and exclude_lead_id.isdigit():
        qs = qs.exclude(pk=int(exclude_lead_id))

    dup = qs.filter(
        Q(custom_data__uhid_id_no__iexact=raw_uhid) |
        Q(custom_data__uhid_no__iexact=raw_uhid)
    ).first()

    if dup:
        assigned_name = dup.assigned_to.get_full_name() if dup.assigned_to else (dup.assigned_to.username if dup.assigned_to else "Unassigned")
        return JsonResponse({
            "exists": True,
            "lead_id": dup.pk,
            "lead_code": dup.lead_code,
            "name": dup.name,
            "assigned_to": assigned_name
        })

    return JsonResponse({"exists": False})


@login_required
def doctor_slots_api(request):
    import re
    from datetime import datetime, time, timedelta
    from django.http import JsonResponse
    from django.utils import timezone
    from django.db import models
    from django.db.models import Q
    from accounts.models import User
    from leads.models import Appointment, DoctorSchedule, DoctorLeave, AppointmentStatus
    
    try:
        doctor_name = request.GET.get("doctor", "").strip()
        date_str = request.GET.get("date", "").strip()
        
        if not doctor_name or not date_str:
            return JsonResponse({"error": "Doctor and date are required", "slots": []})
            
        try:
            req_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return JsonResponse({"error": "Invalid date format", "slots": []})
            
        hospital = request.user.hospital
        
        # Clean doctor name (remove prefixes like Dr. / Dr / Doctor)
        clean_doc_name = re.sub(r'^(dr\.?|doctor)\s+', '', doctor_name, flags=re.IGNORECASE).strip()
        
        # Try finding doctor user
        doctor_user = None
        user_qs = User.objects.filter(role=User.Role.DOCTOR)
        if hospital:
            user_qs = user_qs.filter(hospital=hospital)
            
        for u in user_qs:
            full_name = (u.get_full_name() or "").strip().lower()
            u_clean = re.sub(r'^(dr\.?|doctor)\s+', '', full_name, flags=re.IGNORECASE).strip()
            username = u.username.lower()
            search_low = clean_doc_name.lower()
            doc_raw_low = doctor_name.lower()
            
            if (search_low and (search_low in full_name or search_low in u_clean or search_low in username)) or \
               (doc_raw_low and (doc_raw_low in full_name or doc_raw_low in username)):
                doctor_user = u
                break
        
        # Helper to parse time object from string or time
        def parse_time_val(t_val, default_time):
            if isinstance(t_val, time):
                return t_val
            if isinstance(t_val, str):
                for fmt in ("%H:%M:%S", "%H:%M", "%I:%M %p", "%I:%M%p"):
                    try:
                        return datetime.strptime(t_val.strip(), fmt).time()
                    except ValueError:
                        pass
            return default_time

        # Check doctor OPD start, end and slot duration
        duration = 30
        is_doctor_active = True
        off_days = ["Sunday"]
        
        doctor_opd_start = time(9, 0)
        doctor_opd_end = time(18, 0)
        
        if doctor_user:
            sched = getattr(doctor_user, 'doctor_schedule', None)
            if sched:
                if sched.opd_start_time:
                    doctor_opd_start = parse_time_val(sched.opd_start_time, time(9, 0))
                if sched.opd_end_time:
                    doctor_opd_end = parse_time_val(sched.opd_end_time, time(18, 0))
                if sched.slot_duration_minutes and sched.slot_duration_minutes > 0:
                    duration = sched.slot_duration_minutes
                is_doctor_active = sched.is_available
                if sched.off_days:
                    off_days = [d.strip().lower() for d in sched.off_days.split(",") if d.strip()]

        # If start >= end, fallback to 9 AM to 6 PM
        if doctor_opd_start >= doctor_opd_end:
            doctor_opd_start = time(9, 0)
            doctor_opd_end = time(18, 0)
                    
        # Check day of week off
        day_name = req_date.strftime("%A").lower()
        is_weekly_off = (day_name in [d.lower() for d in off_days]) or (not is_doctor_active)
        
        # Check doctor leave
        leaves = DoctorLeave.objects.filter(
            start_date__lte=req_date,
            end_date__gte=req_date
        )
        if doctor_user:
            leaves = leaves.filter(doctor=doctor_user)
        elif hospital:
            leaves = leaves.filter(hospital=hospital)
            
        full_day_leave = leaves.filter(is_full_day=True).first()

        # If on full day leave or weekly off, return no slots with clear alert message
        if full_day_leave:
            return JsonResponse({
                "slots": [],
                "doctor": doctor_name,
                "date": date_str,
                "is_off": False,
                "is_on_leave": True,
                "leave_reason": full_day_leave.reason or "Personal Leave"
            })

        if is_weekly_off:
            return JsonResponse({
                "slots": [],
                "doctor": doctor_name,
                "date": date_str,
                "is_off": True,
                "is_on_leave": False,
                "off_day_name": req_date.strftime("%A")
            })
            
        # Get booked appointments for this doctor on this date
        booked_apts = Appointment.objects.filter(
            hospital=hospital,
            appointment_date=req_date
        ).exclude(
            status__in=[AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW]
        )
        if doctor_user:
            booked_apts = booked_apts.filter(models.Q(doctor_user=doctor_user) | models.Q(doctor_name__iexact=doctor_name))
        else:
            booked_apts = booked_apts.filter(doctor_name__iexact=doctor_name)
            
        booked_times = set()
        for apt in booked_apts:
            if apt.appointment_time:
                booked_times.add(apt.appointment_time.strftime("%H:%M"))
                
        # Generate slots based on doctor's OPD timings
        curr_dt = datetime.combine(req_date, doctor_opd_start)
        end_dt = datetime.combine(req_date, doctor_opd_end)
        now = timezone.localtime()
        
        slots = []
        while curr_dt < end_dt:
            time_str_24 = curr_dt.strftime("%H:%M")
            time_str_12 = curr_dt.strftime("%I:%M %p")
            slot_t = curr_dt.time()
            
            # Check if partial leave blocks this slot
            partial_leave = False
            for l in leaves.filter(is_full_day=False):
                if l.start_time and l.end_time:
                    if l.start_time <= slot_t < l.end_time:
                        partial_leave = True
                        break
                        
            is_booked = time_str_24 in booked_times
            is_past = (req_date == now.date() and slot_t < now.time()) or (req_date < now.date())
            
            if full_day_leave:
                status = "leave"
                status_text = "On Leave"
            elif is_weekly_off:
                status = "off"
                status_text = "Doctor Off"
            elif partial_leave:
                status = "leave"
                status_text = "On Leave"
            elif is_booked:
                status = "booked"
                status_text = "Booked"
            elif is_past:
                status = "past"
                status_text = "Past"
            else:
                status = "available"
                status_text = "Available"
                
            slots.append({
                "time_24": time_str_24,
                "time_12": time_str_12,
                "status": status,
                "status_text": status_text,
                "available": (status == "available")
            })
            curr_dt += timedelta(minutes=duration)
            
        return JsonResponse({
            "slots": slots,
            "doctor": doctor_name,
            "date": date_str,
            "duration": duration,
            "is_off": is_weekly_off,
            "is_on_leave": bool(full_day_leave),
            "leave_reason": full_day_leave.reason if full_day_leave else ""
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({"error": str(e), "slots": []}, status=200)

# ---------------------------------------------------------------------------
# Hospital Master Configuration Views & Cascading Relationships
# ---------------------------------------------------------------------------

@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_configuration_view(request):
    """
    Dedicated Hospital Configuration Master Settings module for Hospital Admin & Permitted Managers.
    Manages:
    - Hospital Branches
    - Hospital Departments (Linked to Branches)
    - Doctors (Linked to Department, Diseases & Branches with availability)
    - Diseases & Medical Conditions (Linked to Department)
    """
    hospital = request.user.hospital
    if not hospital:
        messages.error(request, "No hospital context found.")
        return redirect("dashboard:home")

    branches = HospitalBranch.objects.filter(hospital=hospital).prefetch_related("departments", "doctors")
    departments = HospitalDepartment.objects.filter(hospital=hospital).prefetch_related("branches", "doctors", "diseases")
    doctors = HospitalDoctor.objects.filter(hospital=hospital).select_related("department", "user").prefetch_related("branches", "associated_diseases", "availabilities")
    diseases = HospitalDisease.objects.filter(hospital=hospital).select_related("department")

    active_tab = request.GET.get("tab", "branches")

    # Doctor login users eligible for linking (strictly active DOCTOR role accounts in this hospital)
    doctor_users = User.objects.filter(
        hospital=hospital,
        role=User.Role.DOCTOR,
        is_active=True
    ).select_related("doctor_profile").order_by("first_name", "username")

    context = {
        "active": "hospital_config",
        "hospital": hospital,
        "branches": branches,
        "departments": departments,
        "doctors": doctors,
        "diseases": diseases,
        "active_tab": active_tab,
        "doctor_users": doctor_users,
    }
    return render(request, "leads/hospital_configuration.html", context)


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_profile_save(request):
    """Update Hospital / Organization Core Profile details."""
    hospital = request.user.hospital
    if not hospital:
        messages.error(request, "No hospital context found.")
        return redirect("dashboard:home")

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        contact_email = request.POST.get("contact_email", "").strip()
        phone = request.POST.get("phone", "").strip()
        registration_no = request.POST.get("registration_no", "").strip()
        address = request.POST.get("address", "").strip()

        if not name:
            messages.error(request, "Hospital / Organization name is required.")
            return redirect("/leads/hospital-configuration/?tab=profile")

        if phone:
            import re
            raw_digits = re.sub(r"\D", "", phone)
            # Accept valid 10-digit mobile (or prefixed with +91/0) or 10-11 digit landline
            if len(raw_digits) == 12 and raw_digits.startswith("91"):
                raw_digits = raw_digits[2:]
            elif len(raw_digits) == 11 and raw_digits.startswith("0"):
                raw_digits = raw_digits[1:]

            if len(raw_digits) < 10 or len(raw_digits) > 11:
                messages.error(request, "Please enter a valid contact phone number (10 digits for mobile or 10-11 digits with STD code).")
                return redirect("/leads/hospital-configuration/?tab=profile")
            phone = raw_digits

        hospital.name = name
        hospital.contact_email = contact_email
        hospital.phone = phone
        hospital.registration_no = registration_no
        hospital.address = address

        if "logo" in request.FILES:
            hospital.logo = request.FILES["logo"]

        hospital.save()
        messages.success(request, f"Hospital profile '{name}' updated successfully.")

    return redirect("/leads/hospital-configuration/?tab=profile")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_branch_save(request, pk=None):
    hospital = request.user.hospital
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        code = request.POST.get("code", "").strip()
        city = request.POST.get("city", "").strip()
        address = request.POST.get("address", "").strip()
        contact_number = request.POST.get("contact_number", "").strip()
        is_main = request.POST.get("is_main_branch") == "1"
        order = int(request.POST.get("order", 0) or 0)

        if not name:
            messages.error(request, "Branch name is required.")
            return redirect(f"/leads/hospital-configuration/?tab=branches")

        if contact_number:
            import re
            digits = re.sub(r"\D", "", contact_number)
            if len(digits) == 12 and digits.startswith("91"):
                digits = digits[2:]
            elif len(digits) == 11 and digits.startswith("0"):
                digits = digits[1:]
            if len(digits) != 10 or digits[0] not in '6789':
                messages.error(request, "Branch contact number must be a valid 10-digit number starting with 6, 7, 8, or 9.")
                return redirect("/leads/hospital-configuration/?tab=branches")
            contact_number = digits

        if is_main:
            # Only one main branch per hospital
            HospitalBranch.objects.filter(hospital=hospital).update(is_main_branch=False)

        if pk:
            branch = get_object_or_404(HospitalBranch, pk=pk, hospital=hospital)
            branch.name = name
            branch.code = code
            branch.city = city
            branch.address = address
            branch.contact_number = contact_number
            branch.is_main_branch = is_main
            branch.order = order
            branch.save()
            messages.success(request, f"Branch '{name}' updated successfully.")
        else:
            HospitalBranch.objects.create(
                hospital=hospital,
                name=name,
                code=code,
                city=city,
                address=address,
                contact_number=contact_number,
                is_main_branch=is_main,
                order=order,
                is_active=True,
            )
            messages.success(request, f"Branch '{name}' created successfully.")

    return redirect("/leads/hospital-configuration/?tab=branches")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_department_save(request, pk=None):
    hospital = request.user.hospital
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        code = request.POST.get("code", "").strip()
        description = request.POST.get("description", "").strip()
        order = int(request.POST.get("order", 0) or 0)
        branch_ids = request.POST.getlist("branches")

        if not name:
            messages.error(request, "Department name is required.")
            return redirect("/leads/hospital-configuration/?tab=departments")

        if pk:
            dept = get_object_or_404(HospitalDepartment, pk=pk, hospital=hospital)
            dept.name = name
            dept.code = code
            dept.description = description
            dept.order = order
            dept.save()
            dept.branches.set(HospitalBranch.objects.filter(id__in=branch_ids, hospital=hospital))
            messages.success(request, f"Department '{name}' updated successfully.")
        else:
            dept = HospitalDepartment.objects.create(
                hospital=hospital,
                name=name,
                code=code,
                description=description,
                order=order,
                is_active=True,
            )
            dept.branches.set(HospitalBranch.objects.filter(id__in=branch_ids, hospital=hospital))
            messages.success(request, f"Department '{name}' created successfully.")

    return redirect("/leads/hospital-configuration/?tab=departments")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_disease_save(request, pk=None):
    hospital = request.user.hospital
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        code = request.POST.get("code", "").strip()
        department_id = request.POST.get("department")
        description = request.POST.get("description", "").strip()
        order = int(request.POST.get("order", 0) or 0)

        if not name or not department_id:
            messages.error(request, "Disease name and Department are required.")
            return redirect("/leads/hospital-configuration/?tab=diseases")

        dept = get_object_or_404(HospitalDepartment, pk=department_id, hospital=hospital)

        if pk:
            disease = get_object_or_404(HospitalDisease, pk=pk, hospital=hospital)
            disease.name = name
            disease.code = code
            disease.department = dept
            disease.description = description
            disease.order = order
            disease.save()
            messages.success(request, f"Disease '{name}' updated successfully.")
        else:
            HospitalDisease.objects.create(
                hospital=hospital,
                name=name,
                code=code,
                department=dept,
                description=description,
                order=order,
                is_active=True,
            )
            messages.success(request, f"Disease '{name}' added successfully.")

    return redirect("/leads/hospital-configuration/?tab=diseases")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_doctor_save(request, pk=None):
    hospital = request.user.hospital
    if request.method == "POST":
        user_id = request.POST.get("user", "").strip()
        name = request.POST.get("name", "").strip()
        department_ids = request.POST.getlist("departments")
        if not department_ids and request.POST.get("department"):
            department_ids = [request.POST.get("department")]
        qualification = request.POST.get("qualification", "").strip()
        specialization = request.POST.get("specialization", "").strip()
        email = request.POST.get("email", "").strip()
        contact_number = request.POST.get("contact_number", "").strip()
        raw_fee = request.POST.get("consultation_fee", "0").strip()
        try:
            consultation_fee = max(0, int(round(float(raw_fee or 0))))
        except (ValueError, TypeError):
            consultation_fee = 0
        order = int(request.POST.get("order", 0) or 0)

        disease_ids = request.POST.getlist("associated_diseases")
        branch_ids = request.POST.getlist("branches")

        if not user_id:
            messages.error(request, "Link to a registered Doctor user login profile is mandatory. Unregistered doctors cannot be created.")
            return redirect("/leads/hospital-configuration/?tab=doctors")

        doc_user = User.objects.filter(pk=user_id, hospital=hospital, role=User.Role.DOCTOR, is_active=True).first()
        if not doc_user:
            messages.error(request, "Selected user is not a valid active Doctor account in this hospital.")
            return redirect("/leads/hospital-configuration/?tab=doctors")

        # Ensure no other HospitalDoctor profile is linked to this user
        existing_doc = HospitalDoctor.objects.filter(hospital=hospital, user=doc_user)
        if pk:
            existing_doc = existing_doc.exclude(pk=pk)
        if existing_doc.exists():
            messages.error(request, f"Doctor user '{doc_user.get_full_name() or doc_user.username}' is already linked to another doctor profile.")
            return redirect("/leads/hospital-configuration/?tab=doctors")

        if not name:
            name = doc_user.get_full_name().strip() or doc_user.username
        # Clean 'Dr.' prefix if manually typed in name field
        import re
        name = re.sub(r"^(dr\.?|doctor)\s+", "", name, flags=re.IGNORECASE).strip() or name

        if not email and doc_user.email:
            email = doc_user.email
        if email:
            email = email.strip()
            if "@" not in email or not re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", email):
                messages.error(request, "Please enter a valid Doctor email address containing '@' (e.g. doctor@hospital.com).")
                return redirect("/leads/hospital-configuration/?tab=doctors")

        if not contact_number and doc_user.phone:
            contact_number = doc_user.phone
        if not specialization and doc_user.speciality:
            specialization = doc_user.speciality

        if not department_ids:
            messages.error(request, "Please assign at least one Clinical Department to the doctor.")
            return redirect("/leads/hospital-configuration/?tab=doctors")

        selected_depts = HospitalDepartment.objects.filter(id__in=department_ids, hospital=hospital)
        primary_dept = selected_depts.first()

        if pk:
            doc = get_object_or_404(HospitalDoctor, pk=pk, hospital=hospital)
            doc.name = name
            doc.user = doc_user
            doc.department = primary_dept
            doc.qualification = qualification
            doc.specialization = specialization
            doc.contact_number = contact_number
            doc.email = email
            doc.consultation_fee = consultation_fee
            doc.order = order
            doc.save()
            doc.departments.set(selected_depts)
            if disease_ids:
                doc.associated_diseases.set(HospitalDisease.objects.filter(id__in=disease_ids, hospital=hospital))
            else:
                # If no specific disease is chosen, automatically assign all diseases belonging to the selected department(s)
                all_dept_diseases = HospitalDisease.objects.filter(department__in=selected_depts, hospital=hospital, is_active=True)
                doc.associated_diseases.set(all_dept_diseases)
            
            # Sync Branch availabilities
            selected_branches = HospitalBranch.objects.filter(id__in=branch_ids, hospital=hospital)
            for b in selected_branches:
                DoctorBranchAvailability.objects.get_or_create(doctor=doc, branch=b, defaults={"is_active": True})
            DoctorBranchAvailability.objects.filter(doctor=doc).exclude(branch__in=selected_branches).delete()

            messages.success(request, f"Doctor 'Dr. {name}' updated successfully.")
        else:
            doc = HospitalDoctor.objects.create(
                hospital=hospital,
                name=name,
                user=doc_user,
                department=primary_dept,
                qualification=qualification,
                specialization=specialization,
                contact_number=contact_number,
                email=email,
                consultation_fee=consultation_fee,
                order=order,
                is_active=True,
            )
            doc.departments.set(selected_depts)
            if disease_ids:
                doc.associated_diseases.set(HospitalDisease.objects.filter(id__in=disease_ids, hospital=hospital))
            else:
                # If no specific disease is chosen, automatically assign all diseases belonging to the selected department(s)
                all_dept_diseases = HospitalDisease.objects.filter(department__in=selected_depts, hospital=hospital, is_active=True)
                doc.associated_diseases.set(all_dept_diseases)
            
            selected_branches = HospitalBranch.objects.filter(id__in=branch_ids, hospital=hospital)
            for b in selected_branches:
                DoctorBranchAvailability.objects.create(doctor=doc, branch=b, is_active=True)

            messages.success(request, f"Doctor 'Dr. {name}' registered successfully.")

        # Sync MasterItem 'Doctors'
        from leads.models import MasterGroup, MasterItem
        doc_grp = MasterGroup.objects.filter(name__iexact='Doctors').first()
        if doc_grp:
            MasterItem.objects.get_or_create(
                group=doc_grp,
                hospital=hospital,
                name=name,
                defaults={"is_active": True}
            )

    return redirect("/leads/hospital-configuration/?tab=doctors")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_branch_toggle(request, pk):
    branch = get_object_or_404(HospitalBranch, pk=pk, hospital=request.user.hospital)
    branch.is_active = not branch.is_active
    branch.save(update_fields=["is_active"])
    status_str = "activated" if branch.is_active else "deactivated"
    messages.success(request, f"Hospital Branch '{branch.name}' has been {status_str}.")
    return redirect("/leads/hospital-configuration/?tab=branches")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_department_toggle(request, pk):
    dept = get_object_or_404(HospitalDepartment, pk=pk, hospital=request.user.hospital)
    dept.is_active = not dept.is_active
    dept.save(update_fields=["is_active"])
    status_str = "activated" if dept.is_active else "deactivated"
    messages.success(request, f"Department '{dept.name}' has been {status_str}.")
    return redirect("/leads/hospital-configuration/?tab=departments")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_disease_toggle(request, pk):
    dis = get_object_or_404(HospitalDisease, pk=pk, hospital=request.user.hospital)
    dis.is_active = not dis.is_active
    dis.save(update_fields=["is_active"])
    status_str = "activated" if dis.is_active else "deactivated"
    messages.success(request, f"Disease / Condition '{dis.name}' has been {status_str}.")
    return redirect("/leads/hospital-configuration/?tab=diseases")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_doctor_toggle(request, pk):
    doc = get_object_or_404(HospitalDoctor, pk=pk, hospital=request.user.hospital)
    doc.is_active = not doc.is_active
    doc.save(update_fields=["is_active"])
    status_str = "activated" if doc.is_active else "deactivated"
    messages.success(request, f"Doctor 'Dr. {doc.name}' has been {status_str}.")
    return redirect("/leads/hospital-configuration/?tab=doctors")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def hospital_master_excel_import(request):
    """
    Bulk import Hospital Master data from Excel/CSV containing:
    Hospital Branch | Department | Doctor | Disease
    """
    hospital = request.user.hospital
    if not hospital:
        messages.error(request, "No hospital context found.")
        return redirect("dashboard:home")

    if request.method == "POST" and request.FILES.get("excel_file"):
        uploaded_file = request.FILES["excel_file"]
        filename = uploaded_file.name.lower()

        try:
            import pandas as pd
            if filename.endswith(".csv"):
                df = pd.read_csv(uploaded_file)
            else:
                df = pd.read_excel(uploaded_file)

            # Clean and normalize column names
            df.columns = [str(c).strip().lower().replace(" ", "_").replace("-", "_") for c in df.columns]
            
            # Map possible column header variations
            branch_col = next((c for c in df.columns if "branch" in c), None)
            dept_col = next((c for c in df.columns if "dept" in c or "department" in c), None)
            doc_col = next((c for c in df.columns if "doc" in c or "doctor" in c), None)
            dis_col = next((c for c in df.columns if "dis" in c or "disease" in c or "condition" in c), None)

            # ---------------------------------------------------------
            # High-Performance In-Memory Pre-fetching & Caching (O(1))
            # ---------------------------------------------------------
            from django.db import transaction

            # Pre-load existing hospital masters to prevent thousands of DB queries
            existing_branches = {b.name.strip().lower(): b for b in HospitalBranch.objects.filter(hospital=hospital)}
            existing_depts = {d.name.strip().lower(): d for d in HospitalDepartment.objects.filter(hospital=hospital).prefetch_related("branches")}
            existing_docs = {doc.name.strip().lower(): doc for doc in HospitalDoctor.objects.filter(hospital=hospital).prefetch_related("departments", "associated_diseases")}
            existing_diseases = {(dis.department_id, dis.name.strip().lower()): dis for dis in HospitalDisease.objects.filter(hospital=hospital)}
            existing_availabilities = {(a.doctor_id, a.branch_id) for a in DoctorBranchAvailability.objects.filter(doctor__hospital=hospital)}

            # Track in-memory relations to avoid duplicate M2M operations
            dept_branch_links = {(d.id, b.id) for d in existing_depts.values() for b in d.branches.all()}
            doc_dept_links = {(doc.id, dept.id) for doc in existing_docs.values() for dept in doc.departments.all()}
            doc_disease_links = {(doc.id, dis.id) for doc in existing_docs.values() for dis in doc.associated_diseases.all()}

            branches_created = 0
            depts_created = 0
            docs_created = 0
            diseases_created = 0
            existing_skipped = 0
            rows_processed = 0

            with transaction.atomic():
                # Convert dataframe to simple Python dicts (much faster than df.iterrows())
                records = df.to_dict(orient="records")

                for row in records:
                    raw_branch = str(row.get(branch_col, "")).strip() if branch_col and pd.notna(row.get(branch_col)) else ""
                    raw_dept = str(row.get(dept_col, "")).strip() if dept_col and pd.notna(row.get(dept_col)) else ""
                    raw_doc = str(row.get(doc_col, "")).strip() if doc_col and pd.notna(row.get(doc_col)) else ""
                    raw_disease = str(row.get(dis_col, "")).strip() if dis_col and pd.notna(row.get(dis_col)) else ""

                    if not raw_dept and not raw_doc and not raw_disease and not raw_branch:
                        continue

                    rows_processed += 1
                    branch_obj = None
                    dept_obj = None
                    doc_obj = None
                    disease_obj = None

                    # 1. Branch
                    if raw_branch and raw_branch.lower() not in ["nan", "none", ""]:
                        b_key = raw_branch.lower()
                        if b_key in existing_branches:
                            branch_obj = existing_branches[b_key]
                        else:
                            branch_obj = HospitalBranch.objects.create(
                                hospital=hospital,
                                name=raw_branch,
                                is_active=True
                            )
                            existing_branches[b_key] = branch_obj
                            branches_created += 1

                    # 2. Department
                    if raw_dept and raw_dept.lower() not in ["nan", "none", ""]:
                        d_key = raw_dept.lower()
                        if d_key in existing_depts:
                            dept_obj = existing_depts[d_key]
                        else:
                            dept_obj = HospitalDepartment.objects.create(
                                hospital=hospital,
                                name=raw_dept,
                                is_active=True
                            )
                            existing_depts[d_key] = dept_obj
                            depts_created += 1

                        # Link branch to department if not already linked
                        if branch_obj and dept_obj:
                            link_key = (dept_obj.id, branch_obj.id)
                            if link_key not in dept_branch_links:
                                dept_obj.branches.add(branch_obj)
                                dept_branch_links.add(link_key)

                    # 3. Doctor
                    if raw_doc and raw_doc.lower() not in ["nan", "none", ""]:
                        clean_doc_name = raw_doc
                        if clean_doc_name.lower().startswith("dr.") or clean_doc_name.lower().startswith("dr "):
                            clean_doc_name = clean_doc_name[3:].strip()

                        doc_key = clean_doc_name.lower()
                        if doc_key in existing_docs:
                            doc_obj = existing_docs[doc_key]
                            existing_skipped += 1
                        else:
                            doc_obj = HospitalDoctor.objects.create(
                                hospital=hospital,
                                name=clean_doc_name,
                                department=dept_obj,
                                is_active=True
                            )
                            existing_docs[doc_key] = doc_obj
                            docs_created += 1

                        # Link department to doctor
                        if dept_obj:
                            doc_dept_key = (doc_obj.id, dept_obj.id)
                            if doc_dept_key not in doc_dept_links:
                                doc_obj.departments.add(dept_obj)
                                doc_dept_links.add(doc_dept_key)
                            if not doc_obj.department:
                                doc_obj.department = dept_obj
                                doc_obj.save(update_fields=["department"])

                        # Link branch availability
                        if branch_obj:
                            avail_key = (doc_obj.id, branch_obj.id)
                            if avail_key not in existing_availabilities:
                                DoctorBranchAvailability.objects.create(
                                    doctor=doc_obj,
                                    branch=branch_obj,
                                    is_active=True
                                )
                                existing_availabilities.add(avail_key)

                    # 4. Disease / Condition
                    if raw_disease and raw_disease.lower() not in ["nan", "none", ""] and dept_obj:
                        dis_key = (dept_obj.id, raw_disease.lower())
                        if dis_key in existing_diseases:
                            disease_obj = existing_diseases[dis_key]
                        else:
                            disease_obj = HospitalDisease.objects.create(
                                hospital=hospital,
                                department=dept_obj,
                                name=raw_disease,
                                is_active=True
                            )
                            existing_diseases[dis_key] = disease_obj
                            diseases_created += 1

                        # Link disease to doctor
                        if doc_obj and disease_obj:
                            doc_dis_key = (doc_obj.id, disease_obj.id)
                            if doc_dis_key not in doc_disease_links:
                                doc_obj.associated_diseases.add(disease_obj)
                                doc_disease_links.add(doc_dis_key)

            messages.success(
                request,
                f"Excel Import Completed Successfully! Processed {rows_processed} rows in milliseconds. "
                f"Added: {branches_created} branches, {depts_created} departments, "
                f"{docs_created} doctors, {diseases_created} diseases/conditions."
            )
        except Exception as ex:
            messages.error(request, f"Failed to import Excel/CSV file: {str(ex)}")

    return redirect("/leads/hospital-configuration/")


@login_required
def hospital_master_sample_download(request):
    """Download Sample Excel / CSV template for Hospital Master Configuration"""
    from django.http import HttpResponse
    fmt = request.GET.get("format", "csv").lower()
    
    if fmt in ["xlsx", "excel"]:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Hospital Master Template"
        
        headers = ["hospital_branch", "department", "doctor", "disease"]
        ws.append(headers)
        
        header_fill = PatternFill(start_color="2563EB", end_color="2563EB", fill_type="solid")
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        thin_border = Border(
            left=Side(style='thin', color='E2E8F0'),
            right=Side(style='thin', color='E2E8F0'),
            top=Side(style='thin', color='E2E8F0'),
            bottom=Side(style='thin', color='E2E8F0')
        )
        
        for col_num, cell in enumerate(ws[1], 1):
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = thin_border
            
        sample_rows = [
            ["Nelson Hospital Dhantoli", "Neurology", "Dr. Raj ratan", "Migraine"],
            ["Nelson Hospital Dhantoli", "Neurology", "Dr. Raj ratan", "Stroke"],
            ["Nelson Hospital Dhantoli", "Neurology", "Dr. Raj ratan", "Dementia"],
            ["Nelson Luxe Mother & Child Care Hospital, Wardhmannagar", "Gynaecology", "Dr. Priya Sharma", "Infertility / IVF"],
            ["Central Brain & Spine Hospital, Dhantoli", "Cardiology", "Dr. Rajesh Patil", "Hypertension"],
        ]
        
        for r_idx, row in enumerate(sample_rows, 2):
            ws.append(row)
            for cell in ws[r_idx]:
                cell.border = thin_border
                cell.alignment = Alignment(vertical="center")

        ws.column_dimensions['A'].width = 38
        ws.column_dimensions['B'].width = 24
        ws.column_dimensions['C'].width = 24
        ws.column_dimensions['D'].width = 28
        
        response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        response["Content-Disposition"] = 'attachment; filename="hospital_master_import_template.xlsx"'
        wb.save(response)
        return response

    import csv
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="hospital_master_template.csv"'

    writer = csv.writer(response)
    writer.writerow(["hospital_branch", "department", "doctor", "disease"])
    writer.writerow(["Nelson Hospital Dhantoli", "Neurology", "Dr. Raj ratan", "Migraine"])
    writer.writerow(["Nelson Hospital Dhantoli", "Neurology", "Dr. Raj ratan", "Stroke"])
    writer.writerow(["Nelson Hospital Dhantoli", "Neurology", "Dr. Raj ratan", "Dementia"])
    writer.writerow(["Nelson Luxe Mother & Child Care Hospital, Wardhmannagar", "Gynaecology", "Dr. Priya Sharma", "Infertility / IVF"])
    writer.writerow(["Central Brain & Spine Hospital, Dhantoli", "Cardiology", "Dr. Rajesh Patil", "Hypertension"])

    return response


@login_required
def export_hospital_config_excel(request):
    """
    Export full Hospital Configuration (Branches, Departments, Doctors, Diseases)
    into a beautifully formatted Excel (.xlsx) file with cascading mapping sheets.
    """
    from django.http import HttpResponse
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    
    hospital = request.user.hospital
    h_filter = {"hospital": hospital} if hospital else {}
    hospital_name = hospital.name if hospital else "All Hospitals"

    branches = HospitalBranch.objects.filter(**h_filter).order_by("order", "name")
    departments = HospitalDepartment.objects.filter(**h_filter).order_by("order", "name")
    doctors = HospitalDoctor.objects.filter(**h_filter).order_by("order", "name")
    diseases = HospitalDisease.objects.filter(**h_filter).order_by("department__name", "order", "name")

    wb = openpyxl.Workbook()
    
    header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    sec_fill = PatternFill(start_color="0284C7", end_color="0284C7", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    thin_border = Border(
        left=Side(style='thin', color='CBD5E1'),
        right=Side(style='thin', color='CBD5E1'),
        top=Side(style='thin', color='CBD5E1'),
        bottom=Side(style='thin', color='CBD5E1')
    )

    # ----------------------------------------------------
    # Sheet 1: Master Mapping (Import-Compatible Table)
    # ----------------------------------------------------
    ws1 = wb.active
    ws1.title = "Hospital Master Mapping"
    
    ws1.append(["hospital_branch", "department", "doctor", "disease"])
    for cell in ws1[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border
    
    row_count = 1
    # Build complete linked rows
    for dept in departments:
        dept_branches = list(dept.branches.all()) or [None]
        dept_diseases = list(dept.diseases.all()) or [None]
        dept_doctors = list(dept.doctors.all()) or [None]

        for b in dept_branches:
            b_name = b.name if b else ""
            for doc in dept_doctors:
                doc_name = f"Dr. {doc.name}" if doc else ""
                # Get diseases linked to this doctor or department
                doc_diseases = list(doc.associated_diseases.filter(department=dept)) if doc else []
                applicable_diseases = doc_diseases if doc_diseases else dept_diseases

                for dis in applicable_diseases:
                    dis_name = dis.name if dis else ""
                    row_count += 1
                    ws1.append([b_name, dept.name, doc_name, dis_name])
                    for cell in ws1[row_count]:
                        cell.border = thin_border
                        cell.alignment = Alignment(vertical="center")

    if row_count == 1:
        # Fallback if no deep mappings configured yet: export each master directly
        for b in branches:
            row_count += 1
            ws1.append([b.name, "", "", ""])
            for cell in ws1[row_count]:
                cell.border = thin_border

    ws1.column_dimensions['A'].width = 38
    ws1.column_dimensions['B'].width = 24
    ws1.column_dimensions['C'].width = 26
    ws1.column_dimensions['D'].width = 30

    # ----------------------------------------------------
    # Sheet 2: Branches Directory
    # ----------------------------------------------------
    ws2 = wb.create_sheet(title="Branches")
    ws2.append(["Branch Name", "Code", "City", "Address", "Contact Number", "Status"])
    for cell in ws2[1]:
        cell.fill = sec_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    for r_idx, b in enumerate(branches, 2):
        ws2.append([
            b.name,
            b.code or "",
            b.city or "",
            b.address or "",
            b.contact_number or "",
            "Active" if b.is_active else "Inactive"
        ])
        for cell in ws2[r_idx]:
            cell.border = thin_border
            cell.alignment = Alignment(vertical="center")

    ws2.column_dimensions['A'].width = 36
    ws2.column_dimensions['B'].width = 16
    ws2.column_dimensions['C'].width = 18
    ws2.column_dimensions['D'].width = 40
    ws2.column_dimensions['E'].width = 20
    ws2.column_dimensions['F'].width = 14

    # ----------------------------------------------------
    # Sheet 3: Departments Directory
    # ----------------------------------------------------
    ws3 = wb.create_sheet(title="Departments")
    ws3.append(["Department Name", "Code", "Associated Branches", "Status"])
    for cell in ws3[1]:
        cell.fill = sec_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    for r_idx, d in enumerate(departments, 2):
        branch_names = ", ".join([b.name for b in d.branches.all()])
        ws3.append([
            d.name,
            d.code or "",
            branch_names or "All Branches",
            "Active" if d.is_active else "Inactive"
        ])
        for cell in ws3[r_idx]:
            cell.border = thin_border
            cell.alignment = Alignment(vertical="center")

    ws3.column_dimensions['A'].width = 28
    ws3.column_dimensions['B'].width = 16
    ws3.column_dimensions['C'].width = 45
    ws3.column_dimensions['D'].width = 14

    # ----------------------------------------------------
    # Sheet 4: Doctors Directory
    # ----------------------------------------------------
    ws4 = wb.create_sheet(title="Doctors")
    ws4.append(["Doctor Name", "User Account", "Department(s)", "Specialization", "Fee (Rs.)", "Branches", "Status"])
    for cell in ws4[1]:
        cell.fill = sec_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    for r_idx, doc in enumerate(doctors, 2):
        dept_str = ", ".join([dept.name for dept in doc.departments.all()]) or (doc.department.name if doc.department else "")
        branch_str = ", ".join([b.name for b in doc.branches.all()])
        ws4.append([
            f"Dr. {doc.name}",
            doc.user.username if doc.user else "Not Linked",
            dept_str,
            doc.specialization or doc.qualification or "",
            float(doc.consultation_fee or 0),
            branch_str or "All Branches",
            "Active" if doc.is_active else "Inactive"
        ])
        for cell in ws4[r_idx]:
            cell.border = thin_border
            cell.alignment = Alignment(vertical="center")

    ws4.column_dimensions['A'].width = 26
    ws4.column_dimensions['B'].width = 18
    ws4.column_dimensions['C'].width = 30
    ws4.column_dimensions['D'].width = 26
    ws4.column_dimensions['E'].width = 14
    ws4.column_dimensions['F'].width = 35
    ws4.column_dimensions['G'].width = 14

    # ----------------------------------------------------
    # Sheet 5: Diseases Directory
    # ----------------------------------------------------
    ws5 = wb.create_sheet(title="Diseases & Conditions")
    ws5.append(["Disease / Condition", "Department", "Code", "Status"])
    for cell in ws5[1]:
        cell.fill = sec_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    for r_idx, dis in enumerate(diseases, 2):
        ws5.append([
            dis.name,
            dis.department.name if dis.department else "",
            dis.code or "",
            "Active" if dis.is_active else "Inactive"
        ])
        for cell in ws5[r_idx]:
            cell.border = thin_border
            cell.alignment = Alignment(vertical="center")

    ws5.column_dimensions['A'].width = 32
    ws5.column_dimensions['B'].width = 26
    ws5.column_dimensions['C'].width = 16
    ws5.column_dimensions['D'].width = 14

    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    filename = f"Hospital_Configuration_{timezone.now().strftime('%Y%m%d_%H%M')}.xlsx"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


@login_required
def export_hospital_config_pdf(request):
    """
    Renders high-quality printable PDF view of all Hospital Configurations
    (Branches, Departments, Doctors, and Disease Mappings).
    """
    hospital = request.user.hospital
    h_filter = {"hospital": hospital} if hospital else {}
    hospital_name = hospital.name if hospital else "All Hospitals"

    branches = HospitalBranch.objects.filter(**h_filter).order_by("order", "name")
    departments = HospitalDepartment.objects.filter(**h_filter).order_by("order", "name")
    doctors = HospitalDoctor.objects.filter(**h_filter).prefetch_related("departments", "branches", "associated_diseases").order_by("order", "name")
    diseases = HospitalDisease.objects.filter(**h_filter).select_related("department").order_by("department__name", "order", "name")

    # Build master mapping rows
    mapping_rows = []
    for dept in departments:
        dept_branches = list(dept.branches.all()) or [None]
        dept_diseases = list(dept.diseases.all()) or [None]
        dept_doctors = list(dept.doctors.all()) or [None]

        for b in dept_branches:
            b_name = b.name if b else "-"
            for doc in dept_doctors:
                doc_name = f"Dr. {doc.name}" if doc else "-"
                doc_diseases = list(doc.associated_diseases.filter(department=dept)) if doc else []
                applicable_diseases = doc_diseases if doc_diseases else dept_diseases

                for dis in applicable_diseases:
                    dis_name = dis.name if dis else "-"
                    mapping_rows.append({
                        "branch": b_name,
                        "department": dept.name,
                        "doctor": doc_name,
                        "disease": dis_name
                    })

    if not mapping_rows:
        for b in branches:
            mapping_rows.append({
                "branch": b.name,
                "department": "-",
                "doctor": "-",
                "disease": "-"
            })

    context = {
        "hospital_name": hospital_name,
        "branches": branches,
        "departments": departments,
        "doctors": doctors,
        "diseases": diseases,
        "mapping_rows": mapping_rows,
        "now": timezone.now(),
    }
    return render(request, "leads/hospital_config_print_pdf.html", context)


@login_required
def cascading_hospital_data_api(request):
    """
    High-Performance JSON API for dynamic cascading dependent dropdowns:
    Branch -> Department -> Doctor / Disease
    """
    hospital = request.user.hospital
    if not hospital:
        return JsonResponse({"error": "No hospital context"}, status=400)

    branch_id = request.GET.get("branch_id")
    dept_id = request.GET.get("department_id")
    doctor_id = request.GET.get("doctor_id")

    res = {
        "departments": [],
        "doctors": [],
        "diseases": [],
    }

    # 1. If branch selected, filter departments available at this branch
    if branch_id:
        if str(branch_id).isdigit():
            branch = HospitalBranch.objects.filter(pk=branch_id, hospital=hospital, is_active=True).first()
        else:
            branch = HospitalBranch.objects.filter(name__iexact=branch_id, hospital=hospital, is_active=True).first()
            if not branch:
                branch = HospitalBranch.objects.filter(name__icontains=branch_id, hospital=hospital, is_active=True).first()

        if branch:
            dept_qs = branch.departments.filter(is_active=True).order_by("order", "name")
            res["departments"] = [{"id": d.id, "name": d.name} for d in dept_qs]

    # 2. If department selected, filter doctors and diseases for this department
    if dept_id:
        if str(dept_id).isdigit():
            dept = HospitalDepartment.objects.filter(pk=dept_id, hospital=hospital, is_active=True).first()
        else:
            dept = HospitalDepartment.objects.filter(name__iexact=dept_id, hospital=hospital, is_active=True).first()
            
        if dept:
            doc_qs = HospitalDoctor.objects.filter(
                models.Q(departments=dept) | models.Q(department=dept),
                hospital=hospital,
                is_active=True
            ).distinct().order_by("order", "name")
            
            if branch_id and str(branch_id).isdigit():
                doc_qs = doc_qs.filter(branches__id=branch_id)
            res["doctors"] = [{"id": doc.id, "name": doc.name, "display_name": f"Dr. {doc.name}" if not doc.name.lower().startswith("dr") else doc.name, "fee": float(doc.consultation_fee)} for doc in doc_qs]

            dis_qs = dept.diseases.filter(is_active=True).order_by("order", "name")
            res["diseases"] = [{"id": dis.id, "name": dis.name} for dis in dis_qs]

    # 3. If doctor selected, return doctor's available branches and diseases
    if doctor_id:
        doc = HospitalDoctor.objects.filter(pk=doctor_id, hospital=hospital, is_active=True).first()
        if doc:
            res["diseases"] = [{"id": dis.id, "name": dis.name} for dis in doc.associated_diseases.filter(is_active=True)]
            res["branches"] = [{"id": b.id, "name": b.name} for b in doc.branches.filter(is_active=True)]

    return JsonResponse(res)


# ---------------------------------------------------------------------------
# Super Admin & Admin: Bulk Lead Transfer Across Users
# ---------------------------------------------------------------------------

@login_required
def bulk_lead_transfer(request):
    """
    Allows Super Admins & Admins to reassign/transfer leads from one user to another in bulk.
    Preserves all previous follow-up remarks, notes, comments, and timeline history intact.
    Restricted to Lead Attendant, Counsellor, and HR roles for current active business.
    """
    from accounts.models import User, Hospital
    from leads.models import Lead, LeadStage, Course
    from followups.models import Activity, ActivityType
    from notifications.models import Notification
    from django.db import transaction
    from django.core.paginator import Paginator
    from datetime import datetime

    is_superadmin = request.user.is_superuser or request.user.role == User.Role.SUPER_ADMIN
    is_admin = request.user.role in (User.Role.ADMIN, User.Role.MANAGER)
    
    if not (is_superadmin or is_admin):
        messages.error(request, "Permission denied. Only Super Admins and Admins can transfer leads in bulk.")
        return redirect("dashboard:home")

    # Determine Active Business/Tenant (from session or GET query param for super admin switch)
    active_biz_id = request.GET.get("business") or request.GET.get("hospital") or request.session.get("active_business_id")
    active_hospital = None
    if request.user.hospital:
        active_hospital = request.user.hospital
    elif active_biz_id and str(active_biz_id).isdigit():
        active_hospital = Hospital.objects.filter(pk=int(active_biz_id)).first()

    # Allowed roles for assignment/transfer: LEAD_ATTENDENT, COUNSELLOR, HR
    allowed_roles = [User.Role.LEAD_ATTENDENT, User.Role.COUNSELLOR, User.Role.HR]

    # Filter available users list strictly based on current active business/tenant
    if active_hospital:
        users_qs = User.objects.filter(
            hospital=active_hospital,
            role__in=allowed_roles,
            is_active=True
        ).order_by("first_name", "username")
        courses_qs = Course.objects.filter(hospital=active_hospital, is_active=True).order_by("name")
    else:
        # Default Zappcode Academy / Global pool
        users_qs = User.objects.filter(
            hospital__isnull=True,
            role__in=allowed_roles,
            is_active=True
        ).order_by("first_name", "username")
        courses_qs = Course.objects.filter(is_active=True).order_by("name")

    # LeadStage is global across tenant types
    stages_qs = LeadStage.objects.filter(is_active=True).order_by("order", "name")


    # Helper function to query matching leads based on source user and filter params
    def _get_matching_leads(source_uid, stage_id, course_id, deal_status, date_from_val, date_to_val, search_q):
        base_leads = Lead.objects.filter(is_archived=False)
        if active_hospital:
            base_leads = base_leads.filter(hospital=active_hospital)
        elif not is_superadmin:
            base_leads = base_leads.filter(hospital__isnull=True)

        if source_uid == "unassigned":
            base_leads = base_leads.filter(assigned_to__isnull=True)
        elif source_uid and str(source_uid).isdigit():
            base_leads = base_leads.filter(assigned_to_id=int(source_uid))
        else:
            return Lead.objects.none()

        if stage_id and str(stage_id).isdigit():
            base_leads = base_leads.filter(stage_id=int(stage_id))

        if course_id and str(course_id).isdigit():
            base_leads = base_leads.filter(course_id=int(course_id))

        if deal_status:
            base_leads = base_leads.filter(deal_status=deal_status)

        if search_q:
            base_leads = base_leads.filter(
                Q(name__icontains=search_q) |
                Q(mobile__icontains=search_q) |
                Q(lead_code__icontains=search_q) |
                Q(city__icontains=search_q)
            )

        def _parse_d(v):
            if not v:
                return None
            for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
                try:
                    return datetime.strptime(v.strip(), fmt).date()
                except ValueError:
                    pass
            return None

        df = _parse_d(date_from_val)
        dt = _parse_d(date_to_val)
        if df:
            base_leads = base_leads.filter(inquiry_date__gte=df)
        if dt:
            base_leads = base_leads.filter(inquiry_date__lte=dt)

        return base_leads.order_by("-updated_at", "-id")

    # AJAX Live Preview Count / List API with Pagination
    if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.GET.get("format") == "json":
        src_id = request.GET.get("source_user", "").strip()
        stg_id = request.GET.get("stage", "").strip()
        crs_id = request.GET.get("course", "").strip()
        ds_val = request.GET.get("deal_status", "").strip()
        d_from = request.GET.get("date_from", "").strip()
        d_to = request.GET.get("date_to", "").strip()
        sq = request.GET.get("q", "").strip()
        page_num = request.GET.get("page", 1)
        per_page = int(request.GET.get("per_page", 10))

        matched_qs = _get_matching_leads(src_id, stg_id, crs_id, ds_val, d_from, d_to, sq)
        total_count = matched_qs.count()

        paginator = Paginator(matched_qs, per_page)
        page_obj = paginator.get_page(page_num)

        sample_leads = []
        for l in page_obj.object_list:
            sample_leads.append({
                "id": l.id,
                "lead_code": l.lead_code or f"#{l.id}",
                "name": l.name or "",
                "mobile": l.mobile or "",
                "deal_status": l.deal_status or "Open",
                "city": l.city or (l.custom_data or {}).get("location", "") or "",
                "inquiry_date": str(l.inquiry_date) if l.inquiry_date else "",
            })

        return JsonResponse({
            "success": True,
            "count": total_count,
            "leads": sample_leads,
            "current_page": page_obj.number,
            "num_pages": paginator.num_pages,
            "has_next": page_obj.has_next(),
            "has_previous": page_obj.has_previous(),
            "start_index": page_obj.start_index() if total_count > 0 else 0,
            "end_index": page_obj.end_index() if total_count > 0 else 0,
        })

    # POST Execution: Perform Bulk Lead Transfer
    if request.method == "POST":
        source_user_id = request.POST.get("source_user", "").strip()
        target_user_id = request.POST.get("target_user", "").strip()
        stage_id = request.POST.get("stage", "").strip()
        course_id = request.POST.get("course", "").strip()
        deal_status = request.POST.get("deal_status", "").strip()
        date_from = request.POST.get("date_from", "").strip()
        date_to = request.POST.get("date_to", "").strip()
        search_q = request.POST.get("q", "").strip()
        transfer_limit = request.POST.get("transfer_limit", "").strip()
        selected_lead_ids = request.POST.getlist("selected_lead_ids")

        if not source_user_id:
            messages.error(request, "Please select the Source User (or Unassigned) from whom leads should be transferred.")
            return redirect("leads:bulk_lead_transfer")

        if not target_user_id or not target_user_id.isdigit():
            messages.error(request, "Please select a valid Target Assignee to receive the leads.")
            return redirect("leads:bulk_lead_transfer")

        target_user = User.objects.filter(pk=int(target_user_id), is_active=True).first()
        if not target_user:
            messages.error(request, "Target assignee user was not found or is inactive.")
            return redirect("leads:bulk_lead_transfer")

        source_user = None
        if source_user_id.isdigit():
            source_user = User.objects.filter(pk=int(source_user_id)).first()
            source_name = source_user.get_full_name() or source_user.username
        else:
            source_name = "Unassigned Pool"

        target_name = target_user.get_full_name() or target_user.username

        # Gather queryset of leads to transfer
        if selected_lead_ids:
            clean_ids = [int(i) for i in selected_lead_ids if str(i).isdigit()]
            leads_to_transfer = Lead.objects.filter(id__in=clean_ids, is_archived=False)
            if active_hospital:
                leads_to_transfer = leads_to_transfer.filter(hospital=active_hospital)
        else:
            leads_to_transfer = _get_matching_leads(source_user_id, stage_id, course_id, deal_status, date_from, date_to, search_q)
            if transfer_limit and transfer_limit.isdigit() and int(transfer_limit) > 0:
                limit_num = int(transfer_limit)
                lead_id_slice = list(leads_to_transfer.values_list("id", flat=True)[:limit_num])
                leads_to_transfer = Lead.objects.filter(id__in=lead_id_slice)

        transfer_count = leads_to_transfer.count()
        if transfer_count == 0:
            messages.warning(request, "No matching leads found to transfer with the selected criteria.")
            return redirect("leads:bulk_lead_transfer")

        # Perform atomic batch update and record timeline activity logs
        admin_name = request.user.get_full_name() or request.user.username
        now_time = timezone.now()

        with transaction.atomic():
            lead_objs = list(leads_to_transfer)
            # 1. Bulk update assigned_to on lead objects and synchronize branch if target user belongs to a branch
            for l in lead_objs:
                l.assigned_to = target_user
                l.updated_at = now_time
                if target_user.branch:
                    if not isinstance(l.custom_data, dict):
                        l.custom_data = {}
                    l.custom_data["hospital_branch"] = target_user.branch.name
                    l.custom_data["branch"] = target_user.branch.name
                    l.custom_data["dyn_hospital_branch"] = target_user.branch.name
                    l.custom_data["dyn_branch"] = target_user.branch.name
                l.save(update_fields=["assigned_to", "updated_at", "custom_data"])

            # 2. Add Activity log for each lead to record the transfer in timeline
            activities = []
            for l in lead_objs:
                activities.append(
                    Activity(
                        lead_id=l.id,
                        activity_type=ActivityType.SYSTEM,
                        description=f"Lead ownership transferred from {source_name} to {target_name} by Admin ({admin_name}). Historical notes & remarks preserved.",
                        created_by=request.user,
                    )
                )
            if activities:
                Activity.objects.bulk_create(activities)

            # 3. Create In-App Notification for Target Assignee
            Notification.objects.create(
                user=target_user,
                title="Bulk Leads Assigned",
                message=f"{transfer_count} leads have been reassigned/transferred to you from {source_name} by {admin_name}. You can now follow up with them.",
                link="/leads/my-leads/" if not target_user.is_hospital_user else "/dashboard/telecaller/my-leads/",
            )

        messages.success(request, f"Successfully transferred {transfer_count} lead(s) from '{source_name}' to '{target_name}'! All previous call remarks and timeline history remain intact.")
        return redirect("leads:bulk_lead_transfer")

    return render(request, "leads/bulk_transfer.html", {
        "active": "bulk_transfer",
        "active_hospital": active_hospital,
        "users": users_qs,
        "stages": stages_qs,
        "courses": courses_qs,
        "deal_statuses": [
            ("OPEN", "Open / In Progress"),
            ("WON", "Won / Payment / Admission Done"),
            ("LOST", "Lost / Cancelled / Not Interested"),
        ],
    })




