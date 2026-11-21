import json
import logging
from datetime import datetime, date, timedelta
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
def meta_webhook(request, business_id=None):
    """
    Meta (Facebook/Instagram) Lead Ads Webhook Endpoint.
    GET  → Meta verification handshake (one-time setup)
    POST → Real-time lead notification from Meta
    Supports dedicated per-business endpoints: /meta-ads/webhook/<business_id>/
    as well as global multi-tenant endpoint: /meta-ads/webhook/
    """
    import hmac
    import hashlib

    connection = None
    if business_id:
        connection = MetaAdsConnection.objects.filter(is_active=True, hospital__id=business_id).first()
    if not connection:
        v_token = request.GET.get("hub.verify_token")
        if v_token:
            connection = MetaAdsConnection.objects.filter(is_active=True, webhook_verify_token=v_token).first()
    if not connection:
        # Default global active connection fallback
        connection = (
            MetaAdsConnection.objects.filter(is_active=True, hospital__id=7).first()
            or MetaAdsConnection.objects.filter(is_active=True, name__icontains="academy").first()
            or MetaAdsConnection.objects.filter(is_active=True).first()
        )

    # ── 1. Verification Handshake (GET) ──
    if request.method == "GET":
        mode = request.GET.get("hub.mode")
        received_token = request.GET.get("hub.verify_token", "")
        challenge = request.GET.get("hub.challenge", "")

        valid_tokens = {
            "zappcode_meta_webhook_secret_2026",
            "zappcode_academy_verify_2026",
            "nelson_hospital_verify_2026",
        }
        if connection and connection.webhook_verify_token:
            valid_tokens.add(connection.webhook_verify_token)
        if hasattr(settings, "META_WEBHOOK_VERIFY_TOKEN"):
            valid_tokens.add(settings.META_WEBHOOK_VERIFY_TOKEN)

        # Timing-attack safe comparison using hmac.compare_digest
        is_token_valid = any(hmac.compare_digest(vt, received_token) for vt in valid_tokens if vt)

        if mode == "subscribe" and is_token_valid:
            logger.info(f"✅ Meta webhook verified successfully with token: {received_token}")
            return HttpResponse(challenge, content_type="text/plain")
        
        logger.warning(f"❌ Meta webhook verification failed. Received token: {received_token}")
        return HttpResponse("Verification failed.", status=403)

    # ── 2. Real-time Lead Notification (POST) ──
    if request.method == "POST":
        try:
            # Payload Signature Check (if app_secret is configured on connection)
            signature_header = request.headers.get("X-Hub-Signature-256")
            if signature_header and connection and connection.app_secret:
                try:
                    hash_method, received_signature = signature_header.split("=", 1)
                    if hash_method == "sha256":
                        expected_hash = hmac.new(
                            connection.app_secret.encode("utf-8"),
                            msg=request.body,
                            digestmod=hashlib.sha256
                        ).hexdigest()
                        if not hmac.compare_digest(expected_hash, received_signature):
                            logger.warning(f"⚠️ Meta webhook signature mismatch for connection {connection.name}!")
                except Exception as sig_err:
                    logger.warning(f"⚠️ Error parsing webhook signature: {sig_err}")

            payload = json.loads(request.body)
            logger.info(f"Meta webhook received: {payload}")
            for entry in payload.get("entry", []):
                # Detect page_id from entry level (standard Meta webhook payload)
                entry_page_id = str(entry.get("id", "")).strip()

                for change in entry.get("changes", []):
                    if change.get("field") == "leadgen":
                        val = change.get("value", {})
                        lead_gen_id = val.get("leadgen_id")
                        if not lead_gen_id:
                            continue

                        # Precise connection resolution:
                        # 1. By payload page_id (from value or entry)
                        val_page_id = str(val.get("page_id") or entry_page_id).strip()
                        target_conn = None
                        if val_page_id:
                            target_conn = MetaAdsConnection.objects.filter(is_active=True, page_id=val_page_id).first()

                        # 2. If business_id is in webhook URL path (/meta-ads/webhook/<business_id>/)
                        if not target_conn and business_id:
                            target_conn = MetaAdsConnection.objects.filter(is_active=True, hospital__id=business_id).first()

                        # 3. By form_id from MetaLeadForm table
                        val_form_id = str(val.get("form_id", "")).strip()
                        if not target_conn and val_form_id:
                            form_rec = MetaLeadForm.objects.filter(form_id=val_form_id, connection__is_active=True).select_related('connection').first()
                            if form_rec:
                                target_conn = form_rec.connection

                        # 4. Fallback to default active connection
                        if not target_conn:
                            target_conn = connection or MetaAdsConnection.objects.filter(is_active=True).first()

                        if target_conn:
                            _create_lead_from_meta(target_conn, lead_gen_id)
                        else:
                            logger.warning(f"Could not resolve active MetaAdsConnection for lead {lead_gen_id} (Page: {val_page_id})")
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

    # Ensure mobile is sanitized, country codes handled, and clean 10 digits prioritized
    mobile_raw = str(data.get("clean_mobile") or data.get("phone") or "").strip()
    digits_only = "".join(ch for ch in mobile_raw if ch.isdigit())
    if len(digits_only) == 12 and digits_only.startswith("91"):
        mobile_num = digits_only[2:]
    elif len(digits_only) == 13 and digits_only.startswith("091"):
        mobile_num = digits_only[3:]
    elif len(digits_only) == 11 and digits_only.startswith("0"):
        mobile_num = digits_only[1:]
    elif len(digits_only) >= 10:
        mobile_num = digits_only[-10:]
    elif digits_only:
        mobile_num = digits_only
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

    # Business association -> Strictly bind to connection's assigned business (ID-level tenant isolation)
    from accounts.models import Business, Hospital
    business = getattr(connection, "hospital", None) or getattr(connection, "business", None)
    if not business:
        # If connection has no hospital linked, check connection name or default to Academy
        if "academy" in (getattr(connection, "name", "") or "").lower():
            business = Hospital.objects.filter(industry="ACADEMY", is_active=True).first()
        elif "hospital" in (getattr(connection, "name", "") or "").lower():
            business = Hospital.objects.filter(industry="HOSPITAL", is_active=True).first()
        else:
            business = Hospital.objects.filter(industry="ACADEMY", is_active=True).first() or Business.objects.filter(is_active=True).first()
        
        # Auto-heal: bind this connection to the resolved business permanently so it never fluctuates
        if business and connection and not connection.business_id:
            try:
                connection.hospital = business
                connection.save(update_fields=["hospital"])
                logger.info(f"Auto-bound MetaAdsConnection {connection.id} to business {business.id} ({business.name})")
            except Exception as e:
                logger.warning(f"Failed to auto-bind connection to business: {e}")

    # 1. Stage (Fresh / New)
    stage = (
        LeadStage.objects.filter(hospital=business, name__iexact='Fresh').first()
        or LeadStage.objects.filter(hospital=business, name__iexact='New').first()
        or LeadStage.objects.filter(name__iexact='Fresh').first()
        or LeadStage.objects.filter(name__iexact='New').first()
        or LeadStage.objects.filter(is_active=True).order_by("order").first()
    )

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
            hospital=business,
            is_active=True
        )

    # 4. Course Association
    course_obj = None
    course_name = data.get("course")
    if course_name:
        course_obj = Course.objects.filter(name__iexact=course_name, hospital=business).first() or \
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
        hospital=business,
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
            Q(hospital=business) | Q(hospital__isnull=True) | Q(is_superuser=True)
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
            hosp_biz = Business.objects.filter(industry=Hospital.Industry.HOSPITAL, is_active=True).first() or Business.objects.first()
            source_obj = LeadSource.objects.filter(name__iexact="Just Dial").first() or LeadSource.objects.filter(name__icontains="Justdial").first()
            cat_obj = SourceCategory.objects.filter(name__icontains="Aggregator").first() or SourceCategory.objects.filter(name__icontains="Digital").first()

            lead = Lead.objects.create(
                name=name,
                mobile=clean_phone,
                city=city,
                hospital=hosp_biz,
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
            hosp_biz = Business.objects.filter(industry=Hospital.Industry.HOSPITAL, is_active=True).first() or Business.objects.first()
            source_obj = LeadSource.objects.filter(name__iexact="PRACTO").first() or LeadSource.objects.filter(name__icontains="Practo").first()
            cat_obj = SourceCategory.objects.filter(name__icontains="Healthcare").first() or SourceCategory.objects.filter(name__icontains="Digital").first()

            lead = Lead.objects.create(
                name=name,
                mobile=clean_phone,
                city="Nagpur",
                hospital=hosp_biz,
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


@csrf_exempt
def website_lead_webhook(request, business_id=1):
    """
    Webhook endpoint to ingest real-time appointment bookings and inquiries 
    from Nelson Hospital website (WordPress Contact Form 7 / Webhook Plugin).
    Default or path business_id is 1 (Nelson Hospital Dhantoli).
    """
    import re
    from accounts.models import Hospital
    from leads.models import Lead, LeadSource, SourceCategory, Campaign, LeadStage, LeadTemperature

    if request.method == "GET":
        return JsonResponse({
            "status": "active",
            "service": "Nelson Hospital Website Lead Ingestion Webhook",
            "target_business_id": business_id
        }, status=200)

    if request.method == "POST":
        try:
            # Parse payload from JSON body or standard POST form data (CF7 Webhook sends either)
            payload = {}
            if request.body:
                try:
                    payload = json.loads(request.body)
                except Exception:
                    pass
            if not payload:
                payload = request.POST.dict()

            # If payload has nested "fields" wrapper (e.g. {"form_id": "...", "fields": {...}})
            if isinstance(payload.get("fields"), dict):
                inner_fields = payload.pop("fields")
                inner_fields.update(payload)
                payload = inner_fields

            # Helper to safely extract string value even if CF7 sends it as a single-element list e.g. ["Oncology"]
            def _extract_str(val, default=""):
                if isinstance(val, (list, tuple)):
                    val = val[0] if val else default
                return str(val or default).strip()

            # Contact Form 7 field name variations
            name = _extract_str(
                payload.get("your-name") or payload.get("name") or payload.get("patient_name")
                or payload.get("full_name") or payload.get("your_name"),
                default="Website Patient"
            )

            phone = _extract_str(
                payload.get("your-phone") or payload.get("your-tel") or payload.get("phone")
                or payload.get("mobile") or payload.get("contact") or payload.get("your_phone")
            )

            email = _extract_str(
                payload.get("your-email") or payload.get("email") or payload.get("your_email")
            )

            department = _extract_str(
                payload.get("your-department") or payload.get("department") or payload.get("speciality")
                or payload.get("your_department")
            )

            doctor = _extract_str(
                payload.get("your-doctor") or payload.get("doctor") or payload.get("doctor_name")
                or payload.get("your_doctor")
            )

            appointment_date = _extract_str(
                payload.get("appointment-date") or payload.get("appointment_date") or payload.get("date")
                or payload.get("booking_date") or payload.get("your-date")
            )

            appointment_time = _extract_str(
                payload.get("appointment-time") or payload.get("appointment_time") or payload.get("time")
            )

            message = _extract_str(
                payload.get("your-message") or payload.get("message") or payload.get("notes")
                or payload.get("comments") or payload.get("your_message")
            )

            # Target Business Tenant (Hospital ID: 1 by default)
            hosp_biz = Hospital.objects.filter(pk=business_id, is_active=True).first()
            if not hosp_biz:
                hosp_biz = Hospital.objects.filter(industry=Hospital.Industry.HOSPITAL, is_active=True).first() or Hospital.objects.first()

            # Clean 10-digit mobile number
            clean_phone = "".join(filter(str.isdigit, str(phone)))[-10:] if phone else ""

            # Detect whether source is Dr. Nelsa Chatbot or Website Appointment Form
            source_type = _extract_str(
                payload.get("source_type") or payload.get("source") or payload.get("type")
            ).lower()
            is_chatbot = "chat" in source_type or "nelsa" in source_type or bool(payload.get("concern"))

            # Hospital Branch vs Patient Address:
            # - Appointment Modal form has hospital branch dropdown (e.g. "Dhantoli", "CBSH")
            # - Dr. Nelsa Chatbot has patient personal address
            hospital_branch_val = _extract_str(
                payload.get("your-branch") or payload.get("branch") or payload.get("hospital_branch")
            )
            
            patient_address_val = _extract_str(
                payload.get("your-address") or payload.get("address") or payload.get("location")
            )

            if not is_chatbot and not hospital_branch_val and patient_address_val:
                # In appointment form, the location dropdown is the hospital branch
                hospital_branch_val = patient_address_val
                patient_address_val = ""

            selected_branch = hospital_branch_val or (hosp_biz.name if hosp_biz else "Nelson Hospital Dhantoli")
            city = patient_address_val or _extract_str(payload.get("city"), default="Nagpur")

            # Resolve Category: Organic
            cat_obj = SourceCategory.objects.filter(name__iexact="Organic").first() or SourceCategory.objects.filter(name__iexact="Digital").first() or SourceCategory.objects.first()
            # Lead Source: Website (or Chatbot)
            source_obj = LeadSource.objects.filter(name__iexact="Website").first() or LeadSource.objects.filter(name__icontains="Web").first()
            
            # Campaign differentiation under Organic:
            # 1. "Website Appointment Form"
            # 2. "Dr. Nelsa Chatbot"
            target_campaign_name = "Dr. Nelsa Chatbot" if is_chatbot else "Website Appointment Form"
            camp_obj = Campaign.objects.filter(name__iexact=target_campaign_name, hospital=hosp_biz).first()
            if not camp_obj:
                camp_obj = Campaign.objects.filter(name__iexact="ORGANIC LEAD", hospital=hosp_biz).first() or Campaign.objects.first()

            # Resolve initial stage
            stage_obj = LeadStage.objects.filter(name__iexact="Fresh", hospital=hosp_biz).first()
            if not stage_obj:
                stage_obj = LeadStage.objects.filter(name__iexact="Fresh").first()

            # Prepare custom_data with all appointment & branch details
            custom_data = {
                "branch": selected_branch,
                "hospital_branch": selected_branch,
                "lead_source": "Website",
                "campaign": target_campaign_name,
                "deal_status": "Uncontacted",
                "source_type": "Dr. Nelsa Chatbot" if is_chatbot else "Website Appointment Form",
                "raw_form_data": payload
            }
            if department:
                custom_data["department"] = department
            if doctor:
                custom_data["doctor"] = doctor
            if appointment_date:
                custom_data["appo_booked_date"] = appointment_date
                custom_data["appointment_status"] = "Awaiting Approval from Doctor"
            if appointment_time:
                custom_data["appointment_time"] = appointment_time
            if is_chatbot and payload.get("concern"):
                custom_data["health_concern"] = str(payload.get("concern")).strip()

            # -------------------------------------------------------------
            # SMART PATIENT DEDUPLICATION: (Mobile + Patient Name Matching)
            # -------------------------------------------------------------
            # If same mobile number has a DIFFERENT patient name (e.g. father booking for child or wife):
            # -> DO NOT overwrite! CREATE A NEW PATIENT LEAD and link them.
            # If SAME mobile and SAME/SIMILAR patient name:
            # -> DEDUPLICATE and update existing inquiry notes.
            existing_lead = None
            if clean_phone:
                same_mobile_leads = Lead.objects.filter(
                    hospital=hosp_biz,
                    mobile=clean_phone,
                    is_archived=False
                ).order_by("-created_at")

                input_name_clean = re.sub(r"[^a-zA-Z0-9\s]", "", name.lower()).strip()

                for cand in same_mobile_leads:
                    cand_name_clean = re.sub(r"[^a-zA-Z0-9\s]", "", cand.name.lower()).strip()
                    # Check exact or partial name match (e.g., "Anita" in "Anita Sharma")
                    if (
                        input_name_clean == cand_name_clean
                        or (len(input_name_clean) >= 3 and input_name_clean in cand_name_clean)
                        or (len(cand_name_clean) >= 3 and cand_name_clean in input_name_clean)
                    ):
                        existing_lead = cand
                        break

            if existing_lead:
                # Same patient submitted again -> Deduplicate and log activity/timeline
                source_label = "Dr. Nelsa Chatbot" if is_chatbot else "Website Appointment Form"
                new_inquiry_log = f"\n[Re-Inquiry via {source_label} on {timezone.localtime().strftime('%d-%m-%Y %I:%M %p')}]: {message or 'Appointment inquiry re-submitted'}"
                existing_lead.notes = (str(existing_lead.notes or "") + new_inquiry_log).strip()
                existing_lead.custom_data.update(custom_data)
                existing_lead.save()

                try:
                    from followups.models import Activity
                    Activity.objects.create(
                        lead=existing_lead,
                        activity_type="NOTE",
                        description=f"Website Re-Inquiry received via {source_label}. Dept: {department or 'N/A'}, Branch: {selected_branch}.",
                    )
                except Exception:
                    pass

                logger.info(f"🔄 Website Lead Deduplicated & Updated: {existing_lead.lead_code} - {existing_lead.name}")
                return JsonResponse({
                    "status": "success",
                    "action": "updated",
                    "lead_id": existing_lead.id,
                    "lead_code": existing_lead.lead_code,
                    "message": "Existing patient record updated with new website inquiry."
                }, status=200)

            # Different patient name or first-time number -> Create New Patient Lead
            notes_text = message or (f"Website Chatbot inquiry: {payload.get('concern')}" if is_chatbot else "Website Appointment inquiry from nelsonhospitals.com")
            lead = Lead.objects.create(
                name=name,
                mobile=clean_phone,
                email=email,
                city=city,
                hospital=hosp_biz,
                lead_source=source_obj,
                source_category=cat_obj,
                campaign=camp_obj,
                stage=stage_obj,
                temperature=LeadTemperature.HOT if appointment_date else LeadTemperature.WARM,
                ad_platform="Website",
                inquiry_date=timezone.localdate(),
                custom_data=custom_data,
                notes=notes_text
            )

            logger.info(f"✅ Website Lead Ingested: {lead.lead_code} - {lead.name} (Phone: {clean_phone}) for {hosp_biz.name}")
            return JsonResponse({
                "status": "success",
                "action": "created",
                "lead_id": lead.id,
                "lead_code": lead.lead_code,
                "business_id": hosp_biz.id,
                "business_name": hosp_biz.name,
                "message": "Lead successfully created in CRM."
            }, status=201)

        except Exception as e:
            logger.error(f"❌ Error ingesting website lead: {e}", exc_info=True)
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

    # 1. Identify all active businesses
    all_businesses = list(Hospital.objects.filter(is_active=True).order_by("name"))

    # Determine if current user is Super Admin
    is_super_admin = getattr(request.user, "is_superuser", False) or getattr(request.user, "role", "") == "SUPER_ADMIN"

    # Active business determination
    req_biz = request.GET.get("business", "").strip()
    if not req_biz and not is_super_admin:
        req_biz = str(request.session.get("active_business_id", "")).strip()

    active_business = None
    if req_biz and req_biz.isdigit():
        active_business = Hospital.objects.filter(id=int(req_biz), is_active=True).first()
    elif not is_super_admin and request.user.hospital:
        active_business = request.user.hospital
    elif is_super_admin and req_biz:
        if req_biz == "all":
            active_business = None
        elif req_biz.isdigit():
            active_business = Hospital.objects.filter(id=int(req_biz), is_active=True).first()

    # Active channel selection: 'all', 'meta', 'justdial', 'practo'
    active_channel = request.GET.get("channel", "all").strip().lower()
    if active_channel not in ("all", "meta", "justdial", "practo"):
        active_channel = "all"

    # Date preset calculation: today, last_7d, last_30d, all, custom
    date_preset = request.GET.get("date_preset", "last_30d").strip().lower()
    start_date_str = request.GET.get("start_date", "").strip()
    end_date_str = request.GET.get("end_date", "").strip()

    DATE_PRESETS = [
        ("today", "Today"),
        ("last_7d", "Last 7 Days"),
        ("last_30d", "Last 30 Days"),
        ("all", "All"),
        ("custom", "Custom"),
    ]

    now = timezone.now()
    today_date = now.date()
    date_filter = Q()

    if date_preset == "today":
        date_filter = Q(inquiry_date=today_date)
    elif date_preset == "last_7d":
        date_filter = Q(inquiry_date__gte=today_date - timezone.timedelta(days=7))
    elif date_preset == "last_30d":
        date_filter = Q(inquiry_date__gte=today_date - timezone.timedelta(days=30))
    elif date_preset == "all":
        date_filter = Q()
    elif date_preset == "custom":
        from datetime import datetime
        if start_date_str:
            try:
                sd = datetime.strptime(start_date_str, "%Y-%m-%d").date()
                date_filter &= Q(inquiry_date__gte=sd)
            except ValueError:
                pass
        if end_date_str:
            try:
                ed = datetime.strptime(end_date_str, "%Y-%m-%d").date()
                date_filter &= Q(inquiry_date__lte=ed)
            except ValueError:
                pass
    else:
        # Default fallback to last_30d
        date_preset = "last_30d"
        date_filter = Q(inquiry_date__gte=today_date - timezone.timedelta(days=30))

    # 2. Base Leads Queryset according to business scope
    leads_base = Lead.objects.filter(is_archived=False)
    if active_business:
        leads_base = leads_base.filter(hospital=active_business)
    elif not is_super_admin and request.user.hospital:
        leads_base = leads_base.filter(hospital=request.user.hospital)

    if date_filter:
        leads_base = leads_base.filter(date_filter)

    # Calculate per-business lead counts for top business pills
    business_analytics_list = []
    for biz in all_businesses:
        b_leads = Lead.objects.filter(hospital=biz, is_archived=False)
        if date_filter:
            b_leads = b_leads.filter(date_filter)
        b_count = b_leads.count()
        business_analytics_list.append({
            "business": biz,
            "id": biz.id,
            "name": biz.name,
            "count": b_count,
        })

    total_all_businesses_count = sum(item["count"] for item in business_analytics_list)

    # Channel-specific sub-queries
    meta_leads_qs = leads_base.filter(
        Q(ad_platform="Meta") | Q(lead_source__name__icontains="Meta") | Q(lead_source__name__icontains="Facebook") | Q(custom_data__lead_source__icontains="Meta") | Q(custom_data__lead_source__icontains="Facebook")
    )
    justdial_leads_qs = leads_base.filter(
        Q(ad_platform="Justdial") | Q(lead_source__name__icontains="Justdial") | Q(lead_source__name__icontains="Just Dial") | Q(custom_data__lead_source__icontains="Justdial") | Q(custom_data__lead_source__icontains="Just Dial")
    )
    practo_leads_qs = leads_base.filter(
        Q(ad_platform="Practo") | Q(lead_source__name__icontains="Practo") | Q(custom_data__lead_source__icontains="Practo")
    )

    channel_meta_count = meta_leads_qs.count()
    channel_justdial_count = justdial_leads_qs.count()
    channel_practo_count = practo_leads_qs.count()

    # Active channel leads selection
    if active_channel == "meta":
        active_channel_leads = meta_leads_qs
        channel_name_display = "Meta Ads (Facebook & Instagram)"
    elif active_channel == "justdial":
        active_channel_leads = justdial_leads_qs
        channel_name_display = "Justdial Inquiries"
    elif active_channel == "practo":
        active_channel_leads = practo_leads_qs
        channel_name_display = "Practo Consultations"
    else:
        active_channel_leads = leads_base
        channel_name_display = "All Marketing Channels"

    # Meta API Connection for active business or global default
    if active_business:
        connection = MetaAdsConnection.objects.filter(is_active=True, hospital=active_business).first()
        if not connection:
            connection = MetaAdsConnection.objects.filter(is_active=True, name__icontains=active_business.name).first()
    else:
        connection = MetaAdsConnection.objects.filter(is_active=True).first()

    # Campaign stats & Insights
    campaigns = []
    total_spend = 0
    total_leads = 0
    total_clicks = 0
    total_impressions = 0
    error = None

    if connection and connection.page_access_token:
        campaigns, error = get_campaign_insights(connection.page_access_token, connection.ad_account_id, date_preset)
        if campaigns:
            for c in campaigns:
                MetaCampaignStat.objects.update_or_create(
                    hospital=connection.hospital,
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
            if active_business:
                db_stats = db_stats.filter(hospital=active_business)
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

        total_spend = sum(c.get("spend", 0) for c in campaigns)
        total_leads = sum(c.get("leads_count", 0) for c in campaigns)
        total_clicks = sum(c.get("clicks", 0) for c in campaigns)
        total_impressions = sum(c.get("impressions", 0) for c in campaigns)


    # Lead Table Feed
    recent_channel_leads = active_channel_leads.select_related("stage", "assigned_to", "course", "lead_source", "hospital").order_by("-inquiry_date", "-id")[:60]

    # Funnel Stats
    stages = LeadStage.objects.filter(is_active=True).order_by("order")
    funnel = [
        {"name": s.name, "count": active_channel_leads.filter(stage=s).count()}
        for s in stages
    ]

    # Metrics for Top KPI Cards
    channel_total_count = active_channel_leads.count()
    channel_converted_count = active_channel_leads.filter(
        Q(deal_status="WON") | Q(admission_status="ADMISSION_DONE") | Q(stage__name__icontains="Admission") | Q(stage__name__icontains="Payment") | Q(stage__name__icontains="Converted")
    ).count()
    channel_conv_rate = round((channel_converted_count / channel_total_count * 100), 1) if channel_total_count else 0.0

    # Lead Volume Chart Timeline (Day-by-day)
    chart_labels = []
    chart_values = []
    if date_preset == "today":
        today_leads = active_channel_leads.filter(inquiry_date=today_date)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timezone.timedelta(days=1)
        for h in range(8, 22, 2):
            chart_labels.append(f"{h:02d}:00")
            h_start = today_start.replace(hour=h)
            h_end = today_start.replace(hour=h+2) if h < 20 else today_end
            chart_values.append(today_leads.filter(created_at__gte=h_start, created_at__lt=h_end).count())
    else:
        days_count = 7 if date_preset in ("last_7d", "yesterday") else 14
        for i in range(days_count - 1, -1, -1):
            target_date = today_date - timezone.timedelta(days=i)
            chart_labels.append(target_date.strftime("%d %b"))
            chart_values.append(active_channel_leads.filter(inquiry_date=target_date).count())

    overall_cpl = round(total_spend / total_leads, 2) if total_leads else 0
    overall_ctr = round((total_clicks / total_impressions) * 100, 2) if total_impressions else 0

    return render(request, "meta_ads/campaign_dashboard.html", {
        "active": "meta_ads",
        "is_super_admin": is_super_admin,
        "all_businesses": all_businesses,
        "business_analytics_list": business_analytics_list,
        "total_all_businesses_count": total_all_businesses_count,
        "active_business": active_business,
        "active_channel": active_channel,
        "channel_name_display": channel_name_display,
        "connection": connection,
        "campaigns": campaigns,
        "recent_meta_leads": recent_channel_leads,
        "funnel": funnel,
        "channel_total_count": channel_total_count,
        "channel_converted_count": channel_converted_count,
        "channel_conv_rate": channel_conv_rate,
        "channel_meta_count": channel_meta_count,
        "channel_justdial_count": channel_justdial_count,
        "channel_practo_count": channel_practo_count,
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
        "start_date": start_date_str,
        "end_date": end_date_str,
        "today_date": today_date.strftime("%Y-%m-%d"),
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
        if connection and connection.hospital:
            lead_filter["hospital"] = connection.hospital

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

    from accounts.models import Business
    biz_id = request.POST.get("business_id") or request.POST.get("entity")
    target_biz = None
    if biz_id and str(biz_id).isdigit():
        target_biz = Business.objects.filter(id=int(biz_id), is_active=True).first()
    if not target_biz:
        target_biz = getattr(request.user, "business", None) or Business.objects.filter(is_active=True).first()

    conn_name = f"{target_biz.name} Meta Ads" if target_biz else "Business Meta Ads"
    default_verify = f"{target_biz.name.lower().replace(' ', '_')}_verify_2026" if target_biz else "crm_meta_verify_2026"

    connection = None
    if target_biz:
        connection = MetaAdsConnection.objects.filter(is_active=True, hospital=target_biz).first()
    if not connection:
        # Default global webhook is permanently attached to Zappcode Academy (Business ID: 7)
        connection = (
            MetaAdsConnection.objects.filter(is_active=True, hospital__id=7).first()
            or MetaAdsConnection.objects.filter(is_active=True, name__icontains="academy").first()
            or MetaAdsConnection.objects.filter(is_active=True).first()
        )

    if not connection:
        # Create separate connection for this specific business
        connection = MetaAdsConnection.objects.create(
            name=conn_name,
            hospital=target_biz,
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


# ─────────────────────────────────────────────────────────────────────────────
# 🎓 ACADEMY DEDICATED CAMPAIGN ANALYTICS (Clean, Isolated & Fast)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def academy_campaign_analytics(request):
    """
    Dedicated Campaign Analytics page for Academy Admin.
    - Scoped strictly to the logged-in Academy business.
    - Shows Top KPI Cards (Spend, Leads, Avg CPL, Active Campaigns).
    - Table with Campaign Name, Status, Budget, Total Spent, Leads, CPL.
    - Date Filters: Today, Yesterday, Last 7 Days, Last 30 Days, This Month, All Time.
    - Clean Modal to Connect / Update Meta credentials without clutter.
    """
    from accounts.models import Hospital
    from django.contrib import messages
    from leads.models import Lead

    user = request.user
    target_biz = getattr(user, "business", None)
    if not target_biz:
        target_biz = Hospital.objects.filter(industry="ACADEMY", is_active=True).first()

    # Find Meta Ads Connection strictly for this specific business
    connection = None
    if target_biz:
        connection = MetaAdsConnection.objects.filter(hospital=target_biz, is_active=True).first()

    # Date preset filter
    date_preset = request.GET.get("date_preset", "all").strip()
    # Normalize 'all', 'all_time', 'maximum'
    if date_preset in ("all", "all_time", "maximum"):
        date_preset = "maximum"
    elif date_preset not in ("today", "last_7d", "last_30d", "custom"):
        date_preset = "maximum"

    custom_start = request.GET.get("start_date", "").strip()
    custom_end = request.GET.get("end_date", "").strip()
    if custom_start and custom_end:
        date_preset = "custom"

    # Fetch stored campaign stats for this business
    stat_qs = MetaCampaignStat.objects.all()
    if target_biz:
        stat_qs = stat_qs.filter(Q(hospital=target_biz) | Q(hospital__isnull=True))

    campaigns = list(stat_qs.filter(date_preset=date_preset).order_by("-spend"))

    # Also compute real-time leads associated with this Academy's Meta campaigns
    lead_qs = Lead.objects.filter(is_archived=False)
    if target_biz:
        lead_qs = lead_qs.filter(hospital=target_biz)

    today = timezone.localdate()
    parsed_start = None
    parsed_end = None
    if date_preset == "today":
        lead_qs = lead_qs.filter(inquiry_date=today)
        parsed_start = today
        parsed_end = today
    elif date_preset == "last_7d":
        parsed_start = today - timezone.timedelta(days=7)
        parsed_end = today
        lead_qs = lead_qs.filter(inquiry_date__gte=parsed_start)
    elif date_preset == "last_30d":
        parsed_start = today - timezone.timedelta(days=30)
        parsed_end = today
        lead_qs = lead_qs.filter(inquiry_date__gte=parsed_start)
    elif date_preset == "custom" and custom_start and custom_end:
        try:
            parsed_start = datetime.strptime(custom_start, "%Y-%m-%d").date()
            parsed_end = datetime.strptime(custom_end, "%Y-%m-%d").date()
            lead_qs = lead_qs.filter(inquiry_date__gte=parsed_start, inquiry_date__lte=parsed_end)
        except Exception:
            parsed_start = today - timezone.timedelta(days=30)
            parsed_end = today
            date_preset = "last_30d"

    meta_leads_qs = lead_qs.filter(Q(ad_platform__iexact="Meta") | Q(lead_source__name__icontains="Meta") | Q(lead_source__name__icontains="Facebook"))
    total_leads_captured = meta_leads_qs.count()

    # Aggregate KPIs
    total_spend = sum(float(c.spend) for c in campaigns)
    total_campaign_leads = sum(int(c.leads_count) for c in campaigns)
    effective_leads = max(total_campaign_leads, total_leads_captured)
    avg_cpl = round(total_spend / effective_leads, 2) if effective_leads else 0

    # ─────────────────────────────────────────────────────────────────────────
    #  Chart 1: Campaign-wise Leads & Budget
    # ─────────────────────────────────────────────────────────────────────────
    from leads.models import Campaign
    import json
    
    # Map campaigns from both MetaCampaignStat and CRM Campaign models
    campaign_chart_labels = []
    campaign_chart_leads = []
    campaign_chart_budgets = []

    if campaigns:
        # If API stats exist from Meta
        for c in campaigns[:10]:
            campaign_chart_labels.append(c.campaign_name[:24] if c.campaign_name else "Unnamed")
            campaign_chart_leads.append(int(c.leads_count or 0))
            campaign_chart_budgets.append(float(c.spend or 0))
    else:
        # Fallback to CRM Lead counts per Campaign object
        crm_campaigns = Campaign.objects.filter(is_active=True)
        if target_biz:
            crm_campaigns = crm_campaigns.filter(hospital=target_biz)
        
        camp_list = []
        for c in crm_campaigns:
            c_leads = lead_qs.filter(campaign=c).count()
            b_cost = float(c.cost or 0)
            if c_leads > 0 or b_cost > 0:
                camp_list.append({
                    "name": c.name[:24] if c.name else "Unnamed",
                    "leads": c_leads,
                    "budget": b_cost,
                })
        
        # Sort by leads count descending
        camp_list.sort(key=lambda x: x["leads"], reverse=True)
        for item in camp_list[:10]:
            campaign_chart_labels.append(item["name"])
            campaign_chart_leads.append(item["leads"])
            campaign_chart_budgets.append(item["budget"])

    # ─────────────────────────────────────────────────────────────────────────
    # 📈 Chart 2: Date-wise Leads Volume Trend
    # ─────────────────────────────────────────────────────────────────────────
    date_chart_labels = []
    date_chart_counts = []

    if date_preset == "custom" and parsed_start and parsed_end:
        start_date = parsed_start
        end_date = parsed_end
    elif date_preset == "today":
        start_date = today
        end_date = today
    elif date_preset == "last_7d":
        start_date = today - timezone.timedelta(days=6)
        end_date = today
    elif date_preset == "last_30d":
        start_date = today - timezone.timedelta(days=29)
        end_date = today
    else:  # maximum / all time
        # Last 30 days for clean chart rendering
        start_date = today - timezone.timedelta(days=29)
        end_date = today

    # Group leads by inquiry_date or created_at
    date_counts_map = {}
    date_leads_qs = lead_qs.filter(
        Q(inquiry_date__gte=start_date, inquiry_date__lte=end_date) | 
        (Q(inquiry_date__isnull=True) & Q(created_at__date__gte=start_date, created_at__date__lte=end_date))
    )
    for l in date_leads_qs:
        d = l.inquiry_date or l.created_at.date()
        date_counts_map[d] = date_counts_map.get(d, 0) + 1

    curr = start_date
    # Cap timeline loop at 90 days to prevent excessive ticks
    loop_count = 0
    while curr <= end_date and loop_count < 90:
        date_chart_labels.append(curr.strftime("%d %b"))
        date_chart_counts.append(date_counts_map.get(curr, 0))
        curr += timezone.timedelta(days=1)
        loop_count += 1

        # ─────────────────────────────────────────────────────────────────────────
    #  Campaign Intent Analytics Matrix Calculation (High Performance & Fail-Safe)
    # ─────────────────────────────────────────────────────────────────────────
    from leads.models import LeadIntent
    campaign_intent_data = []
    overall_intent = {
        "total": 0,
        "highly": 0,
        "moderate": 0,
        "normal": 0,
        "below_avg": 0,
        "not_int": 0,
        "highly_pct": 0,
        "moderate_pct": 0,
        "normal_pct": 0,
        "below_avg_pct": 0,
        "not_int_pct": 0,
    }

    try:
        eval_leads = list(lead_qs.select_related("campaign", "stage"))
        camp_lead_map = {}

        def resolve_intent_fast(l):
            """Ultra-fast in-memory evaluation without triggering nested child DB queries."""
            st_name = (l.stage.name if l.stage else "").strip().lower()
            adm_st = str(l.admission_status or "").strip().upper()
            cd = l.custom_data or {}
            raw_ds = str(cd.get("deal_status") or "").strip().lower()
            appt_st = str(cd.get("appointment_status") or "").strip().lower()

            # 1. Lost Check
            is_lost = (
                l.deal_status == "LOST"
                or adm_st in ("LOST", "CANCELLED", "DROPOUT")
                or raw_ds in ("lost", "cancelled")
                or any(k in st_name for k in ["lost", "cancelled", "not int", "rejected", "dropout", "dropped"])
                or any(k in appt_st for k in ["lost", "cancel", "not int"])
            )
            if is_lost:
                return LeadIntent.NOT_INTERESTED.value

            # 2. Highly Interested Check (Admission / Payment / Confirmed / Won)
            is_highly = (
                l.deal_status == "WON"
                or adm_st in ("WON", "ADMISSION_DONE")
                or any(k in st_name for k in ["admission done", "admission planned", "payment done", "appointment completed", "booking confirmed", "appointment confirmed", "closed won"])
                or any(k in raw_ds for k in ["won", "admission done", "payment done"])
                or any(k in appt_st for k in ["payment done", "appointment completed", "booking confirmed", "appointment confirmed", "booked", "confirm", "complet"])
            )
            if is_highly:
                return LeadIntent.HIGHLY_INTERESTED.value

            # 3. Explicit Intent Level if set and not default
            stored_intent = str(l.intent_level or "").strip().upper()
            if stored_intent and stored_intent in LeadIntent.values and stored_intent != LeadIntent.NORMAL_INTERESTED.value:
                return stored_intent

            # 4. Temperature Fallback
            stored_temp = str(l.temperature or "").strip().lower()
            if stored_temp == "hot":
                return LeadIntent.HIGHLY_INTERESTED.value
            elif stored_temp == "warm":
                return LeadIntent.MODERATE_INTERESTED.value
            elif stored_temp == "cold":
                return LeadIntent.BELOW_AVERAGE.value
            elif stored_temp == "freeze":
                return LeadIntent.NOT_INTERESTED.value

            return LeadIntent.NORMAL_INTERESTED.value

        for l in eval_leads:
            c_name = ""
            if l.campaign:
                c_name = l.campaign.name
            else:
                cd = l.custom_data or {}
                c_name = cd.get("campaign") or cd.get("campaign_name") or "Direct / Organic"
            c_name = str(c_name).strip() if c_name else "Direct / Organic"

            if c_name not in camp_lead_map:
                camp_lead_map[c_name] = []
            camp_lead_map[c_name].append(l)

        for c_name, leads_in_camp in camp_lead_map.items():
            total_c_leads = len(leads_in_camp)
            if total_c_leads == 0:
                continue

            intents = [resolve_intent_fast(l) for l in leads_in_camp]
            highly_cnt = intents.count(LeadIntent.HIGHLY_INTERESTED.value)
            moderate_cnt = intents.count(LeadIntent.MODERATE_INTERESTED.value)
            normal_cnt = intents.count(LeadIntent.NORMAL_INTERESTED.value)
            below_avg_cnt = intents.count(LeadIntent.BELOW_AVERAGE.value)
            not_int_cnt = intents.count(LeadIntent.NOT_INTERESTED.value)

            # Calculate percentages
            highly_pct = round((highly_cnt / total_c_leads) * 100, 1)
            moderate_pct = round((moderate_cnt / total_c_leads) * 100, 1)
            normal_pct = round((normal_cnt / total_c_leads) * 100, 1)
            below_avg_pct = round((below_avg_cnt / total_c_leads) * 100, 1)
            not_int_pct = round((not_int_cnt / total_c_leads) * 100, 1)

            weighted_score = (highly_cnt * 100 + moderate_cnt * 75 + normal_cnt * 50 + below_avg_cnt * 25) / total_c_leads
            quality_score = round(weighted_score, 1)

            campaign_intent_data.append({
                "campaign_name": c_name,
                "total_leads": total_c_leads,
                "highly_interested": highly_cnt,
                "highly_pct": highly_pct,
                "moderate_interested": moderate_cnt,
                "moderate_pct": moderate_pct,
                "normal_interested": normal_cnt,
                "normal_pct": normal_pct,
                "below_average": below_avg_cnt,
                "below_avg_pct": below_avg_pct,
                "not_interested": not_int_cnt,
                "not_int_pct": not_int_pct,
                "quality_score": quality_score,
            })

        campaign_intent_data.sort(key=lambda x: (x["total_leads"], x["quality_score"]), reverse=True)

        overall_total = len(eval_leads)
        overall_intent = {
            "total": overall_total,
            "highly": sum(c["highly_interested"] for c in campaign_intent_data),
            "moderate": sum(c["moderate_interested"] for c in campaign_intent_data),
            "normal": sum(c["normal_interested"] for c in campaign_intent_data),
            "below_avg": sum(c["below_average"] for c in campaign_intent_data),
            "not_int": sum(c["not_interested"] for c in campaign_intent_data),
        }
        if overall_total > 0:
            overall_intent["highly_pct"] = round((overall_intent["highly"] / overall_total) * 100, 1)
            overall_intent["moderate_pct"] = round((overall_intent["moderate"] / overall_total) * 100, 1)
            overall_intent["normal_pct"] = round((overall_intent["normal"] / overall_total) * 100, 1)
            overall_intent["below_avg_pct"] = round((overall_intent["below_avg"] / overall_total) * 100, 1)
            overall_intent["not_int_pct"] = round((overall_intent["not_int"] / overall_total) * 100, 1)
    except Exception as e:
        logger.exception(f"Error calculating campaign intent matrix: {e}")

    return render(request, "academy/dashboard/campaign_analytics.html", {
        "campaign_intent_data": campaign_intent_data,
        "overall_intent": overall_intent,
        "active": "campaign_analytics",
        "target_biz": target_biz,
        "connection": connection,
        "campaigns": campaigns,
        "date_preset": date_preset,
        "custom_start": custom_start,
        "custom_end": custom_end,
        "total_spend": total_spend,
        "total_leads": effective_leads,
        "avg_cpl": avg_cpl,
        "active_campaigns_count": len(campaign_chart_labels) if not campaigns else len(campaigns),
        "recent_leads": meta_leads_qs.order_by("-created_at")[:10],
        # Chart JSON data
        "camp_labels_json": json.dumps(campaign_chart_labels),
        "camp_leads_json": json.dumps(campaign_chart_leads),
        "camp_budgets_json": json.dumps(campaign_chart_budgets),
        "date_labels_json": json.dumps(date_chart_labels),
        "date_counts_json": json.dumps(date_chart_counts),
        "has_chart_data": bool(any(campaign_chart_leads) or any(date_chart_counts)),
    })


@login_required
def academy_save_meta_credentials(request):
    """Save or update Meta Ads credentials for the logged-in Academy with live verification."""
    from accounts.models import Hospital
    from django.contrib import messages
    from django.shortcuts import redirect
    from .api import test_meta_connection

    if request.method != "POST":
        return redirect("meta_ads:academy_campaign_analytics")

    user = request.user
    target_biz = getattr(user, "business", None)
    if not target_biz:
        target_biz = Hospital.objects.filter(industry="ACADEMY", is_active=True).first()

    ad_account_id = request.POST.get("ad_account_id", "").strip().replace("act_", "")
    page_access_token = request.POST.get("page_access_token", "").strip()
    page_id = request.POST.get("page_id", "").strip()

    if not ad_account_id or not page_access_token:
        messages.error(request, "Ad Account ID and Access Token are required.")
        return redirect("meta_ads:academy_campaign_analytics")

    # 1. Live Verify Token & Ad Account with Meta API first
    is_valid, validation_msg = test_meta_connection(page_access_token, ad_account_id)
    if not is_valid:
        messages.error(
            request,
            f"❌ Meta Connection Failed: {validation_msg}. Please ensure 'ads_read' & 'ads_management' permissions are granted to Ad Account {ad_account_id}."
        )
        return redirect("meta_ads:academy_campaign_analytics")

    # 2. Update or create connection for this Academy
    connection, created = MetaAdsConnection.objects.update_or_create(
        hospital=target_biz,
        defaults={
            "name": f"{target_biz.name if target_biz else 'Academy'} Meta Ads",
            "ad_account_id": ad_account_id,
            "page_access_token": page_access_token,
            "page_id": page_id,
            "webhook_verify_token": "zappcode_academy_verify_2026",
            "is_active": True,
        }
    )

    action = "connected" if created else "updated"
    messages.success(request, f"✅ Meta Ads account verified & {action}! ({validation_msg})")
    return redirect("meta_ads:academy_campaign_analytics")


@login_required
def academy_sync_meta_now(request):
    """Trigger real-time pull of campaigns and insights from Meta for this Academy."""
    from accounts.models import Hospital
    from django.contrib import messages
    from django.shortcuts import redirect
    from .api import get_campaign_insights, fetch_all_form_leads

    user = request.user
    target_biz = getattr(user, "business", None)
    if not target_biz:
        target_biz = Hospital.objects.filter(industry="ACADEMY", is_active=True).first()

    connection = MetaAdsConnection.objects.filter(hospital=target_biz, is_active=True).first()
    if not connection:
        # Default global webhook is permanently attached to Zappcode Academy (Business ID: 7)
        connection = (
            MetaAdsConnection.objects.filter(is_active=True, hospital__id=7).first()
            or MetaAdsConnection.objects.filter(is_active=True, name__icontains="academy").first()
            or MetaAdsConnection.objects.filter(is_active=True).first()
        )

    if not connection or not connection.page_access_token or not connection.ad_account_id:
        messages.error(request, "Please connect your Meta Ad Account ID and Access Token first.")
        return redirect("meta_ads:academy_campaign_analytics")

    date_preset = request.POST.get("date_preset", "last_30d")
    campaigns, error_msg = get_campaign_insights(connection.page_access_token, connection.ad_account_id, date_preset)

    if error_msg:
        messages.error(
            request,
            f"❌ Meta Sync Failed: {error_msg}. (Make sure your Meta System User has 'ads_read' access to Ad Account {connection.ad_account_id})."
        )
        return redirect(f"/meta-ads/academy/campaigns/?date_preset={date_preset}")

    count = 0
    for c in campaigns:
        MetaCampaignStat.objects.update_or_create(
            hospital=target_biz,
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

    # Also pull fresh form leads if page_id is provided
    leads_imported = 0
    if connection.page_id:
        try:
            leads_data = fetch_all_form_leads(connection.page_access_token, connection.page_id, limit_per_form=50)
            for item in leads_data:
                lead = create_or_update_meta_lead(connection, item)
                if lead:
                    leads_imported += 1
        except Exception as e:
            logger.warning(f"Lead fetch error during sync: {e}")

    connection.last_synced_at = timezone.now()
    connection.save(update_fields=["last_synced_at"])

    lead_msg = f" and imported {leads_imported} new lead(s)" if leads_imported else ""
    messages.success(request, f"Successfully synced {count} campaign(s){lead_msg} from Meta Ads!")
    return redirect(f"/meta-ads/academy/campaigns/?date_preset={date_preset}")



