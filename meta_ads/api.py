import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

META_GRAPH_URL = "https://graph.facebook.com/v19.0"


def resolve_course_name(raw_str):
    """Normalize raw course string from Meta form to CRM course name."""
    if not raw_str:
        return ""
    s = str(raw_str).strip().lower().replace("_", " ")
    if "data anal" in s:
        return "Data Analytics"
    if "ai" in s or "machine learn" in s or "ml" in s:
        return "AI & Machine Learning"
    if "data sci" in s:
        return "Data Science"
    if "python" in s:
        return "Python programming"
    if "full stack" in s:
        return "Full Stack Development"
    if "digital market" in s:
        return "Digital Marketing"
    if "web dev" in s:
        return "Frontend Web Development"
    return str(raw_str).strip().replace("_", " ").title()


def parse_lead_field_data(item):
    """Robustly parse lead fields from Meta Graph API item."""
    fields = {
        "meta_lead_id": str(item.get("id")),
        "created_time": item.get("created_time", ""),
        "name": "",
        "phone": "",
        "email": "",
        "city": "",
        "course": "",
        "other_details": [],
        "raw": item,
    }
    
    for f in item.get("field_data", []):
        fname = f.get("name", "").lower()
        vals = f.get("values", [])
        val_str = str(vals[0]).strip() if vals else ""
        if not val_str:
            continue
            
        if any(k in fname for k in ["full_name", "first_name", "name"]) and not fields["name"]:
            fields["name"] = val_str
        elif any(k in fname for k in ["phone", "mobile"]) and not fields["phone"]:
            fields["phone"] = val_str
        elif "email" in fname and not fields["email"]:
            fields["email"] = val_str
        elif "city" in fname and not fields["city"]:
            fields["city"] = val_str
        elif any(k in fname for k in ["course", "service", "looking_for"]) and not fields["course"]:
            fields["course"] = resolve_course_name(val_str)
        else:
            fields["other_details"].append(f"{f.get('name')}: {val_str}")
            
    # Clean phone (strip non-digits, take 10 or 12 digits)
    if fields["phone"]:
        clean_phone = "".join(ch for ch in fields["phone"] if ch.isdigit())
        if len(clean_phone) > 10 and clean_phone.startswith("91"):
            clean_phone = clean_phone[2:]
        fields["clean_mobile"] = clean_phone
    else:
        fields["clean_mobile"] = ""
        
    return fields


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

        parsed = parse_lead_field_data(data)
        parsed.update({
            "campaign_id": data.get("campaign_id", ""),
            "campaign_name": data.get("campaign_name", ""),
            "ad_set_name": data.get("adset_name", ""),
            "ad_name": data.get("ad_name", ""),
            "form_id": data.get("form_id", ""),
        })
        return parsed
    except Exception as e:
        logger.error(f"Meta API error fetching lead {lead_id}: {e}")
        return None


def fetch_all_form_leads(access_token, page_id, limit_per_form=100):
    """
    Fetch all recent leads across all active leadgen forms for a Meta Page.
    Returns a list of parsed lead dicts.
    """
    leads = []
    try:
        forms_url = f"{META_GRAPH_URL}/{page_id}/leadgen_forms"
        forms_res = requests.get(forms_url, params={"access_token": access_token, "limit": 100}, timeout=15)
        forms_res.raise_for_status()
        forms = forms_res.json().get("data", [])
        logger.info(f"Checking {len(forms)} leadgen forms on Meta Page {page_id}...")

        for f in forms:
            fid = f.get("id")
            fname = f.get("name", "Form")
            url = f"{META_GRAPH_URL}/{fid}/leads"
            params = {"access_token": access_token, "limit": limit_per_form}

            # Fetch first page (or more if needed)
            l_res = requests.get(url, params=params, timeout=15)
            if l_res.status_code != 200:
                continue
            ldata = l_res.json().get("data", [])

            for item in ldata:
                lead_dict = parse_lead_field_data(item)
                lead_dict["form_id"] = fid
                lead_dict["form_name"] = fname
                # Ignore test dummy leads
                if "test@meta.com" in lead_dict["email"].lower():
                    continue
                leads.append(lead_dict)

        logger.info(f"Total Meta form leads fetched: {len(leads)}")
    except Exception as e:
        logger.error(f"Error fetching Meta form leads: {e}")
        
    return leads


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
