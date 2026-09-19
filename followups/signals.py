from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import FollowUp, Note, Activity, ActivityType


@receiver(post_save, sender=FollowUp)
def _followup_activity(sender, instance, created, **kwargs):
    lead = instance.lead
    if created:
        Activity.objects.create(
            lead=lead, activity_type=ActivityType.FOLLOWUP,
            description=f"[{instance.get_followup_mode_display()}] {instance.get_followup_status_display()}: {instance.comment or '(no comment)'}",
            created_by=instance.created_by,
        )
    # Refresh denormalized cache on Lead
    today_date = __import__('datetime').date.today()
    latest = lead.followups.order_by("-followup_date", "-followup_time").first()
    
    # Only active/pending follow-ups should set upcoming next_followup_date
    pending_fus = lead.followups.filter(
        followup_status__in=['PENDING', 'CALL_BACK', 'RESCHEDULED'],
        next_followup_date__isnull=False
    )
    # Get the nearest upcoming follow-up (soonest future next_followup_date)
    upcoming = pending_fus.filter(next_followup_date__gte=today_date).order_by("next_followup_date").first()
    # If no future pending follow-up, take the most recently scheduled pending one
    if not upcoming:
        upcoming = pending_fus.order_by("-next_followup_date").first()

    lead.last_followup_date = latest.followup_date if latest else lead.last_followup_date
    lead.next_followup_date = upcoming.next_followup_date if upcoming else None
    lead.next_followup_time = upcoming.next_followup_time if upcoming else None
    lead.followup_count = lead.followups.count()

    update_kwargs = {
        "last_followup_date": lead.last_followup_date,
        "next_followup_date": lead.next_followup_date,
        "next_followup_time": lead.next_followup_time,
        "followup_count": lead.followup_count,
    }
    from leads.models import LeadTemperature, LeadStage
    if lead.temperature == LeadTemperature.UNCONTACTED or lead.temperature == "UNCONTACTED":
        lead.temperature = LeadTemperature.WARM
        update_kwargs["temperature"] = LeadTemperature.WARM
    if not lead.stage or lead.stage.name.lower() in ['new', 'fresh', 'uncontacted']:
        fu_stage = LeadStage.objects.filter(name__iexact='Follow-up').first() or LeadStage.objects.filter(name__iexact='Contacted').first()
        if fu_stage:
            lead.stage = fu_stage
            update_kwargs["stage"] = fu_stage

    Lead = lead.__class__
    Lead.objects.filter(pk=lead.pk).update(**update_kwargs)


@receiver(post_save, sender=Note)
def _note_activity(sender, instance, created, **kwargs):
    if created:
        Activity.objects.create(
            lead=instance.lead, activity_type=ActivityType.NOTE,
            description=instance.note, created_by=instance.created_by,
        )
