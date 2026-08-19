import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zappcodecrm.settings")
django.setup()

from leads.models import Lead, MasterGroup, MasterItem
from accounts.models import Hospital

def run():
    # Assuming there is a Nelson hospital
    nelson = Hospital.objects.filter(name__icontains="nelson").first()
    if not nelson:
        print("Nelson hospital not found")
        return

    leads = Lead.objects.filter(hospital=nelson)
    print(f"Found {leads.count()} leads for {nelson.name}")

    groups = {
        "Campaigns": MasterGroup.objects.get_or_create(name="Campaigns", defaults={"description": "Marketing Campaigns"})[0],
        "Lead Sources": MasterGroup.objects.get_or_create(name="Lead Sources", defaults={"description": "Sources of Leads"})[0],
        "Deal Statuses": MasterGroup.objects.get_or_create(name="Deal Statuses", defaults={"description": "Lead Status / Temperature"})[0],
    }

    # Extract distinct values
    campaigns = set(leads.exclude(campaign__isnull=True).values_list("campaign__name", flat=True))
    sources = set(leads.exclude(lead_source__isnull=True).values_list("lead_source__name", flat=True))
    statuses = set(leads.exclude(deal_status="").values_list("deal_status", flat=True))

    # Fallbacks / Defaults if none exist
    if not statuses:
        statuses = {"OPEN", "WON", "LOST", "HOLD"}

    # Create MasterItems
    for name in campaigns:
        MasterItem.objects.get_or_create(group=groups["Campaigns"], hospital=nelson, name=name)
        print(f"Created Campaign: {name}")

    for name in sources:
        MasterItem.objects.get_or_create(group=groups["Lead Sources"], hospital=nelson, name=name)
        print(f"Created Lead Source: {name}")
        
    for st in statuses:
        # Convert internal status name to human readable? e.g. "OPEN" -> "Open"
        MasterItem.objects.get_or_create(group=groups["Deal Statuses"], hospital=nelson, name=st.title())
        print(f"Created Deal Status: {st.title()}")
        
    # Now, migrate data to custom_data for existing leads
    migrated_count = 0
    for lead in leads:
        cd = lead.custom_data or {}
        modified = False
        
        if lead.campaign and lead.campaign.name and 'campaign' not in cd:
            cd['campaign'] = lead.campaign.name
            modified = True
            
        if lead.lead_source and lead.lead_source.name and 'lead_source' not in cd:
            cd['lead_source'] = lead.lead_source.name
            modified = True
            
        if lead.deal_status and 'deal_status' not in cd:
            cd['deal_status'] = lead.deal_status.title()
            modified = True
            
        if modified:
            lead.custom_data = cd
            lead.save(update_fields=['custom_data'])
            migrated_count += 1
            
    print(f"Migrated data for {migrated_count} leads")

if __name__ == "__main__":
    run()
