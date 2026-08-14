#!/usr/bin/env python
"""
Import All_Leads_Combined.xlsx into the LeadCRM database.
Run via: python import_leads_script.py
"""
import os, sys, django, numpy as np, pandas as pd
from pathlib import Path

# ── Django setup ───────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.utils import timezone
from leads.models import Lead, LeadSource, SourceCategory, Course, LeadStage

# ── Config ─────────────────────────────────────────────────────────────────────
XLSX_PATH = (
    '/Users/adarshwahewal/Library/Application Support/Claude/'
    'local-agent-mode-sessions/d9cf44f0-95d9-456d-b079-9a7a1b5bdd29/'
    'acb847cd-33f7-451a-aeb2-9823d44c7ccf/'
    'local_7f15892f-be96-4612-90bd-c943ca4c771b/outputs/'
    'Leads_Cleaned_Monthwise/All_Leads_Combined.xlsx'
)

# ── Helpers ────────────────────────────────────────────────────────────────────
def safe(val):
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return ''
    return str(val).strip()

def get_or_create_obj(model, cache, name, extra_defaults=None):
    if not name:
        return None
    key = name.lower()
    if key not in cache:
        defaults = extra_defaults or {}
        obj, _ = model.objects.get_or_create(name=name, defaults=defaults)
        cache[key] = obj
    return cache[key]

# ── Pre-load master caches ─────────────────────────────────────────────────────
course_cache   = {c.name.lower(): c for c in Course.objects.all()}
src_cat_cache  = {s.name.lower(): s for s in SourceCategory.objects.all()}
src_cache      = {s.name.lower(): s for s in LeadSource.objects.all()}
stage_cache    = {s.name.lower(): s for s in LeadStage.objects.all()}

def get_stage(name):
    return get_or_create_obj(LeadStage, stage_cache, name)

def map_temp_and_stage(status_val):
    s = safe(status_val).lower()
    if 'hot' in s:
        return 'HOT', get_stage('Interested')
    if 'warm' in s:
        return 'WARM', get_stage('Follow-up')
    if 'cold' in s:
        return 'COLD', get_stage('Contacted')
    if 'call not' in s:
        return 'COLD', get_stage('New')
    return '', get_stage('New')

# ── Load xlsx ──────────────────────────────────────────────────────────────────
print(f"Loading {XLSX_PATH} ...")
df = pd.read_excel(XLSX_PATH)
print(f"Rows loaded: {len(df)}")

# ── Build Lead objects ─────────────────────────────────────────────────────────
leads_to_create = []
skipped = 0

for idx, row in df.iterrows():
    name   = safe(row.get('Name'))
    mobile = safe(row.get('Mobile'))

    if not name and not mobile:
        skipped += 1
        continue

    # Inquiry date
    raw_date = row.get('Inquiry Date')
    try:
        inq_date = pd.to_datetime(raw_date).date() if pd.notna(raw_date) else timezone.localdate()
    except Exception:
        inq_date = timezone.localdate()

    # Last followup date
    raw_lfd = row.get('Last Follow-up Date')
    try:
        last_fu = pd.to_datetime(raw_lfd).date() if pd.notna(raw_lfd) else None
    except Exception:
        last_fu = None

    # Course
    course_name = safe(row.get('Course'))
    course_obj  = get_or_create_obj(Course, course_cache, course_name) if course_name else None

    # Source category
    src_cat_name = safe(row.get('Source Category'))
    src_cat_obj  = get_or_create_obj(SourceCategory, src_cat_cache, src_cat_name) if src_cat_name else None

    # Lead source (link to src_cat)
    src_name = safe(row.get('Lead Source'))
    src_obj  = None
    if src_name:
        key = src_name.lower()
        if key not in src_cache:
            obj, _ = LeadSource.objects.get_or_create(
                name=src_name,
                defaults={'source_category': src_cat_obj}
            )
            src_cache[key] = obj
        src_obj = src_cache[key]

    # Temperature & stage
    temperature, stage_obj = map_temp_and_stage(row.get('Status'))

    # Deal status
    deal_raw = safe(row.get('Deal Status')).lower()
    if 'closed' in deal_raw:
        deal_status = 'CLOSED'
    else:
        deal_status = 'OPEN'

    # Follow-up count
    try:
        fu_count = int(row.get('Follow-up Count', 0) or 0)
    except:
        fu_count = 0

    # Notes / last comment
    last_comment = safe(row.get('Last Comment'))
    notes_text   = safe(row.get('Notes'))

    leads_to_create.append(Lead(
        name=name or 'Unknown',
        mobile=mobile,
        city=safe(row.get('City')),
        location=safe(row.get('Area / Locality')),
        course=course_obj,
        source_category=src_cat_obj,
        lead_source=src_obj,
        stage=stage_obj,
        temperature=temperature,
        deal_status=deal_status,
        inquiry_date=inq_date,
        last_followup_date=last_fu,
        followup_count=fu_count,
        notes=f"{last_comment}\n{notes_text}".strip() if (last_comment or notes_text) else '',
        is_archived=False,
        import_source_file='All_Leads_Combined.xlsx',
        import_source_row=idx + 2,
    ))

print(f"Skipped {skipped} blank rows. Creating {len(leads_to_create)} leads...")

# Assign unique lead codes before inserting
import random, string
def gen_code():
    return 'ZK' + ''.join(random.choices(string.digits, k=6))

used_codes = set()
BATCH = 100
created = 0

for i in range(0, len(leads_to_create), BATCH):
    batch = leads_to_create[i:i+BATCH]
    for lead in batch:
        code = gen_code()
        while code in used_codes:
            code = gen_code()
        used_codes.add(code)
        lead.lead_code = code
    Lead.objects.bulk_create(batch, batch_size=BATCH)
    created += len(batch)
    print(f"  Inserted {created}/{len(leads_to_create)}")

total = Lead.objects.count()
print(f"\n✅ Done! Total leads in DB: {total}")
