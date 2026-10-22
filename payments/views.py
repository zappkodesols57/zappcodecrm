from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.db.models import Sum
from django.utils import timezone

from admissions.models import Admission
from .models import Payment, PaymentStatus


@login_required
def payment_list(request):
    payments = Payment.objects.select_related("admission", "admission__lead").all()
    totals = payments.filter(payment_status=PaymentStatus.SUCCESS).aggregate(collected=Sum("amount"))
    return render(request, "payments/list.html", {
        "active": "payments", "payments": payments, "collected": totals["collected"] or 0,
    })


@login_required
def payment_add(request, admission_id):
    from decimal import Decimal
    from leads.models import LeadStage, DealStatus, AdmissionStatus
    from followups.models import Activity, ActivityType

    admission = get_object_or_404(Admission, pk=admission_id)
    lead = admission.lead

    # Ensure initial token amount is recorded if missing in payments
    if not admission.payments.exists() and lead and lead.custom_data.get("token_amount"):
        try:
            token_val = Decimal(str(lead.custom_data.get("token_amount") or 0))
            if token_val > 0:
                Payment.objects.create(
                    admission=admission,
                    amount=token_val,
                    payment_date=admission.admission_date or timezone.localdate(),
                    payment_mode="UPI",
                    payment_status=PaymentStatus.SUCCESS,
                    remarks="Initial token payment",
                )
        except Exception:
            pass

    current_pending = admission.pending
    if current_pending < Decimal("0.00"):
        current_pending = Decimal("0.00")

    if request.method == "POST":
        raw_amount = request.POST.get("amount", "").strip()
        try:
            amount = Decimal(raw_amount)
        except Exception:
            messages.error(request, "Please enter a valid numeric amount.")
            return render(request, "payments/add.html", {
                "active": "payments", "admission": admission, "pending_amount": current_pending,
            })

        if amount <= Decimal("0.00"):
            messages.error(request, "Payment amount must be greater than zero.")
            return render(request, "payments/add.html", {
                "active": "payments", "admission": admission, "pending_amount": current_pending,
            })

        if amount > current_pending:
            messages.error(
                request,
                f"Amount (₹{amount:,.0f}) cannot exceed current Pending Amount (₹{current_pending:,.0f})."
            )
            return render(request, "payments/add.html", {
                "active": "payments", "admission": admission, "pending_amount": current_pending,
            })

        payment = Payment.objects.create(
            admission=admission,
            amount=amount,
            payment_date=request.POST.get("payment_date") or timezone.localdate(),
            payment_mode=request.POST.get("payment_mode", "CASH"),
            transaction_reference=request.POST.get("transaction_reference", "").strip(),
            payment_status=request.POST.get("payment_status", PaymentStatus.SUCCESS),
            remarks=request.POST.get("remarks", "").strip(),
        )

        # Log Activity
        if lead:
            Activity.objects.create(
                lead=lead,
                activity_type=ActivityType.PAYMENT,
                description=f"Payment recorded: ₹{amount:,.0f} via {payment.get_payment_mode_display()}. Transaction ref: {payment.transaction_reference or 'N/A'}",
                created_by=request.user,
            )

        # Recalculate remaining pending balance after payment
        new_collected = admission.collected
        remaining_pending = max(Decimal("0.00"), admission.final_fee - new_collected)

        # If lead has active unpaid EMI installments, automatically adjust/redistribute
        # upcoming EMIs so that the sum of remaining installments equals the remaining pending amount!
        if admission.payment_plan == "EMI":
            unpaid_installments = list(admission.installments.filter(is_paid=False).order_by("due_date"))
            if unpaid_installments:
                if remaining_pending <= Decimal("0.00"):
                    # All balance cleared by ad-hoc payment; remove or zero out upcoming installments
                    for inst in unpaid_installments:
                        inst.amount = Decimal("0.00")
                        inst.is_paid = True
                        inst.paid_date = payment.payment_date
                        inst.save()
                else:
                    count_unpaid = len(unpaid_installments)
                    import math
                    # Distribute remaining_pending across unpaid installments
                    ceil_emi = Decimal(str(math.ceil(float(remaining_pending) / count_unpaid)))
                    for idx, inst in enumerate(unpaid_installments):
                        if idx == count_unpaid - 1:
                            # Last installment absorbs the exact remainder
                            sum_prev = ceil_emi * (count_unpaid - 1)
                            inst.amount = max(Decimal("0.00"), remaining_pending - sum_prev)
                        else:
                            inst.amount = ceil_emi
                        inst.save()

        # Sync lead state & custom_data
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

        messages.success(request, f"Payment of ₹{amount:,.0f} successfully recorded. Remaining Pending Amount: ₹{remaining_pending:,.0f}.")
        return redirect("leads:lead_detail", pk=admission.lead_id)

    return render(request, "payments/add.html", {
        "active": "payments", "admission": admission, "pending_amount": current_pending,
    })
