import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models


class PaymentPlan(models.TextChoices):
    FULL = "FULL", "One-time Full Payment"
    EMI = "EMI", "Installment / EMI Plan"


class CourseStatus(models.TextChoices):
    NOT_STARTED = "NOT_STARTED", "Not Started"
    STARTED = "STARTED", "Started"
    ONGOING = "ONGOING", "Ongoing"
    COMPLETED = "COMPLETED", "Completed"
    DROPOUT = "DROPOUT", "Dropout"


class Admission(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.OneToOneField("leads.Lead", on_delete=models.CASCADE, related_name="admission")
    student_name = models.CharField(max_length=150)
    course = models.ForeignKey("leads.Course", on_delete=models.SET_NULL, null=True)
    admission_date = models.DateField()
    total_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    max_allowed_discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    extra_discount_reason = models.TextField(blank=True)
    final_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_plan = models.CharField(max_length=15, choices=PaymentPlan.choices, default=PaymentPlan.FULL)
    course_status = models.CharField(max_length=15, choices=CourseStatus.choices, default=CourseStatus.ONGOING)
    assigned_counselor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        total_fee = Decimal(str(self.total_fee or 0))
        discount = Decimal(str(self.discount or 0))
        self.final_fee = total_fee - discount
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Admission — {self.student_name}"

    @property
    def collected(self):
        return sum(p.amount for p in self.payments.filter(payment_status="SUCCESS"))

    @property
    def pending(self):
        return self.final_fee - self.collected


class Installment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    admission = models.ForeignKey(Admission, on_delete=models.CASCADE, related_name="installments")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    due_date = models.DateField()
    is_paid = models.BooleanField(default=False)
    paid_date = models.DateField(null=True, blank=True)
    reminder_sent_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["due_date"]

    def __str__(self):
        return f"₹{self.amount} due on {self.due_date} - {self.admission.student_name}"
