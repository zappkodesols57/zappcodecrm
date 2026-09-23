import json
from datetime import datetime, timedelta
import re
import pandas as pd
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.db.models import Q, Count

from leads.models import Lead, SourceCategory, LeadSource, Course, LeadStage
from leads.models import Campaign as HospitalCampaign
from accounts.models import User, Hospital
from followups.models import FollowUp, FollowUpStatus, FollowUpMode
from .models import ImportJob, ImportError as ImportErrorModel
from . import cleaning

TARGET_FIELDS = [
    ("IGNORE", "— Ignore this column (ID / Ad ID / Non-required) —"),
    ("name", "Patient / Lead Name"),
    ("mobile", "Mobile / Phone Number"),
    ("email", "Email Address"),
    ("gender", "Gender (Male / Female / Other)"),
    ("age", "Age"),
    ("city", "City / Location / Area"),
    ("doctor", "Doctor / Consultant"),
    ("department", "Department / Speciality"),
    ("campaign", "Campaign Name"),
    ("source", "Lead Source / Platform (FB, IG, Google, etc.)"),
    ("assigned_to", "Assigned To / Telecaller / Executive"),
    ("inquiry_date", "Date / Created At (DD/MM/YYYY)"),
    ("notes", "Comments / Notes / Survey Question Responses"),
]

GUESS_KEYWORDS = {
    "name": ["your_name", "your name", "patient name", "patient", "full name", "customer name", "lead name", "client name", "user name", "name", "first name", "naam"],
    "mobile": ["phone_number", "phone number", "mobile number", "mobile", "phone", "contact number", "contact", "call number", "whatsapp number", "whatsapp", "cell"],
    "email": ["email", "e-mail", "mail", "email address"],
    "gender": ["gender", "sex", "m/f"],
    "age": ["age", "years", "yrs"],
    "city": ["city", "location", "address", "area", "town", "district"],
    "doctor": ["doctor", "dr name", "consultant", "physician", "surgeon"],
    "department": ["department", "speciality", "dept", "specialization"],
    "campaign": ["campaign name", "campaign", "ad name", "ad set name"],
    "source": ["origin", "source", "platform", "publisher platform", "lead source", "channel"],
    "assigned_to": ["lead attendent", "lead attendant", "lead_attendent", "lead_attendant", "attendent", "attendant", "assigned to", "assigned", "telecaller", "tele caller", "executive", "caller", "agent", "lead owner", "owner", "assignee", "counsellor", "counselor"],
    "inquiry_date": ["created at", "created_at", "date", "created time", "lead date", "inquiry date", "lead time"],
    "notes": ["remark", "comment", "issue", "note", "problem", "symptom", "query", "reason", "question", "समस्या", "रोग"],
}


def _resolve_assigned_user(raw_val, hospital=None, default_user=None):
    """Finds or matches a User/Telecaller by name or username, or returns default_user."""
    if not raw_val or str(raw_val).strip() in ("", "-", "nan", "NaT", "none", "null"):
        return default_user
    from accounts.models import User
    from django.db.models import Q
    s = str(raw_val).strip()
    s_low = s.lower()
    
    qs = User.objects.filter(is_active=True)
    if hospital:
        qs_h = qs.filter(hospital=hospital)
        if qs_h.exists():
            qs = qs_h

    # 1. Exact username or full name match
    for u in qs:
        uname = (u.username or "").strip().lower()
        fname = (u.get_full_name() or "").strip().lower()
        if s_low == uname or s_low == fname or (fname and s_low in fname) or (uname and s_low in uname):
            return u
            
    # 2. First name / Last name partial match
    for u in qs:
        if (u.first_name and u.first_name.lower() in s_low) or (u.last_name and u.last_name.lower() in s_low):
            return u

    return default_user


def _guess_field(header):
    h = str(header).strip().lower()
    for field, keywords in GUESS_KEYWORDS.items():
        if any(k in h for k in keywords):
            return field
    return "IGNORE"


def _is_date_header(header):
    if isinstance(header, (datetime,)):
        return True
    try:
        import datetime as dt
        if isinstance(header, dt.date):
            return True
    except Exception:
        pass
    return cleaning.parse_date(header) is not None and len(str(header)) >= 6


def _detect_header_row(raw_df):
    for i in range(min(5, len(raw_df))):
        row = raw_df.iloc[i]
        non_null = row.notna().sum()
        text_like = sum(1 for v in row if isinstance(v, str) and len(v.strip()) > 1)
        if non_null >= 3 and text_like >= 2:
            return i
    return 0


def _get_or_create_source(category_name, source_name):
    if not category_name or not source_name:
        return None, None
    cat, _ = SourceCategory.objects.get_or_create(name=category_name)
    src, _ = LeadSource.objects.get_or_create(name=source_name, category=cat)
    return cat, src


def _get_or_create_course(name):
    if not name:
        return None
    course, _ = Course.objects.get_or_create(name=name)
    return course


def _default_stage():
    stage = LeadStage.objects.order_by("order").first()
    if not stage:
        stage = LeadStage.objects.create(name="New", order=0)
    return stage


def _load_excel_or_csv(file_path, filename=""):
    """Robustly reads Excel (.xlsx, .xls, .xlsm, .xlsb) or CSV with automatic engine fallback and multi-encoding support."""
    fn_lower = filename.lower()
    
    # 1. Try CSV parsing if filename ends with .csv or fallback
    if fn_lower.endswith(".csv"):
        for enc in ["utf-8-sig", "utf-8", "latin1", "cp1252", "iso-8859-1"]:
            try:
                df = pd.read_csv(file_path, encoding=enc)
                return {"type": "df", "df": df, "sheets": ["CSV Data"]}
            except Exception:
                continue
                
    # 2. Try pd.ExcelFile with automatic and explicit engine fallbacks
    for eng in [None, "openpyxl", "xlrd", "pyxlsb"]:
        try:
            if eng:
                xl = pd.ExcelFile(file_path, engine=eng)
            else:
                xl = pd.ExcelFile(file_path)
            return {"type": "excel", "xl": xl, "sheets": xl.sheet_names, "engine": eng}
        except Exception:
            continue
            
    # 3. Last fallback: Try reading as CSV even if named .xlsx/.xls
    for enc in ["utf-8-sig", "utf-8", "latin1", "cp1252"]:
        try:
            df = pd.read_csv(file_path, encoding=enc)
            return {"type": "df", "df": df, "sheets": ["Imported Data"]}
        except Exception:
            continue
            
    raise ValueError("File format could not be read. Please upload a valid .xlsx, .xls, or .csv file.")


from django.core.cache import cache
from accounts.models import User, Hospital
from leads.models import Campaign as HospitalCampaign
from .nel_hospital_import_service import (
    parse_any_file_to_dataframe,
    extract_campaign_lead_data,
    check_duplicates_in_db,
    generate_lead_code,
    parse_flexible_date,
)
from django.db import transaction


def _can_user_access_import(user):
    """
    Lead Attendant, Hospital Manager, Hospital Admin, Super Admin can import campaign leads.
    """
    if not user.is_authenticated:
        return False
    if user.is_superuser or user.role in (User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER, User.Role.LEAD_ATTENDENT):
        return True
    return getattr(user, "can_import_export", False)


@login_required
@user_passes_test(_can_user_access_import)
def upload(request):
    """
    Unified Import Leads View:
    - Mode 1: Campaign Leads (Available to Lead Attendant, Manager, Admin, Super Admin)
    - Mode 2: Previous Lead Data (Available to Manager, Admin, Super Admin)
    - Zappcode Super Admin can select target hospital/business.
    """
    user = request.user
    is_super_admin_no_hospital = bool(user.role == User.Role.SUPER_ADMIN and not user.hospital)
    can_import_previous = bool(user.is_superuser or user.role in (User.Role.SUPER_ADMIN, User.Role.ADMIN, User.Role.MANAGER))
    
    # Determine if current scope is Hospital vs Zappcode Academy
    is_hospital = bool(user.hospital or (user.is_hospital_user and not is_super_admin_no_hospital))

    # Available campaigns & courses with current leads count
    from django.db.models import Count, Q
    all_hospitals = []
    if is_super_admin_no_hospital:
        all_hospitals = Hospital.objects.filter(is_active=True).annotate(
            leads_count=Count("leads", filter=Q(leads__is_archived=False))
        ).order_by("name")

    if user.hospital:
        campaigns = HospitalCampaign.objects.filter(hospital=user.hospital, is_active=True).annotate(leads_count=Count("leads")).order_by("-id")
        courses = Course.objects.filter(hospital=user.hospital, is_active=True).annotate(leads_count=Count("leads")).order_by("-id")
        current_leads_count = Lead.objects.filter(hospital=user.hospital, is_archived=False).count()
    elif is_super_admin_no_hospital and all_hospitals.exists():
        first_h = all_hospitals.first()
        campaigns = HospitalCampaign.objects.filter(hospital=first_h, is_active=True).annotate(leads_count=Count("leads")).order_by("-id")
        courses = Course.objects.filter(hospital=first_h, is_active=True).annotate(leads_count=Count("leads")).order_by("-id")
        current_leads_count = Lead.objects.filter(hospital=first_h, is_archived=False).count()
    else:
        campaigns = HospitalCampaign.objects.filter(is_active=True).annotate(leads_count=Count("leads")).order_by("-id")
        courses = Course.objects.filter(is_active=True).annotate(leads_count=Count("leads")).order_by("-id")
        current_leads_count = Lead.objects.filter(is_archived=False).count()

    today = timezone.localdate()
    date_preset = request.GET.get('date_preset', 'today')
    start_date_str = request.GET.get('start_date', '')
    end_date_str = request.GET.get('end_date', '')

    if date_preset == 'today':
        filter_start = today
        filter_end = today
        preset_label = today.strftime('%d-%m-%Y')
    elif date_preset == 'yesterday':
        yesterday = today - timedelta(days=1)
        filter_start = yesterday
        filter_end = yesterday
        preset_label = yesterday.strftime('%d-%m-%Y')
    elif date_preset == 'last_7d':
        filter_start = today - timedelta(days=7)
        filter_end = today
        preset_label = f"{filter_start.strftime('%d-%m-%Y')} to {filter_end.strftime('%d-%m-%Y')}"
    elif date_preset == 'this_month':
        filter_start = today.replace(day=1)
        filter_end = today
        preset_label = f"{filter_start.strftime('%d-%m-%Y')} to {filter_end.strftime('%d-%m-%Y')}"
    elif date_preset == 'all_time':
        filter_start = None
        filter_end = None
        preset_label = "All Time"
    elif date_preset == 'custom' and start_date_str:
        try:
            filter_start = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            filter_end = datetime.strptime(end_date_str, '%Y-%m-%d').date() if end_date_str else filter_start
            preset_label = f"{filter_start.strftime('%d-%m-%Y')} to {filter_end.strftime('%d-%m-%Y')}"
        except ValueError:
            filter_start = today
            filter_end = today
            preset_label = today.strftime('%d-%m-%Y')

    # Filter base leads and import jobs for current scope
    if user.hospital:
        base_leads_qs = Lead.objects.filter(hospital=user.hospital, is_archived=False)
        base_jobs_qs = ImportJob.objects.filter(created_by__hospital=user.hospital)
    else:
        # Global Super Admin: can see all leads across all businesses
        base_leads_qs = Lead.objects.filter(is_archived=False)
        base_jobs_qs = ImportJob.objects.all()

    import datetime as dt_module
    if filter_start and filter_end:
        start_dt = timezone.make_aware(dt_module.datetime.combine(filter_start, dt_module.time.min))
        end_dt = timezone.make_aware(dt_module.datetime.combine(filter_end, dt_module.time.max))
        period_leads_qs = base_leads_qs.filter(
            Q(created_at__gte=start_dt, created_at__lte=end_dt) |
            Q(inquiry_date__gte=filter_start, inquiry_date__lte=filter_end)
        )
        period_jobs_qs = base_jobs_qs.filter(
            created_at__gte=start_dt, created_at__lte=end_dt
        )
    else:
        period_leads_qs = base_leads_qs
        period_jobs_qs = base_jobs_qs

    total_period_leads = period_leads_qs.count()

    # 1. Campaigns Breakdown (Hospital)
    campaigns_data = []
    for c in campaigns:
        p_cnt = period_leads_qs.filter(Q(campaign=c) | Q(custom_data__campaign=c.name)).count()
        campaigns_data.append({
            "id": c.id,
            "name": c.name,
            "platform": c.platform or "General",
            "period_leads": p_cnt,
            "all_time_leads": c.leads_count,
        })
    campaigns_data.sort(key=lambda x: x["period_leads"], reverse=True)

    # 2. Courses Breakdown (Academy)
    courses_data = []
    for crs in courses:
        p_cnt = period_leads_qs.filter(Q(course=crs) | Q(custom_data__course__icontains=crs.name)).count()
        courses_data.append({
            "id": crs.id,
            "name": crs.name,
            "category": getattr(crs, "category", "") or "Technology",
            "period_leads": p_cnt,
            "all_time_leads": crs.leads_count,
        })
    courses_data.sort(key=lambda x: x["period_leads"], reverse=True)

    # Only show files that actually created/imported leads (> 0) in this period
    recent_jobs = period_jobs_qs.filter(imported_count__gt=0).order_by('-created_at')[:15]
    if user.hospital:
        hospital_name = user.hospital.name
    elif is_super_admin_no_hospital and all_hospitals.exists():
        hospital_name = all_hospitals.first().name
    else:
        hospital_name = "Zappcode Academy"

    can_manage_master_data = bool(user.is_superuser or user.role in (User.Role.SUPER_ADMIN, User.Role.ADMIN))
    can_delete_master_data = bool(user.is_superuser or user.role == User.Role.SUPER_ADMIN or (user.role == User.Role.ADMIN and user.can_delete_master_data))

    context = {
        "active": "import",
        "is_hospital": is_hospital,
        "is_super_admin_no_hospital": is_super_admin_no_hospital,
        "can_import_previous": can_import_previous,
        "can_manage_master_data": can_manage_master_data,
        "can_delete_master_data": can_delete_master_data,
        "available_campaigns": campaigns,
        "available_courses": courses,
        "campaigns_data": campaigns_data,
        "courses_data": courses_data,
        "total_period_leads": total_period_leads,
        "recent_jobs": recent_jobs,
        "hospital_name": hospital_name,
        "today_date_str": today.strftime('%d-%m-%Y'),
        "date_preset": date_preset,
        "start_date": start_date_str,
        "end_date": end_date_str,
        "preset_label": preset_label,
        "all_hospitals": all_hospitals,
        "current_leads_count": current_leads_count,
    }
    return render(request, "imports/upload.html", context)


