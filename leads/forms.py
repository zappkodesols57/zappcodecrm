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
