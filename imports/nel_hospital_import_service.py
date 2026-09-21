import io
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, date
import pandas as pd
import numpy as np
from django.utils import timezone
from django.db import transaction

from leads.models import Lead, LeadStage, LeadSource, SourceCategory, Campaign, LeadTemperature, DealStatus, AdmissionStatus
from accounts.models import User, Hospital


def clean_phone_number(raw):
    """
    Cleans phone numbers:
    - handles floats like 9876543210.0
    - strips spaces, dashes, parentheses, +91, 0 prefix
    - returns clean 10-digit string
    """
    if raw is None or (isinstance(raw, float) and np.isnan(raw)):
        return ""
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none", "null", "nat"):
        return ""
    if s.endswith(".0"):
        s = s[:-2]
    # Split if multiple numbers exist
    parts = re.split(r"[/,;]", s)
    for p in parts:
        digits = re.sub(r"\D", "", p.strip())
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        elif len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        if len(digits) == 10:
            return digits
    # Fallback to pure digits
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 10:
        return digits[-10:]
    return digits


def parse_flexible_date(raw):
    """Parses date/datetime from ISO strings, Excel timestamps, or strings."""
    if raw is None or (isinstance(raw, float) and np.isnan(raw)):
        return timezone.localdate()
    if isinstance(raw, (datetime, pd.Timestamp)):
        return raw.date()
    if isinstance(raw, date):
        return raw
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none", "nat", "-"):
        return timezone.localdate()
    
    # ISO 8601 e.g. 2026-08-23T07:16:47.000
    if "t" in s.lower():
        try:
            clean_s = s.split(".")[0].replace("Z", "").replace("z", "")
            return datetime.fromisoformat(clean_s).date()
        except Exception:
            pass
            
    formats = [
        "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
        "%m/%d/%Y", "%Y/%m/%d", "%d-%b-%Y", "%d %b %Y",
        "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
            
    try:
        parsed = pd.to_datetime(s, errors="coerce")
        if pd.notna(parsed):
            return parsed.date()
    except Exception:
        pass
    return timezone.localdate()


def read_xml_spreadsheet(file_bytes):
    """
    Parses Microsoft Office Excel 2003 XML Spreadsheet (<Workbook>).
    """
    tree = ET.fromstring(file_bytes)
    ns = {
        'ss': 'urn:schemas-microsoft-com:office:spreadsheet',
        'o': 'urn:schemas-microsoft-com:office:office',
        'x': 'urn:schemas-microsoft-com:office:excel',
        'html': 'http://www.w3.org/TR/REC-html40'
    }
    
    # Find all rows in the first Worksheet
    rows = tree.findall('.//ss:Worksheet//ss:Table//ss:Row', ns)
    if not rows:
        # Try finding anywhere
        rows = tree.findall('.//ss:Row', ns)
    if not rows:
        raise ValueError("No table rows found in XML Spreadsheet.")

    data = []
    headers = []
    
    for r_idx, row in enumerate(rows):
        cells = row.findall('ss:Cell', ns)
        row_values = []
        current_col = 0
        for cell in cells:
            idx_attr = cell.get(f"{{{ns['ss']}}}Index")
            if idx_attr:
                target_col = int(idx_attr) - 1
                while current_col < target_col:
                    row_values.append("")
                    current_col += 1
            data_elem = cell.find('ss:Data', ns)
            if data_elem is not None and data_elem.text is not None:
                row_values.append(data_elem.text.strip())
            else:
                row_values.append("")
            current_col += 1
        if r_idx == 0:
            headers = [h.strip() for h in row_values]
        else:
            # Pad row if needed
            if len(row_values) < len(headers):
                row_values.extend([""] * (len(headers) - len(row_values)))
            elif len(row_values) > len(headers):
                row_values = row_values[:len(headers)]
            data.append(row_values)
            
    df = pd.DataFrame(data, columns=headers)
    return df


def parse_any_file_to_dataframe(uploaded_file):
    """
    Reads an uploaded file object or path and returns a pandas DataFrame.
    Supports .xlsx, .xls, .xml (Excel 2003 XML), .csv, .tsv.
    """
    filename = getattr(uploaded_file, 'name', str(uploaded_file)).lower()
    
    if hasattr(uploaded_file, 'read'):
        content = uploaded_file.read()
        if hasattr(uploaded_file, 'seek'):
            uploaded_file.seek(0)
    else:
        with open(uploaded_file, 'rb') as f:
            content = f.read()

    # 1. XML Spreadsheet check
    if filename.endswith(".xml") or (b"<?xml" in content[:200] and b"schemas-microsoft-com:office:spreadsheet" in content[:1000]):
        return read_xml_spreadsheet(content)

    # 2. XLSX or XLS
    if filename.endswith(".xlsx") or filename.endswith(".xls"):
        try:
            return pd.read_excel(io.BytesIO(content))
        except Exception:
            # Fallback if it's an XML renamed to .xls or .xlsx
            if b"<?xml" in content[:200]:
                return read_xml_spreadsheet(content)
            raise

    # 3. CSV / TSV / Google Sheets Export
    encodings = ['utf-8', 'utf-8-sig', 'latin-1', 'cp1252']
    for enc in encodings:
        try:
            text = content.decode(enc)
            sep = '\t' if filename.endswith('.tsv') or '\t' in text.split('\n')[0] else ','
            return pd.read_csv(io.StringIO(text), sep=sep)
        except Exception:
            continue

    # Fallback to pandas read_csv
    return pd.read_csv(io.BytesIO(content))


def extract_campaign_lead_data(df, target_campaign=None, target_hospital=None):
    """
    Specifically cleans and structures leads from Campaign / Ad exports (like Meta/Google leads).
    Extracts core fields:
      - Patient Name
      - Phone
      - Email
      - Platform (maps to LeadSource)
      - Created Time
      - Survey Question (e.g. how_many_months_pregnant_are_you?)
      - Raw Metadata (ad_id, ad_name, adset_name, form_name, etc.)
    """
    processed_rows = []
    
    # Normalize header names for easy matching
    col_map = {}
    for col in df.columns:
        c_clean = str(col).strip().lower().replace(" ", "_").replace("-", "_")
        col_map[col] = c_clean

    # Known column mapping heuristics
    name_cols = [
        "your_name", "full_name", "patient_name", "lead_name", "customer_name", 
        "client_name", "name", "first_name", "user_name", "contact_name", "naam"
    ]
    phone_cols = [
        "phone_number", "phone", "mobile", "mobile_no", "contact", 
        "contact_no", "whatsapp", "whatsapp_number", "call_number", "cell"
    ]
    email_cols = ["email", "e_mail", "email_address", "mail"]
    date_cols = ["created_time", "created_at", "date", "inquiry_date", "lead_date", "lead_created_date", "time"]
    platform_cols = ["platform", "source", "publisher_platform", "lead_source", "channel"]
    attendant_cols = [
        "lead_attendent", "lead_attendant", "attendent", "attendant", 
        "assigned_to", "assigned", "telecaller", "caller", "counsellor", "agent", "executive"
    ]
    doctor_cols = ["doctor", "dr_name", "consultant", "physician", "surgeon", "dr"]
    gender_cols = ["gender", "sex", "m/f"]
    address_cols = ["adderess", "address", "location", "city", "area", "town", "district"]
    due_date_cols = ["due_date", "edd", "expected_delivery_date"]
    
    # Financial & clinical bills
    uhid_cols = ["uhid_no", "uhid_id_no", "uhid", "patient_id"]
    opd_done_date_cols = ["opd_done_date", "visit_date", "appointment_date"]
    pharmacy_bill_cols = ["pharmacy_bill", "pharmacy"]
    opd_bill_cols = ["opd_bill", "opd"]
    ipd_bill_cols = ["ipd_bill", "ipd_no", "ipd"]
    investigation_bill_cols = ["investigation_bill", "investigation", "lab_bill"]
    total_bill_cols = ["total", "total_bill", "total_amount", "total_paid"]
    final_status_cols = ["final_status", "deal_status", "status", "stage"]

    # Follow up columns
    fu1_date_cols = ["first_follow_up_date", "first_followup_date", "1st_follow_up_date", "follow_up_1_date"]
    fu1_remark_cols = ["first_follow_up_remark", "first_followup_remark", "1st_follow_up_remark", "remark_1", "follow_up_1_remark"]
    fu2_date_cols = ["second_follow_up_date", "second_followup_date", "2nd_follow_up_date", "follow_up_2_date"]
    fu2_remark_cols = ["second_follow_up_remark", "second_followup_remark", "2nd_follow_up_remark", "remark_2", "follow_up_2_remark"]

    # Excluded from survey/generic notes because they are recognized structured columns
    structured_col_keys = set(
        name_cols + phone_cols + email_cols + date_cols + platform_cols +
        attendant_cols + doctor_cols + gender_cols + address_cols + due_date_cols +
        uhid_cols + opd_done_date_cols + pharmacy_bill_cols + opd_bill_cols + ipd_bill_cols +
        investigation_bill_cols + total_bill_cols + final_status_cols +
        fu1_date_cols + fu1_remark_cols + fu2_date_cols + fu2_remark_cols +
        ["sr_no", "sr_no.", "campaign_name", "campaign", "patient_update"]
    )

    # Find survey / remark questions
    survey_cols = []
    for col in df.columns:
        c_clean = col_map.get(col, "")
        if c_clean in structured_col_keys:
            continue
        if "?" in str(col) or any(k in c_clean for k in ["pregnant", "query", "month", "समस्या", "रोग", "symptom", "problem"]):
            survey_cols.append(col)

    unknown_counter = 1

    for idx, row in df.iterrows():
        # Helper to extract clean string value
        def get_col_val(candidate_cols):
            for orig_col, clean_c in col_map.items():
                if (clean_c in candidate_cols or any(cand == clean_c for cand in candidate_cols)) and pd.notna(row.get(orig_col)):
                    s = str(row.get(orig_col)).strip()
                    if s and s.lower() not in ("nan", "none", "null", "-", "na", "nat"):
                        return s
            return ""

        # 1. Phone
        phone_val = ""
        for orig_col, clean_c in col_map.items():
            if clean_c in phone_cols and pd.notna(row.get(orig_col)):
                raw_phone = row.get(orig_col)
                phone_val = clean_phone_number(raw_phone)
                if phone_val:
                    break

        # 2. Email
        email_val = ""
        for orig_col, clean_c in col_map.items():
            if clean_c in email_cols and pd.notna(row.get(orig_col)):
                raw_email = str(row.get(orig_col)).strip()
                if "@" in raw_email:
                    if raw_email.lower().startswith("www."):
                        raw_email = raw_email[4:]
                    email_val = raw_email.lower()
                    break

        # 3. Name (Direct Column -> or Fallback to Email -> or Sequenced Unknown Patient)
        name_val = ""
        for orig_col, clean_c in col_map.items():
            if (clean_c in name_cols or any(nk in clean_c for nk in ["your_name", "full_name", "patient_name", "lead_name", "customer_name"])) and pd.notna(row.get(orig_col)):
                raw_n = str(row.get(orig_col)).strip()
                if raw_n and raw_n.lower() not in ("nan", "none", "null", "-", "na", "nat"):
                    name_val = raw_n
                    break

        # If name not found in name column, extract from email
        if not name_val and email_val:
            email_user = email_val.split("@")[0]
            clean_email_name = re.sub(r"[0-9_\.\-]+", " ", email_user).strip().title()
            if len(clean_email_name) >= 2:
                name_val = clean_email_name

        if not name_val:
            name_val = f"Unknown Patient {unknown_counter}"
            unknown_counter += 1

        # 4. Platform / Source
        platform_val = get_col_val(platform_cols)
        canonical_platform = "Meta Ads"
        plat_lower = platform_val.lower()
        if "ig" in plat_lower or "insta" in plat_lower:
            canonical_platform = "Instagram"
        elif "fb" in plat_lower or "face" in plat_lower:
            canonical_platform = "Facebook"
        elif "google" in plat_lower or "gads" in plat_lower:
            canonical_platform = "Google Ads"
        elif "whats" in plat_lower or "wa" in plat_lower:
            canonical_platform = "WhatsApp"
        elif "web" in plat_lower:
            canonical_platform = "Website"
        elif platform_val:
            canonical_platform = platform_val.title()

        # 5. Inquiry Date
        inquiry_date_val = timezone.localdate()
        raw_created_time = None
        for orig_col, clean_c in col_map.items():
            if clean_c in date_cols and pd.notna(row.get(orig_col)):
                raw_created_time = str(row.get(orig_col))
                inquiry_date_val = parse_flexible_date(row.get(orig_col))
                break

        # 6. City / Address
        city_val = get_col_val(address_cols)

        # 7. Course / Program
        course_name_val = ""
        for orig_col, clean_c in col_map.items():
            if any(k in clean_c for k in ["course", "service", "program", "specialization", "stream"]) and pd.notna(row.get(orig_col)):
                raw_crs = str(row.get(orig_col)).strip()
                if raw_crs and raw_crs.lower() not in ("nan", "none", "null", "-"):
                    course_name_val = raw_crs
                    break

        # 8. Lead Attendant & Doctor
        attendant_raw = get_col_val(attendant_cols)
        doctor_raw = get_col_val(doctor_cols)
        gender_raw = get_col_val(gender_cols)
        due_date_raw = get_col_val(due_date_cols)

        # 9. Follow-up 1 & 2 details
        fu1_date_raw = get_col_val(fu1_date_cols)
        fu1_remark_raw = get_col_val(fu1_remark_cols)
        fu2_date_raw = get_col_val(fu2_date_cols)
        fu2_remark_raw = get_col_val(fu2_remark_cols)

        # 10. Financial / Billing fields
        uhid_raw = get_col_val(uhid_cols)
        opd_done_date_raw = get_col_val(opd_done_date_cols)
        pharmacy_bill_raw = get_col_val(pharmacy_bill_cols)
        opd_bill_raw = get_col_val(opd_bill_cols)
        ipd_bill_raw = get_col_val(ipd_bill_cols)
        investigation_bill_raw = get_col_val(investigation_bill_cols)
        total_bill_raw = get_col_val(total_bill_cols)
        final_status_raw = get_col_val(final_status_cols)
        patient_update_raw = get_col_val(["patient_update", "update"])

        # 11. Survey questions & custom data
        survey_notes = []
        custom_data_survey = {}
        if city_val:
            custom_data_survey["city"] = city_val
            custom_data_survey["location"] = city_val
        if course_name_val:
            custom_data_survey["course"] = course_name_val
        if gender_raw:
            custom_data_survey["gender"] = gender_raw.upper() if gender_raw.lower() in ("m", "f", "male", "female") else gender_raw.title()
        if doctor_raw:
            custom_data_survey["doctor"] = doctor_raw
        if due_date_raw:
            custom_data_survey["due_date"] = due_date_raw
        if uhid_raw:
            custom_data_survey["uhid_id_no"] = uhid_raw
        if opd_done_date_raw:
            custom_data_survey["visit_date"] = str(parse_flexible_date(opd_done_date_raw))
        if pharmacy_bill_raw:
            try:
                custom_data_survey["pharmacy_bill"] = float(re.sub(r"[^\d.]", "", pharmacy_bill_raw))
            except Exception:
                pass
        if opd_bill_raw:
            try:
                custom_data_survey["opd_bill"] = float(re.sub(r"[^\d.]", "", opd_bill_raw))
            except Exception:
                pass
        if ipd_bill_raw:
            try:
                custom_data_survey["ipd_bill"] = float(re.sub(r"[^\d.]", "", ipd_bill_raw))
            except Exception:
                pass
        if investigation_bill_raw:
            custom_data_survey["investigation"] = investigation_bill_raw
        if total_bill_raw:
            try:
                custom_data_survey["total"] = float(re.sub(r"[^\d.]", "", total_bill_raw))
                custom_data_survey["total_paid"] = custom_data_survey["total"]
            except Exception:
                pass

        if final_status_raw:
            custom_data_survey["deal_status"] = final_status_raw
            custom_data_survey["done"] = final_status_raw

        # Follow-ups in custom_data
        if fu1_date_raw:
            custom_data_survey["calling_date_remark_1"] = str(parse_flexible_date(fu1_date_raw))
        if fu1_remark_raw:
            custom_data_survey["remark_1"] = fu1_remark_raw
        if fu2_date_raw:
            custom_data_survey["calling_date_remark_2"] = str(parse_flexible_date(fu2_date_raw))
        if fu2_remark_raw:
            custom_data_survey["remark_2"] = fu2_remark_raw

        if not custom_data_survey.get("priority"):
            custom_data_survey["priority"] = "Hot"

        # Survey questions (only truly unmapped questions)
        for s_col in survey_cols:
            s_val = row.get(s_col)
            if pd.notna(s_val) and str(s_val).strip():
                clean_val = str(s_val).strip().replace("_", " ")
                clean_label = s_col.replace("_", " ").title()
                survey_notes.append(f"{clean_label}: {clean_val}")
                custom_data_survey[s_col] = clean_val

        if patient_update_raw:
            survey_notes.append(f"Patient Update: {patient_update_raw}")

        notes_combined = "\n".join(survey_notes)

        # Raw Metadata dictionary
        raw_meta = {}
        for col in df.columns:
            val = row.get(col)
            if pd.notna(val):
                raw_meta[str(col)] = str(val)

        external_id = raw_meta.get('id') or raw_meta.get('ad_id') or raw_meta.get('lead_id') or raw_meta.get('Lead ID') or ""

        if not phone_val and name_val == "Unknown Patient":
            continue

        processed_rows.append({
            "row_index": idx + 1,
            "name": name_val,
            "mobile": phone_val,
            "email": email_val,
            "city": city_val,
            "course_name": course_name_val,
            "source_name": canonical_platform,
            "inquiry_date": str(inquiry_date_val),
            "created_time_str": raw_created_time or str(inquiry_date_val),
            "notes": notes_combined,
            "custom_data": custom_data_survey,
            "raw_metadata": raw_meta,
            "external_lead_id": external_id,
            "campaign_name": target_campaign.name if target_campaign else raw_meta.get('campaign_name', raw_meta.get('Form Name', '')),
            "attendant_raw": attendant_raw,
            "doctor_raw": doctor_raw,
            "gender_raw": gender_raw,
            "due_date_raw": due_date_raw,
            "final_status_raw": final_status_raw,
            "fu1_date": str(parse_flexible_date(fu1_date_raw)) if fu1_date_raw else None,
            "fu1_remark": fu1_remark_raw,
            "fu2_date": str(parse_flexible_date(fu2_date_raw)) if fu2_date_raw else None,
            "fu2_remark": fu2_remark_raw,
        })

    return processed_rows


def check_duplicates_in_db(lead_rows, hospital=None):
    """
    Checks each lead row against existing leads in database by 10-digit mobile.
    """
    mobiles = [r['mobile'] for r in lead_rows if r.get('mobile')]
    
    lead_qs = Lead.objects.filter(is_archived=False, mobile__in=mobiles)
    if hospital:
        lead_qs = lead_qs.filter(hospital=hospital)
    
    existing_by_mobile = {}
    for lead in lead_qs.select_related('stage', 'assigned_to', 'campaign'):
        if lead.mobile:
            existing_by_mobile[lead.mobile] = lead

    processed = []
    duplicate_count = 0
    new_count = 0

    for r in lead_rows:
        mobile = r.get('mobile')
        dup_lead = existing_by_mobile.get(mobile) if mobile else None
        
        row_copy = dict(r)
        if dup_lead:
            duplicate_count += 1
            row_copy['is_duplicate'] = True
            row_copy['existing_lead_id'] = dup_lead.pk
            row_copy['existing_lead_code'] = dup_lead.lead_code
            row_copy['existing_name'] = dup_lead.name
            row_copy['existing_stage'] = dup_lead.stage.name if dup_lead.stage else "New"
            row_copy['existing_assigned'] = dup_lead.assigned_to.get_full_name() if dup_lead.assigned_to else "Unassigned"
            row_copy['existing_campaign'] = dup_lead.campaign.name if dup_lead.campaign else "—"
            row_copy['existing_inquiry_date'] = str(dup_lead.inquiry_date)
            row_copy['duplicate_action'] = 'discard'
        else:
            new_count += 1
            row_copy['is_duplicate'] = False
            row_copy['duplicate_action'] = 'create'

        processed.append(row_copy)

    stats = {
        "total": len(lead_rows),
        "new_unique": new_count,
        "duplicates": duplicate_count,
    }
    return processed, stats


def generate_lead_code(hospital=None):
    """Generates unique lead code like NL-2026-000123 or LD-2026-000123."""
    year = timezone.now().year
    prefix = "NL-" if (hospital and "nelson" in hospital.name.lower()) else "LD-"
    full_prefix = f"{prefix}{year}-"
    
    last = Lead.objects.filter(lead_code__startswith=full_prefix).order_by("-lead_code").first()
    if last and last.lead_code:
        try:
            seq = int(last.lead_code.split("-")[-1]) + 1
        except Exception:
            seq = Lead.objects.count() + 1
    else:
        seq = 1
    return f"{full_prefix}{seq:06d}"
