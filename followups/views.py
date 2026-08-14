from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone

from leads.models import Lead
from .models import FollowUp, FollowUpStatus


def _board(request, leads, active, title):
    return render(request, "followups/board.html", {"active": active, "title": title, "leads": leads})


@login_required
def today(request):
    d = timezone.localdate()
    leads = Lead.objects.filter(next_followup_date=d, is_archived=False).select_related("stage", "assigned_to")
    return _board(request, leads, "fu_today", "Today's Follow-ups")


@login_required
def upcoming(request):
    d = timezone.localdate()
    leads = Lead.objects.filter(next_followup_date__gt=d, is_archived=False).select_related("stage", "assigned_to").order_by("next_followup_date")
    return _board(request, leads, "fu_upcoming", "Upcoming Follow-ups")


@login_required
def overdue(request):
    d = timezone.localdate()
    leads = Lead.objects.filter(next_followup_date__lt=d, is_archived=False).select_related("stage", "assigned_to").order_by("next_followup_date")
    return _board(request, leads, "fu_overdue", "Overdue Follow-ups")


@login_required
def complete(request, pk):
    lead = get_object_or_404(Lead, pk=pk)
    if request.method == "POST":
        FollowUp.objects.create(
            lead=lead, followup_date=timezone.localdate(), followup_mode="CALL",
            followup_status=FollowUpStatus.COMPLETED, comment=request.POST.get("comment", "Marked complete"),
            created_by=request.user,
        )
        Lead.objects.filter(pk=pk).update(next_followup_date=None, next_followup_time=None)
        messages.success(request, "Follow-up marked complete.")
    return redirect(request.META.get("HTTP_REFERER", "/"))
