import uuid
from django.contrib.auth.models import AbstractUser
from django.db import models


class Hospital(models.Model):
    name = models.CharField(max_length=255)
    logo = models.ImageField(upload_to='hospital_logos/', null=True, blank=True)
    contact_email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
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

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"

    @property
    def can_manage_users(self):
        return self.role in (self.Role.SUPER_ADMIN, self.Role.ADMIN, self.Role.MANAGER)

    @property
    def can_assign_leads(self):
        return self.role in (self.Role.SUPER_ADMIN, self.Role.ADMIN, self.Role.MANAGER)

    @property
    def can_manage_masters(self):
        return self.role in (self.Role.SUPER_ADMIN,)

    @property
    def can_import_export(self):
        return self.role in (self.Role.SUPER_ADMIN, self.Role.MANAGER)

    @property
    def is_read_only(self):
        return False
