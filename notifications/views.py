from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from .models import Notification
from datetime import timedelta
from django.utils import timezone

@login_required
def get_unread_notifications(request):
    notifications = request.user.notifications.filter(is_read=False).order_by('-created_at')[:10]
    data = []
    for n in notifications:
        data.append({
            'id': n.id,
            'title': n.title,
            'message': n.message,
            'link': n.link,
            'created_at': n.created_at.strftime("%Y-%m-%d %H:%M"),
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
