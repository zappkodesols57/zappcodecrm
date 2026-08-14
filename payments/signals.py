from django.db.models.signals import post_save
from django.dispatch import receiver

from followups.models import Activity, ActivityType
from .models import Payment


@receiver(post_save, sender=Payment)
def _payment_activity(sender, instance, created, **kwargs):
    if created:
        Activity.objects.create(
            lead=instance.admission.lead, activity_type=ActivityType.PAYMENT,
            description=f"Payment of ₹{instance.amount} recorded ({instance.get_payment_mode_display()}, {instance.get_payment_status_display()})",
        )
