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
    branch = models.ForeignKey("leads.HospitalBranch", on_delete=models.SET_NULL, null=True, blank=True, related_name="users", help_text="Assigned Branch for Branch Managers, Telecallers, and Doctors. Null/Empty means All Branches (Full Business Access).")
    
    # Store individual permission overrides here
    custom_permissions = models.JSONField(default=dict, blank=True)

    @property
    def business_type(self):
        """
        Returns the business type of the user's assigned tenant/organization:
        - 'hospital': Healthcare / clinic tenants (e.g. Nelson Hospital)
        - 'academy': Education / coaching / Zappcode Academy tenants
        """
        if not self.hospital:
            return "academy"
        btype = (self.hospital.settings or {}).get("business_type")
        if btype:
            return str(btype).strip().lower()
        name_lower = (self.hospital.name or "").lower()
        if "hospital" in name_lower or "clinic" in name_lower or "medical" in name_lower or "nelson" in name_lower:
            return "hospital"
        return "academy"

    @property
    def is_hospital_user(self):
        """True if user belongs to a hospital-type business."""
        return self.business_type == "hospital"

    @property
    def is_zappcode_user(self):
        """True if user belongs to academy/education business or global Zappcode super admin."""
        return self.business_type == "academy"

    @property
    def custom_role_display(self):
        is_hospital = self.is_hospital_user
        prefix = "hospital-user" if is_hospital else "zappcode-user"
        if self.role == self.Role.SUPER_ADMIN:
            role_name = "Hospital Super Admin" if is_hospital else "Zappcode Super Admin"
        elif self.role == self.Role.ADMIN:
            role_name = "Hospital Admin" if is_hospital else "Zappcode Admin"
        elif self.role == self.Role.MANAGER:
            role_name = "Hospital Manager" if is_hospital else "Zappcode Manager"
        else:
            role_name = self.get_role_display()
        return f"{prefix} ({role_name})"

    @property
    def doctor_departments_list(self):
        """
        Returns a list of dicts for all assigned departments for this doctor:
        [{'name': 'NEUROLOGY', 'is_primary': True}, {'name': 'PEDIATRIC', 'is_primary': False}]
        The primary department chosen at profile creation time is marked with is_primary=True.
        """
        primary_name = (self.department or "").strip()
        result = []
        seen = set()

        if primary_name:
            result.append({"name": primary_name, "is_primary": True})
            seen.add(primary_name.lower())

        # Check linked HospitalDoctor profile departments
        doc_profile = getattr(self, "doctor_profile", None)
        if not doc_profile and self.hospital and self.role == self.Role.DOCTOR:
            from leads.models import HospitalDoctor
            doc_profile = HospitalDoctor.objects.filter(hospital=self.hospital, user=self).first()

        if doc_profile:
            # Check secondary / many-to-many departments
            for dept in doc_profile.departments.filter(is_active=True):
                d_name = dept.name.strip()
                if d_name.lower() not in seen:
                    result.append({"name": d_name, "is_primary": False})
                    seen.add(d_name.lower())
            # Check doc_profile.department fallback
            if doc_profile.department and doc_profile.department.name.strip().lower() not in seen:
                d_name = doc_profile.department.name.strip()
                result.append({"name": d_name, "is_primary": (len(result) == 0)})
                seen.add(d_name.lower())

        return result

    @property
    def doctor_departments_display(self):
        """
        Returns HTML formatted string of departments:
        Primary department in <strong>...</strong> and other assigned departments normal.
        """
        depts = self.doctor_departments_list
        if not depts:
            return self.department or "General OPD"
        
        parts = []
        for d in depts:
            if d["is_primary"]:
                parts.append(f"<strong>{d['name']}</strong>")
            else:
                parts.append(f"{d['name']}")
        return ", ".join(parts)

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"

    def has_dynamic_permission(self, perm_key, default=False):
        """
        Check if the user has a specific permission.
        1. Checks custom_permissions for an individual override.
        2. Falls back to HospitalRolePermission for their hospital and role (cached on user instance).
        3. Returns the default if not configured.
        """
        if perm_key in self.custom_permissions:
            return self.custom_permissions[perm_key]
        
        if self.hospital:
            if not hasattr(self, '_cached_hospital_role_perm'):
                self._cached_hospital_role_perm = HospitalRolePermission.objects.filter(
                    hospital=self.hospital, role=self.role
                ).first()
            role_perm = self._cached_hospital_role_perm
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
    def bulk_self_assign_limit(self):
        try:
            return int(self.custom_permissions.get("bulk_self_assign_limit", 25))
        except (ValueError, TypeError):
            return 25

    @property
    def can_self_assign(self):
        # Doctors, Admins, Super Admins, and Managers should never self-assign leads (only telecallers, counsellors, HR, attendants)
        if self.role in (self.Role.DOCTOR, self.Role.ADMIN, self.Role.SUPER_ADMIN, self.Role.MANAGER):
            return False
        if "allow_self_assign" in self.custom_permissions:
            return bool(self.custom_permissions["allow_self_assign"])
        if "can_self_assign" in self.custom_permissions:
            return bool(self.custom_permissions["can_self_assign"])
        return self.role in (self.Role.COUNSELLOR, self.Role.HR, self.Role.LEAD_ATTENDENT)

    @property
    def can_manage_users(self):
        return self.has_dynamic_permission("manage_users", default=self.role in (self.Role.SUPER_ADMIN, self.Role.ADMIN, self.Role.MANAGER))

    @property
    def can_assign_leads(self):
        return self.has_dynamic_permission("assign_leads", default=self.role in (self.Role.SUPER_ADMIN, self.Role.ADMIN, self.Role.MANAGER))

    @property
    def can_manage_masters(self):
        return self.has_dynamic_permission("manage_masters", default=self.role in (self.Role.SUPER_ADMIN, self.Role.ADMIN, self.Role.MANAGER))

    @property
    def can_manage_hospital_profile(self):
        return self.has_dynamic_permission("manage_hospital_profile", default=self.role in (self.Role.SUPER_ADMIN, self.Role.ADMIN, self.Role.MANAGER))

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
    def can_delete_master_data(self):
        """Permission to delete/purge master lead data. Super Admin always can; Business Admin can only if granted permission."""
        if self.role == self.Role.SUPER_ADMIN:
            return True
        if self.role == self.Role.ADMIN:
            return bool(self.has_dynamic_permission("delete_master_data", default=False))
        return False

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