@login_required
@user_passes_test(_can_user_access_import)
def ajax_create_course(request):
    """Creates a new Course via AJAX from the import screen."""
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Invalid request method"}, status=400)

    name = request.POST.get("name", "").strip()
    if not name:
        return JsonResponse({"success": False, "error": "Course name is required."})

    hospital_id = request.POST.get("hospital_id", "").strip()
    target_hospital = None
    if hospital_id:
        target_hospital = Hospital.objects.filter(pk=hospital_id).first()
    elif request.user.hospital and "zappcode" not in (request.user.hospital.name or "").lower():
        target_hospital = request.user.hospital

    course, created = Course.objects.get_or_create(
        name=name,
        hospital=target_hospital,
        defaults={"is_active": True}
    )

    return JsonResponse({
        "success": True,
        "course": {
            "id": course.id,
            "name": course.name,
            "hospital_id": course.hospital_id,
        }
    })


@login_required
@user_passes_test(_can_user_access_import)
def ajax_create_campaign(request):
    """Creates a new Campaign via AJAX from the import screen."""
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Invalid request method"}, status=400)
    
    name = request.POST.get("name", "").strip()
    platform = request.POST.get("platform", "Meta Ads").strip()
    ad_set = request.POST.get("ad_set", "").strip()
    hospital_id = request.POST.get("hospital_id", "").strip()

    if not name:
        return JsonResponse({"success": False, "error": "Campaign name is required."})

    target_hospital = None
    if hospital_id:
        target_hospital = Hospital.objects.filter(pk=hospital_id).first()
    elif request.user.hospital:
        target_hospital = request.user.hospital

    start_date_str = request.POST.get("start_date", "").strip()
    end_date_str = request.POST.get("end_date", "").strip()

    start_date = parse_flexible_date(start_date_str) if start_date_str else None
    end_date = parse_flexible_date(end_date_str) if end_date_str else None

    if start_date and end_date and end_date < start_date:
        return JsonResponse({"success": False, "error": "End Date must be greater than or equal to Start Date."})

    defaults = {
        "platform": platform,
        "ad_set": ad_set,
        "is_active": True,
    }
    if start_date:
        defaults["start_date"] = start_date
    if end_date:
        defaults["end_date"] = end_date

    campaign, created = HospitalCampaign.objects.get_or_create(
        name=name,
        hospital=target_hospital,
        defaults=defaults
    )
    if not created:
        if start_date and not campaign.start_date:
            campaign.start_date = start_date
        if end_date and not campaign.end_date:
            campaign.end_date = end_date
        campaign.save()

    return JsonResponse({
        "success": True,
        "campaign": {
            "id": campaign.id,
            "name": campaign.name,
            "platform": campaign.platform,
            "hospital_id": campaign.hospital_id,
        }
    })


@login_required
@user_passes_test(_can_user_access_import)
def ajax_business_data(request):
    """
    Returns campaigns, courses, business type, and lead count for a given hospital/business ID.
    Used for seamless dynamic switching on the import page by Super Admin.
    """
    hospital_id = request.GET.get("hospital_id", "").strip()
    hospital = None
    if hospital_id:
        hospital = Hospital.objects.filter(pk=hospital_id).first()
    elif request.user.hospital:
        hospital = request.user.hospital

    is_hospital = True
    if hospital:
        btype = (hospital.settings or {}).get("business_type")
        if btype:
            is_hospital = (str(btype).strip().lower() == "hospital")
        else:
            name_lower = (hospital.name or "").lower()
            is_hospital = not ("academy" in name_lower or "zappcode" in name_lower)
    elif not request.user.hospital:
        # Default global super admin without selected business
        is_hospital = False

    from django.db.models import Count
    if hospital:
        campaigns = list(
            HospitalCampaign.objects.filter(hospital=hospital, is_active=True)
            .annotate(leads_count=Count("leads"))
            .order_by("-id")
            .values("id", "name", "platform", "leads_count")
        )
        courses = list(
            Course.objects.filter(hospital=hospital, is_active=True)
            .annotate(leads_count=Count("leads"))
            .order_by("-id")
            .values("id", "name", "leads_count")
        )
        total_leads = Lead.objects.filter(hospital=hospital, is_archived=False).count()
        business_name = hospital.name
    else:
        campaigns = list(
            HospitalCampaign.objects.filter(is_active=True)
            .annotate(leads_count=Count("leads"))
            .order_by("-id")
            .values("id", "name", "platform", "leads_count")
        )
        courses = list(
            Course.objects.filter(is_active=True)
            .annotate(leads_count=Count("leads"))
            .order_by("-id")
            .values("id", "name", "leads_count")
        )
        total_leads = Lead.objects.filter(is_archived=False).count()
        business_name = "All Businesses"

    return JsonResponse({
        "success": True,
        "is_hospital": is_hospital,
        "business_name": business_name,
        "total_leads": total_leads,
        "campaigns": campaigns,
        "courses": courses,
    })


@login_required
@user_passes_test(_can_user_access_import)
def campaign_import_process(request):
    """
    Processes uploaded Campaign Lead file (.xml, .xlsx, .xls, .csv).
    If duplicate_strategy == 'preview', renders interactive conflict resolution screen.
    Otherwise executes directly.
    """
    if request.method != "POST" or "lead_file" not in request.FILES:
        messages.error(request, "Please select a valid leads file to upload.")
        return redirect("imports:upload")

    uploaded_file = request.FILES["lead_file"]
    campaign_id = request.POST.get("campaign_id")
    target_hospital_id = request.POST.get("target_hospital_id")
    duplicate_strategy = request.POST.get("duplicate_strategy", "preview")

    # Determine target hospital
    target_hospital = None
    if target_hospital_id:
        target_hospital = Hospital.objects.filter(pk=target_hospital_id).first()
    elif request.user.hospital:
        target_hospital = request.user.hospital

    # Parse dataframe first
    try:
        df = parse_any_file_to_dataframe(uploaded_file)
    except Exception as e:
        messages.error(request, f"Could not read the uploaded file: {e}")
        return redirect("imports:upload")

    if df is None or len(df) == 0:
        messages.error(request, "The uploaded file is empty.")
        return redirect("imports:upload")

    # Determine Campaign
    campaign = None
    if campaign_id:
        campaign = HospitalCampaign.objects.filter(pk=campaign_id).first()

    # If no campaign selected, auto-resolve from Form Name column or filename
    if not campaign:
        auto_campaign_name = ""
        for col in ["Form Name", "form_name", "Campaign Name", "campaign_name", "Campaign"]:
            if col in df.columns and df[col].dropna().count() > 0:
                auto_campaign_name = str(df[col].dropna().iloc[0]).strip()
                break
        
        if not auto_campaign_name:
            import os
            auto_campaign_name = os.path.splitext(uploaded_file.name)[0].replace("_", " ").strip()
            
        if auto_campaign_name:
            campaign, _ = HospitalCampaign.objects.get_or_create(
                name=auto_campaign_name,
                hospital=target_hospital,
                defaults={"platform": "Meta Ads", "is_active": True}
            )

    if not campaign:
        messages.error(request, "Please select or create a Campaign for these leads.")
        return redirect("imports:upload")

    # Clean and extract campaign rows
    lead_rows = extract_campaign_lead_data(df, target_campaign=campaign, target_hospital=target_hospital)
    if not lead_rows:
        messages.error(request, "No valid lead rows could be extracted. Please check the file columns.")
        return redirect("imports:upload")

    # Check duplicates against DB
    processed_rows, stats = check_duplicates_in_db(lead_rows, hospital=target_hospital)

    # If preview strategy requested or duplicates exist
    if duplicate_strategy == "preview":
        import uuid
        cache_key = f"camp_import_{uuid.uuid4().hex}"
        cache_payload = {
            "rows": processed_rows,
            "campaign_id": campaign.id,
            "target_hospital_id": target_hospital.id if target_hospital else None,
            "original_filename": uploaded_file.name,
        }
        # Store in cache (file-based)
        cache.set(cache_key, cache_payload, timeout=86400)
        # Also store in DB session as fallback
        request.session[cache_key] = cache_payload
        request.session.modified = True

        context = {
            "active": "import",
            "campaign": campaign,
            "target_hospital": target_hospital,
            "original_filename": uploaded_file.name,
            "stats": stats,
            "rows": processed_rows,
            "cache_key": cache_key,
        }
        return render(request, "imports/nel_campaign_import_preview.html", context)

    # Automatic execution based on strategy (skip / create / update)
    return _execute_campaign_leads_import(
        request, processed_rows, campaign, target_hospital, uploaded_file.name, default_strategy=duplicate_strategy
    )


@login_required
@user_passes_test(_can_user_access_import)
def campaign_import_execute(request):
    """
    Executes the campaign leads import after user confirms duplicate actions.
    """
    if request.method != "POST":
        return redirect("imports:upload")

    cache_key = request.POST.get("cache_key")
    cached_data = cache.get(cache_key) if cache_key else None
    
    # Fallback to session if cache missed
    if not cached_data and cache_key and cache_key in request.session:
        cached_data = request.session.get(cache_key)

    if not cached_data:
        messages.error(request, "Import session expired or not found. Please upload the file again.")
        return redirect("imports:upload")

    rows = cached_data.get("rows", [])
    campaign_id = request.POST.get("campaign_id") or cached_data.get("campaign_id")
    target_hospital_id = request.POST.get("target_hospital_id") or cached_data.get("target_hospital_id")
    original_filename = cached_data.get("original_filename", "campaign_leads.xml")

    campaign = HospitalCampaign.objects.filter(pk=campaign_id).first()
    target_hospital = Hospital.objects.filter(pk=target_hospital_id).first() if target_hospital_id else request.user.hospital

    # Apply row-level action decisions from form
    for idx, r in enumerate(rows):
        action_val = request.POST.get(f"action_{idx}")
        if action_val:
            r["duplicate_action"] = action_val

    return _execute_campaign_leads_import(
        request, rows, campaign, target_hospital, original_filename
    )


