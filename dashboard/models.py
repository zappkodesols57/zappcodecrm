import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone


class DailyReport(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="daily_reports")
    report_date = models.DateField(default=timezone.localdate, db_index=True)

    # Call stats
    calls_attended = models.PositiveIntegerField(default=0, verbose_name="Calls Attended (Total)")
    calls_not_connected = models.PositiveIntegerField(default=0, verbose_name="Calls Not Connected")
    outgoing_calls = models.PositiveIntegerField(default=0, verbose_name="Outgoing Calls Made")
    incoming_calls = models.PositiveIntegerField(default=0, verbose_name="Incoming Calls Received")

    # Lead outcomes & Overview
    leads_assigned = models.PositiveIntegerField(default=0, verbose_name="Leads Assigned Today")
    appointments_booked = models.PositiveIntegerField(default=0, verbose_name="Appointments Booked / Approved")
    freeze_leads = models.PositiveIntegerField(default=0, verbose_name="Freeze Leads (Cancelled / Not Interested)")
    leads_cold = models.PositiveIntegerField(default=0, verbose_name="Cold Leads (Not Interested)")
    leads_interested = models.PositiveIntegerField(default=0, verbose_name="Interested Leads")
    leads_visited = models.PositiveIntegerField(default=0, verbose_name="Student Visits (Leads Visited)")
    admissions_done = models.PositiveIntegerField(default=0, verbose_name="Admissions Completed Today")
    fees_collected = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="Fees Payments Collected (₹)")
    follow_ups_pending = models.PositiveIntegerField(default=0, verbose_name="Follow-ups Pending")
    follow_ups_taken = models.PositiveIntegerField(default=0, verbose_name="Follow-ups Taken Today")

    # Login / Logout info for the day
    first_login_at = models.DateTimeField(null=True, blank=True, verbose_name="First Login Time")
    last_logout_at = models.DateTimeField(null=True, blank=True, verbose_name="Last Logout Time")

    # Qualitative
    key_highlight = models.CharField(max_length=300, blank=True, verbose_name="Key Highlight / Achievement")
    challenges_faced = models.TextField(blank=True, verbose_name="Challenges Faced")
    tomorrow_priority = models.TextField(blank=True, verbose_name="Tomorrow's Priority / Plan")
    other_updates = models.TextField(blank=True, verbose_name="Other Updates / Summary")

    # Mood / Energy self-rating (1–5)
    MOOD_CHOICES = [(1, "😞 Very Low"), (2, "😕 Low"), (3, "😐 Okay"), (4, "🙂 Good"), (5, "😄 Excellent")]
    mood_rating = models.PositiveSmallIntegerField(default=3, choices=MOOD_CHOICES, verbose_name="Energy / Mood Rating")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-report_date", "-created_at"]
        unique_together = ("user", "report_date")

    def __str__(self):
        return f"{self.user.username} - {self.report_date}"


class TaskReminder(models.Model):
    class Priority(models.TextChoices):
        LOW = "LOW", "Low"
        MEDIUM = "MEDIUM", "Medium"
        HIGH = "HIGH", "High"
        URGENT = "URGENT", "Urgent / Critical"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        IN_PROGRESS = "IN_PROGRESS", "In Progress"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tasks_created")
    lead = models.ForeignKey("leads.Lead", on_delete=models.SET_NULL, null=True, blank=True, related_name="linked_tasks")
    title = models.CharField(max_length=255, verbose_name="Task Title")
    description = models.TextField(blank=True, verbose_name="Task Details")
    due_date = models.DateField(default=timezone.localdate, verbose_name="Due Date")
    due_time = models.TimeField(null=True, blank=True, verbose_name="Due Time / Reminder Time")
    priority = models.CharField(max_length=20, choices=Priority.choices, default=Priority.MEDIUM)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    sync_to_followup = models.BooleanField(default=False, verbose_name="Sync as Follow-up on Lead")
    is_reported_to_admin = models.BooleanField(default=False, verbose_name="Reported to Admin")
    admin_report_notes = models.TextField(blank=True, verbose_name="Report Summary sent to Admin")
    reported_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "due_date", "due_time", "-created_at"]

    def __str__(self):
        return f"{self.title} ({self.get_status_display()})"

