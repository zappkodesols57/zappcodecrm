import uuid
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Custom user with CRM role. Role drives server-side permission checks
    everywhere (views, querysets) — never trust the frontend alone."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Role(models.TextChoices):
        SUPER_ADMIN = "SUPER_ADMIN", "Super Admin"
        MANAGER = "MANAGER", "Manager"
        COUNSELLOR = "COUNSELLOR", "Counsellor"
        HR = "HR", "HR"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.COUNSELLOR)
    phone = models.CharField(max_length=20, blank=True)
    is_active_employee = models.BooleanField(default=True)
    is_approved = models.BooleanField(default=False, db_index=True)
    reports_to = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="team_members"
    )

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"

    @property
    def can_manage_users(self):
        return self.role in (self.Role.SUPER_ADMIN, self.Role.MANAGER)

    @property
    def can_assign_leads(self):
        return self.role in (self.Role.SUPER_ADMIN, self.Role.MANAGER)

    @property
    def can_manage_masters(self):
        return self.role in (self.Role.SUPER_ADMIN,)

    @property
    def can_import_export(self):
        return self.role in (self.Role.SUPER_ADMIN, self.Role.MANAGER)

    @property
    def is_read_only(self):
        return False