def _execute_campaign_leads_import(request, rows, campaign, target_hospital, original_filename, default_strategy=None):
    """Core function to create/update Lead records with transaction safety and update Campaign start/end dates."""
    from leads.models import MasterGroup, MasterItem, LeadCustomField
    from followups.models import FollowUp, FollowUpStatus, FollowUpMode
    default_cat, _ = SourceCategory.objects.get_or_create(name="Digital Marketing", defaults={"order": 1})
    stage_new = LeadStage.objects.filter(name__iexact="New").first() or LeadStage.objects.first()

    # Pre-cache stages for status mapping
    stage_cache = {s.name.strip().lower(): s for s in LeadStage.objects.all()}

    # Pre-cache users for instant attendant matching
    user_cache = {}
    user_hospital = target_hospital or request.user.hospital
    all_active_users = list(User.objects.filter(is_active=True).select_related('hospital'))
    for u in all_active_users:
        u_key_list = []
        if u.username:
            u_key_list.append(u.username.strip().lower())
        fname = (u.get_full_name() or "").strip().lower()
        if fname:
            u_key_list.append(fname)
        if u.first_name:
            u_key_list.append(u.first_name.strip().lower())
        
        for k in u_key_list:
            if user_hospital:
                if u.hospital_id == user_hospital.id:
                    user_cache[k] = u
                elif k not in user_cache:
                    user_cache[k] = u
            else:
                if k not in user_cache:
                    user_cache[k] = u

    def fast_resolve_user(raw_val):
        if not raw_val:
            return None
        s = str(raw_val).strip().lower()
        if s in ("", "-", "nan", "nat", "none", "null"):
            return None
        if s in user_cache:
            return user_cache[s]
        for k, u in user_cache.items():
            if k in s or s in k:
                return u
        return None

    job = ImportJob.objects.create(
        original_filename=original_filename,
        total_rows=len(rows),
        created_by=request.user,
        status=ImportJob.Status.PROCESSING,
    )

    imported_count = 0
    updated_count = 0
    skipped_count = 0

    source_cache = {}
    extracted_dates = []
    followups_to_create = []

    with transaction.atomic():
        for r in rows:
            action = r.get("duplicate_action") or default_strategy or "create"
            mobile = r.get("mobile", "")
            name = r.get("name", "Unknown Patient")
            email = r.get("email", "")
            inquiry_date = parse_flexible_date(r.get("inquiry_date"))
            if inquiry_date:
                extracted_dates.append(inquiry_date)
            source_name = r.get("source_name", "Instagram")
            notes = r.get("notes", "")
            custom_data = r.get("custom_data", {})
            raw_meta = r.get("raw_metadata", {})
            external_id = r.get("external_lead_id", "")

            # Resolve Assigned Attendant
            attendant_raw = r.get("attendant_raw", "")
            assigned_user = fast_resolve_user(attendant_raw)

            # Resolve Stage & Deal Status
            final_status_raw = str(r.get("final_status_raw") or "").strip().lower()
            lead_stage = stage_new
            lead_deal_status = "OPEN"

            if "lost" in final_status_raw:
                lead_deal_status = "LOST"
                if "lost" in stage_cache:
                    lead_stage = stage_cache["lost"]
            elif "hold" in final_status_raw:
                lead_deal_status = "HOLD"
                if "hold" in stage_cache:
                    lead_stage = stage_cache["hold"]
            elif "won" in final_status_raw or "admission" in final_status_raw:
                lead_deal_status = "WON"
                if "complete" in stage_cache:
                    lead_stage = stage_cache["complete"]
                elif "admission" in stage_cache:
                    lead_stage = stage_cache["admission"]
            elif final_status_raw in stage_cache:
                lead_stage = stage_cache[final_status_raw]

            # If duplicate and user chose to discard
            if r.get("is_duplicate") and action == "discard":
                skipped_count += 1
                continue

            # Lead Source resolution
            if source_name not in source_cache:
                src_obj, _ = LeadSource.objects.get_or_create(
                    name=source_name,
                    category=default_cat,
                    defaults={"is_active": True}
                )
                source_cache[source_name] = src_obj
            lead_source_obj = source_cache[source_name]

            # Course resolution
            course_obj = None
            course_name = r.get("course_name", "")
            if course_name:
                course_obj = Course.objects.filter(
                    Q(name__iexact=course_name) | Q(name__icontains=course_name)
                ).first()
                if not course_obj:
                    course_obj = Course.objects.create(
                        name=course_name,
                        hospital=target_hospital,
                        is_active=True
                    )

            # If duplicate and user chose update
            if r.get("is_duplicate") and action == "update" and r.get("existing_lead_id"):
                existing = Lead.objects.filter(pk=r["existing_lead_id"]).first()
                if existing:
                    if email and not existing.email:
                        existing.email = email
                    if r.get("city") and not existing.city:
                        existing.city = r.get("city")
                    if course_obj and not existing.course:
                        existing.course = course_obj
                    if assigned_user:
                        existing.assigned_to = assigned_user
                    if campaign:
                        existing.campaign = campaign
                    if notes:
                        existing.notes = (existing.notes + "\n" + notes).strip()
                    if custom_data:
                        existing.custom_data.update(custom_data)
                    existing.import_job = job
                    existing.save()
                    updated_count += 1
                    lead_record = existing
            else:
                # Create New Lead
                lead_code = generate_lead_code(hospital=target_hospital)
                
                new_lead = Lead.objects.create(
                    lead_code=lead_code,
                    name=name,
                    mobile=mobile,
                    email=email,
                    city=r.get("city", ""),
                    location=r.get("city", ""),
                    course=course_obj,
                    hospital=target_hospital,
                    campaign=campaign,
                    assigned_to=assigned_user,
                    source_category=default_cat,
                    lead_source=lead_source_obj,
                    stage=lead_stage,
                    temperature="HOT",
                    deal_status=lead_deal_status,
                    admission_status="NOT_APPLIED",
                    inquiry_date=inquiry_date,
                    notes=notes,
                    custom_data=custom_data,
                    raw_source_metadata=raw_meta,
                    external_lead_id=external_id,
                    import_source_file=original_filename,
                    import_job=job,
                    created_by=request.user,
                )
                imported_count += 1
                lead_record = new_lead

            # Queue follow-ups if present in row
            fu1_d = r.get("fu1_date")
            fu1_rem = r.get("fu1_remark")
            fu2_d = r.get("fu2_date")
            fu2_rem = r.get("fu2_remark")

            fu_items = [
                (fu1_d, fu1_rem),
                (fu2_d, fu2_rem),
            ]
            for f_date_str, f_rem in fu_items:
                if f_date_str or f_rem:
                    parsed_fu_date = parse_flexible_date(f_date_str) if f_date_str else inquiry_date
                    st_choice = FollowUpStatus.COMPLETED if parsed_fu_date <= timezone.localdate() else FollowUpStatus.PENDING
                    followups_to_create.append(FollowUp(
                        lead=lead_record,
                        followup_date=parsed_fu_date,
                        followup_mode=FollowUpMode.CALL,
                        followup_status=st_choice,
                        comment=f_rem or "Follow-up logged via leads import",
                        created_by=request.user,
                        imported_from_excel=True,
                    ))

        # Bulk insert follow-ups
        if followups_to_create:
            FollowUp.objects.bulk_create(followups_to_create, batch_size=500)

        # Automatically update Campaign start_date (earliest date) and end_date (latest date)
        if campaign and extracted_dates:
            min_d = min(extracted_dates)
            max_d = max(extracted_dates)
            if not campaign.start_date or min_d < campaign.start_date:
                campaign.start_date = min_d
            if not campaign.end_date or max_d > campaign.end_date:
                campaign.end_date = max_d
            campaign.save(update_fields=["start_date", "end_date"])

    job.imported_count = imported_count + updated_count
    job.updated_count = updated_count
    job.skipped_count = skipped_count
    job.status = ImportJob.Status.DONE
    job.completed_at = timezone.now()
    job.save()

    messages.success(
        request,
        f"✅ Leads Import Successful! {imported_count} new leads created with Campaign '{campaign.name}', "
        f"{updated_count} existing records updated, {skipped_count} duplicates skipped."
    )

    if request.user.role == User.Role.LEAD_ATTENDENT:
        return redirect("dashboard:telecaller_new_enquiries")
    return redirect("leads:lead_list")


@login_required
@user_passes_test(lambda u: u.can_import_export)
def pick_sheet(request, pk):
    job = get_object_or_404(ImportJob, pk=pk)
    sheet_name = request.POST.get("sheet_name")
    
    # Robust read
    raw = None
    for eng in ["openpyxl", "xlrd", None]:
        try:
            if eng:
                raw = pd.read_excel(job.file.path, sheet_name=sheet_name, header=None, engine=eng)
            else:
                raw = pd.read_excel(job.file.path, sheet_name=sheet_name, header=None)
            break
        except Exception:
            continue
            
    if raw is None:
        try:
            raw = pd.read_csv(job.file.path, header=None, encoding="utf-8-sig")
        except Exception:
            raw = pd.read_csv(job.file.path, header=None, encoding="latin1")

    header_row = _detect_header_row(raw)
    headers = list(raw.iloc[header_row])
    job.sheet_name = sheet_name
    job.column_mapping = {"header_row": header_row}
    job.save(update_fields=["sheet_name", "column_mapping"])

    columns = []
    date_columns = []
    for idx, h in enumerate(headers):
        if pd.isna(h):
            continue
        if _is_date_header(h):
            date_columns.append({"idx": idx, "label": str(h)})
        else:
            columns.append({"idx": idx, "label": str(h), "guess": _guess_field(h)})

    return render(request, "imports/map_columns.html", {
        "active": "import", "job": job, "columns": columns, "date_columns": date_columns,
        "target_fields": TARGET_FIELDS,
    })


def _build_mapping_from_post(request):
    mapping = {}
    for key, val in request.POST.items():
        if key.startswith("map_") and val != "IGNORE":
            idx = key.replace("map_", "")
            mapping[idx] = val
    date_cols = request.POST.getlist("date_col_idx")
    return mapping, date_cols


@login_required
@user_passes_test(lambda u: u.can_import_export)
def preview(request, pk):
    job = get_object_or_404(ImportJob, pk=pk)
    mapping, date_cols = _build_mapping_from_post(request)
    header_row = job.column_mapping.get("header_row", 0)
    job.column_mapping = {"header_row": header_row, "field_map": mapping, "date_columns": date_cols}
    job.save(update_fields=["column_mapping"])

    df = pd.read_excel(job.file.path, sheet_name=job.sheet_name, header=header_row)
    df = df.dropna(how="all")
    job.total_rows = len(df)
    job.save(update_fields=["total_rows"])

    preview_rows = []
    cols = list(df.columns)
    for _, row in df.head(10).iterrows():
        parsed = _parse_row(row, cols, mapping)
        preview_rows.append(parsed)

    return render(request, "imports/preview.html", {
        "active": "import", "job": job, "preview_rows": preview_rows, "total_rows": job.total_rows,
    })


