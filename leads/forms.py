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
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        self.current_user = user
        super().__init__(*args, **kwargs)
        
        # Load JSON fields into form
        if self.instance and self.instance.pk:
            cd = self.instance.custom_data or {}
            for field in ['gender', 'age', 'department', 'doctor', 'appointment_status', 'appo_booked_date', 'appointment_time', 'followup_date', 'followup_time', 'visit_date', 'priority', 'uhid_id_no', 'ipd_no', 'pharmacy_bill', 'opd_bill', 'investigation', 'total', 'calling_date_remark_1', 'remark_1', 'calling_date_remark_2', 'calling_time_remark_2', 'remark_2', 'calling_date_remark_3', 'remark_3', 'deal_status', 'campaign', 'lead_source', 'comments']:
                if field in cd and field in self.fields:
                    self.fields[field].initial = cd[field]
            
            # If comments not in custom_data, load from instance.notes
            if not self.fields['comments'].initial and self.instance.notes:
                self.fields['comments'].initial = self.instance.notes
            
            # If appointment_time is not in custom_data, load from Appointment relation
            if not self.fields['appointment_time'].initial:
                from leads.models import Appointment
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
            self.fields["department"].choices = [("", "-- Select Department --")] + get_tenant_items("Departments")
            
            # Combine Doctors from Master Items AND Registered Doctor Users
            master_doctors = get_tenant_items("Doctors")
            registered_doctor_qs = User.objects.filter(role=User.Role.DOCTOR, is_active=True)
            if user and user.hospital:
                registered_doctor_qs = registered_doctor_qs.filter(hospital=user.hospital)
                
            registered_doctors = []
            for doc in registered_doctor_qs:
                doc_display = doc.get_full_name().strip() or doc.username
                registered_doctors.append((doc_display, doc_display))
                
            # Merge and deduplicate choices preserving order
            all_doc_names = set()
            combined_doctors = []
            for doc_val, doc_lbl in (registered_doctors + master_doctors):
                if doc_val and doc_val.lower() not in all_doc_names:
                    all_doc_names.add(doc_val.lower())
                    combined_doctors.append((doc_val, doc_lbl))
                    
            self.fields["doctor"].choices = [("", "-- Select Doctor --")] + combined_doctors
            self.fields["gender"].choices = [("", "-- Select Gender --")] + get_tenant_items("Genders")
            self.fields["priority"].choices = [("", "-- Select Priority --")] + get_tenant_items("Priorities")
            self.fields["appointment_status"].choices = [("", "-- Select Appointment Status --")] + get_tenant_items("Appointment Statuses")
            self.fields["deal_status"].choices = [("", "-- Select Deal Status --")] + get_tenant_items("Deal Statuses")
            # Merge Campaign choices from Campaign model + MasterItem
            from leads.models import Campaign
            camp_qs = Campaign.objects.filter(is_active=True)
            if user and user.hospital:
                camp_qs = camp_qs.filter(Q(hospital=user.hospital) | Q(hospital__isnull=True))
            model_campaigns = [(c.name, c.name) for c in camp_qs]
            master_campaigns = get_tenant_items("Campaigns")
            
            all_camp_names = set()
            combined_campaigns = []
            for c_val, c_lbl in (model_campaigns + master_campaigns):
                if c_val and c_val.lower() not in all_camp_names:
                    all_camp_names.add(c_val.lower())
                    combined_campaigns.append((c_val, c_lbl))

            self.fields["campaign"].choices = [("", "-- Select Campaign --")] + combined_campaigns
            self.fields["lead_source"].choices = [("", "-- Select Lead Source --")] + get_tenant_items("Lead Sources")
        except Exception as e:
            import traceback
            traceback.print_exc()

        if "source_category" in self.fields:
            self.fields["source_category"].queryset = SourceCategory.objects.filter(is_active=True).order_by("order", "name")
            
        # Dynamically load Admin-Configured Custom Form Fields
        from leads.models import LeadCustomField
        cf_qs = LeadCustomField.objects.filter(is_active=True)
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
                opts = [("", f"-- Select {cf.label} --")] + [(opt, opt) for opt in cf.get_options_list()]
                self.fields[fname] = NonStrictChoiceField(choices=opts, required=cf.is_required, label=cf.label, initial=field_initial)
            else: # TEXT
                self.fields[fname] = forms.CharField(max_length=255, required=cf.is_required, label=cf.label, initial=field_initial)

            if cf.placeholder:
                self.fields[fname].widget.attrs["placeholder"] = cf.placeholder
            if cf.help_text:
                self.fields[fname].help_text = cf.help_text

        for name, field in self.fields.items():
            css = "form-select" if isinstance(field.widget, (forms.Select, forms.SelectMultiple)) else "form-control"
            field.widget.attrs.setdefault("class", css)

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

        if has_payment or is_already_completed:
            from leads.models import DealStatus, AdmissionStatus, LeadStage
            instance.deal_status = DealStatus.WON
            instance.admission_status = AdmissionStatus.ADMISSION_DONE
            cd['deal_status'] = 'Won (Payment Done)'
            cd['appointment_status'] = 'Completed'

            # Move to closed stage
            won_stage = LeadStage.objects.filter(name__iexact='Admission').first() or \
                        LeadStage.objects.filter(name__iexact='Payment Done').first() or \
                        LeadStage.objects.filter(name__iexact='Visited').first()
            if won_stage:
                instance.stage = won_stage

            if existing_apt and existing_apt.status != AppointmentStatus.COMPLETED:
                existing_apt.status = AppointmentStatus.COMPLETED
                existing_apt.save(update_fields=['status'])

        elif "BOOKED" in appo_status_upper or (appo_date and doc_name and not cd.get('appointment_status')):
            # Only set Awaiting Approval if it's a new booking waiting for doctor's approval
            cd['appointment_status'] = "Awaiting Approval from Doctor"
            if appo_date and not instance.next_followup_date:
                instance.next_followup_date = appo_date

        elif appo_status:
            cd['appointment_status'] = appo_status

        # If Appointment Status is WAITING and followup_date is provided
        fu_date = self.cleaned_data.get('followup_date')
        fu_time = self.cleaned_data.get('followup_time')
        if "WAITING" in appo_status_upper or fu_date:
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
            # 1. Create FollowUp record if waiting with followup date
            if fu_date:
                from followups.models import FollowUp, FollowUpStatus, FollowUpMode
                FollowUp.objects.create(
                    lead=instance,
                    followup_date=fu_date,
                    followup_time=fu_time,
                    followup_mode=FollowUpMode.CALL,
                    followup_status=FollowUpStatus.PENDING,
                    comment="Appointment in Waiting status. Follow-up scheduled.",
                    created_by=getattr(self, 'current_user', None)
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
