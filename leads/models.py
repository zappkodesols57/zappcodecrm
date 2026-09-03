import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone


# ---------------------------------------------------------------------------
# Master data (Settings module manages these — never hardcode values in views)
# ---------------------------------------------------------------------------

class SourceCategory(models.Model):
    name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "name"]
        verbose_name_plural = "Source categories"

    def __str__(self):
        return self.name


class LeadSource(models.Model):
    name = models.CharField(max_length=100)
    category = models.ForeignKey(SourceCategory, on_delete=models.PROTECT, related_name="sources")
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "name"]
        unique_together = ("name", "category")

    def __str__(self):
        return self.name


class Campaign(models.Model):
    hospital = models.ForeignKey("accounts.Hospital", on_delete=models.CASCADE, null=True, blank=True, related_name="campaigns")
    name = models.CharField(max_length=150)
    platform = models.CharField(max_length=100, blank=True)
    campaign_id = models.CharField(max_length=150, blank=True)
    ad_name = models.CharField(max_length=150, blank=True)
    ad_set = models.CharField(max_length=150, blank=True)
    landing_page = models.CharField(max_length=255, blank=True)
    cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return self.name


class Course(models.Model):
    hospital = models.ForeignKey("accounts.Hospital", on_delete=models.CASCADE, null=True, blank=True, related_name="courses")
    name = models.CharField(max_length=150)
    base_price = models.PositiveIntegerField(default=0, help_text="Base course fee in Rupees")
    max_discount = models.PositiveIntegerField(default=0, help_text="Maximum allowed discount in Rupees")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        unique_together = ("hospital", "name")

    def __str__(self):
        return self.name


class LeadStage(models.Model):
    """Configurable pipeline stage (New -> Contacted -> ... -> Admission)."""
    name = models.CharField(max_length=60, unique=True)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class Tag(models.Model):
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Universal Dynamic Masters (Parent Group & Child Sub-Master Items)
# ---------------------------------------------------------------------------

class MasterGroup(models.Model):
    """
    Parent Master Category (e.g., 'Qualifications', 'Branches', 'Loss Reasons', 'Cities').
    Allows admins to create new master categories dynamically without backend code.
    """
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True, blank=True)
    description = models.TextField(blank=True, help_text="Purpose/usage of this master category")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Master Group"
        verbose_name_plural = "Master Groups"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            from django.utils.text import slugify
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    @classmethod
    def get_active_choices(cls, slug_or_name):
        """Helper to return active sub-master choices for form dropdowns."""
        group = cls.objects.filter(models.Q(slug=slug_or_name) | models.Q(name__iexact=slug_or_name), is_active=True).first()
        if not group:
            return MasterItem.objects.none()
        return group.items.filter(is_active=True).order_by("order", "name")


# ---------------------------------------------------------------------------
# Hospital Master Configuration Models
# ---------------------------------------------------------------------------

class HospitalBranch(models.Model):
    hospital = models.ForeignKey("accounts.Hospital", on_delete=models.CASCADE, related_name="branches")
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=50, blank=True)
    city = models.CharField(max_length=100, blank=True)
    address = models.TextField(blank=True)
    contact_number = models.CharField(max_length=30, blank=True)
    is_main_branch = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "name"]
        unique_together = ("hospital", "name")
        verbose_name = "Hospital Branch"
        verbose_name_plural = "Hospital Branches"

    def __str__(self):
        return f"{self.name} ({self.city})" if self.city else self.name


class HospitalDepartment(models.Model):
    hospital = models.ForeignKey("accounts.Hospital", on_delete=models.CASCADE, related_name="hospital_departments")
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=50, blank=True)
    description = models.TextField(blank=True)
    branches = models.ManyToManyField(HospitalBranch, related_name="departments", blank=True)
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "name"]
        unique_together = ("hospital", "name")
        verbose_name = "Hospital Department"
        verbose_name_plural = "Hospital Departments"

    def __str__(self):
        return self.name


class HospitalDisease(models.Model):
    hospital = models.ForeignKey("accounts.Hospital", on_delete=models.CASCADE, related_name="diseases")
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=50, blank=True)
    department = models.ForeignKey(HospitalDepartment, on_delete=models.CASCADE, related_name="diseases")
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "name"]
        unique_together = ("hospital", "department", "name")
        verbose_name = "Disease / Condition"
        verbose_name_plural = "Diseases & Conditions"

    def __str__(self):
        return f"{self.name} ({self.department.name})"


