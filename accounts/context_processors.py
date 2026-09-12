from accounts.models import Hospital

def global_business_context(request):
    if not request.user.is_authenticated:
        return {}
    
    # Only superadmins or global admins can switch businesses
    is_superadmin = request.user.is_superuser or request.user.role == 'SUPER_ADMIN'
    
    active_business_id = request.session.get('active_business_id', '')
    active_business = None
    if active_business_id and str(active_business_id).isdigit():
        active_business = Hospital.objects.filter(id=int(active_business_id), is_active=True).first()
        if not active_business:
            # Fallback if hospital was deleted or inactivated
            request.session.pop('active_business_id', None)
            active_business_id = ''
            
    all_businesses = list(Hospital.objects.filter(is_active=True).order_by('name')) if is_superadmin else []
    
    # Effective hospital for the session: user's own hospital, or selected active_business
    effective_hospital = request.user.hospital or active_business

    return {
        'is_superadmin_user': is_superadmin,
        'global_businesses': all_businesses,
        'active_business_id': active_business_id,
        'active_business': active_business,
        'effective_hospital': effective_hospital,
    }
