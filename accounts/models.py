import uuid
from django.contrib.auth.models import AbstractUser
from django.db import models


class Hospital(models.Model):
    name = models.CharField(max_length=255)
    logo = models.ImageField(upload_to='hospital_logos/', null=True, blank=True)
    contact_email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    registration_no = models.CharField(max_length=100, blank=True)
    settings = models.JSONField(default=dict, blank=True)
    allowed_roles = models.JSONField(default=list, blank=True, help_text="List of roles enabled for this business/tenant (e.g. ['ADMIN', 'MANAGER', 'LEAD_ATTENDENT', 'DOCTOR'])")
    is_active = models.BooleanField(default=True, db_index=True, help_text="Active status of this business/tenant")
    created_at = models.DateTimeField(auto_now_add=True)

    def get_allowed_roles(self):
        """Returns list of allowed role keys for this business. Excludes Super Admin & Zappcode internal roles for hospitals."""
        if self.allowed_roles and isinstance(self.allowed_roles, list) and len(self.allowed_roles) > 0:
            return self.allowed_roles
        # Hospital Roles: only Admin, Manager, Lead Attendant, and Doctor
        hospital_default_roles = [
            User.Role.ADMIN,
            User.Role.MANAGER,
            User.Role.LEAD_ATTENDENT,
            User.Role.DOCTOR,
        ]
        return [r for r in hospital_default_roles if r in [c[0] for c in User.Role.choices]]

    def __str__(self):
        return self.name

