from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver

from audit.utils import log_action
from followups.models import Activity, ActivityType
from .models import Lead


@receiver(pre_save, sender=Lead)
def _stash_previous_state(sender, instance, **kwargs):
    if instance.pk:
        try:
            instance._previous = Lead.objects.get(pk=instance.pk)
        except Lead.DoesNotExist:
            instance._previous = None
    else:
        instance._previous = None


@receiver(post_save, sender=Lead)
def _lead_activity_and_audit(sender, instance, created, **kwargs):
    if created:
        source = instance.lead_source.name if instance.lead_source else "Unknown"
        Activity.objects.create(
            lead=instance,
            activity_type=ActivityType.LEAD_CREATED,
            description=f"Lead created. Source: {source}"
            + (f", Campaign: {instance.campaign.name}" if instance.campaign else ""),
            created_by=instance.created_by,
        )
        log_action("LEAD_CREATED", obj=instance, new_value=str(instance))
        return

    prev = getattr(instance, "_previous", None)
    if prev is None:
        return

    if prev.stage_id != instance.stage_id:
        Activity.objects.create(
            lead=instance, activity_type=ActivityType.STAGE_CHANGE,
            description=f"Stage changed from '{prev.stage}' to '{instance.stage}'",
        )
        log_action("STAGE_CHANGE", obj=instance, old_value=prev.stage, new_value=instance.stage)

    if prev.temperature != instance.temperature:
        Activity.objects.create(
            lead=instance, activity_type=ActivityType.TEMPERATURE_CHANGE,
            description=f"Temperature changed from '{prev.get_temperature_display()}' to '{instance.get_temperature_display()}'",
        )
        log_action("TEMPERATURE_CHANGE", obj=instance, old_value=prev.temperature, new_value=instance.temperature)

    if prev.assigned_to_id != instance.assigned_to_id:
        Activity.objects.create(
            lead=instance, activity_type=ActivityType.ASSIGNMENT,
            description=f"Assigned to '{instance.assigned_to}' (was '{prev.assigned_to}')",
        )
        log_action("ASSIGNMENT", obj=instance, old_value=str(prev.assigned_to), new_value=str(instance.assigned_to))

    if prev.source_category_id != instance.source_category_id or prev.lead_source_id != instance.lead_source_id:
        log_action(
            "ATTRIBUTION_CHANGE", obj=instance,
            old_value=f"{prev.source_category}/{prev.lead_source}",
            new_value=f"{instance.source_category}/{instance.lead_source}",
        )
