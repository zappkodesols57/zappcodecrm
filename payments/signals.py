from django.db.models.signals import post_save
from django.dispatch import receiver
from django.db.models import Q

from followups.models import Activity, ActivityType
from notifications.models import Notification
from accounts.models import User
from .models import Payment, PaymentStatus


@receiver(post_save, sender=Payment)
def _payment_activity(sender, instance, created, **kwargs):
    if created:
        lead = getattr(instance.admission, 'lead', None) if hasattr(instance, 'admission') and instance.admission else None
        if lead:
            Activity.objects.create(
                lead=lead, activity_type=ActivityType.PAYMENT,
                description=f"Payment of ₹{instance.amount} recorded ({instance.get_payment_mode_display()}, {instance.get_payment_status_display()})",
            )

            # Notify Admin / Superadmin when payment is successfully recorded
            if instance.payment_status == PaymentStatus.SUCCESS:
                admin_qs = User.objects.filter(is_active=True).filter(
                    Q(is_superuser=True) |
                    Q(role__in=[User.Role.SUPER_ADMIN, User.Role.ADMIN])
                )
                if lead.hospital:
                    admin_qs = admin_qs.filter(Q(hospital=lead.hospital) | Q(hospital__isnull=True))
                
                for adm in admin_qs.distinct():
                    Notification.objects.create(
                        user=adm,
                        title=f"💰 Payment Completed: {lead.name}",
                        message=f"Payment of ₹{instance.amount} ({instance.get_payment_mode_display()}) successfully completed for patient {lead.name}.",
                        link=f"/leads/{lead.pk}/",
                    )
