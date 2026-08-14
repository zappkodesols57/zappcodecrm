from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from django.core.mail import send_mail
from django.db.models import Sum, Q, F, DecimalField
from django.db.models.functions import Coalesce
from .models import Admission, Installment, PaymentPlan, CourseStatus


@login_required
def admission_list(request):
    # Annotate total successful collected fees for database filtering
    admissions = Admission.objects.select_related("lead", "course", "assigned_counselor").annotate(
        total_collected=Coalesce(Sum('payments__amount', filter=Q(payments__payment_status='SUCCESS')), Decimal('0.00'), output_field=DecimalField())
    )

    # Dynamic Dashboard Stats (calculated before filters are applied)
    stats_all = admissions
    total_count = stats_all.count()
    full_paid_count = sum(1 for a in stats_all if a.total_collected >= a.final_fee)
    emi_count = stats_all.filter(payment_plan=PaymentPlan.EMI).count()
    completed_count = stats_all.filter(course_status=CourseStatus.COMPLETED).count()
    total_pending_fees = sum((a.final_fee - a.total_collected) for a in stats_all)

    # Apply filters
    search_query = request.GET.get("search", "").strip()
    plan_filter = request.GET.get("payment_plan", "")
    course_filter = request.GET.get("course_status", "")
    fee_filter = request.GET.get("fee_status", "")

    if search_query:
        admissions = admissions.filter(Q(student_name__icontains=search_query) | Q(lead__lead_code__icontains=search_query))
    if plan_filter:
        admissions = admissions.filter(payment_plan=plan_filter)
    if course_filter:
        admissions = admissions.filter(course_status=course_filter)
    if fee_filter == "FULLY_PAID":
        admissions = [a for a in admissions if a.total_collected >= a.final_fee]
    elif fee_filter == "PENDING":
        admissions = [a for a in admissions if a.total_collected < a.final_fee]

    return render(request, "admissions/list.html", {
        "active": "admissions",
        "admissions": admissions,
        "total_count": total_count,
        "full_paid_count": full_paid_count,
        "emi_count": emi_count,
        "completed_count": completed_count,
        "total_pending_fees": total_pending_fees,
        "search_query": search_query,
        "plan_filter": plan_filter,
        "course_filter": course_filter,
        "fee_filter": fee_filter,
        "payment_plans": PaymentPlan.choices,
        "course_statuses": CourseStatus.choices,
    })


@login_required
def admission_update_status(request, pk):
    admission = get_object_or_404(Admission, pk=pk)
    if request.method == "POST":
        payment_plan = request.POST.get("payment_plan")
        course_status = request.POST.get("course_status")
        if payment_plan in PaymentPlan.values:
            admission.payment_plan = payment_plan
        if course_status in CourseStatus.values:
            admission.course_status = course_status
        admission.save()
        messages.success(request, "Admission status updated successfully.")
    return redirect(request.META.get('HTTP_REFERER', 'admissions:list'))


@login_required
def add_installment(request, pk):
    admission = get_object_or_404(Admission, pk=pk)
    if request.method == "POST":
        amount = request.POST.get("amount")
        due_date = request.POST.get("due_date")
        if amount and due_date:
            Installment.objects.create(
                admission=admission,
                amount=amount,
                due_date=due_date
            )
            messages.success(request, "EMI Installment scheduled successfully.")
        else:
            messages.error(request, "Amount and Due Date are required.")
    return redirect(request.META.get('HTTP_REFERER', 'admissions:list'))


@login_required
def installment_toggle_paid(request, pk):
    inst = get_object_or_404(Installment, pk=pk)
    inst.is_paid = not inst.is_paid
    inst.paid_date = timezone.localdate() if inst.is_paid else None
    inst.save()
    messages.success(request, f"EMI installment status changed to {'Paid' if inst.is_paid else 'Unpaid'}.")
    return redirect(request.META.get('HTTP_REFERER', 'admissions:list'))


