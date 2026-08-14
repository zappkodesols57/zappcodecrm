"""
One-off historical data migration for the two source workbooks:
  - ZA Leads Data Till June 2026.xlsx
  - Academy Leads for Jul-Aug 2026.xlsx

Usage:
  python manage.py migrate_excel_data /path/to/ZA.xlsx /path/to/Academy.xlsx

What it does (see docs/EXCEL_ANALYSIS.md for the full data-quality report
this was derived from):
  - PRIMARY sheets create Lead records: '2526Leads' (ZA — the master
    consolidated list), 'June-July', 'Aug', 'OLD data calling ' (Academy).
  - ENRICHMENT sheets ('Old Sheet 25', 'Feb-MarLead26', 'April - May 26',
    'May-June 26') are wide, one-column-per-date follow-up logs. Every
    non-empty date-column cell becomes a FollowUp record attached to the
    lead with the matching mobile number (matched via Lead.clean_mobile),
    or creates the lead first if it doesn't exist yet.
  - SKIPPED sheets ('Total Leads', 'Dashboard', 'Positive Leads Jan-May
    2026', 'New Jan Leads-26', 'Most Recent ', 'Dec-Jan Leads status ',
    'Medium Cold', 'Positive', 'Avg positive', 'Daily updates') are
    status-filtered views that are >90% duplicates of the sheets above —
    skipped to avoid re-processing the same leads twice. 'Counselling' is
    excluded entirely: it is staff/counsellor hiring data, not student leads.
  - Every ambiguous value (unrecognized source, unmapped course spelling,
    unparseable lead-state) is written to ambiguous_values_report.csv in
    the current directory instead of being silently guessed.
"""
import csv
import sys

import pandas as pd
from django.core.management.base import BaseCommand
from django.utils import timezone

from leads.models import Lead, LeadStage
from followups.models import FollowUp, FollowUpStatus, FollowUpMode
from imports import cleaning


def find_col(columns, keywords, exclude=None):
    exclude = exclude or []
    for i, c in enumerate(columns):
        cs = str(c).strip().lower()
        if any(ex in cs for ex in exclude):
            continue
        if any(k in cs for k in keywords):
            return i
    return None


def is_date_header(h):
    import datetime as dt
    if isinstance(h, (dt.datetime, dt.date)):
        return True
    return cleaning.parse_date(h) is not None and len(str(h)) >= 6