def _parse_row(row, cols, mapping, user=None):
    data = {}
    survey_questions = []
    
    for idx_str, field in mapping.items():
        idx = int(idx_str)
        if idx >= len(cols):
            continue
        val = row.iloc[idx] if hasattr(row, "iloc") else row[cols[idx]]
        if pd.isna(val):
            val = ""
        data[field] = val

    # Gather unmapped or question-like columns into comments / survey notes
    for idx, col_name in enumerate(cols):
        idx_str = str(idx)
        field_assigned = mapping.get(idx_str, "IGNORE")
        col_str = str(col_name).strip()
        val = row.iloc[idx] if hasattr(row, "iloc") else row[col_name]
        
        if pd.isna(val) or str(val).strip() in ("", "-", "nan", "NaT"):
            continue
            
        val_str = str(val).strip()
        
        # Check if column is a survey question or non-standard custom header (e.g. Marathi/Hindi question, pregnant months, etc.)
        if field_assigned == "IGNORE":
            # Ignore technical ID columns
            col_lower = col_str.lower()
            if any(tech_id in col_lower for tech_id in ["ad_id", "adset_id", "campaign_id", "form_id", "lead_id", "hospital_id", "is_organic"]):
                continue
            if len(col_str) > 2 and not col_lower.startswith("unnamed"):
                clean_q_name = col_str.replace("_", " ").strip()
                survey_questions.append(f"[{clean_q_name}]: {val_str}")

    name = str(data.get("name", "")).strip()
    if name.lower() in ("nan", "none", "null", "-", "na", "nat"):
        name = ""
    email_val = str(data.get("email", "")).strip()
    if not name and email_val and "@" in email_val:
        email_user = email_val.split("@")[0]
        clean_email_name = re.sub(r"[0-9_\.\-]+", " ", email_user).strip().title()
        if len(clean_email_name) >= 2:
            name = clean_email_name

    mobile, alt_mobile = cleaning.clean_phone(data.get("mobile"))
    source_cat, source_name, source_ambiguous = cleaning.normalize_source(data.get("source"))
    temperature, temp_ambiguous = cleaning.normalize_temperature(data.get("temperature"))
    inquiry_date = cleaning.parse_date(data.get("inquiry_date")) or timezone.localdate()

    # Combine explicit notes with survey questions
    base_notes = str(data.get("notes", "") or "").strip()
    all_notes_list = []
    if base_notes:
        all_notes_list.append(base_notes)
    if survey_questions:
        all_notes_list.extend(survey_questions)
    combined_notes = "\n".join(all_notes_list)

    warnings = []
    if not name:
        name = f"Unknown Patient (Row {row.name if hasattr(row, 'name') else ''})"
    if not mobile:
        warnings.append("Missing/invalid mobile number")

    assigned_user = _resolve_assigned_user(data.get("assigned_to"), hospital=getattr(user, 'hospital', None) if user else None)

    return {
        "name": name, "mobile": mobile, "alt_mobile": alt_mobile,
        "email": str(data.get("email", "") or "").strip(),
        "gender": str(data.get("gender", "") or "").strip(),
        "age": data.get("age", ""),
        "city": str(data.get("city", "") or "").strip(),
        "doctor": str(data.get("doctor", "") or "").strip(),
        "department": str(data.get("department", "") or "").strip(),
        "campaign_name": str(data.get("campaign", "") or "").strip(),
        "source_category": source_cat, "source_name": source_name,
        "assigned_user": assigned_user,
        "assigned_to_raw": str(data.get("assigned_to", "") or "").strip(),
        "temperature": "UNCONTACTED", "inquiry_date": inquiry_date,
        "notes": combined_notes,
        "deal_status": "OPEN",
        "admission_status": "NOT_APPLIED",
        "warnings": warnings,
    }


@login_required
@user_passes_test(lambda u: u.can_import_export or u.role in ("ADMIN", "SUPER_ADMIN", "MANAGER", "LEAD_ATTENDENT"))
def run_import(request, pk):
    from leads.models import Campaign as HospitalCampaign
    job = get_object_or_404(ImportJob, pk=pk)
    header_row = job.column_mapping.get("header_row", 0)
    mapping = job.column_mapping.get("field_map", {})
    date_cols_idx = [int(i) for i in job.column_mapping.get("date_columns", [])]
    df = None
    for eng in ["openpyxl", "xlrd", None]:
        try:
            if eng:
                df = pd.read_excel(job.file.path, sheet_name=job.sheet_name, header=header_row, engine=eng)
            else:
                df = pd.read_excel(job.file.path, sheet_name=job.sheet_name, header=header_row)
            break
        except Exception:
            continue
            
    if df is None:
        try:
            df = pd.read_csv(job.file.path, header=header_row, encoding="utf-8-sig")
        except Exception:
            df = pd.read_csv(job.file.path, header=header_row, encoding="latin1")

    df = df.dropna(how="all")
    cols = list(df.columns)

    imported = updated = skipped = duplicate = invalid = 0
    default_stage = _default_stage()
    user_hospital = request.user.hospital

    for row_num, (_, row) in enumerate(df.iterrows(), start=header_row + 2):
        parsed = _parse_row(row, cols, mapping, user=request.user)
        if not parsed["name"] or not parsed["mobile"]:
            invalid += 1
            ImportErrorModel.objects.create(
                job=job, row_number=row_num,
                error_message="Missing required field(s): " + ", ".join(
                    w for w in ["Missing name", "Missing/invalid mobile number"] if w in parsed["warnings"]
                ),
                raw_row_data={str(c): str(row[c]) for c in cols[:15]},
            )
            continue

        existing = Lead.objects.filter(mobile=parsed["mobile"]).first()
        if not existing:
            digits = parsed["mobile"]
            existing = next((l for l in Lead.objects.only("id", "mobile") if Lead.clean_mobile(l.mobile) == digits), None)

        if existing and on_duplicate == "skip":
            duplicate += 1
            continue

        cat, src = _get_or_create_source(parsed["source_category"], parsed["source_name"])
        
        # Auto-register new Lead Source into Master Data & Lead Custom Field options
        from leads.models import MasterGroup, MasterItem, LeadCustomField
        if parsed["source_name"] and user_hospital:
            s_name = parsed["source_name"].strip()
            mg_src, _ = MasterGroup.objects.get_or_create(name="Lead Sources")
            MasterItem.objects.get_or_create(
                group=mg_src,
                name=s_name,
                hospital=user_hospital,
                defaults={"is_active": True}
            )
            cf_src = LeadCustomField.objects.filter(hospital=user_hospital, name="lead_source").first()
            if cf_src:
                existing_opts = [o.strip() for o in cf_src.options.split(",") if o.strip()]
                if not any(o.lower() == s_name.lower() for o in existing_opts):
                    existing_opts.append(s_name)
                    cf_src.options = ", ".join(existing_opts)
                    cf_src.save(update_fields=["options"])

        # Link or Auto-create Campaign for Hospital
        campaign_obj = None
        if parsed["campaign_name"]:
            c_name = parsed["campaign_name"].strip()
            if user_hospital:
                campaign_obj, _ = HospitalCampaign.objects.get_or_create(
                    hospital=user_hospital,
                    name=c_name,
                    defaults={"platform": parsed["source_name"] or "Meta Ads", "is_active": True}
                )
                # Auto-register new Campaign into Master Data & Lead Custom Field options
                mg_camp, _ = MasterGroup.objects.get_or_create(name="Campaigns")
                MasterItem.objects.get_or_create(
                    group=mg_camp,
                    name=c_name,
                    hospital=user_hospital,
                    defaults={"is_active": True}
                )
                cf_camp = LeadCustomField.objects.filter(hospital=user_hospital, name="campaign").first()
                if cf_camp:
                    existing_copts = [o.strip() for o in cf_camp.options.split(",") if o.strip()]
                    if not any(o.lower() == c_name.lower() for o in existing_copts):
                        existing_copts.append(c_name)
                        cf_camp.options = ", ".join(existing_copts)
                        cf_camp.save(update_fields=["options"])
            else:
                campaign_obj, _ = HospitalCampaign.objects.get_or_create(
                    name=c_name,
                    defaults={"platform": parsed["source_name"] or "Meta Ads", "is_active": True}
                )

        # Auto-register new Location/City into Master Data & Lead Custom Field options
        if parsed["city"] and user_hospital:
            city_name = parsed["city"].strip()
            mg_loc, _ = MasterGroup.objects.get_or_create(name="Locations")
            MasterItem.objects.get_or_create(
                group=mg_loc,
                name=city_name,
                hospital=user_hospital,
                defaults={"is_active": True}
            )
            cf_loc = LeadCustomField.objects.filter(hospital=user_hospital, name="location").first()
            if cf_loc:
                existing_lopts = [o.strip() for o in cf_loc.options.split(",") if o.strip()]
                if not any(o.lower() == city_name.lower() for o in existing_lopts):
                    existing_lopts.append(city_name)
                    cf_loc.options = ", ".join(existing_lopts)
                    cf_loc.save(update_fields=["options"])

        custom_data_payload = {}
        if parsed.get("doctor") and str(parsed["doctor"]).strip().lower() not in ("nan", "none", "null", "-", "nat"):
            custom_data_payload["doctor"] = str(parsed["doctor"]).strip()
        if parsed.get("department") and str(parsed["department"]).strip().lower() not in ("nan", "none", "null", "-", "nat"):
            custom_data_payload["department"] = str(parsed["department"]).strip()
        if parsed.get("age") is not None and pd.notna(parsed["age"]):
            c_age = str(parsed["age"]).strip()
            if c_age.lower() not in ("nan", "none", "null", "-", "nat", ""):
                if c_age.endswith(".0"):
                    c_age = c_age[:-2]
                custom_data_payload["age"] = c_age
        if parsed.get("gender") and str(parsed["gender"]).strip().lower() not in ("nan", "none", "null", "-", "nat"):
            custom_data_payload["gender"] = str(parsed["gender"]).strip()

        if existing and on_duplicate == "update":
            existing.city = parsed["city"] or existing.city
            existing.email = parsed["email"] or existing.email
            if parsed.get("assigned_user"):
                existing.assigned_to = parsed["assigned_user"]
            if campaign_obj:
                existing.campaign = campaign_obj
            if parsed["notes"]:
                existing.notes = (existing.notes + "\n" + parsed["notes"]).strip()
            if custom_data_payload:
                if not isinstance(existing.custom_data, dict):
                    existing.custom_data = {}
                existing.custom_data.update(custom_data_payload)
            existing.import_job = job
            existing.import_source_file = job.original_filename
            existing.save()
            lead = existing
            updated += 1
        else:
            lead = Lead.objects.create(
                name=parsed["name"], mobile=parsed["mobile"], alternate_mobile=parsed["alt_mobile"],
                email=parsed["email"], city=parsed["city"], location=parsed["city"],
                campaign=campaign_obj,
                assigned_to=parsed.get("assigned_user"),
                temperature=parsed["temperature"], stage=default_stage,
                deal_status=parsed["deal_status"], admission_status=parsed["admission_status"],
                inquiry_date=parsed["inquiry_date"], source_category=cat, lead_source=src,
                notes=parsed["notes"], created_by=request.user, hospital=user_hospital,
                custom_data=custom_data_payload,
                import_source_file=job.original_filename, import_source_sheet=job.sheet_name,
                import_source_row=row_num, import_job=job,
            )
            imported += 1

        # historical follow-up date columns -> FollowUp records
        for idx in date_cols_idx:
            if idx >= len(cols):
                continue
            comment = row.iloc[idx]
            if pd.isna(comment) or str(comment).strip() in ("", "-", "nan", "NaT"):
                continue
            fu_date = cleaning.parse_date(cols[idx]) or parsed["inquiry_date"]
            FollowUp.objects.create(
                lead=lead, followup_date=fu_date, followup_mode=FollowUpMode.OTHER,
                followup_status=FollowUpStatus.COMPLETED, comment=str(comment).strip(),
                created_by=request.user, imported_from_excel=True,
            )

    job.imported_count = imported + updated
    job.updated_count = updated
    job.duplicate_count = duplicate
    job.invalid_count = invalid
    job.status = ImportJob.Status.DONE
    job.completed_at = timezone.now()
    job.save()

    messages.success(request, f"Import complete: {imported} created, {updated} updated, {duplicate} duplicate skipped, {invalid} invalid.")
    return redirect("imports:job_detail", pk=job.pk)


@login_required
@user_passes_test(lambda u: u.can_import_export)
def job_detail(request, pk):
    job = get_object_or_404(ImportJob, pk=pk)
    return render(request, "imports/job_detail.html", {"active": "import_history", "job": job, "errors": job.errors.all()[:200]})


@login_required
@user_passes_test(lambda u: u.can_import_export)
def history(request):
    from datetime import datetime, timedelta
    from django.utils import timezone
    from django.db.models import Q

    user = request.user
    jobs_qs = ImportJob.objects.select_related("created_by")
    if user.hospital:
        jobs_qs = jobs_qs.filter(created_by__hospital=user.hospital)

    date_preset = request.GET.get('date_preset', 'all_time')
    start_date_str = request.GET.get('start_date', '')
    end_date_str = request.GET.get('end_date', '')
    
    today = timezone.localdate()
    filter_start = None
    filter_end = None
    preset_label = "All Time"

    if date_preset == 'today':
        filter_start = today
        filter_end = today
        preset_label = f"Today ({today.strftime('%d-%m-%Y')})"
    elif date_preset == 'yesterday':
        yesterday = today - timedelta(days=1)
        filter_start = yesterday
        filter_end = yesterday
        preset_label = f"Yesterday ({yesterday.strftime('%d-%m-%Y')})"
    elif date_preset == 'last_7d':
        filter_start = today - timedelta(days=7)
        filter_end = today
        preset_label = f"Last 7 Days ({filter_start.strftime('%d-%m-%Y')} to {filter_end.strftime('%d-%m-%Y')})"
    elif date_preset == 'this_month':
        filter_start = today.replace(day=1)
        filter_end = today
        preset_label = f"This Month ({filter_start.strftime('%d-%m-%Y')} to {filter_end.strftime('%d-%m-%Y')})"
    elif date_preset == 'custom' and start_date_str:
        try:
            filter_start = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            filter_end = datetime.strptime(end_date_str, '%Y-%m-%d').date() if end_date_str else filter_start
            preset_label = f"{filter_start.strftime('%d-%m-%Y')} to {filter_end.strftime('%d-%m-%Y')}"
        except ValueError:
            filter_start = None
            filter_end = None
            preset_label = "All Time"

    if filter_start and filter_end:
        jobs_qs = jobs_qs.filter(created_at__date__gte=filter_start, created_at__date__lte=filter_end)

    hospital_name = user.hospital.name if user.hospital else "Zappcode CRM"

    return render(request, "imports/history.html", {
        "active": "import_history",
        "jobs": jobs_qs,
        "date_preset": date_preset,
        "start_date": start_date_str,
        "end_date": end_date_str,
        "preset_label": preset_label,
        "hospital_name": hospital_name,
        "today_date_str": today.strftime('%d-%m-%Y'),
    })


