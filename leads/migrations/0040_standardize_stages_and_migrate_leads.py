from django.db import migrations

def standardize_stages_and_migrate_leads(apps, schema_editor):
    Hospital = apps.get_model('accounts', 'Hospital')
    LeadStage = apps.get_model('leads', 'LeadStage')
    Lead = apps.get_model('leads', 'Lead')

    # Fetch Businesses
    nelson_hosp = Hospital.objects.filter(id=1).first()
    zappcode_acad = Hospital.objects.filter(id=7).first()

    # 1. STANDARDIZED HOSPITAL STAGES (13 STAGES)
    # Order, Name, BusinessType
    hospital_stage_specs = [
        (1, "New"),
        (2, "Assigned"),
        (3, "Follow Up Pending"),
        (4, "Follow Up Rescheduled"),
        (5, "Follow Up Done"),
        (6, "Appointment - Approval Pending"),
        (7, "Appointment - Confirmed"),
        (8, "Appointment Done"),
        (9, "IPD Booking"),
        (10, "IPD Done"),
        (11, "Payment Pending"),
        (12, "Payment Done"),
        (13, "Cancelled"),
    ]

    hospital_stage_map = {}
    if nelson_hosp:
        for order, name in hospital_stage_specs:
            st, _ = LeadStage.objects.get_or_create(
                hospital=nelson_hosp,
                name=name,
                defaults={
                    'order': order,
                    'is_active': True,
                    'business_type': 'HOSPITAL'
                }
            )
            # Ensure order and active status
            st.order = order
            st.is_active = True
            st.business_type = 'HOSPITAL'
            st.save(update_fields=['order', 'is_active', 'business_type'])
            hospital_stage_map[name.lower()] = st

    # 2. STANDARDIZED ACADEMY STAGES (12 STAGES)
    academy_stage_specs = [
        (1, "New"),
        (2, "Assigned"),
        (3, "Follow Up Pending"),
        (4, "Follow Up Rescheduled"),
        (5, "Follow Up Done"),
        (6, "Visit Planned"),
        (7, "Visit Done"),
        (8, "Admission Planned"),
        (9, "Admission Done"),
        (10, "Payment Pending"),
        (11, "Payment Done"),
        (12, "Cancelled"),
    ]

    academy_stage_map = {}
    if zappcode_acad:
        for order, name in academy_stage_specs:
            st, _ = LeadStage.objects.get_or_create(
                hospital=zappcode_acad,
                name=name,
                defaults={
                    'order': order,
                    'is_active': True,
                    'business_type': 'ACADEMY'
                }
            )
            st.order = order
            st.is_active = True
            st.business_type = 'ACADEMY'
            st.save(update_fields=['order', 'is_active', 'business_type'])
            academy_stage_map[name.lower()] = st

    # 3. MIGRATE HOSPITAL LEADS (ID: 1)
    if nelson_hosp:
        for lead in Lead.objects.filter(hospital=nelson_hosp).select_related('stage'):
            old_name = (lead.stage.name if lead.stage else "").strip().lower()
            target_st = None
            deal_st = "UNCONTACTED"

            if old_name in ("cancelled", "lost", "dropout", "rejected", "not interested") or "cancel" in old_name or "lost" in old_name:
                target_st = hospital_stage_map.get("cancelled")
                deal_st = "LOST"
            elif old_name in ("payment done", "complete", "completed", "payment completed") or "payment done" in old_name:
                target_st = hospital_stage_map.get("payment done")
                deal_st = "WON"
            elif old_name in ("payment pending", "payment"):
                target_st = hospital_stage_map.get("payment pending")
                deal_st = "WON"
            elif old_name in ("ipd done", "admission done"):
                target_st = hospital_stage_map.get("ipd done")
                deal_st = "WON"
            elif old_name in ("ipd booking", "admission pending"):
                target_st = hospital_stage_map.get("ipd booking")
                deal_st = "INTERESTED"
            elif old_name in ("appointment done", "appointment completed", "visited"):
                target_st = hospital_stage_map.get("appointment done")
                deal_st = "WON"
            elif old_name in ("appointment - confirmed", "appointment confirmed", "booking confirmed"):
                target_st = hospital_stage_map.get("appointment - confirmed")
                deal_st = "INTERESTED"
            elif old_name in ("appointment - approval pending", "awaiting approval from doctor", "booking"):
                target_st = hospital_stage_map.get("appointment - approval pending")
                deal_st = "INTERESTED"
            elif old_name in ("follow up - completed", "follow up completed", "follow up done", "counselling done"):
                target_st = hospital_stage_map.get("follow up done")
                deal_st = "INTERESTED"
            elif old_name in ("follow up - rescheduled", "rescheduled"):
                target_st = hospital_stage_map.get("follow up rescheduled")
                deal_st = "INTERESTED"
            elif old_name in ("follow up - pending", "follow up pending", "follow up", "follow-up", "contacted", "interested"):
                target_st = hospital_stage_map.get("follow up pending")
                deal_st = "CONTACTED"
            elif old_name in ("call not done", "assigned"):
                target_st = hospital_stage_map.get("assigned")
                deal_st = "UNCONTACTED"
            else: # new, fresh, test, or None
                target_st = hospital_stage_map.get("new")
                deal_st = "UNCONTACTED"

            if target_st:
                lead.stage = target_st
                lead.deal_status = deal_st
                lead.save(update_fields=['stage', 'deal_status'])

    # 4. MIGRATE ACADEMY LEADS (ID: 7)
    if zappcode_acad:
        for lead in Lead.objects.filter(hospital=zappcode_acad).select_related('stage'):
            old_name = (lead.stage.name if lead.stage else "").strip().lower()
            target_st = None
            deal_st = "UNCONTACTED"

            if old_name in ("cancelled", "lost", "dropout", "rejected", "not interested") or "cancel" in old_name or "lost" in old_name:
                target_st = academy_stage_map.get("cancelled")
                deal_st = "LOST"
            elif old_name in ("payment done", "admission & payment", "complete", "won"):
                target_st = academy_stage_map.get("payment done")
                deal_st = "WON"
            elif old_name in ("payment pending",):
                target_st = academy_stage_map.get("payment pending")
                deal_st = "WON"
            elif old_name in ("admission done",):
                target_st = academy_stage_map.get("admission done")
                deal_st = "WON"
            elif old_name in ("admission planned", "admission pending"):
                target_st = academy_stage_map.get("admission planned")
                deal_st = "INTERESTED"
            elif old_name in ("visit done", "visited", "counselling done"):
                target_st = academy_stage_map.get("visit done")
                deal_st = "WON"
            elif old_name in ("visit planned",):
                target_st = academy_stage_map.get("visit planned")
                deal_st = "INTERESTED"
            elif old_name in ("follow up completed", "follow up done"):
                target_st = academy_stage_map.get("follow up done")
                deal_st = "INTERESTED"
            elif old_name in ("follow up rescheduled", "rescheduled"):
                target_st = academy_stage_map.get("follow up rescheduled")
                deal_st = "INTERESTED"
            elif old_name in ("follow up - pending", "follow up pending", "follow up needed", "follow up", "contacted"):
                target_st = academy_stage_map.get("follow up pending")
                deal_st = "CONTACTED"
            elif old_name in ("call not done", "assigned"):
                target_st = academy_stage_map.get("assigned")
                deal_st = "UNCONTACTED"
            else:
                target_st = academy_stage_map.get("new")
                deal_st = "UNCONTACTED"

            if target_st:
                lead.stage = target_st
                lead.deal_status = deal_st
                lead.save(update_fields=['stage', 'deal_status'])

    # 5. DEACTIVATE OLD OBSOLETE GLOBAL STAGES (hospital_id=None or redundant)
    active_new_stage_ids = list(hospital_stage_map.values()) + list(academy_stage_map.values())
    active_ids = [st.id for st in active_new_stage_ids if st]
    LeadStage.objects.exclude(id__in=active_ids).update(is_active=False)

def reverse_standardize_stages(apps, schema_editor):
    pass

class Migration(migrations.Migration):

    dependencies = [
        ('leads', '0039_add_uncontacted_deal_status'),
    ]

    operations = [
        migrations.RunPython(standardize_stages_and_migrate_leads, reverse_standardize_stages),
    ]
