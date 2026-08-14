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
    admission = get_object_or_404(Admission, pk=admission_id)
    if request.method == "POST":
        Payment.objects.create(
            admission=admission,
            amount=request.POST.get("amount") or 0,
            payment_date=request.POST.get("payment_date") or timezone.localdate(),
            payment_mode=request.POST.get("payment_mode", "CASH"),
            transaction_reference=request.POST.get("transaction_reference", ""),
            payment_status=request.POST.get("payment_status", PaymentStatus.SUCCESS),
            remarks=request.POST.get("remarks", ""),
        )
        messages.success(request, "Payment recorded.")
        return redirect("leads:lead_detail", pk=admission.lead_id)
    return render(request, "payments/add.html", {"active": "payments", "admission": admission})