@login_required
def export_leads(request):
    is_hospital = bool(request.user.hospital)
    is_download = request.GET.get("download") == "1"
    is_preview = request.GET.get("preview") == "1"
    
    if not is_download and not is_preview:
        from leads.models import SourceCategory, LeadSource, Campaign, Course, LeadStage
        from accounts.models import User
        
        base_leads = Lead.objects.filter(is_archived=False)
        if is_hospital:
            base_leads = base_leads.filter(hospital=request.user.hospital)
            
        used_sc_ids = base_leads.values_list("source_category_id", flat=True).distinct()
        used_stage_ids = base_leads.values_list("stage_id", flat=True).distinct()
        used_emp_ids = base_leads.values_list("assigned_to_id", flat=True).distinct()
        
        source_categories = SourceCategory.objects.filter(id__in=used_sc_ids)
        stages = LeadStage.objects.filter(id__in=used_stage_ids)
        employees = User.objects.filter(id__in=used_emp_ids)
        
        nelson_locations = []
        nelson_campaigns = []
        nelson_lead_sources = []
        nelson_deal_statuses = []
        
        if is_hospital:
            from leads.models import MasterGroup
            def get_master(name):
                grp = MasterGroup.objects.filter(name=name).first()
                if grp:
                    return grp.items.filter(hospital=request.user.hospital, is_active=True).values_list("name", flat=True)
                return []
            nelson_locations = get_master("Locations")
            nelson_campaigns = get_master("Campaigns")
            nelson_lead_sources = get_master("Lead Sources")
            nelson_deal_statuses = get_master("Deal Statuses")
            
            lead_sources = []
            campaigns = []
            distinct_cities = []
        else:
            used_ls_ids = base_leads.values_list("lead_source_id", flat=True).distinct()
            used_camp_ids = base_leads.values_list("campaign_id", flat=True).distinct()
            lead_sources = LeadSource.objects.filter(id__in=used_ls_ids)
            campaigns = Campaign.objects.filter(id__in=used_camp_ids)
            distinct_cities = sorted(list(set(base_leads.exclude(city="").values_list("city", flat=True))))
        
        # Only include courses if it's not a hospital tenant
        courses = Course.objects.all() if not is_hospital else []
        
        context = {
            "active": "export",
            "source_categories": source_categories,
            "lead_sources": lead_sources,
            "campaigns": campaigns,
            "courses": courses,
            "stages": stages,
            "employees": employees,
            "cities": distinct_cities,
            "is_hospital": is_hospital,
            "nelson_locations": nelson_locations,
            "nelson_campaigns": nelson_campaigns,
            "nelson_lead_sources": nelson_lead_sources,
            "nelson_deal_statuses": nelson_deal_statuses,
        }
        return render(request, "imports/export_leads_filter.html", context)



    from django.db.models import Q
    from leads.views import FK_FILTER_FIELDS, CHAR_FILTER_FIELDS
    leads = Lead.objects.select_related("course", "stage", "lead_source", "source_category", "assigned_to").filter(is_archived=False)
    
    if is_hospital:
        leads = leads.filter(hospital=request.user.hospital)
        
    q = request.GET.get("q", "").strip()
    if q:
        if is_hospital:
            leads = leads.filter(
                Q(lead_code__icontains=q) | Q(name__icontains=q) | Q(mobile__icontains=q)
                | Q(email__icontains=q) | Q(location__icontains=q)
                | Q(custom_data__lead_source__icontains=q) | Q(custom_data__campaign__icontains=q)
            )
        else:
            leads = leads.filter(
                Q(lead_code__icontains=q) | Q(name__icontains=q) | Q(mobile__icontains=q)
                | Q(email__icontains=q) | Q(city__icontains=q) | Q(course__name__icontains=q)
                | Q(lead_source__name__icontains=q) | Q(campaign__name__icontains=q)
            )
        
    for field in FK_FILTER_FIELDS:
        val = request.GET.get(field)
        if val:
            if is_hospital and field in ['campaign', 'lead_source']:
                leads = leads.filter(**{f"custom_data__{field}": val})
            else:
                leads = leads.filter(**{f"{field}_id": val})

    for field in CHAR_FILTER_FIELDS:
        val = request.GET.get(field)
        if val:
            leads = leads.filter(**{field: val})

    if is_hospital:
        location = request.GET.get("location")
        if location:
            leads = leads.filter(location__iexact=location)
            
        deal_status = request.GET.get("deal_status")
        if deal_status:
            leads = leads.filter(custom_data__deal_status=deal_status)
    else:
        city = request.GET.get("city")
        if city:
            leads = leads.filter(city__iexact=city)
        
        deal_status = request.GET.get("deal_status")
        if deal_status:
            leads = leads.filter(deal_status=deal_status)
            
        admission_status = request.GET.get("admission_status")
        if admission_status:
            leads = leads.filter(admission_status=admission_status)

    def _parse_date_input(val):
        if not val:
            return None
        from datetime import datetime
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(val.strip(), fmt).date()
            except ValueError:
                continue
        return None

    date_from = _parse_date_input(request.GET.get("date_from"))
    date_to = _parse_date_input(request.GET.get("date_to"))
    if date_from or date_to:
        from datetime import datetime, time
        from django.utils import timezone
        tz = timezone.get_current_timezone()
        
        # We match on either created_at datetime range or inquiry_date date range for maximum compatibility
        q_date = Q()
        if date_from:
            dt_from = timezone.make_aware(datetime.combine(date_from, time.min), tz)
            q_date &= (Q(created_at__gte=dt_from) | Q(inquiry_date__gte=date_from))
        if date_to:
            dt_to = timezone.make_aware(datetime.combine(date_to, time.max), tz)
            q_date &= (Q(created_at__lte=dt_to) | Q(inquiry_date__lte=date_to))
        
        leads = leads.filter(q_date)

    def build_row(l):
        if is_hospital:
            cd = l.custom_data or {}
            doc = cd.get("doctor") or ""
            dept = cd.get("department") or ""
            loc = cd.get("location") or l.city or l.location or ""
            src = cd.get("lead_source") or (l.lead_source.name if l.lead_source else "")
            camp = cd.get("campaign") or (l.campaign.name if l.campaign else "")
            apt_st = cd.get("appointment_status") or l.display_status
            
            return {
                "Lead ID": l.lead_code,
                "Patient Name": l.name,
                "Mobile": l.mobile,
                "Email": l.email,
                "Location": loc,
                "Doctor / Consultant": doc,
                "Department": dept,
                "Lead Source": src,
                "Campaign": camp,
                "Status": l.display_status,
                "Appointment Status": apt_st,
                "Temperature": l.get_temperature_display(),
                "Inquiry Date": str(l.inquiry_date or ""),
                "Assigned To": str(l.assigned_to.get_full_name() if l.assigned_to else (cd.get("lead_attendant") or "")),
                "Next Follow-up": str(l.next_followup_date) if l.next_followup_date else "", 
                "Created At": l.effective_created_formatted or (l.created_at.strftime("%Y-%m-%d %H:%M") if l.created_at else ""),
            }
        else:
            return {
                "Lead ID": l.lead_code, "Name": l.name, "Mobile": l.mobile, "Email": l.email,
                "City": l.city, "Course": str(l.course or ""),
                "Lead Source": str(l.lead_source or ""), "Campaign": str(l.campaign or ""),
                "Stage": str(l.stage), "Temperature": l.get_temperature_display(),
                "Deal Status": l.get_deal_status_display(), "Admission Status": l.get_admission_status_display(),
                "Inquiry Date": str(l.inquiry_date), "Assigned To": str(l.assigned_to or ""),
                "Next Follow-up": str(l.next_followup_date) if l.next_followup_date else "", 
                "Created At": l.effective_created_formatted or (l.created_at.strftime("%Y-%m-%d %H:%M") if l.created_at else ""),
            }

    if is_preview:
        total_count = leads.count()
        preview_leads = leads.order_by("-id")[:10]
        rows = [build_row(l) for l in preview_leads]
        from django.http import JsonResponse
        return JsonResponse({"total_count": total_count, "rows": rows})

    # Download
    rows = [build_row(l) for l in leads]
    df = pd.DataFrame(rows)
    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="leads_export_{timezone.now().strftime("%Y%m%d_%H%M")}.xlsx"'
    df.to_excel(response, index=False, sheet_name="Leads")
    return response


