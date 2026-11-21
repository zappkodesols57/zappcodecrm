import logging
from django.core.management.base import BaseCommand
from django.utils import timezone
from meta_ads.models import MetaAdsConnection
from meta_ads.api import fetch_all_form_leads
from meta_ads.views import create_or_update_meta_lead

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Fetch and sync recent leads from Meta Lead Ads into CRM business-wise (for daily cron job or manual trigger)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--business",
            type=int,
            default=None,
            help="Specific Business ID / Hospital ID to sync (e.g. --business 7)",
        )
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
        target_business_id = options.get("business")
        limit = options.get("limit", 100)
        dry_run = options.get("dry_run", False)

        now_str = timezone.now().strftime("%Y-%m-%d %H:%M:%S")
        self.stdout.write(self.style.NOTICE(f"[{now_str}] Starting Meta Leads Sync..."))

        connections_qs = MetaAdsConnection.objects.filter(is_active=True)
        if target_business_id:
            connections_qs = connections_qs.filter(hospital__id=target_business_id)

        if not connections_qs.exists():
            self.stdout.write(self.style.ERROR(
                f"[!] No active MetaAdsConnection found" + (f" for business ID {target_business_id}" if target_business_id else ".")
            ))
            return

        total_created = 0
        total_skipped = 0

        for connection in connections_qs:
            biz_name = connection.hospital.name if connection.hospital else connection.name
            biz_id = connection.business_id or "Unassigned"
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n--> Syncing Meta Connection: '{connection.name}' [Business ID: {biz_id} - {biz_name}]"
            ))

            if not connection.page_access_token or not connection.page_id:
                self.stdout.write(self.style.WARNING(f"  [!] Missing Page Access Token or Page ID for '{connection.name}'. Skipping."))
                continue

            try:
                leads_data = fetch_all_form_leads(
                    connection.page_access_token,
                    connection.page_id,
                    max_forms=100,
                    limit_per_form=limit
                )
            except Exception as api_err:
                self.stdout.write(self.style.ERROR(f"  [!] Error fetching forms/leads for {connection.name}: {api_err}"))
                continue

            self.stdout.write(f"  Found {len(leads_data)} lead records across Meta forms.")

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
                    self.stdout.write(f"    [Dry-run] Would create: {name_safe} | {phone_safe} | {course_safe}")
                    created_count += 1
                else:
                    lead = create_or_update_meta_lead(connection, item)
                    if lead:
                        created_count += 1
                        lead_name_safe = str(lead.name).encode("ascii", "replace").decode("ascii")
                        lead_course_safe = str(lead.course.name if lead.course else "General").encode("ascii", "replace").decode("ascii")
                        self.stdout.write(self.style.SUCCESS(f"    [+] Created: {lead.lead_code} - {lead_name_safe} ({lead_course_safe})"))
                    else:
                        skipped_count += 1

            if not dry_run:
                connection.last_synced_at = timezone.now()
                connection.save(update_fields=["last_synced_at"])

            self.stdout.write(self.style.SUCCESS(
                f"  Sync finished for {biz_name}: {created_count} imported, {skipped_count} skipped/already exist."
            ))
            total_created += created_count
            total_skipped += skipped_count

        self.stdout.write(self.style.SUCCESS(
            f"\n[DONE] Overall Meta Leads Sync Complete: {total_created} newly imported, {total_skipped} skipped."
        ))