class HospitalDoctor(models.Model):
    hospital = models.ForeignKey("accounts.Hospital", on_delete=models.CASCADE, related_name="hospital_doctors")
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="doctor_profile")
    name = models.CharField(max_length=150)
    qualification = models.CharField(max_length=150, blank=True)
    specialization = models.CharField(max_length=150, blank=True)
    contact_number = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    consultation_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.0)
    department = models.ForeignKey(HospitalDepartment, on_delete=models.SET_NULL, null=True, blank=True, related_name="primary_doctors")
    departments = models.ManyToManyField(HospitalDepartment, blank=True, related_name="doctors")
    associated_diseases = models.ManyToManyField(HospitalDisease, blank=True, related_name="doctors")
    branches = models.ManyToManyField(HospitalBranch, through="DoctorBranchAvailability", related_name="doctors", blank=True)
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "name"]
        unique_together = ("hospital", "name")
        verbose_name = "Doctor"
        verbose_name_plural = "Doctors"

    def __str__(self):
        return f"Dr. {self.name}" if not self.name.lower().startswith("dr") else self.name


class DoctorBranchAvailability(models.Model):
    doctor = models.ForeignKey(HospitalDoctor, on_delete=models.CASCADE, related_name="availabilities")
    branch = models.ForeignKey(HospitalBranch, on_delete=models.CASCADE, related_name="doctor_availabilities")
    days_of_week = models.JSONField(default=list, blank=True, help_text="e.g. ['Monday', 'Tuesday', 'Wednesday']")
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    slot_duration_minutes = models.PositiveIntegerField(default=15)
    max_patients_per_slot = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("doctor", "branch")
        verbose_name = "Doctor Branch Availability"
        verbose_name_plural = "Doctor Branch Availabilities"

    def __str__(self):
        return f"{self.doctor.name} at {self.branch.name}"


class MasterItem(models.Model):
    """
    Sub-Master Item belonging to a MasterGroup (e.g., 'B.Tech' under 'Qualifications').
    """
    group = models.ForeignKey(MasterGroup, on_delete=models.CASCADE, related_name="items")
    hospital = models.ForeignKey("accounts.Hospital", on_delete=models.CASCADE, null=True, blank=True, related_name="master_items")
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=50, blank=True, help_text="Optional short code or identifier")
    order = models.PositiveIntegerField(default=0, help_text="Sort order")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "name"]
        unique_together = ("group", "name", "hospital")
        verbose_name = "Master Item"
        verbose_name_plural = "Master Items"

class LeadCustomField(models.Model):
    """
    Allows Admin to dynamically create, edit, toggle, or delete custom form fields
    for leads without touching codebase (e.g. Policy No, Blood Group, Guardian Name, etc.).
    """
    class FieldType(models.TextChoices):
        TEXT = "TEXT", "Single Line Text"
        NUMBER = "NUMBER", "Number / Integer"
        DECIMAL = "DECIMAL", "Currency / Decimal"
        DATE = "DATE", "Date Picker"
        DROPDOWN = "DROPDOWN", "Dropdown (Select List)"
        TEXTAREA = "TEXTAREA", "Multi-line Text (Textarea)"
        CHECKBOX = "CHECKBOX", "Checkbox (Yes / No)"

    hospital = models.ForeignKey("accounts.Hospital", on_delete=models.CASCADE, null=True, blank=True, related_name="custom_form_fields")
    name = models.CharField(max_length=100, help_text="Field Identifier / Slug (e.g. guardian_name)")
    label = models.CharField(max_length=150, help_text="Label displayed on form (e.g. Guardian Name)")
    field_type = models.CharField(max_length=20, choices=FieldType.choices, default=FieldType.TEXT)
    options = models.TextField(blank=True, help_text="Comma-separated options for Dropdown type (e.g. Option 1, Option 2)")
    placeholder = models.CharField(max_length=255, blank=True)
    help_text = models.CharField(max_length=255, blank=True)
    is_required = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_system = models.BooleanField(default=False, help_text="True if this is a core standard field")
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "created_at"]
        verbose_name = "Lead Custom Field"
        verbose_name_plural = "Lead Custom Fields"

    def __str__(self):
        return f"{self.label} ({self.field_type})"

    def get_options_list(self):
        if not self.options:
            return []
        return [opt.strip() for opt in self.options.split(",") if opt.strip()]

    def get_type_code(self):
        code_map = {
            "TEXT": "T",
            "NUMBER": "N",
            "DROPDOWN": "D",
            "DATE": "Dt",
            "TEXTAREA": "Tx",
            "CHECKBOX": "Cb",
            "DECIMAL": "Dec",
        }
        return code_map.get(self.field_type, "T")

    def get_type_short_label(self):
        label_map = {
            "TEXT": "Text (T)",
            "NUMBER": "Number (N)",
            "DROPDOWN": "Dropdown (D)",
            "DATE": "Date (Dt)",
            "TEXTAREA": "Textarea (Tx)",
            "CHECKBOX": "Checkbox (Cb)",
            "DECIMAL": "Decimal (Dec)",
        }
        return label_map.get(self.field_type, self.field_type)



# ---------------------------------------------------------------------------
# Lead
# ---------------------------------------------------------------------------

class LeadTemperature(models.TextChoices):
    UNCONTACTED = "UNCONTACTED", "Uncontacted"
    NOT_PICKED = "NOT_PICKED", "Call Not Picked"
    HOT = "HOT", "Hot"
    WARM = "WARM", "Warm"
    COLD = "COLD", "Cold (Not Interested)"


class DealStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    WON = "WON", "Won"
    LOST = "LOST", "Lost"
    HOLD = "HOLD", "Hold"


class AdmissionStatus(models.TextChoices):
    NOT_APPLIED = "NOT_APPLIED", "Not Applied"
    INTERESTED = "INTERESTED", "Interested"
    APPLIED = "APPLIED", "Applied"
    ADMISSION_DONE = "ADMISSION_DONE", "Admission Done"
    CANCELLED = "CANCELLED", "Cancelled"


class ReferralType(models.TextChoices):
    STUDENT = "STUDENT", "Student Referral"
    EMPLOYEE = "EMPLOYEE", "Employee Referral"
    PARTNER = "PARTNER", "Partner Referral"
    OTHER = "OTHER", "Other"


def next_lead_code():
    year = timezone.now().year
    prefix = f"LD-{year}-"
    last = Lead.objects.filter(lead_code__startswith=prefix).order_by("-lead_code").first()
    seq = int(last.lead_code.split("-")[-1]) + 1 if last else 1
    return f"{prefix}{seq:06d}"


class Lead(models.Model):
    # Identity
    lead_code = models.CharField(max_length=20, unique=True, editable=False)

    # Basic information
    name = models.CharField(max_length=150)
    mobile = models.CharField(max_length=20, db_index=True)
    alternate_mobile = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True, db_index=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    location = models.CharField(max_length=255, blank=True)

    # Education
    education = models.CharField(max_length=150, blank=True)
    qualification = models.CharField(max_length=150, blank=True)
    graduation_year = models.PositiveIntegerField(null=True, blank=True)

    # Lead information
    course = models.ForeignKey(Course, on_delete=models.SET_NULL, null=True, blank=True, related_name="leads")
    lead_type = models.CharField(max_length=50, blank=True)
    temperature = models.CharField(max_length=20, choices=LeadTemperature.choices, default=LeadTemperature.UNCONTACTED, db_index=True)
    stage = models.ForeignKey(LeadStage, on_delete=models.PROTECT, related_name="leads")
    deal_status = models.CharField(max_length=10, choices=DealStatus.choices, default=DealStatus.OPEN, db_index=True)
    admission_status = models.CharField(max_length=20, choices=AdmissionStatus.choices, default=AdmissionStatus.NOT_APPLIED)
    inquiry_date = models.DateField(default=timezone.localdate, db_index=True)

    # CURRENT attribution (can evolve / be corrected — history kept via AuditLog)
    source_category = models.ForeignKey(SourceCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name="leads")
    lead_source = models.ForeignKey(LeadSource, on_delete=models.SET_NULL, null=True, blank=True, related_name="leads")
    campaign = models.ForeignKey(Campaign, on_delete=models.SET_NULL, null=True, blank=True, related_name="leads")
    ad_platform = models.CharField(max_length=100, blank=True)
    campaign_id_text = models.CharField(max_length=150, blank=True, help_text="Raw campaign/ad id string if no Campaign record exists")
    referral_type = models.CharField(max_length=10, choices=ReferralType.choices, blank=True)
    referral_person = models.CharField(max_length=150, blank=True)
    referral_contact = models.CharField(max_length=50, blank=True)
    referral_notes = models.TextField(blank=True)
    landing_page = models.CharField(max_length=255, blank=True)
    utm_source = models.CharField(max_length=100, blank=True)
    utm_medium = models.CharField(max_length=100, blank=True)
    utm_campaign = models.CharField(max_length=100, blank=True)
    utm_term = models.CharField(max_length=100, blank=True)
    utm_content = models.CharField(max_length=100, blank=True)

    # ORIGINAL attribution — set once at creation, never overwritten (rule #21 / #11)
    original_source_category = models.ForeignKey(SourceCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name="+", editable=False)
    original_lead_source = models.ForeignKey(LeadSource, on_delete=models.SET_NULL, null=True, blank=True, related_name="+", editable=False)
    original_campaign = models.ForeignKey(Campaign, on_delete=models.SET_NULL, null=True, blank=True, related_name="+", editable=False)
    original_utm_source = models.CharField(max_length=100, blank=True, editable=False)
    original_utm_medium = models.CharField(max_length=100, blank=True, editable=False)
    original_utm_campaign = models.CharField(max_length=100, blank=True, editable=False)
    original_referral_person = models.CharField(max_length=150, blank=True, editable=False)
    original_landing_page = models.CharField(max_length=255, blank=True, editable=False)
    external_lead_id = models.CharField(max_length=150, blank=True, db_index=True, help_text="ID from external system (API/Website/Ad platform) — used to prevent duplicate auto-created leads")
    raw_source_metadata = models.JSONField(null=True, blank=True)

    # Custom Data for Tenant specific flexible attributes (e.g., Doctor, Disease)
    custom_data = models.JSONField(default=dict, blank=True)

    # Assignment
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_leads", db_index=True)
    hospital = models.ForeignKey("accounts.Hospital", on_delete=models.CASCADE, null=True, blank=True, related_name="leads")
    assigned_manager = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="managed_leads")

    # Follow-up cache (denormalized for fast list/dashboard queries; source of truth is FollowUp model)
    next_followup_date = models.DateField(null=True, blank=True, db_index=True)
    next_followup_time = models.TimeField(null=True, blank=True)
    last_followup_date = models.DateField(null=True, blank=True)
    followup_count = models.PositiveIntegerField(default=0)

    # Additional
    notes = models.TextField(blank=True)
    tags = models.ManyToManyField(Tag, blank=True, related_name="leads")

    # Bookkeeping
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_leads")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_archived = models.BooleanField(default=False, db_index=True)

    # Migration provenance — every imported row keeps a pointer back to its Excel origin
    import_source_file = models.CharField(max_length=255, blank=True)
    import_source_sheet = models.CharField(max_length=100, blank=True)
    import_source_row = models.PositiveIntegerField(null=True, blank=True)
    import_job = models.ForeignKey("imports.ImportJob", on_delete=models.SET_NULL, null=True, blank=True, related_name="imported_leads")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["mobile"]),
            models.Index(fields=["stage", "temperature"]),
            models.Index(fields=["deal_status"]),
            models.Index(fields=["inquiry_date"]),
            models.Index(fields=["next_followup_date"]),
        ]

    def __str__(self):
        return f"{self.lead_code} — {self.name}"

    def get_custom(self, key, default=""):
        if isinstance(self.custom_data, dict):
            return self.custom_data.get(key, default)
        return default

    @property
    def effective_created_display(self):
        """
        Original Date & Time:
        - If lead came from Excel / Ads Import or has inquiry_date / historical timestamp:
          shows the original inquiry_date (+ received time if present).
        - If manually registered via Walk-in / Add Lead form:
          shows the exact creation datetime when saved.
        """
        cd = self.custom_data or {}
        time_str = cd.get('lead_received_time') or cd.get('time')
        
        # If it was imported from campaign/excel file or has historical inquiry date:
        if self.import_job_id or self.import_source_file or (self.inquiry_date and self.created_at and self.inquiry_date < self.created_at.date()):
            d_str = self.inquiry_date.strftime('%d %b %Y') if self.inquiry_date else (self.created_at.strftime('%d %b %Y') if self.created_at else '—')
            if time_str and str(time_str).strip().lower() not in ('none', 'nan', ''):
                return f"{d_str}, {time_str}"
            return d_str

        # Manually added / direct walk-in leads:
        if self.created_at:
            return self.created_at.strftime('%d %b %Y, %I:%M %p')
        if self.inquiry_date:
            return self.inquiry_date.strftime('%d %b %Y')
        return "—"

    @property
    def custom_dept(self):
        dept = self.get_custom("department") or self.get_custom("disease")
        if not dept and self.course_id and self.course:
            return self.course.name
        return dept or ""

    @property
    def custom_doctor(self):
        return self.get_custom("doctor")

    @property
    def custom_source(self):
        src = self.get_custom("lead_source") or self.get_custom("source")
        if not src and self.lead_source_id and self.lead_source:
            return self.lead_source.name
        return src or ""

    @property
    def custom_temperature(self):
        """
        Calculates the lead temperature based on assignment, calling remarks, and appointment status:
        - If appointment status is set (Booked, Booking, Follow-up, Not Interested, Cancelled, etc.): blank/None
        - If Unassigned (New/Open untouched): 'Hot'
        - If Assigned with no calling remarks: 'Hot'
        - If 1st calling remark is 'Call Not Received' / unanswered: 'Warm'
        - If 2nd calling remark is also 'Call Not Received' / unanswered: 'Cold'
        - If 3rd calling remark is also 'Call Not Received' / unanswered: 'Freeze'
        - If other valid calling remark / note: returns temperature or note classification
        """
        cd = self.custom_data or {}
        raw_apt = str(cd.get("appointment_status") or "").strip().upper()
        raw_ds = str(cd.get("deal_status") or "").strip().upper()
        tot = self.total_billed_amount

        # Terminal / Appointment Statuses -> Temperature is blank/None
        if tot > 0 or self.deal_status == DealStatus.WON or "WON" in raw_ds or "PAYMENT" in raw_ds:
            return None
        if "PAYMENT" in raw_apt or "COMPLET" in raw_apt or "VISIT" in raw_apt or "DONE" in raw_apt:
            return None
        if "BOOK" in raw_apt or "CONFIRM" in raw_apt or "APPROV" in raw_apt or "AWAIT" in raw_apt or raw_apt == "YES":
            return None
        if "CANCEL" in raw_apt or "NOT INT" in raw_apt or self.deal_status == DealStatus.LOST or "LOST" in raw_ds:
            return None
        if "FOLLOW" in raw_apt or "WAIT" in raw_apt or "RESCHEDULE" in raw_apt:
            return None

        # Check remarks
        r1 = str(cd.get("remark_1") or "").strip()
        r2 = str(cd.get("remark_2") or "").strip()
        r3 = str(cd.get("remark_3") or "").strip()

        def is_clean_val(v):
            return bool(v and v.lower() not in ("nan", "none", "—", "-", ""))

        def is_call_not_rec(v):
            if not is_clean_val(v):
                return False
            v_up = v.upper()
            return any(k in v_up for k in [
                "CALL NOT REC", "NOT REC", "CALL CUT", "RINGING", "NOT PICK",
                "BUSY", "SWITCH OFF", "NOT REACHABLE", "NO ANSWER", "DECLINE", "UNANSWERED"
            ])

        has_r1 = is_clean_val(r1)
        has_r2 = is_clean_val(r2)
        has_r3 = is_clean_val(r3)

        # Untouched / no calling remarks taken yet -> Hot
        if not has_r1 and not has_r2 and not has_r3:
            return "Hot"

        # 3rd remark is Call Not Received -> Freeze
        if is_call_not_rec(r3):
            return "Freeze"

        # 2nd remark is Call Not Received -> Cold
        if is_call_not_rec(r2):
            return "Cold"

        # 1st remark is Call Not Received -> Warm
        if is_call_not_rec(r1):
            return "Warm"

        # If user explicitly selected a temperature on lead
        temp_val = (self.temperature or "").strip()
        if temp_val in ["HOT", "WARM", "COLD", "NOT_PICKED", "UNCONTACTED"]:
            if temp_val == "HOT":
                return "Hot"
            elif temp_val == "WARM":
                return "Warm"
            elif temp_val in ["COLD", "NOT_PICKED"]:
                return "Cold"

        return "Warm"

    @property
    def custom_priority(self):
        st = self.display_status
        if st in ("Payment Done", "Booked", "Booking Confirmed", "Awaiting Approval from Doctor", "Payment Pending", "Cancelled", "Lost", "Awaiting Approval", "Follow-up Needed", "Not Interested"):
            return None

        prio = self.get_custom("priority")
        if prio and prio.lower() not in ("nan", "none", ""):
            return prio.title()

        return self.custom_temperature

    @property
    def custom_camp(self):
        camp = self.get_custom("campaign")
        if not camp and self.campaign_id and self.campaign:
            return self.campaign.name
        return camp or ""

    @property
    def total_billed_amount(self):
        cd = self.custom_data or {}
        try:
            val = float(cd.get("total_paid") or cd.get("total") or 0.0)
            return val
        except (ValueError, TypeError):
            return 0.0

    @property
    def custom_deal_status(self):
        cd = self.custom_data or {}
        tot = self.total_billed_amount
        raw_ds = str(cd.get("deal_status") or (self.stage.name if self.stage_id and self.stage else "")).strip()
        appt_st = str(cd.get("appointment_status") or "").strip()
        appt_st_up = appt_st.upper()
        attendant = cd.get("lead_attendant") or (self.assigned_to.get_full_name() if self.assigned_to else "")

        # Check appointment confirmation status from linked Appointment record
        latest_apt = self.appointments.order_by('-id').first() if self.pk else None
        has_doctor_approved = False
        if latest_apt:
            has_doctor_approved = (latest_apt.status in [AppointmentStatus.APPROVED, AppointmentStatus.COMPLETED])
        elif cd.get('appointment_confirmed_at'):
            has_doctor_approved = True

        # 1. Payment Done (total bill > 0 or deal status Won)
        if tot > 0 or self.deal_status == DealStatus.WON or raw_ds.lower() in ("won", "won (payment done)", "admission", "admission done", "payment done"):
            if tot > 0:
                return "Payment Done"
            if "COMPLET" in appt_st_up or "DONE" in appt_st_up or "VISIT" in appt_st_up:
                return "Payment Pending"
            elif "BOOK" in appt_st_up or "CONFIRM" in appt_st_up or "YES" in appt_st_up or "AWAIT" in appt_st_up:
                return "Booking Confirmed" if has_doctor_approved else "Awaiting Approval from Doctor"
            elif "CANCEL" in appt_st_up or self.deal_status == DealStatus.LOST or "LOST" in raw_ds.upper():
                return "Cancelled" if "CANCEL" in appt_st_up else "Lost"
            return "Payment Done"

        # 2. Specific Appointment Statuses set by User / Admin / Doctor
        if appt_st and appt_st.lower() not in ("nan", "none", "", "—", "-"):
            if "COMPLET" in appt_st_up or "DONE" in appt_st_up or "VISIT" in appt_st_up:
                return "Completed Appointment" if tot == 0 else "Payment Done"
            if "AWAIT" in appt_st_up or "PENDING" in appt_st_up:
                return "Booking Confirmed" if has_doctor_approved else "Awaiting Approval from Doctor"
            if "CONFIRM" in appt_st_up or "APPROV" in appt_st_up:
                return "Booking Confirmed" if has_doctor_approved else "Awaiting Approval from Doctor"
            if "BOOK" in appt_st_up or appt_st_up == "YES":
                return "Booking Confirmed" if has_doctor_approved else "Awaiting Approval from Doctor"
            if "FOLLOW" in appt_st_up or "WAIT" in appt_st_up:
                return "Follow-up Needed"
            if "NOT INT" in appt_st_up:
                return "Not Interested"
            if "CANCEL" in appt_st_up:
                return "Cancelled"
            if "LOST" in appt_st_up:
                return "Lost"
            return appt_st

        # If lead has booked date/slot or appointment record but no appointment_status string
        if (cd.get('appo_booked_date') or latest_apt):
            return "Booking Confirmed" if has_doctor_approved else "Awaiting Approval from Doctor"

        # 3. Check Cancelled / Lost in Deal Status
        if "CANCEL" in raw_ds.upper():
            return "Cancelled"
        if self.deal_status == DealStatus.LOST or "LOST" in raw_ds.upper() or "NOT INT" in raw_ds.upper():
            return "Lost"

        # 4. Check Assignment and Untouched state
        is_assigned = bool(self.assigned_to_id or (attendant and str(attendant).strip().lower() not in ("unassigned", "none", "nan", "", "-")))
        
        has_calling_notes = any(
            cd.get(k) and str(cd.get(k)).strip().lower() not in ("nan", "none", "", "—", "-")
            for k in ["remark_1", "remark_2", "remark_3"]
        )

        today = timezone.localdate()
        lead_creation_date = self.created_at.date() if self.created_at else (self.inquiry_date or today)

        # If lead has an Attendant assigned and no terminal appointment status is set:
        if is_assigned:
            if raw_ds and raw_ds.lower() not in ("open", "new", "nan", "none", "", "assigned"):
                return raw_ds
            return "Assigned"

        # If lead was added TODAY and is unassigned -> Status: "New"
        if not is_assigned and not has_calling_notes and lead_creation_date == today:
            return "New"

        # If unassigned and older -> Status: "Open"
        if not is_assigned:
            return "Open"

        if raw_ds and raw_ds.lower() not in ("open", "new", "nan", "none", ""):
            return raw_ds

        return "Open"

    @property
    def display_status(self):
        return self.custom_deal_status

    @property
    def remark_detail(self):
        """
        Dynamic remark based on lifecycle:
        - If Payment Done: Total billed amount (e.g. ₹47,226)
        - If Booked / Booking Confirmed: Appointment date & time (or Slot Scheduled)
        - If Payment Pending: 'Billing Pending'
        - If Lost / Cancelled: Reason or specific remark
        - If New (Added Today, unassigned & untouched): 'New Enquiry'
        - If Open:
            - If created_date < today and untouched/unassigned: 'Hot'
            - If attendant assigned and untouched: 'Hot'
            - Remark 1 is 'Call Not Received' / unanswered: 'Warm'
            - Remark 2 is also 'Call Not Received' / unanswered: 'Cold'
            - Remark 3 is also 'Call Not Received' / unanswered: 'Freeze'
            - Otherwise: Actual calling note or temperature
        """
        st = self.display_status
        cd = self.custom_data or {}
        tot = self.total_billed_amount

        if st == "Payment Done":
            if tot > 0:
                return f"₹{tot:,.0f}" if tot.is_integer() else f"₹{tot:,.2f}"
            return "₹0"

        if st in ("Booked", "Booking Confirmed", "Awaiting Approval from Doctor", "Booking Approval Pending"):
            appo_date = cd.get("appo_booked_date") or cd.get("appointment_date") or self.next_followup_date
            appo_time = cd.get("appointment_time")
            if appo_date and appo_time and str(appo_time).strip() not in ('-', 'None', ''):
                return f"{appo_date} ({appo_time})"
            elif appo_date:
                return f"{appo_date}"
            return "Slot Scheduled"

        if st == "Payment Pending":
            return "Billing Pending"

        if st in ("Lost", "Cancelled", "Not Interested"):
            reason = cd.get("cancellation_reason") or cd.get("remark_1") or cd.get("remark_2") or cd.get("remark_3")
            if reason and str(reason).strip().lower() not in ("nan", "none", ""):
                return str(reason).strip()[:40]
            return st

        if st == "New":
            return "Hot"

        # For Open / Assigned (and other active leads):
        r1 = str(cd.get("remark_1") or "").strip()
        r2 = str(cd.get("remark_2") or "").strip()
        r3 = str(cd.get("remark_3") or "").strip()

        def is_clean_val(v):
            return bool(v and v.lower() not in ("nan", "none", "—", "-", ""))

        def is_call_not_rec(v):
            if not is_clean_val(v):
                return False
            v_up = v.upper()
            return any(k in v_up for k in [
                "CALL NOT REC", "NOT REC", "CALL CUT", "RINGING", "NOT PICK",
                "BUSY", "SWITCH OFF", "NOT REACHABLE", "NO ANSWER", "DECLINE", "UNANSWERED"
            ])

        has_r1 = is_clean_val(r1)
        has_r2 = is_clean_val(r2)
        has_r3 = is_clean_val(r3)

        # Condition 1: No calling remark taken yet -> Hot
        if not has_r1 and not has_r2 and not has_r3:
            return "Hot"

        # Condition 2: 3rd remark is Call Not Received -> Freeze
        if is_call_not_rec(r3):
            return "Freeze"

        # Condition 3: 2nd remark is Call Not Received -> Cold
        if is_call_not_rec(r2):
            return "Cold"

        # Condition 4: 1st remark is Call Not Received -> Warm
        if is_call_not_rec(r1):
            return "Warm"

        # Fallback to latest human note or temperature
        for rk_val in [r3, r2, r1, str(cd.get("comments") or "").strip()]:
            if is_clean_val(rk_val):
                return rk_val[:40]

        return self.custom_temperature or "Warm"

    @property
    def display_next_followup_date(self):
        """
        Returns the next follow-up date only if genuinely scheduled.
        For leads with 'Payment Done', booking date should never be shown as next follow-up
        unless an explicit follow-up record with a next_followup_date was added.
        """
        st = self.display_status
        if st == "Payment Done":
            # Check if there is an explicit follow-up record with next_followup_date
            latest_fu = self.followups.filter(next_followup_date__isnull=False).order_by('-id').first()
            if latest_fu and latest_fu.next_followup_date:
                return latest_fu.next_followup_date
            return None
        return self.next_followup_date


    def save(self, *args, **kwargs):
        if not self.lead_code:
            self.lead_code = next_lead_code()
        if not self.temperature or self.temperature.strip() not in LeadTemperature.values:
            self.temperature = LeadTemperature.UNCONTACTED
        is_new = self._state.adding
        if is_new:
            # freeze original attribution at creation time — rule: never overwrite later
            self.original_source_category = self.source_category
            self.original_lead_source = self.lead_source
            self.original_campaign = self.campaign
            self.original_utm_source = self.utm_source
            self.original_utm_medium = self.utm_medium
            self.original_utm_campaign = self.utm_campaign
            self.original_referral_person = self.referral_person
            self.original_landing_page = self.landing_page
        super().save(*args, **kwargs)

    @property
    def is_booked(self):
        """Check if lead has a confirmed booked appointment or status."""
        cd = self.custom_data or {}
        st = str(self.display_status or "").strip().lower()
        if "awaiting approval" in st or "approval pending" in st:
            return False
        if "booking confirmed" in st or "completed" in st or "won" in st or "payment done" in st:
            return True
        apt_st = str(cd.get("appointment_status") or "").strip().lower()
        if "awaiting" in apt_st:
            return False
        if "confirm" in apt_st or "complete" in apt_st or "done" in apt_st:
            return True
        return False

    @property
    def whatsapp_message(self):
        """
        Dynamically returns:
        - If Booked: Customized Appointment Confirmation with Dr, Date, Time & Hospital name.
        - If Not Booked: Warm Welcoming greeting message with Hospital & Agent name.
        """
        import urllib.parse
        cd = self.custom_data or {}
        hosp = (self.hospital.name if self.hospital else "Nelson Mother & Child Care Hospital").strip()
        patient_name = (self.name or "Valued Patient").strip()
        doc = (cd.get("doctor") or "our specialist doctor").strip()
        appt_date = str(cd.get("appo_booked_date") or cd.get("appointment_date") or "").strip()
        appt_time = str(cd.get("appointment_time") or "").strip()

        if self.is_booked:
            date_part = f" for {appt_date}" if appt_date else ""
            time_part = f" at {appt_time}" if appt_time else ""
            text = (
                f"Hello {patient_name}, Greetings from {hosp}!\n\n"
                f"Your appointment with Dr. {doc} at {hosp} is confirmed{date_part}{time_part}.\n\n"
                f"Please reach 15 minutes prior to your scheduled slot. We look forward to serving you with the highest standard of care.\n\n"
                f"For any queries, feel free to reply here.\n"
                f"Warm Regards,\n{hosp}"
            )
        else:
            text = (
                f"Hello {patient_name}, Greetings from {hosp}!\n\n"
                f"Thank you for connecting with us. We are pleased to assist you with your healthcare and doctor consultation inquiry.\n\n"
                f"Please let us know your preferred date, time, or specialist requirement so we can assist you with your appointment.\n\n"
                f"Warm Regards,\nPatient Care Team - {hosp}"
            )
        return urllib.parse.quote(text)

    @property
    def clean_phone_number(self):
        """Returns 10-digit clean mobile number of the lead instance for WhatsApp links."""
        return self.normalize_mobile(self.mobile)

    @classmethod
    def clean_mobile(cls, raw=None):
        """Normalize phone string or return current instance clean phone number."""
        import re
        if raw is None:
            return ""
        digits = re.sub(r"\D", "", str(raw or ""))
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        if len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        return digits

    @staticmethod
    def normalize_mobile(raw):
        """Normalize a phone string for duplicate-detection matching and WhatsApp links."""
        import re
        digits = re.sub(r"\D", "", str(raw or ""))
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        if len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        return digits

    def find_duplicates(self):
        digits = self.normalize_mobile(self.mobile)
        qs = Lead.objects.exclude(pk=self.pk)
        if digits:
            from django.db.models.functions import Replace
            candidates = [l for l in qs.only("id", "mobile") if self.normalize_mobile(l.mobile) == digits]
            if candidates:
                return Lead.objects.filter(pk__in=[c.pk for c in candidates])
        if self.email:
            return qs.filter(email__iexact=self.email)
        return Lead.objects.none()

