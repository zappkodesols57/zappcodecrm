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
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class User(AbstractUser):
    """Custom user with CRM role. Role drives server-side permission checks
    everywhere (views, querysets) — never trust the frontend alone."""

    class Role(models.TextChoices):
        SUPER_ADMIN = "SUPER_ADMIN", "Super Admin"
        ADMIN = "ADMIN", "Admin"
        MANAGER = "MANAGER", "Manager"
        LEAD_ATTENDENT = "LEAD_ATTENDENT", "Lead Attendent"
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
        return self.has_dynamic_permission("import_export", default=self.role in (self.Role.SUPER_ADMIN, self.Role.MANAGER))

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

class HospitalRolePermission(models.Model):
    hospital = models.ForeignKey(Hospital, on_delete=models.CASCADE, related_name="role_permissions")
    role = models.CharField(max_length=50, choices=User.Role.choices)
    permissions = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = ("hospital", "role")

    def __str__(self):
        return f"{self.hospital.name} - {self.get_role_display()} Permissions"
