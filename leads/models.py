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
    name = models.CharField(max_length=150, unique=True)
    base_price = models.PositiveIntegerField(default=0, help_text="Base course fee in Rupees")
    max_discount = models.PositiveIntegerField(default=0, help_text="Maximum allowed discount in Rupees")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

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

    def __str__(self):
        return f"{self.group.name} → {self.name}"



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

    @staticmethod
    def clean_mobile(raw):
        """Normalize a phone string for duplicate-detection matching."""
        import re
        digits = re.sub(r"\D", "", str(raw or ""))
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        if len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        return digits

    def find_duplicates(self):
        digits = self.clean_mobile(self.mobile)
        qs = Lead.objects.exclude(pk=self.pk)
        if digits:
            from django.db.models.functions import Replace
            candidates = [l for l in qs.only("id", "mobile") if Lead.clean_mobile(l.mobile) == digits]
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
    SCHEDULED = "SCHEDULED", "Scheduled"
    COMPLETED = "COMPLETED", "Completed"
    CANCELLED = "CANCELLED", "Cancelled"
    NO_SHOW = "NO_SHOW", "No-Show"

class Appointment(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="appointments")
    hospital = models.ForeignKey("accounts.Hospital", on_delete=models.CASCADE, null=True, blank=True, related_name="appointments")
    doctor_name = models.CharField(max_length=150)
    appointment_date = models.DateField()
    appointment_time = models.TimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=AppointmentStatus.choices, default=AppointmentStatus.SCHEDULED)
    notes = models.TextField(blank=True)
    
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ["-appointment_date", "-appointment_time"]
        
    def __str__(self):
        return f"{self.lead.name} - {self.doctor_name} ({self.appointment_date})"

