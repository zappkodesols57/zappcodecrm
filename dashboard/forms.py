from django import forms
from .models import DailyReport


class DailyReportForm(forms.ModelForm):
    class Meta:
        model = DailyReport
        fields = [
            "leads_assigned", "calls_attended", "outgoing_calls", "incoming_calls", "calls_not_connected",
            "follow_ups_taken", "follow_ups_pending", "appointments_booked", "freeze_leads",
            "leads_cold", "leads_interested", "leads_visited", "admissions_done",
            "key_highlight", "challenges_faced", "tomorrow_priority", "other_updates",
            "mood_rating",
        ]
        widgets = {
            "leads_assigned":     forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "calls_attended":     forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "outgoing_calls":     forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "incoming_calls":     forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "calls_not_connected":forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "follow_ups_taken":   forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "follow_ups_pending": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "appointments_booked":forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "freeze_leads":       forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "leads_cold":         forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "leads_interested":   forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "leads_visited":      forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "admissions_done":    forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "key_highlight":      forms.TextInput(attrs={"class": "form-control form-control-sm", "placeholder": "e.g. Completed all pending calls, 2 appointments confirmed..."}),
            "challenges_faced":   forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 2, "placeholder": "Any issues, blockers, or difficult patient leads..."}),
            "tomorrow_priority":  forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 2, "placeholder": "What will you focus on tomorrow?"}),
            "other_updates":      forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 2, "placeholder": "Any other updates or summary notes..."}),
            "mood_rating":        forms.Select(attrs={"class": "form-select form-select-sm"}),
        }
