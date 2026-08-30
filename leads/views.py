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

from followups.models import FollowUp, Note, Activity, FollowUpMode, FollowUpStatus
from admissions.models import Admission
from accounts.models import User
from .models import (
    Lead, SourceCategory, LeadSource, Campaign, Course, LeadStage, Tag, 
    MasterGroup, MasterItem, HospitalBranch, HospitalDepartment, HospitalDoctor, 
    HospitalDisease, DoctorBranchAvailability, DealStatus, LeadTemperature,
)
from .forms import (
    LeadForm, HospitalLeadForm, SourceCategoryForm, LeadSourceForm, CampaignForm, CourseForm, LeadStageForm,
)


def _can_edit_lead(user, lead):
    # 1. Multi-Tenant Business Alignment Check
    if user.hospital:
        if lead.hospital != user.hospital:
            return False
    else:
        if lead.hospital is not None:
            if not (user.role == User.Role.SUPER_ADMIN or user.is_superuser):
                return False

    # 2. Within-Business Edit Permission Check
    if user.can_edit_any_lead:
        return True
    if user.can_edit_own_leads and lead.assigned_to == user:
        return True
    if user.hospital and lead.hospital == user.hospital and lead.assigned_to is None:
        return True
    if not user.hospital and lead.hospital is None and lead.assigned_to is None:
        return True
    return False

def _can_access_lead(user, lead):
    # 1. Multi-Tenant Business Alignment Check
    if user.hospital:
        if lead.hospital != user.hospital:
            return False
    else:
        if lead.hospital is not None:
            if not (user.role == User.Role.SUPER_ADMIN or user.is_superuser):
                return False

    # 2. Doctor within same hospital can view patient leads
    if user.role == User.Role.DOCTOR:
        return True

    # 3. Within-Business Access Permission Check
    if user.can_view_all_leads:
        return True
    if user.can_view_team_leads:
        team = User.objects.filter(reports_to=user)
        if lead.assigned_to == user or lead.assigned_to in team:
            return True
    if user.can_view_assigned_leads and lead.assigned_to == user:
        return True
    if user.hospital and lead.hospital == user.hospital and lead.assigned_to is None:
        return True
    if not user.hospital and lead.hospital is None and lead.assigned_to is None:
        return True
    return False


FK_FILTER_FIELDS = [
    "source_category", "lead_source", "campaign", "course", "stage", "assigned_to", "import_job",
]
CHAR_FILTER_FIELDS = [
    "temperature", "deal_status", "admission_status",
]


