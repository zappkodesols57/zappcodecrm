import json
from datetime import datetime

import pandas as pd
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone

from leads.models import Lead, SourceCategory, LeadSource, Course, LeadStage
from followups.models import FollowUp, FollowUpStatus, FollowUpMode
from .models import ImportJob, ImportError as ImportErrorModel
from . import cleaning

TARGET_FIELDS = [
    ("IGNORE", "— Ignore this column —"),
    ("name", "Name"),
    ("mobile", "Mobile Number"),
    ("email", "Email"),
    ("city", "City"),
    ("course", "Course"),
    ("source", "Lead Source / Origin"),
    ("temperature", "Lead Temperature / State"),
    ("deal_status", "Deal Status"),
    ("admission_status", "Admission Status"),
    ("inquiry_date", "Inquiry Date"),
    ("notes", "Notes"),
]

GUESS_KEYWORDS = {
    "name": ["name"],
    "mobile": ["contact", "mobile", "phone"],
    "email": ["email"],
    "city": ["city", "location"],
    "course": ["course"],
    "source": ["origin", "source", "leads origin"],
    "temperature": ["lead state", "state", "temperature"],
    "deal_status": ["deal status"],
    "admission_status": ["admission", "admision"],
    "inquiry_date": ["inquiry", "date of inquiry"],
    "notes": ["remark", "comment", "issue", "note"],
}


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


@login_required
@user_passes_test(lambda u: u.can_import_export)
def upload(request):
    if request.method == "POST" and "excel_file" in request.FILES:
        job = ImportJob.objects.create(
            file=request.FILES["excel_file"],
            original_filename=request.FILES["excel_file"].name,
            created_by=request.user,
        )
        try:
            xl = pd.ExcelFile(job.file.path)
        except Exception as e:
            job.delete()
            messages.error(request, f"Could not read this file: {e}")
            return redirect("imports:upload")
        return render(request, "imports/select_sheet.html", {
            "active": "import", "job": job, "sheets": xl.sheet_names,
        })
    return render(request, "imports/upload.html", {"active": "import"})


@login_required
@user_passes_test(lambda u: u.can_import_export)
def pick_sheet(request, pk):
    job = get_object_or_404(ImportJob, pk=pk)
    sheet_name = request.POST.get("sheet_name")
    raw = pd.read_excel(job.file.path, sheet_name=sheet_name, header=None)
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


def _parse_row(row, cols, mapping):
    data = {}
    for idx_str, field in mapping.items():
        idx = int(idx_str)
        if idx >= len(cols):
            continue
        val = row.iloc[idx] if hasattr(row, "iloc") else row[cols[idx]]
        data[field] = val

    name = str(data.get("name", "")).strip()
    mobile, alt_mobile = cleaning.clean_phone(data.get("mobile"))
    source_cat, source_name, source_ambiguous = cleaning.normalize_source(data.get("source"))
    temperature, temp_ambiguous = cleaning.normalize_temperature(data.get("temperature"))
    course_name, course_ambiguous = cleaning.normalize_course(data.get("course"))
    inquiry_date = cleaning.parse_date(data.get("inquiry_date")) or timezone.localdate()

    warnings = []
    if not name or name.lower() == "nan":
        warnings.append("Missing name")
    if not mobile:
        warnings.append("Missing/invalid mobile number")
    if source_ambiguous and data.get("source"):
        warnings.append(f"Ambiguous source '{data.get('source')}' — flagged for review")
    if course_ambiguous and data.get("course"):
        warnings.append(f"Unrecognized course '{data.get('course')}' — kept as-is")

    return {
        "name": name, "mobile": mobile, "alt_mobile": alt_mobile,
        "email": str(data.get("email", "") or "").strip(),
        "city": str(data.get("city", "") or "").strip(),
        "course_name": course_name, "source_category": source_cat, "source_name": source_name,
        "temperature": temperature or "UNCONTACTED", "inquiry_date": inquiry_date,
        "notes": str(data.get("notes", "") or "").strip(),
        "deal_status": cleaning.normalize_deal_status(data.get("deal_status"), data.get("admission_status")),
        "admission_status": cleaning.normalize_admission_status(data.get("admission_status")),
        "warnings": warnings,
    }


