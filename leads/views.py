from collections import defaultdict
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone

from followups.models import FollowUp, Note, Activity, FollowUpMode, FollowUpStatus
from admissions.models import Admission
from accounts.models import User
from .models import Lead, SourceCategory, LeadSource, Campaign, Course, LeadStage, Tag, MasterGroup, MasterItem
from .forms import (
    LeadForm, HospitalLeadForm, SourceCategoryForm, LeadSourceForm, CampaignForm, CourseForm, LeadStageForm,
)


def _can_edit_lead(user, lead):
    if user.can_edit_any_lead:
        return True
    if user.can_edit_own_leads and lead.assigned_to == user:
        return True
    # Allow hospital users to edit unassigned leads in their hospital
    if user.hospital and lead.hospital == user.hospital and lead.assigned_to is None:
        return True
    return False

def _can_access_lead(user, lead):
    if user.can_view_all_leads:
        return True
    if user.can_view_team_leads:
        team = User.objects.filter(reports_to=user)
        if lead.assigned_to == user or lead.assigned_to in team:
            return True
    if user.can_view_assigned_leads and lead.assigned_to == user:
        return True
    # Allow hospital users to view unassigned leads in their hospital (like New Enquiries)
    if user.hospital and lead.hospital == user.hospital and lead.assigned_to is None:
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

    q = request.GET.get("q", "").strip()
    if q:
        leads = leads.filter(
            Q(lead_code__icontains=q) | Q(name__icontains=q) | Q(mobile__icontains=q)
            | Q(email__icontains=q) | Q(city__icontains=q) | Q(course__name__icontains=q)
            | Q(lead_source__name__icontains=q) | Q(campaign__name__icontains=q)
        )

    for field in FK_FILTER_FIELDS:
        if field == "import_job":
            continue
        val = request.GET.get(field)
        if val:
            if request.user.hospital and field in ['campaign', 'lead_source', 'source_category']:
                leads = leads.filter(**{f"custom_data__{field}": val})
            else:
                leads = leads.filter(**{f"{field}_id": val})

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

    for field in CHAR_FILTER_FIELDS:
        val = request.GET.get(field)
        if val:
            if request.user.hospital and field in ['deal_status', 'temperature', 'admission_status']:
                leads = leads.filter(**{f"custom_data__{field}": val})
            else:
                leads = leads.filter(**{field: val})

    city = request.GET.get("city")
    if city:
        leads = leads.filter(city__iexact=city)
        
    location = request.GET.get("location")
    if location:
        leads = leads.filter(location__iexact=location)

    def _parse_date_input(val):
        if not val:
            return None
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
        # Match leads that have revenue (custom_data total, Admission payments, or Won status)
        leads = leads.filter(
            (Q(custom_data__total__isnull=False) & ~Q(custom_data__total__in=["0", "0.00", "", "0.0", 0, 0.0])) |
            Q(admission__payments__payment_status='SUCCESS', admission__payments__amount__gt=0) |
            Q(deal_status='WON') |
            Q(admission_status='ADMISSION_DONE')
        ).distinct()

    paginator = Paginator(leads, 25)
    page = paginator.get_page(request.GET.get("page"))

    query_params = request.GET.copy()
    if 'page' in query_params:
        del query_params['page']

    # Filter dropdown options to only those that have at least one lead associated
    active_leads = Lead.objects.filter(is_archived=False)
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
    
    import_job_id = request.GET.get("import_job")
    selected_import_job = None
    if import_job_id:
        from imports.models import ImportJob
        selected_import_job = ImportJob.objects.filter(pk=import_job_id).first()

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

    return render(request, "leads/lead_list.html", context)