@login_required
def lead_list(request):
    leads = Lead.objects.select_related(
        "course", "stage", "lead_source", "source_category", "campaign", "assigned_to"
    ).filter(is_archived=False)

    if request.user.hospital:
        leads = leads.filter(hospital=request.user.hospital)
        
        if not request.user.can_view_all_leads:
            if request.user.can_view_team_leads:
                # View leads assigned to team members reporting to this user
                team = User.objects.filter(reports_to=request.user)
                leads = leads.filter(Q(assigned_to=request.user) | Q(assigned_to__in=team))
            elif request.user.can_view_assigned_leads:
                leads = leads.filter(assigned_to=request.user)
            else:
                leads = leads.none()
    else:
        # Zappcode users -> strictly only show Zappcode leads (hospital__isnull=True)
        leads = leads.filter(hospital__isnull=True)

        # Only Zappcode Admin / Super Admin can view ALL leads
        is_zappcode_admin = request.user.role in (User.Role.SUPER_ADMIN, User.Role.ADMIN) or request.user.is_superuser
        if not is_zappcode_admin:
            if request.user.role == User.Role.MANAGER or request.user.can_view_team_leads:
                team = User.objects.filter(reports_to=request.user)
                leads = leads.filter(Q(assigned_to=request.user) | Q(assigned_to__in=team))
            else:
                # Counsellors, HR, and other employees only see their own assigned leads
                leads = leads.filter(assigned_to=request.user)

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
    selected_departments = request.GET.getlist("department")
    selected_doctors = request.GET.getlist("doctor")
    selected_assigned = request.GET.getlist("assigned_to")
    selected_deal_statuses = request.GET.getlist("deal_status")
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
                emp_q |= Q(assigned_to_id=int(emp_val))
        leads = leads.filter(emp_q)

    # 6. Deal Status & Stage filter
    if selected_deal_statuses:
        st_q = Q()
        for ds_val in selected_deal_statuses:
            if ds_val:
                st_q |= Q(deal_status__iexact=ds_val) | Q(custom_data__deal_status__iexact=ds_val) | Q(stage__name__iexact=ds_val)
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

    # 7. Appointment Status filter
    if selected_appointment_statuses:
        apt_q = Q()
        for apt_val in selected_appointment_statuses:
            if apt_val:
                apt_q |= Q(custom_data__appointment_status__icontains=apt_val)
        leads = leads.filter(apt_q)

    # 8. Priority & Temperature filter
    if selected_priorities or selected_temperatures:
        prio_q = Q()
        for p_val in (selected_priorities + selected_temperatures):
            if p_val:
                prio_q |= Q(custom_data__priority__iexact=p_val) | Q(temperature__iexact=p_val)
        leads = leads.filter(prio_q)

    # 9. Location / City filter
    if selected_locations:
        loc_q = Q()
        for loc_val in selected_locations:
            if loc_val:
                loc_q |= Q(location__iexact=loc_val) | Q(city__iexact=loc_val) | Q(custom_data__location__iexact=loc_val)
        leads = leads.filter(loc_q)

    # Legacy field fallback
    city = request.GET.get("city")
    if city and not selected_locations:
        leads = leads.filter(city__iexact=city)

    import_job_id = request.GET.get("import_job")
    selected_import_job = None
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
    today = timezone.localdate()
    if followup_filter == "overdue":
        leads = leads.filter(next_followup_date__lt=today)
    elif followup_filter == "today":
        leads = leads.filter(next_followup_date=today)

    is_nelson = not request.user.hospital or 'nelson' in request.user.hospital.name.lower()

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
                    "Created At": l.created_at.strftime("%Y-%m-%d %H:%M") if l.created_at else "",
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
                    "Created At": l.created_at.strftime("%Y-%m-%d %H:%M") if l.created_at else "",
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

    # Filter dropdown options to only those that have at least one lead associated
    active_leads = Lead.objects.filter(is_archived=False)
    if request.user.hospital:
        active_leads = active_leads.filter(hospital=request.user.hospital)
    else:
        active_leads = active_leads.filter(hospital__isnull=True)

    if request.user.role in ('COUNSELLOR', 'HR'):
        active_leads = active_leads.filter(assigned_to=request.user)
    
    used_sc_ids = active_leads.values_list("source_category_id", flat=True).distinct()
    used_ls_ids = active_leads.values_list("lead_source_id", flat=True).distinct()
    used_camp_ids = active_leads.values_list("campaign_id", flat=True).distinct()
    used_course_ids = active_leads.values_list("course_id", flat=True).distinct()
    used_stage_ids = active_leads.values_list("stage_id", flat=True).distinct()
    used_emp_ids = active_leads.values_list("assigned_to_id", flat=True).distinct()

    distinct_cities = sorted(list(set(active_leads.exclude(city="").values_list("city", flat=True))))
    distinct_locations = sorted(list(set(active_leads.exclude(location="").values_list("location", flat=True))))
    
    # Extract departments and doctors only for hospital users
    filter_departments = []
    filter_doctors = []
    filter_appointment_statuses = []
    filter_priorities = ["Hot", "Warm", "Cold"]

    if request.user.hospital:
        filter_departments = list(HospitalDepartment.objects.filter(hospital=request.user.hospital, is_active=True).values_list("name", flat=True))
        filter_doctors = list(HospitalDoctor.objects.filter(hospital=request.user.hospital, is_active=True).values_list("name", flat=True))
        if not filter_departments:
            filter_departments = list(MasterGroup.get_active_choices("Departments").filter(hospital=request.user.hospital).values_list("name", flat=True))
        if not filter_doctors:
            filter_doctors = list(MasterGroup.get_active_choices("Doctors").filter(hospital=request.user.hospital).values_list("name", flat=True))
        if not filter_departments:
            filter_departments = ["Gynaecology", "Paediatrics", "NICU / PICU", "Obstetrics", "General OPD"]
        if not filter_doctors:
            filter_doctors = list(User.objects.filter(hospital=request.user.hospital, role=User.Role.DOCTOR, is_active=True).values_list("first_name", flat=True))
        filter_appointment_statuses = ["Booked", "Booking Done", "Pending Confirmation", "Awaiting Doctor Approval", "Visited / OPD Done", "Cancelled", "Not Interested", "Payment Done"]

    active_filters_count = (
        len(selected_campaigns) + len(selected_sources) + len(selected_departments) +
        len(selected_doctors) + len(selected_assigned) + len(selected_deal_statuses) +
        len(selected_appointment_statuses) + len(selected_priorities) + len(selected_temperatures) +
        len(selected_locations) + len(selected_stages) +
        (1 if (date_from or date_to) else 0)
    )

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
        "courses": Course.objects.filter(id__in=used_course_ids),
        "stages": LeadStage.objects.filter(id__in=used_stage_ids),
        "cities": distinct_cities,
        "locations": distinct_locations,
        "filter_locations": distinct_locations or distinct_cities,
        "filter_departments": filter_departments,
        "filter_doctors": filter_doctors,
        "filter_appointment_statuses": filter_appointment_statuses,
        "filter_priorities": filter_priorities,
        "deal_status_choices": DealStatus.choices,
        "selected_campaigns": selected_campaigns,
        "selected_sources": selected_sources,
        "selected_departments": selected_departments,
        "selected_doctors": selected_doctors,
        "selected_assigned": selected_assigned,
        "selected_deal_statuses": selected_deal_statuses,
        "selected_appointment_statuses": selected_appointment_statuses,
        "selected_priorities": selected_priorities,
        "selected_temperatures": selected_temperatures,
        "selected_locations": selected_locations,
        "selected_stages": selected_stages,
        "date_from_val": request.GET.get("date_from", "") or request.GET.get("date", ""),
        "date_to_val": request.GET.get("date_to", ""),
        "current_sort": sort_by,
        "active_filters_count": active_filters_count,
        "request_get": request.GET,
    }

    if request.user.hospital:
        context["employees"] = User.objects.filter(hospital=request.user.hospital, is_active=True, is_approved=True)
    else:
        context["employees"] = User.objects.filter(is_active=True, is_approved=True)

    if request.user.hospital:
        context["hospital_campaigns"] = MasterGroup.get_active_choices("Campaigns").filter(hospital=request.user.hospital)
        context["hospital_sources"] = MasterGroup.get_active_choices("Lead Sources").filter(hospital=request.user.hospital)
        context["hospital_statuses"] = MasterGroup.get_active_choices("Deal Statuses").filter(hospital=request.user.hospital)

    template_name = "leads/hospital_lead_list.html" if request.user.hospital else "leads/academy_lead_list.html"
    return render(request, template_name, context)


