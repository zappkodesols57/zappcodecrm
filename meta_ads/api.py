import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

META_GRAPH_URL = "https://graph.facebook.com/v19.0"


def get_lead_details(access_token, lead_id):
    """Fetch a single lead's field data from Meta Graph API."""
    try:
        url = f"{META_GRAPH_URL}/{lead_id}"
        resp = requests.get(url, params={
            "access_token": access_token,
            "fields": "field_data,created_time,ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,form_id"
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        # Flatten field_data list into a dict
        fields = {}
        for item in data.get("field_data", []):
            fields[item["name"]] = item["values"][0] if item.get("values") else ""

        return {
            "meta_lead_id": lead_id,
            "name": fields.get("full_name") or fields.get("name", ""),
            "phone": fields.get("phone_number") or fields.get("mobile", ""),
            "email": fields.get("email", ""),
            "city": fields.get("city", ""),
            "department": fields.get("department") or fields.get("service", ""),
            "campaign_id": data.get("campaign_id", ""),
            "campaign_name": data.get("campaign_name", ""),
            "ad_set_name": data.get("adset_name", ""),
            "ad_name": data.get("ad_name", ""),
            "form_id": data.get("form_id", ""),
            "created_time": data.get("created_time", ""),
            "raw": data,
        }
    except Exception as e:
        logger.error(f"Meta API error fetching lead {lead_id}: {e}")
        return None


def get_campaign_insights(access_token, ad_account_id, date_preset="last_30d"):
    """Fetch campaign-level insights from Meta Ads API."""
    try:
        url = f"{META_GRAPH_URL}/act_{ad_account_id}/insights"
        resp = requests.get(url, params={
            "access_token": access_token,
            "level": "campaign",
            "date_preset": date_preset,
            "fields": "campaign_id,campaign_name,spend,impressions,clicks,reach,actions",
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        results = []
        for row in data.get("data", []):
            leads_count = 0
            for action in row.get("actions", []):
                if action.get("action_type") in ("lead", "onsite_conversion.lead_grouped"):
                    leads_count += int(action.get("value", 0))

            spend = float(row.get("spend", 0))
            clicks = int(row.get("clicks", 0))
            impressions = int(row.get("impressions", 0))

            results.append({
                "campaign_id": row.get("campaign_id", ""),
                "campaign_name": row.get("campaign_name", ""),
                "spend": spend,
                "impressions": impressions,
                "clicks": clicks,
                "leads_count": leads_count,
                "reach": int(row.get("reach", 0)),
                "cpl": round(spend / leads_count, 2) if leads_count else 0,
                "cpc": round(spend / clicks, 2) if clicks else 0,
                "ctr": round((clicks / impressions) * 100, 2) if impressions else 0,
            })
        return results
    except Exception as e:
        logger.error(f"Meta Insights API error: {e}")
        return []
