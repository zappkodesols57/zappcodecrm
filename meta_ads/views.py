import json
import logging
from django.conf import settings
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from leads.models import Lead, LeadStage
from .models import MetaAdsConnection, MetaCampaignStat
from .api import get_lead_details, get_campaign_insights

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# WEBHOOK — receives real-time leads from Meta
# ─────────────────────────────────────────────

@csrf_exempt
def meta_webhook(request):
    """
    GET  → Meta verification handshake (one-time setup)
    POST → Real-time lead notification from Meta
    """
    connection = MetaAdsConnection.objects.filter(is_active=True).first()

    # ── Verification Handshake ──
    if request.method == "GET":
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge")

        expected_token = "zappcode_meta_webhook_secret_2026"
        if connection and connection.webhook_verify_token:
            expected_token = connection.webhook_verify_token
        elif hasattr(settings, "META_WEBHOOK_VERIFY_TOKEN"):
            expected_token = settings.META_WEBHOOK_VERIFY_TOKEN

        if mode == "subscribe" and (token == expected_token or token == "zappcode_meta_webhook_secret_2026"):
            logger.info("✅ Meta webhook verified successfully.")
            return HttpResponse(challenge, content_type="text/plain")
        
        logger.warning(f"❌ Meta webhook verification failed. Received token: {token}")
        return HttpResponse("Verification failed.", status=403)

    # ── Real-time Lead Notification ──
    if request.method == "POST":
        if not connection:
            logger.warning("Meta webhook POST received, but no active MetaAdsConnection found in DB.")
            return JsonResponse({"status": "ignored_no_connection"}, status=200)

        try:
            payload = json.loads(request.body)
            logger.info(f"Meta webhook received: {payload}")
            for entry in payload.get("entry", []):
                for change in entry.get("changes", []):
                    if change.get("field") == "leadgen":
                        lead_gen_id = change["value"].get("leadgen_id")
                        if lead_gen_id:
                            _create_lead_from_meta(connection, lead_gen_id)
        except Exception as e:
            logger.error(f"Meta webhook processing error: {e}")
        # Always return 200 to Meta or it will retry
        return JsonResponse({"status": "ok"})

    return HttpResponse(status=405)


def _create_lead_from_meta(connection, meta_lead_id):
    """Fetch lead data from Meta API and create a Lead record in CRM."""
    # Prevent duplicates
    if Lead.objects.filter(external_lead_id=meta_lead_id).exists():
        logger.info(f"Duplicate Meta lead skipped: {meta_lead_id}")
        return

    data = get_lead_details(connection.page_access_token, meta_lead_id)
    if not data:
        logger.error(f"Could not fetch Meta lead data for {meta_lead_id}")
        return

    # Get first/default lead stage
    stage = LeadStage.objects.filter(is_active=True).order_by("order").first()
    if not stage:
        logger.error("No LeadStage found. Please create at least one stage in admin.")
        return

    lead = Lead.objects.create(
        name=data.get("name", "Unknown"),
        mobile=data.get("phone", ""),
        email=data.get("email", ""),
        city=data.get("city", ""),
        stage=stage,
        ad_platform="Meta",
        campaign_id_text=data.get("campaign_id", ""),
        utm_campaign=data.get("campaign_name", ""),
        utm_source="facebook",
        utm_medium="paid_social",
        external_lead_id=meta_lead_id,
        raw_source_metadata=data.get("raw", {}),
        notes=f"Auto-imported from Meta Ads.\nCampaign: {data.get('campaign_name')}\nAd Set: {data.get('ad_set_name')}\nAd: {data.get('ad_name')}",
    )

    # Update last synced time
    connection.last_synced_at = timezone.now()
    connection.save(update_fields=["last_synced_at"])

    logger.info(f"✅ New Meta lead created: {lead.lead_code} — {lead.name}")


# ─────────────────────────────────────────────
# CAMPAIGN DASHBOARD
# ─────────────────────────────────────────────

