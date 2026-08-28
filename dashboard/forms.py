from django import forms
from .models import DailyReport


class AcademyDailyReportForm(forms.ModelForm):
    """Specific form for Zappcode / Education Academies (no hospital fields)."""
    class Meta:
        model = DailyReport
        fields = [
            "leads_visited", "admissions_done", "fees_collected", "leads_assigned", "follow_ups_taken",
            "calls_attended", "outgoing_calls", "incoming_calls", "calls_not_connected",
            "leads_interested", "leads_cold", "freeze_leads", "follow_ups_pending",
            "key_highlight", "challenges_faced", "tomorrow_priority", "other_updates",
            "mood_rating",
        ]
        widgets = {
            "leads_visited":      forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "admissions_done":    forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "fees_collected":     forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "0.01", "oninput": "if(this.value < 0) this.value = 0", "placeholder": "0.00"}),
            "leads_assigned":     forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "follow_ups_taken":   forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "calls_attended":     forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "outgoing_calls":     forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "incoming_calls":     forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "calls_not_connected":forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "leads_interested":   forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "leads_cold":         forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "freeze_leads":       forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "follow_ups_pending": forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "key_highlight":      forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Completed all scheduled follow-ups, 2 admissions confirmed..."}),
            "challenges_faced":   forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "Any issues, blockers, or difficult student inquiries..."}),
            "tomorrow_priority":  forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "What will you focus on tomorrow?"}),
            "other_updates":      forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "Any other updates or summary notes..."}),
            "mood_rating":        forms.Select(attrs={"class": "form-select no-tom-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name not in ["key_highlight", "challenges_faced", "tomorrow_priority", "other_updates", "mood_rating"]:
                field.required = False

    def clean(self):
        cleaned_data = super().clean()
        int_fields = [
            "leads_visited", "admissions_done", "leads_assigned", "follow_ups_taken",
            "calls_attended", "outgoing_calls", "incoming_calls", "calls_not_connected",
            "leads_interested", "leads_cold", "freeze_leads", "follow_ups_pending",
        ]
        for field in int_fields:
            val = cleaned_data.get(field)
            if val is not None and val < 0:
                cleaned_data[field] = 0
        fees = cleaned_data.get("fees_collected")
        if fees is not None and fees < 0:
            cleaned_data["fees_collected"] = 0
        return cleaned_data


class HospitalDailyReportForm(forms.ModelForm):
    """Specific form for Hospital / Nelson Medical consultations."""
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
            "leads_assigned":     forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "calls_attended":     forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "outgoing_calls":     forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "incoming_calls":     forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "calls_not_connected":forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "follow_ups_taken":   forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "follow_ups_pending": forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "appointments_booked":forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "freeze_leads":       forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "leads_cold":         forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "leads_interested":   forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "leads_visited":      forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "admissions_done":    forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "oninput": "this.value = this.value.replace(/[^0-9]/g, '')", "placeholder": "0"}),
            "key_highlight":      forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Completed all patient calls, 2 appointments confirmed..."}),
            "challenges_faced":   forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "Any issues, blockers, or difficult patient leads..."}),
            "tomorrow_priority":  forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "What will you focus on tomorrow?"}),
            "other_updates":      forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "Any other updates or summary notes..."}),
            "mood_rating":        forms.Select(attrs={"class": "form-select no-tom-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name not in ["key_highlight", "challenges_faced", "tomorrow_priority", "other_updates", "mood_rating"]:
                field.required = False

    def clean(self):
        cleaned_data = super().clean()
        int_fields = [
            "leads_assigned", "calls_attended", "outgoing_calls", "incoming_calls", "calls_not_connected",
            "follow_ups_taken", "follow_ups_pending", "appointments_booked", "freeze_leads",
            "leads_cold", "leads_interested", "leads_visited", "admissions_done",
        ]
        for field in int_fields:
            val = cleaned_data.get(field)
            if val is not None and val < 0:
                cleaned_data[field] = 0
        return cleaned_data


# Alias for backward compatibility
DailyReportForm = HospitalDailyReportForm
