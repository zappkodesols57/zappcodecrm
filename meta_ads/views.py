import json
import logging
from django.conf import settings
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from django.db.models import Q, Count, Sum
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

@csrf_exempt
def justdial_webhook(request):
    """
    Webhook endpoint to ingest real-time patient inquiries from Justdial.
    Supports GET handshake and POST inquiry payload.
    """
    from accounts.models import Hospital
    from leads.models import Lead, LeadSource, SourceCategory, LeadTemperature

    if request.method == "GET":
        return HttpResponse("Justdial Webhook Endpoint Active", content_type="text/plain")

    if request.method == "POST":
        try:
            payload = json.loads(request.body) if request.body else request.POST.dict()
            name = payload.get("name") or payload.get("caller_name") or payload.get("lead_name") or "Justdial Patient"
            phone = payload.get("mobile") or payload.get("phone") or payload.get("caller_phone") or ""
            department = payload.get("department") or payload.get("category") or payload.get("speciality") or "General Medicine"
            city = payload.get("city") or "Nagpur"
            notes = payload.get("notes") or payload.get("query") or "Inquiry received via Justdial integration."

            clean_phone = "".join(filter(str.isdigit, str(phone)))[-10:] if phone else ""
            nelson_hosp = Hospital.objects.filter(name__icontains="Nelson").first()
            source_obj = LeadSource.objects.filter(name__iexact="Just Dial").first() or LeadSource.objects.filter(name__icontains="Justdial").first()
            cat_obj = SourceCategory.objects.filter(name__icontains="Aggregator").first() or SourceCategory.objects.filter(name__icontains="Digital").first()

            lead = Lead.objects.create(
                name=name,
                mobile=clean_phone,
                city=city,
                hospital=nelson_hosp,
                lead_source=source_obj,
                source_category=cat_obj,
                temperature=LeadTemperature.HOT,
                ad_platform="Justdial",
                inquiry_date=timezone.localdate(),
                custom_data={
                    "department": department,
                    "lead_source": "Just Dial",
                    "channel": "Justdial Healthcare Feed"
                },
                notes=notes
            )
            logger.info(f"✅ Justdial Lead Captured: {lead.lead_code} - {lead.name} ({department})")
            return JsonResponse({"status": "success", "lead_code": lead.lead_code}, status=200)
        except Exception as e:
            logger.error(f"Error ingesting Justdial lead: {e}")
            return JsonResponse({"status": "error", "message": str(e)}, status=400)
    return HttpResponse(status=405)


@csrf_exempt
def practo_webhook(request):
    """
    Webhook endpoint to ingest real-time appointment bookings and doctor inquiries from Practo.
    """
    from accounts.models import Hospital
    from leads.models import Lead, LeadSource, SourceCategory, LeadTemperature

    if request.method == "GET":
        return HttpResponse("Practo Integration Webhook Active", content_type="text/plain")

    if request.method == "POST":
        try:
            payload = json.loads(request.body) if request.body else request.POST.dict()
            name = payload.get("patient_name") or payload.get("name") or "Practo Patient"
            phone = payload.get("phone") or payload.get("mobile") or ""
            doctor = payload.get("doctor_name") or payload.get("doctor") or ""
            department = payload.get("department") or payload.get("speciality") or "Pediatrics"
            notes = payload.get("notes") or f"Practo consultation request for Dr. {doctor}"

            clean_phone = "".join(filter(str.isdigit, str(phone)))[-10:] if phone else ""
            nelson_hosp = Hospital.objects.filter(name__icontains="Nelson").first()
            source_obj = LeadSource.objects.filter(name__iexact="PRACTO").first() or LeadSource.objects.filter(name__icontains="Practo").first()
            cat_obj = SourceCategory.objects.filter(name__icontains="Healthcare").first() or SourceCategory.objects.filter(name__icontains="Digital").first()

            lead = Lead.objects.create(
                name=name,
                mobile=clean_phone,
                city="Nagpur",
                hospital=nelson_hosp,
                lead_source=source_obj,
                source_category=cat_obj,
                temperature=LeadTemperature.HOT,
                ad_platform="Practo",
                inquiry_date=timezone.localdate(),
                custom_data={
                    "doctor": doctor,
                    "department": department,
                    "lead_source": "Practo",
                    "channel": "Practo Direct Consult"
                },
                notes=notes
            )
            logger.info(f"✅ Practo Lead Captured: {lead.lead_code} - {lead.name} ({doctor})")
            return JsonResponse({"status": "success", "lead_code": lead.lead_code}, status=200)
        except Exception as e:
            logger.error(f"Error ingesting Practo lead: {e}")
            return JsonResponse({"status": "error", "message": str(e)}, status=400)
    return HttpResponse(status=405)


