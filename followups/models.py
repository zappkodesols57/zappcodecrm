import uuid
from django.conf import settings
from django.db import models


class FollowUpMode(models.TextChoices):
    CALL = "CALL", "Call"
    CALL_OUTGOING = "CALL_OUTGOING", "Outgoing Call"
    CALL_INCOMING = "CALL_INCOMING", "Incoming Call"
    WHATSAPP = "WHATSAPP", "WhatsApp"
    SMS = "SMS", "SMS"
    EMAIL = "EMAIL", "Email"
    VISIT = "VISIT", "Visit"
    OTHER = "OTHER", "Other"


class FollowUpStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    COMPLETED = "COMPLETED", "Completed"
    NOT_CONNECTED = "NOT_CONNECTED", "Not Connected"
    INTERESTED = "INTERESTED", "Interested"
    NOT_INTERESTED = "NOT_INTERESTED", "Not Interested"
    RESCHEDULED = "RESCHEDULED", "Rescheduled"
    CANCELLED = "CANCELLED", "Cancelled"


class FollowUp(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey("leads.Lead", on_delete=models.CASCADE, related_name="followups")
    followup_date = models.DateField(db_index=True)
    followup_time = models.TimeField(null=True, blank=True)
    followup_mode = models.CharField(max_length=20, choices=FollowUpMode.choices, default=FollowUpMode.CALL)
    followup_status = models.CharField(max_length=20, choices=FollowUpStatus.choices, default=FollowUpStatus.PENDING, db_index=True)
    comment = models.TextField(blank=True)
    next_followup_date = models.DateField(null=True, blank=True)
    next_followup_time = models.TimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # provenance for rows migrated from historical Excel date-columns
    imported_from_excel = models.BooleanField(default=False)

    class Meta:
        ordering = ["-followup_date", "-followup_time"]

    def __str__(self):
        return f"{self.lead.lead_code} follow-up {self.followup_date}"


class Note(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey("leads.Lead", on_delete=models.CASCADE, related_name="lead_notes")
    note = models.TextField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]


class ActivityType(models.TextChoices):
    LEAD_CREATED = "LEAD_CREATED", "Lead Created"
    STAGE_CHANGE = "STAGE_CHANGE", "Stage Changed"
    TEMPERATURE_CHANGE = "TEMPERATURE_CHANGE", "Temperature Changed"
    ASSIGNMENT = "ASSIGNMENT", "Assignment Changed"
    FOLLOWUP = "FOLLOWUP", "Follow-up"
    NOTE = "NOTE", "Note Added"
    ADMISSION = "ADMISSION", "Admission"
    PAYMENT = "PAYMENT", "Payment"
    SYSTEM = "SYSTEM", "System Event"


class Activity(models.Model):
    """Feeds the Lead Timeline. Written by signals/views whenever something
    timeline-worthy happens — never edited after the fact."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey("leads.Lead", on_delete=models.CASCADE, related_name="activities")
    activity_type = models.CharField(max_length=25, choices=ActivityType.choices)
    description = models.TextField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "Activities"
