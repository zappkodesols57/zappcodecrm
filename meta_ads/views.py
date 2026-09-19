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

        valid_tokens = {
            "zappcode_meta_webhook_secret_2026",
            "zappcode_academy_verify_2026",
            "nelson_hospital_verify_2026",
        }
        if connection and connection.webhook_verify_token:
            valid_tokens.add(connection.webhook_verify_token)
        if hasattr(settings, "META_WEBHOOK_VERIFY_TOKEN"):
            valid_tokens.add(settings.META_WEBHOOK_VERIFY_TOKEN)

        if mode == "subscribe" and token in valid_tokens:
            logger.info(f"✅ Meta webhook verified successfully with token: {token}")
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


def create_or_update_meta_lead(connection, data):
    """Fetch lead data from Meta API and create a Lead record in CRM with course mapping & deduplication."""
    from leads.models import Lead, LeadStage, LeadSource, SourceCategory, Campaign, Course, LeadTemperature
    from accounts.models import Hospital

    meta_lead_id = data.get("meta_lead_id")
    if not meta_lead_id:
        return None

    # Prevent duplicate by external_lead_id or notes content
    from django.db.models import Q
    if Lead.objects.filter(Q(external_lead_id=meta_lead_id) | Q(notes__contains=meta_lead_id)).exists():
        logger.info(f"Duplicate Meta lead skipped (external_lead_id/notes): {meta_lead_id}")
        return None

    # Ensure mobile is sanitized and strictly within 20 chars (handles dummy Meta test leads and extended strings)
    mobile_raw = str(data.get("clean_mobile") or data.get("phone") or "").strip()
    digits_only = "".join(ch for ch in mobile_raw if ch.isdigit())
    if digits_only:
        mobile_num = digits_only[-15:]
    elif mobile_raw:
        mobile_num = mobile_raw[:20]
    else:
        mobile_num = "9999999999"

    # Also prevent duplicate if mobile matches and created recently
    if len(mobile_num) >= 10:
        clean_10 = mobile_num[-10:]
        if Lead.objects.filter(mobile__endswith=clean_10, ad_platform="Meta", created_at__gte=timezone.now() - timezone.timedelta(hours=24)).exists():
            logger.info(f"Duplicate Meta lead skipped (recent mobile): {clean_10}")
            return None

    # Hospital association -> Check connection or fallback to matching hospital
    conn_name = (getattr(connection, "name", "") or "").lower()
    if "nelson" in conn_name:
        hospital = Hospital.objects.filter(name__icontains="Nelson").first()
    elif getattr(connection, "hospital", None):
        hospital = connection.hospital
    elif "zappcode" in conn_name:
        hospital = Hospital.objects.filter(name__icontains="Zappcode").first()
    else:
        hospital = Hospital.objects.filter(name__icontains="Nelson").first() or Hospital.objects.first()

    # 1. Stage
    stage = LeadStage.objects.filter(name__iexact='New').first() or LeadStage.objects.filter(is_active=True).order_by("order").first()

    # 2. Source Category & Lead Source
    source_cat = SourceCategory.objects.filter(name__icontains="Digital").first() or \
                 SourceCategory.objects.filter(name__icontains="Social").first() or \
                 SourceCategory.objects.filter(name__icontains="Ads").first()
    if not source_cat:
        source_cat = SourceCategory.objects.create(name="Digital Marketing", order=1)

    lead_source = LeadSource.objects.filter(name__icontains="Meta").first() or \
                  LeadSource.objects.filter(name__icontains="Facebook").first()
    if not lead_source:
        lead_source = LeadSource.objects.create(name="Meta Ads", category=source_cat, order=1)

    # 3. Campaign
    campaign_name = (data.get("campaign_name") or data.get("form_name") or "Meta Leads Campaign").strip()
    campaign_obj = Campaign.objects.filter(name__iexact=campaign_name).first()
    if not campaign_obj and campaign_name:
        campaign_obj = Campaign.objects.create(
            name=campaign_name[:150],
            platform="FACEBOOK",
            campaign_id=data.get("campaign_id", "")[:150],
            hospital=hospital,
            is_active=True
        )

    # 4. Course Association
    course_obj = None
    course_name = data.get("course")
    if course_name:
        course_obj = Course.objects.filter(name__iexact=course_name, hospital=hospital).first() or \
                     Course.objects.filter(name__icontains=course_name).first()

    # 5. Inquiry date
    inq_date = timezone.localdate()
    c_time = data.get("created_time")
    if c_time:
        try:
            from datetime import datetime
            dt = datetime.strptime(c_time[:19], "%Y-%m-%dT%H:%M:%S")
            inq_date = dt.date()
        except Exception:
            pass

    notes_lines = [
        f"[Lead ID]: {meta_lead_id}",
        f"[Form Name]: {data.get('form_name', '')}",
    ]
    if course_name:
        notes_lines.append(f"[Course / Service]: {course_name}")
    if data.get("other_details"):
        notes_lines.append(" | ".join(data.get("other_details")))

    lead = Lead.objects.create(
        name=(data.get("name") or "Meta Lead")[:150],
        mobile=mobile_num[:20],
        email=(data.get("email") or "")[:254],
        city=(data.get("city") or "")[:100],
        location=(data.get("city") or "")[:255],
        course=course_obj,
        hospital=hospital,
        stage=stage,
        temperature=LeadTemperature.HOT,
        source_category=source_cat,
        lead_source=lead_source,
        original_lead_source=lead_source,
        original_source_category=source_cat,
        campaign=campaign_obj,
        original_campaign=campaign_obj,
        ad_platform="Meta",
        campaign_id_text=data.get("campaign_id", ""),
        utm_campaign=campaign_name,
        utm_source="facebook",
        utm_medium="paid_social",
        external_lead_id=meta_lead_id,
        inquiry_date=inq_date,
        raw_source_metadata=data.get("raw", {}),
        custom_data={"priority": "Hot", "lead_source": "Meta Ads"},
        notes="\n".join(notes_lines),
    )

    logger.info(f"✅ New Meta lead created: {lead.lead_code} — {lead.name} ({lead.course})")

    # Generate In-app Notification for Admins, Managers, and Counsellors
    try:
        from notifications.models import Notification
        from accounts.models import User
        from django.urls import reverse
        from django.db.models import Q

        notify_users = User.objects.filter(
            Q(is_superuser=True) |
            Q(role__in=['SUPER_ADMIN', 'ADMIN', 'MANAGER', 'COUNSELLOR', 'LEAD_ATTENDENT'])
        ).filter(
            Q(hospital=hospital) | Q(hospital__isnull=True) | Q(is_superuser=True)
        ).distinct()

        link = reverse('leads:lead_detail', args=[lead.pk])
        course_display = f" for {lead.course.name}" if lead.course else ""
        for u in notify_users:
            Notification.objects.create(
                user=u,
                title="New Meta Lead Captured",
                message=f"Lead {lead.name} ({lead.mobile}){course_display} arrived from Meta Ads.",
                link=link
            )
    except Exception as e:
        logger.error(f"Failed to create notification for lead {lead.lead_code}: {e}")

    return lead


