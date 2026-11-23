
import json
from leads.models import Lead, LeadSource, Campaign, LeadStage
from django.contrib.auth import get_user_model
from django.db.models import Q
from decimal import Decimal

User = get_user_model()
HOSPITAL_ID = 1

# Ensure LeadSource 'Google My Business' exists for Hospital
gmb_source, _ = LeadSource.objects.get_or_create(
    hospital_id=HOSPITAL_ID,
    name='Google My Business',
    defaults={'category': 'Organic', 'is_active': True}
)
if gmb_source.category != 'Organic':
    gmb_source.category = 'Organic'
    gmb_source.save()

# Ensure Campaign 'Google My Business Organic' exists
camp, _ = Campaign.objects.get_or_create(
    hospital_id=HOSPITAL_ID,
    name='Google My Business Organic',
    defaults={'is_active': True}
)

records = [{"name": "MAST.KAAIRAV SINGH", "mobile": "7091903313", "owner": "Payal", "stage": "Payment Done", "status": "WON", "city": "Nagpur", "doctor": "Dr. Kinkar", "ipd": 0.0, "opd": 700.0, "rev": 1062.0, "uhid": "UHID-221142275"}, {"name": "MAST.KAMRAN KHAN", "mobile": "8319437394", "owner": "Roshni dhantoli hosp", "stage": "Payment Done", "status": "WON", "city": "Chhindwara", "doctor": "Dr. Banait", "ipd": 0.0, "opd": 700.0, "rev": 3090.0, "uhid": "UHID-221142286"}, {"name": "MR.RAMJI SAHU", "mobile": "8964922120", "owner": "Payal", "stage": "Payment Done", "status": "WON", "city": "Chhindwara", "doctor": "Dr. Maheshkar", "ipd": 0.0, "opd": 800.0, "rev": 4725.0, "uhid": "UHID-221142322"}, {"name": "MAST.SAURYAMAN SINGH", "mobile": "7415714843", "owner": "Roshni dhantoli hosp", "stage": "Assigned", "status": "OPEN", "city": "Saoner", "doctor": "Dr. Meshram", "ipd": 0.0, "opd": 0.0, "rev": 0.0, "uhid": ""}, {"name": "MRS.ASHANA KHAN", "mobile": "8839906609", "owner": "Payal", "stage": "Payment Done", "status": "WON", "city": "Betul", "doctor": "Dr. Shinde", "ipd": 0.0, "opd": 800.0, "rev": 2714.0, "uhid": "UHID-260600139"}, {"name": "MRS.ARYA RAWLAMI", "mobile": "9893767999", "owner": "Roshni dhantoli hosp", "stage": "Payment Done", "status": "WON", "city": "Nagpur", "doctor": "Dr. Bhute", "ipd": 0.0, "opd": 350.0, "rev": 350.0, "uhid": "UHID-221142435"}, {"name": "MRS.SAMIKSHA WANKHEDE", "mobile": "8485096073", "owner": "Roshni dhantoli hosp", "stage": "Lost", "status": "LOST", "city": "Nagpur", "doctor": "Dr. Rewanwar", "ipd": 0.0, "opd": 0.0, "rev": 0.0, "uhid": ""}, {"name": "MAST.NAKUL SEN", "mobile": "8817689476", "owner": "Roshni dhantoli hosp", "stage": "Payment Done", "status": "WON", "city": "Sagar", "doctor": "Dr. Bandarkar", "ipd": 0.0, "opd": 800.0, "rev": 800.0, "uhid": "UHID-260600154"}, {"name": "MAST AYANSH CHOKSE", "mobile": "9669813170", "owner": "Payal", "stage": "Lost", "status": "LOST", "city": "", "doctor": "Dr. Kinkar", "ipd": 0.0, "opd": 0.0, "rev": 0.0, "uhid": ""}, {"name": "MR.AKASH TIWARI", "mobile": "8793653561", "owner": "Roshni dhantoli hosp", "stage": "Payment Pending", "status": "OPEN", "city": "", "doctor": "Dr. Sabu", "ipd": 0.0, "opd": 0.0, "rev": 0.0, "uhid": ""}, {"name": "MRS.DIPALI JUGE", "mobile": "9766210130", "owner": "Roshni dhantoli hosp", "stage": "Payment Done", "status": "WON", "city": "", "doctor": "Dr. Marar", "ipd": 37260.0, "opd": 700.0, "rev": 45326.0, "uhid": "UHID-221142638"}, {"name": "MAST.BHUMIK CHOURY", "mobile": "9055088959", "owner": "Roshni dhantoli hosp", "stage": "Payment Done", "status": "WON", "city": "Chhindwara", "doctor": "Dr. Thakre", "ipd": 0.0, "opd": 700.0, "rev": 700.0, "uhid": "UHID-221142678"}, {"name": "MR.NARAYAN RAO SABLE", "mobile": "9522771771", "owner": "Roshni dhantoli hosp", "stage": "Payment Done", "status": "WON", "city": "Betul", "doctor": "Dr. Kinkar", "ipd": 0.0, "opd": 800.0, "rev": 1816.0, "uhid": "UHID-221142713"}, {"name": "MRS.MADHURI SABLE", "mobile": "9522771771", "owner": "Roshni dhantoli hosp", "stage": "Payment Done", "status": "WON", "city": "Betul", "doctor": "Dr. Kinkar", "ipd": 0.0, "opd": 800.0, "rev": 946.0, "uhid": "UHID-221142714"}, {"name": "MAST.AAYAN SINGH", "mobile": "7723023554", "owner": "Payal", "stage": "Payment Done", "status": "WON", "city": "", "doctor": "Dr. Khadse", "ipd": 0.0, "opd": 1500.0, "rev": 5183.0, "uhid": "UHID-221142705"}, {"name": "MRS.PRITI KURLE", "mobile": "9156888619", "owner": "Roshni dhantoli hosp", "stage": "Assigned", "status": "OPEN", "city": "", "doctor": "Dr. Bhute", "ipd": 0.0, "opd": 0.0, "rev": 0.0, "uhid": ""}, {"name": "MRS.VAISHANVI BURDKAR", "mobile": "8149710861", "owner": "Roshni dhantoli hosp", "stage": "Cancelled", "status": "LOST", "city": "", "doctor": "Dr. Shinde", "ipd": 0.0, "opd": 0.0, "rev": 0.0, "uhid": ""}, {"name": "MRS.SHIVANI KOHLE", "mobile": "8087207115", "owner": "Roshni dhantoli hosp", "stage": "Payment Done", "status": "WON", "city": "", "doctor": "Dr. Bhute", "ipd": 0.0, "opd": 700.0, "rev": 6771.0, "uhid": "UHID-221142855"}, {"name": "MAST.KEYUSH DESHMUKH", "mobile": "8888226118", "owner": "Roshni dhantoli hosp", "stage": "Payment Done", "status": "WON", "city": "", "doctor": "Dr. Azad", "ipd": 7821.0, "opd": 700.0, "rev": 11763.0, "uhid": "UHID-221142870"}, {"name": "MRS.VIPASHYANA  KAMBLE", "mobile": "9699981931", "owner": "Roshni dhantoli hosp", "stage": "Payment Done", "status": "WON", "city": "", "doctor": "Dr. Bhute", "ipd": 0.0, "opd": 350.0, "rev": 350.0, "uhid": "UHID-221142933"}, {"name": "MAST.ARYAWARDAN PATHAK", "mobile": "9359389244", "owner": "Roshni dhantoli hosp", "stage": "Payment Done", "status": "WON", "city": "", "doctor": "Dr. Khadse", "ipd": 0.0, "opd": 1200.0, "rev": 2464.0, "uhid": "UHID-221143060"}, {"name": "MS. KANIKA PATWARI", "mobile": "9131615070", "owner": "Roshni dhantoli hosp", "stage": "Payment Done", "status": "WON", "city": "", "doctor": "Dr. Devraj", "ipd": 0.0, "opd": 1400.0, "rev": 23954.0, "uhid": "UHID-221143160"}, {"name": "MRS.YERPUDE", "mobile": "9767400239", "owner": "Roshni dhantoli hosp", "stage": "Assigned", "status": "OPEN", "city": "", "doctor": "Not Assigned", "ipd": 0.0, "opd": 0.0, "rev": 0.0, "uhid": ""}, {"name": "MAST.RIYANSH GEDAM", "mobile": "9637797144", "owner": "Roshni dhantoli hosp", "stage": "Payment Done", "status": "WON", "city": "Bivapur", "doctor": "Dr. Wagh", "ipd": 0.0, "opd": 1100.0, "rev": 1100.0, "uhid": "UHID-221143553"}, {"name": "MRS.RUCHI DUBEY", "mobile": "7774038191", "owner": "Roshni dhantoli hosp", "stage": "Assigned", "status": "OPEN", "city": "", "doctor": "Dr. Devraj", "ipd": 0.0, "opd": 0.0, "rev": 0.0, "uhid": ""}, {"name": "MRS.NISHA MESHRAM", "mobile": "7972471679", "owner": "Roshni dhantoli hosp", "stage": "Payment Done", "status": "WON", "city": "", "doctor": "Dr. Marar", "ipd": 0.0, "opd": 1200.0, "rev": 1200.0, "uhid": "UHID-221134471"}, {"name": "B/O OF CHANDNI RATHOD", "mobile": "6263776556", "owner": "Roshni dhantoli hosp", "stage": "Payment Done", "status": "WON", "city": "Jabhalpur", "doctor": "Not Assigned", "ipd": 158560.0, "opd": 0.0, "rev": 188838.0, "uhid": "UHID-221143653"}, {"name": "MRS. PRIYA KANFADE", "mobile": "8055477908", "owner": "Pallavi", "stage": "Assigned", "status": "OPEN", "city": "", "doctor": "Not Assigned", "ipd": 0.0, "opd": 0.0, "rev": 0.0, "uhid": ""}]

