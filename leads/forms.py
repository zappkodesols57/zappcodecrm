import re
from datetime import datetime, date
from django.utils import timezone
from django import forms
from django.db import models
from accounts.models import User
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
            "mobile": forms.TextInput(attrs={
                "class": "form-control",
                "maxlength": "10",
                "minlength": "10",
                "pattern": "^[0-9]{10}$",
                "inputmode": "numeric",
                "placeholder": "10-digit mobile number",
                "oninput": "this.value=this.value.replace(/[^0-9]/g,'').slice(0,10)",
            }),
            "alternate_mobile": forms.TextInput(attrs={
                "class": "form-control",
                "maxlength": "10",
                "minlength": "10",
                "pattern": "^[0-9]{10}$",
                "inputmode": "numeric",
                "placeholder": "10-digit alternate mobile",
                "oninput": "this.value=this.value.replace(/[^0-9]/g,'').slice(0,10)",
            }),
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

        if "mobile" in self.fields:
            self.fields["mobile"].widget.attrs.update({
                "maxlength": "10",
                "minlength": "10",
                "pattern": "^[0-9]{10}$",
                "inputmode": "numeric",
                "placeholder": "10-digit mobile number",
                "oninput": "this.value=this.value.replace(/[^0-9]/g,'').slice(0,10)",
            })
        if "alternate_mobile" in self.fields:
            self.fields["alternate_mobile"].widget.attrs.update({
                "maxlength": "10",
                "minlength": "10",
                "pattern": "^[0-9]{10}$",
                "inputmode": "numeric",
                "placeholder": "10-digit alternate mobile",
                "oninput": "this.value=this.value.replace(/[^0-9]/g,'').slice(0,10)",
            })

    def clean_mobile(self):
        mobile = self.cleaned_data.get("mobile", "")
        if mobile:
            digits = re.sub(r"\D", "", str(mobile))
            if len(digits) == 12 and digits.startswith("91"):
                digits = digits[2:]
            elif len(digits) == 11 and digits.startswith("0"):
                digits = digits[1:]
            if len(digits) != 10:
                raise forms.ValidationError("Mobile number must be exactly 10 digits.")
            return digits
        return mobile

    def clean_alternate_mobile(self):
        alt = self.cleaned_data.get("alternate_mobile", "")
        if alt:
            digits = re.sub(r"\D", "", str(alt))
            if len(digits) == 12 and digits.startswith("91"):
                digits = digits[2:]
            elif len(digits) == 11 and digits.startswith("0"):
                digits = digits[1:]
            if len(digits) != 10:
                raise forms.ValidationError("Alternate mobile number must be exactly 10 digits.")
            return digits
        return alt


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



class NonStrictChoiceField(forms.ChoiceField):
    """ChoiceField that renders options in <select> dropdown while allowing any value without validation error."""
    def validate(self, value):
        if self.required and not value:
            raise forms.ValidationError(self.error_messages['required'], code='required')

