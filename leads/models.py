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

    @property
    def business(self):
        """Standardized business tenant object."""
        return self.hospital

    @property
    def business_id(self):
        """Standardized business ID."""
        return self.hospital_id

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return self.name


class Course(models.Model):
    hospital = models.ForeignKey("accounts.Hospital", on_delete=models.CASCADE, null=True, blank=True, related_name="courses")
    name = models.CharField(max_length=150)
    base_price = models.PositiveIntegerField(default=0, help_text="Base course fee in Rupees")
    max_discount = models.PositiveIntegerField(default=0, help_text="Maximum allowed discount in Rupees")
    tutor = models.CharField(max_length=150, blank=True, help_text="Tutor / Trainer / Instructor Name")
    batch = models.CharField(max_length=150, blank=True, help_text="Batch Name / Code (e.g. Batch A, Morning Batch, Full-Stack Python June)")
    batch_time = models.CharField(max_length=100, blank=True, help_text="Batch Timing (e.g. 10:00 AM - 12:00 PM)")
    description = models.TextField(blank=True, help_text="Course overview or syllabus summary")
    is_active = models.BooleanField(default=True)
    is_archived = models.BooleanField(default=False, db_index=True)

    @property
    def business(self):
        """Standardized business tenant object."""
        return self.hospital

    @business.setter
    def business(self, value):
        self.hospital = value

    @property
    def business_id(self):
        """Standardized business ID."""
        return self.hospital_id

    @business_id.setter
    def business_id(self, value):
        self.hospital_id = value

    class Meta:
        ordering = ["name"]
        unique_together = ("hospital", "name")

    def __str__(self):
        return self.name


class Trainer(models.Model):
    """
    Dedicated Trainer / Instructor master entity for Academy businesses.
    """
    hospital = models.ForeignKey("accounts.Hospital", on_delete=models.CASCADE, null=True, blank=True, related_name="trainers")
    name = models.CharField(max_length=150)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    specialization = models.CharField(max_length=200, blank=True, help_text="e.g. Python Full Stack, Data Analytics, React")
    user = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="trainer_profile", help_text="Linked CRM User if trainer has a login")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def business(self):
        return self.hospital

    @business.setter
    def business(self, value):
        self.hospital = value

    @property
    def business_id(self):
        return self.hospital_id

    @business_id.setter
    def business_id(self, value):
        self.hospital_id = value

    class Meta:
        ordering = ["name"]
        unique_together = ("hospital", "name")

    def __str__(self):
        return self.name


class Batch(models.Model):
    """
    Dedicated Batch master entity for Academy businesses.
    Directly connects Course, Trainer, Start Time, and End Time.
    """
    hospital = models.ForeignKey("accounts.Hospital", on_delete=models.CASCADE, null=True, blank=True, related_name="batches")
    name = models.CharField(max_length=150, help_text="Batch Name (e.g. Morning Python Batch 01)")
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="batches")
    trainer = models.ForeignKey(Trainer, on_delete=models.SET_NULL, null=True, blank=True, related_name="batches")
    start_time = models.TimeField(null=True, blank=True, help_text="Batch start time (e.g. 10:00 AM)")
    end_time = models.TimeField(null=True, blank=True, help_text="Batch end time (e.g. 12:00 PM)")
    start_date = models.DateField(null=True, blank=True, help_text="Batch starting date")
    max_strength = models.PositiveIntegerField(default=30, blank=True, null=True, help_text="Max student capacity")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def timing_display(self):
        if self.start_time and self.end_time:
            return f"{self.start_time.strftime('%I:%M %p')} - {self.end_time.strftime('%I:%M %p')}"
        elif self.start_time:
            return f"From {self.start_time.strftime('%I:%M %p')}"
        return "Timing TBD"

    @property
    def business(self):
        return self.hospital

    @business.setter
    def business(self, value):
        self.hospital = value

    @property
    def business_id(self):
        return self.hospital_id

    @business_id.setter
    def business_id(self, value):
        self.hospital_id = value

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        trainer_str = f" ({self.trainer.name})" if self.trainer else ""
        return f"{self.name} - {self.course.name}{trainer_str}"


class LeadStage(models.Model):
    """Configurable pipeline stage (New -> Contacted -> ... -> Admission)."""
    class BusinessType(models.TextChoices):
        ALL = "ALL", "All / Shared"
        HOSPITAL = "HOSPITAL", "Hospital Only"
        ACADEMY = "ACADEMY", "Academy Only"

    hospital = models.ForeignKey("accounts.Hospital", on_delete=models.CASCADE, null=True, blank=True, related_name="stages", help_text="Specific hospital/business tenant. Null/None means global or Zappcode Academy.")
    name = models.CharField(max_length=60)
    business_type = models.CharField(max_length=20, choices=BusinessType.choices, default=BusinessType.ALL, db_index=True)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["order", "name"]
        unique_together = ("hospital", "name", "business_type")

    @property
    def business(self):
        """Standardized business tenant object."""
        return self.hospital

    @business.setter
    def business(self, value):
        self.hospital = value

    @property
    def business_id(self):
        """Standardized business ID."""
        return self.hospital_id

    @business_id.setter
    def business_id(self, value):
        self.hospital_id = value

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

    @property
    def business(self):
        return self.hospital

    @property
    def business_id(self):
        return self.hospital_id

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

    @property
    def business(self):
        return self.hospital

    @property
    def business_id(self):
        return self.hospital_id

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

    @property
    def business(self):
        return self.hospital

    @property
    def business_id(self):
        return self.hospital_id

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

    @property
    def business(self):
        return self.hospital

    @property
    def business_id(self):
        return self.hospital_id

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

    @property
    def business(self):
        return self.hospital

    @property
    def business_id(self):
        return self.hospital_id

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

    @property
    def business(self):
        return self.hospital

    @property
    def business_id(self):
        return self.hospital_id

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
    HOT = "HOT", "Hot"
    WARM = "WARM", "Warm"
    COLD = "COLD", "Cold"
    FREEZE = "FREEZE", "Freeze"


class DealStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    CONTACTED = "CONTACTED", "Contacted"
    INTERESTED = "INTERESTED", "Interested"
    WON = "WON", "Won"
    LOST = "LOST", "Lost"


class AdmissionStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    HOLD = "HOLD", "Hold"
    WON = "WON", "Won"
    LOST = "LOST", "Lost"


class ReferralType(models.TextChoices):
    STUDENT = "STUDENT", "Student Referral"
    EMPLOYEE = "EMPLOYEE", "Employee Referral"
    PARTNER = "PARTNER", "Partner Referral"
    OTHER = "OTHER", "Other"


def get_business_lead_prefix(hospital=None):
    """
    Generates generic 2-4 uppercase letter business prefix for lead codes.
    If hospital/business has code (e.g. BIZ-HOSP-001 -> 'HP-', or initials 'NL-') or defaults to 'LD-'.
    """
    if not hospital:
        return "LD-"
    
    # If hospital has explicit custom business code prefix
    b_code = getattr(hospital, "business_code", "") or ""
    if b_code:
        # e.g., 'BIZ-ACAD-001' -> 'AC-', 'BIZ-HOSP-001' -> 'HP-'
        parts = b_code.split("-")
        if len(parts) >= 2 and parts[1]:
            return f"{parts[1][:3].upper()}-"

    # Derive from business name initials (e.g., 'Nelson Mother & Child Care Hospital' -> 'NL-')
    name = (getattr(hospital, "name", "") or "").strip()
    if name:
        words = [w for w in name.replace("&", " ").replace("-", " ").split() if w]
        if len(words) >= 2:
            initials = f"{words[0][0]}{words[1][0]}".upper()
            return f"{initials}-"
        elif len(words) == 1:
            return f"{words[0][:2].upper()}-"

    return "LD-"


def next_lead_code(hospital=None):
    year = timezone.now().year
    prefix = get_business_lead_prefix(hospital)
    full_prefix = f"{prefix}{year}-"
    
    # Efficiently extract the highest sequence using reverse order limit 1
    latest_code = (
        Lead.objects.filter(lead_code__startswith=full_prefix)
        .order_by("-lead_code")
        .values_list("lead_code", flat=True)
        .first()
    )
    max_seq = 0
    if latest_code and "-" in latest_code:
        parts = latest_code.split("-")
        if len(parts) >= 3 and parts[-1].isdigit():
            try:
                max_seq = int(parts[-1])
            except ValueError:
                max_seq = 0

    seq = max_seq + 1
    new_code = f"{full_prefix}{seq:06d}"
    # Guard against gaps or collisions
    while Lead.objects.filter(lead_code=new_code).exists():
        seq += 1
        new_code = f"{full_prefix}{seq:06d}"
    return new_code


