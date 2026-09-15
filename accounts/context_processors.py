from accounts.models import Hospital

def global_business_context(request):
    if not request.user.is_authenticated:
        return {}
    
    # Only true Global Super Admins (role SUPER_ADMIN or superuser, with NO assigned hospital) can switch businesses or have global multi-tenant access.
    # Any business admin (role ADMIN / MANAGER / etc. or user with an assigned hospital) is strictly limited to their business.
    is_superadmin = (request.user.is_superuser or request.user.role == 'SUPER_ADMIN') and not bool(request.user.hospital)
    
    active_business_id = request.session.get('active_business_id', '') if is_superadmin else ''
    active_business = None
    if is_superadmin and active_business_id and str(active_business_id).isdigit():
        active_business = Hospital.objects.filter(id=int(active_business_id), is_active=True).first()
        if not active_business:
            # Fallback if hospital was deleted or inactivated
            request.session.pop('active_business_id', None)
            active_business_id = ''
            
    all_businesses = list(Hospital.objects.filter(is_active=True).order_by('name')) if is_superadmin else []
    
    # Effective hospital for the session: user's own hospital, or selected active_business for global superadmin
    effective_hospital = request.user.hospital or (active_business if is_superadmin else None)

    return {
        'is_superadmin_user': is_superadmin,
        'global_businesses': all_businesses,
        'active_business_id': active_business_id,
        'active_business': active_business,
        'effective_hospital': effective_hospital,
    }
