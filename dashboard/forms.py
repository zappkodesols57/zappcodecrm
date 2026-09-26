from django import forms
from .models import DailyReport


class AcademyDailyReportForm(forms.ModelForm):
    """Clean custom EOD form for Zappcode Academy (Counsellor & HR roles)."""
    class Meta:
        model = DailyReport
        fields = [
            "leads_assigned",
            "calls_attended",
            "admissions_done",
            "payments_done",
            "pending_leads",
            "tomorrow_followups",
            "mood",
        ]
        widgets = {
            "leads_assigned":     forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "Enter assigned leads"}),
            "calls_attended":     forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "Enter today's calls"}),
            "admissions_done":    forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "Enter admissions done"}),
            "payments_done":      forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "Enter payments done"}),
            "pending_leads":      forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "Enter pending leads"}),
            "tomorrow_followups": forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "Enter tomorrow's follow-ups"}),
            "mood":               forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name != "mood":
                field.required = False

    def clean(self):
        cleaned_data = super().clean()
        int_fields = [
            "leads_assigned", "calls_attended", "admissions_done",
            "payments_done", "pending_leads", "tomorrow_followups",
        ]
        for field in int_fields:
            val = cleaned_data.get(field)
            if val is not None and val < 0:
                cleaned_data[field] = 0
        return cleaned_data


class HospitalDailyReportForm(forms.ModelForm):
    """Specific form for Hospital / Nelson Medical consultations."""
    MOOD_RATING_CHOICES = [
        (1, "😞 Very Low"),
        (2, "😕 Low"),
        (3, "😐 Moderate"),
        (4, "🙂 Good"),
        (5, "😄 Great"),
    ]
    mood_rating = forms.TypedChoiceField(
        choices=MOOD_RATING_CHOICES,
        coerce=int,
        initial=3,
        required=False,
        widget=forms.Select(attrs={"class": "form-select no-tom-select"})
    )

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
            "leads_assigned":     forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()", "oninput": "if(this.value.length > 1 && this.value.startsWith('0')) this.value = this.value.replace(/^0+/, '') || '0'"}),
            "calls_attended":     forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()", "oninput": "if(this.value.length > 1 && this.value.startsWith('0')) this.value = this.value.replace(/^0+/, '') || '0'"}),
            "outgoing_calls":     forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()", "oninput": "if(this.value.length > 1 && this.value.startsWith('0')) this.value = this.value.replace(/^0+/, '') || '0'"}),
            "incoming_calls":     forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()", "oninput": "if(this.value.length > 1 && this.value.startsWith('0')) this.value = this.value.replace(/^0+/, '') || '0'"}),
            "calls_not_connected":forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()", "oninput": "if(this.value.length > 1 && this.value.startsWith('0')) this.value = this.value.replace(/^0+/, '') || '0'"}),
            "follow_ups_taken":   forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()", "oninput": "if(this.value.length > 1 && this.value.startsWith('0')) this.value = this.value.replace(/^0+/, '') || '0'"}),
            "follow_ups_pending": forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()", "oninput": "if(this.value.length > 1 && this.value.startsWith('0')) this.value = this.value.replace(/^0+/, '') || '0'"}),
            "appointments_booked":forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()", "oninput": "if(this.value.length > 1 && this.value.startsWith('0')) this.value = this.value.replace(/^0+/, '') || '0'"}),
            "freeze_leads":       forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()", "oninput": "if(this.value.length > 1 && this.value.startsWith('0')) this.value = this.value.replace(/^0+/, '') || '0'"}),
            "leads_cold":         forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()", "oninput": "if(this.value.length > 1 && this.value.startsWith('0')) this.value = this.value.replace(/^0+/, '') || '0'"}),
            "leads_interested":   forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()", "oninput": "if(this.value.length > 1 && this.value.startsWith('0')) this.value = this.value.replace(/^0+/, '') || '0'"}),
            "leads_visited":      forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()", "oninput": "if(this.value.length > 1 && this.value.startsWith('0')) this.value = this.value.replace(/^0+/, '') || '0'"}),
            "admissions_done":    forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()", "oninput": "if(this.value.length > 1 && this.value.startsWith('0')) this.value = this.value.replace(/^0+/, '') || '0'"}),
            "key_highlight":      forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Completed all patient calls, 2 appointments confirmed..."}),
            "challenges_faced":   forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "Any issues, blockers, or difficult patient leads..."}),
            "tomorrow_priority":  forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "What will you focus on tomorrow?"}),
            "other_updates":      forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "Any other updates or summary notes..."}),
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


class DoctorDailyReportForm(forms.ModelForm):
    """Specific clean EOD form for Doctors."""
    MOOD_RATING_CHOICES = [
        (1, "😞 Very Low"),
        (2, "😕 Low"),
        (3, "😐 Moderate"),
        (4, "🙂 Good"),
        (5, "😄 Great"),
    ]
    mood_rating = forms.TypedChoiceField(
        choices=MOOD_RATING_CHOICES,
        coerce=int,
        initial=3,
        required=False,
        widget=forms.Select(attrs={"class": "form-select no-tom-select"})
    )

    class Meta:
        model = DailyReport
        fields = [
            "leads_assigned",       # Appointment requests received today
            "appointments_booked",  # Appointments accepted / approved today
            "pending_leads",        # Today's scheduled appointments
            "freeze_leads",         # Appointments cancelled
            "admissions_done",      # Appointments completed
            "tomorrow_followups",   # Tomorrow's scheduled appointments
            "mood_rating",          # Mood / Energy rating
        ]
        widgets = {
            "leads_assigned":     forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()", "oninput": "if(this.value.length > 1 && this.value.startsWith('0')) this.value = this.value.replace(/^0+/, '') || '0'"}),
            "appointments_booked":forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()", "oninput": "if(this.value.length > 1 && this.value.startsWith('0')) this.value = this.value.replace(/^0+/, '') || '0'"}),
            "pending_leads":      forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()", "oninput": "if(this.value.length > 1 && this.value.startsWith('0')) this.value = this.value.replace(/^0+/, '') || '0'"}),
            "freeze_leads":       forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()", "oninput": "if(this.value.length > 1 && this.value.startsWith('0')) this.value = this.value.replace(/^0+/, '') || '0'"}),
            "admissions_done":    forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()", "oninput": "if(this.value.length > 1 && this.value.startsWith('0')) this.value = this.value.replace(/^0+/, '') || '0'"}),
            "tomorrow_followups": forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()", "oninput": "if(this.value.length > 1 && this.value.startsWith('0')) this.value = this.value.replace(/^0+/, '') || '0'"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name != "mood_rating":
                field.required = False

    def clean(self):
        cleaned_data = super().clean()
        int_fields = [
            "leads_assigned", "appointments_booked", "pending_leads",
            "freeze_leads", "admissions_done", "tomorrow_followups",
        ]
        for field in int_fields:
            val = cleaned_data.get(field)
            if val is not None and val < 0:
                cleaned_data[field] = 0
        return cleaned_data


# Alias for backward compatibility
DailyReportForm = HospitalDailyReportForm