class User(AbstractUser):
    """Custom user with CRM role. Role drives server-side permission checks
    everywhere (views, querysets) — never trust the frontend alone."""

    class Role(models.TextChoices):
        SUPER_ADMIN = "SUPER_ADMIN", "Super Admin"
        ADMIN = "ADMIN", "Admin"
        MANAGER = "MANAGER", "Manager"
        LEAD_ATTENDENT = "LEAD_ATTENDENT", "Lead Attendant"
        DOCTOR = "DOCTOR", "Doctor"
        HR = "HR", "HR"
        COUNSELLOR = "COUNSELLOR", "Counsellor"

    profile_picture = models.ImageField(upload_to='profile_pics/', null=True, blank=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.LEAD_ATTENDENT)
    phone = models.CharField(max_length=20, blank=True)
    is_active_employee = models.BooleanField(default=True)
    is_approved = models.BooleanField(default=False, db_index=True)
    reports_to = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="team_members"
    )
    DEPARTMENT_CHOICES = [
        ("GYNAEC", "GYNAEC"),
        ("NEUROLOGY", "NEUROLOGY"),
        ("NEURO SURGERY", "NEURO SURGERY"),
        ("PED.NEUROLOGY", "PED.NEUROLOGY"),
        ("PEDIATRIC", "PEDIATRIC"),
        ("OPTHALMOLOGY", "OPTHALMOLOGY"),
        ("ORTHOPEDICS", "ORTHOPEDICS"),
        ("GASTROLOGY", "GASTROLOGY"),
        ("PEDIATRIC NEPHROLOGOGIST", "PEDIATRIC NEPHROLOGOGIST"),
        ("PED.SURGERY", "PED.SURGERY"),
        ("CARDIAC SURGERY", "CARDIAC SURGERY"),
        ("PLASTIC SURGERY", "PLASTIC SURGERY"),
        ("GENRAL SURGERY", "GENRAL SURGERY"),
        ("GENERAL MEDICINE", "GENERAL MEDICINE"),
        ("UROLOGY", "UROLOGY"),
        ("PHYSIOTHERAPY", "PHYSIOTHERAPY"),
        ("ENT", "ENT"),
        ("DERMATOLOGIST", "DERMATOLOGIST"),
    ]

    SPECIALITY_CHOICES = [
        ("Surgeon", "Surgeon"),
        ("Physician", "Physician"),
        ("Pediatrician", "Pediatrician"),
        ("Gynecologist", "Gynecologist"),
        ("Neurologist", "Neurologist"),
        ("Cardiologist", "Cardiologist"),
        ("Dermatologist", "Dermatologist"),
        ("Orthopedist", "Orthopedist"),
        ("ENT Specialist", "ENT Specialist"),
        ("Physiotherapist", "Physiotherapist"),
        ("Urologist", "Urologist"),
        ("Ophthalmologist", "Ophthalmologist"),
        ("Other", "Other"),
    ]

    department = models.CharField(max_length=50, choices=DEPARTMENT_CHOICES, null=True, blank=True)
    speciality = models.CharField(max_length=50, choices=SPECIALITY_CHOICES, null=True, blank=True)
    hospital = models.ForeignKey(Hospital, on_delete=models.SET_NULL, null=True, blank=True, related_name="users")
    
    # Store individual permission overrides here
    custom_permissions = models.JSONField(default=dict, blank=True)

    @property
    def is_hospital_user(self):
        return bool(self.hospital)

    @property
    def is_zappcode_user(self):
        return not bool(self.hospital)

    @property
    def custom_role_display(self):
        prefix = "hospital-user" if self.hospital else "zappcode-user"
        if self.role == self.Role.SUPER_ADMIN:
            role_name = "Hospital Super Admin" if self.hospital else "Zappcode Super Admin"
        elif self.role == self.Role.ADMIN:
            role_name = "Hospital Admin" if self.hospital else "Zappcode Admin"
        elif self.role == self.Role.MANAGER:
            role_name = "Hospital Manager" if self.hospital else "Zappcode Manager"
        else:
            role_name = self.get_role_display()
        return f"{prefix} ({role_name})"

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"

    def has_dynamic_permission(self, perm_key, default=False):
        """
        Check if the user has a specific permission.
        1. Checks custom_permissions for an individual override.
        2. Falls back to HospitalRolePermission for their hospital and role.
        3. Returns the default if not configured.
        """
        if perm_key in self.custom_permissions:
            return self.custom_permissions[perm_key]
        
        if self.hospital:
            role_perm = HospitalRolePermission.objects.filter(hospital=self.hospital, role=self.role).first()
            if role_perm and perm_key in role_perm.permissions:
                return role_perm.permissions[perm_key]
                
        return default

    @property
    def daily_call_target(self):
        try:
            return int(self.custom_permissions.get("daily_call_target", 100))
        except (ValueError, TypeError):
            return 100

    @property
    def can_manage_users(self):
        return self.has_dynamic_permission("manage_users", default=self.role in (self.Role.SUPER_ADMIN, self.Role.ADMIN, self.Role.MANAGER))

    @property
    def can_assign_leads(self):
        return self.has_dynamic_permission("assign_leads", default=self.role in (self.Role.SUPER_ADMIN, self.Role.ADMIN, self.Role.MANAGER))

    @property
    def can_manage_masters(self):
        return self.has_dynamic_permission("manage_masters", default=self.role in (self.Role.SUPER_ADMIN,))

    @property
    def can_import_export(self):
        return self.has_dynamic_permission("import_export", default=self.role in (self.Role.SUPER_ADMIN, self.Role.ADMIN, self.Role.MANAGER, self.Role.LEAD_ATTENDENT, self.Role.COUNSELLOR))

    @property
    def can_view_all_leads(self):
        return self.has_dynamic_permission("view_all_leads", default=self.role in (self.Role.SUPER_ADMIN, self.Role.ADMIN, self.Role.MANAGER))
        
    @property
    def can_view_team_leads(self):
        return self.has_dynamic_permission("view_team_leads", default=self.role in (self.Role.MANAGER, self.Role.LEAD_ATTENDENT))
        
    @property
    def can_view_assigned_leads(self):
        return self.has_dynamic_permission("view_assigned_leads", default=True)

    @property
    def can_add_leads(self):
        return self.has_dynamic_permission("add_leads", default=self.role != self.Role.DOCTOR)
        
    @property
    def can_edit_any_lead(self):
        return self.has_dynamic_permission("edit_any_lead", default=self.role in (self.Role.SUPER_ADMIN, self.Role.ADMIN, self.Role.MANAGER))
        
    @property
    def can_edit_own_leads(self):
        return self.has_dynamic_permission("edit_own_leads", default=True)
        
    @property
    def can_delete_leads(self):
        return self.has_dynamic_permission("delete_leads", default=self.role in (self.Role.SUPER_ADMIN, self.Role.ADMIN))

    @property
    def is_read_only(self):
        return self.has_dynamic_permission("read_only", default=False)

    @property
    def can_view_admin_dashboard(self):
        return self.has_dynamic_permission("view_admin_dashboard", default=self.role in (self.Role.SUPER_ADMIN, self.Role.ADMIN))

    @property
    def can_manage_campaigns(self):
        return self.has_dynamic_permission("manage_campaigns", default=self.role in (self.Role.SUPER_ADMIN, self.Role.ADMIN))

    @property
    def can_view_financials(self):
        return self.has_dynamic_permission("view_financials", default=self.role in (self.Role.SUPER_ADMIN, self.Role.ADMIN))

    @property
    def can_manage_hospital_profile(self):
        return self.has_dynamic_permission("manage_hospital_profile", default=self.role in (self.Role.SUPER_ADMIN, self.Role.ADMIN))

    @property
    def can_view_reports(self):
        return self.has_dynamic_permission("view_reports", default=self.role in (self.Role.SUPER_ADMIN, self.Role.ADMIN))


class HospitalRolePermission(models.Model):
    hospital = models.ForeignKey(Hospital, on_delete=models.CASCADE, related_name="role_permissions")
    role = models.CharField(max_length=50, choices=User.Role.choices)
    permissions = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = ("hospital", "role")

    def __str__(self):
        return f"{self.hospital.name} - {self.get_role_display()} Permissions"
