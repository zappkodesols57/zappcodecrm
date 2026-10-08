from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from .models import Notification
from datetime import timedelta
from django.utils import timezone

@login_required
def get_unread_notifications(request):
    try:
        tz = timezone.get_current_timezone()
        now = timezone.localtime(timezone.now(), tz)
        today = now.date()
        from datetime import datetime, time
        start_today = timezone.make_aware(datetime.combine(today, time.min), tz)
        end_today = timezone.make_aware(datetime.combine(today, time.max), tz)
        
        from leads.models import Lead, Appointment, AppointmentStatus
        from followups.models import FollowUp, FollowUpStatus
        from dashboard.models import TaskReminder
        
        user = request.user
        is_admin_or_mgr = bool(user.is_superuser or user.role in ('SUPER_ADMIN', 'ADMIN', 'MANAGER'))
        is_doctor = (user.role == 'DOCTOR')
        user_hosp = user.hospital

        # 1. NEW UNCAPTURED LEADS ALERT (For Telecallers & Managers/Admins in Hospital)
        if not is_doctor and user_hosp:
            fresh_uncaptured = Lead.objects.filter(
                hospital=user_hosp,
                assigned_to__isnull=True,
                is_archived=False,
                created_at__range=(start_today, end_today)
            ).order_by('-created_at')[:3]
            for f_lead in fresh_uncaptured:
                notif_t = f"🆕 New Unassigned Lead: {f_lead.name}"
                fu_link = f"/leads/{f_lead.pk}/"
                exists = Notification.objects.filter(
                    user=user,
                    created_at__range=(start_today, end_today),
                    title=notif_t
                ).exists()
                if not exists:
                    Notification.objects.create(
                        user=user,
                        title=notif_t,
                        message=f"A new inquiry for {f_lead.name} (#{f_lead.lead_code or f_lead.pk}) is waiting to be captured.",
                        link=fu_link
                    )

        # 2. FOLLOW-UP REMINDERS (30m before & 5m before alerts)
        fu_qs = FollowUp.objects.filter(
            followup_date=today,
            followup_status=FollowUpStatus.PENDING
        ).select_related('lead')
        if not is_admin_or_mgr:
            fu_qs = fu_qs.filter(lead__assigned_to=user)
        elif user_hosp:
            fu_qs = fu_qs.filter(lead__hospital=user_hosp)

        for fu in fu_qs:
            if not fu.lead:
                continue
            time_msg = ""
            alert_types = []  # ('30m', '5m', 'due')
            
            if fu.followup_time:
                fu_dt = timezone.datetime.combine(today, fu.followup_time)
                fu_dt = timezone.make_aware(fu_dt, tz)
                diff_minutes = (fu_dt - now).total_seconds() / 60.0
                time_msg = f" at {fu.followup_time.strftime('%I:%M %p')}"
                
                # 30-min window alert (25 to 35 mins before)
                if 25 <= diff_minutes <= 35:
                    alert_types.append(('30m', f"⏳ Follow-up in 30 Mins: {fu.lead.name}", f"Upcoming call with patient {fu.lead.name}{time_msg} in 30 minutes."))
                # 5-min window alert (-10 to 5 mins before)
                if -10 <= diff_minutes <= 6:
                    alert_types.append(('5m', f"⏰ Follow-up Due Now: {fu.lead.name}", f"Scheduled follow-up with patient {fu.lead.name}{time_msg} is due. Mobile: {fu.lead.mobile}"))
            else:
                alert_types.append(('daily', f"⏰ Follow-up Due Today: {fu.lead.name}", f"Follow-up with patient {fu.lead.name} is scheduled for today."))

            for a_key, notif_title, notif_msg in alert_types:
                fu_link = f"/leads/{fu.lead.pk}/"
                exists = Notification.objects.filter(
                    user=user,
                    created_at__range=(start_today, end_today),
                    title=notif_title
                ).exists()
                if not exists:
                    Notification.objects.create(
                        user=user,
                        title=notif_title,
                        message=notif_msg,
                        link=fu_link
                    )

        # 3. APPOINTMENT REMINDERS (30m before & 5m before for Doctor and Telecaller)
        apt_qs = Appointment.objects.filter(
            appointment_date=today,
            status__in=[AppointmentStatus.APPROVED, AppointmentStatus.SCHEDULED, AppointmentStatus.PENDING_APPROVAL]
        ).select_related('lead', 'doctor_user')
        if is_doctor:
            apt_qs = apt_qs.filter(Q(doctor_user=user) | Q(doctor_name__icontains=user.get_full_name() or user.username))
        elif not is_admin_or_mgr:
            apt_qs = apt_qs.filter(lead__assigned_to=user)
        elif user_hosp:
            apt_qs = apt_qs.filter(hospital=user_hosp)

        for apt in apt_qs:
            if not apt.lead:
                continue
            time_msg = ""
            apt_alerts = []
            if apt.appointment_time:
                apt_dt = timezone.datetime.combine(today, apt.appointment_time)
                apt_dt = timezone.make_aware(apt_dt, tz)
                diff_minutes = (apt_dt - now).total_seconds() / 60.0
                time_msg = f" at {apt.appointment_time.strftime('%I:%M %p')}"
                if 25 <= diff_minutes <= 35:
                    apt_alerts.append(('30m', f"📅 Appointment in 30 Mins: {apt.lead.name}", f"Appointment with Dr. {apt.doctor_name} for patient {apt.lead.name}{time_msg} starts in 30 minutes."))
                if -10 <= diff_minutes <= 6:
                    apt_alerts.append(('5m', f"🩺 Appointment Due: {apt.lead.name}", f"Appointment with Dr. {apt.doctor_name} for patient {apt.lead.name}{time_msg} is starting now."))
            else:
                apt_alerts.append(('daily', f"📅 Appointment Today: {apt.lead.name}", f"Patient {apt.lead.name} has an appointment scheduled with Dr. {apt.doctor_name} today."))

            for a_key, apt_title, apt_msg in apt_alerts:
                apt_link = f"/leads/{apt.lead.pk}/"
                exists = Notification.objects.filter(
                    user=user,
                    created_at__range=(start_today, end_today),
                    title=apt_title
                ).exists()
                if not exists:
                    Notification.objects.create(
                        user=user,
                        title=apt_title,
                        message=apt_msg,
                        link=apt_link
                    )

        # 4. Check pending task reminders for this user scheduled for today
        pending_tasks = TaskReminder.objects.filter(
            user=user,
            due_date=today,
            status__in=[TaskReminder.Status.PENDING, TaskReminder.Status.IN_PROGRESS]
        ).select_related('lead')

        for task in pending_tasks:
            time_msg = ""
            should_alert = False
            if task.due_time:
                task_dt = timezone.datetime.combine(today, task.due_time)
                task_dt = timezone.make_aware(task_dt, tz)
                diff_minutes = (task_dt - now).total_seconds() / 60.0
                time_msg = f" at {task.due_time.strftime('%I:%M %p')}"
                if -60 <= diff_minutes <= 30:
                    should_alert = True
            else:
                should_alert = True

            if should_alert:
                task_title = f"📋 Task Reminder: {task.title}"
                task_link = f"/leads/{task.lead.pk}/" if task.lead else "/dashboard/tasks/"
                task_desc = f" ({task.description[:80]}...)" if task.description else ""
                
                exists = Notification.objects.filter(
                    user=user,
                    created_at__range=(start_today, end_today)
                ).filter(
                    Q(title=task_title) | (Q(link=task_link) & Q(title__icontains=task.title))
                ).exists()

                if not exists:
                    Notification.objects.create(
                        user=user,
                        title=task_title,
                        message=f"Task '{task.title}'{time_msg} is due today.{task_desc}",
                        link=task_link
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
    except Exception as e:
        # Gracefully handle temporary network drop / DB reconnect without crashing client
        return JsonResponse({'count': 0, 'notifications': [], 'status': 'retry'})

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