def _create_lead_from_meta(connection, meta_lead_id):
    """Fetch lead data from Meta API and create a Lead record in CRM."""
    data = get_lead_details(connection.page_access_token, meta_lead_id)
    if not data:
        logger.error(f"Could not fetch Meta lead data for {meta_lead_id}")
        return None

    lead = create_or_update_meta_lead(connection, data)
    if lead:
        connection.last_synced_at = timezone.now()
        connection.save(update_fields=["last_synced_at"])
    return lead


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

    # Filter Meta leads by hospital:
    # Only show leads that belong to the current user's hospital (or the connection's hospital)
    user_hospital = getattr(request.user, "hospital", None)
    lead_filter = {"ad_platform": "Meta"}
    if user_hospital:
        lead_filter["hospital"] = user_hospital
    elif connection and hasattr(connection, "hospital") and connection.hospital:
        lead_filter["hospital"] = connection.hospital
    elif connection and "nelson" in (connection.name or "").lower():
        from accounts.models import Hospital
        nelson_hosp = Hospital.objects.filter(name__icontains="Nelson").first()
        if nelson_hosp:
            lead_filter["hospital"] = nelson_hosp

    # Recent leads from Meta
    recent_meta_leads = (
        Lead.objects.filter(**lead_filter)
        .select_related("stage", "assigned_to")
        .order_by("-created_at")[:10]
    )

    # Lead funnel data (Meta leads only for this hospital)
    meta_leads_qs = Lead.objects.filter(**lead_filter)
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
    user_hospital = getattr(request.user, "hospital", None)
    lead_filter = {"ad_platform": "Meta"}
    if user_hospital:
        lead_filter["hospital"] = user_hospital
    else:
        connection = MetaAdsConnection.objects.filter(is_active=True).first()
        if connection and "nelson" in (connection.name or "").lower():
            from accounts.models import Hospital
            nelson_hosp = Hospital.objects.filter(name__icontains="Nelson").first()
            if nelson_hosp:
                lead_filter["hospital"] = nelson_hosp

    leads = (
        Lead.objects.filter(**lead_filter)
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


@login_required
def sync_leads_now(request):
    """Manual trigger to fetch new leads from all Meta forms and import into CRM."""
    from django.contrib import messages
    from django.shortcuts import redirect
    from .api import fetch_all_form_leads

    if request.method != "POST":
        return redirect("meta_ads:dashboard")

    connection = MetaAdsConnection.objects.filter(is_active=True).first()
    if not connection:
        messages.error(request, "❌ No active Meta Ads connection found.")
        return redirect("meta_ads:dashboard")

    if not connection.page_access_token or not connection.page_id:
        messages.error(request, "❌ Missing Page Access Token or Page ID in Meta connection.")
        return redirect("meta_ads:dashboard")

    try:
        leads_data = fetch_all_form_leads(connection.page_access_token, connection.page_id, limit_per_form=100)
        created_count = 0
        for item in leads_data:
            lead = create_or_update_meta_lead(connection, item)
            if lead:
                created_count += 1

        connection.last_synced_at = timezone.now()
        connection.save(update_fields=["last_synced_at"])

        if created_count > 0:
            messages.success(request, f"🎉 Successfully imported {created_count} fresh lead(s) from Meta into CRM!")
        else:
            messages.info(request, "✅ Meta leads are already up to date. No new leads found.")
    except Exception as e:
        logger.error(f"Error in sync_leads_now: {e}")
        messages.error(request, f"Error syncing leads from Meta: {str(e)}")

    return redirect("meta_ads:dashboard")


@login_required
def update_meta_token(request):
    """
    Allow Nelson Admin / authorized managers to update the Meta Page Access Token
    (temporary/short-lived or long-lived) directly from the Meta Ads page.
    """
    from django.contrib import messages
    from django.shortcuts import redirect

    if request.method != "POST":
        return redirect("meta_ads:dashboard")

    # Check permission (Nelson Admin / Manager or Superadmin)
    if not (getattr(request.user, "can_manage_campaigns", False) or request.user.is_superuser or request.user.role in ["ADMIN", "MANAGER", "SUPER_ADMIN"]):
        messages.error(request, "Permission denied to update Meta configuration.")
        return redirect("meta_ads:dashboard")

    new_token = request.POST.get("page_access_token", "").strip()
    ad_account_id = request.POST.get("ad_account_id", "").strip()
    page_id = request.POST.get("page_id", "").strip()
    verify_token = request.POST.get("webhook_verify_token", "").strip()

    if not new_token:
        messages.error(request, "❌ Token cannot be empty.")
        return redirect("meta_ads:dashboard")

    connection = MetaAdsConnection.objects.filter(is_active=True).first()
    if not connection:
        # Create connection for Nelson Hospital if not existing
        connection = MetaAdsConnection.objects.create(
            name="Nelson Hospital",
            page_access_token=new_token,
            ad_account_id=ad_account_id or "",
            page_id=page_id or "",
            webhook_verify_token=verify_token or "nelson_hospital_verify_2026",
            is_active=True
        )
        messages.success(request, "✅ Meta Ads Connection created and token updated successfully!")
    else:
        connection.page_access_token = new_token
        if ad_account_id:
            connection.ad_account_id = ad_account_id.replace("act_", "").strip()
        if page_id:
            connection.page_id = page_id
        if verify_token:
            connection.webhook_verify_token = verify_token
        connection.save()
        messages.success(request, "✅ Meta Page Access Token updated successfully!")

    return redirect("meta_ads:dashboard")

