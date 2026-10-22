from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from django.core.mail import send_mail
from django.db.models import Sum, Q, F, DecimalField
from django.db.models.functions import Coalesce
from .models import Admission, Installment, PaymentPlan, CourseStatus
from leads.models import Course


@login_required
def admission_list(request):
    # Prefetch payments and related fields for optimal performance
    admissions = Admission.objects.select_related("lead", "course", "assigned_counselor").prefetch_related("payments").annotate(
        total_collected=Coalesce(Sum('payments__amount', filter=Q(payments__payment_status='SUCCESS')), Decimal('0.00'), output_field=DecimalField())
    )

    target_biz = request.user.hospital if request.user.hospital else None
    if target_biz:
        admissions = admissions.filter(lead__hospital=target_biz)
    # Global Super Admin (no hospital): see all admissions

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
    course_status_filter = request.GET.get("course_status", "")
    fee_filter = request.GET.get("fee_status", "")
    trainer_filter = request.GET.get("trainer", "").strip()
    course_filter = request.GET.get("course", "").strip()

    if search_query:
        admissions = admissions.filter(
            Q(student_name__icontains=search_query) |
            Q(lead__lead_code__icontains=search_query) |
            Q(lead__mobile__icontains=search_query) |
            Q(lead__email__icontains=search_query)
        )
    if plan_filter:
        admissions = admissions.filter(payment_plan=plan_filter)
    if course_status_filter:
        admissions = admissions.filter(course_status=course_status_filter)
    if trainer_filter:
        admissions = admissions.filter(
            Q(tutor__iexact=trainer_filter) |
            Q(lead__custom_data__tutor__iexact=trainer_filter) |
            Q(course__tutor__iexact=trainer_filter)
        )
    if course_filter:
        if course_filter.isdigit():
            admissions = admissions.filter(course_id=int(course_filter))
        else:
            admissions = admissions.filter(course__name__iexact=course_filter)

    if fee_filter == "FULLY_PAID":
        admissions = [a for a in admissions if a.total_collected >= a.final_fee]
    elif fee_filter == "PENDING":
        admissions = [a for a in admissions if a.total_collected < a.final_fee]

    # Pre-calculate computed payment attributes on each admission object
    for a in admissions:
        a.collected_amount = a.total_collected if hasattr(a, 'total_collected') else a.collected
        a.pending_amount = max(Decimal('0.00'), a.final_fee - a.collected_amount)
        if a.collected_amount >= a.final_fee and a.final_fee > 0:
            a.payment_status_badge = "Full Paid"
            a.payment_status_class = "success"
        elif a.collected_amount > 0:
            a.payment_status_badge = "Partial Paid"
            a.payment_status_class = "warning"
        else:
            a.payment_status_badge = "Unpaid"
            a.payment_status_class = "danger"

        # Determine payment mode display
        first_success_pay = next((p for p in a.payments.all() if p.payment_status == 'SUCCESS'), None)
        if first_success_pay:
            a.payment_mode_display = first_success_pay.get_payment_mode_display()
        elif a.payment_option == 'ONE_TIME_CASH':
            a.payment_mode_display = "Cash"
        elif a.payment_option == 'ONE_TIME_UPI':
            a.payment_mode_display = "UPI / Online"
        elif a.payment_plan == PaymentPlan.EMI:
            a.payment_mode_display = "EMI / Online"
        else:
            a.payment_mode_display = "UPI / Online"

    # Distinct trainers and courses for dropdown filter
    from leads.models import Trainer, Course
    courses_qs = Course.objects.filter(is_active=True, hospital=target_biz) if target_biz else Course.objects.filter(is_active=True)
    trainers_qs = Trainer.objects.filter(is_active=True, hospital=target_biz) if target_biz else Trainer.objects.filter(is_active=True)
    distinct_trainers = sorted(list(set(
        list(trainers_qs.values_list("name", flat=True)) +
        list(Admission.objects.exclude(tutor="").values_list("tutor", flat=True).distinct())
    )))

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
        "course_status_filter": course_status_filter,
        "fee_filter": fee_filter,
        "trainer_filter": trainer_filter,
        "course_filter": course_filter,
        "courses": courses_qs,
        "trainers": distinct_trainers,
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
    from decimal import Decimal
    from payments.models import Payment, PaymentMode, PaymentStatus
    from leads.models import LeadStage, DealStatus, AdmissionStatus
    from followups.models import Activity, ActivityType

    inst = get_object_or_404(Installment, pk=pk)
    admission = inst.admission
    lead = admission.lead

    if not inst.is_paid:
        # User is trying to mark this installment as PAID
        current_pending = admission.final_fee - admission.collected
        if current_pending <= Decimal("0.00"):
            messages.warning(request, "This admission is already fully paid. Cannot mark additional EMI installments as paid.")
            return redirect(request.META.get('HTTP_REFERER', 'admissions:list'))

        # If remaining pending is less than the installment amount (due to earlier ad-hoc payments), cap it!
        actual_paid_amount = min(inst.amount, current_pending)
        inst.amount = actual_paid_amount
        inst.is_paid = True
        inst.paid_date = timezone.localdate()
        inst.save()

        # Create corresponding Payment record if not exists
        payment, created = Payment.objects.get_or_create(
            admission=admission,
            transaction_reference=ref_tag,
            defaults={
                "amount": actual_paid_amount,
                "payment_date": inst.paid_date,
                "payment_mode": PaymentMode.UPI,
                "payment_status": PaymentStatus.SUCCESS,
                "remarks": f"EMI Installment payment (Due: {inst.due_date.strftime('%d-%b-%Y')})",
            }
        )
        if not created and payment.amount != actual_paid_amount:
            payment.amount = actual_paid_amount
            payment.save()

        if lead and created:
            Activity.objects.create(
                lead=lead,
                activity_type=ActivityType.PAYMENT,
                description=f"EMI installment marked Paid: ₹{actual_paid_amount:,.0f} (Due date: {inst.due_date.strftime('%d-%b-%Y')})",
                created_by=request.user,
            )
        action_text = "Paid"

        # Rebalance upcoming remaining unpaid installments with the newly remaining pending balance
        new_collected = admission.collected
        new_remaining = max(Decimal("0.00"), admission.final_fee - new_collected)
        unpaid_installments = list(admission.installments.filter(is_paid=False).exclude(id=inst.id).order_by("due_date"))
        if unpaid_installments:
            if new_remaining <= Decimal("0.00"):
                # No balance remaining; mark the rest completed with 0 or clean up
                for rem_inst in unpaid_installments:
                    rem_inst.amount = Decimal("0.00")
                    rem_inst.is_paid = True
                    rem_inst.paid_date = inst.paid_date
                    rem_inst.save()
            else:
                count_unpaid = len(unpaid_installments)
                import math
                ceil_emi = Decimal(str(math.ceil(float(new_remaining) / count_unpaid)))
                for idx, rem_inst in enumerate(unpaid_installments):
                    if idx == count_unpaid - 1:
                        sum_prev = ceil_emi * (count_unpaid - 1)
                        rem_inst.amount = max(Decimal("0.00"), new_remaining - sum_prev)
                    else:
                        rem_inst.amount = ceil_emi
                    rem_inst.save()
    else:
        # User is reverting installment to UNPAID
        inst.is_paid = False
        inst.paid_date = None
        inst.save()

        # Remove corresponding Payment record if exists
        deleted_count, _ = Payment.objects.filter(
            admission=admission,
            transaction_reference=ref_tag
        ).delete()
        if lead and deleted_count:
            Activity.objects.create(
                lead=lead,
                activity_type=ActivityType.PAYMENT,
                description=f"EMI installment reverted to Unpaid: ₹{inst.amount:,.0f} (Due date: {inst.due_date.strftime('%d-%b-%Y')})",
                created_by=request.user,
            )
        action_text = "Unpaid"

        # Rebalance all unpaid installments with current pending amount
        new_collected = admission.collected
        new_remaining = max(Decimal("0.00"), admission.final_fee - new_collected)
        unpaid_installments = list(admission.installments.filter(is_paid=False).order_by("due_date"))
        if unpaid_installments:
            count_unpaid = len(unpaid_installments)
            import math
            ceil_emi = Decimal(str(math.ceil(float(new_remaining) / count_unpaid)))
            for idx, rem_inst in enumerate(unpaid_installments):
                if idx == count_unpaid - 1:
                    sum_prev = ceil_emi * (count_unpaid - 1)
                    rem_inst.amount = max(Decimal("0.00"), new_remaining - sum_prev)
                else:
                    rem_inst.amount = ceil_emi
                rem_inst.save()

    # Sync lead custom_data and stages
    new_collected = admission.collected
    remaining_pending = max(Decimal("0.00"), admission.final_fee - new_collected)

    if lead:
        cd = lead.custom_data or {}
        cd["total_paid"] = float(new_collected)
        cd["balance_amount"] = float(remaining_pending)
        lead.custom_data = cd

        if remaining_pending <= Decimal("0.00"):
            won_stage = (
                LeadStage.objects.filter(name__iexact="Payment Done", is_active=True).first()
                or LeadStage.objects.filter(name__icontains="Payment Done").first()
            )
            if won_stage:
                lead.stage = won_stage
            lead.deal_status = DealStatus.WON
            lead.admission_status = AdmissionStatus.WON
            cd["deal_status"] = "Won"
        else:
            pending_stage = (
                LeadStage.objects.filter(name__iexact="Payment Pending", is_active=True).first()
                or LeadStage.objects.filter(name__icontains="Payment Pending").first()
            )
            if pending_stage:
                lead.stage = pending_stage
            lead.deal_status = DealStatus.WON
            cd["deal_status"] = "Payment Pending"

        lead.save()

    messages.success(request, f"EMI installment marked {action_text}. Updated Paid: ₹{new_collected:,.0f}, Pending: ₹{remaining_pending:,.0f}.")
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
        form = DirectAdmissionForm(request.POST, user=request.user)
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
            trainer = form.cleaned_data.get("trainer", "")
            batch = form.cleaned_data.get("batch", "")
            batch_time = form.cleaned_data.get("batch_time", "")
            date_of_joining = form.cleaned_data.get("date_of_joining")
            payment_mode = form.cleaned_data.get("payment_mode", "ONE_TIME_UPI")
            token_paid = form.cleaned_data.get("token_paid") or Decimal("0.00")
            emi_months = form.cleaned_data.get("emi_months") or 0
            payment_plan = PaymentPlan.EMI if payment_mode == "EMI" else PaymentPlan.FULL
            final_fee = max(Decimal("0.00"), total_fee - discount)

            # 1. Create or retrieve lead record
            admission_stage = LeadStage.objects.filter(name__icontains="admission").first() or LeadStage.objects.order_by("-order").first()
            digits = Lead.clean_mobile(mobile)
            existing_leads = [l for l in Lead.objects.all() if Lead.clean_mobile(l.mobile) == digits] if digits else []

            custom_payload = {
                "tutor": trainer,
                "batch": batch,
                "batch_time": batch_time,
                "payment_option": payment_mode,
                "total": str(final_fee),
                "total_paid": str(final_fee if payment_mode != "EMI" else token_paid),
                "token_amount": str(token_paid),
                "balance_amount": str(max(Decimal("0.00"), final_fee - token_paid)),
            }
            if date_of_joining:
                custom_payload["date_of_joining"] = str(date_of_joining)

            if existing_leads:
                lead = existing_leads[0]
                lead.name = student_name
                if email: lead.email = email
                if city: lead.city = city
                lead.course = course
                lead.stage = admission_stage
                lead.deal_status = DealStatus.WON
                lead.admission_status = AdmissionStatus.WON
                if assigned_counselor: lead.assigned_to = assigned_counselor
                cd = lead.custom_data or {}
                cd.update(custom_payload)
                lead.custom_data = cd
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
                    admission_status=AdmissionStatus.WON,
                    assigned_to=assigned_counselor or request.user,
                    created_by=request.user,
                    inquiry_date=admission_date,
                    hospital=request.user.hospital if request.user.hospital else None,
                    custom_data=custom_payload,
                )

            # 2. Create or update Admission record
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
                    "payment_option": payment_mode,
                    "emi_months": emi_months if payment_mode == "EMI" else 0,
                    "monthly_emi_amount": (final_fee - token_paid) / emi_months if (payment_mode == "EMI" and emi_months > 0) else Decimal("0.00"),
                    "date_of_joining": date_of_joining,
                    "tutor": trainer,
                    "batch": batch,
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
                admission.payment_option = payment_mode
                admission.emi_months = emi_months if payment_mode == "EMI" else 0
                if payment_mode == "EMI" and emi_months > 0:
                    admission.monthly_emi_amount = (final_fee - token_paid) / emi_months
                if date_of_joining: admission.date_of_joining = date_of_joining
                if trainer: admission.tutor = trainer
                if batch: admission.batch = batch
                admission.course_status = course_status
                if assigned_counselor: admission.assigned_counselor = assigned_counselor
                if notes: admission.notes = notes
                admission.save()

            # 3. Create initial Payment record if token or full payment was paid
            from payments.models import Payment, PaymentMode, PaymentStatus
            pay_amt = final_fee if payment_mode != "EMI" else token_paid
            if pay_amt > 0:
                p_mode_map = {
                    "ONE_TIME_CASH": PaymentMode.CASH,
                    "ONE_TIME_UPI": PaymentMode.UPI,
                    "EMI": PaymentMode.UPI,
                }
                Payment.objects.create(
                    admission=admission,
                    amount=pay_amt,
                    payment_date=admission_date,
                    payment_mode=p_mode_map.get(payment_mode, PaymentMode.UPI),
                    payment_status=PaymentStatus.SUCCESS,
                    remarks="Admission payment recorded directly",
                )

            # 4. Schedule EMI installments if applicable
            if payment_mode == "EMI" and emi_months > 0:
                from admissions.models import Installment
                from dateutil.relativedelta import relativedelta
                rem_bal = final_fee - token_paid
                monthly_amt = (rem_bal / emi_months).quantize(Decimal("1.00")) if emi_months else Decimal("0.00")
                for m in range(1, emi_months + 1):
                    due_d = admission_date + relativedelta(months=m)
                    Installment.objects.create(
                        admission=admission,
                        amount=monthly_amt,
                        due_date=due_d,
                    )

            messages.success(request, f"Student admission for '{student_name}' recorded successfully.")
            return redirect("admissions:list")
    else:
        form = DirectAdmissionForm(user=request.user)

    import json
    courses_qs = Course.objects.filter(is_active=True, hospital=request.user.hospital) if request.user.hospital else Course.objects.filter(is_active=True)
    course_data = {
        str(c.id): {
            "name": c.name,
            "base_price": float(c.base_price),
            "max_discount": float(c.max_discount),
            "tutor": c.tutor or "",
        }
        for c in courses_qs
    }

    # Available batches & trainers for auto-suggestions
    from leads.models import Batch, Trainer
    target_biz = request.user.hospital if request.user.hospital else None
    batches_qs = Batch.objects.filter(is_active=True, hospital=target_biz) if target_biz else Batch.objects.filter(is_active=True)
    trainers_qs = Trainer.objects.filter(is_active=True, hospital=target_biz) if target_biz else Trainer.objects.filter(is_active=True)

    available_batches = [
        {
            "id": b.id,
            "name": b.name,
            "course_id": b.course_id,
            "trainer": b.trainer.name if b.trainer else "",
            "timing_display": b.timing_display,
        }
        for b in batches_qs.select_related("trainer", "course")
    ]
    available_trainers = list(trainers_qs.values_list("name", flat=True).distinct())

    return render(request, "admissions/admission_form.html", {
        "active": "admissions",
        "form": form,
        "course_data_json": json.dumps(course_data),
        "available_batches": available_batches,
        "available_trainers": available_trainers,
    })
