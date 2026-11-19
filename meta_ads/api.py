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
            
    # Clean phone: normalize international code (+91, 0091, 0) and strip formatting spaces/dashes
    if fields["phone"]:
        raw_p = str(fields["phone"]).strip()
        digits = "".join(ch for ch in raw_p if ch.isdigit())
        if len(digits) == 12 and digits.startswith("91"):
            clean_phone = digits[2:]
        elif len(digits) == 13 and digits.startswith("091"):
            clean_phone = digits[3:]
        elif len(digits) == 11 and digits.startswith("0"):
            clean_phone = digits[1:]
        elif len(digits) > 10:
            clean_phone = digits[-10:]
        else:
            clean_phone = digits
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


def fetch_all_form_leads(access_token, page_id, limit_per_form=200, max_forms=None):
    """
    Fetch all leads across all leadgen forms for a Meta Page using full Graph API cursor pagination.
    - Paginates through all available forms on the Page.
    - Paginates through all leads in each form (up to limit_per_form).
    - Guarantees no leads are dropped regardless of form count or lead volume.
    Returns a list of parsed lead dicts.
    """
    leads = []
    try:
        # 1. Fetch ALL leadgen forms for the page using pagination
        all_forms = []
        forms_url = f"{META_GRAPH_URL}/{page_id}/leadgen_forms"
        forms_params = {"access_token": access_token, "limit": 100}

        while forms_url:
            forms_res = requests.get(forms_url, params=forms_params, timeout=15)
            if forms_res.status_code != 200:
                break
            forms_payload = forms_res.json()
            page_data = forms_payload.get("data", [])
            all_forms.extend(page_data)

            # Check next cursor page for forms
            paging = forms_payload.get("paging", {})
            forms_url = paging.get("next")
            forms_params = {} # params already encoded in next URL if present

            if max_forms and len(all_forms) >= max_forms:
                all_forms = all_forms[:max_forms]
                break

        # Filter for ACTIVE forms first; fallback to all forms if no active flag
        active_forms = [f for f in all_forms if f.get("status") == "ACTIVE"]
        forms_to_check = active_forms if active_forms else all_forms

        logger.info(f"Checking {len(forms_to_check)} forms (of {len(all_forms)} total) on Meta Page {page_id}...")

        # 2. Iterate each form and paginate leads
        for f in forms_to_check:
            fid = f.get("id")
            fname = f.get("name", "Form")
            leads_url = f"{META_GRAPH_URL}/{fid}/leads"
            leads_params = {"access_token": access_token, "limit": min(limit_per_form or 100, 100)}
            form_leads_collected = 0

            while leads_url:
                try:
                    l_res = requests.get(leads_url, params=leads_params, timeout=10)
                    if l_res.status_code != 200:
                        break
                    leads_payload = l_res.json()
                    ldata = leads_payload.get("data", [])
                    if not ldata:
                        break
                except Exception as form_err:
                    logger.warning(f"Timeout/Error fetching leads for form {fid}: {form_err}")
                    break

                for item in ldata:
                    lead_dict = parse_lead_field_data(item)
                    lead_dict["form_id"] = fid
                    lead_dict["form_name"] = fname
                    # Ignore test dummy leads
                    if "test@meta.com" in (lead_dict.get("email") or "").lower():
                        continue
                    leads.append(lead_dict)
                    form_leads_collected += 1

                # If reached limit requested per form, stop fetching this form
                if limit_per_form and form_leads_collected >= limit_per_form:
                    break

                # Follow Graph API pagination cursor for leads
                leads_paging = leads_payload.get("paging", {})
                leads_url = leads_paging.get("next")
                leads_params = {} # Already encoded in next URL

        logger.info(f"Total Meta form leads fetched across all pages: {len(leads)}")
    except Exception as e:
        logger.error(f"Error fetching Meta form leads: {e}")
        
    return leads


def test_meta_connection(access_token, ad_account_id):
    """
    Validate the Meta Access Token and Ad Account permissions.
    Returns (success: bool, message: str)
    """
    clean_acc_id = str(ad_account_id).strip().replace("act_", "")
    try:
        url = f"{META_GRAPH_URL}/act_{clean_acc_id}"
        resp = requests.get(
            url,
            params={
                "access_token": access_token,
                "fields": "id,name,account_status,currency,timezone_name",
            },
            timeout=10,
        )
        data = resp.json()
        if resp.status_code != 200 or "error" in data:
            err_msg = data.get("error", {}).get("message", "Unknown Meta error.")
            code = data.get("error", {}).get("code", resp.status_code)
            return False, f"Meta API Error ({code}): {err_msg}"
        return True, f"Connected to {data.get('name', 'Ad Account')} (ID: {clean_acc_id})"
    except Exception as e:
        return False, f"Network or connection error: {str(e)}"


def get_campaign_insights(access_token, ad_account_id, date_preset="last_30d"):
    """
    Fetch campaign-level insights from Meta Ads API.
    Returns (results_list, error_message_or_None)
    """
    clean_acc_id = str(ad_account_id).strip().replace("act_", "")
    try:
        url = f"{META_GRAPH_URL}/act_{clean_acc_id}/insights"
        resp = requests.get(url, params={
            "access_token": access_token,
            "level": "campaign",
            "date_preset": date_preset,
            "fields": "campaign_id,campaign_name,spend,impressions,clicks,reach,actions",
        }, timeout=15)
        data = resp.json()

        if resp.status_code != 200 or "error" in data:
            err = data.get("error", {})
            err_msg = err.get("message", "Permission or Account error from Meta.")
            logger.error(f"Meta Insights API error: {resp.status_code} - {err_msg}")
            return [], err_msg

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
        return results, None
    except Exception as e:
        logger.error(f"Meta Insights API exception: {e}")
        return [], str(e)