class SafeCustomDict(dict):
    """
    A dictionary subclass that returns empty string '' or default value for missing keys
    and allows attribute-style access. Prevents Django template VariableDoesNotExist exceptions.
    """
    def __getitem__(self, key):
        if key in self:
            return super().__getitem__(key)
        return ""

    def __getattr__(self, key):
        if key.startswith("_") or key in (
            "resolve_expression",
            "prepare_database_save",
            "as_sql",
            "target",
            "get_source_expressions",
            "set_source_expressions",
        ):
            raise AttributeError(f"'SafeCustomDict' object has no attribute '{key}'")
        if key in self:
            return self[key]
        raise AttributeError(f"'SafeCustomDict' object has no attribute '{key}'")


def wrap_safe_custom_data(data):
    if isinstance(data, dict) and not isinstance(data, SafeCustomDict):
        return SafeCustomDict(data)
    return data if data is not None else SafeCustomDict()


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
    temperature = models.CharField(max_length=20, choices=LeadTemperature.choices, default=LeadTemperature.HOT, db_index=True)
    stage = models.ForeignKey(LeadStage, on_delete=models.PROTECT, related_name="leads")
    deal_status = models.CharField(max_length=20, choices=DealStatus.choices, default=DealStatus.OPEN, db_index=True)
    admission_status = models.CharField(max_length=20, choices=AdmissionStatus.choices, default=AdmissionStatus.OPEN)
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
    parent_lead = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="clones", help_text="Original lead if this is a re-engagement clone")

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
            models.Index(fields=["hospital", "-created_at"]),
            models.Index(fields=["hospital", "assigned_to"]),
            models.Index(fields=["hospital", "stage"]),
            models.Index(fields=["mobile"]),
            models.Index(fields=["stage", "temperature"]),
            models.Index(fields=["deal_status"]),
            models.Index(fields=["inquiry_date"]),
            models.Index(fields=["next_followup_date"]),
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if isinstance(self.custom_data, dict) and not isinstance(self.custom_data, SafeCustomDict):
            self.custom_data = SafeCustomDict(self.custom_data)

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        if isinstance(instance.custom_data, dict) and not isinstance(instance.custom_data, SafeCustomDict):
            instance.custom_data = SafeCustomDict(instance.custom_data)
        return instance

    def __str__(self):
        return f"{self.lead_code} — {self.name}"

    def get_custom(self, key, default=""):
        """Safely fetch values from custom_data dictionary."""
        if not self.custom_data or not isinstance(self.custom_data, dict):
            return default
        val = self.custom_data.get(key)
        return val if val is not None else default


    @property
    def business(self):
        """Standardized business tenant object."""
        return self.hospital

    @business.setter
    def business(self, value):
        self.hospital = value

    @property
    def business_id(self):
        """Standardized business ID."""
        return self.hospital_id

    @business_id.setter
    def business_id(self, value):
        self.hospital_id = value

    @property
    def effective_created_date(self):
        """Returns the actual logical date when lead originated (inquiry_date if present, else created_at date)."""
        if self.inquiry_date:
            return self.inquiry_date
        if self.created_at:
            return timezone.localdate(self.created_at)
        return None

    @property
    def effective_created_formatted(self):
        """Returns standardized YYYY-MM-DD HH:MM or YYYY-MM-DD string of creation/receive time."""
        cd = self.custom_data or {}
        time_str = cd.get('lead_received_time') or cd.get('time')
        if self.import_job_id or self.import_source_file or (self.inquiry_date and self.created_at and self.inquiry_date < self.created_at.date()):
            d_str = str(self.inquiry_date) if self.inquiry_date else (self.created_at.strftime('%Y-%m-%d') if self.created_at else '')
            if time_str and str(time_str).strip().lower() not in ('none', 'nan', ''):
                return f"{d_str} {time_str}"
            return d_str
        if self.created_at:
            return timezone.localtime(self.created_at).strftime('%Y-%m-%d %H:%M')
        if self.inquiry_date:
            return str(self.inquiry_date)
        return ""

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
            return timezone.localtime(self.created_at).strftime('%d %b %Y, %I:%M %p')
        if self.inquiry_date:
            return self.inquiry_date.strftime('%d %b %Y')
        return "—"

    @property
    def custom_dept(self):
        dept = self.get_custom("department") or self.get_custom("disease") or self.get_custom("dept")
        if not dept and self.course_id and self.course:
            return self.course.name
        if not dept and hasattr(self, '_prefetched_objects_cache') and 'appointments' in self._prefetched_objects_cache:
            appts = self.appointments.all()
            if appts:
                # If appointment has notes or department hints or doctor
                pass
        return dept or ""

    @property
    def custom_branch(self):
        branch = self.get_custom("hospital_branch") or self.get_custom("branch") or self.get_custom("dyn_hospital_branch") or self.get_custom("dyn_branch")
        if not branch and self.hospital_id and self.hospital:
            return self.hospital.name
        return branch or ""

    @property
    def custom_disease(self):
        return self.get_custom("disease") or self.get_custom("dyn_disease") or ""

    @property
    def custom_doctor(self):
        doc = self.get_custom("doctor") or self.get_custom("appointed_doctor") or self.get_custom("doctor_name")
        if not doc and hasattr(self, '_prefetched_objects_cache') and 'appointments' in self._prefetched_objects_cache:
            appts = self.appointments.all()
            if appts and appts[0].doctor_name:
                return appts[0].doctor_name
        elif not doc and self.pk:
            first_appt = self.appointments.first()
            if first_appt and first_appt.doctor_name:
                return first_appt.doctor_name
        if not doc and self.assigned_to and getattr(self.assigned_to, 'role', '') == 'DOCTOR':
            return self.assigned_to.get_full_name() or self.assigned_to.username
        return doc or ""

    @property
    def custom_doctor_remark(self):
        return (
            self.get_custom("doctor_remark")
            or self.get_custom("doctor_rejection_reason")
            or self.get_custom("reject_reason")
            or self.get_custom("doctor_cancel_reason")
            or self.get_custom("last_doctor_remark")
            or self.get_custom("doctor_reschedule_remark")
            or ""
        )
    @property
    def is_hospital_industry(self):
        """Returns True ONLY if the business industry assigned by SuperAdmin is HOSPITAL."""
        if not self.hospital_id or not self.hospital:
            return False
        return getattr(self.hospital, 'industry', None) == 'HOSPITAL'

    @property
    def is_academy_industry(self):
        """Returns True ONLY if the business industry assigned by SuperAdmin is ACADEMY."""
        if not self.hospital_id or not self.hospital:
            return False
        return getattr(self.hospital, 'industry', None) == 'ACADEMY'

    @property
    def custom_reschedule_remark(self):
        return self.get_custom("doctor_reschedule_remark") or self.get_custom("reschedule_remark") or ""

    @property
    def custom_source(self):
        src = self.get_custom("lead_source") or self.get_custom("source")
        if not src and self.lead_source_id and self.lead_source:
            return self.lead_source.name
        if not src and self.raw_source_metadata and isinstance(self.raw_source_metadata, dict):
            for k, v in self.raw_source_metadata.items():
                if "source" in k.lower() and v and str(v).strip().lower() not in ("none", "nan", "", "-", "null"):
                    return str(v).strip()
        if src and str(src).strip().lower() not in ('none', 'nan', '', '-', '—', 'null'):
            return str(src).strip()
        return "Direct"

    @property
    def custom_campaign(self):
        camp = self.get_custom("campaign") or self.get_custom("campaign_name")
        if not camp and self.campaign_id and self.campaign:
            return self.campaign.name
        if not camp and self.raw_source_metadata and isinstance(self.raw_source_metadata, dict):
            for k, v in self.raw_source_metadata.items():
                if "campaign" in k.lower() and v and str(v).strip().lower() not in ("none", "nan", "", "-", "null"):
                    return str(v).strip()
        if camp and str(camp).strip().lower() not in ('none', 'nan', '', '-', '—', 'null'):
            return str(camp).strip()
        return ""

    @property
    def display_stage(self):
        """
        Dynamically calculates pipeline Stage:
        - For Hospital: dynamic clinical pipeline (Awaiting Doctor Approval, Booking Confirmed, Completed, Payment Done, Follow up, New, Lost, etc.)
        - For Academy and other industries: uses the configured stage name, or 'New'.
        """
        st_name = (self.stage.name if self.stage_id and self.stage else "").strip()
        if not self.is_hospital_industry:
            return st_name or "New"

        cd = self.custom_data or {}
        tot = self.total_billed_amount
        st_up = st_name.upper()
        adm_st = str(self.admission_status or "").strip().upper()
        appt_st = str(cd.get("appointment_status") or "").strip().upper()
        raw_ds = str(cd.get("deal_status") or "").strip().upper()

        # Doctor Cancelled / Rejected appointment awaiting follow-up -> Stage is 'Appointment Cancelled'
        if "DOCTOR CANCELLED" in appt_st or "DOCTOR REJECT" in appt_st or st_name == "appointment cancelled" or "APPOINTMENT CANCELLED" in st_up:
            return "Appointment Cancelled"

        # Check Lost / Cancelled:
        # If custom_temperature is Freeze (e.g. 3 negative remarks) or explicit lost/cancelled
        temp_val = self.custom_temperature
        if temp_val == "Freeze":
            return "Lost"

        if adm_st in ("LOST", "CANCELLED") or (("CANCEL" in st_up or "LOST" in st_up or "NOT INT" in st_up) and st_name != "appointment cancelled") or "LOST" in appt_st or ("CANCEL" in appt_st and "DOCTOR" not in appt_st):
            return "Cancelled" if ("CANCEL" in st_up or "CANCEL" in appt_st or adm_st == "CANCELLED") else "Lost"

        if tot > 0 or "PAYMENT" in raw_ds or "PAYMENT" in appt_st or "PAYMENT" in st_up or (st_up == "PAYMENT" and cd.get("payment_status") == "Done"):
            return "Payment Done"

        if "CONFIRM" in st_up or "CONFIRM" in appt_st or "BOOKING CONFIRMED" in appt_st or "APPROVED" in appt_st:
            return "Appointment Confirmed"

        if "COMPLET" in appt_st or "CONSULTATION COMPLETED" in appt_st or "COMPLET" in st_up or "WON" in raw_ds or adm_st == "WON":
            return "Appointment Completed"

        if "AWAIT" in appt_st or "APPROVAL" in appt_st or "PENDING_APPROVAL" in appt_st or "AWAIT" in st_up:
            return "Awaiting Approval from Doctor"

        has_active_pending_fu = bool(self.next_followup_date)
        if self.pk and hasattr(self, '_prefetched_objects_cache') and 'followups' in self._prefetched_objects_cache:
            has_active_pending_fu = bool(self.followups.all())
        elif self.pk:
            from followups.models import FollowUp
            has_active_pending_fu = FollowUp.objects.filter(lead=self).exists()

        def is_clean_val(v):
            return bool(v and str(v).strip().lower() not in ("nan", "none", "—", "-", "", "null", "nil"))

        has_remarks = any(is_clean_val(cd.get(k)) for k in ["remark_1", "remark_2", "remark_3", "followup_remark", "comments"])

        if has_active_pending_fu or has_remarks or "FOLLOW" in st_up or "FOLLOW" in appt_st:
            return "Follow up"

        return "New"

    @property
    def custom_temperature(self):
        """
        Calculates lead temperature:
        - For Academy: Dynamic temperature based on Academy lifecycle:
            * Lost/Cancelled -> Freeze
            * Payment Done / Admission Done -> Hot
            * Visit Planned / Visited / Admission Pending -> Min Warm (or Hot)
            * Negative remark -> steps down by 1 level (HOT -> WARM -> COLD -> FREEZE)
            * Positive remark -> stays HOT or steps up
        - For Hospital: Dynamic recalculation based on doctor appointments and clinical interaction remarks.
        - For Other / Generic: Returns stored temperature display.
        """
        if self.is_academy_industry:
            st_name = (self.stage.name if self.stage_id and self.stage else "").strip().lower()
            adm_st = str(self.admission_status or "").strip().upper()
            cd = self.custom_data or {}
            cd_ds = str(cd.get("deal_status") or "").strip().upper()

            # 1. Lost / Cancelled -> FREEZE
            if (
                adm_st in ("LOST", "CANCELLED", "DROPOUT")
                or st_name in ("cancelled", "lost", "not interested", "rejected", "closed lost", "dropped", "dropout")
                or self.deal_status == DealStatus.LOST
                or cd_ds in ("LOST", "CANCELLED")
            ):
                return "Freeze"

            # 2. Payment Done or Admission Done -> HOT
            if (
                adm_st in ("WON", "ADMISSION_DONE")
                or self.total_billed_amount > 0
                or st_name in ("payment done", "closed won", "admission done")
                or self.deal_status == DealStatus.WON
                or cd_ds in ("WON", "WON (PAYMENT DONE)")
            ):
                return "Hot"

            # Count positive and negative remarks/interactions
            pos_kw = [
                "INTERESTED", "INTRESTED", "POSITIVE", "WILL VISIT", "READY TO JOIN",
                "VISIT PLANNED", "VISITED", "ADMISSION", "ENROLLED", "GOOD RESPONSE",
                "CALLBACK", "CALL BACK", "WANTS ADMISSION", "FEES AGREED"
            ]
            neg_kw = [
                "NOT INTERESTED", "NOT INTRESTED", "NO INTEREST", "NO INTREST",
                "REJECTED", "WRONG NUMBER", "INVALID NUMBER", "CALL DISCONNECTED",
                "BUSY", "SWITCH OFF", "NOT REACHABLE", "NO ANSWER", "DECLINE",
                "CALL NOT REC", "NOT REC", "CALL CUT", "DNP", "DONT WANT", "DON'T WANT",
                "FEE HIGH", "TOO EXPENSIVE", "NOT REQUIRED"
            ]

            def is_clean_val(v):
                return bool(v and str(v).strip().lower() not in ("nan", "none", "—", "-", "", "null", "nil"))

            interactions = []
            for r in [cd.get("remark_1"), cd.get("remark_2"), cd.get("remark_3"), cd.get("followup_remark"), cd.get("comments")]:
                if is_clean_val(r):
                    interactions.append(str(r).strip())

            if self.pk:
                try:
                    for n in self.lead_notes.all():
                        if is_clean_val(n.note):
                            interactions.append(str(n.note).strip())
                except Exception:
                    pass

                try:
                    for fu in self.followups.all():
                        if is_clean_val(fu.comment):
                            interactions.append(str(fu.comment).strip())
                        if fu.followup_status in ["DNP", "NOT_INTERESTED", "NOT_CONNECTED", "CANCELLED"]:
                            interactions.append(fu.get_followup_status_display())
                        elif fu.followup_status in ["INTERESTED", "COMPLETED", "DONE"]:
                            interactions.append(fu.get_followup_status_display())
                except Exception:
                    pass

            # Step-down scale: 3=HOT, 2=WARM, 1=COLD, 0=FREEZE
            # Fresh leads start at HOT (3)
            current_level = 3
            for text in interactions:
                t_up = str(text).upper()
                is_pos = any(k in t_up for k in pos_kw)
                is_neg = any(k in t_up for k in neg_kw)
                if is_neg:
                    current_level = max(0, current_level - 1)
                elif is_pos:
                    current_level = min(3, current_level + 1)

            # 3. Visit Planned / Visited / Admission Pending -> Min Warm (level 2)
            if st_name in ("visit planned", "visited", "admission pending", "payment pending"):
                current_level = max(2, current_level)

            levels = ["Freeze", "Cold", "Warm", "Hot"]
            return levels[current_level]

        if not self.is_hospital_industry:
            return self.get_temperature_display() if hasattr(self, 'get_temperature_display') else (self.temperature or "Warm")

        st_name = (self.stage.name if self.stage_id and self.stage else "").strip().lower()
        adm_st = str(self.admission_status or "").strip().upper()
        cd = self.custom_data or {}
        appt_st = str(cd.get("appointment_status") or "").strip().upper()
        raw_ds = str(cd.get("deal_status") or "").strip().upper()
        tot = self.total_billed_amount

        is_doctor_cancelled = "DOCTOR CANCELLED" in appt_st or "DOCTOR REJECT" in appt_st or st_name == "appointment cancelled"

        # 1. Lost / Cancelled leads are ALWAYS Freeze (unless it's a doctor-cancelled lead pending re-followup)
        if not is_doctor_cancelled and (
            st_name in ("cancelled", "lost", "not interested") 
            or adm_st in ("LOST", "CANCELLED") 
            or self.deal_status == DealStatus.LOST
            or "CANCEL" in appt_st
            or "LOST" in appt_st
            or "NOT INT" in appt_st
        ):
            return "Freeze"

        # 2. Beyond early stages (Confirmed Booking, Completed Consultation, Payment Done) -> Hide temperature (returns None)
        if (
            tot > 0 
            or "PAYMENT" in raw_ds 
            or "PAYMENT" in appt_st 
            or "PAYMENT" in st_name.upper()
            or "COMPLET" in appt_st 
            or "CONFIRM" in appt_st
            or "APPROVED" in appt_st
            or adm_st == "WON"
            or self.deal_status == DealStatus.WON
        ):
            return None

        # 3. Early stages: New, Assigned, Follow-up, Doctor Approval Pending, Appointment Cancelled
        TEMP_LEVELS = ["Freeze", "Cold", "Warm", "Hot"]

        # Base starting temperature:
        if is_doctor_cancelled:
            # If explicit temperature was assigned (stepped down), use it as base
            if self.temperature == LeadTemperature.FREEZE:
                base_level = 0
            elif self.temperature == LeadTemperature.COLD:
                base_level = 1
            elif self.temperature == LeadTemperature.WARM:
                base_level = 2
            else:
                base_level = 2  # default Warm
        elif adm_st == "HOLD" or str(self.deal_status).upper() == "HOLD":
            base_level = 1  # Cold
        elif adm_st == "OPEN":
            base_level = 2  # Warm
        else:
            base_level = 3  # Hot

        pos_keywords = [
            "INTERESTED", "INTRESTED", "INTREST", "CALLBACK", "POSITIVE", "WILL VISIT", "ASKED FOR DETAILS",
            "READY TO BOOK", "OPD VISIT", "ADMISSION PLANNED", "GOOD RESPONSE", "APPOINTMENT SCHEDULED",
            "VISIT PLANNED", "VISITED", "ADMISSION DONE", "PAYMENT DONE", "READY TO JOIN", "JOINING"
        ]
        neg_keywords = [
            "CALL NOT REC", "NOT REC", "CALL CUT", "RINGING", "NOT PICK",
            "BUSY", "SWITCH OFF", "NOT REACHABLE", "NO ANSWER", "DECLINE", "UNANSWERED",
            "WRONG NUMBER", "INVALID NUMBER", "OUT OF SERVICE", "NOT ANSWERING", "DNP",
            "NOT INTERESTED", "NOT INTRESTED", "NOT INTREST", "NO INTREST", "NO INTEREST",
            "NOT REQUIRED", "NO REQUIREMENT", "DON'T WANT", "DONT WANT",
            "NO RESPONSE", "CALL BACK LATER", "CALL DISCONNECTED", "REJECTED"
        ]

        def is_clean_val(v):
            return bool(v and str(v).strip().lower() not in ("nan", "none", "—", "-", "", "null", "nil"))

        def matches_any(v, keywords):
            if not is_clean_val(v):
                return False
            v_up = str(v).upper()
            return any(k.upper() in v_up for k in keywords if k)

        # Collect all interactions chronologically
        interactions = []
        for r in [cd.get("remark_1"), cd.get("remark_2"), cd.get("remark_3"), cd.get("followup_remark"), cd.get("comments")]:
            if is_clean_val(r):
                interactions.append(str(r).strip())

        if self.pk:
            if hasattr(self, '_prefetched_objects_cache') and 'lead_notes' in self._prefetched_objects_cache:
                for n in self.lead_notes.all():
                    if is_clean_val(n.note):
                        interactions.append(str(n.note).strip())
            else:
                try:
                    for n in self.lead_notes.all():
                        if is_clean_val(n.note):
                            interactions.append(str(n.note).strip())
                except Exception:
                    pass

            if hasattr(self, '_prefetched_objects_cache') and 'followups' in self._prefetched_objects_cache:
                for fu in self.followups.all():
                    if is_clean_val(fu.comment):
                        interactions.append(str(fu.comment).strip())
                    if fu.followup_status in ["DNP", "NOT_INTERESTED", "NOT_CONNECTED", "CANCELLED"]:
                        interactions.append(fu.get_followup_status_display())
                    elif fu.followup_status in ["INTERESTED", "COMPLETED", "DONE"]:
                        interactions.append(fu.get_followup_status_display())
            else:
                try:
                    for fu in self.followups.all():
                        if is_clean_val(fu.comment):
                            interactions.append(str(fu.comment).strip())
                        if fu.followup_status in ["DNP", "NOT_INTERESTED", "NOT_CONNECTED", "CANCELLED"]:
                            interactions.append(fu.get_followup_status_display())
                        elif fu.followup_status in ["INTERESTED", "COMPLETED", "DONE"]:
                            interactions.append(fu.get_followup_status_display())
                except Exception:
                    pass

        # Start from base_level according to status
        current_level = base_level
        min_level = 0

        # Check explicit 3 remarks first
        r1, r2, r3 = cd.get("remark_1"), cd.get("remark_2"), cd.get("remark_3")
        if is_clean_val(r1) and is_clean_val(r2) and is_clean_val(r3):
            if matches_any(r1, neg_keywords) and matches_any(r2, neg_keywords) and matches_any(r3, neg_keywords):
                return "Freeze"

        neg_count = 0
        pos_count = 0
        for text in interactions:
            is_pos = matches_any(text, pos_keywords)
            is_neg = matches_any(text, neg_keywords)

            if is_pos and not is_neg:
                pos_count += 1
            elif is_neg and not is_pos:
                neg_count += 1
            elif is_pos and is_neg:
                if "NOT" in str(text).upper() or "NO " in str(text).upper():
                    neg_count += 1
                else:
                    pos_count += 1

        # User Rule for Hospital: Freeze ONLY if 3 negative remarks exist and no positive interactions
        if neg_count >= 3 and pos_count == 0:
            return "Freeze"

        # If 1 or 2 negative remarks exist, keep actionable: Cold
        if neg_count > 0 and pos_count == 0:
            return "Cold"

        if pos_count > neg_count:
            return "Hot"

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
        if camp and str(camp).strip().lower() not in ('none', 'nan', '', '-', '—', 'null'):
            return str(camp).strip()
        return "nan"

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
        """
        Calculates Lead / Deal Status:
        - For Hospital: returns Hospital clinical pipeline flow ('New', 'Open', 'Pending', 'Won', 'Lost', 'Completed').
        - For Academy: returns Academy lifecycle deal status ('Open', 'Contacted', 'Interested', 'Won', 'Lost').
        - For Other / Generic: returns admission_status or deal_status.
        """
        if self.is_academy_industry:
            if self.deal_status:
                return self.get_deal_status_display() or self.deal_status
            if self.admission_status:
                return self.get_admission_status_display() or self.admission_status
            return "Open"

        if not self.is_hospital_industry:
            if self.admission_status:
                return self.get_admission_status_display() or self.admission_status
            if self.deal_status:
                return self.get_deal_status_display() or self.deal_status
            return "Open"

        cd = self.custom_data or {}
        tot = self.total_billed_amount
        st_name = (self.stage.name if self.stage_id and self.stage else "").strip().lower()
        adm_st = str(self.admission_status or "").strip().upper()
        appt_st = str(cd.get("appointment_status") or "").strip()
        appt_st_up = appt_st.upper()
        raw_ds = str(cd.get("deal_status") or "").strip().upper()
        temp = (self.custom_temperature or self.temperature or "").strip().upper()
        attendant = cd.get("lead_attendant") or (self.assigned_to.get_full_name() if self.assigned_to else "")

        is_doctor_cancelled = "DOCTOR CANCELLED" in appt_st_up or "DOCTOR REJECT" in appt_st_up or st_name == "appointment cancelled"

        # 1. Check LOST (Temperature Freeze, Cancelled stage/status, or DealStatus LOST)
        if not is_doctor_cancelled and (
            temp == "FREEZE"
            or self.temperature == LeadTemperature.FREEZE
            or self.deal_status == DealStatus.LOST
            or adm_st in ("LOST", "CANCELLED")
            or "CANCEL" in st_name
            or "LOST" in st_name
            or "NOT INT" in st_name
            or "CANCEL" in appt_st_up
            or "LOST" in appt_st_up
            or "NOT INT" in appt_st_up
            or "CANCEL" in raw_ds
            or "LOST" in raw_ds
        ):
            return "Lost"

        # 2. Check WON (Payment Done, Total Billed > 0, Won deal status, or Stage is Payment Done)
        has_payment_done = (
            tot > 0
            or "PAYMENT DONE" in raw_ds
            or "PAYMENT DONE" in appt_st_up
            or "PAYMENT DONE" in st_name.upper()
            or st_name == "payment done"
            or (st_name == "payment" and cd.get("payment_status") == "Done")
            or self.deal_status == DealStatus.WON
            or adm_st in ("WON", "ADMISSION_DONE")
        )
        if has_payment_done:
            return "Won"

        # Doctor Cancelled leads awaiting follow-up are strictly PENDING
        if is_doctor_cancelled:
            return "Pending"

        # Check if there is an active future or scheduled appointment / follow-up
        has_active_pending_fu = bool(self.next_followup_date)
        if self.pk and hasattr(self, '_prefetched_objects_cache') and 'followups' in self._prefetched_objects_cache:
            has_active_pending_fu = any(fu.followup_status in ('PENDING', 'RESCHEDULED') for fu in self.followups.all())
        elif self.pk:
            from followups.models import FollowUp, FollowUpStatus
            has_active_pending_fu = FollowUp.objects.filter(lead=self, followup_status__in=[FollowUpStatus.PENDING, FollowUpStatus.RESCHEDULED]).exists()

        has_booking_pending = any(k in appt_st_up for k in ["PENDING", "AWAIT", "RESCHEDULE", "SCHEDULE", "NEXT FOLLOW"]) or "AWAIT" in st_name.upper()
        if self.pk and hasattr(self, '_prefetched_objects_cache') and 'appointments' in self._prefetched_objects_cache:
            has_future_or_pending_apt = any(a.status in ('PENDING_APPROVAL', 'SCHEDULED', 'APPROVED') for a in self.appointments.all())
        elif self.pk:
            from leads.models import Appointment, AppointmentStatus
            has_future_or_pending_apt = Appointment.objects.filter(lead=self, status__in=[AppointmentStatus.PENDING_APPROVAL, AppointmentStatus.SCHEDULED, AppointmentStatus.APPROVED]).exists()
        else:
            has_future_or_pending_apt = False

        is_payment_pending = (st_name == "payment" and cd.get("payment_status") != "Done") or "PAYMENT PENDING" in appt_st_up or "PAYMENT PENDING" in raw_ds

        # 3. Check PENDING if there is an active follow-up / pending / scheduled appointment
        if has_active_pending_fu or has_booking_pending or has_future_or_pending_apt or is_payment_pending or ("FOLLOW" in st_name and has_active_pending_fu):
            return "Pending"

        # 4. Check Completed (OPD Consultation completed / visit completed)
        has_consultation_done = any(k in appt_st_up for k in ["COMPLET", "DONE", "VISIT", "CONSULTATION COMPLETE"]) or any(k in raw_ds for k in ["COMPLET", "DONE", "VISIT"])
        if has_consultation_done and not has_future_or_pending_apt and not has_active_pending_fu:
            return "Completed"

        # Check if all follow-ups are completed
        has_completed_fu = False
        if self.pk and hasattr(self, '_prefetched_objects_cache') and 'followups' in self._prefetched_objects_cache:
            has_completed_fu = any(fu.followup_status == 'COMPLETED' for fu in self.followups.all())
        elif self.pk:
            from followups.models import FollowUp, FollowUpStatus
            has_completed_fu = FollowUp.objects.filter(lead=self, followup_status=FollowUpStatus.COMPLETED).exists()

        if has_completed_fu and not has_active_pending_fu and not has_future_or_pending_apt:
            return "Completed"

        # 5. Check OPEN: Leads with follow-up remarks, interaction history, or active follow-up stage
        def is_clean_val(v):
            return bool(v and str(v).strip().lower() not in ("nan", "none", "—", "-", "", "null", "nil"))

        has_followup_or_remarks = bool(
            has_active_pending_fu
            or self.followup_count > 0
            or self.last_followup_date
            or any(is_clean_val(cd.get(k)) for k in ["remark_1", "remark_2", "remark_3", "followup_remark", "comments"])
            or bool(self.notes and self.notes.strip())
            or (self.pk and hasattr(self, '_prefetched_objects_cache') and 'followups' in self._prefetched_objects_cache and bool(self.followups.all()))
            or (self.pk and not hasattr(self, '_prefetched_objects_cache') and self.followups.exists())
        )

        if has_followup_or_remarks or "FOLLOW" in st_name:
            return "Open"

        # 6. Check NEW (Fresh leads without follow-up remarks, appointments, payments)
        return "New"

    @property
    def display_status(self):
        return self.custom_deal_status

    @property
    def display_deal_status(self):
        """
        Calculates human-readable, consistent Deal Status for the lead.
        - For Hospital: returns Hospital standardized deal status.
        - For Academy: returns Academy standardized deal status ('Open', 'Contacted', 'Interested', 'Won', 'Lost').
        - For Other: uses deal_status / admission_status text.
        """
        if self.is_hospital_industry:
            st = self.custom_deal_status
            if st in ("Lost", "Cancelled"):
                return "Lost"
            if st in ("Won", "Payment Done"):
                return "Won"
            if st in ("Pending", "Follow-up"):
                return "Pending"
            if st == "Open":
                return "Open"
            if st == "New":
                return "New"
            return self.get_deal_status_display() or self.deal_status or "Open"

        if self.is_academy_industry:
            return self.get_deal_status_display() or self.deal_status or "Open"

        return self.get_admission_status_display() or self.get_deal_status_display() or self.admission_status or self.deal_status or "Open"

    @property
    def interaction_remark(self):
        """
        Returns the actual text remark / note logged for patient interaction:
        - Prioritizes custom_data calling remarks (remark_3, remark_2, remark_1, followup_remark, comments)
        - Then checks latest FollowUp record comment
        - Then checks Lead.notes
        - Returns '—' if no textual remark exists.
        """
        cd = self.custom_data or {}

        def is_clean_text(v):
            if not v:
                return False
            s = str(v).strip()
            return bool(s and s.lower() not in ('nan', 'none', '—', '-', '', 'null', 'nil', 'na', 'n/a'))

        # 1. Check custom_data calling remarks in reverse order (most recent first)
        for k in ['remark_3', 'remark_2', 'remark_1', 'followup_remark', 'cancellation_reason', 'calling_remark']:
            val = cd.get(k)
            if is_clean_text(val):
                return str(val).strip()

        # 2. Check latest FollowUp record comment
        if hasattr(self, '_prefetched_objects_cache') and 'followups' in self._prefetched_objects_cache:
            fus = list(self.followups.all())
            if fus:
                for fu in fus:
                    if is_clean_text(fu.comment):
                        return str(fu.comment).strip()
        else:
            latest_fu = self.followups.exclude(comment__in=['', None]).order_by('-id').first()
            if latest_fu and is_clean_text(latest_fu.comment):
                return str(latest_fu.comment).strip()

        # 3. Check custom_data comments or Lead.notes (Inquiry / Questions)
        comm = cd.get('comments')
        if is_clean_text(comm):
            return str(comm).strip()

        if is_clean_text(self.notes):
            return str(self.notes).strip()

        return "—"

    @property
    def user_calling_remark(self):
        """
        Returns remarks entered ONLY by CRM users during calling / followups.
        Returns empty string if no user has made a call interaction yet.
        """
        cd = self.custom_data or {}
        def is_clean_text(v):
            if not v:
                return False
            s = str(v).strip()
            return bool(s and s.lower() not in ('nan', 'none', '—', '-', '', 'null', 'nil', 'na', 'n/a'))

        for k in ['remark_3', 'remark_2', 'remark_1', 'followup_remark', 'cancellation_reason', 'calling_remark']:
            val = cd.get(k)
            if is_clean_text(val):
                return str(val).strip()

        if hasattr(self, '_prefetched_objects_cache') and 'followups' in self._prefetched_objects_cache:
            for fu in self.followups.all():
                if is_clean_text(fu.comment):
                    return str(fu.comment).strip()
        else:
            latest_fu = self.followups.exclude(comment__in=['', None]).order_by('-id').first()
            if latest_fu and is_clean_text(latest_fu.comment):
                return str(latest_fu.comment).strip()

        return ""

    @property
    def inquiry_notes(self):
        """
        Returns initial lead form questions, symptoms, or survey responses entered at lead creation time.
        """
        cd = self.custom_data or {}
        def is_clean_text(v):
            if not v:
                return False
            s = str(v).strip()
            return bool(s and s.lower() not in ('nan', 'none', '—', '-', '', 'null', 'nil', 'na', 'n/a'))

        comm = cd.get('comments') or cd.get('survey_notes') or cd.get('issue')
        if is_clean_text(comm):
            return str(comm).strip()
        if is_clean_text(self.notes):
            return str(self.notes).strip()
        return ""

    @property
    def remark_detail(self):
        """
        Returns the actual text remark / note logged for patient interactions.
        Never returns currency amounts or appointment dates.
        """
        rem = self.interaction_remark
        if rem and rem != "—":
            return rem

        # If no actual textual remark exists, return temperature classification or '—'
        temp = self.custom_temperature
        if temp:
            return temp

        return "—"

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
            self.lead_code = next_lead_code(self.hospital)
        if not self.temperature or self.temperature.strip() not in LeadTemperature.values:
            self.temperature = LeadTemperature.WARM
        if not self.stage_id:
            default_stage = (
                LeadStage.objects.filter(name__iexact='New').first()
                or LeadStage.objects.order_by('order', 'id').first()
            )
            if not default_stage:
                # Create default 'New' stage if table is completely empty
                default_stage, _ = LeadStage.objects.get_or_create(
                    name="New",
                    defaults={"order": 1, "is_active": True}
                )
            if default_stage:
                self.stage = default_stage
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

        # Clean custom_data of any invalid JSON values (like float NaN / None strings)
        if isinstance(self.custom_data, dict):
            import math
            cleaned_cd = {}
            for k, v in self.custom_data.items():
                if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                    continue
                if str(v).strip().lower() in ("nan", "nat"):
                    continue
                cleaned_cd[str(k)] = v
            self.custom_data = cleaned_cd
        elif self.custom_data is None:
            self.custom_data = {}

        # ---------------------------------------------------------------------
        # AUTOMATIC DEAL STATUS RESOLUTION
        # ---------------------------------------------------------------------
        st_name = (self.stage.name if self.stage else "").strip().lower()
        adm_st = str(self.admission_status or "").strip().upper()
        cd = self.custom_data
        cd_apt = str(cd.get("appointment_status") or "").strip().lower()
        cd_ds = str(cd.get("deal_status") or "").strip().lower()

        # Check Total Billed
        tot = 0.0
        try:
            tot = float(cd.get("total_paid") or cd.get("total") or 0.0)
        except (ValueError, TypeError):
            tot = 0.0

        is_doc_cancelled = "doctor cancel" in cd_apt or "doctor reject" in cd_apt or st_name == "appointment cancelled"

        if self.is_academy_industry:
            # =================================================================
            # ACADEMY / EDUCATION SPECIFIC LIFECYCLE RESOLUTION
            # =================================================================
            # 1. Lost / Cancelled
            if (
                adm_st in ("LOST", "CANCELLED", "DROPOUT")
                or st_name in ("cancelled", "lost", "not interested", "rejected", "closed lost", "dropped", "dropout")
                or cd_ds in ("lost", "cancelled")
            ):
                self.deal_status = DealStatus.LOST
                self.temperature = LeadTemperature.FREEZE
                cd["deal_status"] = "Lost"

            # 2. Payment Done -> Won (HOT)
            elif (
                adm_st in ("WON", "ADMISSION_DONE")
                or tot > 0
                or st_name in ("payment done", "closed won")
                or cd_ds in ("won", "won (payment done)")
            ):
                self.deal_status = DealStatus.WON
                self.temperature = LeadTemperature.HOT
                cd["deal_status"] = "Won"

            # 3. Admission Done -> INTERESTED (HOT)
            elif st_name in ("admission done",) or cd_ds in ("admission done",):
                self.deal_status = DealStatus.INTERESTED
                self.temperature = LeadTemperature.HOT
                cd["deal_status"] = "Interested"

            # 4. Visit Planned / Visited / Admission Pending / Payment Pending -> INTERESTED (at least WARM)
            elif (
                st_name in ("visit planned", "visited", "admission pending", "payment pending")
                or cd_ds in ("interested", "visit planned", "visited", "admission pending", "payment pending")
            ):
                self.deal_status = DealStatus.INTERESTED
                cd["deal_status"] = "Interested"
                if self.temperature in (LeadTemperature.COLD, LeadTemperature.FREEZE):
                    self.temperature = LeadTemperature.WARM

            # 5. Follow-up / Follow-up Completed / Contacted
            elif (
                st_name in ("follow up", "follow-up", "follow up completed")
                or cd_ds in ("follow up", "follow-up", "follow up completed")
            ):
                self.deal_status = DealStatus.OPEN
                cd["deal_status"] = "Open"

            # 6. Assigned (counsellor assigned) / New (unassigned)
            elif st_name in ("assigned", "new", "fresh", "open") or not self.deal_status:
                if self.deal_status == DealStatus.CONTACTED and st_name == "assigned":
                    pass  # keep Contacted if counsellor added remarks
                else:
                    self.deal_status = DealStatus.OPEN
                    cd["deal_status"] = "Open"
                # Fresh unassigned leads default to HOT
                if not self.assigned_to and st_name in ("new", "fresh", ""):
                    self.temperature = LeadTemperature.HOT

        elif self.is_hospital_industry:
            # =================================================================
            # HOSPITAL CLINICAL PIPELINE (EXISTING LOGIC 100% PRESERVED)
            # =================================================================
            # Condition 1: LOST
            if not is_doc_cancelled and (
                adm_st in ("LOST", "CANCELLED", "DROPOUT")
                or st_name in ("cancelled", "lost", "not interested", "rejected", "closed lost", "dropped", "dropout")
                or "cancel" in cd_apt
                or "not int" in cd_apt
                or (cd_ds in ("lost", "cancelled") and adm_st not in ("OPEN", "HOLD", "WON"))
            ):
                self.deal_status = DealStatus.LOST
                cd["deal_status"] = "Cancelled" if ("cancel" in cd_apt or adm_st == "CANCELLED" or "cancel" in st_name) else "Lost"

            # Condition 2: WON
            elif (
                adm_st in ("WON", "ADMISSION_DONE")
                or tot > 0
                or st_name in ("payment done", "appointment completed", "admission done", "admission", "payment completed", "closed won")
                or "payment done" in cd_apt
                or "payment done" in cd_ds
                or cd_ds in ("won", "won (payment done)", "admission")
            ):
                self.deal_status = DealStatus.WON
                cd["deal_status"] = "Won (Payment Done)" if tot > 0 else "Won"

            # Condition 3: CONTACTED
            elif (
                st_name in ("follow-up", "follow up", "contacted", "awaiting doctor approval", "awaiting approval from doctor", "appointment confirmed", "booking confirmed", "payment pending", "visited", "visit planned", "interested")
                or any(k in cd_apt for k in ["follow", "visit", "book", "confirm", "contact", "awaiting"])
                or self.last_followup_date is not None
                or self.next_followup_date is not None
            ):
                self.deal_status = DealStatus.CONTACTED
                cd["deal_status"] = "Contacted"

            # Condition 4: OPEN
            elif (
                adm_st == "OPEN"
                or st_name in ("new", "fresh", "uncontacted", "assigned", "open")
                or not self.deal_status
            ):
                self.deal_status = DealStatus.OPEN
                cd["deal_status"] = "Open"

        else:
            # =================================================================
            # GENERIC / OTHER FUTURE INDUSTRIES (CLEAN SAFE DEFAULT)
            # =================================================================
            if adm_st in ("LOST", "CANCELLED") or st_name in ("cancelled", "lost"):
                self.deal_status = DealStatus.LOST
                cd["deal_status"] = "Lost"
            elif adm_st == "WON" or st_name in ("won", "closed won"):
                self.deal_status = DealStatus.WON
                cd["deal_status"] = "Won"
            elif not self.deal_status:
                self.deal_status = DealStatus.OPEN
                cd["deal_status"] = "Open"

        super().save(*args, **kwargs)

    @property
    def is_billing_done(self):
        """Check if billing / payment is completed for this lead."""
        cd = self.custom_data or {}
        st = str(self.display_status or "").strip().lower()
        if "payment done" in st or "billing done" in st or "won" in st:
            return True
        deal_st = str(cd.get("deal_status") or "").strip().lower()
        if "payment" in deal_st or "admission" in deal_st or "won" in deal_st:
            return True
        apt_st = str(cd.get("appointment_status") or "").strip().lower()
        if "payment" in apt_st:
            return True
        if cd.get("total") and str(cd.get("total")).strip() not in ["0", "0.0", "0.00", ""]:
            return True
        return False

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
        if "confirm" in apt_st or "complete" in apt_st or "done" in apt_st or "booked" in apt_st:
            return True
        return False

    def get_dynamic_whatsapp_message(self, user=None, msg_type=None):
        """
        Dynamically generates WhatsApp message text considering UserCustomMessage (if confirmed by user).
        Falls back to standard system generated message if not customized or not confirmed.
        msg_type: 'BOOKING', 'FOLLOWUP', or 'BILLING' (auto-detected if None)
        """
        import urllib.parse
        from leads.models import UserCustomMessage

        cd = self.custom_data or {}
        # =====================================================================
        # 1. ACADEMY / EDUCATION INDUSTRY
        # =====================================================================
        if self.is_academy_industry:
            student_name = (self.name or "Valued Student").strip()
            biz_name = (self.hospital.name if self.hospital else "Zappcode Academy").strip()
            # If no branch is explicitly set in custom_data, keep branch_name empty
            raw_branch = (cd.get("branch") or cd.get("hospital_branch") or "").strip()
            branch_name = raw_branch if raw_branch.lower() not in (biz_name.lower(), "main campus") else ""
            biz_address = (self.hospital.address if self.hospital and hasattr(self.hospital, 'address') and self.hospital.address else (f"{branch_name}, {biz_name}" if branch_name else biz_name)).strip()
            course_name = (self.course.name if self.course else cd.get("course") or "our training program").strip()
            
            counselor_user = user or self.assigned_to or self.created_by
            counselor_name = (counselor_user.get_full_name() or counselor_user.username if counselor_user else "Admissions Team").strip()
            
            visit_date = str(cd.get("visit_date") or self.next_followup_date or "Scheduled Visit Date").strip()
            visit_slot = str(cd.get("visit_slot") or cd.get("slot") or "11:00 AM - 01:00 PM").strip()
            admission_date = str(cd.get("admission_date") or timezone.localdate()).strip()
            batch_name = str(cd.get("batch") or cd.get("selected_batch") or "Upcoming Regular Batch").strip()
            batch_timing = str(cd.get("batch_time") or cd.get("selected_batch_timing") or "Morning Session").strip()
            
            try:
                total_fee = float(cd.get('total') or cd.get('course_fee') or (self.course.base_price if self.course else 0) or 0)
            except (ValueError, TypeError):
                total_fee = 0.0
            try:
                token_paid = float(cd.get('token_amount') or cd.get('total_paid') or 0)
            except (ValueError, TypeError):
                token_paid = 0.0
            try:
                balance_amt = float(cd.get('balance_amount') or max(0, total_fee - token_paid))
            except (ValueError, TypeError):
                balance_amt = max(0, total_fee - token_paid)
                
            due_date = str(cd.get("next_emi_date") or cd.get("payment_due_date") or "Upcoming Due Date").strip()

            if not msg_type:
                st_name = (self.stage.name if self.stage else "").lower()
                # 1. PAYMENT_DUE: Only for actual payment pending / payment reminder stage or leads where fee is configured and balance is pending
                is_payment_stage = "payment pending" in st_name or st_name == "payment" or "fee pending" in st_name
                has_actual_balance = (
                    balance_amt > 0 and (
                        bool(cd.get("total") or cd.get("course_fee") or cd.get("final_fee")) or
                        self.deal_status == DealStatus.WON or
                        "admission" in st_name or
                        "payment" in st_name
                    )
                )
                if is_payment_stage or (has_actual_balance and "payment" in st_name):
                    msg_type = "PAYMENT_DUE"
                elif self.deal_status == DealStatus.WON or "admission" in st_name:
                    msg_type = "ADMISSION"
                elif "visit" in st_name or cd.get("visit_date"):
                    msg_type = "VISIT"
                elif has_actual_balance and self.deal_status == DealStatus.WON:
                    # Won lead with remaining balance can be payment due
                    msg_type = "PAYMENT_DUE"
                else:
                    msg_type = "ENQUIRY"

            # Format branch suffix only if distinct branch actually exists
            biz_branch_display = f"{biz_name} ({branch_name})" if branch_name else biz_name

            # Check for confirmed UserCustomMessage (Default off, Custom on)
            custom_obj = None
            if user:
                custom_obj = UserCustomMessage.objects.filter(user=user, message_type=msg_type, is_confirmed=True).first()
            if not custom_obj and self.assigned_to:
                custom_obj = UserCustomMessage.objects.filter(user=self.assigned_to, message_type=msg_type, is_confirmed=True).first()
            if not custom_obj and self.hospital:
                custom_obj = UserCustomMessage.objects.filter(hospital=self.hospital, message_type=msg_type, is_confirmed=True).first()
            if not custom_obj:
                custom_obj = UserCustomMessage.objects.filter(message_type=msg_type, is_confirmed=True).first()

            if custom_obj and custom_obj.custom_text and custom_obj.custom_text.strip():
                text = custom_obj.custom_text
                replacements = {
                    "{student_name}": student_name,
                    "{patient_name}": student_name,
                    "{client_name}": student_name,
                    "{course_name}": course_name,
                    "{business_name}": biz_name,
                    "{hospital_name}": biz_name,
                    "{branch_name}": branch_name or biz_name,
                    "{business_address}": biz_address,
                    "{hospital_address}": biz_address,
                    "{counselor_name}": counselor_name,
                    "{user_name}": counselor_name,
                    "{visit_date}": visit_date,
                    "{visit_slot}": visit_slot,
                    "{admission_date}": admission_date,
                    "{batch_name}": batch_name,
                    "{batch_timing}": batch_timing,
                    "{payment_due_date}": due_date,
                    "{due_date}": due_date,
                    "{total_amount}": f"₹{total_fee:,.0f}" if total_fee else "₹0",
                    "{paid_amount}": f"₹{token_paid:,.0f}" if token_paid else "₹0",
                    "{balance_amount}": f"₹{balance_amt:,.0f}" if balance_amt else "₹0",
                }
                for k, v in replacements.items():
                    text = text.replace(k, str(v))
                return text

            # Default System Generated Templates for Academy (Active when custom is OFF)
            if msg_type in ["PAYMENT_DUE", "BILLING"]:
                return (
                    f"Hello {student_name},\n\n"
                    f"This is a gentle payment reminder from {biz_branch_display} regarding your {course_name} course.\n\n"
                    f"📌 Course Fee Details:\n"
                    f"• Total Fee: ₹{total_fee:,.0f}\n"
                    f"• Paid / Token: ₹{token_paid:,.0f}\n"
                    f"• Pending Balance: ₹{balance_amt:,.0f}\n"
                    f"• Due Date: {due_date}\n\n"
                    f"Please complete your payment before {due_date} to ensure uninterrupted learning and access to LMS & live classes.\n\n"
                    f"For payment queries or fee receipt assistance, please contact your counselor.\n\n"
                    f"Warm Regards,\n{counselor_name}\nAdmissions & Accounts Team - {biz_name}"
                )
            elif msg_type in ["ADMISSION", "BOOKING"]:
                return (
                    f"Hello {student_name},\n\n"
                    f"🎉 Congratulations & Welcome to {biz_branch_display}!\n\n"
                    f"We are delighted to confirm your admission for the {course_name} program.\n\n"
                    f"📌 Enrollment Overview:\n"
                    f"• Admission Date: {admission_date}\n"
                    f"• Batch: {batch_name}\n"
                    f"• Timing: {batch_timing}\n"
                    f"• Campus: {biz_address}\n\n"
                    f"Your student portal & orientation details will be shared shortly. We are excited to support your career journey!\n\n"
                    f"Best Wishes,\n{counselor_name}\nAdmissions Team - {biz_name}"
                )
            elif msg_type == "VISIT":
                return (
                    f"Hello {student_name},\n\n"
                    f"Greetings from {biz_branch_display}!\n\n"
                    f"Your campus visit & one-on-one career counseling demo session for {course_name} is confirmed:\n\n"
                    f"📅 Date: {visit_date}\n"
                    f"⏰ Slot / Time: {visit_slot}\n"
                    f"📍 Campus Address: {biz_address}\n\n"
                    f"Please arrive on time. Our counseling team looks forward to guiding you through our practical curriculum and lab setup.\n\n"
                    f"Warm Regards,\n{counselor_name}\nCareer Counseling Team - {biz_name}"
                )
            else:
                return (
                    f"Hello {student_name},\n\n"
                    f"Thank you for inquiring about our {course_name} program at {biz_branch_display}!\n\n"
                    f"We are excited to help you upgrade your technical skills and build a rewarding career.\n\n"
                    f"📍 Campus Address: {biz_address}\n\n"
                    f"Would you prefer a 10-minute online counseling call or a visit to our campus for a free demo session?\n\n"
                    f"Warm Regards,\n{counselor_name}\nAdmissions & Career Team - {biz_name}"
                )

        # =====================================================================
        # 2. HEALTHCARE / HOSPITAL INDUSTRY
        # =====================================================================
        if self.is_hospital_industry:
            patient_name = (self.name or "Valued Patient").strip()
            hosp_name = (self.hospital.name if self.hospital else "Nelson Mother & Child Care Hospital").strip()
            branch_name = (cd.get("hospital_branch") or cd.get("branch") or (self.hospital.name if self.hospital else "Main Branch")).strip()
            hosp_address = (self.hospital.address if self.hospital and hasattr(self.hospital, 'address') and self.hospital.address else f"{branch_name}, {hosp_name}").strip()
            doc_name = (cd.get("doctor") or "our specialist doctor").strip()
            if doc_name and not doc_name.lower().startswith("dr"):
                doc_name = f"Dr. {doc_name}"
                
            appt_date = str(cd.get("appo_booked_date") or cd.get("appointment_date") or "").strip()
            appt_time = str(cd.get("appointment_time") or "").strip()
            agent_user = user or self.assigned_to or self.created_by
            agent_name = (agent_user.get_full_name() or agent_user.username if agent_user else "Patient Care Team").strip()

            if not msg_type:
                if self.is_billing_done:
                    msg_type = "BILLING"
                elif self.is_booked:
                    msg_type = "BOOKING"
                else:
                    msg_type = "FOLLOWUP"

            # Check for confirmed UserCustomMessage (Default off, Custom on)
            custom_obj = None
            if user:
                custom_obj = UserCustomMessage.objects.filter(user=user, message_type=msg_type, is_confirmed=True).first()
            if not custom_obj and self.assigned_to:
                custom_obj = UserCustomMessage.objects.filter(user=self.assigned_to, message_type=msg_type, is_confirmed=True).first()

            if custom_obj and custom_obj.custom_text and custom_obj.custom_text.strip():
                text = custom_obj.custom_text
                replacements = {
                    "{patient_name}": patient_name,
                    "{student_name}": patient_name,
                    "{client_name}": patient_name,
                    "{hospital_name}": hosp_name,
                    "{business_name}": hosp_name,
                    "{branch_name}": branch_name,
                    "{hospital_address}": hosp_address,
                    "{business_address}": hosp_address,
                    "{doctor_name}": doc_name,
                    "{appointment_date}": appt_date or "Scheduled Date",
                    "{appointment_time}": appt_time or "Scheduled Slot",
                    "{user_name}": agent_name,
                }
                for k, v in replacements.items():
                    text = text.replace(k, str(v))
                return text

            # Default System Generated Templates for Hospital (Active when custom is OFF)
            if msg_type == "BILLING":
                date_part = f" on {appt_date}" if appt_date else ""
                return (
                    f"Hello {patient_name},\n\n"
                    f"Thank you for visiting {hosp_name} ({branch_name})!\n\n"
                    f"Your consultation with {doc_name}{date_part} has been completed successfully.\n\n"
                    f"We truly appreciate having the opportunity to care for your health and well-being. If you have any follow-up questions, prescription queries, or require further medical assistance, please feel free to reach out to us.\n\n"
                    f"Hospital Address: {hosp_address}\n\n"
                    f"Wishing you great health and a speedy recovery!\n\n"
                    f"Warm Regards,\n{agent_name}\nPatient Care Team - {hosp_name}"
                )
            elif msg_type == "BOOKING":
                date_part = f" on {appt_date}" if appt_date else ""
                time_part = f" at {appt_time}" if appt_time else ""
                return (
                    f"Hello {patient_name},\n\n"
                    f"Welcome to {hosp_name} ({branch_name})!\n\n"
                    f"Your consultation appointment with {doc_name} is confirmed{date_part}{time_part}.\n\n"
                    f"Hospital Address: {hosp_address}\n\n"
                    f"Please arrive 15 minutes prior to your scheduled slot. We look forward to assisting you.\n\n"
                    f"For any queries or assistance, feel free to contact us.\n\n"
                    f"Warm Regards,\n{agent_name}\n{hosp_name}"
                )
            else:
                return (
                    f"Hello {patient_name},\n\n"
                    f"Thank you for your enquiry with {hosp_name} ({branch_name})!\n\n"
                    f"We are pleased to assist you with your healthcare and doctor consultation inquiry.\n\n"
                    f"Hospital Address: {hosp_address}\n\n"
                    f"Please let us know your preferred date, time, or specialist requirement so we can schedule your appointment promptly.\n\n"
                    f"Warm Regards,\n{agent_name}\nPatient Care Team - {hosp_name}"
                )

        # =====================================================================
        # 3. OTHER / GENERAL BUSINESSES (IT Services, Real Estate, Agency, Corporate)
        # =====================================================================
        client_name = (self.name or "Valued Client").strip()
        biz_name = (self.hospital.name if self.hospital else "Our Business").strip()
        branch_name = (cd.get("branch") or cd.get("hospital_branch") or (self.hospital.name if self.hospital else "Head Office")).strip()
        biz_address = (self.hospital.address if self.hospital and hasattr(self.hospital, 'address') and self.hospital.address else f"{branch_name}, {biz_name}").strip()
        service_name = (self.course.name if self.course else cd.get("service") or cd.get("requirement") or "our services").strip()
        
        agent_user = user or self.assigned_to or self.created_by
        agent_name = (agent_user.get_full_name() or agent_user.username if agent_user else "Client Support Team").strip()
        
        booking_date = str(cd.get("appointment_date") or self.next_followup_date or "Scheduled Date").strip()
        booking_time = str(cd.get("appointment_time") or "Business Hours").strip()

        if not msg_type:
            if self.deal_status == DealStatus.WON or self.is_billing_done:
                msg_type = "BILLING"
            elif self.is_booked or "book" in (self.stage.name if self.stage else "").lower():
                msg_type = "BOOKING"
            else:
                msg_type = "FOLLOWUP"

        # Check for confirmed UserCustomMessage (Default off, Custom on)
        custom_obj = None
        if user:
            custom_obj = UserCustomMessage.objects.filter(user=user, message_type=msg_type, is_confirmed=True).first()
        if not custom_obj and self.assigned_to:
            custom_obj = UserCustomMessage.objects.filter(user=self.assigned_to, message_type=msg_type, is_confirmed=True).first()

        if custom_obj and custom_obj.custom_text and custom_obj.custom_text.strip():
            text = custom_obj.custom_text
            replacements = {
                "{client_name}": client_name,
                "{student_name}": client_name,
                "{patient_name}": client_name,
                "{service_name}": service_name,
                "{course_name}": service_name,
                "{business_name}": biz_name,
                "{hospital_name}": biz_name,
                "{branch_name}": branch_name,
                "{business_address}": biz_address,
                "{hospital_address}": biz_address,
                "{booking_date}": booking_date,
                "{appointment_date}": booking_date,
                "{booking_time}": booking_time,
                "{appointment_time}": booking_time,
                "{agent_name}": agent_name,
                "{user_name}": agent_name,
            }
            for k, v in replacements.items():
                text = text.replace(k, str(v))
            return text

        # Default System Generated Templates for Other Industries (Active when custom is OFF)
        if msg_type == "BILLING":
            return (
                f"Hello {client_name},\n\n"
                f"Thank you for choosing {biz_name} ({branch_name})!\n\n"
                f"Your order/service completion for {service_name} has been processed successfully.\n\n"
                f"If you need further assistance or project support, feel free to reach out to us.\n\n"
                f"Office Address: {biz_address}\n\n"
                f"Warm Regards,\n{agent_name}\nCustomer Success Team - {biz_name}"
            )
        elif msg_type == "BOOKING":
            return (
                f"Hello {client_name},\n\n"
                f"Greetings from {biz_name}!\n\n"
                f"Your meeting / demo appointment for {service_name} is confirmed on {booking_date} at {booking_time}.\n\n"
                f"Office Address: {biz_address}\n\n"
                f"We look forward to meeting with you.\n\n"
                f"Warm Regards,\n{agent_name}\n{biz_name}"
            )
        else:
            return (
                f"Hello {client_name},\n\n"
                f"Thank you for contacting {biz_name} regarding {service_name}!\n\n"
                f"Our team is glad to assist you. Please let us know a convenient time to discuss your requirements in detail.\n\n"
                f"Office Address: {biz_address}\n\n"
                f"Warm Regards,\n{agent_name}\nBusiness Team - {biz_name}"
            )

    @property
    def whatsapp_message(self):
        """
        Dynamically returns URL-encoded WhatsApp message.
        """
        import urllib.parse
        if self.is_academy_industry:
            st_name = (self.stage.name if self.stage else "").lower()
            cd = self.custom_data or {}
            try:
                tot = float(cd.get('total') or cd.get('course_fee') or (self.course.base_price if self.course else 0) or 0)
                paid = float(cd.get('token_amount') or cd.get('total_paid') or 0)
                bal = float(cd.get('balance_amount') or max(0, tot - paid))
            except Exception:
                bal = 0.0
            is_payment_stage = "payment pending" in st_name or st_name == "payment" or "fee pending" in st_name
            has_actual_balance = (
                bal > 0 and (
                    bool(cd.get("total") or cd.get("course_fee") or cd.get("final_fee")) or
                    self.deal_status == DealStatus.WON or
                    "admission" in st_name or
                    "payment" in st_name
                )
            )
            if is_payment_stage or (has_actual_balance and "payment" in st_name):
                msg_type = "PAYMENT_DUE"
            elif self.deal_status == DealStatus.WON or "admission" in st_name:
                msg_type = "ADMISSION"
            elif "visit" in st_name or cd.get("visit_date"):
                msg_type = "VISIT"
            elif has_actual_balance and self.deal_status == DealStatus.WON:
                msg_type = "PAYMENT_DUE"
            else:
                msg_type = "ENQUIRY"
        elif self.is_billing_done:
            msg_type = "BILLING"
        elif self.is_booked:
            msg_type = "BOOKING"
        else:
            msg_type = "FOLLOWUP"
        text = self.get_dynamic_whatsapp_message(msg_type=msg_type)
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
    is_rescheduled = models.BooleanField(default=False)
    rescheduled_from_date = models.DateField(null=True, blank=True)
    rescheduled_from_time = models.TimeField(null=True, blank=True)
    
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def business(self):
        return self.hospital

    @property
    def business_id(self):
        return self.hospital_id
    
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

    @property
    def business(self):
        return self.hospital

    @property
    def business_id(self):
        return self.hospital_id

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

    @property
    def business(self):
        return self.hospital

    @property
    def business_id(self):
        return self.hospital_id

    class Meta:
        ordering = ["-start_date"]

    def __str__(self):
        return f"{self.doctor.username} on leave: {self.start_date} to {self.end_date}"