class NelsonLeadData(models.Model):
    lead = models.OneToOneField(Lead, on_delete=models.CASCADE, related_name='nelson_data')
    nelson_dantoli = models.CharField(max_length=150, blank=True)
    lead_received_time = models.TimeField(null=True, blank=True)
    lead_calling_time = models.TimeField(null=True, blank=True)
    gender = models.CharField(max_length=20, blank=True)
    age = models.CharField(max_length=10, blank=True)
    department = models.CharField(max_length=150, blank=True)
    doctor = models.CharField(max_length=150, blank=True)
    
    appo_book = models.CharField(max_length=50, blank=True)
    appo_booked_date = models.DateField(null=True, blank=True)
    
    calling_date_remark_1 = models.DateField(null=True, blank=True)
    remark_1 = models.TextField(blank=True)
    calling_time_remark_2 = models.TimeField(null=True, blank=True)
    calling_date_remark_2 = models.DateField(null=True, blank=True)
    remark_2 = models.TextField(blank=True)
    calling_date_remark_3 = models.DateField(null=True, blank=True)
    remark_3 = models.TextField(blank=True)
    
    done = models.CharField(max_length=100, blank=True)
    visit_date = models.DateField(null=True, blank=True)
    
    uhid_id_no = models.CharField(max_length=100, blank=True)
    pharmacy_bill = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    opd_bill = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    ipd_no = models.CharField(max_length=100, blank=True)
    investigation = models.CharField(max_length=255, blank=True)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    priority = models.CharField(max_length=50, blank=True)

    def __str__(self):
        return f"Nelson Data for {self.lead.name}"


