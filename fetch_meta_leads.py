import zipfile
import requests
import json
import pandas as pd
from datetime import datetime

# Read token from backup zip
with zipfile.ZipFile(r'F:\untitled folder\zappcodecrm_backup.zip', 'r') as z:
    token = z.read('new_page_token.txt').decode('utf-8').strip()

page_id = '1001555886574980'
forms_url = f'https://graph.facebook.com/v19.0/{page_id}/leadgen_forms?access_token={token}&limit=100'
forms = requests.get(forms_url).json().get('data', [])

all_leads = []

print(f"Checking {len(forms)} forms from Meta Page...")

for f in forms:
    fid = f['id']
    fname = f['name']
    
    url = f'https://graph.facebook.com/v19.0/{fid}/leads?access_token={token}&limit=250'
    form_lead_count = 0
    while url:
        res = requests.get(url).json()
        data = res.get('data', [])
        for item in data:
            form_lead_count += 1
            lead_id = item.get('id')
            created_time = item.get('created_time')
            
            lead_row = {
                'Lead ID': lead_id,
                'Created Time': created_time,
                'Form Name': fname,
                'Full Name': '',
                'Phone': '',
                'Email': '',
                'City': '',
                'Other Details': []
            }
            
            for field in item.get('field_data', []):
                name = field.get('name', '').lower()
                vals = field.get('values', [])
                val_str = vals[0] if vals else ''
                
                if any(k in name for k in ['full_name', 'name']) and not lead_row['Full Name']:
                    lead_row['Full Name'] = val_str
                elif any(k in name for k in ['phone', 'mobile']):
                    lead_row['Phone'] = val_str
                elif 'email' in name:
                    lead_row['Email'] = val_str
                elif 'city' in name:
                    lead_row['City'] = val_str
                else:
                    field_title = field.get('name', 'detail')
                    lead_row['Other Details'].append(f"{field_title}: {val_str}")
            
            lead_row['Other Details'] = " | ".join(lead_row['Other Details'])
            all_leads.append(lead_row)
            
        url = res.get('paging', {}).get('next')
    
    if form_lead_count > 0:
        print(f"  [+] {fname}: {form_lead_count} leads fetched")

df = pd.DataFrame(all_leads)
print(f"\n==========================================")
print(f"Total Meta Leads Extracted: {len(df)}")
print(f"==========================================")

output_excel = r'F:\untitled folder\zappcodecrm\Zappcode_Academy_All_Meta_Leads.xlsx'
df.to_excel(output_excel, index=False)
print(f"Excel saved successfully: {output_excel}")

# Also save a clean JSON file for fast inspection if needed
output_json = r'F:\untitled folder\zappcodecrm\meta_leads_preview.json'
df.head(20).to_json(output_json, orient='records', indent=2)
print(f"Preview JSON saved: {output_json}")

