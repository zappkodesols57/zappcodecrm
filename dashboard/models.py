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

    # Lead outcomes
    leads_cold = models.PositiveIntegerField(default=0, verbose_name="Cold Leads (Not Interested)")
    leads_interested = models.PositiveIntegerField(default=0, verbose_name="Interested Leads")
    leads_visited = models.PositiveIntegerField(default=0, verbose_name="Leads Who Visited")
    admissions_done = models.PositiveIntegerField(default=0, verbose_name="Admissions Done Today")
    follow_ups_pending = models.PositiveIntegerField(default=0, verbose_name="Follow-ups Pending")

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
