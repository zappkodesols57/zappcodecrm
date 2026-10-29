import json
# CRM Views - Auto-reloaded for schema sync
from collections import defaultdict
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import models
from django.db.models import Q, F, Max
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone

from followups.models import FollowUp, Note, Activity, ActivityType, FollowUpMode, FollowUpStatus
from admissions.models import Admission
from accounts.models import User, Hospital
from leads.models import (
    Lead, SourceCategory, LeadSource, Campaign, Course, Trainer, Batch, LeadStage, Tag, 
    MasterGroup, MasterItem, HospitalBranch, HospitalDepartment, HospitalDoctor, 
    HospitalDisease, DoctorBranchAvailability, DealStatus, LeadTemperature,
    AdmissionStatus, Appointment, AppointmentStatus,
)
from leads.forms import (
    LeadForm, HospitalLeadForm, SourceCategoryForm, LeadSourceForm, CampaignForm, CourseForm, TrainerForm, BatchForm, LeadStageForm,
)




# ===========================================================================
# ⚙️ UNIVERSAL MASTERS & DYNAMIC CUSTOM FIELDS
# ===========================================================================

def _get_business_for_custom_fields(request):
    """Resolve target business for Custom Lead Form view & operations."""
    user = request.user
    user_biz = getattr(user, 'business', None)
    if user_biz:
        return user_biz
    # For Super Admin: check GET parameter, session, or default
    biz_val = (
        request.GET.get("business", "").strip()
        or request.POST.get("business", "").strip()
        or str(request.session.get("active_business_id", "")).strip()
    )
    if biz_val.lower() == "default":
        return "default"
    if biz_val and biz_val.isdigit():
        target = Hospital.objects.filter(id=int(biz_val), is_active=True).first()
        if target:
            return target
    # Default to first active business
    return Hospital.objects.filter(is_active=True).order_by("id").first()


