from django.core.management.base import BaseCommand
from leads.models import MasterGroup, MasterItem


DEFAULT_MASTERS = {
    "Qualifications / Degrees": {
        "description": "Educational qualifications for lead background tracking",
        "items": [
            ("10th Standard / SSC", "SSC", 1),
            ("12th Standard / HSC", "HSC", 2),
            ("B.Tech / B.E. (Engineering)", "BTECH", 3),
            ("B.Sc / B.C.A (Science / IT)", "BCA", 4),
            ("B.Com / B.B.A (Commerce / Mgmt)", "BCOM", 5),
            ("B.A. (Arts / Humanities)", "BA", 6),
            ("M.Tech / M.E.", "MTECH", 7),
            ("M.Sc / M.C.A", "MCA", 8),
            ("M.B.A / P.G.D.M", "MBA", 9),
            ("Diploma Holder", "DIPLOMA", 10),
            ("Other / Working Professional", "OTHER", 11),
        ]
    },
    "Branch / Center Offices": {
        "description": "Physical branch or training center locations",
        "items": [
            ("Head Office - Main Center", "HO", 1),
            ("Branch 1 - North Center", "BR1", 2),
            ("Branch 2 - South Center", "BR2", 3),
            ("Online / Remote Training", "ONLINE", 4),
        ]
    },
    "Lead Rejection / Loss Reasons": {
        "description": "Reasons why a deal or lead was lost / dropped",
        "items": [
            ("Fee / Price Too High", "PRICE", 1),
            ("Joined Competitor Institute", "COMPETITOR", 2),
            ("Location / Distance Issue", "LOCATION", 3),
            ("Timing / Batch Schedule Conflict", "TIMING", 4),
            ("Not Interested Anymore", "NO_INT", 5),
            ("Invalid / Wrong Number", "WRONG_NO", 6),
            ("Looking for Placement Only", "PLACEMENT", 7),
            ("Other / Unspecified", "OTHER", 8),
        ]
    },
    "Work Experience Level": {
        "description": "Professional work experience background of candidates",
        "items": [
            ("Fresher / Student (0 Years)", "FRESHER", 1),
            ("1 - 2 Years", "EXP_1_2", 2),
            ("3 - 5 Years", "EXP_3_5", 3),
            ("5+ Years", "EXP_5PLUS", 4),
        ]
    }
}


class Command(BaseCommand):
    help = "Seed standard Universal Master categories and sub-master items."

    def handle(self, *args, **options):
        for group_name, group_data in DEFAULT_MASTERS.items():
            group, created = MasterGroup.objects.get_or_create(
                name=group_name,
                defaults={"description": group_data["description"], "is_active": True}
            )
            if created:
                self.stdout.write(self.style.SUCCESS(f"Created Master Group: {group_name}"))
            else:
                self.stdout.write(f"Master Group exists: {group_name}")

            for name, code, order in group_data["items"]:
                item, item_created = MasterItem.objects.get_or_create(
                    group=group,
                    name=name,
                    defaults={"code": code, "order": order, "is_active": True}
                )
                if item_created:
                    self.stdout.write(f"  + Added item: {name}")

        self.stdout.write(self.style.SUCCESS("Universal Masters successfully seeded."))
