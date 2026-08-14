from django.db.models.signals import post_save
from django.dispatch import receiver

from followups.models import Activity, ActivityType
from .models import Admission


@receiver(post_save, sender=Admission)
def _admission_activity(sender, instance, created, **kwargs):
    if created:
        Activity.objects.create(
            lead=instance.lead, activity_type=ActivityType.ADMISSION,
            description=f"Admission created for {instance.course}. Final fee ₹{instance.final_fee}",
        )
        Lead = instance.lead.__class__
        Lead.objects.filter(pk=instance.lead_id).update(admission_status="ADMISSION_DONE", deal_status="WON")
