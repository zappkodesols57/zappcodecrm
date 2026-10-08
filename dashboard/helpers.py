from django.utils import timezone
from datetime import datetime
from leads.models import DealStatus, AdmissionStatus
from accounts.models import Hospital

def filter_uncontacted_leads_ids(c_base, today=None):
    """
    Returns list of lead IDs that are genuinely fresh/uncontacted (Call Not Done):
    - Fresh/untouched leads with NO calling remarks (remark_1..5, comments, followup_remark, user notes)
    - Leads with NO follow-ups logged or scheduled (next_followup_date is None, followup_count == 0, no FollowUp entries)
    - Leads with NO calling dates (calling_date_remark_*, last_called_date)
    Strictly excludes:
    - Leads with terminal deal status (WON, LOST) or admission done
    - Leads with payments billed (total > 0)
    - Leads with confirmed/completed appointments
    - Any lead that has already been contacted, has follow-up remarks, or has scheduled follow-ups
    """
    if today is None:
        today = timezone.localdate()

    # 1. Broad DB exclusions (fast indexed filter, order_by() strips created_at filesort)
    q = c_base.order_by().filter(
        deal_status__in=[DealStatus.OPEN, 'New', 'OPEN']
    ).exclude(
        deal_status__in=[DealStatus.WON, DealStatus.LOST, 'WON', 'LOST', 'CLOSED']
    ).exclude(
        admission_status__in=[AdmissionStatus.WON, 'ADMISSION_DONE', 'WON']
    ).exclude(
        admission__isnull=False
    ).filter(
        next_followup_date__isnull=True,
        followup_count=0
    )

    rows = list(q.values('id', 'custom_data', 'stage__name', 'notes'))

    if not rows:
        return []

    lead_ids = [r['id'] for r in rows]

    # Fast batch check for any FollowUp records in database for these leads (limit/slice safely)
    from followups.models import FollowUp
    leads_with_followups = set()
    # Batch in chunks of 500 to prevent oversized SQL IN queries
    for i in range(0, len(lead_ids), 500):
        chunk = lead_ids[i:i+500]
        leads_with_followups.update(FollowUp.objects.filter(lead_id__in=chunk).values_list('lead_id', flat=True))

    terminal_statuses = {'booked', 'completed', 'payment done', 'payment pending', 'cancelled', 'visited', 'admission done', 'won', 'lost', 'not interested', 'follow up', 'follow-up', 'followup'}
    terminal_stages = {'admission done', 'complete', 'lost', 'cancelled', 'won', 'follow up', 'follow-up', 'followup'}

    def is_clean_val(v):
        if not v:
            return False
        s = str(v).strip()
        return bool(s and s.lower() not in ('nan', 'none', '—', '-', '', 'null', 'nil', 'na', 'n/a'))

    def has_user_notes(notes_str):
        if not is_clean_val(notes_str):
            return False
        s = str(notes_str).strip()
        if s.startswith('[Lead ID]') and len(s.splitlines()) <= 1:
            return False
        return True

    matched_ids = []

    for r in rows:
        lid = r['id']
        if lid in leads_with_followups:
            continue

        cd = r['custom_data'] or {}
        st_name = (r['stage__name'] or '').strip().lower()

        # Total billed check
        tot = 0.0
        try:
            tot = float(cd.get('total_paid') or cd.get('total') or 0.0)
        except (ValueError, TypeError):
            tot = 0.0
        if tot > 0:
            continue

        raw_apt = str(cd.get('appointment_status') or '').strip().lower()
        raw_ds = str(cd.get('deal_status') or '').strip().lower()

        if raw_apt in terminal_statuses or raw_ds in terminal_statuses:
            continue
        if any(k in raw_apt for k in ['book', 'confirm', 'payment done', 'completed', 'visit planned', 'visited', 'follow']):
            continue
        if any(k in raw_ds for k in ['book', 'confirm', 'payment done', 'completed', 'visit planned', 'visited', 'follow']):
            continue

        if st_name in terminal_stages:
            continue

        # Check actual calling remarks fields (excludes lead question/survey notes)
        r1 = cd.get('remark_1')
        r2 = cd.get('remark_2')
        r3 = cd.get('remark_3')
        r4 = cd.get('remark_4')
        r5 = cd.get('remark_5')
        f_rem = cd.get('followup_remark')
        c_date1 = cd.get('calling_date_remark_1')
        c_date2 = cd.get('calling_date_remark_2')
        c_date3 = cd.get('calling_date_remark_3')
        last_called = cd.get('last_called_date')

        has_calling_remark = any(is_clean_val(rk) for rk in [r1, r2, r3, r4, r5, f_rem, c_date1, c_date2, c_date3, last_called])
        if has_calling_remark:
            continue

        matched_ids.append(lid)

    return matched_ids


def extract_lead_followup_date(l):
    """
    Extracts the effective scheduled follow-up date for a lead.
    Only considers active/pending follow-ups (FollowUpStatus.PENDING / RESCHEDULED)
    and excludes completed leads.
    """
    from followups.models import FollowUpStatus

    # Check prefetched followups first if present
    if hasattr(l, '_prefetched_objects_cache') and 'followups' in l._prefetched_objects_cache:
        pending_fus = [fu for fu in l.followups.all() if fu.followup_status in [FollowUpStatus.PENDING, FollowUpStatus.RESCHEDULED]]
        if pending_fus:
            # Return earliest upcoming/scheduled pending date
            for fu in sorted(pending_fus, key=lambda x: (x.followup_date, x.followup_time or datetime.min.time())):
                if fu.followup_date:
                    return fu.followup_date
        # If all followups are COMPLETED, return None
        all_fus = list(l.followups.all())
        if all_fus and all(fu.followup_status in [FollowUpStatus.COMPLETED, "COMPLETED", "DONE"] for fu in all_fus):
            return None
    else:
        pending_fu = l.followups.filter(
            followup_status__in=[FollowUpStatus.PENDING, FollowUpStatus.RESCHEDULED]
        ).order_by('followup_date', 'followup_time').first()
        if pending_fu and pending_fu.followup_date:
            return pending_fu.followup_date
        
        has_any_fu = l.followups.exists()
        if has_any_fu:
            # Has followups but none are pending (all completed/done)
            return None

    if l.next_followup_date:
        return l.next_followup_date

    cd = l.custom_data or {}
    for k in ['next_followup_date', 'followup_date', 'calling_date_remark_1', 'calling_date_remark_2', 'calling_date_remark_3']:
        v = cd.get(k)
        if v:
            v_str = str(v).strip()[:10]
            try:
                return datetime.strptime(v_str, '%Y-%m-%d').date()
            except Exception:
                try:
                    return datetime.strptime(v_str, '%d-%m-%Y').date()
                except Exception:
                    pass
    return None

def _get_effective_hospital(request):
    """
    Resolves the effective hospital/business for reports:
    1. If user has a hospital attached, returns that hospital.
    2. For Super Admin / Global Admin: checks GET 'business'/'hospital' or session 'active_business_id'.
    3. Returns (hospital_object, is_filtered_by_hospital).
    """
    user = request.user
    if user.hospital:
        return user.hospital
    selected_biz_id = (
        request.GET.get("business", "").strip()
        or request.GET.get("hospital", "").strip()
        or str(request.session.get("active_business_id", "")).strip()
    )
    if selected_biz_id and selected_biz_id.isdigit():
        return Hospital.objects.filter(id=int(selected_biz_id)).first()
    return None


