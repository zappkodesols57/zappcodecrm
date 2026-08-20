from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.core.paginator import Paginator
from django.db.models import Q

from leads.models import Lead
from .models import FollowUp, FollowUpStatus
from accounts.models import User

def _filter_by_role(user, leads):
    if user.hospital:
        leads = leads.filter(hospital=user.hospital)
        
    if not user.can_view_all_leads:
        if user.can_view_team_leads:
            team = User.objects.filter(reports_to=user)
            leads = leads.filter(Q(assigned_to=user) | Q(assigned_to__in=team))
        elif user.can_view_assigned_leads:
            leads = leads.filter(assigned_to=user)
        else:
            leads = leads.none()
            
    return leads

def _board(request, leads, active, title, date_info=None):
    # Search filter
    q = request.GET.get('q', '').strip()
    if q:
        leads = leads.filter(
            Q(name__icontains=q) | 
            Q(mobile__icontains=q) | 
            Q(lead_code__icontains=q) |
            Q(custom_data__doctor__icontains=q) |
            Q(custom_data__department__icontains=q)
        )
        
    paginator = Paginator(leads, 25)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1) if hasattr(paginator, 'get_elided_page_range') else paginator.page_range
    
    query_params = request.GET.copy()
    if 'page' in query_params:
        del query_params['page']
        
    return render(request, "followups/board.html", {
        "active": active, 
        "title": title, 
        "page_obj": page_obj,
        "leads": page_obj,
        "page_range": page_range,
        "query_params": query_params.urlencode(),
        "total_count": paginator.count,
        "q": q,
        "date_info": date_info,
    })


@login_required
def today(request):
    d = timezone.localdate()
    today_str = d.strftime("%Y-%m-%d")
    
    # Query leads having next_followup_date today OR hospital remark calling dates today OR appo_booked_date today
    leads = Lead.objects.filter(
        is_archived=False
    ).filter(
        Q(next_followup_date=d) |
        Q(custom_data__calling_date_remark_1=today_str) |
        Q(custom_data__calling_date_remark_2=today_str) |
        Q(custom_data__calling_date_remark_3=today_str) |
        Q(custom_data__appo_booked_date=today_str)
    ).select_related("assigned_to", "stage").order_by("-updated_at")
    
    leads = _filter_by_role(request.user, leads)
    return _board(request, leads, "fu_today", "Today's Follow-ups & Appointments", d)


@login_required
def upcoming(request):
    d = timezone.localdate()
    today_str = d.strftime("%Y-%m-%d")
    
    leads = Lead.objects.filter(
        is_archived=False
    ).filter(
        Q(next_followup_date__gt=d) |
        Q(custom_data__calling_date_remark_1__gt=today_str) |
        Q(custom_data__calling_date_remark_2__gt=today_str) |
        Q(custom_data__calling_date_remark_3__gt=today_str) |
        Q(custom_data__appo_booked_date__gt=today_str)
    ).select_related("assigned_to", "stage").order_by("next_followup_date")
    
    leads = _filter_by_role(request.user, leads)
    return _board(request, leads, "fu_upcoming", "Upcoming Follow-ups & Appointments", d)


@login_required
def overdue(request):
    d = timezone.localdate()
    today_str = d.strftime("%Y-%m-%d")
    
    leads = Lead.objects.filter(
        is_archived=False
    ).filter(
        Q(next_followup_date__lt=d) |
        Q(custom_data__calling_date_remark_1__lt=today_str, custom_data__calling_date_remark_1__gt="") |
        Q(custom_data__calling_date_remark_2__lt=today_str, custom_data__calling_date_remark_2__gt="") |
        Q(custom_data__calling_date_remark_3__lt=today_str, custom_data__calling_date_remark_3__gt="") |
        Q(custom_data__appo_booked_date__lt=today_str, custom_data__appo_booked_date__gt="")
    ).exclude(
        custom_data__deal_status__in=["Won", "Lost"]
    ).select_related("assigned_to", "stage").order_by("-updated_at")
    
    leads = _filter_by_role(request.user, leads)
    return _board(request, leads, "fu_overdue", "Overdue Follow-ups & Pending Calls", d)


@login_required
def complete(request, pk):
    lead = get_object_or_404(Lead, pk=pk)
    if request.method == "POST":
        FollowUp.objects.create(
            lead=lead, followup_date=timezone.localdate(), followup_mode="CALL",
            followup_status=FollowUpStatus.COMPLETED, comment=request.POST.get("comment", "Marked complete"),
            created_by=request.user,
        )
        update_kwargs = {"next_followup_date": None, "next_followup_time": None}
        if lead.assigned_to is None:
            update_kwargs["assigned_to"] = request.user
        Lead.objects.filter(pk=pk).update(**update_kwargs)
        messages.success(request, "Follow-up marked complete.")
    return redirect(request.META.get("HTTP_REFERER", "/"))
