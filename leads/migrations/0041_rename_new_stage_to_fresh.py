from django.db import migrations

def rename_new_to_fresh(apps, schema_editor):
    LeadStage = apps.get_model('leads', 'LeadStage')
    # Update Hospital New -> Fresh
    LeadStage.objects.filter(hospital_id=1, name__iexact='New').update(name='Fresh')
    # Update Academy New -> Fresh
    LeadStage.objects.filter(hospital_id=7, name__iexact='New').update(name='Fresh')
    # Update any global active New -> Fresh
    LeadStage.objects.filter(name__iexact='New', is_active=True).update(name='Fresh')

def reverse_rename(apps, schema_editor):
    LeadStage = apps.get_model('leads', 'LeadStage')
    LeadStage.objects.filter(hospital_id=1, name__iexact='Fresh').update(name='New')
    LeadStage.objects.filter(hospital_id=7, name__iexact='Fresh').update(name='New')
    LeadStage.objects.filter(name__iexact='Fresh', is_active=True).update(name='New')

class Migration(migrations.Migration):

    dependencies = [
        ('leads', '0040_standardize_stages_and_migrate_leads'),
    ]

    operations = [
        migrations.RunPython(rename_new_to_fresh, reverse_rename),
    ]
