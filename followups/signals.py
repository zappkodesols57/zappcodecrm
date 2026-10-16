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
    from leads.models import LeadTemperature, LeadStage, DealStatus
    # Dynamically update temperature based on positive/negative remarks
    new_temp = (lead.custom_temperature or "").upper()
    if new_temp in LeadTemperature.values:
        lead.temperature = new_temp
        update_kwargs["temperature"] = new_temp

    # Industry-specific follow-up stage progression
    if lead.is_academy_industry:
        if instance.followup_status == 'COMPLETED':
            fu_done_stage = LeadStage.objects.filter(name__iexact='Follow Up Completed', is_active=True).first()
            if fu_done_stage:
                lead.stage = fu_done_stage
                update_kwargs["stage"] = fu_done_stage
        elif not lead.stage or lead.stage.name.lower() in ['new', 'fresh', 'uncontacted', 'assigned']:
            fu_stage = LeadStage.objects.filter(name__iexact='Follow up', is_active=True).first() or LeadStage.objects.filter(name__iexact='Follow-up', is_active=True).first()
            if fu_stage:
                lead.stage = fu_stage
                update_kwargs["stage"] = fu_stage
    elif lead.is_hospital_industry:
        if not lead.stage or lead.stage.name.lower() in ['new', 'fresh', 'uncontacted']:
            fu_stage = LeadStage.objects.filter(name__iexact='Follow up').first() or LeadStage.objects.filter(name__iexact='Follow-up').first() or LeadStage.objects.filter(name__iexact='Contacted').first()
            if fu_stage:
                lead.stage = fu_stage
                update_kwargs["stage"] = fu_stage

    Lead = lead.__class__
    Lead.objects.filter(pk=lead.pk).update(**update_kwargs)


@receiver(post_save, sender=Note)
def _note_activity(sender, instance, created, **kwargs):
    if created:
        lead = instance.lead
        Activity.objects.create(
            lead=lead, activity_type=ActivityType.NOTE,
            description=instance.note, created_by=instance.created_by,
        )
        # Academy specific: Counselor remark added updates deal_status to CONTACTED and recalculates temperature
        if lead and lead.is_academy_industry:
            from leads.models import LeadTemperature, DealStatus
            update_fields = {}
            if lead.deal_status in [DealStatus.OPEN, "", None]:
                lead.deal_status = DealStatus.CONTACTED
                update_fields["deal_status"] = DealStatus.CONTACTED
            new_temp = (lead.custom_temperature or "").upper()
            if new_temp in LeadTemperature.values:
                lead.temperature = new_temp
                update_fields["temperature"] = new_temp
            if update_fields:
                lead.__class__.objects.filter(pk=lead.pk).update(**update_fields)