@login_required
def lead_add(request):
    if request.user.role == User.Role.DOCTOR or not request.user.can_add_leads:
        messages.error(request, "Doctors cannot create new leads.")
        return redirect("dashboard:doctor_home")
        
    duplicates = None
    FormClass = HospitalLeadForm if request.user.hospital else LeadForm
    template = "leads/hospital_lead_form.html" if request.user.hospital else "leads/lead_form.html"
    
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
                
            # If creator is a Lead Attendant and assigned_to wasn't set, assign to them
            if request.user.role == User.Role.LEAD_ATTENDENT and not lead.assigned_to:
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
        if request.user.role == User.Role.LEAD_ATTENDENT:
            return redirect("dashboard:telecaller_my_leads")
        return redirect("leads:lead_list")

    if not _can_edit_lead(request.user, lead):
        messages.error(request, "You do not have permission to edit this lead.")
        return redirect("leads:lead_list")
        
    FormClass = HospitalLeadForm if request.user.hospital else LeadForm
    template = "leads/hospital_lead_form.html" if request.user.hospital else "leads/lead_form.html"
    
    if request.method == "POST":
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
                cd.get('appointment_status') in ['Booked', 'Cancelled']
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

            messages.success(request, "Lead updated successfully.")
            return redirect("leads:lead_detail", pk=lead.pk)
    else:
        form = FormClass(instance=lead, user=request.user)

    # Check if appointment is completed either via Lead custom_data or Appointment model
    from leads.models import Appointment, AppointmentStatus
    cd = lead.custom_data or {}
    apt_status_str = str(cd.get('appointment_status', '')).upper()
    has_completed_apt = Appointment.objects.filter(lead=lead, status=AppointmentStatus.COMPLETED).exists()
    is_appointment_completed = 'COMPLET' in apt_status_str or 'DONE' in apt_status_str or 'VISIT' in apt_status_str or has_completed_apt

    return render(request, template, {
        "active": "leads_all",
        "form": form,
        "mode": "Edit",
        "obj": lead,
        "is_appointment_completed": is_appointment_completed,
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
        
    template = "leads/hospital_lead_detail.html" if request.user.hospital else "leads/lead_detail.html"
    return render(request, template, {
        "active": "leads_all", "lead": lead, "timeline": timeline, "admission": admission,
        "latest_appointment": latest_appointment,
        "custom_field_data": custom_field_data,
        "followup_modes": FollowUpMode.choices, "followup_statuses": FollowUpStatus.choices,
        "today": timezone.localdate(),
        "employees": employees,
        "managers": managers,
    })


@login_required
def lead_archive(request, pk):
    lead = _get_lead_or_redirect(request, pk)
    if not lead:
        return redirect("leads:lead_list")
    if not _can_access_lead(request.user, lead):
        messages.error(request, "You do not have permission to access this lead.")
        return redirect("leads:lead_list")
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
        FollowUp.objects.create(
            lead=lead,
            followup_date=request.POST.get("followup_date") or timezone.localdate(),
            followup_time=request.POST.get("followup_time") or None,
            followup_mode=request.POST.get("followup_mode", FollowUpMode.CALL),
            followup_status=request.POST.get("followup_status", FollowUpStatus.COMPLETED),
            comment=request.POST.get("comment", ""),
            next_followup_date=request.POST.get("next_followup_date") or None,
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
    if request.user.hospital:
        custom_fields = LeadCustomField.objects.filter(hospital=request.user.hospital)
    else:
        custom_fields = LeadCustomField.objects.filter(hospital__isnull=True)

    return render(request, "leads/universal_masters.html", {
        "active": "universal_masters",
        "groups": groups,
        "selected_group": selected_group,
        "items": items,
        "custom_fields": custom_fields,
        "tab": request.GET.get("tab", "masters"),
    })


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def custom_field_add(request):
    from leads.models import LeadCustomField
    from django.utils.text import slugify
    if request.method == "POST":
        label = request.POST.get("label", "").strip()
        field_type = request.POST.get("field_type", "TEXT")
        options = request.POST.get("options", "").strip()
        placeholder = request.POST.get("placeholder", "").strip()
        help_text = request.POST.get("help_text", "").strip()
        is_required = request.POST.get("is_required") == "on"
        order = request.POST.get("order", 0)
        try:
            order = int(order)
        except ValueError:
            order = 0

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
            messages.success(request, f"New custom form field '{label}' added successfully.")
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
            field.label = label
            field.field_type = field_type
            field.options = options
            field.placeholder = placeholder
            field.help_text = help_text
            field.is_required = is_required
            field.is_active = is_active
            field.order = order
            field.save()
            messages.success(request, f"Custom form field '{label}' updated successfully.")
        return redirect("/leads/universal-masters/?tab=custom_fields")
    return redirect("/leads/universal-masters/?tab=custom_fields")


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

