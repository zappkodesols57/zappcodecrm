from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from .models import Notification
from datetime import timedelta
from django.utils import timezone

@login_required
def get_unread_notifications(request):
    tz = timezone.get_current_timezone()
    now = timezone.localtime(timezone.now(), tz)
    today = now.date()
    
    from leads.models import Lead
    from followups.models import FollowUp, FollowUpStatus
    
    # 1. Check pending followups for this user scheduled for today
    pending_fu = FollowUp.objects.filter(
        lead__assigned_to=request.user,
        followup_date=today,
        followup_status=FollowUpStatus.PENDING
    ).select_related('lead')

    for fu in pending_fu:
        time_msg = ""
        should_alert = False
        if fu.followup_time:
            fu_dt = timezone.datetime.combine(today, fu.followup_time)
            fu_dt = timezone.make_aware(fu_dt, tz)
            diff_minutes = (fu_dt - now).total_seconds() / 60.0
            time_msg = f" at {fu.followup_time.strftime('%I:%M %p')}"
            # Alert only when within 5 mins before scheduled time up to 60 mins after
            if -60 <= diff_minutes <= 5:
                should_alert = True

        if should_alert:
            notif_title = f"⏰ Follow-up Due: {fu.lead.name}"
            exists = Notification.objects.filter(
                user=request.user,
                title=notif_title,
                created_at__date=today
            ).exists()
            if not exists:
                Notification.objects.create(
                    user=request.user,
                    title=notif_title,
                    message=f"Scheduled follow-up for patient {fu.lead.name}{time_msg} is now due. Contact: {fu.lead.mobile}",
                    link=f"/leads/{fu.lead.pk}/"
                )

    # 2. Return unread notifications formatted in accurate local time
    notifications = request.user.notifications.filter(is_read=False).order_by('-created_at')[:10]
    data = []
    for n in notifications:
        local_created = timezone.localtime(n.created_at, tz)
        data.append({
            'id': n.id,
            'title': n.title,
            'message': n.message,
            'link': n.link,
            'created_at': local_created.strftime("%I:%M %p"),
            'full_time': local_created.strftime("%d %b %Y, %I:%M %p"),
        })
    count = request.user.notifications.filter(is_read=False).count()
    return JsonResponse({'count': count, 'notifications': data})

@login_required
def mark_notification_read(request, pk):
    n = get_object_or_404(Notification, pk=pk, user=request.user)
    n.is_read = True
    n.save(update_fields=['is_read'])
    return JsonResponse({'status': 'ok'})

@login_required
def mark_all_read(request):
    if request.method == "POST":
        request.user.notifications.filter(is_read=False).update(is_read=True)
        return JsonResponse({'status': 'ok'})
    return JsonResponse({'status': 'invalid method'}, status=400)

from django.core.paginator import Paginator

@login_required
def notification_list(request):
    thirty_days_ago = timezone.now() - timedelta(days=30)
    notifications = request.user.notifications.filter(created_at__gte=thirty_days_ago).order_by('-created_at')
    
    paginator = Paginator(notifications, 15)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1) if hasattr(paginator, 'get_elided_page_range') else paginator.page_range
    
    query_params = request.GET.copy()
    if 'page' in query_params:
        del query_params['page']
        
    return render(request, 'notifications/list.html', {
        'page_obj': page_obj,
        'notifications': page_obj,
        'page_range': page_range,
        'query_params': query_params.urlencode(),
        'active': 'notifications'
    })
