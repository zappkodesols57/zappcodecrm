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
    gender = forms.ChoiceField(choices=[], required=False)
    age = forms.IntegerField(required=False)
    department = forms.ChoiceField(choices=[], required=False)
    doctor = forms.ChoiceField(choices=[], required=False)
    appointment_status = forms.ChoiceField(choices=[], required=False)
    appo_booked_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), required=False)
    visit_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), required=False)
    priority = forms.ChoiceField(choices=[], required=False)
    location = forms.ChoiceField(choices=[], required=False)
    
    # Custom Overrides for System Fields
    deal_status = forms.ChoiceField(choices=[], required=False)
    campaign = forms.ChoiceField(choices=[], required=False)
    lead_source = forms.ChoiceField(choices=[], required=False)
    
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
            "name", "mobile", "location", 
            "source_category", "assigned_to", "notes"
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
            for field in ['gender', 'age', 'department', 'doctor', 'appointment_status', 'appo_booked_date', 'visit_date', 'priority', 'uhid_id_no', 'ipd_no', 'pharmacy_bill', 'opd_bill', 'investigation', 'total', 'calling_date_remark_1', 'remark_1', 'calling_date_remark_2', 'calling_time_remark_2', 'remark_2', 'calling_date_remark_3', 'remark_3', 'deal_status', 'campaign', 'lead_source']:
                if field in cd:
                    self.fields[field].initial = cd[field]

        # Restrict assignment for Lead Attendants
        if user and not user.can_assign_leads:
            if "assigned_to" in self.fields:
                self.fields.pop("assigned_to")
        elif "assigned_to" in self.fields:
            if user and user.hospital:
                self.fields["assigned_to"].queryset = self.fields["assigned_to"].queryset.filter(hospital=user.hospital)
            self.fields["assigned_to"].empty_label = "Select Assignee"
                
        # Populate Master Data dropdowns dynamically
        from leads.models import MasterGroup, MasterItem
        from django.db.models import Q
        
        def get_tenant_items(group_name):
            try:
                group = MasterGroup.objects.filter(name__iexact=group_name).first()
                if not group:
                    return []
                items = group.items.filter(is_active=True)
                if user and user.hospital:
                    # Filter by hospital or universal items
                    items = items.filter(Q(hospital=user.hospital) | Q(hospital__isnull=True))
                return list(items.values_list("name", "name"))
            except Exception as ex:
                print(f"Error loading {group_name}: {ex}")
                return []
        
        try:
            loc_items = list(MasterItem.objects.filter(group__name__iexact="Locations", is_active=True).order_by("name").values_list("name", "name"))
            self.fields["location"].choices = [("", "-- Select Patient Location (City, State) --")] + loc_items
            self.fields["department"].choices = [("", "-- Select Department --")] + get_tenant_items("Departments")
            self.fields["doctor"].choices = [("", "-- Select Doctor --")] + get_tenant_items("Doctors")
            self.fields["gender"].choices = [("", "-- Select Gender --")] + get_tenant_items("Genders")
            self.fields["priority"].choices = [("", "-- Select Priority --")] + get_tenant_items("Priorities")
            self.fields["appointment_status"].choices = [("", "-- Select Appointment Status --")] + get_tenant_items("Appointment Statuses")
            self.fields["deal_status"].choices = [("", "-- Select Deal Status --")] + get_tenant_items("Deal Statuses")
            self.fields["campaign"].choices = [("", "-- Select Campaign --")] + get_tenant_items("Campaigns")
            self.fields["lead_source"].choices = [("", "-- Select Lead Source --")] + get_tenant_items("Lead Sources")
        except Exception as e:
            import traceback
            traceback.print_exc()

        if "source_category" in self.fields:
            self.fields["source_category"].queryset = SourceCategory.objects.filter(is_active=True).order_by("order", "name")
            
        for name, field in self.fields.items():
            css = "form-select" if isinstance(field.widget, (forms.Select, forms.SelectMultiple)) else "form-control"
            field.widget.attrs.setdefault("class", css)

    def save(self, commit=True):
        instance = super().save(commit=False)
        cd = instance.custom_data or {}
        
        # Intelligently split and save standard location into City, State, and Location columns
        loc_val = self.cleaned_data.get('location')
        if loc_val:
            loc_str = str(loc_val).strip()
            instance.location = loc_str
            if "," in loc_str:
                parts = [p.strip() for p in loc_str.split(",", 1)]
                instance.city = parts[0]
                instance.state = parts[1]
            else:
                instance.city = loc_str
            
        # Extract custom fields
        custom_fields = ['location', 'gender', 'age', 'department', 'doctor', 'appointment_status', 'priority', 'uhid_id_no', 'ipd_no', 'pharmacy_bill', 'opd_bill', 'investigation', 'total', 'remark_1', 'remark_2', 'remark_3', 'deal_status', 'campaign', 'lead_source']
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