class AppointmentStatus(models.TextChoices):
    PENDING_APPROVAL = "PENDING_APPROVAL", "Pending Approval"
    APPROVED = "APPROVED", "Approved"
    SCHEDULED = "SCHEDULED", "Scheduled"
    COMPLETED = "COMPLETED", "Completed"
    CANCELLED = "CANCELLED", "Cancelled"
    NO_SHOW = "NO_SHOW", "No-Show"

class Appointment(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="appointments")
    hospital = models.ForeignKey("accounts.Hospital", on_delete=models.CASCADE, null=True, blank=True, related_name="appointments")
    doctor_name = models.CharField(max_length=150)
    doctor_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="doctor_appointments")
    appointment_date = models.DateField(db_index=True)
    appointment_time = models.TimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=AppointmentStatus.choices, default=AppointmentStatus.PENDING_APPROVAL, db_index=True)
    doctor_notes = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ["-appointment_date", "-appointment_time"]
        
    def __str__(self):
        return f"{self.lead.name} - {self.doctor_name} ({self.appointment_date} {self.appointment_time or ''})"


class DoctorSchedule(models.Model):
    doctor = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="doctor_schedule")
    hospital = models.ForeignKey("accounts.Hospital", on_delete=models.CASCADE, null=True, blank=True, related_name="doctor_schedules")
    opd_start_time = models.TimeField(default="09:00")
    opd_end_time = models.TimeField(default="17:00")
    slot_duration_minutes = models.PositiveIntegerField(default=30)
    is_available = models.BooleanField(default=True)
    off_days = models.CharField(max_length=100, blank=True, default="Sunday", help_text="Comma-separated off days (e.g. Sunday)")

    def __str__(self):
        return f"Schedule for {self.doctor.get_full_name() or self.doctor.username}"


class DoctorLeave(models.Model):
    doctor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="doctor_leaves")
    hospital = models.ForeignKey("accounts.Hospital", on_delete=models.CASCADE, null=True, blank=True, related_name="doctor_leaves")
    start_date = models.DateField(db_index=True)
    end_date = models.DateField(db_index=True)
    is_full_day = models.BooleanField(default=True)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    reason = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-start_date"]

    def __str__(self):
        return f"{self.doctor.username} on leave: {self.start_date} to {self.end_date}"