def _ensure_business_core_fields(h):
    """Initializes core lead form fields for a business if not yet initialized."""
    from leads.models import LeadCustomField, HospitalDisease, HospitalBranch, Course, LeadStage
    
    # ── Universal Default Lead Form Fields (for new businesses / system template) ──
    default_lead_form_fields = [
        ('lead_id', 'Lead ID', 'TEXT', 1, False, True, 'Auto-generated ID (System)', '', True),
        ('name', 'Full Name', 'TEXT', 2, True, True, 'Enter full name', '', True),
        ('mobile', 'Phone Number', 'TEXT', 3, True, True, 'Phone with country code (e.g. +91 9876543210)', '', True),
        ('email', 'Email Address', 'TEXT', 4, False, True, 'e.g. client@example.com', '', True),
        ('industry_category', 'Industry / Category', 'DROPDOWN', 5, False, True, 'Select Industry', 'Healthcare, Academy, Travel, E-commerce, Other', True),
        ('lead_source', 'Lead Source', 'DROPDOWN', 6, False, True, 'Select Lead Source', 'Website, Social Media, Referral, Ad Campaign, Walk-in, Call, Event, Partner', True),
        ('stage', 'Lead Status / Pipeline Stage', 'DROPDOWN', 7, True, True, 'Select Status', 'New, Contacted, Qualified, Proposal Sent, Negotiation, Converted, Lost', True),
        ('assigned_to', 'Lead Owner / Assigned To', 'DROPDOWN', 8, False, True, 'Select Owner / Counselor', 'Dynamic user list', True),
        ('lead_score', 'Lead Score', 'NUMBER', 9, False, True, 'e.g. 85', '', True),
        ('hospital_branch', 'Hospital Branch / Location', 'DROPDOWN', 10, False, True, 'Select Branch / Location', 'Main Branch, City Center, North Branch', True),
        ('city', 'City', 'TEXT', 11, False, True, 'Enter city name', '', True),
        ('country', 'Country', 'DROPDOWN', 12, False, True, 'Select Country', 'India, United States, United Kingdom, Canada, Australia, UAE, Other', True),
        ('preferred_contact_method', 'Preferred Contact Method', 'DROPDOWN', 13, False, True, 'Select Contact Method', 'Call, Email, WhatsApp, SMS', True),
        ('preferred_language', 'Preferred Language', 'DROPDOWN', 14, False, True, 'Select Language', 'English, Hindi, Marathi, Gujarati, Spanish, French, Other', True),
        ('budget_range', 'Budget Range', 'DROPDOWN', 15, False, True, 'Select Budget', 'Under ₹25,000, ₹25,000 - ₹50,000, ₹50,000 - ₹1,00,000, ₹1,00,000 - ₹2,50,000, Above ₹2,50,000', True),
        ('notes', 'Notes / Remarks', 'TEXTAREA', 16, False, True, 'Free text notes, conversation summary, or remarks...', '', True),
        ('tags', 'Tags', 'TEXT', 17, False, True, 'Tags for segmentation (comma-separated)', '', True),
        ('created_at_date', 'Created Date', 'DATE', 18, False, True, 'System date (auto)', '', True),
        ('last_contacted_date', 'Last Contacted Date', 'DATE', 19, False, True, 'Date of last interaction', '', True),
        ('next_followup_date', 'Next Follow-up Date', 'DATE', 20, False, True, 'Next scheduled follow-up date', '', True),
        ('utm_source_campaign', 'UTM Source / Campaign', 'TEXT', 21, False, True, 'Marketing attribution tracking', '', True),
        ('marketing_consent', 'Marketing Consent (Opt-in)', 'CHECKBOX', 22, False, True, 'Compliance opt-in confirmed', '', True),
    ]

    # If initializing the default template (hospital is None / "default")
    if h is None or h == "default":
        for f_name, f_lbl, f_type, f_ord, f_req, f_act, f_ph, f_opt, f_sys in default_lead_form_fields:
            if not LeadCustomField.objects.filter(hospital__isnull=True, name=f_name).exists():
                LeadCustomField.objects.create(
                    hospital=None, name=f_name, label=f_lbl, field_type=f_type,
                    order=f_ord, is_required=f_req, is_active=f_act,
                    placeholder=f_ph, options=f_opt, is_system=f_sys
                )
        return

    is_academy = getattr(h, "industry", None) == Hospital.Industry.ACADEMY
    is_healthcare = getattr(h, "industry", None) == Hospital.Industry.HOSPITAL

    if is_academy:
        core_field_defs = [
            ('name', 'Student Name', 'TEXT', 1, True, True, 'Enter full student name', ''),
            ('mobile', 'Mobile Number', 'TEXT', 2, True, True, '10-digit mobile number', ''),
            ('alternate_mobile', 'Alternate Mobile', 'TEXT', 3, False, True, '10-digit alternate mobile', ''),
            ('email', 'Email Address', 'TEXT', 4, False, True, 'e.g. student@gmail.com', ''),
            ('state', 'State', 'DROPDOWN', 5, True, True, 'Select State', 'Maharashtra, Madhya Pradesh, Gujarat, Karnataka, Delhi, Other'),
            ('city', 'City', 'DROPDOWN', 6, True, True, 'Select City', 'Nagpur, Pune, Mumbai, Nashik, Aurangabad, Wardha, Amravati, Chandrapur, Bhandara, Gondia, Other'),
            ('location', 'Area / Location', 'TEXT', 7, False, True, 'Area or locality in city', ''),
            ('education', 'Education Category', 'DROPDOWN', 8, True, True, 'Select Education Category', 'Engineering, Medical, Management, Arts & Commerce, Polytechnic / Diploma, School Student, Other'),
            ('qualification', 'Qualification / Degree', 'DROPDOWN', 9, True, True, 'Select Qualification', 'B.Tech / B.E, BCA, MCA, B.Sc, M.Sc, Diploma, 12th Standard, Other'),
            ('graduation_year', 'Graduation / Passing Year', 'TEXT', 10, False, True, 'e.g. 2025, 2026', ''),
            ('course', 'Course Interested', 'DROPDOWN', 11, True, True, 'Select Course', 'Data Analytics, Data Science, Full Stack Python, Full Stack Java, Digital Marketing, AI & Machine Learning, Software Testing'),
            ('temperature', 'Lead Temperature', 'DROPDOWN', 12, False, True, 'Select Temperature', 'HOT, WARM, COLD'),
            ('stage', 'Lead Stage', 'DROPDOWN', 13, True, True, 'Select Stage', 'New, Contacted, Follow Up, Demo Attended, Interested, Admission Confirmed, Lost / Dropped'),
            ('deal_status', 'Deal Status', 'DROPDOWN', 14, False, True, 'Select Deal Status', 'OPEN, WON, LOST, ON_HOLD'),
            ('admission_status', 'Admission Status', 'DROPDOWN', 15, True, True, 'Select Admission Status', 'OPEN, HOLD, WON, LOST'),
            ('inquiry_date', 'Inquiry Date', 'DATE', 16, True, True, 'Select inquiry date', ''),
            ('lead_source', 'Lead Source', 'DROPDOWN', 17, False, True, 'Select Source', 'Meta Ads, Google Ads, Direct Walk-in, College Visit, JustDial, Website Form, Referral, Student Referral'),
            ('campaign', 'Campaign', 'DROPDOWN', 18, False, True, 'Select Campaign', 'ZA Meta Campaign 2026, Summer Batch Campaign, Python Masters, B2B Zappkode'),
        ]
    elif is_healthcare:
        core_field_defs = [
            ('name', 'Patient Name', 'TEXT', 1, True, True, 'Enter full patient name', ''),
            ('mobile', 'Mobile Number', 'TEXT', 2, True, True, '10-digit mobile number', ''),
            ('age', 'Age', 'NUMBER', 3, False, True, 'e.g. 35', ''),
            ('gender', 'Gender', 'DROPDOWN', 4, False, True, 'Select Gender', 'Male, Female, Other'),
            ('comments', 'Comments / Notes', 'TEXTAREA', 5, False, True, 'Enter patient notes...', ''),
            ('location', 'Location', 'DROPDOWN', 6, False, True, 'Select Location', 'Nagpur, Wardha, Hinganghat, Chandrapur, Amravati, Bhandara, Yavatmal, Gondia'),
            ('lead_source', 'Lead Source', 'DROPDOWN', 7, False, True, 'Select Lead Source', 'Google Ads, Facebook / Instagram, Walk-in, Doctor Referral, Website, Newspaper, Camp / Event'),
            ('campaign', 'Campaign', 'DROPDOWN', 8, False, True, 'Select Campaign', 'Summer Health Checkup, Cardiology Camp, Free OPD Camp, Digital Awareness 2026'),
            ('appointment_status', 'Appointment Status', 'DROPDOWN', 9, False, True, 'Select Status', 'Interested, Booked, Visited, Follow-up Needed, Cancelled / Rescheduled, Not Interested'),
            ('hospital_branch', 'Hospital Branch', 'DROPDOWN', 10, True, True, 'Select Branch', 'Dhantoli, Main Branch'),
            ('department', 'Department', 'DROPDOWN', 11, False, True, 'Select Department', 'Cardiology, Neurology, Orthopedics, Pediatrics, Oncology, Gynecology, General Medicine'),
            ('disease', 'Disease', 'DROPDOWN', 12, False, True, 'Select Disease', ''),
            ('doctor', 'Doctor', 'DROPDOWN', 13, False, True, 'Select Doctor', 'Dr. Pradeep Patil, Dr. Rahul Sharma, Dr. Priya Deshmukh, Dr. Amit Verma'),
        ]
    else:
        # Any other / new business automatically inherits the Default Custom Lead Form!
        core_field_defs = [
            (f_name, f_lbl, f_type, f_ord, f_req, f_act, f_ph, f_opt)
            for f_name, f_lbl, f_type, f_ord, f_req, f_act, f_ph, f_opt, _ in default_lead_form_fields
        ]

    for f_name, f_lbl, f_type, f_ord, f_req, f_act, f_ph, f_opt in core_field_defs:
        if not LeadCustomField.objects.filter(hospital=h, name=f_name).exists():
            LeadCustomField.objects.create(
                hospital=h, name=f_name, label=f_lbl, field_type=f_type,
                order=f_ord, is_required=f_req, is_active=f_act,
                placeholder=f_ph, options=f_opt, is_system=True
            )

    # Sync dynamic choices from other tables if available
    if is_academy:
        cf_course = LeadCustomField.objects.filter(hospital=h, name="course").first()
        if cf_course:
            course_names = list(Course.objects.filter(is_active=True, hospital=h).values_list("name", flat=True)[:15]) or list(Course.objects.filter(is_active=True).values_list("name", flat=True)[:15])
            if course_names:
                cf_course.options = ", ".join(course_names)
                cf_course.save(update_fields=["options"])

        cf_camp = LeadCustomField.objects.filter(hospital=h, name="campaign").first()
        if cf_camp:
            academy_camps = list(Campaign.objects.filter(hospital=h, is_active=True).values_list("name", flat=True))
            if academy_camps:
                cf_camp.options = ", ".join(academy_camps)
                cf_camp.save(update_fields=["options"])

    elif is_healthcare:
        cf_disease = LeadCustomField.objects.filter(hospital=h, name="disease").first()
        if cf_disease:
            dis_names = list(HospitalDisease.objects.filter(hospital=h, is_active=True).values_list("name", flat=True))
            if dis_names:
                cf_disease.options = ", ".join(dis_names)
                cf_disease.save(update_fields=["options"])

        cf_branch = LeadCustomField.objects.filter(hospital=h, name="hospital_branch").first()
        if cf_branch:
            branch_names = list(HospitalBranch.objects.filter(hospital=h, is_active=True).values_list("name", flat=True))
            if branch_names:
                cf_branch.options = ", ".join(branch_names)
                cf_branch.save(update_fields=["options"])

        cf_camp = LeadCustomField.objects.filter(hospital=h, name="campaign").first()
        if cf_camp:
            hosp_camps = list(Campaign.objects.filter(hospital=h, is_active=True).values_list("name", flat=True))
            if hosp_camps:
                cf_camp.options = ", ".join(hosp_camps)
                cf_camp.save(update_fields=["options"])


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def universal_master_list(request):
    from leads.models import LeadCustomField
    from accounts.models import Hospital

    user_biz = getattr(request.user, "business", None)
    is_global_admin = request.user.is_superuser or (
        request.user.role == User.Role.SUPER_ADMIN and not user_biz
    )

    # Ensure default universal fields are seeded
    _ensure_business_core_fields("default")

    h = _get_business_for_custom_fields(request)
    if h != "default":
        _ensure_business_core_fields(h)

    available_businesses = list(Hospital.objects.filter(is_active=True).order_by("id")) if is_global_admin else []
    if user_biz and not available_businesses:
        available_businesses = [user_biz]

    if h == "default":
        all_fields_qs = LeadCustomField.objects.filter(hospital__isnull=True).order_by('order', 'id')
        current_hospital = None
        is_default_tab = True
    else:
        all_fields_qs = LeadCustomField.objects.filter(hospital=h).order_by('order', 'id') if h else LeadCustomField.objects.all().order_by('order', 'id')
        current_hospital = h
        is_default_tab = False

    # Fetch Positive & Negative remark Master Groups and Items
    from leads.models import MasterGroup, MasterItem
    pos_group, _ = MasterGroup.objects.get_or_create(
        name="Positive Remarks", 
        defaults={"description": "Positive call remarks & notes that shift lead temperature UP to Hot / Warm"}
    )
    neg_group, _ = MasterGroup.objects.get_or_create(
        name="Negative Remarks", 
        defaults={"description": "Negative call remarks & notes that shift lead temperature DOWN to Warm / Cold / Freeze"}
    )

    pos_items = MasterItem.objects.filter(group=pos_group, hospital=current_hospital).order_by("order", "name")
    neg_items = MasterItem.objects.filter(group=neg_group, hospital=current_hospital).order_by("order", "name")

    # Fetch Archived Leads based on selected business scope
    from leads.models import Lead
    from django.core.paginator import Paginator

    if is_default_tab:
        archived_leads_qs = Lead.objects.filter(hospital__isnull=True, is_archived=True)
        total_archived = Lead.objects.filter(is_archived=True).count()
    elif current_hospital:
        archived_leads_qs = Lead.objects.filter(hospital=current_hospital, is_archived=True)
        total_archived = Lead.objects.filter(hospital=current_hospital, is_archived=True).count()
    else:
        archived_leads_qs = Lead.objects.filter(is_archived=True)
        total_archived = Lead.objects.filter(is_archived=True).count()

    archived_leads_qs = archived_leads_qs.select_related('hospital', 'assigned_to', 'stage', 'campaign').order_by('-updated_at')

    q_archived = request.GET.get('q_archived', '').strip()
    if q_archived:
        archived_leads_qs = archived_leads_qs.filter(
            Q(name__icontains=q_archived) |
            Q(mobile__icontains=q_archived) |
            Q(email__icontains=q_archived) |
            Q(lead_code__icontains=q_archived)
        )

    archived_paginator = Paginator(archived_leads_qs, 25)
    archived_page_num = request.GET.get('archived_page', 1)
    archived_page_obj = archived_paginator.get_page(archived_page_num)

    # If business-specific has no items yet, fallback/copy defaults or show list
    active_main_tab = request.GET.get("tab", "fields")

    return render(request, "leads/universal_masters.html", {
        "active": "universal_masters",
        "active_main_tab": active_main_tab,
        "all_fields": all_fields_qs,
        "current_business": current_hospital,
        "current_hospital": current_hospital,
        "is_default_tab": is_default_tab,
        "available_businesses": available_businesses,
        "is_global_admin": is_global_admin,
        "pos_group": pos_group,
        "neg_group": neg_group,
        "pos_items": pos_items,
        "neg_items": neg_items,
        "archived_leads": archived_page_obj,
        "archived_count": total_archived,
        "q_archived": q_archived,
    })


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def custom_field_add(request):
    from leads.models import LeadCustomField
    from django.utils.text import slugify
    from django.db.models import Max, F
    
    target_hospital = _get_business_for_custom_fields(request)
    target_hospital_obj = None if target_hospital == "default" else target_hospital

    if request.method == "POST":
        label = request.POST.get("label", "").strip()
        field_type = request.POST.get("field_type", "TEXT")
        options = request.POST.get("options", "").strip()
        placeholder = request.POST.get("placeholder", "").strip()
        help_text = request.POST.get("help_text", "").strip()
        is_required = request.POST.get("is_required") == "on"
        order_raw = request.POST.get("order", "").strip()
        biz_param = request.POST.get("business", "").strip() or ("default" if target_hospital == "default" else (str(target_hospital.id) if target_hospital else ""))

        qs = LeadCustomField.objects.filter(hospital=target_hospital_obj)

        try:
            order_val = int(order_raw) if order_raw else None
        except ValueError:
            order_val = None

        if order_val is None or order_val <= 0:
            max_order = qs.aggregate(m=Max('order'))['m'] or 0
            order = max_order + 1
        else:
            order = order_val
            qs.filter(order__gte=order).update(order=F('order') + 1)

        if label:
            name = slugify(label).replace("-", "_")
            base_name = name
            count = 1
            while LeadCustomField.objects.filter(hospital=target_hospital_obj, name=name).exists():
                name = f"{base_name}_{count}"
                count += 1

            LeadCustomField.objects.create(
                hospital=target_hospital_obj,
                name=name,
                label=label,
                field_type=field_type,
                options=options,
                placeholder=placeholder,
                help_text=help_text,
                is_required=is_required,
                order=order,
                is_active=True,
            )
            messages.success(request, f"New custom form field '{label}' added at position #{order} successfully.")
            redirect_url = f"/leads/universal-masters/?tab=custom_fields&business={biz_param}" if biz_param else "/leads/universal-masters/?tab=custom_fields"
            return redirect(redirect_url)
        messages.error(request, "Field label is required.")
    
    biz_param = request.GET.get("business", "").strip() or ("default" if target_hospital == "default" else (str(target_hospital.id) if target_hospital else ""))
    redirect_url = f"/leads/universal-masters/?tab=custom_fields&business={biz_param}" if biz_param else "/leads/universal-masters/?tab=custom_fields"
    return redirect(redirect_url)


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def custom_field_edit(request, pk):
    from leads.models import LeadCustomField
    field = get_object_or_404(LeadCustomField, pk=pk)
    
    is_superadmin = request.user.is_superuser or request.user.role == User.Role.SUPER_ADMIN
    user_biz = getattr(request.user, "business", None)
    if not is_superadmin and field.business != user_biz:
        messages.error(request, "Permission denied.")
        return redirect("/leads/universal-masters/?tab=custom_fields")
        
    biz_param = request.POST.get("business", "").strip() or (str(field.hospital.id) if field.hospital else "default")

    if request.method == "POST":
        label = request.POST.get("label", "").strip()
        field_type = request.POST.get("field_type", "TEXT")
        options = request.POST.get("options", "").strip()
        placeholder = request.POST.get("placeholder", "").strip()
        help_text = request.POST.get("help_text", "").strip()
        is_required = request.POST.get("is_required") == "on"
        is_active = request.POST.get("is_active") == "on"
        order = request.POST.get("order", 0)
        try:
            order = int(order)
        except ValueError:
            order = 0

        if label:
            old_order = field.order
            field.label = label
            field.field_type = field_type
            field.options = options
            field.placeholder = placeholder
            field.help_text = help_text
            field.is_required = is_required
            field.is_active = is_active
            
            all_fields = list(LeadCustomField.objects.filter(hospital=field.hospital).order_by('order', 'id'))
            
            if order > 0 and order != old_order:
                all_fields = [f for f in all_fields if f.pk != field.pk]
                insert_idx = max(0, min(order - 1, len(all_fields)))
                all_fields.insert(insert_idx, field)
                
                for idx, f in enumerate(all_fields):
                    f.order = idx + 1
                    if f.pk == field.pk:
                        field.order = idx + 1
                        field.save()
                    else:
                        f.save(update_fields=['order'])
            else:
                field.save()

            FIELD_TO_GROUP_MAP = {
                "appointment_status": "Appointment Statuses",
                "lead_source": "Lead Sources",
                "campaign": "Campaigns",
                "location": "Locations",
                "gender": "Genders",
                "priority": "Priorities",
                "deal_status": "Deal Statuses",
            }
            if field.name in FIELD_TO_GROUP_MAP and field.field_type == LeadCustomField.FieldType.DROPDOWN:
                from leads.models import MasterGroup, MasterItem
                group_name = FIELD_TO_GROUP_MAP[field.name]
                mg, _ = MasterGroup.objects.get_or_create(name=group_name)
                new_opts = [o.strip() for o in options.split(",") if o.strip()]
                if new_opts:
                    mg.items.filter(hospital=field.hospital).exclude(name__in=new_opts).delete()
                    for idx, opt_name in enumerate(new_opts):
                        MasterItem.objects.update_or_create(
                            group=mg,
                            hospital=field.hospital,
                            name=opt_name,
                            defaults={"order": idx + 1, "is_active": True}
                        )
                
            messages.success(request, f"Form field '{label}' updated successfully.")
        redirect_url = f"/leads/universal-masters/?business={biz_param}" if biz_param else "/leads/universal-masters/"
        return redirect(redirect_url)
    redirect_url = f"/leads/universal-masters/?business={biz_param}" if biz_param else "/leads/universal-masters/"
    return redirect(redirect_url)


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def custom_field_toggle(request, pk):
    from leads.models import LeadCustomField
    field = get_object_or_404(LeadCustomField, pk=pk)
    
    is_superadmin = request.user.is_superuser or request.user.role == User.Role.SUPER_ADMIN
    user_biz = getattr(request.user, "business", None)
    if not is_superadmin and field.business != user_biz:
        messages.error(request, "Permission denied.")
        return redirect("/leads/universal-masters/?tab=custom_fields")
        
    field.is_active = not field.is_active
    field.save(update_fields=["is_active"])
    messages.success(request, f"Field '{field.label}' is now {'Active' if field.is_active else 'Hidden'}.")
    biz_param = request.GET.get("business", "").strip() or (str(field.hospital.id) if field.hospital else "default")
    redirect_url = f"/leads/universal-masters/?tab=custom_fields&business={biz_param}" if biz_param else "/leads/universal-masters/?tab=custom_fields"
    return redirect(redirect_url)


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def custom_field_delete(request, pk):
    from leads.models import LeadCustomField
    field = get_object_or_404(LeadCustomField, pk=pk)
    
    is_superadmin = request.user.is_superuser or request.user.role == User.Role.SUPER_ADMIN
    user_biz = getattr(request.user, "business", None)
    if not is_superadmin and field.business != user_biz:
        messages.error(request, "Permission denied.")
        return redirect("/leads/universal-masters/?tab=custom_fields")
        
    label = field.label
    biz_param = request.POST.get("business", "").strip() or (str(field.hospital.id) if field.hospital else "default")
    field.delete()
    messages.success(request, f"Custom form field '{label}' removed from lead form.")
    redirect_url = f"/leads/universal-masters/?tab=custom_fields&business={biz_param}" if biz_param else "/leads/universal-masters/?tab=custom_fields"
    return redirect(redirect_url)


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def master_group_add(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        if name:
            group, created = MasterGroup.objects.get_or_create(name=name, defaults={"description": description})
            if created:
                messages.success(request, f"Master Category '{name}' created successfully.")
            else:
                messages.warning(request, f"Master Category '{name}' already exists.")
            return redirect(f"/leads/universal-masters/?group_id={group.pk}")
        messages.error(request, "Category name is required.")
    return redirect("leads:universal_masters")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def master_group_edit(request, pk):
    group = get_object_or_404(MasterGroup, pk=pk)
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        is_active = request.POST.get("is_active") == "on"
        if name:
            group.name = name
            group.description = description
            group.is_active = is_active
            group.save()
            messages.success(request, f"Master Category '{group.name}' updated.")
        return redirect(f"/leads/universal-masters/?group_id={group.pk}")
    return redirect("leads:universal_masters")


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def master_group_delete(request, pk):
    group = get_object_or_404(MasterGroup, pk=pk)
    if request.method == "POST":
        name = group.name
        group.delete()
        messages.success(request, f"Master Category '{name}' and its sub-items deleted.")
    return redirect("leads:universal_masters")


@login_required
def master_item_add(request):
    if request.method == "POST":
        group_id = request.POST.get("group_id")
        group = get_object_or_404(MasterGroup, pk=group_id)
        name = request.POST.get("name", "").strip()
        code = request.POST.get("code", "").strip()
        order = request.POST.get("order", 0)
        biz_param = request.POST.get("business", "").strip()
        from_view = request.POST.get("from_view", "").strip()
        
        target_hospital = None
        is_superadmin = request.user.is_superuser or request.user.role == User.Role.SUPER_ADMIN
        user_biz = getattr(request.user, "business", None)
        if is_superadmin:
            if biz_param and biz_param != "default":
                from accounts.models import Hospital
                target_hospital = Hospital.objects.filter(id=biz_param).first()
        else:
            target_hospital = user_biz

        try:
            order = int(order)
        except ValueError:
            order = 0

        if name:
            item, created = MasterItem.objects.get_or_create(
                group=group, name=name, hospital=target_hospital, 
                defaults={"code": code, "order": order, "is_active": True}
            )
            if created:
                # Log audit trail for keyword addition
                from audit.utils import log_action
                biz_label = target_hospital.name if target_hospital else "Global"
                log_action(
                    action="ADD_TEMPERATURE_KEYWORD",
                    obj=item,
                    new_value=f"Added '{name}' keyword to {group.name} for {biz_label}",
                    user=request.user
                )
                messages.success(request, f"Remark keyword '{name}' added successfully to {group.name}.")
            else:
                messages.warning(request, f"Keyword '{name}' already exists in {group.name}.")
        else:
            messages.error(request, "Keyword / Remark text is required.")

        if from_view == "temperature_manager":
            redirect_url = f"/leads/temperature-manager/?business={biz_param}" if biz_param else "/leads/temperature-manager/"
            return redirect(redirect_url)
        elif from_view == "hospital_config":
            return redirect("/leads/hospital-configuration/?tab=temperature")
        
        redirect_url = f"/leads/universal-masters/?tab=temperature&business={biz_param}" if biz_param else "/leads/universal-masters/?tab=temperature"
        return redirect(redirect_url)
    return redirect("leads:universal_masters")


@login_required
def master_item_edit(request, pk):
    item = get_object_or_404(MasterItem, pk=pk)
    is_superadmin = request.user.is_superuser or request.user.role == User.Role.SUPER_ADMIN
    user_biz = getattr(request.user, "business", None)
    if not is_superadmin and item.hospital and item.hospital != user_biz:
        messages.error(request, "You do not have permission to edit this item.")
        return redirect("leads:temperature_manager")
        
    biz_param = request.POST.get("business", "").strip()
    from_view = request.POST.get("from_view", "").strip()

    if request.method == "POST":
        old_name = item.name
        old_active = item.is_active
        name = request.POST.get("name", "").strip()
        code = request.POST.get("code", "").strip()
        order = request.POST.get("order", 0)
        is_active = request.POST.get("is_active") == "on"
        try:
            order = int(order)
        except ValueError:
            order = 0

        if name:
            item.name = name
            item.code = code
            item.order = order
            item.is_active = is_active
            item.save()

            from audit.utils import log_action
            log_action(
                action="UPDATE_TEMPERATURE_KEYWORD",
                obj=item,
                old_value=f"{old_name} (Active: {old_active})",
                new_value=f"{item.name} (Active: {item.is_active})",
                user=request.user
            )
            messages.success(request, f"Remark keyword '{item.name}' updated.")

        if from_view == "temperature_manager":
            redirect_url = f"/leads/temperature-manager/?business={biz_param}" if biz_param else "/leads/temperature-manager/"
            return redirect(redirect_url)
        elif from_view == "hospital_config":
            return redirect("/leads/hospital-configuration/?tab=temperature")
        
        redirect_url = f"/leads/universal-masters/?tab=temperature&business={biz_param}" if biz_param else "/leads/universal-masters/?tab=temperature"
        return redirect(redirect_url)
    return redirect("leads:temperature_manager")


@login_required
def master_item_toggle(request, pk):
    item = get_object_or_404(MasterItem, pk=pk)
    is_superadmin = request.user.is_superuser or request.user.role == User.Role.SUPER_ADMIN
    user_biz = getattr(request.user, "business", None)
    if not is_superadmin and item.hospital and item.hospital != user_biz:
        messages.error(request, "You do not have permission to modify this item.")
        return redirect("leads:temperature_manager")
        
    biz_param = request.GET.get("business", "").strip()
    from_view = request.GET.get("from_view", "").strip()

    old_state = item.is_active
    item.is_active = not item.is_active
    item.save(update_fields=["is_active"])

    from audit.utils import log_action
    log_action(
        action="TOGGLE_TEMPERATURE_KEYWORD",
        obj=item,
        old_value=f"Active: {old_state}",
        new_value=f"Active: {item.is_active}",
        user=request.user
    )
    messages.success(request, f"Status for '{item.name}' changed to {'Active' if item.is_active else 'Inactive'}.")
    
    if from_view == "temperature_manager":
        redirect_url = f"/leads/temperature-manager/?business={biz_param}" if biz_param else "/leads/temperature-manager/"
        return redirect(redirect_url)
    elif from_view == "hospital_config":
        return redirect("/leads/hospital-configuration/?tab=temperature")
    redirect_url = f"/leads/universal-masters/?tab=temperature&business={biz_param}" if biz_param else "/leads/universal-masters/?tab=temperature"
    return redirect(redirect_url)


@login_required
def master_item_delete(request, pk):
    item = get_object_or_404(MasterItem, pk=pk)
    is_superadmin = request.user.is_superuser or request.user.role == User.Role.SUPER_ADMIN
    user_biz = getattr(request.user, "business", None)
    if not is_superadmin and item.hospital and item.hospital != user_biz:
        messages.error(request, "You do not have permission to delete this item.")
        return redirect("leads:temperature_manager")
        
    biz_param = request.POST.get("business", "").strip() or request.GET.get("business", "").strip()
    from_view = request.POST.get("from_view", "").strip() or request.GET.get("from_view", "").strip()

    name = item.name
    group_name = item.group.name if item.group else "Group"
    biz_label = item.hospital.name if item.hospital else "Global"

    from audit.utils import log_action
    log_action(
        action="DELETE_TEMPERATURE_KEYWORD",
        obj=item,
        old_value=f"Deleted '{name}' from {group_name} ({biz_label})",
        user=request.user
    )

    item.delete()
    messages.success(request, f"Remark keyword '{name}' deleted.")
    
    if from_view == "temperature_manager":
        redirect_url = f"/leads/temperature-manager/?business={biz_param}" if biz_param else "/leads/temperature-manager/"
        return redirect(redirect_url)
    elif from_view == "hospital_config":
        return redirect("/leads/hospital-configuration/?tab=temperature")
    redirect_url = f"/leads/universal-masters/?tab=temperature&business={biz_param}" if biz_param else "/leads/universal-masters/?tab=temperature"
    return redirect(redirect_url)


@login_required
@user_passes_test(lambda u: u.can_manage_masters)
def universal_master_import(request):
    if request.method == "POST" and request.FILES.get("import_file"):
        import pandas as pd
        excel_file = request.FILES["import_file"]
        
        try:
            if excel_file.name.endswith('.csv'):
                df = pd.read_csv(excel_file)
            else:
                df = pd.read_excel(excel_file)
                
            items_created = 0
            
            for column in df.columns:
                group_name = str(column).strip()
                if not group_name or group_name.lower() == 'unnamed':
                    continue
                    
                group, _ = MasterGroup.objects.get_or_create(name=group_name)
                
                for value in df[column].dropna():
                    item_name = str(value).strip()
                    if item_name:
                        item, created = MasterItem.objects.get_or_create(
                            group=group, 
                            name=item_name, 
                            hospital=request.user.hospital,
                            defaults={"is_active": True}
                        )
                        if created:
                            items_created += 1
                            
            messages.success(request, f"Successfully imported {items_created} items from file.")
        except Exception as e:
            messages.error(request, f"Error processing file: {str(e)}")
            
    return redirect("leads:universal_masters")


@login_required
def temperature_manager(request):
    """
    Temperature Manager View:
    Accessible by business staff (Counsellors, HR, Lead Attendants, Managers, Admins, SuperAdmins).
    Restricted for Doctors.
    """
    if request.user.role == User.Role.DOCTOR:
        messages.error(request, "Permission denied: Doctors do not have access to Temperature Manager.")
        return redirect("dashboard:home")
    from leads.models import MasterGroup, MasterItem
    from accounts.models import Hospital

    user_biz = getattr(request.user, "business", None)
    is_global_admin = request.user.is_superuser or (
        request.user.role == User.Role.SUPER_ADMIN and not user_biz
    )

    available_businesses = list(Hospital.objects.filter(is_active=True).order_by("id")) if is_global_admin else []
    if user_biz and not available_businesses:
        available_businesses = [user_biz]

    biz_param = request.GET.get("business", "").strip()
    if is_global_admin:
        if biz_param == "default":
            current_hospital = None
            is_default_tab = True
        elif biz_param and biz_param.isdigit():
            current_hospital = Hospital.objects.filter(id=int(biz_param)).first()
            is_default_tab = False
        else:
            current_hospital = user_biz
            is_default_tab = False if current_hospital else True
    else:
        current_hospital = user_biz
        is_default_tab = False if current_hospital else True

    pos_group, _ = MasterGroup.objects.get_or_create(
        name="Positive Remarks", 
        defaults={"description": "Positive call remarks & notes that shift lead temperature UP to Hot / Warm"}
    )
    neg_group, _ = MasterGroup.objects.get_or_create(
        name="Negative Remarks", 
        defaults={"description": "Negative call remarks & notes that shift lead temperature DOWN to Warm / Cold / Freeze"}
    )

    pos_items = MasterItem.objects.filter(group=pos_group, hospital=current_hospital).order_by("order", "name")
    neg_items = MasterItem.objects.filter(group=neg_group, hospital=current_hospital).order_by("order", "name")

    context = {
        "active": "temperature_manager",
        "current_hospital": current_hospital,
        "is_global_admin": is_global_admin,
        "available_businesses": available_businesses,
        "is_default_tab": is_default_tab,
        "pos_group": pos_group,
        "neg_group": neg_group,
        "pos_items": pos_items,
        "neg_items": neg_items,
    }
    return render(request, "leads/temperature_manager.html", context)


@login_required
def custom_message_manager(request):
    """
    WhatsApp Custom Template Manager:
    Allows user to customize automated WhatsApp message templates.
    - For Academy businesses: Enquiry/Welcome, Campus Visit, Admission Confirmed, Payment Due/Installment templates.
    - For Hospital businesses: Booking, Follow-up, Billing/Thank-you templates.
    """
    if request.user.role == User.Role.DOCTOR:
        messages.error(request, "Permission denied: Doctors do not have access to Custom Message Manager.")
        return redirect("dashboard:home")

    from leads.models import UserCustomMessage
    from accounts.models import Hospital
    import urllib.parse

    user = request.user
    biz = getattr(user, 'business', None)
    
    # Determine business domain strictly from industry
    is_hospital = False
    is_academy = False
    if biz and hasattr(biz, 'industry'):
        if biz.industry == "HOSPITAL":
            is_hospital = True
        elif biz.industry == "ACADEMY":
            is_academy = True
    elif user.industry == "HOSPITAL":
        is_hospital = True
    elif user.industry == "ACADEMY" or getattr(user, 'is_zappcode_user', False):
        is_academy = True

    agent_name = user.get_full_name() or user.username

    # =========================================================================
    # 1. ACADEMY / EDUCATION INDUSTRY
    # =========================================================================
    if is_academy:
        biz_name = biz.name if biz else "Zappcode Academy"
        biz_address = (biz.address if biz and hasattr(biz, 'address') and biz.address else f"{biz_name} Main Campus").strip()

        # 1. Enquiry / Welcome Template
        default_enquiry_text = (
            f"Hello {{student_name}},\n\n"
            f"Thank you for inquiring about our {{course_name}} program at {{business_name}}!\n\n"
            f"We are excited to help you elevate your career with hands-on live project training and expert mentorship.\n\n"
            f"📍 Campus Address: {{business_address}}\n\n"
            f"Would you prefer a 10-minute online counseling session or visiting our campus for a personalized demo class?\n\n"
            f"Warm Regards,\n{{counselor_name}}\nAdmissions & Career Team - {{business_name}}"
        )

        # 2. Campus Visit Template
        default_visit_text = (
            f"Hello {{student_name}},\n\n"
            f"Greetings from {{business_name}}!\n\n"
            f"Your campus visit & career counseling demo session for {{course_name}} has been scheduled:\n\n"
            f"📅 Date: {{visit_date}}\n"
            f"⏰ Slot / Time: {{visit_slot}}\n"
            f"📍 Campus Address: {{business_address}}\n\n"
            f"Please arrive on time. Our counseling team looks forward to meeting you and giving you a comprehensive tour of our lab facilities.\n\n"
            f"Warm Regards,\n{{counselor_name}}\nAdmissions Team - {{business_name}}"
        )

        # 3. Admission Confirmed Template
        default_admission_text = (
            f"Hello {{student_name}},\n\n"
            f"🎉 Congratulations & Welcome to {{business_name}}!\n\n"
            f"We are thrilled to confirm your admission for the {{course_name}} program.\n\n"
            f"📌 Enrollment Details:\n"
            f"• Admission Date: {{admission_date}}\n"
            f"• Batch: {{batch_name}}\n"
            f"• Timing: {{batch_timing}}\n"
            f"• Campus: {{business_address}}\n\n"
            f"Your LMS access credentials and orientation schedule will be shared with you shortly.\n\n"
            f"Best Wishes,\n{{counselor_name}}\nAdmissions Team - {{business_name}}"
        )

        # 4. Payment Due / Pending Template
        default_payment_text = (
            f"Hello {{student_name}},\n\n"
            f"This is a gentle payment reminder from {{business_name}} regarding your {{course_name}} fee.\n\n"
            f"📌 Fee Summary:\n"
            f"• Total Course Fee: {{total_amount}}\n"
            f"• Amount Paid: {{paid_amount}}\n"
            f"• Pending Balance: {{balance_amount}}\n"
            f"• Payment Due Date: {{payment_due_date}}\n\n"
            f"Please ensure payment is completed by {{payment_due_date}} to keep your LMS access and batch allocation active.\n\n"
            f"Warm Regards,\n{{counselor_name}}\nAccounts & Admissions - {{business_name}}"
        )

        enquiry_msg_obj, _ = UserCustomMessage.objects.get_or_create(
            user=user, message_type="ENQUIRY",
            defaults={"hospital": biz, "custom_text": default_enquiry_text, "is_confirmed": False}
        )
        visit_msg_obj, _ = UserCustomMessage.objects.get_or_create(
            user=user, message_type="VISIT",
            defaults={"hospital": biz, "custom_text": default_visit_text, "is_confirmed": False}
        )
        admission_msg_obj, _ = UserCustomMessage.objects.get_or_create(
            user=user, message_type="ADMISSION",
            defaults={"hospital": biz, "custom_text": default_admission_text, "is_confirmed": False}
        )
        payment_msg_obj, _ = UserCustomMessage.objects.get_or_create(
            user=user, message_type="PAYMENT_DUE",
            defaults={"hospital": biz, "custom_text": default_payment_text, "is_confirmed": False}
        )

        template_map = {
            "ENQUIRY": (enquiry_msg_obj, default_enquiry_text),
            "VISIT": (visit_msg_obj, default_visit_text),
            "ADMISSION": (admission_msg_obj, default_admission_text),
            "PAYMENT_DUE": (payment_msg_obj, default_payment_text),
        }

        if request.method == "POST":
            action = request.POST.get("action", "save")
            msg_type = request.POST.get("message_type", "ENQUIRY").upper()
            custom_text = request.POST.get("custom_text", "").strip()

            target_obj, def_text = template_map.get(msg_type, (enquiry_msg_obj, default_enquiry_text))

            if action == "save":
                target_obj.custom_text = custom_text
                target_obj.hospital = biz
                target_obj.save()
                messages.success(request, f"{target_obj.get_message_type_display()} updated successfully. Click 'Confirm & Activate' to use your custom message.")
            elif action == "confirm":
                target_obj.custom_text = custom_text or target_obj.custom_text
                target_obj.is_confirmed = True
                target_obj.hospital = biz
                target_obj.save()
                messages.success(request, f"✅ Custom {target_obj.get_message_type_display()} is now ACTIVE! Default system template is OFF.")
            elif action == "reset_default":
                target_obj.is_confirmed = False
                target_obj.custom_text = def_text
                target_obj.save()
                messages.info(request, f"Custom message turned OFF. Switched back to System Default template for {target_obj.get_message_type_display()}.")

            return redirect(f"/leads/custom-messages/?tab={msg_type.lower()}")

        active_tab = request.GET.get("tab", "enquiry").lower()

        return render(request, "leads/custom_message_manager.html", {
            "active": "custom_message_manager",
            "is_academy": True,
            "is_hospital": False,
            "enquiry_msg": enquiry_msg_obj,
            "visit_msg": visit_msg_obj,
            "admission_msg": admission_msg_obj,
            "payment_msg": payment_msg_obj,
            "default_enquiry_text": default_enquiry_text,
            "default_visit_text": default_visit_text,
            "default_admission_text": default_admission_text,
            "default_payment_text": default_payment_text,
            "active_tab": active_tab,
            "biz": biz,
            "hosp": biz,
        })

    # =========================================================================
    # 2. HEALTHCARE / HOSPITAL INDUSTRY
    # =========================================================================
    hosp = biz
    if is_hospital:
        hosp_name = hosp.name if hosp else "Nelson Mother & Child Care Hospital"

        default_booking_text = (
            f"Hello {{patient_name}},\n\n"
            f"Welcome to {{hospital_name}} ({{branch_name}})!\n\n"
            f"Your consultation appointment with {{doctor_name}} is confirmed on {{appointment_date}} at {{appointment_time}}.\n\n"
            f"Hospital Address: {{hospital_address}}\n\n"
            f"Please arrive 15 minutes prior to your scheduled slot. We look forward to assisting you.\n\n"
            f"For any queries or assistance, feel free to contact us.\n\n"
            f"Warm Regards,\n{{user_name}}\n{{hospital_name}}"
        )

        default_followup_text = (
            f"Hello {{patient_name}},\n\n"
            f"Thank you for your enquiry with {{hospital_name}} ({{branch_name}})!\n\n"
            f"We are pleased to assist you with your healthcare and doctor consultation inquiry.\n\n"
            f"Hospital Address: {{hospital_address}}\n\n"
            f"Please let us know your preferred date, time, or specialist requirement so we can schedule your appointment promptly.\n\n"
            f"Warm Regards,\n{{user_name}}\nPatient Care Team - {{hospital_name}}"
        )

        default_billing_text = (
            f"Hello {{patient_name}},\n\n"
            f"Thank you for visiting {{hospital_name}} ({{branch_name}})!\n\n"
            f"Your consultation with {{doctor_name}} on {{appointment_date}} has been completed successfully.\n\n"
            f"We truly appreciate having the opportunity to care for your health and well-being. If you have any follow-up questions, prescription queries, or require further medical assistance, please feel free to reach out to us.\n\n"
            f"Hospital Address: {{hospital_address}}\n\n"
            f"Wishing you great health and a speedy recovery!\n\n"
            f"Warm Regards,\n{{user_name}}\nPatient Care Team - {{hospital_name}}"
        )
    else:
        # =====================================================================
        # 3. OTHER / GENERAL BUSINESSES (IT Services, Agency, Corporate)
        # =====================================================================
        biz_name = hosp.name if hosp else "Our Business"
        biz_address = (hosp.address if hosp and hasattr(hosp, 'address') and hosp.address else f"{biz_name} Office").strip()

        default_booking_text = (
            f"Hello {{client_name}},\n\n"
            f"Greetings from {{business_name}}!\n\n"
            f"Your meeting / demo appointment for {{service_name}} is confirmed on {{booking_date}} at {{booking_time}}.\n\n"
            f"Office Address: {{business_address}}\n\n"
            f"We look forward to meeting with you.\n\n"
            f"Warm Regards,\n{{user_name}}\n{{business_name}}"
        )

        default_followup_text = (
            f"Hello {{client_name}},\n\n"
            f"Thank you for contacting {{business_name}} regarding {{service_name}}!\n\n"
            f"Our team is glad to assist you. Please let us know a convenient time to discuss your requirements in detail.\n\n"
            f"Office Address: {{business_address}}\n\n"
            f"Warm Regards,\n{{user_name}}\nBusiness Team - {{business_name}}"
        )

        default_billing_text = (
            f"Hello {{client_name}},\n\n"
            f"Thank you for choosing {{business_name}}!\n\n"
            f"Your order/service completion for {{service_name}} has been processed successfully.\n\n"
            f"If you need further assistance or project support, feel free to reach out to us.\n\n"
            f"Office Address: {{business_address}}\n\n"
            f"Warm Regards,\n{{user_name}}\nCustomer Success Team - {{business_name}}"
        )

    booking_msg_obj, _ = UserCustomMessage.objects.get_or_create(
        user=user, message_type="BOOKING",
        defaults={"hospital": hosp, "custom_text": default_booking_text, "is_confirmed": False}
    )
    followup_msg_obj, _ = UserCustomMessage.objects.get_or_create(
        user=user, message_type="FOLLOWUP",
        defaults={"hospital": hosp, "custom_text": default_followup_text, "is_confirmed": False}
    )
    billing_msg_obj, _ = UserCustomMessage.objects.get_or_create(
        user=user, message_type="BILLING",
        defaults={"hospital": hosp, "custom_text": default_billing_text, "is_confirmed": False}
    )

    if request.method == "POST":
        action = request.POST.get("action", "save")
        msg_type = request.POST.get("message_type", "BOOKING").upper()
        custom_text = request.POST.get("custom_text", "").strip()

        if msg_type == "BOOKING":
            target_obj = booking_msg_obj
            default_text = default_booking_text
        elif msg_type == "BILLING":
            target_obj = billing_msg_obj
            default_text = default_billing_text
        else:
            target_obj = followup_msg_obj
            default_text = default_followup_text
        
        if action == "save":
            target_obj.custom_text = custom_text
            target_obj.hospital = hosp
            target_obj.save()
            messages.success(request, f"{target_obj.get_message_type_display()} updated successfully. Please confirm to activate your custom message.")
        elif action == "confirm":
            target_obj.custom_text = custom_text or target_obj.custom_text
            target_obj.is_confirmed = True
            target_obj.hospital = hosp
            target_obj.save()
            messages.success(request, f"✅ Custom {target_obj.get_message_type_display()} confirmed and activated! Default system template is now OFF.")
        elif action == "reset_default":
            target_obj.is_confirmed = False
            target_obj.custom_text = default_text
            target_obj.save()
            messages.info(request, f"Custom message turned OFF. Switched back to System Generated Default template for {target_obj.get_message_type_display()}.")

        return redirect(f"/leads/custom-messages/?tab={msg_type.lower()}")

    active_tab = request.GET.get("tab", "booking").lower()

    return render(request, "leads/custom_message_manager.html", {
        "active": "custom_message_manager",
        "is_academy": False,
        "is_hospital": is_hospital,
        "booking_msg": booking_msg_obj,
        "followup_msg": followup_msg_obj,
        "billing_msg": billing_msg_obj,
        "default_booking_text": default_booking_text,
        "default_followup_text": default_followup_text,
        "default_billing_text": default_billing_text,
        "active_tab": active_tab,
        "hosp": hosp,
        "biz": hosp,
    })