updated = 0
for r in records:
    name = r['name']
    mob = r['mobile']
    owner_str = r['owner']
    stage_str = r['stage']
    city = r['city']
    doctor = r['doctor']
    ipd = r['ipd']
    opd = r['opd']
    rev = r['rev']
    uhid = r['uhid']

    # Find lead by name in hospital_id=1
    qs = Lead.objects.filter(hospital_id=HOSPITAL_ID, full_name__iexact=name)
    if not qs.exists():
        # try first name / contains
        qs = Lead.objects.filter(hospital_id=HOSPITAL_ID, full_name__icontains=name.split()[0])
    
    if qs.exists():
        lead = qs.first()
        lead.phone = mob
        lead.lead_source = gmb_source
        lead.campaign = camp
        lead.city = city
        if doctor:
            lead.doctor_name = doctor
        
        # telecaller assignment
        if owner_str:
            u_qs = User.objects.filter(
                Q(username__icontains=owner_str.split()[0]) |
                Q(first_name__icontains=owner_str.split()[0])
            )
            if u_qs.exists():
                lead.assigned_to = u_qs.first()
        
        # stage
        if stage_str:
            s_qs = LeadStage.objects.filter(hospital_id=HOSPITAL_ID, name__iexact=stage_str)
            if not s_qs.exists():
                s_qs = LeadStage.objects.filter(hospital_id=HOSPITAL_ID, name__icontains=stage_str)
            if s_qs.exists():
                lead.stage = s_qs.first()
        
        if not lead.custom_data:
            lead.custom_data = {}
        if ipd:
            lead.custom_data['ipd_bill'] = float(ipd)
        if opd:
            lead.custom_data['opd_bill'] = float(opd)
        if rev:
            lead.custom_data['total_revenue'] = float(rev)
        if uhid:
            lead.custom_data['uhid'] = uhid
            
        lead.save()
        updated += 1

print(f'Successfully updated {updated} leads for Hospital ID {HOSPITAL_ID}')