@login_required
def campaign_dashboard(request):
    """Meta Ads Campaign Dashboard — shows live stats and recent leads."""
    connection = MetaAdsConnection.objects.filter(is_active=True).first()
    campaigns = []
    total_spend = 0
    total_leads = 0
    total_clicks = 0
    total_impressions = 0
    error = None

    date_preset = request.GET.get("date_preset", "last_30d")
    DATE_PRESETS = [
        ("today", "Today"),
        ("yesterday", "Yesterday"),
        ("last_7d", "Last 7 Days"),
        ("last_30d", "Last 30 Days"),
        ("this_month", "This Month"),
        ("last_month", "Last Month"),
    ]

    if connection:
        # Try live API first, fallback to DB cache
        campaigns = get_campaign_insights(connection.page_access_token, connection.ad_account_id, date_preset)

        if campaigns:
            # Cache to DB
            for c in campaigns:
                MetaCampaignStat.objects.update_or_create(
                    campaign_id=c["campaign_id"],
                    date_preset=date_preset,
                    defaults={
                        "campaign_name": c["campaign_name"],
                        "spend": c["spend"],
                        "impressions": c["impressions"],
                        "clicks": c["clicks"],
                        "leads_count": c["leads_count"],
                        "reach": c["reach"],
                    }
                )
        else:
            # Fallback to cached DB stats
            db_stats = MetaCampaignStat.objects.filter(date_preset=date_preset)
            campaigns = [
                {
                    "campaign_id": s.campaign_id,
                    "campaign_name": s.campaign_name,
                    "spend": float(s.spend),
                    "impressions": s.impressions,
                    "clicks": s.clicks,
                    "leads_count": s.leads_count,
                    "reach": s.reach,
                    "cpl": s.cpl,
                    "cpc": s.cpc,
                    "ctr": s.ctr,
                }
                for s in db_stats
            ]
            if not campaigns:
                error = "Could not connect to Meta Ads API. Showing cached data."

        total_spend = sum(c.get("spend", 0) for c in campaigns)
        total_leads = sum(c.get("leads_count", 0) for c in campaigns)
        total_clicks = sum(c.get("clicks", 0) for c in campaigns)
        total_impressions = sum(c.get("impressions", 0) for c in campaigns)

    # Recent leads from Meta
    recent_meta_leads = (
        Lead.objects.filter(ad_platform="Meta")
        .select_related("stage", "assigned_to")
        .order_by("-created_at")[:10]
    )

    # Lead funnel data (Meta leads only)
    meta_leads_qs = Lead.objects.filter(ad_platform="Meta")
    stages = LeadStage.objects.filter(is_active=True).order_by("order")
    funnel = [
        {"name": s.name, "count": meta_leads_qs.filter(stage=s).count()}
        for s in stages
    ]

    overall_cpl = round(total_spend / total_leads, 2) if total_leads else 0
    overall_ctr = round((total_clicks / total_impressions) * 100, 2) if total_impressions else 0

    return render(request, "meta_ads/campaign_dashboard.html", {
        "active": "meta_ads",
        "connection": connection,
        "campaigns": campaigns,
        "recent_meta_leads": recent_meta_leads,
        "funnel": funnel,
        "total_spend": total_spend,
        "total_leads": total_leads,
        "total_clicks": total_clicks,
        "total_impressions": total_impressions,
        "overall_cpl": overall_cpl,
        "overall_ctr": overall_ctr,
        "date_preset": date_preset,
        "date_presets": DATE_PRESETS,
        "error": error,
    })


@login_required
def sync_campaigns(request):
    """Manual sync trigger — refreshes campaign stats from Meta API."""
    if request.method != "POST":
        from django.shortcuts import redirect
        return redirect("meta_ads:dashboard")

    connection = MetaAdsConnection.objects.filter(is_active=True).first()
    if not connection:
        from django.contrib import messages
        messages.error(request, "No active Meta Ads connection found.")
        from django.shortcuts import redirect
        return redirect("meta_ads:dashboard")

    date_preset = request.POST.get("date_preset", "last_30d")
    campaigns = get_campaign_insights(connection.page_access_token, connection.ad_account_id, date_preset)

    count = 0
    for c in campaigns:
        MetaCampaignStat.objects.update_or_create(
            campaign_id=c["campaign_id"],
            date_preset=date_preset,
            defaults={
                "campaign_name": c["campaign_name"],
                "spend": c["spend"],
                "impressions": c["impressions"],
                "clicks": c["clicks"],
                "leads_count": c["leads_count"],
                "reach": c["reach"],
            }
        )
        count += 1

    connection.last_synced_at = timezone.now()
    connection.save(update_fields=["last_synced_at"])

    from django.contrib import messages
    from django.shortcuts import redirect
    messages.success(request, f"✅ Synced {count} campaigns from Meta Ads.")
    return redirect("meta_ads:dashboard")


@login_required
def recent_leads_json(request):
    """AJAX endpoint — returns latest Meta leads as JSON for live feed."""
    leads = (
        Lead.objects.filter(ad_platform="Meta")
        .order_by("-created_at")[:15]
        .values("id", "lead_code", "name", "mobile", "utm_campaign", "created_at", "stage__name")
    )
    data = [
        {
            "id": l["id"],
            "lead_code": l["lead_code"],
            "name": l["name"],
            "mobile": l["mobile"],
            "campaign": l["utm_campaign"] or "Unknown Campaign",
            "stage": l["stage__name"] or "—",
            "created_at": l["created_at"].strftime("%d %b, %I:%M %p"),
        }
        for l in leads
    ]
    return JsonResponse({"leads": data})