@login_required
def admissions_bulk_action(request):
    if request.method != "POST":
        return redirect("admissions:list")
    
    selected_ids = request.POST.getlist("selected")
    action = request.POST.get("bulk_action")
    
    if not selected_ids:
        messages.warning(request, "No students selected.")
        return redirect("admissions:list")
        
    admissions = Admission.objects.filter(pk__in=selected_ids)
    
    if action == "mark_ongoing":
        admissions.update(course_status=CourseStatus.ONGOING)
        messages.success(request, f"Marked {admissions.count()} student(s) course status as Ongoing.")
        
    elif action == "mark_completed":
        admissions.update(course_status=CourseStatus.COMPLETED)
        messages.success(request, f"Marked {admissions.count()} student(s) course status as Completed.")
        
    return redirect("admissions:list")


@login_required
def add_admission(request):
    from .forms import DirectAdmissionForm
    from leads.models import Lead, LeadStage, AdmissionStatus, DealStatus, LeadTemperature

    if request.method == "POST":
        form = DirectAdmissionForm(request.POST)
        if form.is_valid():
            student_name = form.cleaned_data["student_name"]
            mobile = form.cleaned_data["mobile"]
            email = form.cleaned_data.get("email", "")
            city = form.cleaned_data.get("city", "")
            course = form.cleaned_data["course"]
            admission_date = form.cleaned_data["admission_date"]
            total_fee = form.cleaned_data["total_fee"]
            discount = form.cleaned_data.get("discount") or Decimal("0.00")
            extra_discount_reason = form.cleaned_data.get("extra_discount_reason", "")
            payment_plan = form.cleaned_data["payment_plan"]
            course_status = form.cleaned_data["course_status"]
            assigned_counselor = form.cleaned_data.get("assigned_counselor")
            notes = form.cleaned_data.get("notes", "")

            # 1. Create or retrieve lead record
            admission_stage = LeadStage.objects.filter(name__icontains="admission").first() or LeadStage.objects.order_by("-order").first()
            digits = Lead.clean_mobile(mobile)
            existing_leads = [l for l in Lead.objects.all() if Lead.clean_mobile(l.mobile) == digits] if digits else []

            if existing_leads:
                lead = existing_leads[0]
                lead.name = student_name
                if email: lead.email = email
                if city: lead.city = city
                lead.course = course
                lead.stage = admission_stage
                lead.deal_status = DealStatus.WON
                lead.admission_status = AdmissionStatus.ADMISSION_DONE
                if assigned_counselor: lead.assigned_to = assigned_counselor
                lead.save()
            else:
                lead = Lead.objects.create(
                    name=student_name,
                    mobile=mobile,
                    email=email,
                    city=city,
                    course=course,
                    stage=admission_stage,
                    temperature=LeadTemperature.HOT,
                    deal_status=DealStatus.WON,
                    admission_status=AdmissionStatus.ADMISSION_DONE,
                    assigned_to=assigned_counselor or request.user,
                    created_by=request.user,
                    inquiry_date=admission_date,
                )

            # 2. Create Admission record
            admission, created = Admission.objects.get_or_create(
                lead=lead,
                defaults={
                    "student_name": student_name,
                    "course": course,
                    "admission_date": admission_date,
                    "total_fee": total_fee,
                    "discount": discount,
                    "max_allowed_discount": getattr(course, "max_discount", Decimal("0.00")),
                    "extra_discount_reason": extra_discount_reason,
                    "payment_plan": payment_plan,
                    "course_status": course_status,
                    "assigned_counselor": assigned_counselor or request.user,
                    "notes": notes,
                }
            )

            if not created:
                admission.student_name = student_name
                admission.course = course
                admission.admission_date = admission_date
                admission.total_fee = total_fee
                admission.discount = discount
                admission.extra_discount_reason = extra_discount_reason
                admission.payment_plan = payment_plan
                admission.course_status = course_status
                if assigned_counselor: admission.assigned_counselor = assigned_counselor
                if notes: admission.notes = notes
                admission.save()

            messages.success(request, f"Student admission for '{student_name}' recorded successfully.")
            return redirect("admissions:list")
    else:
        form = DirectAdmissionForm()

    return render(request, "admissions/admission_form.html", {
        "active": "admissions",
        "form": form,
    })