@login_required
def lead_add(request):
    if request.user.role == User.Role.DOCTOR or not request.user.can_add_leads:
        messages.error(request, "Doctors cannot create new leads.")
        return redirect("dashboard:doctor_home")
        
    duplicates = None
    FormClass = HospitalLeadForm if request.user.hospital else LeadForm
    template = "leads/hospital_lead_form.html" if request.user.hospital else "leads/academy_lead_form.html"
    
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
                
            # If creator is a Lead Attendant, always assign to themselves
            if request.user.role == User.Role.LEAD_ATTENDENT:
                lead.assigned_to = request.user
            
            # Ensure defaults
            from leads.models import LeadStage, LeadSource, SourceCategory, Appointment, AppointmentStatus
            from notifications.models import Notification
            
            if not lead.stage_id:
                if lead.assigned_to:
                    lead.stage = LeadStage.objects.filter(name__iexact='Assigned').first() or LeadStage.objects.first()
                else:
                    lead.stage = LeadStage.objects.first()
                
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
        
    FormClass = HospitalLeadForm if lead.hospital else LeadForm
    template = "leads/hospital_lead_form.html" if lead.hospital else "leads/academy_lead_form.html"
    
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
            has_call_interaction = bool(
                cd.get('remark_1') or cd.get('calling_date_remark_1') or 
                cd.get('remark_2') or cd.get('calling_date_remark_2') or 
                cd.get('remark_3') or cd.get('calling_date_remark_3') or
                cd.get('appointment_status') in ['Booked', 'Cancelled', 'Confirmed', 'Completed', 'Visited']
            )
            
            try:
                # If payment is done / deal won, keep stage as Payment Done / Admission Done
                is_won = saved_lead.deal_status == DealStatus.WON or saved_lead.admission_status == AdmissionStatus.ADMISSION_DONE or bool(cd.get('total') and float(cd.get('total') or 0) > 0)
                if is_won:
                    won_stage = LeadStage.objects.filter(name__iexact='Admission').first() or \
                                LeadStage.objects.filter(name__iexact='Payment Done').first() or \
                                LeadStage.objects.filter(name__iexact='Visited').first()
                    if won_stage:
                        saved_lead.stage = won_stage
                        saved_lead.deal_status = DealStatus.WON
                        saved_lead.admission_status = AdmissionStatus.ADMISSION_DONE
                        cd['deal_status'] = 'Won (Payment Done)'
                        cd['appointment_status'] = 'Payment Done'
                        saved_lead.custom_data = cd
                elif cd.get('appointment_status'):
                    apt_st = cd.get('appointment_status')
                    cd['deal_status'] = apt_st
                    stage_match = LeadStage.objects.filter(name__iexact=apt_st).first()
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

            # 2. Notify Doctor if appointment exists for this lead
            apt = Appointment.objects.filter(lead=saved_lead).order_by('-id').first()
            if apt and apt.doctor_user and apt.doctor_user != request.user:
                time_str = apt.appointment_time.strftime('%I:%M %p') if apt.appointment_time else 'Slot not fixed'
                Notification.objects.create(
                    user=apt.doctor_user,
                    title="Appointment Update",
                    message=f"Patient {saved_lead.name} appointment scheduled for {apt.appointment_date.strftime('%d %b %Y')} at {time_str}.",
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
    
    # If appointment exists or lead has booked date/doctor and completed/confirmed status
    is_appointment_confirmed = False
    if latest_appointment:
        is_appointment_confirmed = True
    elif cd.get('appo_booked_date') or cd.get('doctor'):
        is_appointment_confirmed = True

    current_apt_status = cd.get("appointment_status") or ""
    if (is_appointment_confirmed or is_payment_done or is_appointment_completed) and (not current_apt_status or current_apt_status in ['Completed', 'Payment Done', 'Booked']):
        current_apt_status = "Booking"

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
        "is_payment_done": is_payment_done,
        "grand_total_paid": grand_total_paid,
        "billing_history_list": billing_history,
        "latest_appointment": latest_appointment,
        "all_appointments": Appointment.objects.filter(lead=lead).order_by("-appointment_date"),
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
    admission = getattr(lead, "admission", None)
    
    # Retrieve active/approved users for the assignment form
    employees = User.objects.filter(is_active=True, is_approved=True, role__in=['COUNSELLOR', 'HR', User.Role.MANAGER])
    managers = User.objects.filter(is_active=True, is_approved=True, role__in=[User.Role.SUPER_ADMIN, User.Role.MANAGER])
    
    latest_appointment = None
    custom_field_data = []
    if request.user.hospital:
        from leads.models import Appointment, LeadCustomField
        latest_appointment = Appointment.objects.filter(lead=lead).order_by('-id').first()
        cfs = LeadCustomField.objects.filter(hospital=request.user.hospital, is_active=True).order_by("order")
        cd = lead.custom_data or {}
        for cf in cfs:
            if cf.name in cd and cd[cf.name] != "":
                custom_field_data.append({"label": cf.label, "value": cd[cf.name]})
        
    template = "leads/hospital_lead_detail.html" if request.user.hospital else "leads/academy_lead_detail.html"
    return render(request, template, {
        "active": "leads_all", "lead": lead, "timeline": timeline, "admission": admission,
        "latest_appointment": latest_appointment,
        "custom_field_data": custom_field_data,
        "followup_modes": FollowUpMode.choices, "followup_statuses": FollowUpStatus.choices,
        "today": timezone.localdate(),
        "employees": employees,
        "managers": managers,
    })


def _can_archive_lead(user):
    if user.is_superuser:
        return True
    if user.role == User.Role.SUPER_ADMIN:
        return True
    if user.hospital and user.role in [User.Role.SUPER_ADMIN, User.Role.MANAGER]:
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
    messages.success(request, f"Lead {'archived' if lead.is_archived else 'restored'}.")
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
        Note.objects.create(lead=lead, note=request.POST["note"].strip(), created_by=request.user)
        if lead.assigned_to is None:
            lead.assigned_to = request.user
            lead.save(update_fields=["assigned_to"])
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
            lead.save(update_fields=["assigned_to"])
        messages.success(request, "Follow-up recorded.")
    return redirect("leads:lead_detail", pk=pk)


@login_required
def convert_admission(request, pk):
    lead = _get_lead_or_redirect(request, pk)
    if not lead:
        return redirect("leads:lead_list")
    if not _can_access_lead(request.user, lead):
        messages.error(request, "You do not have permission to access this lead.")
        return redirect("leads:lead_list")
    if hasattr(lead, "admission"):
        messages.info(request, "This lead already has an admission record.")
        return redirect("leads:lead_detail", pk=pk)
    if request.method == "POST":
        total_fee = float(request.POST.get("total_fee") or 0)
        discount = float(request.POST.get("discount") or 0)
        max_discount = float(lead.course.max_discount) if lead.course else 0.0
        extra_reason = request.POST.get("extra_discount_reason", "").strip()

        # Backend validation
        if discount > max_discount and not extra_reason:
            messages.error(request, "Reason for extra discount is required since the discount exceeds the course maximum allowed discount limit.")
            return redirect("leads:lead_detail", pk=pk)

        # Update Lead stage to Admission dynamically when converted
        admission_stage = LeadStage.objects.filter(name__icontains="admission", is_active=True).first()
        if admission_stage:
            lead.stage = admission_stage
            lead.deal_status = "WON"
            lead.save(update_fields=["stage", "deal_status"])

        Admission.objects.create(
            lead=lead,
            student_name=lead.name,
            course=lead.course,
            admission_date=request.POST.get("admission_date") or timezone.localdate(),
            total_fee=total_fee,
            discount=discount,
            max_allowed_discount=max_discount,
            extra_discount_reason=extra_reason if discount > max_discount else "",
            assigned_counselor=lead.assigned_to,
        )
        messages.success(request, "Lead converted to admission.")
        return redirect("admissions:list")
    return redirect("leads:lead_detail", pk=pk)


@login_required
def bulk_action(request):
    if request.method != "POST":
        return redirect("leads:lead_list")
    ids = request.POST.getlist("selected")
    action = request.POST.get("bulk_action")
    leads = Lead.objects.filter(pk__in=ids)
    if not leads.exists():
        messages.warning(request, "No leads selected.")
        return redirect("leads:lead_list")

    if action == "assign":
        if not request.user.can_assign_leads:
            messages.error(request, "You don't have permission to assign leads.")
            return redirect("leads:lead_list")
        emp_id = request.POST.get("assign_to")
        leads.update(assigned_to_id=emp_id or None)
        messages.success(request, f"{leads.count()} lead(s) assigned.")
    elif action == "stage":
        stage_id = request.POST.get("stage")
        leads.update(stage_id=stage_id)
        messages.success(request, f"{leads.count()} lead(s) updated.")
    elif action == "archive":
        if not _can_archive_lead(request.user):
            messages.error(request, "Only Hospital Admins and Zappcode Super Admins can archive leads.")
            return redirect("leads:lead_list")
        leads.update(is_archived=True)
        messages.success(request, f"{leads.count()} lead(s) archived.")
    return redirect("leads:lead_list")


@login_required
def duplicates(request):
    all_leads = list(Lead.objects.select_related("stage", "assigned_to", "lead_source").filter(is_archived=False))
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
    if request.method == "POST":
        form = CourseForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "New Course added to Master.")
            return redirect("leads:course_master")
        else:
            messages.error(request, f"Could not add course: {form.errors.as_text()}")
        return redirect("leads:course_master")

    # Display official courses (exclude legacy unformatted import courses)
    courses = Course.objects.exclude(is_active=False, base_price=0)
    form = CourseForm()
    return render(request, "leads/course_master.html", {
        "active": "course_master",
        "courses": courses,
        "form": form
    })


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
            messages.success(request, "Course updated.")
            return redirect("leads:course_master")
        else:
            messages.error(request, f"Could not update: {form.errors.as_text()}")
    else:
        form = CourseForm(instance=course)
    return render(request, "leads/course_edit.html", {
        "form": form, "course": course, "active": "course_master"
    })