@login_required
@user_passes_test(_can_user_access_import)
def download_template(request):
    """
    Downloads entity-specific Lead Import Template:
    - Hospital template for Nelson / Hospital users.
    - Student/Course template for Zappcode Academy users.
    """
    from openpyxl import Workbook
    from openpyxl.worksheet.datavalidation import DataValidation
    from openpyxl.styles import Font, PatternFill, Alignment
    from leads.models import (
        HospitalDepartment, HospitalDoctor, HospitalBranch, 
        LeadSource, LeadTemperature, AppointmentStatus, Campaign as HospitalCampaign, Course
    )

    wb = Workbook()
    ws = wb.active

    is_hospital = request.user.is_hospital_user

    if is_hospital:
        ws.title = "Hospital Leads Template"
        headers = [
            "Inquiry Date", 
            "Patient Name", 
            "Mobile", 
            "Alternate Mobile", 
            "Email", 
            "Location / City", 
            "Department", 
            "Doctor / Consultant", 
            "Campaign",
            "Lead Source", 
            "Lead Priority / Temp", 
            "Appointment Status", 
            "Notes / Medical Concern"
        ]
        sample_row = [
            "10-09-2026", "Ramesh Kumar", "9876543210", "", "ramesh@example.com",
            "Nagpur", "NEUROLOGY", "Dr. Sharma", "Nelson Neuro Camp", "Meta Ads", "Hot", "Booked", "Consultation needed"
        ]
    else:
        ws.title = "Zappcode Leads Template"
        headers = [
            "Inquiry Date",
            "Full Name",
            "Phone",
            "Email",
            "City",
            "Course / Service",
            "Lead Source",
            "Campaign Name",
            "Timeline",
            "Notes / Query"
        ]
        sample_row = [
            "10-09-2026", "Rahul Verma", "9876543210", "rahul@example.com",
            "Nagpur", "Data Analytics", "Meta Ads", "Python & Data Science Campaign", "Immediate", "Looking for placement assistance"
        ]
    
    ws.append(headers)
    ws.append(sample_row)

    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4F46E5" if not is_hospital else "1F497D", end_color="4F46E5" if not is_hospital else "1F497D", fill_type="solid")
    align_center = Alignment(horizontal="center", vertical="center")
    
    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = align_center

    sample_font = Font(name="Calibri", size=10, italic=True, color="595959")
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=2, column=col_idx)
        cell.font = sample_font

    user_hospital = request.user.hospital
    data_ws = wb.create_sheet(title="DropdownData")

    if is_hospital:
        if user_hospital:
            departments = list(HospitalDepartment.objects.filter(hospital=user_hospital, is_active=True).values_list("name", flat=True))
            doctors = list(HospitalDoctor.objects.filter(hospital=user_hospital, is_active=True).values_list("name", flat=True))
            campaigns = list(HospitalCampaign.objects.filter(hospital=user_hospital, is_active=True).values_list("name", flat=True))
        else:
            departments = list(HospitalDepartment.objects.filter(is_active=True).values_list("name", flat=True))
            doctors = list(HospitalDoctor.objects.filter(is_active=True).values_list("name", flat=True))
            campaigns = list(HospitalCampaign.objects.filter(is_active=True).values_list("name", flat=True))

        sources = list(LeadSource.objects.filter(is_active=True).values_list("name", flat=True))
        if not sources:
            sources = ["Instagram", "Facebook", "Meta Ads", "Google Ads", "Website", "WhatsApp", "Walk-in"]
            
        temperatures = ["HOT", "WARM", "COLD", "UNCONTACTED"]
        appt_statuses = [choice[0] for choice in AppointmentStatus.choices]
        
        for idx, item in enumerate(departments, start=1):
            data_ws.cell(row=idx, column=1, value=item)
        for idx, item in enumerate(doctors, start=1):
            data_ws.cell(row=idx, column=2, value=item)
        for idx, item in enumerate(campaigns, start=1):
            data_ws.cell(row=idx, column=3, value=item)
        for idx, item in enumerate(sources, start=1):
            data_ws.cell(row=idx, column=4, value=item)
        for idx, item in enumerate(temperatures, start=1):
            data_ws.cell(row=idx, column=5, value=item)
        for idx, item in enumerate(appt_statuses, start=1):
            data_ws.cell(row=idx, column=6, value=item)

        def add_validation(col_letter, data_col_letter, count, prompt):
            if count == 0:
                return
            dv = DataValidation(
                type="list", 
                formula1=f"DropdownData!${data_col_letter}$1:${data_col_letter}${count}", 
                allow_blank=True
            )
            dv.error = 'Your entry is not in the list'
            dv.errorTitle = 'Invalid Entry'
            dv.prompt = prompt
            dv.promptTitle = 'Select from list'
            ws.add_data_validation(dv)
            dv.add(f"{col_letter}3:{col_letter}1000")

        add_validation("G", "A", len(departments), "Select a department")
        add_validation("H", "B", len(doctors), "Select a doctor")
        add_validation("I", "C", len(campaigns), "Select a campaign")
        add_validation("J", "D", len(sources), "Select a lead source")
        add_validation("K", "E", len(temperatures), "Select temperature / priority")
        add_validation("L", "F", len(appt_statuses), "Select appointment status")
    else:
        # Zappcode Academy Template dropdowns
        courses = list(Course.objects.filter(is_active=True).values_list("name", flat=True))
        if not courses:
            courses = ["Full Stack Python", "Data Analytics", "Java Full Stack", "Web Development", "UI/UX Design", "Digital Marketing"]
        sources = list(LeadSource.objects.filter(is_active=True).values_list("name", flat=True))
        if not sources:
            sources = ["Meta Ads", "Google Ads", "Instagram", "Facebook", "LinkedIn", "Website", "Walk-in", "Referral"]
        timelines = ["Immediate", "Within 1 Week", "Within 1 Month", "Next Batch", "Information Only"]

        for idx, item in enumerate(courses, start=1):
            data_ws.cell(row=idx, column=1, value=item)
        for idx, item in enumerate(sources, start=1):
            data_ws.cell(row=idx, column=2, value=item)
        for idx, item in enumerate(timelines, start=1):
            data_ws.cell(row=idx, column=3, value=item)

        def add_validation(col_letter, data_col_letter, count, prompt):
            if count == 0:
                return
            dv = DataValidation(
                type="list", 
                formula1=f"DropdownData!${data_col_letter}$1:${data_col_letter}${count}", 
                allow_blank=True
            )
            dv.error = 'Your entry is not in the list'
            dv.errorTitle = 'Invalid Entry'
            dv.prompt = prompt
            dv.promptTitle = 'Select from list'
            ws.add_data_validation(dv)
            dv.add(f"{col_letter}3:{col_letter}1000")

        add_validation("F", "A", len(courses), "Select a course")
        add_validation("G", "B", len(sources), "Select a lead source")
        add_validation("I", "C", len(timelines), "Select enrollment timeline")

    data_ws.sheet_state = "hidden"

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = col[0].column_letter
        ws.column_dimensions[col_letter].width = max(max_len + 3, 15)

    filename = "nelson_hospital_leads_template.xlsx" if is_hospital else "zappcode_academy_leads_template.xlsx"
    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


