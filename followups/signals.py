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
    # refresh denormalized cache on Lead
    latest = lead.followups.order_by("-followup_date", "-followup_time").first()
    upcoming = lead.followups.filter(next_followup_date__isnull=False).order_by("-followup_date").first()
    lead.last_followup_date = latest.followup_date if latest else lead.last_followup_date
    lead.next_followup_date = upcoming.next_followup_date if upcoming else lead.next_followup_date
    lead.next_followup_time = upcoming.next_followup_time if upcoming else lead.next_followup_time
    lead.followup_count = lead.followups.count()
    Lead = lead.__class__
    Lead.objects.filter(pk=lead.pk).update(
        last_followup_date=lead.last_followup_date,
        next_followup_date=lead.next_followup_date,
        next_followup_time=lead.next_followup_time,
        followup_count=lead.followup_count,
    )


@receiver(post_save, sender=Note)
def _note_activity(sender, instance, created, **kwargs):
    if created:
        Activity.objects.create(
            lead=instance.lead, activity_type=ActivityType.NOTE,
            description=instance.note, created_by=instance.created_by,
        )
