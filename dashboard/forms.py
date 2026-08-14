from django import forms
from .models import DailyReport


class DailyReportForm(forms.ModelForm):
    class Meta:
        model = DailyReport
        fields = [
            "calls_attended", "outgoing_calls", "incoming_calls", "calls_not_connected",
            "leads_cold", "leads_interested", "leads_visited", "admissions_done", "follow_ups_pending",
            "key_highlight", "challenges_faced", "tomorrow_priority", "other_updates",
            "mood_rating",
        ]
        widgets = {
            "calls_attended":     forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "outgoing_calls":     forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "incoming_calls":     forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "calls_not_connected":forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "leads_cold":         forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "leads_interested":   forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "leads_visited":      forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "admissions_done":    forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "follow_ups_pending": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "key_highlight":      forms.TextInput(attrs={"class": "form-control form-control-sm", "placeholder": "e.g. 3 admissions closed, met monthly target..."}),
            "challenges_faced":   forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 3, "placeholder": "Any issues, blockers, or difficult leads..."}),
            "tomorrow_priority":  forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 3, "placeholder": "What will you focus on tomorrow?"}),
            "other_updates":      forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 3, "placeholder": "Any other updates or notes..."}),
            "mood_rating":        forms.Select(attrs={"class": "form-select form-select-sm"}),
        }
