import os
import django
import pandas as pd
from datetime import datetime
import math

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from leads.models import Lead, Course, LeadStage
from accounts.models import Hospital
from followups.models import FollowUp

def clean_val(val):
    if pd.isna(val) or val == 'nan':
        return ""
    if isinstance(val, float) and math.isnan(val):
        return ""
    return str(val).strip()

def clean_date(val):
    if pd.isna(val):
        return None
    if isinstance(val, datetime):
        return val.date()
    try:
        return pd.to_datetime(val).date()
    except:
        return None

def import_data():
    file_path_local = 'F:/NELSON LEAD  (5).xlsx'
    file_path_server = '/var/www/zappcodecrm/NELSON_LEAD.xlsx'
    
    file_path = file_path_local if os.path.exists(file_path_local) else file_path_server
    
    print(f"Reading {file_path}...")
    df = pd.read_excel(file_path)
    
    # Get or create Nelson Hospital
    hospital, _ = Hospital.objects.get_or_create(
        name="Nelson Hospital", 
        defaults={'phone': '0000000000', 'address': 'Nagpur'}
    )
    
    # Default Stage
    stage, _ = LeadStage.objects.get_or_create(name="New", defaults={'order': 1})
    
    count = 0
    for index, row in df.iterrows():
        name = clean_val(row.get('PAITENT NAME', 'Unknown'))
        if not name or name.lower() == 'nan':
            continue
            
        mobile = clean_val(row.get('MOBILE NO', ''))
        # Ensure mobile is digits only and truncated to 15 chars
        mobile = ''.join(filter(str.isdigit, mobile))[:15]
        if not mobile:
            mobile = "0000000000"
            
        city = clean_val(row.get('LOCATION', ''))
        department_name = clean_val(row.get('DEPARTMENT', ''))
        
        # Get or create course/department
        course = None
        if department_name:
            course, _ = Course.objects.get_or_create(name=department_name)
            
        inquiry_date = clean_date(row.get('DATE'))
        if not inquiry_date:
            inquiry_date = datetime.now().date()
        
        campaign_name = clean_val(row.get('CAMPAIGN NAME', ''))
        
        # Create Lead
        lead = Lead.objects.create(
            name=name,
            mobile=mobile,
            city=city,
            course=course,
            stage=stage,
            inquiry_date=inquiry_date,
            hospital=hospital
        )
        
        # Create Nelson Data using proper model
        from leads.models import NelsonLeadData
        NelsonLeadData.objects.create(
            lead=lead,
            nelson_dantoli=clean_val(row.get('NELSON DANTOLI')),
            gender=clean_val(row.get('FEMALE')),
            age=clean_val(row.get('AGE')),
            department=clean_val(row.get('DEPARTMENT')),
            doctor=clean_val(row.get('DOCOTOR')),
            appo_book=clean_val(row.get('APPO.BOOK')),
            appo_booked_date=clean_date(row.get('APPO.BOOKED DATE')),
            remark_1=clean_val(row.get('REMARK:1')),
            remark_2=clean_val(row.get('REMARK:2')),
            remark_3=clean_val(row.get('REMARK:3')),
            uhid_id_no=clean_val(row.get('UHID ID NO')),
            ipd_no=clean_val(row.get('IPD NO')),
            investigation=clean_val(row.get('INVESTIGATION ')),
            pharmacy_bill=0 if not str(clean_val(row.get('PHARMACY BILL'))).isdigit() else clean_val(row.get('PHARMACY BILL')),
            opd_bill=0 if not str(clean_val(row.get('OPD BILL'))).isdigit() else clean_val(row.get('OPD BILL')),
            total=0 if not str(clean_val(row.get('TOTAL'))).isdigit() else clean_val(row.get('TOTAL')),
        )
        
        # Process FollowUps (Remarks)
        for i in range(1, 4):
            remark_date = clean_date(row.get(f'CALLING DATE REMARK {i}'))
            remark_text = clean_val(row.get(f'REMARK:{i}'))
            if remark_text:
                FollowUp.objects.create(
                    lead=lead,
                    followup_mode='CALL',
                    comment=f"Remark {i}: {remark_text}",
                    followup_date=remark_date if remark_date else datetime.now().date(),
                    followup_status='COMPLETED'
                )
                
        count += 1
        if count % 100 == 0:
            print(f"Processed {count} leads...")
            
    print(f"Successfully imported {count} leads and their follow-ups into Nelson Hospital!")

if __name__ == '__main__':
    import_data()
