from django.core.management.base import BaseCommand
from leads.models import SourceCategory, LeadSource, Course, LeadStage


CATEGORIES = {
    "Digital Marketing": ["Google Ads", "Meta Ads", "Instagram", "YouTube", "Google Organic", "Website", "Landing Page", "Paid Ads - Unspecified"],
    "Direct": ["Walk-in", "Phone Call", "Office Enquiry"],
    "Referral": ["Student Referral", "Employee Referral", "Partner Referral"],
    "Outreach": ["WhatsApp", "Email", "Cold Call"],
    "Offline": ["College Visit", "Event", "Exhibition"],
    "Other": ["Other", "ChatGPT Referral"],
}

COURSES = [
    "Data Analytics", "Data Science", "Data Analytics + AI", "AI", "AI/ML", "Python",
    "Advance Python", "Python + AI", "Python Full Stack", "Full Stack Development",
    "Full Stack Development + AI", "Business Analyst", "Advance Excel",
]

STAGES = [
    ("New", 0), ("Contacted", 1), ("Interested", 2), ("Follow-up", 3),
    ("Visit Planned", 4), ("Visited", 5), ("Counselling Done", 6),
    ("Negotiation", 7), ("Admission", 8), ("Lost", 9), ("Hold", 10),
]


class Command(BaseCommand):
    help = "Seed default master data (source categories, lead sources, courses, lead stages)."

    def handle(self, *args, **options):
        for cat_name, sources in CATEGORIES.items():
            cat, _ = SourceCategory.objects.get_or_create(name=cat_name)
            for i, src_name in enumerate(sources):
                LeadSource.objects.get_or_create(name=src_name, category=cat, defaults={"order": i})

        for name in COURSES:
            Course.objects.get_or_create(name=name)

        for name, order in STAGES:
            LeadStage.objects.get_or_create(name=name, defaults={"order": order})

        self.stdout.write(self.style.SUCCESS("Master data seeded."))