class UserCustomMessage(models.Model):
    """
    Custom WhatsApp message templates defined by User or Hospital.
    Supports 3 message types:
    - 'BOOKING': Appointment Confirmation Message
    - 'FOLLOWUP': Thank you for Enquiry & Follow-up Message
    - 'BILLING': Thank you for Visiting & Payment/Billing Completed Message
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="custom_messages")
    hospital = models.ForeignKey("accounts.Hospital", on_delete=models.CASCADE, null=True, blank=True, related_name="custom_messages")
    message_type = models.CharField(
        max_length=20,
        choices=[
            ("BOOKING", "Booking / Admission Message"),
            ("FOLLOWUP", "Follow-up / Welcome Enquiry Message"),
            ("BILLING", "Billing / Payment Message"),
            ("VISIT", "Campus Visit Template"),
            ("ADMISSION", "Admission Confirmed Template"),
            ("PAYMENT_DUE", "Payment Due / Installment Template"),
            ("ENQUIRY", "Welcome & Enquiry Template"),
        ],
        db_index=True,
    )
    custom_text = models.TextField(blank=True, help_text="Custom template text with placeholders like {student_name}, {course_name}, {business_name}, {visit_date}, {visit_slot}, {admission_date}, {payment_due_date}, {balance_amount}, {counselor_name}, {business_address}")
    is_confirmed = models.BooleanField(default=False, help_text="True if user has confirmed using this custom template over system default")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "message_type")
        verbose_name = "User Custom Message"
        verbose_name_plural = "User Custom Messages"

    @property
    def business(self):
        return self.hospital

    @business.setter
    def business(self, value):
        self.hospital = value

    @property
    def business_id(self):
        return self.hospital_id

    @business_id.setter
    def business_id(self, value):
        self.hospital_id = value

    def __str__(self):
        return f"{self.user.username} - {self.get_message_type_display()} ({'Custom Active' if self.is_confirmed else 'System Default'})"