@login_required
def assign_lead(request, pk):
    lead = get_object_or_404(Lead, pk=pk)
    if not request.user.can_assign_leads:
        raise PermissionDenied("You do not have permission to assign leads.")
    if request.method == "POST":
        assigned_to_id = request.POST.get("assigned_to")
        assigned_manager_id = request.POST.get("assigned_manager")
        
        # Update assigned_to
        if assigned_to_id:
            lead.assigned_to_id = assigned_to_id
            try:
                assigned_stage = LeadStage.objects.filter(name__iexact='Assigned').first() or LeadStage.objects.create(name='Assigned', order=2)
                if not lead.stage or lead.stage.name.lower() in ['new', 'fresh', 'uncontacted']:
                    lead.stage = assigned_stage
            except Exception:
                pass
        else:
            lead.assigned_to = None
            
        # Update assigned_manager
        if assigned_manager_id:
            lead.assigned_manager_id = assigned_manager_id
        else:
            lead.assigned_manager = None
            
        lead.save()
        messages.success(request, "Lead assignment updated successfully.")
    return redirect("leads:lead_detail", pk=pk)


# ---------------------------------------------------------------------------
# Universal Master Management (Master & Sub-Master System)
# ---------------------------------------------------------------------------

@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def universal_master_list(request):
    groups = MasterGroup.objects.prefetch_related("items").all()
    selected_group_id = request.GET.get("group_id")
    
    selected_group = None
    if selected_group_id:
        selected_group = groups.filter(pk=selected_group_id).first()
    if not selected_group and groups.exists():
        selected_group = groups.first()

    items = []
    if selected_group:
        if request.user.hospital:
            items = selected_group.items.filter(hospital=request.user.hospital)
        else:
            items = selected_group.items.filter(hospital__isnull=True)

    from leads.models import LeadCustomField
    h = request.user.hospital
    
    # Ensure default core fields exist in DB for this hospital if not yet initialized
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
        ('campaign', 'Campaign', 'DROPDOWN', 11, False, True, 'Select Campaign', 'Summer Health Checkup, Cardiology Camp, Free OPD Camp, Digital Awareness 2026')
    ]
    if h:
        for f_name, f_lbl, f_type, f_ord, f_req, f_act, f_ph, f_opt in core_field_defs:
            if not LeadCustomField.objects.filter(hospital=h, name=f_name).exists():
                LeadCustomField.objects.create(
                    hospital=h, name=f_name, label=f_lbl, field_type=f_type,
                    order=f_ord, is_required=f_req, is_active=f_act,
                    placeholder=f_ph, options=f_opt, is_system=True
                )

    if h:
        # Automatically sync current options for special master fields so admin modal displays them
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

        all_fields_qs = LeadCustomField.objects.filter(hospital=h).order_by('order', 'id')
    else:
        all_fields_qs = LeadCustomField.objects.filter(hospital__isnull=True).order_by('order', 'id')

    return render(request, "leads/universal_masters.html", {
        "active": "universal_masters",
        "all_fields": all_fields_qs,
    })


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def custom_field_add(request):
    from leads.models import LeadCustomField
    from django.utils.text import slugify
    from django.db.models import Max, F
    if request.method == "POST":
        label = request.POST.get("label", "").strip()
        field_type = request.POST.get("field_type", "TEXT")
        options = request.POST.get("options", "").strip()
        placeholder = request.POST.get("placeholder", "").strip()
        help_text = request.POST.get("help_text", "").strip()
        is_required = request.POST.get("is_required") == "on"
        order_raw = request.POST.get("order", "").strip()

        qs = LeadCustomField.objects.filter(hospital=request.user.hospital)

        try:
            order_val = int(order_raw) if order_raw else None
        except ValueError:
            order_val = None

        if order_val is None or order_val <= 0:
            # Add to the end: max order + 1
            max_order = qs.aggregate(m=Max('order'))['m'] or 0
            order = max_order + 1
        else:
            order = order_val
            # Shift all subsequent fields with order >= specified order by +1
            qs.filter(order__gte=order).update(order=F('order') + 1)

        if label:
            name = slugify(label).replace("-", "_")
            # Ensure unique name per hospital
            base_name = name
            count = 1
            while LeadCustomField.objects.filter(hospital=request.user.hospital, name=name).exists():
                name = f"{base_name}_{count}"
                count += 1

            LeadCustomField.objects.create(
                hospital=request.user.hospital,
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
            return redirect("/leads/universal-masters/?tab=custom_fields")
        messages.error(request, "Field label is required.")
    return redirect("/leads/universal-masters/?tab=custom_fields")



@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def custom_field_edit(request, pk):
    from leads.models import LeadCustomField
    field = get_object_or_404(LeadCustomField, pk=pk)
    if field.hospital != request.user.hospital:
        messages.error(request, "Permission denied.")
        return redirect("/leads/universal-masters/?tab=custom_fields")
        
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
            
            # Fetch all fields in current order
            all_fields = list(LeadCustomField.objects.filter(hospital=request.user.hospital).order_by('order', 'id'))
            
            if order > 0 and order != old_order:
                # Remove field from current list position
                all_fields = [f for f in all_fields if f.pk != field.pk]
                # Insert at new 0-indexed position (order - 1)
                insert_idx = max(0, min(order - 1, len(all_fields)))
                all_fields.insert(insert_idx, field)
                
                # Reassign clean contiguous orders: 1, 2, 3...
                for idx, f in enumerate(all_fields):
                    f.order = idx + 1
                    if f.pk == field.pk:
                        field.order = idx + 1
                        field.save()
                    else:
                        f.save(update_fields=['order'])
            else:
                field.save()

            # Automatically sync updated options into corresponding MasterGroup/MasterItems
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
                    # Deactivate or remove old items not in new list
                    mg.items.filter(hospital=request.user.hospital).exclude(name__in=new_opts).delete()
                    for idx, opt_name in enumerate(new_opts):
                        MasterItem.objects.update_or_create(
                            group=mg,
                            hospital=request.user.hospital,
                            name=opt_name,
                            defaults={"order": idx + 1, "is_active": True}
                        )
                
            messages.success(request, f"Form field '{label}' updated successfully.")
        return redirect("/leads/universal-masters/")
    return redirect("/leads/universal-masters/")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def custom_field_toggle(request, pk):
    from leads.models import LeadCustomField
    field = get_object_or_404(LeadCustomField, pk=pk)
    if field.hospital != request.user.hospital:
        messages.error(request, "Permission denied.")
        return redirect("/leads/universal-masters/?tab=custom_fields")
        
    field.is_active = not field.is_active
    field.save(update_fields=["is_active"])
    messages.success(request, f"Field '{field.label}' is now {'Active' if field.is_active else 'Hidden'}.")
    return redirect("/leads/universal-masters/?tab=custom_fields")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def custom_field_delete(request, pk):
    from leads.models import LeadCustomField
    field = get_object_or_404(LeadCustomField, pk=pk)
    if field.hospital != request.user.hospital:
        messages.error(request, "Permission denied.")
        return redirect("/leads/universal-masters/?tab=custom_fields")
        
    label = field.label
    field.delete()
    messages.success(request, f"Custom form field '{label}' removed from lead form.")
    return redirect("/leads/universal-masters/?tab=custom_fields")


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
        try:
            order = int(order)
        except ValueError:
            order = 0

        if name:
            item, created = MasterItem.objects.get_or_create(
                group=group, name=name, hospital=request.user.hospital, 
                defaults={"code": code, "order": order, "is_active": True}
            )
            if created:
                messages.success(request, f"Sub-Master item '{name}' added to {group.name}.")
            else:
                messages.warning(request, f"Item '{name}' already exists in {group.name}.")
            return redirect(f"/leads/universal-masters/?group_id={group.pk}")
        messages.error(request, "Item name is required.")
    return redirect("leads:universal_masters")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def master_item_edit(request, pk):
    item = get_object_or_404(MasterItem, pk=pk)
    if item.hospital != request.user.hospital:
        messages.error(request, "You do not have permission to edit this item.")
        return redirect("leads:universal_masters")
        
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
            messages.success(request, f"Item '{item.name}' updated.")
        return redirect(f"/leads/universal-masters/?group_id={item.group.pk}")
    return redirect("leads:universal_masters")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def master_item_toggle(request, pk):
    item = get_object_or_404(MasterItem, pk=pk)
    if item.hospital != request.user.hospital:
        messages.error(request, "You do not have permission to modify this item.")
        return redirect("leads:universal_masters")
        
    item.is_active = not item.is_active
    item.save(update_fields=["is_active"])
    messages.success(request, f"Status for '{item.name}' changed to {'Active' if item.is_active else 'Inactive'}.")
    return redirect(f"/leads/universal-masters/?group_id={item.group.pk}")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def master_item_delete(request, pk):
    item = get_object_or_404(MasterItem, pk=pk)
    if item.hospital != request.user.hospital:
        messages.error(request, "You do not have permission to delete this item.")
        return redirect("leads:universal_masters")
        
    group_id = item.group.pk
    name = item.name
    item.delete()
    messages.success(request, f"Sub-Master item '{name}' deleted.")
    return redirect(f"/leads/universal-masters/?group_id={group_id}")


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
def lead_self_assign(request, pk):
    from accounts.models import User
    from django.core.exceptions import PermissionDenied
    lead = get_object_or_404(Lead, pk=pk)
    
    if not _can_access_lead(request.user, lead):
        raise PermissionDenied("You do not have permission to access this lead.")
        
    if request.user.role != User.Role.LEAD_ATTENDENT:
        messages.error(request, "Only Lead Attendants can self-assign leads.")
        return redirect('leads:lead_detail', pk=pk)
        
    if lead.assigned_to is not None:
        messages.error(request, "This lead is already assigned to someone else.")
        return redirect('leads:lead_detail', pk=pk)
        
    if request.method == "POST":
        lead.assigned_to = request.user
        try:
            assigned_stage = LeadStage.objects.filter(name__iexact='Assigned').first() or LeadStage.objects.create(name='Assigned', order=2)
            if not lead.stage or lead.stage.name.lower() in ['new', 'fresh', 'uncontacted']:
                lead.stage = assigned_stage
        except Exception:
            pass
        lead.save()
        messages.success(request, "Lead successfully assigned to you and added to My Leads.")
        
    return redirect('leads:lead_detail', pk=pk)

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

            branches_created = 0
            depts_created = 0
            docs_created = 0
            diseases_created = 0
            rows_processed = 0

            for _, row in df.iterrows():
                branch_name = str(row.get(branch_col, "")).strip() if branch_col and pd.notna(row.get(branch_col)) else ""
                dept_name = str(row.get(dept_col, "")).strip() if dept_col and pd.notna(row.get(dept_col)) else ""
                doc_name = str(row.get(doc_col, "")).strip() if doc_col and pd.notna(row.get(doc_col)) else ""
                disease_name = str(row.get(dis_col, "")).strip() if dis_col and pd.notna(row.get(dis_col)) else ""

                if not dept_name and not doc_name and not disease_name and not branch_name:
                    continue

                rows_processed += 1
                branch_obj = None
                dept_obj = None
                doc_obj = None
                disease_obj = None

                # 1. Branch
                if branch_name and branch_name.lower() not in ["nan", "none", ""]:
                    branch_obj, b_created = HospitalBranch.objects.get_or_create(
                        hospital=hospital,
                        name=branch_name,
                        defaults={"is_active": True}
                    )
                    if b_created:
                        branches_created += 1

                # 2. Department
                if dept_name and dept_name.lower() not in ["nan", "none", ""]:
                    dept_obj, d_created = HospitalDepartment.objects.get_or_create(
                        hospital=hospital,
                        name=dept_name,
                        defaults={"is_active": True}
                    )
                    if d_created:
                        depts_created += 1
                    if branch_obj and dept_obj:
                        dept_obj.branches.add(branch_obj)

                # 3. Doctor
                if doc_name and doc_name.lower() not in ["nan", "none", ""]:
                    clean_doc_name = doc_name
                    if clean_doc_name.lower().startswith("dr.") or clean_doc_name.lower().startswith("dr "):
                        clean_doc_name = clean_doc_name[3:].strip()

                    doc_obj = HospitalDoctor.objects.filter(
                        hospital=hospital,
                        name__iexact=clean_doc_name
                    ).first()

                    if not doc_obj:
                        doc_obj = HospitalDoctor.objects.create(
                            hospital=hospital,
                            name=clean_doc_name,
                            department=dept_obj,
                            is_active=True
                        )
                        docs_created += 1

                    if dept_obj:
                        doc_obj.departments.add(dept_obj)
                        if not doc_obj.department:
                            doc_obj.department = dept_obj
                            doc_obj.save(update_fields=["department"])

                    if branch_obj:
                        DoctorBranchAvailability.objects.get_or_create(
                            doctor=doc_obj,
                            branch=branch_obj,
                            defaults={"is_active": True}
                        )

                # 4. Disease / Condition
                if disease_name and disease_name.lower() not in ["nan", "none", ""] and dept_obj:
                    disease_obj, dis_created = HospitalDisease.objects.get_or_create(
                        hospital=hospital,
                        department=dept_obj,
                        name=disease_name,
                        defaults={"is_active": True}
                    )
                    if dis_created:
                        diseases_created += 1

                    if doc_obj and disease_obj:
                        doc_obj.associated_diseases.add(disease_obj)

            messages.success(
                request,
                f"Excel Import Successful! Processed {rows_processed} rows. "
                f"Added {branches_created} branches, {depts_created} departments, "
                f"{docs_created} doctors, {diseases_created} conditions."
            )
        except Exception as ex:
            messages.error(request, f"Failed to import Excel/CSV file: {str(ex)}")

    return redirect("/leads/hospital-configuration/")


@login_required
def hospital_master_sample_download(request):
    """Download Sample Excel / CSV template for Hospital Master Configuration"""
    from django.http import HttpResponse
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