@login_required
@user_passes_test(lambda u: u.can_import_export)
def quick_import(request):
    if request.method == "POST" and "excel_file" in request.FILES:
        excel_file = request.FILES["excel_file"]
        on_duplicate = request.POST.get("on_duplicate", "skip")
        
        job = ImportJob.objects.create(
            file=excel_file,
            original_filename=excel_file.name,
            created_by=request.user,
        )
        
        real_file_path = job.file.path
        df = None
        for eng in ["openpyxl", "xlrd", None]:
            try:
                if eng:
                    df = pd.read_excel(real_file_path, sheet_name=0, engine=eng)
                else:
                    df = pd.read_excel(real_file_path, sheet_name=0)
                break
            except Exception:
                continue
                
        if df is None:
            try:
                df = pd.read_csv(real_file_path, encoding="utf-8-sig")
            except Exception:
                try:
                    df = pd.read_csv(real_file_path, encoding="latin1")
                except Exception as e:
                    job.delete()
                    messages.error(request, f"Could not read the uploaded file: {e}")
                    return redirect("imports:upload")
        
        df = df.dropna(how="all")
        df = df.dropna(how="all")
        df.columns = [str(c).strip() for c in df.columns]
        cols = list(df.columns)
        
        target_hospital_id = request.POST.get("target_hospital_id")
        user_hospital = None
        if target_hospital_id:
            user_hospital = Hospital.objects.filter(pk=target_hospital_id).first()
        elif request.user.hospital and "zappcode" not in (request.user.hospital.name or "").lower():
            user_hospital = request.user.hospital

        # Optional manual campaign association for historical data
        selected_campaign_id = request.POST.get("campaign_id")
        selected_campaign = None
        if selected_campaign_id:
            selected_campaign = HospitalCampaign.objects.filter(pk=selected_campaign_id).first()

        # Dynamic Column Matcher
        def find_matching_col(aliases):
            for col in cols:
                c_clean = col.lower().replace("_", " ").strip()
                for alias in aliases:
                    if alias in c_clean or c_clean == alias:
                        return col
            return None

        # Nelson Master File & General Lead Columns Mapping
        col_date = find_matching_col(["lead receive date", "lead_receive_date", "receive date", "inquiry date", "created at", "created_at", "lead date", "lead time", "date", "created time"])
        col_name = find_matching_col(["patient name", "patient_name", "your_name", "your name", "full name", "lead name", "customer name", "client name", "user name", "name", "first name", "naam"])
        col_mobile = find_matching_col(["contact", "contact number", "contact no", "phone_number", "phone number", "mobile number", "mobile", "phone", "call number", "whatsapp number", "whatsapp", "cell"])
        col_city = find_matching_col(["location", "city", "address", "area", "town", "district"])
        col_dept = find_matching_col(["department", "speciality", "dept", "specialization"])
        col_doctor = find_matching_col(["doctor", "dr name", "consultant", "physician", "surgeon"])
        col_assigned = find_matching_col(["lead attendent", "lead attendant", "attendent", "attendant", "assigned to", "assigned", "telecaller", "tele caller", "executive", "caller", "agent", "lead owner", "owner", "assignee", "counsellor", "counselor"])
        col_due_date = find_matching_col(["due date", "due_date", "edd", "expected delivery date", "delivery date"])
        col_campaign = find_matching_col(["campaign name", "campaign", "ad name", "ad set name"])
        col_source = find_matching_col(["lead source", "source", "platform", "publisher platform", "channel", "origin"])
        col_appt_status = find_matching_col(["appointment status", "appointment_status", "appo book", "appo_book", "appointment state"])
        col_notes = find_matching_col(["remarks", "remark", "comment", "issue", "note", "notes", "symptom", "problem", "query", "समस्या", "रोग"])
        col_branch = find_matching_col(["hosptal branch", "hospital branch", "branch", "hospital_branch"])
        col_recv_time = find_matching_col(["lead receive time", "receive time", "lead_receive_time", "inquiry time"])
        col_calling_time = find_matching_col(["lead calling time", "calling time", "lead_calling_time", "first calling time"])
        col_gender = find_matching_col(["gender", "sex", "m/f"])
        col_age = find_matching_col(["age", "years", "yrs"])
        col_appt_date = find_matching_col(["appointment date", "appointment_date", "appo booked date", "appo_booked_date"])

        # Follow-ups columns (1st, 2nd, 3rd)
        col_fu1_date = find_matching_col(["first follow up date", "first followup date", "1st follow up date", "1st followup date", "calling_date_remark_1", "follow up 1 date"])
        col_fu1_remark = find_matching_col(["first follow up remark", "first followup remark", "1st follow up remark", "1st followup remark", "remark 1", "remark_1", "follow up 1 remark"])
        col_fu1_time = find_matching_col(["first follow up calling time", "1st follow up calling time", "calling_time_remark_1", "calling_time_remark_2", "first followup calling time"])

        col_fu2_date = find_matching_col(["second follow up date", "second followup date", "2nd follow up date", "2nd followup date", "calling_date_remark_2", "follow up 2 date"])
        col_fu2_remark = find_matching_col(["second follow up remark", "second followup remark", "2nd follow up remark", "2nd followup remark", "remark 2", "remark_2", "follow up 2 remark"])

        col_fu3_date = find_matching_col(["third follow up date", "third followup date", "3rd follow up date", "3rd followup date", "calling_date_remark_3", "follow up 3 date"])
        col_fu3_remark = find_matching_col(["third follow up remark", "third followup remark", "3rd follow up remark", "3rd followup remark", "remark 3", "remark_3", "follow up 3 remark"])

        # Final Status, Visit, UHID, Bills, Periods
        col_final_status = find_matching_col(["final status", "final_status", "deal status", "deal_status", "done"])
        col_visit_date = find_matching_col(["visit date", "visit_date", "hospital visit date"])
        col_uhid = find_matching_col(["uhid id no", "uhid no", "uhid", "uhid_id_no", "patient id"])
        col_pharmacy_bill = find_matching_col(["pharmacy bill", "pharmacy_bill", "pharmacy"])
        col_opd_bill = find_matching_col(["opd bill", "opd_bill", "opd"])
        col_ipd_bill = find_matching_col(["ipd bill", "ipd_bill", "ipd no", "ipd_no", "ipd"])
        col_investigation_bill = find_matching_col(["investigation bill", "investigation", "investigation_bill", "lab bill"])
        col_total_bill = find_matching_col(["total bill", "total_bill", "total amount", "total paid", "total"])
        col_month = find_matching_col(["month"])
        col_year = find_matching_col(["year"])
        col_weekdays = find_matching_col(["weekdays", "weekday", "day"])
        col_course = find_matching_col(["course / service", "course", "service", "program", "stream", "specialization"])
        col_email = find_matching_col(["email address", "e-mail", "email", "mail"])

        def clean_val_str(v):
            if v is None:
                return ""
            if pd.isna(v):
                return ""
            s = str(v).strip()
            if s.lower() in ("nan", "none", "null", "-", "na", "nat", ""):
                return ""
            if s.endswith(".0") and re.match(r"^\d+\.0$", s):
                return s[:-2]
            return s

        def parse_clean_date(raw):
            if not raw or pd.isna(raw):
                return None
            return cleaning.parse_date(raw)

        def parse_clean_decimal(raw):
            if raw is None or pd.isna(raw):
                return 0.0
            cleaned_s = re.sub(r"[^\d.]", "", str(raw).strip())
            try:
                return float(cleaned_s) if cleaned_s else 0.0
            except (ValueError, TypeError):
                return 0.0

        if not col_mobile:
            job.delete()
            messages.error(
                request, 
                "Could not detect Mobile / Contact column in your file. "
                "Please make sure your sheet has a column for Contact / Phone / Mobile (e.g. 'Contact', 'phone_number', 'Mobile Number', 'Phone')."
            )
            return redirect("imports:upload")
            
        default_stage = _default_stage()
        from leads.models import Campaign as HospitalCampaign
        
        imported = updated = skipped = duplicate = invalid = 0
        unknown_counter = 1
        
        # 1. Pre-cache all active users for instant attendant matching (eliminates remote DB queries in loop)
        user_cache = {}
        all_active_users = list(User.objects.filter(is_active=True).select_related('hospital'))
        for u in all_active_users:
            u_key_list = []
            if u.username:
                u_key_list.append(u.username.strip().lower())
            fname = (u.get_full_name() or "").strip().lower()
            if fname:
                u_key_list.append(fname)
            if u.first_name:
                u_key_list.append(u.first_name.strip().lower())
            
            for k in u_key_list:
                if user_hospital:
                    if u.hospital_id == user_hospital.id:
                        user_cache[k] = u
                    elif k not in user_cache:
                        user_cache[k] = u
                else:
                    if k not in user_cache:
                        user_cache[k] = u

        def fast_resolve_user(raw_val):
            if not raw_val:
                return None
            s = str(raw_val).strip().lower()
            if s in ("", "-", "nan", "nat", "none", "null"):
                return None
            if s in user_cache:
                return user_cache[s]
            for k, u in user_cache.items():
                if k in s or s in k:
                    return u
            return None

        # 2. Pre-cache campaigns and courses for the hospital
        campaign_cache = {}
        camp_qs = HospitalCampaign.objects.all()
        if user_hospital:
            camp_qs = camp_qs.filter(hospital=user_hospital)
        for c in camp_qs:
            campaign_cache[c.name.strip().lower()] = c

        course_cache = {}
        course_qs = Course.objects.all()
        if user_hospital:
            course_qs = course_qs.filter(hospital=user_hospital)
        for crs in course_qs:
            course_cache[crs.name.strip().lower()] = crs

        # 3. Pre-cache lead sources
        source_cache = {}
        for src in LeadSource.objects.select_related('category').all():
            source_cache[src.name.strip().lower()] = src

        # 4. Pre-cache existing leads mobile numbers
        existing_mobile_map = {}
        for lead_id, raw_mob in Lead.objects.values_list("id", "mobile"):
            if raw_mob:
                cleaned = Lead.clean_mobile(raw_mob)
                if cleaned and cleaned not in existing_mobile_map:
                    existing_mobile_map[cleaned] = lead_id

        # 5. Pre-generate starting lead sequence to avoid querying DB for every row
        year = timezone.now().year
        hosp_prefix = "NL-" if (user_hospital and "nelson" in (user_hospital.name or "").lower()) else "LD-"
        full_prefix = f"{hosp_prefix}{year}-"
        last_lead = Lead.objects.filter(lead_code__startswith=full_prefix).order_by("-lead_code").first()
        current_seq = (int(last_lead.lead_code.split("-")[-1]) if (last_lead and last_lead.lead_code and "-" in last_lead.lead_code) else 0)

        start_idx = 0
        if len(df) > 0 and col_name:
            first_row_name = str(df.iloc[0].get(col_name, "")).strip().lower()
            first_row_mobile = str(df.iloc[0].get(col_mobile, "")).strip()
            if "rahul kumar" in first_row_name or "9876543210" in first_row_mobile:
                start_idx = 1
                
        followups_to_create = []

        for idx in range(start_idx, len(df)):
            row = df.iloc[idx]
            row_num = idx + 2
            
            name = str(row.get(col_name, "")).strip() if col_name else ""
            if name.lower() in ("nan", "none", "null", "-", "na", "nat"):
                name = ""
            
            mobile_raw = row.get(col_mobile)
            mobile, alt_mobile = cleaning.clean_phone(mobile_raw)
            email = str(row.get(col_email, "") or "").strip() if col_email else ""
            if email.lower() in ("nan", "none", "null", "-", "na", "nat"):
                email = ""

            # If name is empty, extract name from email address
            if not name and email and "@" in email:
                email_user = email.split("@")[0]
                clean_email_name = re.sub(r"[0-9_\.\-]+", " ", email_user).strip().title()
                if len(clean_email_name) >= 2:
                    name = clean_email_name

            # If still no name, give numbered unique sequence e.g. "Unknown Patient 1", "Unknown Patient 2"
            if not name:
                name = f"Unknown Patient {unknown_counter}"
                unknown_counter += 1

            if not mobile:
                invalid += 1
                continue
                
            # Basic fields
            email = clean_val_str(row.get(col_email)) if col_email else ""
            city = clean_val_str(row.get(col_city)) if col_city else ""
            course_val = clean_val_str(row.get(col_course)) if col_course else ""
            gender = clean_val_str(row.get(col_gender)) if col_gender else ""
            age_val = row.get(col_age) if col_age else ""
            doctor_val = clean_val_str(row.get(col_doctor)) if col_doctor else ""
            dept_val = clean_val_str(row.get(col_dept)) if col_dept else ""
            campaign_val = clean_val_str(row.get(col_campaign)) if col_campaign else ""
            source_raw = clean_val_str(row.get(col_source)) if col_source else ""
            assigned_raw = clean_val_str(row.get(col_assigned)) if col_assigned else ""
            assigned_user = fast_resolve_user(assigned_raw)
            due_date_val = clean_val_str(row.get(col_due_date)) if col_due_date else ""
            date_raw = row.get(col_date) if col_date else None
            inquiry_date = parse_clean_date(date_raw) or timezone.localdate()

            # Nelson Specific Fields
            appt_status_val = clean_val_str(row.get(col_appt_status)) if col_appt_status else ""
            branch_val = clean_val_str(row.get(col_branch)) if col_branch else ""
            recv_time_val = clean_val_str(row.get(col_recv_time)) if col_recv_time else ""
            calling_time_val = clean_val_str(row.get(col_calling_time)) if col_calling_time else ""
            appt_date_raw = row.get(col_appt_date) if col_appt_date else None
            appt_date_val = parse_clean_date(appt_date_raw)

            # Follow-ups (1, 2, 3)
            fu1_date_raw = row.get(col_fu1_date) if col_fu1_date else None
            fu1_date = parse_clean_date(fu1_date_raw)
            fu1_remark = clean_val_str(row.get(col_fu1_remark)) if col_fu1_remark else ""
            fu1_time = clean_val_str(row.get(col_fu1_time)) if col_fu1_time else ""

            fu2_date_raw = row.get(col_fu2_date) if col_fu2_date else None
            fu2_date = parse_clean_date(fu2_date_raw)
            fu2_remark = clean_val_str(row.get(col_fu2_remark)) if col_fu2_remark else ""

            fu3_date_raw = row.get(col_fu3_date) if col_fu3_date else None
            fu3_date = parse_clean_date(fu3_date_raw)
            fu3_remark = clean_val_str(row.get(col_fu3_remark)) if col_fu3_remark else ""

            # Financial, Status & Details
            final_status_val = clean_val_str(row.get(col_final_status)) if col_final_status else ""
            visit_date_raw = row.get(col_visit_date) if col_visit_date else None
            visit_date_val = parse_clean_date(visit_date_raw)
            uhid_val = clean_val_str(row.get(col_uhid)) if col_uhid else ""
            pharmacy_bill_val = parse_clean_decimal(row.get(col_pharmacy_bill)) if col_pharmacy_bill else 0.0
            opd_bill_val = parse_clean_decimal(row.get(col_opd_bill)) if col_opd_bill else 0.0
            ipd_bill_val = parse_clean_decimal(row.get(col_ipd_bill)) if col_ipd_bill else 0.0
            investigation_bill_val = clean_val_str(row.get(col_investigation_bill)) if col_investigation_bill else ""
            total_bill_val = parse_clean_decimal(row.get(col_total_bill)) if col_total_bill else 0.0
            if total_bill_val == 0.0 and (pharmacy_bill_val or opd_bill_val or ipd_bill_val):
                total_bill_val = pharmacy_bill_val + opd_bill_val + ipd_bill_val

            month_val = clean_val_str(row.get(col_month)) if col_month else ""
            year_val = clean_val_str(row.get(col_year)) if col_year else ""
            weekdays_val = clean_val_str(row.get(col_weekdays)) if col_weekdays else ""
            base_notes = clean_val_str(row.get(col_notes)) if col_notes else ""
            
            # Auto-gather unmapped / survey questions
            known_cols = [c for c in [
                col_name, col_mobile, col_email, col_city, col_course, col_gender, col_age, 
                col_doctor, col_dept, col_campaign, col_source, col_assigned, col_due_date, col_date, col_notes,
                col_appt_status, col_branch, col_recv_time, col_calling_time, col_appt_date,
                col_fu1_date, col_fu1_remark, col_fu1_time, col_fu2_date, col_fu2_remark,
                col_fu3_date, col_fu3_remark, col_final_status, col_visit_date, col_uhid,
                col_pharmacy_bill, col_opd_bill, col_ipd_bill, col_investigation_bill, col_total_bill,
                col_month, col_year, col_weekdays
            ] if c]
            
            survey_notes = []
            for col in cols:
                if col not in known_cols:
                    col_l = col.lower()
                    if any(t in col_l for t in ["ad_id", "adset_id", "campaign_id", "form_id", "lead_id", "hospital_id", "is_organic", "unnamed"]):
                        continue
                    v = row.get(col)
                    if pd.notna(v) and str(v).strip() not in ("", "-", "nan", "NaT"):
                        clean_col_label = col.replace("_", " ").strip()
                        survey_notes.append(f"[{clean_col_label}]: {str(v).strip()}")
                        
            all_notes = []
            if base_notes:
                all_notes.append(base_notes)
            if survey_notes:
                all_notes.extend(survey_notes)
            combined_notes = "\n".join(all_notes)
            
            source_cat, source_name, _ = cleaning.normalize_source(source_raw)
            
            # Fast in-memory duplicate lookup
            cleaned_mob = Lead.clean_mobile(mobile)
            existing_id = existing_mobile_map.get(cleaned_mob)
            existing = None
            if existing_id:
                if on_duplicate == "skip":
                    duplicate += 1
                    continue
                else:
                    existing = Lead.objects.filter(pk=existing_id).first()
                
            # Fast cached source retrieval
            src_key = (source_name or "Meta Ads").strip().lower()
            src = source_cache.get(src_key)
            cat = src.category if src else None
            if not src and source_name:
                cat, src = _get_or_create_source(source_cat, source_name)
                if src:
                    source_cache[src_key] = src
            
            # Fast cached course matching
            course_obj = None
            if course_val:
                crs_key = course_val.strip().lower()
                course_obj = course_cache.get(crs_key)
                if not course_obj:
                    course_obj = Course.objects.create(
                        name=course_val,
                        hospital=user_hospital,
                        is_active=True
                    )
                    course_cache[crs_key] = course_obj

            # Fast cached campaign matching
            campaign_obj = selected_campaign
            if not campaign_obj and campaign_val:
                camp_key = campaign_val.strip().lower()
                campaign_obj = campaign_cache.get(camp_key)
                if not campaign_obj:
                    if user_hospital:
                        campaign_obj, _ = HospitalCampaign.objects.get_or_create(
                            hospital=user_hospital,
                            name=campaign_val,
                            defaults={"platform": source_name or "Meta Ads", "is_active": True}
                        )
                    else:
                        campaign_obj, _ = HospitalCampaign.objects.get_or_create(
                            name=campaign_val,
                            defaults={"platform": source_name or "Meta Ads", "is_active": True}
                        )
                    campaign_cache[camp_key] = campaign_obj
                    
            # Populate Custom Data Payload with all Nelson master attributes
            custom_data_payload = {}
            if course_val:
                custom_data_payload["course"] = course_val
            if doctor_val:
                custom_data_payload["doctor"] = doctor_val
            if dept_val:
                custom_data_payload["department"] = dept_val
            if pd.notna(age_val):
                clean_age_str = str(age_val).strip()
                if clean_age_str.lower() not in ("nan", "none", "null", "-", "nat", ""):
                    if clean_age_str.endswith(".0"):
                        clean_age_str = clean_age_str[:-2]
                    custom_data_payload["age"] = clean_age_str
            if gender:
                custom_data_payload["gender"] = gender
            if appt_status_val:
                custom_data_payload["appointment_status"] = appt_status_val
            if branch_val:
                custom_data_payload["hospital_branch"] = branch_val
                custom_data_payload["nelson_dantoli"] = branch_val
            if due_date_val:
                custom_data_payload["due_date"] = due_date_val
            if recv_time_val:
                custom_data_payload["lead_received_time"] = recv_time_val
            if calling_time_val:
                custom_data_payload["lead_calling_time"] = calling_time_val
            if appt_date_val:
                custom_data_payload["appo_booked_date"] = str(appt_date_val)

            # Follow-ups (dates & remarks stored in custom_data for fast instant rendering)
            if fu1_date:
                custom_data_payload["calling_date_remark_1"] = str(fu1_date)
            if fu1_remark:
                custom_data_payload["remark_1"] = fu1_remark
            if fu1_time:
                custom_data_payload["calling_time_remark_1"] = fu1_time
                custom_data_payload["calling_time_remark_2"] = fu1_time

            if fu2_date:
                custom_data_payload["calling_date_remark_2"] = str(fu2_date)
            if fu2_remark:
                custom_data_payload["remark_2"] = fu2_remark

            if fu3_date:
                custom_data_payload["calling_date_remark_3"] = str(fu3_date)
            if fu3_remark:
                custom_data_payload["remark_3"] = fu3_remark

            # Financial & Visit details
            if final_status_val:
                custom_data_payload["deal_status"] = final_status_val
                custom_data_payload["done"] = final_status_val
            if visit_date_val:
                custom_data_payload["visit_date"] = str(visit_date_val)
            if uhid_val:
                custom_data_payload["uhid_id_no"] = uhid_val
            if pharmacy_bill_val:
                custom_data_payload["pharmacy_bill"] = pharmacy_bill_val
            if opd_bill_val:
                custom_data_payload["opd_bill"] = opd_bill_val
            if ipd_bill_val:
                custom_data_payload["ipd_bill"] = ipd_bill_val
            if investigation_bill_val:
                custom_data_payload["investigation"] = investigation_bill_val
            if total_bill_val:
                custom_data_payload["total"] = total_bill_val
                custom_data_payload["total_paid"] = total_bill_val
            if month_val:
                custom_data_payload["month"] = month_val
            if year_val:
                custom_data_payload["year"] = year_val
            if weekdays_val:
                custom_data_payload["weekdays"] = weekdays_val

            if not custom_data_payload.get("priority"):
                custom_data_payload["priority"] = "Hot"
                
            lead_obj = None
            if existing and on_duplicate == "update":
                existing.city = city or existing.city
                existing.email = email or existing.email
                if course_obj and not existing.course:
                    existing.course = course_obj
                if assigned_user:
                    existing.assigned_to = assigned_user
                if campaign_obj:
                    existing.campaign = campaign_obj
                if combined_notes:
                    existing.notes = (existing.notes + "\n" + combined_notes).strip()
                if custom_data_payload:
                    if not isinstance(existing.custom_data, dict):
                        existing.custom_data = {}
                    existing.custom_data.update(custom_data_payload)
                existing.import_job = job
                existing.import_source_file = job.original_filename
                existing.save()
                lead_obj = existing
                updated += 1
            else:
                current_seq += 1
                gen_code = f"{full_prefix}{current_seq:06d}"
                lead_obj = Lead.objects.create(
                    lead_code=gen_code,
                    name=name, mobile=mobile, alternate_mobile=alt_mobile,
                    email=email, city=city, location=city,
                    course=course_obj,
                    campaign=campaign_obj,
                    assigned_to=assigned_user,
                    temperature="HOT", stage=default_stage,
                    deal_status="OPEN", admission_status="NOT_APPLIED",
                    inquiry_date=inquiry_date, source_category=cat, lead_source=src,
                    notes=combined_notes, created_by=request.user, hospital=user_hospital,
                    custom_data=custom_data_payload,
                    import_source_file=job.original_filename, import_source_sheet="Sheet1",
                    import_source_row=row_num, import_job=job,
                )
                existing_mobile_map[cleaned_mob] = lead_obj.id
                imported += 1

            # Queue FollowUp entries to bulk_create in one single DB operation
            if lead_obj:
                fu_entries = [
                    (fu1_date, fu1_remark, fu1_time or None),
                    (fu2_date, fu2_remark, None),
                    (fu3_date, fu3_remark, None),
                ]
                for f_date, f_remark, f_time in fu_entries:
                    if f_date or f_remark:
                        actual_fu_date = f_date or inquiry_date
                        status_choice = FollowUpStatus.COMPLETED if f_date and f_date <= timezone.localdate() else FollowUpStatus.PENDING
                        parsed_time = None
                        if f_time:
                            for t_fmt in ["%H:%M:%S", "%H:%M", "%I:%M %p", "%I:%M%p"]:
                                try:
                                    parsed_time = datetime.strptime(f_time.strip(), t_fmt).time()
                                    break
                                except Exception:
                                    pass
                        followups_to_create.append(FollowUp(
                            lead=lead_obj,
                            followup_date=actual_fu_date,
                            followup_time=parsed_time,
                            followup_mode=FollowUpMode.CALL,
                            followup_status=status_choice,
                            comment=f_remark or "Follow-up logged via master file upload",
                            created_by=request.user,
                            imported_from_excel=True,
                        ))

        # Bulk insert all queued follow-ups in single batch
        if followups_to_create:
            FollowUp.objects.bulk_create(followups_to_create, batch_size=500)

        job.imported_count = imported
        job.updated_count = updated
        job.duplicate_count = duplicate
        job.invalid_count = invalid
        job.total_rows = len(df) - start_idx
        job.status = ImportJob.Status.DONE
        job.completed_at = timezone.now()
        job.save()
        
        messages.success(
            request, 
            f"Quick import complete: {imported} created, {updated} updated, {duplicate} duplicate skipped, {invalid} invalid."
        )
        return redirect("dashboard:telecaller_new_enquiries" if request.user.role == "LEAD_ATTENDENT" else "imports:job_detail", pk=job.pk) if request.user.role != "LEAD_ATTENDENT" else redirect("dashboard:telecaller_new_enquiries")
        
    return redirect("imports:upload")


