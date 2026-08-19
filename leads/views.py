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
    from accounts.models import User
    if user.role in (User.Role.SUPER_ADMIN, User.Role.MANAGER):
        return True
    return lead.assigned_to == user

def _can_access_lead(user, lead):
    from accounts.models import User
    if user.role in (User.Role.SUPER_ADMIN, User.Role.MANAGER):
        return True
    if user.role == User.Role.LEAD_ATTENDENT and user.hospital == lead.hospital:
        return True
    return lead.assigned_to == user


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

    if request.user.role in ('COUNSELLOR', 'HR'):
        leads = leads.filter(assigned_to=request.user)
    elif request.user.hospital:
        leads = leads.filter(hospital=request.user.hospital)

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
            leads = leads.filter(**{field: val})

    city = request.GET.get("city")
    if city:
        leads = leads.filter(city__iexact=city)

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
        "employees": User.objects.filter(id__in=used_emp_ids),
        "cities": distinct_cities,
        "request_get": request.GET,
    }
    return render(request, "leads/lead_list.html", context)


@login_required
def lead_add(request):
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
            
            # Ensure defaults
            from leads.models import LeadStage, LeadSource, SourceCategory
            if not lead.stage_id:
                lead.stage = LeadStage.objects.first()
                
            lead.save()
            form.save_m2m()
            messages.success(request, "Lead created successfully.")
            return redirect("leads:lead_detail", pk=lead.pk)
    else:
        from django.utils import timezone
        form = FormClass(initial={"inquiry_date": timezone.localdate()}, user=request.user)
        
    return render(request, template, {
        "active": "leads_add", "form": form, "mode": "Add", "duplicates": duplicates,
    })

@login_required
@login_required
def lead_edit(request, pk):
    lead = get_object_or_404(Lead, pk=pk)
    if not _can_edit_lead(request.user, lead):
        raise PermissionDenied("You do not have permission to edit this lead. It may be assigned to someone else.")
        
    FormClass = HospitalLeadForm if request.user.hospital else LeadForm
    template = "leads/hospital_lead_form.html" if request.user.hospital else "leads/lead_form.html"
    
    if request.method == "POST":
        form = FormClass(request.POST, instance=lead, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Lead updated.")
            return redirect("leads:lead_detail", pk=lead.pk)
    else:
        form = FormClass(instance=lead, user=request.user)
    return render(request, template, {"active": "leads_all", "form": form, "mode": "Edit", "obj": lead})


@login_required
def lead_detail(request, pk):
    lead = get_object_or_404(
        Lead.objects.select_related(
            "course", "stage", "lead_source", "source_category", "campaign",
            "assigned_to", "assigned_manager", "original_lead_source", "original_source_category", "original_campaign",
        ), pk=pk
    )
    if not _can_access_lead(request.user, lead):
        raise PermissionDenied("You do not have permission to access this lead.")
    timeline = lead.activities.all()[:200]
    admission = getattr(lead, "admission", None)
    
    # Retrieve active/approved users for the assignment form
    employees = User.objects.filter(is_active=True, is_approved=True, role__in=['COUNSELLOR', 'HR', User.Role.MANAGER])
    managers = User.objects.filter(is_active=True, is_approved=True, role__in=[User.Role.SUPER_ADMIN, User.Role.MANAGER])
    
    return render(request, "leads/lead_detail.html", {
        "active": "leads_all", "lead": lead, "timeline": timeline, "admission": admission,
        "followup_modes": FollowUpMode.choices, "followup_statuses": FollowUpStatus.choices,
        "today": timezone.localdate(),
        "employees": employees,
        "managers": managers,
    })


@login_required
def lead_archive(request, pk):
    lead = get_object_or_404(Lead, pk=pk)
    if not _can_access_lead(request.user, lead):
        raise PermissionDenied("You do not have permission to access this lead.")
    lead.is_archived = not lead.is_archived
    lead.save(update_fields=["is_archived"])
    messages.success(request, f"Lead {'archived' if lead.is_archived else 'restored'}.")
    return redirect("leads:lead_detail", pk=pk)


@login_required
def add_note(request, pk):
    lead = get_object_or_404(Lead, pk=pk)
    if not _can_access_lead(request.user, lead):
        raise PermissionDenied("You do not have permission to access this lead.")
    if request.method == "POST" and request.POST.get("note", "").strip():
        Note.objects.create(lead=lead, note=request.POST["note"].strip(), created_by=request.user)
        messages.success(request, "Note added.")
    return redirect("leads:lead_detail", pk=pk)


@login_required
def add_followup(request, pk):
    lead = get_object_or_404(Lead, pk=pk)
    if not _can_access_lead(request.user, lead):
        raise PermissionDenied("You do not have permission to access this lead.")
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
        messages.success(request, "Follow-up recorded.")
    return redirect("leads:lead_detail", pk=pk)


@login_required
def convert_admission(request, pk):
    lead = get_object_or_404(Lead, pk=pk)
    if not _can_access_lead(request.user, lead):
        raise PermissionDenied("You do not have permission to access this lead.")
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

    items = selected_group.items.all() if selected_group else []

    return render(request, "leads/universal_masters.html", {
        "active": "universal_masters",
        "groups": groups,
        "selected_group": selected_group,
        "items": items,
    })


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
                group=group, name=name, defaults={"code": code, "order": order, "is_active": True}
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
    item.is_active = not item.is_active
    item.save(update_fields=["is_active"])
    messages.success(request, f"Status for '{item.name}' changed to {'Active' if item.is_active else 'Inactive'}.")
    return redirect(f"/leads/universal-masters/?group_id={item.group.pk}")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def master_item_delete(request, pk):
    item = get_object_or_404(MasterItem, pk=pk)
    group_id = item.group.pk
    name = item.name
    item.delete()
    messages.success(request, f"Sub-Master item '{name}' deleted.")
    return redirect(f"/leads/universal-masters/?group_id={group_id}")


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
        lead.save(update_fields=['assigned_to'])
        messages.success(request, "Lead successfully assigned to you.")
        
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