@login_required
@user_passes_test(lambda u: u.can_import_export)
def run_import(request, pk):
    job = get_object_or_404(ImportJob, pk=pk)
    header_row = job.column_mapping.get("header_row", 0)
    mapping = job.column_mapping.get("field_map", {})
    date_cols_idx = [int(i) for i in job.column_mapping.get("date_columns", [])]
    on_duplicate = request.POST.get("on_duplicate", "skip")

    df = pd.read_excel(job.file.path, sheet_name=job.sheet_name, header=header_row)
    df = df.dropna(how="all")
    cols = list(df.columns)

    imported = updated = skipped = duplicate = invalid = 0
    default_stage = _default_stage()

    for row_num, (_, row) in enumerate(df.iterrows(), start=header_row + 2):
        parsed = _parse_row(row, cols, mapping)
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
        course = _get_or_create_course(parsed["course_name"])

        if existing and on_duplicate == "update":
            existing.city = parsed["city"] or existing.city
            existing.email = parsed["email"] or existing.email
            existing.course = course or existing.course
            existing.notes = (existing.notes + "\n" + parsed["notes"]).strip() if parsed["notes"] else existing.notes
            existing.import_job = job
            existing.import_source_file = job.original_filename
            existing.save()
            lead = existing
            updated += 1
        else:
            lead = Lead.objects.create(
                name=parsed["name"], mobile=parsed["mobile"], alternate_mobile=parsed["alt_mobile"],
                email=parsed["email"], city=parsed["city"], course=course,
                temperature=parsed["temperature"], stage=default_stage,
                deal_status=parsed["deal_status"], admission_status=parsed["admission_status"],
                inquiry_date=parsed["inquiry_date"], source_category=cat, lead_source=src,
                notes=parsed["notes"], created_by=request.user,
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
    jobs = ImportJob.objects.select_related("created_by").all()
    return render(request, "imports/history.html", {"active": "import_history", "jobs": jobs})


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
    if date_from:
        leads = leads.filter(inquiry_date__gte=date_from)
    if date_to:
        leads = leads.filter(inquiry_date__lte=date_to)

    def build_row(l):
        if is_hospital:
            cd = l.custom_data or {}
            return {
                "Lead ID": l.lead_code, "Name": l.name, "Mobile": l.mobile, "Email": l.email,
                "Location": l.location, "Source Category": str(l.source_category or ""),
                "Lead Source": cd.get("lead_source", ""), "Campaign": cd.get("campaign", ""),
                "Stage": str(l.stage), "Temperature": l.get_temperature_display(),
                "Deal Status": cd.get("deal_status", ""),
                "Inquiry Date": str(l.inquiry_date), "Assigned To": str(l.assigned_to or ""),
                "Next Follow-up": str(l.next_followup_date) if l.next_followup_date else "", 
                "Created At": l.created_at.strftime("%Y-%m-%d %H:%M"),
            }
        else:
            return {
                "Lead ID": l.lead_code, "Name": l.name, "Mobile": l.mobile, "Email": l.email,
                "City": l.city, "Course": str(l.course or ""), "Source Category": str(l.source_category or ""),
                "Lead Source": str(l.lead_source or ""), "Campaign": str(l.campaign or ""),
                "Stage": str(l.stage), "Temperature": l.get_temperature_display(),
                "Deal Status": l.get_deal_status_display(), "Admission Status": l.get_admission_status_display(),
                "Inquiry Date": str(l.inquiry_date), "Assigned To": str(l.assigned_to or ""),
                "Next Follow-up": str(l.next_followup_date) if l.next_followup_date else "", 
                "Created At": l.created_at.strftime("%Y-%m-%d %H:%M"),
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
@user_passes_test(lambda u: u.can_import_export)
def download_template(request):
    from openpyxl import Workbook
    from openpyxl.worksheet.datavalidation import DataValidation
    from openpyxl.styles import Font, PatternFill, Alignment
    from leads.models import LeadTemperature, DealStatus, AdmissionStatus

    wb = Workbook()
    ws = wb.active
    ws.title = "Leads Import Template"

    headers = [
        "Inquiry Date", 
        "Name", 
        "Mobile", 
        "Alternate Mobile", 
        "Email", 
        "City", 
        "Course", 
        "Lead Source", 
        "Lead Temperature", 
        "Deal Status", 
        "Admission Status", 
        "Notes"
    ]
    
    ws.append(headers)

    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
    align_center = Alignment(horizontal="center", vertical="center")
    
    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = align_center

    sample_row = [
        "2026-08-11",
        "Rahul Kumar",
        "9876543210",
        "9876543211",
        "rahul@example.com",
        "Nagpur",
        "Data Analytics",
        "Meta Ads",
        "WARM",
        "OPEN",
        "NOT_APPLIED",
        "Interested in weekend batch"
    ]
    ws.append(sample_row)

    sample_font = Font(name="Calibri", size=10, italic=True, color="595959")
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=2, column=col_idx)
        cell.font = sample_font

    courses = list(Course.objects.filter(is_active=True).values_list("name", flat=True))
    sources = list(LeadSource.objects.filter(is_active=True).values_list("name", flat=True))
    
    temperatures = [choice[0] for choice in LeadTemperature.choices]
    deal_statuses = [choice[0] for choice in DealStatus.choices]
    admission_statuses = [choice[0] for choice in AdmissionStatus.choices]

    data_ws = wb.create_sheet(title="DropdownData")
    
    for idx, item in enumerate(courses, start=1):
        data_ws.cell(row=idx, column=1, value=item)
    for idx, item in enumerate(sources, start=1):
        data_ws.cell(row=idx, column=2, value=item)
    for idx, item in enumerate(temperatures, start=1):
        data_ws.cell(row=idx, column=3, value=item)
    for idx, item in enumerate(deal_statuses, start=1):
        data_ws.cell(row=idx, column=4, value=item)
    for idx, item in enumerate(admission_statuses, start=1):
        data_ws.cell(row=idx, column=5, value=item)

    data_ws.sheet_state = "hidden"

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

    add_validation("G", "A", len(courses), "Select a course")
    add_validation("H", "B", len(sources), "Select a lead source")
    add_validation("I", "C", len(temperatures), "Select temperature (HOT, WARM, COLD)")
    add_validation("J", "D", len(deal_statuses), "Select deal status (OPEN, WON, LOST, HOLD)")
    add_validation("K", "E", len(admission_statuses), "Select admission status")

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = col[0].column_letter
        ws.column_dimensions[col_letter].width = max(max_len + 3, 15)

    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = 'attachment; filename="leads_import_template.xlsx"'
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
        
        try:
            df = pd.read_excel(excel_file.path, sheet_name=0)
        except Exception as e:
            job.delete()
            messages.error(request, f"Could not read the uploaded Excel file: {e}")
            return redirect("imports:upload")
        
        df = df.dropna(how="all")
        df.columns = [str(c).strip() for c in df.columns]
        
        required_cols = ["Name", "Mobile"]
        missing_cols = [col for col in required_cols if col not in df.columns]
        if missing_cols:
            job.delete()
            messages.error(request, f"Missing required columns in template: {', '.join(missing_cols)}")
            return redirect("imports:upload")
            
        default_stage = _default_stage()
        
        imported = updated = skipped = duplicate = invalid = 0
        cols = list(df.columns)
        
        start_idx = 0
        if len(df) > 0:
            first_row_name = str(df.iloc[0].get("Name", "")).strip().lower()
            first_row_mobile = str(df.iloc[0].get("Mobile", "")).strip()
            if "rahul kumar" in first_row_name or "9876543210" in first_row_mobile:
                start_idx = 1
                
        for idx in range(start_idx, len(df)):
            row = df.iloc[idx]
            row_num = idx + 2
            
            name = str(row.get("Name", "")).strip()
            mobile_raw = row.get("Mobile")
            email = str(row.get("Email", "") or "").strip()
            city = str(row.get("City", "") or "").strip()
            alt_mobile_raw = row.get("Alternate Mobile")
            course_raw = row.get("Course")
            source_raw = row.get("Lead Source")
            temp_raw = row.get("Lead Temperature")
            deal_raw = row.get("Deal Status")
            adm_raw = row.get("Admission Status")
            notes = str(row.get("Notes", "") or "").strip()
            inquiry_date_raw = row.get("Inquiry Date")
            
            mobile, alt_mobile = cleaning.clean_phone(mobile_raw)
            if not alt_mobile and alt_mobile_raw:
                _, alt_mobile = cleaning.clean_phone(alt_mobile_raw)
                
            inquiry_date = cleaning.parse_date(inquiry_date_raw) or timezone.localdate()
            
            if not name or name.lower() == "nan" or not mobile:
                invalid += 1
                error_msg = []
                if not name or name.lower() == "nan":
                    error_msg.append("Missing Name")
                if not mobile:
                    error_msg.append("Missing/invalid Mobile number")
                ImportErrorModel.objects.create(
                    job=job,
                    row_number=row_num,
                    error_message=", ".join(error_msg),
                    raw_row_data={str(c): str(row[c]) for c in cols[:15] if c in row}
                )
                continue
                
            existing = Lead.objects.filter(mobile=mobile).first()
            if not existing:
                existing = next((l for l in Lead.objects.only("id", "mobile") if Lead.clean_mobile(l.mobile) == mobile), None)
                
            if existing:
                if on_duplicate == "skip":
                    duplicate += 1
                    continue
                elif on_duplicate == "create":
                    existing = None
            
            course_name, _ = cleaning.normalize_course(course_raw)
            course = None
            if course_name:
                course, _ = Course.objects.get_or_create(name=course_name)
                
            cat = src = None
            if source_raw:
                cat_name, src_name, _ = cleaning.normalize_source(source_raw)
                if cat_name:
                    cat, _ = SourceCategory.objects.get_or_create(name=cat_name)
                    src, _ = LeadSource.objects.get_or_create(name=src_name, category=cat)
            
            temperature, _ = cleaning.normalize_temperature(temp_raw)
            if not temperature:
                temperature = "COLD"
                
            deal_status = cleaning.normalize_deal_status(deal_raw, adm_raw)
            admission_status = cleaning.normalize_admission_status(adm_raw)
            
            if existing and on_duplicate == "update":
                existing.name = name or existing.name
                existing.email = email or existing.email
                existing.city = city or existing.city
                if course:
                    existing.course = course
                if cat:
                    existing.source_category = cat
                if src:
                    existing.lead_source = src
                existing.temperature = temperature or existing.temperature
                existing.deal_status = deal_status or existing.deal_status
                existing.admission_status = admission_status or existing.admission_status
                existing.notes = (existing.notes + "\n" + notes).strip() if notes else existing.notes
                existing.save()
                updated += 1
            else:
                Lead.objects.create(
                    name=name,
                    mobile=mobile,
                    alternate_mobile=alt_mobile,
                    email=email,
                    city=city,
                    course=course,
                    temperature=temperature,
                    stage=default_stage,
                    deal_status=deal_status,
                    admission_status=admission_status,
                    inquiry_date=inquiry_date,
                    source_category=cat,
                    lead_source=src,
                    notes=notes,
                    created_by=request.user,
                    import_source_file=job.original_filename,
                    import_source_sheet="Sheet1",
                    import_source_row=row_num,
                    import_job=job
                )
                imported += 1
                
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
        return redirect("imports:job_detail", pk=job.pk)
        
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
