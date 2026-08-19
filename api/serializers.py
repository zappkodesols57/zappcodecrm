from rest_framework import serializers
from accounts.models import User, Hospital
from leads.models import Lead, LeadStage, LeadSource, Course, Campaign
from followups.models import FollowUp

class HospitalSerializer(serializers.ModelSerializer):
    class Meta:
        model = Hospital
        fields = ['id', 'name', 'contact_email', 'phone', 'address']

class UserSerializer(serializers.ModelSerializer):
    hospital = HospitalSerializer(read_only=True)
    
    class Meta:
        model = User
        fields = ['id', 'username', 'first_name', 'last_name', 'email', 'phone', 'role', 'department', 'speciality', 'hospital', 'is_active_employee']

class LeadSerializer(serializers.ModelSerializer):
    assigned_to_name = serializers.CharField(source='assigned_to.get_full_name', read_only=True)
    course_name = serializers.CharField(source='course.name', read_only=True)
    source_name = serializers.CharField(source='lead_source.name', read_only=True)
    
    class Meta:
        model = Lead
        # Providing a broad set of fields to match typical API needs.
        fields = [
            'id', 'lead_code', 'name', 'mobile', 'email', 'city', 'location',
            'course', 'course_name', 'temperature', 'stage', 'deal_status',
            'inquiry_date', 'lead_source', 'source_name', 'campaign',
            'assigned_to', 'assigned_to_name', 'hospital', 'notes',
            'next_followup_date', 'next_followup_time', 'created_at', 'nelson_data'
        ]
        read_only_fields = ['id', 'lead_code', 'hospital', 'created_at']

class FollowUpSerializer(serializers.ModelSerializer):
    lead_name = serializers.CharField(source='lead.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    
    class Meta:
        model = FollowUp
        fields = [
            'id', 'lead', 'lead_name', 'created_by', 'created_by_name',
            'followup_date', 'followup_time', 'followup_mode',
            'followup_status', 'comment', 'next_followup_date', 'created_at'
        ]