class Command(BaseCommand):
    help = "Migrate the historical ZA + Academy lead Excel workbooks into the CRM."

    def add_arguments(self, parser):
        parser.add_argument("za_file")
        parser.add_argument("academy_file")

    def handle(self, *args, **options):
        self.ambiguous = []
        self.stats = {"leads_created": 0, "leads_matched": 0, "followups_created": 0, "rows_skipped": 0}
        default_stage = LeadStage.objects.order_by("order").first()

        za = options["za_file"]
        academy = options["academy_file"]

        # --- PRIMARY: creates leads -------------------------------------------------
        self.load_primary_sheet(za, "2526Leads", default_stage,
                                 name_kw=["name"], mobile_kw=["contact"], city_kw=["city"],
                                 course_kw=["course"], origin_kw=["origin"], date_kw=["date of inquiry"],
                                 state_kw=["lead state"], deal_kw=["deal status"], adm_kw=["admision", "admission"])

        self.load_wide_sheet(academy, "June-July", default_stage, header=0,
                              name_kw=["name"], mobile_kw=["contact"], course_kw=["course"],
                              city_kw=["location"], date_kw=["inquiry dt"], create_leads=True)
        self.load_wide_sheet(academy, "Aug", default_stage, header=1,
                              name_kw=["name"], mobile_kw=["contact"], course_kw=["course"],
                              city_kw=["location"], date_kw=["inquiry dt"], create_leads=True)
        self.load_wide_sheet(academy, "OLD data calling ", default_stage, header=1,
                              name_kw=["name"], mobile_kw=["contact"], course_kw=["course"],
                              city_kw=["location"], date_kw=["date of inquiry"], create_leads=True)

        # --- ENRICHMENT: attach follow-ups, create lead only if unmatched ----------
        for sheet in ["Old Sheet 25", "Feb-MarLead26", "April - May 26", "May-June 26"]:
            self.load_wide_sheet(za, sheet, default_stage, header=0,
                                  name_kw=["name"], mobile_kw=["contact"], course_kw=["course"],
                                  city_kw=["city"], date_kw=["date of inquiry"], create_leads=True)

        self.write_ambiguous_report()
        self.stdout.write(self.style.SUCCESS(
            f"Done. Leads created: {self.stats['leads_created']}, "
            f"leads matched (enrichment): {self.stats['leads_matched']}, "
            f"follow-ups created: {self.stats['followups_created']}, "
            f"rows skipped (no usable phone): {self.stats['rows_skipped']}"
        ))
        self.stdout.write(f"Ambiguous values flagged: {len(self.ambiguous)} -> ambiguous_values_report.csv")

    # -------------------------------------------------------------------------
    def get_or_create_lead(self, name, mobile, city, course_raw, default_stage, source_file, sheet, row_num, origin_raw=None, inquiry_date=None):
        digits = Lead.clean_mobile(mobile)
        if not digits:
            self.stats["rows_skipped"] += 1
            return None, False
        existing = next((l for l in Lead.objects.only("id", "mobile") if Lead.clean_mobile(l.mobile) == digits), None)
        if existing:
            self.stats["leads_matched"] += 1
            return Lead.objects.get(pk=existing.pk), False

        course_name, course_amb = cleaning.normalize_course(course_raw)
        course = None
        if course_name:
            from leads.models import Course
            course, _ = Course.objects.get_or_create(name=course_name)
        if course_amb and course_raw:
            self.flag(source_file, sheet, row_num, "course", course_raw, "Unrecognized course spelling — kept as typed")

        cat = src = None
        if origin_raw:
            cat_name, src_name, amb = cleaning.normalize_source(origin_raw)
            if cat_name:
                from leads.models import SourceCategory, LeadSource
                cat, _ = SourceCategory.objects.get_or_create(name=cat_name)
                src, _ = LeadSource.objects.get_or_create(name=src_name, category=cat)
            if amb:
                self.flag(source_file, sheet, row_num, "origin", origin_raw, "Ambiguous / unrecognized source")

        lead = Lead.objects.create(
            name=(name or "Unknown").strip()[:150], mobile=digits, city=(city or "").strip()[:100],
            course=course, stage=default_stage, inquiry_date=inquiry_date or timezone.localdate(),
            source_category=cat, lead_source=src,
            import_source_file=source_file, import_source_sheet=sheet, import_source_row=row_num,
        )
        self.stats["leads_created"] += 1
        return lead, True

    def flag(self, source_file, sheet, row_num, field, raw_value, reason):
        self.ambiguous.append({
            "file": source_file, "sheet": sheet, "row": row_num, "field": field,
            "raw_value": raw_value, "reason": reason,
        })

    def write_ambiguous_report(self):
        if not self.ambiguous:
            return
        with open("ambiguous_values_report.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["file", "sheet", "row", "field", "raw_value", "reason"])
            writer.writeheader()
            writer.writerows(self.ambiguous)

    # -------------------------------------------------------------------------
    def load_primary_sheet(self, path, sheet, default_stage, name_kw, mobile_kw, city_kw, course_kw, origin_kw, date_kw, state_kw, deal_kw, adm_kw):
        try:
            df = pd.read_excel(path, sheet_name=sheet, header=0)
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Skipping {sheet}: {e}"))
            return
        cols = list(df.columns)
        i_name, i_mob, i_city = find_col(cols, name_kw), find_col(cols, mobile_kw), find_col(cols, city_kw)
        i_course, i_origin, i_date = find_col(cols, course_kw), find_col(cols, origin_kw), find_col(cols, date_kw)
        i_state, i_deal, i_adm = find_col(cols, state_kw), find_col(cols, deal_kw), find_col(cols, adm_kw)

        for row_num, row in enumerate(df.itertuples(index=False), start=2):
            name = row[i_name] if i_name is not None else ""
            mobile, alt = cleaning.clean_phone(row[i_mob] if i_mob is not None else None)
            city = row[i_city] if i_city is not None else ""
            course_raw = row[i_course] if i_course is not None else None
            origin_raw = row[i_origin] if i_origin is not None else None
            inquiry_date = cleaning.parse_date(row[i_date]) if i_date is not None else None

            if not mobile:
                self.stats["rows_skipped"] += 1
                continue
            lead, created = self.get_or_create_lead(
                str(name) if pd.notna(name) else "", mobile, str(city) if pd.notna(city) else "",
                course_raw, default_stage, sheet.strip(), sheet, row_num, origin_raw, inquiry_date,
            )
            if not lead or not created:
                continue

            temp_raw = row[i_state] if i_state is not None else None
            deal_raw = row[i_deal] if i_deal is not None else None
            adm_raw = row[i_adm] if i_adm is not None else None
            temperature, temp_amb = cleaning.normalize_temperature(temp_raw)
            if temperature:
                lead.temperature = temperature
            elif temp_raw and pd.notna(temp_raw):
                self.flag(sheet.strip(), sheet, row_num, "lead_state", temp_raw, "Could not map to Hot/Warm/Cold")
            lead.deal_status = cleaning.normalize_deal_status(deal_raw, adm_raw)
            lead.admission_status = cleaning.normalize_admission_status(adm_raw)
            lead.save()

    def load_wide_sheet(self, path, sheet, default_stage, header, name_kw, mobile_kw, course_kw, city_kw, date_kw, create_leads=True):
        try:
            df = pd.read_excel(path, sheet_name=sheet, header=header)
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Skipping {sheet}: {e}"))
            return
        df = df.dropna(how="all")
        cols = list(df.columns)
        i_name, i_mob = find_col(cols, name_kw), find_col(cols, mobile_kw)
        i_course, i_city = find_col(cols, course_kw), find_col(cols, city_kw)
        i_date = find_col(cols, date_kw)
        date_col_idx = [i for i, c in enumerate(cols) if is_date_header(c)]

        for row_num, row in enumerate(df.itertuples(index=False), start=header + 2):
            name = row[i_name] if i_name is not None else ""
            mobile, alt = cleaning.clean_phone(row[i_mob] if i_mob is not None else None)
            if not mobile:
                self.stats["rows_skipped"] += 1
                continue
            city = row[i_city] if i_city is not None else ""
            course_raw = row[i_course] if i_course is not None else None
            inquiry_date = cleaning.parse_date(row[i_date]) if i_date is not None else None

            lead, created = self.get_or_create_lead(
                str(name) if pd.notna(name) else "", mobile, str(city) if pd.notna(city) else "",
                course_raw, default_stage, sheet.strip(), sheet, row_num, None, inquiry_date,
            )
            if not lead:
                continue

            for idx in date_col_idx:
                comment = row[idx]
                if pd.isna(comment) or str(comment).strip() in ("", "-", "nan", "NaT"):
                    continue
                fu_date = cleaning.parse_date(cols[idx]) or (inquiry_date or timezone.localdate())
                if FollowUp.objects.filter(lead=lead, followup_date=fu_date, comment=str(comment).strip()).exists():
                    continue
                FollowUp.objects.create(
                    lead=lead, followup_date=fu_date, followup_mode=FollowUpMode.OTHER,
                    followup_status=FollowUpStatus.COMPLETED, comment=str(comment).strip(),
                    imported_from_excel=True,
                )
                self.stats["followups_created"] += 1