class HospitalLeadForm(forms.ModelForm):
    # Medical & Demographic Fields - using NonStrictChoiceField so choices render in <select> dropdowns and any custom/dynamic value is accepted
    gender = NonStrictChoiceField(choices=[], required=False)
    age = forms.IntegerField(required=False)
    department = NonStrictChoiceField(choices=[], required=False)
    doctor = NonStrictChoiceField(choices=[], required=False)
    appointment_status = NonStrictChoiceField(choices=[], required=False)
    appo_booked_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), required=False)
    appointment_time = forms.TimeField(widget=forms.TimeInput(attrs={"type": "time"}), required=False)
    followup_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), required=False)
    followup_time = forms.TimeField(widget=forms.TimeInput(attrs={"type": "time"}), required=False)
    visit_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), required=False)
    priority = NonStrictChoiceField(choices=[], required=False)
    # Location & Comments
    location = NonStrictChoiceField(choices=[], required=False)
    comments = forms.CharField(widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Enter patient notes, summary, or comments..."}), required=False, label="Comments / Notes")
    
    # Custom Overrides for System Fields
    deal_status = NonStrictChoiceField(choices=[], required=False)
    campaign = NonStrictChoiceField(choices=[], required=False)
    lead_source = NonStrictChoiceField(choices=[], required=False)
    
    cancellation_reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Enter reason for cancellation / not interested..."}), required=False, label="Reason for Cancellation / Not Interested")
    
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
            "mobile": forms.TextInput(attrs={
                "class": "form-control",
                "maxlength": "10",
                "minlength": "10",
                "pattern": "^[0-9]{10}$",
                "inputmode": "numeric",
                "placeholder": "10-digit mobile number",
                "oninput": "this.value=this.value.replace(/[^0-9]/g,'').slice(0,10)",
            }),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        self.current_user = user
        super().__init__(*args, **kwargs)
        
        cd = (self.instance.custom_data or {}) if (self.instance and self.instance.pk) else {}
        # Load JSON fields into form
        if self.instance and self.instance.pk:
            for field in ['gender', 'age', 'department', 'doctor', 'appointment_status', 'appo_booked_date', 'appointment_time', 'followup_date', 'followup_time', 'visit_date', 'priority', 'uhid_id_no', 'ipd_no', 'pharmacy_bill', 'opd_bill', 'investigation', 'total', 'calling_date_remark_1', 'remark_1', 'calling_date_remark_2', 'calling_time_remark_2', 'remark_2', 'calling_date_remark_3', 'remark_3', 'deal_status', 'campaign', 'lead_source', 'comments', 'location', 'cancellation_reason']:
                if field in cd and field in self.fields:
                    self.fields[field].initial = cd[field]
            
            # Load Campaign from Model ForeignKey or custom_data
            if not self.fields['campaign'].initial:
                if self.instance.campaign:
                    self.fields['campaign'].initial = self.instance.campaign.name
                elif self.instance.original_campaign:
                    self.fields['campaign'].initial = self.instance.original_campaign.name
                elif cd.get('campaign'):
                    self.fields['campaign'].initial = cd.get('campaign')

            # Load Lead Source from Model ForeignKey or custom_data
            if not self.fields['lead_source'].initial:
                if self.instance.lead_source:
                    self.fields['lead_source'].initial = self.instance.lead_source.name
                elif self.instance.original_lead_source:
                    self.fields['lead_source'].initial = self.instance.original_lead_source.name
                elif cd.get('lead_source'):
                    self.fields['lead_source'].initial = cd.get('lead_source')
                elif cd.get('source'):
                    self.fields['lead_source'].initial = cd.get('source')

            # Load Location from Lead.location / Lead.city if not set
            if not self.fields['location'].initial:
                loc_val = self.instance.location or self.instance.city or cd.get('city') or cd.get('location')
                if loc_val:
                    self.fields['location'].initial = loc_val

            # If comments not in custom_data, load from instance.notes
            if not self.fields['comments'].initial and self.instance.notes:
                self.fields['comments'].initial = self.instance.notes
            
            # If appointment is booked/approved/scheduled/completed or payment done, ensure appointment_status reflects Booking
            curr_st = str(self.fields['appointment_status'].initial or cd.get('appointment_status') or '').strip().upper()
            from leads.models import Appointment
            has_appt = Appointment.objects.filter(lead=self.instance).exists()
            if has_appt or any(k in curr_st for k in ['COMPLET', 'PAYMENT', 'CONFIRM', 'APPROVED', 'SCHEDULED', 'BOOK']):
                self.fields['appointment_status'].initial = 'Booking'

            # If appointment_time is not in custom_data, load from Appointment relation
            if not self.fields['appointment_time'].initial:
                apt = Appointment.objects.filter(lead=self.instance).order_by('-id').first()
                if apt and apt.appointment_time:
                    self.fields['appointment_time'].initial = apt.appointment_time.strftime('%H:%M')
                elif apt and apt.appointment_date:
                    self.fields['appo_booked_date'].initial = apt.appointment_date

        # Restrict assignment: Only Admin and Manager can choose Lead Owner / Assignee.
        # Lead Attendants are locked to their own user account.
        if "assigned_to" in self.fields:
            self.fields["assigned_to"].label = "Assign To / Lead Owner"
            self.fields["assigned_to"].widget.attrs.update({"class": "form-select"})
            if user and user.hospital:
                self.fields["assigned_to"].queryset = User.objects.filter(hospital=user.hospital, is_active=True)
            else:
                self.fields["assigned_to"].queryset = User.objects.filter(is_active=True)
            self.fields["assigned_to"].empty_label = "-- Select User / Attendant --"
            
            is_manager_or_admin = user and (user.role in [User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER] or user.can_assign_leads or user.is_superuser)
            if not is_manager_or_admin:
                # Lock to current user
                self.fields["assigned_to"].initial = user.pk if user else None
                self.fields["assigned_to"].disabled = True
            else:
                if not self.instance.pk and user:
                    self.fields["assigned_to"].initial = user.pk
                
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
            # 1. Departments filtered by Branch if available
            from leads.models import HospitalDepartment, HospitalDoctor, HospitalBranch, HospitalDisease
            init_branch = cd.get("hospital_branch") or cd.get("branch")
            dept_qs = HospitalDepartment.objects.filter(is_active=True)
            if user and user.hospital:
                dept_qs = dept_qs.filter(hospital=user.hospital)
            if init_branch:
                dept_qs = dept_qs.filter(branches__name__iexact=str(init_branch))

            model_depts = [(d.name, d.name) for d in dept_qs]
            master_depts = get_tenant_items("Departments") if not init_branch else []
            
            all_dept_names = set()
            combined_depts = []
            for d_val, d_lbl in (model_depts + master_depts):
                if d_val and d_val.lower() not in all_dept_names:
                    all_dept_names.add(d_val.lower())
                    combined_depts.append((d_val, d_lbl))

            if init_branch:
                self.fields["department"].choices = [("", "-- Select Department --")] + combined_depts
            else:
                self.fields["department"].choices = [("", "-- Select Hospital Branch First --")] + combined_depts
            
            # 2. Doctors from HospitalDoctor model filtered by Department
            init_dept = self.fields.get("department") and self.fields["department"].initial
            hdoc_qs = HospitalDoctor.objects.filter(is_active=True)
            if user and user.hospital:
                hdoc_qs = hdoc_qs.filter(hospital=user.hospital)
            if init_dept:
                hdoc_qs = hdoc_qs.filter(models.Q(departments__name__iexact=str(init_dept)) | models.Q(department__name__iexact=str(init_dept)))
            
            model_doctors = [(doc.name, f"Dr. {doc.name}" if not doc.name.lower().startswith("dr") else doc.name) for doc in hdoc_qs]

            registered_doctor_qs = User.objects.filter(role=User.Role.DOCTOR, is_active=True)
            if user and user.hospital:
                registered_doctor_qs = registered_doctor_qs.filter(hospital=user.hospital)
            registered_doctors = []
            for doc in registered_doctor_qs:
                doc_display = doc.get_full_name().strip() or doc.username
                registered_doctors.append((doc_display, f"Dr. {doc_display}" if not doc_display.lower().startswith("dr") else doc_display))

            master_doctors = get_tenant_items("Doctors") if not init_dept else []
                
            # Merge and deduplicate choices preserving order
            all_doc_names = set()
            combined_doctors = []
            for doc_val, doc_lbl in (model_doctors + (registered_doctors if not init_dept else []) + master_doctors):
                if doc_val and doc_val.lower() not in all_doc_names:
                    all_doc_names.add(doc_val.lower())
                    combined_doctors.append((doc_val, doc_lbl))
                    
            if init_dept:
                self.fields["doctor"].choices = [("", "-- Select Doctor --")] + combined_doctors
            else:
                self.fields["doctor"].choices = [("", "-- Select Department First --")] + combined_doctors

            # -------------------------------------------------------------------
            # UNIFIED OPTIONS RESOLVER:
            # When options are configured in LeadCustomField (Edit Form Field),
            # use strictly those options. Falls back to MasterItem only if not configured.
            # -------------------------------------------------------------------
            from leads.models import LeadCustomField, Campaign, LeadSource
            def get_field_options(field_name, master_group_name, fallback_default_prompt):
                # 1. If configured in Form Field settings, use strictly configured options
                cf_obj = LeadCustomField.objects.filter(hospital=user.hospital, name=field_name).first() if (user and user.hospital) else None
                if cf_obj and cf_obj.get_options_list():
                    results = [(opt.strip(), opt.strip()) for opt in cf_obj.get_options_list() if opt.strip()]
                    # For campaign & lead source, also include any active model items
                    if field_name == "campaign":
                        seen = {r[0].lower() for r in results}
                        for c in Campaign.objects.filter(hospital=user.hospital, is_active=True):
                            if c.name.strip() and c.name.strip().lower() not in seen:
                                seen.add(c.name.strip().lower())
                                results.append((c.name.strip(), c.name.strip()))
                    return results

                # 2. Fallback to MasterItem group entries
                return get_tenant_items(master_group_name)

            self.fields["gender"].choices = [("", "-- Select Gender --")] + get_field_options("gender", "Genders", "-- Select Gender --")
            self.fields["priority"].choices = [("", "-- Select Priority --")] + get_field_options("priority", "Priorities", "-- Select Priority --")
            appt_status_options = get_field_options("appointment_status", "Appointment Statuses", "-- Select Appointment Status --")
            curr_appt = self.fields["appointment_status"].initial
            if curr_appt:
                opt_dict = {o[0].lower(): o[0] for o in appt_status_options}
                if str(curr_appt).lower() not in opt_dict:
                    # If 'Booked' and 'Booking' is in choices, map initial to 'Booking'
                    if str(curr_appt).lower() == 'booked' and 'booking' in opt_dict:
                        self.fields["appointment_status"].initial = opt_dict['booking']
                    elif str(curr_appt).lower() == 'booking' and 'booked' in opt_dict:
                        self.fields["appointment_status"].initial = opt_dict['booked']
                    else:
                        appt_status_options.insert(0, (str(curr_appt), str(curr_appt)))
            self.fields["appointment_status"].choices = [("", "-- Select Appointment Status --")] + appt_status_options
            self.fields["deal_status"].choices = [("", "-- Select Deal Status --")] + get_field_options("deal_status", "Deal Statuses", "-- Select Deal Status --")

            master_campaigns = get_field_options("campaign", "Campaigns", "-- Select Campaign --")
            master_sources = get_field_options("lead_source", "Lead Sources", "-- Select Lead Source --")
            
            # Ensure current initial values are preserved if present
            curr_camp = self.fields["campaign"].initial
            camp_dict = {c[0].lower(): c for c in master_campaigns}
            if curr_camp and str(curr_camp).lower() not in camp_dict:
                master_campaigns.insert(0, (str(curr_camp), str(curr_camp)))

            curr_src = self.fields["lead_source"].initial
            src_dict = {s[0].lower(): s for s in master_sources}
            if curr_src and str(curr_src).lower() not in src_dict:
                master_sources.insert(0, (str(curr_src), str(curr_src)))

            curr_loc = self.fields["location"].initial
            loc_options = get_field_options("location", "Locations", "-- Select Patient Location --")
            loc_names = {l[0].lower() for l in loc_options if l[0]}
            if curr_loc and str(curr_loc).lower() not in loc_names:
                loc_options.insert(0, (str(curr_loc), str(curr_loc)))

            self.fields["location"].choices = [("", "-- Select Patient Location (City, State) --")] + loc_options
            self.fields["campaign"].choices = [("", "-- Select Campaign --")] + master_campaigns
            self.fields["lead_source"].choices = [("", "-- Select Lead Source --")] + master_sources
        except Exception as e:
            import traceback
            traceback.print_exc()

        if "source_category" in self.fields:
            self.fields["source_category"].queryset = SourceCategory.objects.filter(is_active=True).order_by("order", "name")
            
        # Dynamically load Admin-Configured Custom Form Fields (Non-system only)
        from leads.models import LeadCustomField
        cf_qs = LeadCustomField.objects.filter(is_active=True, is_system=False)
        if user and user.hospital:
            cf_qs = cf_qs.filter(hospital=user.hospital)
        
        self.dynamic_custom_fields = list(cf_qs.order_by("order", "created_at"))
        for cf in self.dynamic_custom_fields:
            fname = f"dyn_{cf.name}"
            field_initial = cd.get(cf.name, "")
            
            if cf.field_type == LeadCustomField.FieldType.NUMBER:
                self.fields[fname] = forms.IntegerField(required=cf.is_required, label=cf.label, initial=field_initial or None)
            elif cf.field_type == LeadCustomField.FieldType.DECIMAL:
                self.fields[fname] = forms.DecimalField(max_digits=12, decimal_places=2, required=cf.is_required, label=cf.label, initial=field_initial or None)
            elif cf.field_type == LeadCustomField.FieldType.DATE:
                self.fields[fname] = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), required=cf.is_required, label=cf.label, initial=field_initial or None)
            elif cf.field_type == LeadCustomField.FieldType.TEXTAREA:
                self.fields[fname] = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=cf.is_required, label=cf.label, initial=field_initial)
            elif cf.field_type == LeadCustomField.FieldType.CHECKBOX:
                self.fields[fname] = forms.BooleanField(required=cf.is_required, label=cf.label, initial=bool(field_initial))
            elif cf.field_type == LeadCustomField.FieldType.DROPDOWN:
                if cf.name == "disease":
                    from leads.models import HospitalDisease
                    init_dept = self.fields.get("department") and self.fields["department"].initial
                    dis_qs = HospitalDisease.objects.filter(is_active=True)
                    if user and user.hospital:
                        dis_qs = dis_qs.filter(hospital=user.hospital)
                    if init_dept:
                        dis_qs = dis_qs.filter(department__name__iexact=str(init_dept))
                        opts = [("", "-- Select Disease / Condition --")] + [(dis.name, dis.name) for dis in dis_qs]
                    else:
                        opts = [("", "-- Select Department First --")] + [(dis.name, f"{dis.name} ({dis.department.name})") for dis in dis_qs]
                elif cf.name in ["hospital_branch", "branch"]:
                    from leads.models import HospitalBranch
                    b_qs = HospitalBranch.objects.filter(is_active=True)
                    if user and user.hospital:
                        b_qs = b_qs.filter(hospital=user.hospital)
                    opts = [("", f"-- Select {cf.label} --")] + [(b.name, b.name) for b in b_qs]
                else:
                    opts = [("", f"-- Select {cf.label} --")] + [(opt, opt) for opt in cf.get_options_list()]
                self.fields[fname] = NonStrictChoiceField(choices=opts, required=cf.is_required, label=cf.label, initial=field_initial)
            else: # TEXT
                self.fields[fname] = forms.CharField(max_length=255, required=cf.is_required, label=cf.label, initial=field_initial)

            if cf.placeholder:
                self.fields[fname].widget.attrs["placeholder"] = cf.placeholder
            if cf.help_text:
                self.fields[fname].help_text = cf.help_text

        for name, field in self.fields.items():
            if isinstance(field, forms.BooleanField):
                field.widget.attrs.setdefault("class", "form-check-input")
            else:
                css = "form-select" if isinstance(field.widget, (forms.Select, forms.SelectMultiple)) else "form-control"
                field.widget.attrs.setdefault("class", css)

        # Attach bound fields to dynamic_custom_fields list for direct template access
        for cf in self.dynamic_custom_fields:
            fname = f"dyn_{cf.name}"
            cf.form_field = self[fname]

        # -----------------------------------------------------------------------
        # UNIFIED DYNAMIC FORM FIELDS PIPELINE
        # Reads configuration directly from LeadCustomField DB table
        # -----------------------------------------------------------------------
        all_ordered_items = []
        
        # Load all active fields for this hospital
        if user and user.hospital:
            configured_fields = LeadCustomField.objects.filter(hospital=user.hospital, is_active=True).order_by('order', 'id')
        else:
            configured_fields = LeadCustomField.objects.filter(hospital__isnull=True, is_active=True).order_by('order', 'id')
            
        for fld in configured_fields:
            if fld.is_system:
                # Update standard field label / required / placeholder if overridden
                if fld.name in self.fields:
                    if fld.label:
                        self.fields[fld.name].label = fld.label
                    self.fields[fld.name].required = fld.is_required
                    if fld.placeholder and hasattr(self.fields[fld.name].widget, 'attrs'):
                        self.fields[fld.name].widget.attrs['placeholder'] = fld.placeholder
                    all_ordered_items.append({
                        "type": "standard",
                        "key": fld.name,
                        "order": fld.order,
                        "is_required": fld.is_required,
                    })
            else:
                fld.form_field = self[f"dyn_{fld.name}"]
                all_ordered_items.append({
                    "type": "custom",
                    "key": f"dyn_{fld.name}",
                    "order": fld.order,
                    "cf": fld,
                    "is_required": fld.is_required,
                })

        # Fallback if DB not yet initialized: keep default standard sequence
        if not all_ordered_items:
            base_standard_fields = [
                {"type": "standard", "key": "name", "order": 1},
                {"type": "standard", "key": "mobile", "order": 2},
                {"type": "standard", "key": "age", "order": 3},
                {"type": "standard", "key": "gender", "order": 4},
                {"type": "standard", "key": "comments", "order": 5},
                {"type": "standard", "key": "location", "order": 6},
                {"type": "standard", "key": "doctor", "order": 7},
                {"type": "standard", "key": "department", "order": 8},
                {"type": "standard", "key": "lead_source", "order": 9},
                {"type": "standard", "key": "appointment_status", "order": 10},
                {"type": "standard", "key": "campaign", "order": 11},
            ]
            all_ordered_items = base_standard_fields

        # Group fields into rows (Row-by-Row Pairing) so left and right fields remain strictly aligned
        self.field_rows = []
        for i in range(0, len(all_ordered_items), 2):
            left_item = all_ordered_items[i]
            right_item = all_ordered_items[i + 1] if i + 1 < len(all_ordered_items) else None
            self.field_rows.append({
                "left": left_item,
                "right": right_item
            })

        # Backward compatibility
        self.left_column_fields = [r["left"] for r in self.field_rows if r["left"]]
        self.right_column_fields = [r["right"] for r in self.field_rows if r["right"]]

        if "mobile" in self.fields:
            self.fields["mobile"].widget.attrs.update({
                "maxlength": "10",
                "minlength": "10",
                "pattern": "^[0-9]{10}$",
                "inputmode": "numeric",
                "placeholder": "10-digit mobile number",
                "oninput": "this.value=this.value.replace(/[^0-9]/g,'').slice(0,10)",
            })

    def clean_mobile(self):
        mobile = self.cleaned_data.get("mobile", "")
        if mobile:
            digits = re.sub(r"\D", "", str(mobile))
            if len(digits) == 12 and digits.startswith("91"):
                digits = digits[2:]
            elif len(digits) == 11 and digits.startswith("0"):
                digits = digits[1:]
            if len(digits) != 10:
                raise forms.ValidationError("Mobile number must be exactly 10 digits.")
            return digits
        return mobile

    def save(self, commit=True):
        instance = super().save(commit=False)
        cd = instance.custom_data or {}
        
        is_manager_or_admin = self.current_user and (self.current_user.role in [User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER] or self.current_user.can_assign_leads)
        if not is_manager_or_admin and self.current_user:
            instance.assigned_to = self.current_user

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
        custom_fields = ['location', 'gender', 'age', 'department', 'doctor', 'appointment_status', 'priority', 'uhid_id_no', 'ipd_no', 'pharmacy_bill', 'opd_bill', 'investigation', 'total', 'remark_1', 'remark_2', 'remark_3', 'deal_status', 'campaign', 'lead_source', 'comments']
        date_time_fields = ['appo_booked_date', 'appointment_time', 'followup_date', 'followup_time', 'visit_date', 'calling_date_remark_1', 'calling_date_remark_2', 'calling_time_remark_2', 'calling_date_remark_3']
        
        # Save comments into instance.notes as well
        if self.cleaned_data.get('comments'):
            instance.notes = self.cleaned_data.get('comments')
        elif self.cleaned_data.get('notes'):
            instance.notes = self.cleaned_data.get('notes')
        
        for field in custom_fields:
            val = self.cleaned_data.get(field)
            if val is not None and str(val).strip() != "":
                # Convert Decimals to string for JSON
                cd[field] = str(val) if isinstance(val, (int, float)) or hasattr(val, 'quantize') else val
            else:
                cd.pop(field, None)

        # Save dynamic custom fields configured by Admin
        for cf in getattr(self, 'dynamic_custom_fields', []):
            fname = f"dyn_{cf.name}"
            dval = self.cleaned_data.get(fname)
            if dval is not None and str(dval).strip() != "":
                if isinstance(dval, (datetime, timezone.datetime)):
                    cd[cf.name] = dval.isoformat()
                elif hasattr(dval, 'isoformat'):
                    cd[cf.name] = dval.isoformat()
                else:
                    cd[cf.name] = str(dval) if isinstance(dval, (int, float)) or hasattr(dval, 'quantize') else dval
            else:
                cd.pop(cf.name, None)
                
        for field in date_time_fields:
            val = self.cleaned_data.get(field)
            if val:
                cd[field] = val.isoformat()
            else:
                cd.pop(field, None)
                
        appo_status = str(self.cleaned_data.get('appointment_status') or '').strip()
        appo_status_upper = appo_status.upper()
        appo_date = self.cleaned_data.get('appo_booked_date')
        doc_name = self.cleaned_data.get('doctor')
        
        # Check if Appointment is already Completed or doctor completed it
        from leads.models import Appointment, AppointmentStatus
        existing_apt = Appointment.objects.filter(lead=instance).order_by('-id').first() if instance.pk else None
        is_already_completed = (existing_apt and existing_apt.status == AppointmentStatus.COMPLETED) or \
                               ('COMPLET' in appo_status_upper or 'DONE' in appo_status_upper or 'VISIT' in appo_status_upper)

        # Check if Billing / Payment details are entered
        total_val = float(self.cleaned_data.get('total') or 0.0)
        opd_val = float(self.cleaned_data.get('opd_bill') or 0.0)
        pharm_val = float(self.cleaned_data.get('pharmacy_bill') or 0.0)
        has_payment = (total_val > 0) or (opd_val > 0) or (pharm_val > 0)

        is_booking_selected = ("BOOK" in appo_status_upper)
        is_followup_needed = ("FOLLOW" in appo_status_upper or "WAIT" in appo_status_upper)
        is_cancelled_or_not_interested = ("CANCEL" in appo_status_upper or "NOT INT" in appo_status_upper)

        # Automatically record interaction / call date
        today_date_iso = timezone.localdate().isoformat()
        cd['last_called_date'] = today_date_iso
        if getattr(self, 'current_user', None):
            cd['last_called_by'] = self.current_user.pk
            cd['last_called_by_name'] = self.current_user.get_full_name() or self.current_user.username

        cancel_reason_val = self.cleaned_data.get('cancellation_reason', '').strip()
        if cancel_reason_val:
            cd['cancellation_reason'] = cancel_reason_val

        # Multi-Bill & Payment History Tracker
        billing_action = self.data.get('billing_action', 'edit_last')
        billing_history = cd.get('billing_history', [])
        if not isinstance(billing_history, list):
            billing_history = []

        current_bill_item = {
            "opd_bill": str(self.cleaned_data.get('opd_bill') or '0'),
            "pharmacy_bill": str(self.cleaned_data.get('pharmacy_bill') or '0'),
            "total": str(self.cleaned_data.get('total') or (opd_val + pharm_val)),
            "uhid_id_no": self.cleaned_data.get('uhid_id_no') or '',
            "ipd_no": self.cleaned_data.get('ipd_no') or '',
            "remark": self.cleaned_data.get('remark_1') or '',
            "date": timezone.now().strftime("%d-%m-%Y %H:%M"),
        }

        if has_payment:
            if billing_action == 'add_new':
                # Append new bill as a separate record in history
                billing_history.append(current_bill_item)
            else:
                # Edit last bill: replace or set last entry in billing_history
                if billing_history:
                    billing_history[-1] = current_bill_item
                else:
                    billing_history.append(current_bill_item)

            cd['billing_history'] = billing_history

            # Calculate grand total paid across all bills in history
            total_sum = sum(float(b.get('total') or 0) for b in billing_history if isinstance(b, dict))
            cd['total_paid'] = f"{total_sum:.2f}"
            cd['total'] = f"{total_sum:.2f}"
        elif billing_history:
            total_sum = sum(float(b.get('total') or 0) for b in billing_history if isinstance(b, dict))
            cd['total_paid'] = f"{total_sum:.2f}"

        if has_payment or is_already_completed:
            from leads.models import DealStatus, AdmissionStatus, LeadStage
            instance.deal_status = DealStatus.WON
            instance.admission_status = AdmissionStatus.ADMISSION_DONE
            cd['deal_status'] = 'Won (Payment Done)'
            cd['appointment_status'] = 'Completed'

            if existing_apt and existing_apt.status != AppointmentStatus.COMPLETED:
                existing_apt.status = AppointmentStatus.COMPLETED
                existing_apt.save(update_fields=['status'])

        elif is_booking_selected or (appo_date and doc_name and not cd.get('appointment_status')):
            cd['appointment_status'] = "Awaiting Approval from Doctor"
            if appo_date and not instance.next_followup_date:
                instance.next_followup_date = appo_date

        elif is_cancelled_or_not_interested:
            from leads.models import DealStatus
            instance.deal_status = DealStatus.LOST
            cd['deal_status'] = 'Lost'
            cd['appointment_status'] = appo_status or 'Cancelled'

        elif appo_status:
            cd['appointment_status'] = appo_status

        # If Appointment Status is Follow-up Needed / Waiting and followup_date is provided
        fu_date = self.cleaned_data.get('followup_date')
        fu_time = self.cleaned_data.get('followup_time')
        if is_followup_needed or fu_date:
            if fu_date:
                instance.next_followup_date = fu_date
                instance.next_followup_time = fu_time

        # Sync Campaign foreign key if matching Campaign object exists
        camp_name = self.cleaned_data.get('campaign')
        if camp_name:
            from leads.models import Campaign
            camp_obj = Campaign.objects.filter(name__iexact=camp_name, is_active=True).first()
            if camp_obj:
                instance.campaign = camp_obj

        instance.custom_data = cd
        def _save_related():
            # 1. Create FollowUp record if Follow-up Needed / Waiting with followup date
            if fu_date:
                from followups.models import FollowUp, FollowUpStatus, FollowUpMode
                from notifications.models import Notification
                fu_obj = FollowUp.objects.create(
                    lead=instance,
                    followup_date=fu_date,
                    followup_time=fu_time,
                    followup_mode=FollowUpMode.CALL,
                    followup_status=FollowUpStatus.PENDING,
                    comment=f"Scheduled Follow-up for lead {instance.name}. Status: {appo_status or 'Follow-up Needed'}",
                    created_by=getattr(self, 'current_user', None)
                )

                # Send Notification to assigned user or creator
                target_user = instance.assigned_to or getattr(self, 'current_user', None)
                if target_user:
                    time_str = f" at {fu_time.strftime('%I:%M %p')}" if fu_time else ""
                    Notification.objects.create(
                        user=target_user,
                        title=f"Follow-up Scheduled: {instance.name}",
                        message=f"Follow-up scheduled for patient {instance.name} on {fu_date}{time_str}. Mobile: {instance.mobile}",
                        link=f"/leads/{instance.pk}/"
                    )
                
            # 2. Create Appointment record if Booked with appointment date
            appo_date = self.cleaned_data.get('appo_booked_date')
            appo_time = self.cleaned_data.get('appointment_time')
            doc_name = self.cleaned_data.get('doctor')
            
            # If appo_time is a string from hidden input
            if isinstance(appo_time, str) and appo_time.strip():
                try:
                    from datetime import datetime
                    appo_time = datetime.strptime(appo_time.strip(), "%H:%M").time()
                except Exception:
                    pass

            if "BOOKED" in appo_status or (appo_date and doc_name):
                if appo_date and doc_name:
                    import re
                    from leads.models import Appointment, AppointmentStatus
                    
                    doc_user = None
                    clean_doc_name = re.sub(r'^(dr\.?|doctor)\s+', '', doc_name, flags=re.IGNORECASE).strip()
                    user_qs = User.objects.filter(role=User.Role.DOCTOR)
                    if getattr(self, 'current_user', None) and self.current_user.hospital:
                        user_qs = user_qs.filter(hospital=self.current_user.hospital)
                        
                    for u in user_qs:
                        full_name = (u.get_full_name() or "").strip().lower()
                        u_clean = re.sub(r'^(dr\.?|doctor)\s+', '', full_name, flags=re.IGNORECASE).strip()
                        username = u.username.lower()
                        search_low = clean_doc_name.lower()
                        doc_raw_low = doc_name.lower()
                        if (search_low and (search_low in full_name or search_low in u_clean or search_low in username)) or \
                           (doc_raw_low and (doc_raw_low in full_name or doc_raw_low in username)):
                            doc_user = u
                            break
                        
                    # Update or create appointment for this lead
                    Appointment.objects.update_or_create(
                        lead=instance,
                        defaults={
                            "hospital": getattr(instance, 'hospital', None),
                            "doctor_name": doc_name,
                            "doctor_user": doc_user,
                            "appointment_date": appo_date,
                            "appointment_time": appo_time,
                            "status": AppointmentStatus.PENDING_APPROVAL,
                            "notes": self.cleaned_data.get('remark_1') or '',
                            "created_by": getattr(self, 'current_user', None)
                        }
                    )

        old_save_m2m = getattr(self, 'save_m2m', None)
        def save_m2m():
            if old_save_m2m:
                old_save_m2m()
            _save_related()

        self.save_m2m = save_m2m

        if commit:
            instance.save()
            self.save_m2m()
            
        return instance
