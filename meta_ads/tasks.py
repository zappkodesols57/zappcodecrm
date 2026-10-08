import logging
from celery import shared_task
from django.utils import timezone
from meta_ads.models import MetaAdsConnection
from meta_ads.api import fetch_all_form_leads
from meta_ads.views import create_or_update_meta_lead
from leads.models import Lead
from django.db.models import Q

logger = logging.getLogger(__name__)


@shared_task(name="meta_ads.tasks.sync_meta_leads_task")
def sync_meta_leads_task(limit=50):
    """
    Periodic Celery background task to pull new leads from Meta Lead Ads.
    Scheduled via Celery Beat (e.g. every 30 seconds for test / 10 mins for prod).
    """
    logger.info("Starting Celery Meta Leads Sync task...")
    
    connection = MetaAdsConnection.objects.filter(is_active=True).first()
    if not connection or not connection.page_access_token or not connection.page_id:
        logger.warning("[!] Celery Meta Sync skipped: No active MetaAdsConnection or missing token/page_id.")
        return {"status": "skipped", "reason": "no_active_connection"}

    try:
        leads_data = fetch_all_form_leads(connection.page_access_token, connection.page_id, limit_per_form=limit)
    except Exception as e:
        logger.error(f"[!] Failed to fetch leads from Meta API in Celery task: {e}")
        return {"status": "error", "error": str(e)}

    created_count = 0
    skipped_count = 0

    for item in leads_data:
        meta_id = item.get("meta_lead_id")
        if Lead.objects.filter(Q(external_lead_id=meta_id) | Q(notes__contains=meta_id)).exists():
            skipped_count += 1
            continue

        try:
            lead = create_or_update_meta_lead(connection, item)
            if lead:
                created_count += 1
            else:
                skipped_count += 1
        except Exception as err:
            logger.error(f"Error creating lead from Meta item {meta_id}: {err}")
            skipped_count += 1

    connection.last_synced_at = timezone.now()
    connection.save(update_fields=["last_synced_at"])

    result_msg = f"[OK] Celery Meta Sync Complete: {created_count} created, {skipped_count} existing/skipped."
    logger.info(result_msg)
    return {"status": "ok", "created": created_count, "skipped": skipped_count}

