import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zappcodecrm.settings")
django.setup()

from accounts.models import Hospital, User
from leads.models import Lead

# Create the hospital
hospital, created = Hospital.objects.get_or_create(
    name="Nelson Hospital",
    defaults={
        "contact_email": "admin@nelsonhospital.com",
        "phone": "9999999999"
    }
)
print(f"Hospital {'created' if created else 'found'}: {hospital}")

# Assign to users
nelson_users = User.objects.filter(username__icontains='nelson')
updated_users = nelson_users.update(hospital=hospital)
print(f"Assigned hospital to {updated_users} users.")

# Assign to leads
nelson_leads = Lead.objects.filter(nelson_data__isnull=False)
updated_leads = nelson_leads.update(hospital=hospital)
print(f"Assigned hospital to {updated_leads} leads.")
