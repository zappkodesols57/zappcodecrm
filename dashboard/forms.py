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
    """Clean EOD form for Hospital Telecaller / Staff with 3-field layout."""
    MOOD_CHOICES = [
        ("Great", "Great"),
        ("Good", "Good"),
        ("Moderate", "Moderate"),
        ("Tired", "Tired"),
        ("Exhausted", "Exhausted"),
        ("Sick", "Sick"),
    ]
    mood = forms.ChoiceField(
        choices=MOOD_CHOICES,
        initial="Good",
        required=False,
        widget=forms.Select(attrs={"class": "form-select no-tom-select"})
    )

    class Meta:
        model = DailyReport
        fields = [
            "leads_assigned", "appointments_booked", "payments_done",
            "follow_ups_taken", "leads_interested", "leads_cold",
            "freeze_leads", "leads_visited", "calls_attended",
            "incoming_calls", "outgoing_calls", "calls_not_connected",
            "mood", "challenges_faced", "tomorrow_priority",
        ]
        widgets = {
            "leads_assigned":     forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()"}),
            "appointments_booked":forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()"}),
            "payments_done":      forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()"}),
            "follow_ups_taken":   forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()"}),
            "leads_interested":   forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()"}),
            "leads_cold":         forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()"}),
            "freeze_leads":       forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()"}),
            "leads_visited":      forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()"}),
            "calls_attended":     forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()"}),
            "incoming_calls":     forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()"}),
            "outgoing_calls":     forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()"}),
            "calls_not_connected":forms.NumberInput(attrs={"class": "form-control", "min": "0", "step": "1", "placeholder": "0", "onfocus": "this.select()"}),
            "challenges_faced":   forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "Enter remarks / notes for today..."}),
            "tomorrow_priority":  forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "Enter tomorrow's plan / tasks..."}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.required = False

    def clean(self):
        cleaned_data = super().clean()
        int_fields = [
            "leads_assigned", "appointments_booked", "payments_done",
            "follow_ups_taken", "leads_interested", "leads_cold",
            "freeze_leads", "leads_visited", "calls_attended",
            "incoming_calls", "outgoing_calls", "calls_not_connected",
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
