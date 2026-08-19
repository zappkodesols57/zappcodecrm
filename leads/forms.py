from django import forms
from .models import Lead, SourceCategory, LeadSource, Campaign, Course, LeadStage, Tag


class LeadForm(forms.ModelForm):
    class Meta:
        model = Lead
        fields = [
            "name", "mobile", "alternate_mobile", "email", "city", "state", "location",
            "education", "qualification", "graduation_year",
            "course", "lead_type", "temperature", "stage", "deal_status", "admission_status", "inquiry_date",
            "source_category", "lead_source", "campaign", "ad_platform", "campaign_id_text",
            "referral_type", "referral_person", "referral_contact", "referral_notes", "landing_page",
            "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
            "assigned_to", "assigned_manager",
            "notes", "tags",
        ]
        widgets = {
            "inquiry_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
            "referral_notes": forms.Textarea(attrs={"rows": 2}),
            "tags": forms.SelectMultiple(),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        
        if user and not user.can_assign_leads:
            if "assigned_to" in self.fields:
                self.fields.pop("assigned_to")
            if "assigned_manager" in self.fields:
                self.fields.pop("assigned_manager")

        # Filter master choice fields to active options from Master Data
        if "course" in self.fields:
            qs = Course.objects.filter(is_active=True)
            if self.instance and self.instance.pk and self.instance.course:
                qs = qs | Course.objects.filter(pk=self.instance.course.pk)
            self.fields["course"].queryset = qs.distinct().order_by("name")

        if "source_category" in self.fields:
            qs = SourceCategory.objects.filter(is_active=True)
            if self.instance and self.instance.pk and self.instance.source_category:
                qs = qs | SourceCategory.objects.filter(pk=self.instance.source_category.pk)
            self.fields["source_category"].queryset = qs.distinct().order_by("order", "name")

        if "lead_source" in self.fields:
            qs = LeadSource.objects.filter(is_active=True)
            if self.instance and self.instance.pk and self.instance.lead_source:
                qs = qs | LeadSource.objects.filter(pk=self.instance.lead_source.pk)
            self.fields["lead_source"].queryset = qs.distinct().order_by("order", "name")

        if "campaign" in self.fields:
            qs = Campaign.objects.filter(is_active=True)
            if self.instance and self.instance.pk and self.instance.campaign:
                qs = qs | Campaign.objects.filter(pk=self.instance.campaign.pk)
            self.fields["campaign"].queryset = qs.distinct().order_by("-id")

        if "stage" in self.fields:
            qs = LeadStage.objects.filter(is_active=True)
            if self.instance and self.instance.pk and self.instance.stage:
                qs = qs | LeadStage.objects.filter(pk=self.instance.stage.pk)
            self.fields["stage"].queryset = qs.distinct().order_by("order", "name")

        for name, field in self.fields.items():
            if name == "tags":
                continue
            css = "form-select" if isinstance(field.widget, (forms.Select, forms.SelectMultiple)) else "form-control"
            field.widget.attrs.setdefault("class", css)
        required = {"name", "mobile", "stage", "inquiry_date"}
        for name in required:
            self.fields[name].required = True


class QuickImportRow:
    """Not a Django form — just a typed container used by the import pipeline."""
    pass


class SourceCategoryForm(forms.ModelForm):
    class Meta:
        model = SourceCategory
        fields = ["name", "order", "is_active"]


class LeadSourceForm(forms.ModelForm):
    class Meta:
        model = LeadSource
        fields = ["name", "category", "order", "is_active"]


class CampaignForm(forms.ModelForm):
    class Meta:
        model = Campaign
        fields = ["name", "platform", "campaign_id", "landing_page", "cost", "start_date", "end_date", "is_active"]
        widgets = {"start_date": forms.DateInput(attrs={"type": "date"}), "end_date": forms.DateInput(attrs={"type": "date"})}


class CourseForm(forms.ModelForm):
    class Meta:
        model = Course
        fields = ["name", "base_price", "max_discount", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name == "is_active":
                field.widget.attrs["class"] = "form-check-input"
            else:
                css = "form-select" if isinstance(field.widget, (forms.Select, forms.SelectMultiple)) else "form-control"
                field.widget.attrs.setdefault("class", css)


class LeadStageForm(forms.ModelForm):
    class Meta:
        model = LeadStage
        fields = ["name", "order", "is_active"]



class HospitalLeadForm(forms.ModelForm):
    # Medical & Demographic Fields
    gender = forms.ChoiceField(choices=[('', 'Select'), ('Male', 'Male'), ('Female', 'Female'), ('Other', 'Other')], required=False)
    age = forms.IntegerField(required=False)
    department = forms.CharField(max_length=100, required=False)
    doctor = forms.CharField(max_length=150, required=False)
    appointment_status = forms.ChoiceField(choices=[('Not Booked', 'Not Booked'), ('Booked', 'Booked'), ('Cancelled', 'Cancelled')], required=False, initial='Not Booked')
    appo_booked_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), required=False)
    visit_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), required=False)
    priority = forms.ChoiceField(choices=[('', 'Select'), ('Hot', 'Hot'), ('Warm', 'Warm'), ('Cold', 'Cold')], required=False)
    location = forms.ChoiceField(choices=[], required=False)
    
    # Billing & ID
    uhid_id_no = forms.CharField(max_length=100, required=False, label="UHID ID NO")
    ipd_no = forms.CharField(max_length=100, required=False, label="IPD NO")
    pharmacy_bill = forms.DecimalField(max_digits=10, decimal_places=2, required=False)
    opd_bill = forms.DecimalField(max_digits=10, decimal_places=2, required=False)
    investigation = forms.CharField(max_length=255, required=False)
    total = forms.DecimalField(max_digits=12, decimal_places=2, required=False)
    
    # Remarks Tracking
    calling_date_remark_1 = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), required=False)
    remark_1 = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)
    
    calling_date_remark_2 = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), required=False)
    calling_time_remark_2 = forms.TimeField(widget=forms.TimeInput(attrs={"type": "time"}), required=False)
    remark_2 = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)
    
    calling_date_remark_3 = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), required=False)
    remark_3 = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)

    class Meta:
        model = Lead
        fields = [
            "name", "mobile", "location", "deal_status", 
            "source_category", "lead_source", "campaign", "assigned_to", "notes"
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        
        # Load JSON fields into form
        if self.instance and self.instance.pk and self.instance.custom_data:
            cd = self.instance.custom_data
            for field in ['gender', 'age', 'department', 'doctor', 'appointment_status', 'appo_booked_date', 'visit_date', 'priority', 'uhid_id_no', 'ipd_no', 'pharmacy_bill', 'opd_bill', 'investigation', 'total', 'calling_date_remark_1', 'remark_1', 'calling_date_remark_2', 'calling_time_remark_2', 'remark_2', 'calling_date_remark_3', 'remark_3']:
                if field in cd:
                    self.fields[field].initial = cd[field]

        # Restrict assignment for Lead Attendants
        if user and not user.can_assign_leads:
            if "assigned_to" in self.fields:
                self.fields.pop("assigned_to")
                
        # Filter Master Data
        try:
            from leads.models import MasterGroup
            group, _ = MasterGroup.objects.get_or_create(name="Locations")
            items = group.items.filter(is_active=True).values_list("name", "name")
            self.fields["location"].choices = [("", "Select Location (City, State)")] + list(items)
        except Exception:
            pass

        if "source_category" in self.fields:
            self.fields["source_category"].queryset = SourceCategory.objects.filter(is_active=True).order_by("order", "name")
        if "lead_source" in self.fields:
            self.fields["lead_source"].queryset = LeadSource.objects.filter(is_active=True).order_by("order", "name")
        if "campaign" in self.fields:
            self.fields["campaign"].queryset = Campaign.objects.filter(is_active=True).order_by("-id")
            
        for name, field in self.fields.items():
            css = "form-select" if isinstance(field.widget, (forms.Select, forms.SelectMultiple)) else "form-control"
            field.widget.attrs.setdefault("class", css)

    def save(self, commit=True):
        instance = super().save(commit=False)
        cd = instance.custom_data or {}
        
        # Extract custom fields
        custom_fields = ['gender', 'age', 'department', 'doctor', 'appointment_status', 'priority', 'uhid_id_no', 'ipd_no', 'pharmacy_bill', 'opd_bill', 'investigation', 'total', 'remark_1', 'remark_2', 'remark_3']
        date_time_fields = ['appo_booked_date', 'visit_date', 'calling_date_remark_1', 'calling_date_remark_2', 'calling_time_remark_2', 'calling_date_remark_3']
        
        for field in custom_fields:
            val = self.cleaned_data.get(field)
            if val is not None and str(val).strip() != "":
                # Convert Decimals to string for JSON
                cd[field] = str(val) if isinstance(val, (int, float)) or hasattr(val, 'quantize') else val
            else:
                cd.pop(field, None)
                
        for field in date_time_fields:
            val = self.cleaned_data.get(field)
            if val:
                cd[field] = val.isoformat()
            else:
                cd.pop(field, None)
                
        instance.custom_data = cd
        if commit:
            instance.save()
            self.save_m2m()
        return instance
