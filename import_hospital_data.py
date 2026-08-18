import os
import django
import pandas as pd
import math

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zappcodecrm.settings")
django.setup()

from accounts.models import Hospital
from leads.models import Lead, NelsonLeadData, LeadSource, Campaign

def clean_val(val):
    if pd.isna(val) or val == 'nan':
        return None
    return str(val).strip()

def run_import():
    file_path = r"C:\Users\DELL-PC\Desktop\NELSON\nelson_data_collection\hospital_crm_dummy_data_500_rows.xlsx"
    df = pd.read_excel(file_path)
    
    hospital = Hospital.objects.filter(name__icontains="Nelson").first()
    if not hospital:
        print("Hospital not found!")
        return

    print(f"Importing {len(df)} rows into {hospital.name}...")

    leads_created = 0
    for idx, row in df.iterrows():
        name = clean_val(row.get('PAITENT NAME')) or f"Patient {idx}"
        mobile = clean_val(row.get('MOBILE NO')) or f"0000000{idx}"
        city = clean_val(row.get('LOCATION')) or ""
        gender = clean_val(row.get('GENDER')) or ""
        
        source_name = clean_val(row.get('SOURCE')) or "Unknown"
        campaign_name = clean_val(row.get('CAMPAIGN NAME')) or "Unknown"
        
        from leads.models import SourceCategory, LeadStage
        default_cat = SourceCategory.objects.first()
        default_stage = LeadStage.objects.first()
        
        source, _ = LeadSource.objects.get_or_create(name=source_name, defaults={'category': default_cat})
        campaign, _ = Campaign.objects.get_or_create(name=campaign_name)

        # Create Lead
        lead, created = Lead.objects.get_or_create(
            mobile=mobile,
            defaults={
                'name': name,
                'city': city,
                'lead_source': source,
                'campaign': campaign,
                'hospital': hospital,
                'stage': default_stage
            }
        )
        
        if created:
            leads_created += 1

        # Create or update NelsonLeadData
        NelsonLeadData.objects.update_or_create(
            lead=lead,
            defaults={
                'gender': gender,
                'age': row.get('AGE') if not pd.isna(row.get('AGE')) else None,
                'doctor': clean_val(row.get('DOCOTOR')) or "",
                'department': clean_val(row.get('DEPARTMENT')) or "",
                'appo_book': clean_val(row.get('APPO.BOOK')) or "",
                'priority': clean_val(row.get('PRIORITY')) or "",
                'total': row.get('TOTAL') if not pd.isna(row.get('TOTAL')) else 0.0,
                'done': clean_val(row.get('DONE')) or "No"
            }
        )

    print(f"Successfully imported and created {leads_created} new leads!")

run_import()