@login_required
@user_passes_test(lambda u: u.can_import_export)
def delete_import(request, pk):
    if request.method == "POST":
        job = get_object_or_404(ImportJob, pk=pk)
        from leads.models import Lead
        leads = Lead.objects.filter(import_job=job)
        leads_count = leads.count()
        leads.delete()
        if job.file:
            try:
                job.file.delete(save=False)
            except Exception:
                pass
        job.delete()
        messages.success(request, f"Import history item and its {leads_count} associated leads have been deleted successfully.")
    return redirect("imports:history")


@login_required
def export_business_master_data(request):
    """
    Exports all active leads of a business into an Excel (.xlsx) file.
    Available to Admins and Super Admins.
    """
    user = request.user
    if not (user.is_superuser or user.role in (User.Role.SUPER_ADMIN, User.Role.ADMIN)):
        messages.error(request, "Permission denied. Only Admins and Super Admins can export master data.")
        return redirect("imports:upload")

    target_hospital_id = request.GET.get("target_hospital_id")
    target_hospital = None
    if user.role == User.Role.SUPER_ADMIN and target_hospital_id:
        target_hospital = Hospital.objects.filter(pk=target_hospital_id).first()
    elif user.hospital:
        target_hospital = user.hospital

    leads_qs = Lead.objects.select_related(
        "course", "stage", "lead_source", "campaign", "assigned_to", "hospital"
    ).filter(is_archived=False)

    if target_hospital:
        leads_qs = leads_qs.filter(hospital=target_hospital)
        biz_name = target_hospital.name
    else:
        biz_name = "All_Businesses"

    is_hospital = False
    if target_hospital:
        btype = (target_hospital.settings or {}).get("business_type")
        if btype:
            is_hospital = (str(btype).strip().lower() == "hospital")
        else:
            name_low = (target_hospital.name or "").lower()
            is_hospital = not ("academy" in name_low or "zappcode" in name_low)

    rows = []
    for l in leads_qs.order_by("-id"):
        cd = l.custom_data or {}
        if not isinstance(cd, dict):
            cd = {}

        if is_hospital:
            rows.append({
                "Lead Code": l.lead_code,
                "Inquiry Date": str(l.inquiry_date or ""),
                "Patient Name": l.name,
                "Contact / Mobile": l.mobile,
                "Alternate Contact": l.alternate_mobile or "",
                "Location / City": l.city or l.location or cd.get("location", ""),
                "Department": cd.get("department", ""),
                "Doctor / Consultant": cd.get("doctor", ""),
                "Assigned To": l.assigned_to.get_full_name() if l.assigned_to else (cd.get("assigned_to", "") or ""),
                "Campaign Name": l.campaign.name if l.campaign else (cd.get("campaign", "") or ""),
                "Lead Source": l.lead_source.name if l.lead_source else (cd.get("lead_source", "") or ""),
                "Appointment Status": cd.get("appointment_status", "") or l.display_status,
                "Hospital Branch": cd.get("hospital_branch", "") or cd.get("nelson_dantoli", ""),
                "Gender": cd.get("gender", ""),
                "Age": cd.get("age", ""),
                "Appointment Date": cd.get("appo_booked_date", ""),
                "1st Follow up Date": cd.get("calling_date_remark_1", ""),
                "1st Follow up Remark": cd.get("remark_1", ""),
                "2nd Follow up Date": cd.get("calling_date_remark_2", ""),
                "2nd Follow up Remark": cd.get("remark_2", ""),
                "3rd Follow up Date": cd.get("calling_date_remark_3", ""),
                "3rd Follow up Remark": cd.get("remark_3", ""),
                "Final Status": cd.get("deal_status", "") or l.get_deal_status_display(),
                "Visit Date": cd.get("visit_date", ""),
                "UHID ID NO": cd.get("uhid_id_no", ""),
                "Pharmacy Bill": cd.get("pharmacy_bill", 0),
                "OPD Bill": cd.get("opd_bill", 0),
                "IPD Bill": cd.get("ipd_bill", 0),
                "Investigation Bill": cd.get("investigation", ""),
                "Total Bill": cd.get("total", 0),
                "Month": cd.get("month", ""),
                "Year": cd.get("year", ""),
                "Weekdays": cd.get("weekdays", ""),
                "Remarks": l.notes or "",
                "Created At": l.effective_created_formatted or (l.created_at.strftime("%Y-%m-%d %H:%M") if l.created_at else ""),
            })
        else:
            rows.append({
                "Lead Code": l.lead_code,
                "Inquiry Date": str(l.inquiry_date or ""),
                "Student Name": l.name,
                "Mobile / Phone": l.mobile,
                "Email": l.email,
                "City": l.city,
                "Course / Program": l.course.name if l.course else (cd.get("course", "") or ""),
                "Stage": str(l.stage),
                "Campaign Name": l.campaign.name if l.campaign else "",
                "Lead Source": l.lead_source.name if l.lead_source else "",
                "Assigned Counsellor": l.assigned_to.get_full_name() if l.assigned_to else "",
                "Deal Status": l.get_deal_status_display(),
                "Admission Status": l.get_admission_status_display(),
                "Notes / Query": l.notes or "",
                "Created At": l.effective_created_formatted or (l.created_at.strftime("%Y-%m-%d %H:%M") if l.created_at else ""),
            })

    df = pd.DataFrame(rows)
    clean_biz_str = re.sub(r"[^\w\-]+", "_", biz_name).strip("_")
    filename = f"{clean_biz_str}_master_leads_{timezone.now().strftime('%Y%m%d_%H%M')}.xlsx"
    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    df.to_excel(response, index=False, sheet_name="Master Leads")
    return response


@login_required
def delete_business_master_data(request):
    """
    Deletes all leads of a business.
    Allowed for:
    - Super Admin: always.
    - Business Admin: only if granted `can_delete_master_data` permission by Super Admin.
    Requires POST request.
    """
    if request.method != "POST":
        return redirect("imports:upload")

    user = request.user
    if not (user.is_superuser or user.role == User.Role.SUPER_ADMIN or (user.role == User.Role.ADMIN and user.can_delete_master_data)):
        messages.error(request, "Permission denied. You do not have authorization to delete master lead data.")
        return redirect("imports:upload")

    target_hospital_id = request.POST.get("target_hospital_id")
    target_hospital = None
    if user.role == User.Role.SUPER_ADMIN and target_hospital_id:
        target_hospital = Hospital.objects.filter(pk=target_hospital_id).first()
    elif user.hospital:
        target_hospital = user.hospital

    leads_qs = Lead.objects.filter(is_archived=False)
    if target_hospital:
        leads_qs = leads_qs.filter(hospital=target_hospital)
        biz_name = target_hospital.name
    else:
        biz_name = "All Businesses"

    total_count = leads_qs.count()
    if total_count == 0:
        messages.info(request, f"No active leads found for {biz_name} to delete.")
        return redirect("imports:upload")

    # Fast bulk delete with cascade cleanup
    leads_qs.delete()

    messages.success(
        request, 
        f"Master data purge complete! Successfully deleted {total_count} leads for '{biz_name}'."
    )
    return redirect("imports:upload")
