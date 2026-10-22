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
from leads.models import (
    Lead, SourceCategory, LeadSource, Campaign, Course, Trainer, Batch, LeadStage, Tag, 
    MasterGroup, MasterItem, HospitalBranch, HospitalDepartment, HospitalDoctor, 
    HospitalDisease, DoctorBranchAvailability, DealStatus, LeadTemperature,
    AdmissionStatus, Appointment, AppointmentStatus,
)
from leads.forms import (
    LeadForm, HospitalLeadForm, SourceCategoryForm, LeadSourceForm, CampaignForm, CourseForm, TrainerForm, BatchForm, LeadStageForm,
)




# ===========================================================================
# 🎓 ZAPPCODE ACADEMY LEAD CYCLE & ACADEMY MASTERS
# ===========================================================================

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

        # Update Lead stage to Admission Done dynamically when converted
        admission_stage = LeadStage.objects.filter(name__iexact="Admission Done", is_active=True).first() or \
                          LeadStage.objects.filter(name__icontains="admission", is_active=True).first()
        if admission_stage:
            lead.stage = admission_stage
        lead.deal_status = DealStatus.WON
        lead.admission_status = AdmissionStatus.WON
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
def update_admission_status(request, pk):
    """
    Directly updates the admission status for Academy leads from the lead view/detail page.
    Automatically updates temperature to Freeze and stage to Cancelled if set to LOST.
    """
    lead = _get_lead_or_redirect(request, pk)
    if not lead:
        return redirect("leads:lead_list")
    
    can_edit = _can_edit_lead(request.user, lead)
    if not can_edit:
        messages.error(request, "Only the assigned counselor or manager can change this lead's admission status.")
        return redirect("leads:lead_detail", pk=pk)

    if request.method == "POST":
        new_status = request.POST.get("admission_status", "").strip().upper()
        if new_status in AdmissionStatus.values:
            old_status_disp = lead.get_admission_status_display()
            lead.admission_status = new_status
            
            if new_status == AdmissionStatus.LOST:
                if lead.is_academy_industry:
                    lead.temperature = LeadTemperature.FREEZE
                    cancelled_stage = (
                        LeadStage.objects.filter(name__iexact="Cancelled", is_active=True, business_type=LeadStage.BusinessType.ACADEMY).first()
                        or LeadStage.objects.filter(name__iexact="Cancelled", is_active=True).first()
                        or LeadStage.objects.filter(name__iexact="Lost", is_active=True).first()
                    )
                    if cancelled_stage:
                        lead.stage = cancelled_stage
                lead.deal_status = DealStatus.LOST
            elif new_status == AdmissionStatus.WON:
                lead.deal_status = DealStatus.WON
                lead.temperature = LeadTemperature.HOT
                if lead.is_academy_industry:
                    won_stage = (
                        LeadStage.objects.filter(name__iexact="Admission Done", is_active=True, business_type=LeadStage.BusinessType.ACADEMY).first()
                        or LeadStage.objects.filter(name__iexact="Admission Done", is_active=True).first()
                        or LeadStage.objects.filter(name__iexact="Payment Done", is_active=True).first()
                    )
                    if won_stage:
                        lead.stage = won_stage
            elif new_status == AdmissionStatus.OPEN:
                lead.deal_status = DealStatus.OPEN
                lead.temperature = LeadTemperature.WARM
                if lead.is_academy_industry and (not lead.stage or lead.stage.name.lower() in ("cancelled", "lost", "admission done", "payment done")):
                    # Revert stage from Cancelled to Follow up (if followups exist) or Assigned
                    if lead.followups.exists():
                        revert_stg = LeadStage.objects.filter(name__iexact="Follow up", is_active=True, business_type=LeadStage.BusinessType.ACADEMY).first()
                    else:
                        revert_stg = LeadStage.objects.filter(name__iexact="Assigned", is_active=True, business_type=LeadStage.BusinessType.ACADEMY).first() or \
                                     LeadStage.objects.filter(name__iexact="New", is_active=True, business_type=LeadStage.BusinessType.ACADEMY).first()
                    if revert_stg:
                        lead.stage = revert_stg

            lead.save()

            # Record Activity
            Activity.objects.create(
                lead=lead,
                created_by=request.user,
                activity_type=ActivityType.STAGE_CHANGE if hasattr(ActivityType, "STAGE_CHANGE") else "STAGE_CHANGE",
                description=f"Admission status updated from {old_status_disp} to {lead.get_admission_status_display()}.",
            )
            messages.success(request, f"Admission status updated to {lead.get_admission_status_display()}.")
        else:
            messages.error(request, "Invalid admission status selected.")

    return redirect("leads:lead_detail", pk=pk)


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

    return render(request, "academy/masters/course_master.html", {
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
        "trainer": Trainer, "batch": Batch,
    }.get(kind)
    if model:
        obj = get_object_or_404(model, pk=pk)
        obj.is_active = not obj.is_active
        obj.save(update_fields=["is_active"])
    if kind == "course":
        return redirect("leads:course_master")
    elif kind == "trainer":
        return redirect("leads:trainer_master")
    elif kind == "batch":
        return redirect("leads:batch_master")
    return redirect("leads:masters")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def trainer_master(request):
    user_hospital = request.user.hospital
    active_biz_id = request.GET.get("business", "").strip() or str(request.session.get("active_business_id", "")).strip()

    target_biz = None
    if user_hospital:
        target_biz = user_hospital
    elif active_biz_id and active_biz_id.isdigit():
        target_biz = Hospital.objects.filter(id=int(active_biz_id), is_active=True).first()
    else:
        # Default to first academy business if superadmin
        target_biz = Hospital.objects.filter(industry="ACADEMY", is_active=True).first()

    if request.method == "POST":
        trainer_id = request.POST.get("trainer_id")
        if trainer_id and str(trainer_id).isdigit():
            trainer_obj = get_object_or_404(Trainer, pk=int(trainer_id))
            form = TrainerForm(request.POST, instance=trainer_obj)
        else:
            form = TrainerForm(request.POST)

        if form.is_valid():
            trainer = form.save(commit=False)
            if target_biz:
                trainer.hospital = target_biz
            trainer.save()
            action = "updated" if trainer_id else "created"
            messages.success(request, f"Trainer '{trainer.name}' {action} successfully! 🎓")
            return redirect("leads:trainer_master")
        else:
            messages.error(request, f"Could not save trainer: {form.errors.as_text()}")
            return redirect("leads:trainer_master")

    trainers = Trainer.objects.all().order_by("name")
    if target_biz:
        trainers = trainers.filter(models.Q(hospital=target_biz) | models.Q(hospital__isnull=True))

    all_businesses = Hospital.objects.filter(is_active=True, industry="ACADEMY").order_by("name")
    form = TrainerForm()

    return render(request, "academy/masters/trainer_master.html", {
        "active": "trainer_master",
        "trainers": trainers,
        "form": form,
        "target_biz": target_biz,
        "all_businesses": all_businesses,
        "active_count": trainers.filter(is_active=True).count(),
        "total_count": trainers.count(),
    })


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def trainer_delete(request, pk):
    trainer = get_object_or_404(Trainer, pk=pk)
    name = trainer.name
    trainer.delete()
    messages.success(request, f"Trainer '{name}' deleted successfully.")
    return redirect("leads:trainer_master")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def batch_master(request):
    user_hospital = request.user.hospital
    active_biz_id = request.GET.get("business", "").strip() or str(request.session.get("active_business_id", "")).strip()

    target_biz = None
    if user_hospital:
        target_biz = user_hospital
    elif active_biz_id and active_biz_id.isdigit():
        target_biz = Hospital.objects.filter(id=int(active_biz_id), is_active=True).first()
    else:
        target_biz = Hospital.objects.filter(industry="ACADEMY", is_active=True).first()

    if request.method == "POST":
        batch_id = request.POST.get("batch_id")
        if batch_id and str(batch_id).isdigit():
            batch_obj = get_object_or_404(Batch, pk=int(batch_id))
            form = BatchForm(request.POST, instance=batch_obj)
        else:
            form = BatchForm(request.POST)

        if form.is_valid():
            batch = form.save(commit=False)
            if target_biz:
                batch.hospital = target_biz
            batch.save()
            action = "updated" if batch_id else "created"
            messages.success(request, f"Batch '{batch.name}' {action} successfully! 📅")
            return redirect("leads:batch_master")
        else:
            messages.error(request, f"Could not save batch: {form.errors.as_text()}")
            return redirect("leads:batch_master")

    batches = Batch.objects.select_related("course", "trainer", "hospital").all().order_by("-id")
    if target_biz:
        batches = batches.filter(models.Q(hospital=target_biz) | models.Q(hospital__isnull=True))

    all_businesses = Hospital.objects.filter(is_active=True, industry="ACADEMY").order_by("name")
    form = BatchForm()
    if target_biz:
        form.fields["course"].queryset = Course.objects.filter(is_active=True, is_archived=False).filter(
            models.Q(hospital=target_biz) | models.Q(hospital__isnull=True)
        )
        form.fields["trainer"].queryset = Trainer.objects.filter(is_active=True).filter(
            models.Q(hospital=target_biz) | models.Q(hospital__isnull=True)
        )

    return render(request, "academy/masters/batch_master.html", {
        "active": "batch_master",
        "batches": batches,
        "form": form,
        "target_biz": target_biz,
        "all_businesses": all_businesses,
        "active_count": batches.filter(is_active=True).count(),
        "total_count": batches.count(),
    })


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def batch_delete(request, pk):
    batch = get_object_or_404(Batch, pk=pk)
    name = batch.name
    batch.delete()
    messages.success(request, f"Batch '{name}' deleted successfully.")
    return redirect("leads:batch_master")


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
    return render(request, "academy/masters/course_edit.html", {
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


@login_required
def payment_done_list(request):
    """
    Payment Done Leads View for Healthcare / Nelson Hospital CRM.
    Displays leads with Payment Done / Won status, OPD/IPD billings or payments completed.
    Dynamic KPIs:
    - Total Leads: All scoped leads matching date/search/filter
    - Payment Done: Count of paid/won leads
    - Conversion Rate (%): (Payment Done / Total Leads) * 100
    - Payment Pending: Count of appointment completed/visited leads awaiting payment
    - Revenue: Total revenue collected (Sum of total billed / total paid)
    """
    is_global_admin = request.user.is_superuser or request.user.role == User.Role.SUPER_ADMIN

    # Active business determination
    selected_hospital_id = (
        request.GET.get("business", "").strip()
        or request.GET.get("hospital", "").strip()
        or str(request.session.get("active_business_id", "")).strip()
    )

    target_hospital = None
    if request.user.hospital:
        target_hospital = request.user.hospital
    elif selected_hospital_id and selected_hospital_id.isdigit():
        target_hospital = Hospital.objects.filter(id=int(selected_hospital_id), is_active=True).first()

    is_healthcare_scope = True
    if target_hospital:
        is_healthcare_scope = (target_hospital.industry == Hospital.Industry.HOSPITAL)

    if not is_healthcare_scope:
        base_qs = Lead.objects.none()
    else:
        base_qs = Lead.objects.select_related(
            "hospital", "assigned_to", "stage", "campaign", "lead_source"
        ).filter(is_archived=False)

        if target_hospital:
            base_qs = base_qs.filter(hospital=target_hospital)
        elif request.user.hospital:
            base_qs = base_qs.filter(hospital=request.user.hospital)
        elif not is_global_admin:
            base_qs = base_qs.filter(hospital__isnull=True)

        if request.user.role == User.Role.MANAGER:
            mgr_branches = list(request.user.assigned_branches.filter(is_active=True)) if hasattr(request.user, 'assigned_branches') and request.user.assigned_branches.exists() else []
            if not mgr_branches and request.user.branch:
                mgr_branches = [request.user.branch]

            if mgr_branches:
                mgr_b_names = [b.name for b in mgr_branches]
                branch_team = User.objects.filter(
                    hospital=request.user.hospital
                ).filter(
                    Q(assigned_branches__in=mgr_branches) |
                    Q(branch__in=mgr_branches)
                ).distinct()

                lead_branch_q = (
                    Q(custom_data__hospital_branch__isnull=True, custom_data__branch__isnull=True) |
                    Q(custom_data__hospital_branch="") |
                    Q(custom_data__branch="")
                )
                for b_name in mgr_b_names:
                    lead_branch_q |= (
                        Q(custom_data__hospital_branch__iexact=b_name) |
                        Q(custom_data__branch__iexact=b_name) |
                        Q(custom_data__dyn_hospital_branch__iexact=b_name) |
                        Q(custom_data__dyn_branch__iexact=b_name)
                    )
                lead_branch_q |= (
                    Q(assigned_to__in=branch_team) |
                    Q(created_by__in=branch_team)
                )
                base_qs = base_qs.filter(lead_branch_q).distinct()
        elif request.user.role == User.Role.DOCTOR:
            doc_name = (request.user.get_full_name() or request.user.username).strip()
            base_qs = base_qs.filter(
                Q(assigned_to=request.user) |
                Q(appointments__doctor_user=request.user) |
                Q(appointments__doctor_name__icontains=doc_name) |
                Q(custom_data__doctor__icontains=doc_name)
            ).distinct()

    # Criteria for Payment Done / Won leads
    payment_done_condition = (
        Q(deal_status=DealStatus.WON)
        | Q(deal_status__in=[DealStatus.WON, "WON", "Payment Done", "PAYMENT DONE", "PAYMENT_DONE"])
        | Q(admission_status__in=["WON", "ADMISSION_DONE", "PAYMENT_DONE"])
        | (Q(custom_data__total__isnull=False) & ~Q(custom_data__total__in=["0", "0.00", "", "0.0", 0, 0.0]))
        | (Q(custom_data__total_paid__isnull=False) & ~Q(custom_data__total_paid__in=["0", "0.00", "", "0.0", 0, 0.0]))
        | (Q(custom_data__opd_bill__isnull=False) & ~Q(custom_data__opd_bill__in=["0", "0.00", "", "0.0", 0, 0.0]))
        | (Q(custom_data__pharmacy_bill__isnull=False) & ~Q(custom_data__pharmacy_bill__in=["0", "0.00", "", "0.0", 0, 0.0]))
        | (Q(custom_data__ipd_bill__isnull=False) & ~Q(custom_data__ipd_bill__in=["0", "0.00", "", "0.0", 0, 0.0]))
        | Q(admission__payments__payment_status="SUCCESS", admission__payments__amount__gt=0)
        | Q(custom_data__deal_status__icontains="Won")
        | Q(custom_data__deal_status__icontains="Payment")
        | Q(custom_data__appointment_status__icontains="Payment Done")
        | Q(stage__name__icontains="Payment")
        | Q(stage__name__icontains="Won")
    )

    paid_leads_qs = base_qs.filter(payment_done_condition).exclude(
        Q(deal_status=DealStatus.LOST) | Q(stage__name__icontains="lost") | Q(stage__name__icontains="cancel")
    ).distinct()

    # Header Filters
    search_q = request.GET.get("q", "").strip()
    date_from = request.GET.get("date_from", "").strip()
    date_to = request.GET.get("date_to", "").strip()
    sel_campaign = request.GET.get("campaign", "").strip()

    filtered_all_leads_qs = base_qs
    if search_q:
        filtered_all_leads_qs = filtered_all_leads_qs.filter(
            Q(name__icontains=search_q) | Q(mobile__icontains=search_q) | Q(city__icontains=search_q)
        )
        paid_leads_qs = paid_leads_qs.filter(
            Q(name__icontains=search_q) | Q(mobile__icontains=search_q) | Q(city__icontains=search_q)
        )
    if date_from:
        filtered_all_leads_qs = filtered_all_leads_qs.filter(
            Q(inquiry_date__gte=date_from) | Q(custom_data__appo_booked_date__gte=date_from)
        )
        paid_leads_qs = paid_leads_qs.filter(
            Q(inquiry_date__gte=date_from) | Q(custom_data__appo_booked_date__gte=date_from)
        )
    if date_to:
        filtered_all_leads_qs = filtered_all_leads_qs.filter(
            Q(inquiry_date__lte=date_to) | Q(custom_data__appo_booked_date__lte=date_to)
        )
        paid_leads_qs = paid_leads_qs.filter(
            Q(inquiry_date__lte=date_to) | Q(custom_data__appo_booked_date__lte=date_to)
        )
    if sel_campaign:
        filtered_all_leads_qs = filtered_all_leads_qs.filter(
            Q(campaign__name__iexact=sel_campaign) | Q(custom_data__campaign__iexact=sel_campaign)
        )
        paid_leads_qs = paid_leads_qs.filter(
            Q(campaign__name__iexact=sel_campaign) | Q(custom_data__campaign__iexact=sel_campaign)
        )

    # 1. Total Leads
    total_leads_count = filtered_all_leads_qs.count()

    # 2. Payment Done list & count
    paid_leads_list = list(paid_leads_qs)
    payment_done_count = len(paid_leads_list)

    # 3. Conversion Rate (%) = (Payment Done / Total Leads) * 100
    if total_leads_count > 0:
        conversion_rate = round((payment_done_count / total_leads_count) * 100, 1)
    else:
        conversion_rate = 0.0

    # 4. Payment Pending (Appointment completed/visited but payment pending)
    # 5. Total Revenue Collected
    payment_pending_count = 0
    total_revenue = 0.0

    for l in paid_leads_list:
        cd = l.custom_data or {}
        # Calculate lead total amount
        amt = 0.0
        try:
            val = cd.get("total") or cd.get("total_paid") or cd.get("opd_bill") or cd.get("amount") or 0
            amt = float(val) if val else 0.0
        except (ValueError, TypeError):
            amt = 0.0
        
        # Attach total_amount on lead object for display
        l.computed_total_amount = amt
        total_revenue += amt

    # Payment Pending count across scope
    pending_pay_qs = filtered_all_leads_qs.filter(
        (
            Q(custom_data__appointment_status__icontains="done")
            | Q(custom_data__appointment_status__icontains="visit")
            | Q(custom_data__appointment_status__icontains="complete")
            | Q(stage__name__icontains="payment pending")
            | Q(custom_data__deal_status__icontains="pending")
        )
        & ~Q(deal_status=DealStatus.WON)
        & ~Q(id__in=[pl.id for pl in paid_leads_list])
    ).exclude(
        Q(deal_status=DealStatus.LOST) | Q(stage__name__icontains="lost") | Q(stage__name__icontains="cancel")
    ).distinct()
    payment_pending_count = pending_pay_qs.count()

    # Format revenue nicely (e.g. ₹ 45,200)
    formatted_revenue = f"₹ {total_revenue:,.0f}" if total_revenue > 0 else "₹ 0"

    # Sort and Paginate
    paid_leads_list.sort(key=lambda x: x.inquiry_date or timezone.localdate(), reverse=True)
    from django.core.paginator import Paginator
    paginator = Paginator(paid_leads_list, 25)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    query_params_dict = request.GET.copy()
    if "page" in query_params_dict:
        del query_params_dict["page"]
    query_params = query_params_dict.urlencode()

    # Campaign breakdown for dropdown (excluding sel_campaign so all remain switchable)
    appts_for_camp_breakdown = base_qs.filter(payment_done_condition).exclude(
        Q(deal_status=DealStatus.LOST) | Q(stage__name__icontains="lost") | Q(stage__name__icontains="cancel")
    ).distinct()
    if search_q:
        appts_for_camp_breakdown = appts_for_camp_breakdown.filter(
            Q(name__icontains=search_q) | Q(mobile__icontains=search_q) | Q(city__icontains=search_q)
        )
    if date_from:
        appts_for_camp_breakdown = appts_for_camp_breakdown.filter(
            Q(inquiry_date__gte=date_from) | Q(custom_data__appo_booked_date__gte=date_from)
        )
    if date_to:
        appts_for_camp_breakdown = appts_for_camp_breakdown.filter(
            Q(inquiry_date__lte=date_to) | Q(custom_data__appo_booked_date__lte=date_to)
        )
    campaign_breakdown = get_campaign_breakdown_stats(base_qs, appts_for_camp_breakdown)

    return render(request, "leads/payment_done_list.html", {
        "active": "payments",
        "target_hospital": target_hospital,
        "is_healthcare_scope": is_healthcare_scope,
        "page_obj": page_obj,
        "total_count": payment_done_count,
        "total_leads_count": total_leads_count,
        "payment_done_count": payment_done_count,
        "conversion_rate": conversion_rate,
        "payment_pending_count": payment_pending_count,
        "total_revenue": total_revenue,
        "formatted_revenue": formatted_revenue,
        "campaign_breakdown": campaign_breakdown,
        "q": search_q,
        "date_from_val": date_from or "",
        "date_to_val": date_to or "",
        "query_params": query_params,
    })
