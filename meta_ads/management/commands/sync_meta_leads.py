import logging
from django.core.management.base import BaseCommand
from django.utils import timezone
from meta_ads.models import MetaAdsConnection
from meta_ads.api import fetch_all_form_leads
from meta_ads.views import create_or_update_meta_lead

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Fetch and sync recent leads from Meta Lead Ads into CRM (for daily 9 AM cron job or manual trigger)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Maximum leads to fetch per active form (default: 100)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Check for new leads without actually creating records in database",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        dry_run = options["dry_run"]

        now_str = timezone.now().strftime("%Y-%m-%d %H:%M:%S")
        self.stdout.write(self.style.NOTICE(f"[{now_str}] Starting Meta Leads Sync for Zappcode Academy..."))

        connection = MetaAdsConnection.objects.filter(is_active=True).first()
        if not connection:
            self.stdout.write(self.style.ERROR("[!] No active MetaAdsConnection found in database."))
            return

        if not connection.page_access_token or not connection.page_id:
            self.stdout.write(self.style.ERROR("[!] Missing Page Access Token or Page ID on Meta connection."))
            return

        leads_data = fetch_all_form_leads(connection.page_access_token, connection.page_id, limit_per_form=limit)
        self.stdout.write(f"Found {len(leads_data)} valid lead records across Meta forms.")

        created_count = 0
        skipped_count = 0

        for item in leads_data:
            meta_id = item.get("meta_lead_id")
            name_safe = str(item.get("name", "")).encode("ascii", "replace").decode("ascii")
            course_safe = str(item.get("course", "")).encode("ascii", "replace").decode("ascii")
            phone_safe = str(item.get("phone", ""))

            from leads.models import Lead
            from django.db.models import Q
            is_duplicate = Lead.objects.filter(Q(external_lead_id=meta_id) | Q(notes__contains=meta_id)).exists()

            if is_duplicate:
                skipped_count += 1
                continue

            if dry_run:
                self.stdout.write(f"  [Dry-run] Would create: {name_safe} | {phone_safe} | {course_safe}")
                created_count += 1
            else:
                lead = create_or_update_meta_lead(connection, item)
                if lead:
                    created_count += 1
                    lead_name_safe = str(lead.name).encode("ascii", "replace").decode("ascii")
                    lead_course_safe = str(lead.course.name if lead.course else "General").encode("ascii", "replace").decode("ascii")
                    self.stdout.write(self.style.SUCCESS(f"  [+] Created: {lead.lead_code} - {lead_name_safe} ({lead_course_safe})"))
                else:
                    skipped_count += 1

        if not dry_run:
            connection.last_synced_at = timezone.now()
            connection.save(update_fields=["last_synced_at"])

        self.stdout.write(self.style.SUCCESS(
            f"[OK] Meta Leads Sync Complete: {created_count} newly imported, {skipped_count} existing/skipped."
        ))