# ─────────────────────────────────────────────
# CAMPAIGN DASHBOARD
# ─────────────────────────────────────────────

@login_required
def campaign_dashboard(request):
    """
    Marketing & Ads Dashboard:
    - Multi-tenant Aware (Nelson Hospital Dhantoli vs Zappcode Academy).
    - Under Nelson Hospital, provides Meta Ads, Justdial, and Practo Channels.
    - Preserves existing active Meta API connection and credentials without changes.
    """
    from accounts.models import Hospital
    from leads.models import Lead, LeadStage

    # 1. Identify all active businesses and active selection
    all_hospitals = list(Hospital.objects.filter(is_active=True).order_by("name"))
    
    # Priority: GET param > Session active_business_id > User's hospital
    req_biz = request.GET.get("business", "").strip()
    if not req_biz:
        req_biz = str(request.session.get("active_business_id", "")).strip()

    active_business = None
    if req_biz and req_biz.isdigit():
        active_business = Hospital.objects.filter(id=int(req_biz), is_active=True).first()
    elif request.user.hospital:
        active_business = request.user.hospital

    # Determine Active Entity Mode: 'nelson' or 'academy'
    # If no specific business selected (Global All), default active tab to Nelson or GET param
    selected_entity = request.GET.get("entity", "").strip().lower()
    if not selected_entity:
        if active_business:
            if "nelson" in active_business.name.lower() or "hospital" in active_business.name.lower():
                selected_entity = "nelson"
            else:
                selected_entity = "academy"
        else:
            selected_entity = "nelson"

    # Nelson sub-channel: 'meta', 'justdial', 'practo'
    active_channel = request.GET.get("channel", "meta").strip().lower()
    if active_channel not in ("meta", "justdial", "practo"):
        active_channel = "meta"

    # Separate Meta connection resolution per business
    if selected_entity == "nelson":
        connection = MetaAdsConnection.objects.filter(is_active=True, name__icontains="Nelson").first()
    else:
        connection = MetaAdsConnection.objects.filter(is_active=True, name__icontains="Zappcode").first()
        if not connection:
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

    if connection and connection.page_access_token:
        campaigns = get_campaign_insights(connection.page_access_token, connection.ad_account_id, date_preset)
        if campaigns:
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
            if not campaigns and selected_entity != "nelson":
                error = "Could not connect to Meta Ads API. Showing cached data."

        total_spend = sum(c.get("spend", 0) for c in campaigns)
        total_leads = sum(c.get("leads_count", 0) for c in campaigns)
        total_clicks = sum(c.get("clicks", 0) for c in campaigns)
        total_impressions = sum(c.get("impressions", 0) for c in campaigns)

    # Date filter logic for leads (using actual lead generation date `inquiry_date` from master/campaign data)
    now = timezone.now()
    today_date = now.date()
    
    date_filter = Q()
    if date_preset == "today":
        date_filter = Q(inquiry_date=today_date)
    elif date_preset == "yesterday":
        yest_date = today_date - timezone.timedelta(days=1)
        date_filter = Q(inquiry_date=yest_date)
    elif date_preset == "last_7d":
        date_filter = Q(inquiry_date__gte=today_date - timezone.timedelta(days=7))
    elif date_preset == "last_30d":
        date_filter = Q(inquiry_date__gte=today_date - timezone.timedelta(days=30))
    elif date_preset == "this_month":
        month_start_date = today_date.replace(day=1)
        date_filter = Q(inquiry_date__gte=month_start_date)
    elif date_preset == "last_month":
        first_of_this_month = today_date.replace(day=1)
        last_month_end = first_of_this_month - timezone.timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)
        date_filter = Q(inquiry_date__gte=last_month_start, inquiry_date__lte=last_month_end)

    # 2. Build Querysets for Nelson vs Academy and Channels
    nelson_hosp = Hospital.objects.filter(name__icontains="Nelson").first()
    academy_hosp = Hospital.objects.filter(name__icontains="Zappcode").first()

    # Nelson Meta Leads — Strictly Meta Ads Leads for Nelson
    nelson_meta_leads_qs = Lead.objects.filter(is_archived=False).filter(
        Q(hospital=nelson_hosp) | Q(custom_data__hospital_branch__icontains="Nelson")
    ).filter(
        Q(ad_platform="Meta") | Q(lead_source__name__icontains="Meta") | Q(lead_source__name__icontains="Facebook")
    )
    if date_filter:
        nelson_meta_leads_qs = nelson_meta_leads_qs.filter(date_filter)

    # Nelson Justdial Leads
    nelson_justdial_leads_qs = Lead.objects.filter(is_archived=False).filter(
        Q(hospital=nelson_hosp) | Q(custom_data__hospital_branch__icontains="Nelson")
    ).filter(
        Q(ad_platform="Justdial") | Q(lead_source__name__icontains="Just Dial") | Q(lead_source__name__icontains="Justdial") | Q(custom_data__lead_source__icontains="Just Dial")
    )
    if date_filter:
        nelson_justdial_leads_qs = nelson_justdial_leads_qs.filter(date_filter)

    # Nelson Practo Leads
    nelson_practo_leads_qs = Lead.objects.filter(is_archived=False).filter(
        Q(hospital=nelson_hosp) | Q(custom_data__hospital_branch__icontains="Nelson")
    ).filter(
        Q(ad_platform="Practo") | Q(lead_source__name__icontains="Practo") | Q(custom_data__lead_source__icontains="Practo")
    )
    if date_filter:
        nelson_practo_leads_qs = nelson_practo_leads_qs.filter(date_filter)

    # Zappcode Academy Leads — Strictly Meta API Ingested Leads for Zappcode Academy
    academy_leads_qs = Lead.objects.filter(is_archived=False).filter(
        Q(hospital=academy_hosp) | Q(custom_data__hospital_branch__icontains="Zappcode")
    ).filter(ad_platform="Meta")
    if date_filter:
        academy_leads_qs = academy_leads_qs.filter(date_filter)

    # Pick active list of leads based on selected entity & channel
    if selected_entity == "nelson":
        if active_channel == "justdial":
            active_channel_leads = nelson_justdial_leads_qs
            channel_name_display = "Justdial Health Inquiries"
        elif active_channel == "practo":
            active_channel_leads = nelson_practo_leads_qs
            channel_name_display = "Practo Doctor Consultations"
        else:
            active_channel_leads = nelson_meta_leads_qs
            channel_name_display = "Nelson Meta Ads (Facebook & Instagram)"
    else:
        active_channel_leads = academy_leads_qs
        channel_name_display = "Zappcode Academy Meta Ads"

    # Recent Leads for Table (Ordered by lead inquiry_date and id, scrollable feed)
    recent_channel_leads = active_channel_leads.select_related("stage", "assigned_to", "course", "lead_source").order_by("-inquiry_date", "-id")[:50]

    # Funnel Stats
    stages = LeadStage.objects.filter(is_active=True).order_by("order")
    funnel = [
        {"name": s.name, "count": active_channel_leads.filter(stage=s).count()}
        for s in stages
    ]

    # Metric counts for cards
    channel_total_count = active_channel_leads.count()
    channel_converted_count = active_channel_leads.filter(
        Q(deal_status="WON") | Q(admission_status="ADMISSION_DONE") | Q(stage__name__icontains="Admission") | Q(stage__name__icontains="Payment") | Q(stage__name__icontains="Converted")
    ).count()
    channel_conv_rate = round((channel_converted_count / channel_total_count * 100), 1) if channel_total_count else 0.0

    # Lead Volume Chart Timeline Calculation (Using actual inquiry_date)
    chart_labels = []
    chart_values = []
    if date_preset == "today":
        # Today hourly breakdown (if leads have timestamp) or daily comparison
        today_leads = active_channel_leads.filter(inquiry_date=today_date)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timezone.timedelta(days=1)
        for h in range(8, 22, 2):
            chart_labels.append(f"{h:02d}:00")
            h_start = today_start.replace(hour=h)
            h_end = today_start.replace(hour=h+2) if h < 20 else today_end
            chart_values.append(today_leads.filter(created_at__gte=h_start, created_at__lt=h_end).count())
    else:
        # Day-by-day breakdown of generated leads
        days_count = 7 if date_preset in ("last_7d", "yesterday") else 14
        for i in range(days_count - 1, -1, -1):
            target_date = today_date - timezone.timedelta(days=i)
            chart_labels.append(target_date.strftime("%d %b"))
            chart_values.append(active_channel_leads.filter(inquiry_date=target_date).count())

    overall_cpl = round(total_spend / total_leads, 2) if total_leads else 0
    overall_ctr = round((total_clicks / total_impressions) * 100, 2) if total_impressions else 0

    return render(request, "meta_ads/campaign_dashboard.html", {
        "active": "meta_ads",
        "all_hospitals": all_hospitals,
        "active_business": active_business,
        "selected_entity": selected_entity,
        "active_channel": active_channel,
        "channel_name_display": channel_name_display,
        "connection": connection,
        "campaigns": campaigns,
        "recent_meta_leads": recent_channel_leads,
        "funnel": funnel,
        "channel_total_count": channel_total_count,
        "channel_converted_count": channel_converted_count,
        "channel_conv_rate": channel_conv_rate,
        "nelson_meta_count": nelson_meta_leads_qs.count(),
        "nelson_justdial_count": nelson_justdial_leads_qs.count(),
        "nelson_practo_count": nelson_practo_leads_qs.count(),
        "academy_leads_count": academy_leads_qs.count(),
        "total_spend": total_spend,
        "total_leads": total_leads,
        "total_clicks": total_clicks,
        "total_impressions": total_impressions,
        "overall_cpl": overall_cpl,
        "overall_ctr": overall_ctr,
        "chart_labels_json": json.dumps(chart_labels),
        "chart_values_json": json.dumps(chart_values),
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

    selected_entity = request.POST.get("entity", "").strip().lower()
    conn_name = "Nelson Hospital" if selected_entity == "nelson" else "Zappcode Academy"
    default_verify = "nelson_hospital_dhantoli_2026" if selected_entity == "nelson" else "zappcode_academy_verify_2026"

    connection = MetaAdsConnection.objects.filter(is_active=True, name__icontains=("Nelson" if selected_entity == "nelson" else "Zappcode")).first()
    if not connection:
        # Create separate connection for this specific business
        connection = MetaAdsConnection.objects.create(
            name=conn_name,
            page_access_token=new_token,
            ad_account_id=ad_account_id.replace("act_", "").strip() if ad_account_id else "",
            page_id=page_id or "",
            webhook_verify_token=verify_token or default_verify,
            is_active=True
        )
        messages.success(request, f"✅ Meta Ads Connection created for {conn_name} successfully!")
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

