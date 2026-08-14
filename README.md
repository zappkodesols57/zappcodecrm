# Zappkode Lead Management CRM

A Django + Bootstrap 5 CRM for managing, tracking, and converting leads — built to replace the
Excel-based workflow while still supporting Excel import/export. This is a working MVP covering
Phases 1–7 of the original spec (see "What's built" below).

Your real historical data is already loaded: **1,209 leads** migrated from `ZA Leads Data Till
June 2026.xlsx` and `Academy Leads for Jul-Aug 2026.xlsx`, with **6,761 follow-up records**
reconstructed from the old date-column call logs.

## Quick start (local, SQLite — zero setup)

```bash
cd leadcrm
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python manage.py runserver
```

Open http://127.0.0.1:8000 and log in:

- **Username:** `admin`
- **Password:** `Admin@12345`

**Change this password immediately** (top-right menu → or via `/django-admin/`) — it's a
placeholder, not meant for real use.

## What's already loaded

- 1,209 leads (deduplicated by mobile number across all source sheets)
- 6,761 follow-up history records, reconstructed from the daily date-column call logs
- Master data: source categories, lead sources, ~20 courses, an 11-stage pipeline
- `ambiguous_values_report.csv` (project root) — 816 values the migration could not confidently
  map (unrecognized course spellings, ambiguous origin text, unparseable lead-state values).
  Nothing was discarded or guessed silently — review this file and correct records in the CRM
  as needed.

To re-run the migration from scratch (e.g. after editing the cleaning rules in
`imports/cleaning.py`):

```bash
python manage.py migrate_excel_data "ZA Leads Data Till June 2026.xlsx" "Academy Leads for Jul-Aug 2026.xlsx"
```

## Key decisions made during Excel analysis

- **"AD" as a lone origin value** (369/562 rows in the master sheet) was confirmed by you to mean
  mixed/unspecified Meta + Google paid ads — mapped to `Digital Marketing / Paid Ads -
  Unspecified`. WhatsApp is always kept as its own distinct source, never folded into "AD".
- **`Counselling` sheet** (ZA workbook) was excluded — it's staff/counsellor hiring data, not
  student leads.
- Several sheets (`Total Leads`, `Positive Leads Jan-May 2026`, `New Jan Leads-26`, `Most Recent`,
  `Dec-Jan Leads status`, `Medium Cold`, `Positive`, `Avg positive`, `Daily updates`) were skipped
  as primary sources because they are >90% duplicate, status-filtered views of the sheets already
  processed — importing them again would just re-process the same leads. Full reasoning is in the
  docstring at the top of `leads/management/commands/migrate_excel_data.py`.

## What's built (MVP scope)

- Lead Management: list/search/filter, add/edit, duplicate detection (by mobile), bulk
  assign/stage/archive, full attribution (source category, lead source, campaign, UTM, referral) with
  **original attribution frozen at creation** and never overwritten by later edits
- Follow-up Management: Today / Upcoming / Overdue boards, follow-up history, auto-updated
  lead timeline
- Lead Timeline: every stage change, assignment, follow-up, note, admission, and payment is logged
  automatically (see `leads/signals.py`, `followups/signals.py`)
- Excel Management: guided import wizard (upload → pick sheet → map columns → preview → import,
  with automatic historical follow-up reconstruction from date-headed columns), import history,
  filtered export to `.xlsx`
- Admissions & Payments, with revenue/pending tracking
- Dashboard: KPI cards + Chart.js charts (source distribution, stage funnel, monthly trend,
  course-wise); Source / Campaign / Employee performance reports
- Settings: manage Source Categories, Lead Sources, Campaigns, Courses, Lead Stages
- Users & Roles (Super Admin / Admin / Manager / Counsellor / Viewer), Audit Log

## What's NOT built yet (roadmap)

The original spec (60 sections) describes a multi-month production system. This MVP intentionally
does not include, in rough priority order:

1. **DRF REST API** — no `/api/` endpoints yet; everything is server-rendered Django views.
2. **Celery + Redis** — background jobs (large imports, scheduled reminders) currently run
   synchronously in the request. Fine at this data volume; add Celery before imports exceed a
   few thousand rows at once or before wiring up reminder notifications.
3. **Granular RBAC** — role checks exist (`user.can_assign_leads`, `can_manage_masters`, etc.) but
   permissions aren't yet enforced per-object (e.g. a Counsellor can currently see all leads, not
   just their own — restrict querysets in `leads/views.py` by `assigned_to=request.user` if you
   want that).
4. **In-app/email/WhatsApp notifications** — no notification system yet.
5. **Automated test suite** — none written; the flows above were manually verified via Django's
   test client during this build (login, CRUD, import wizard end-to-end, admission→payment,
   bulk actions, user management, audit log).
6. **AI features** (lead scoring, next-action suggestions) — explicitly out of scope until the
   core CRM is stable, per the original brief.
7. **Production deployment** (Nginx, Gunicorn process manager, HTTPS, MySQL) — not wired up in
   this environment; see below for how to do it yourself.

## Moving to MySQL / production

1. Install MySQL and create a database: `CREATE DATABASE leadcrm CHARACTER SET utf8mb4;`
2. `pip install mysqlclient` (needs `libmysqlclient-dev` / `default-libmysqlclient-dev` system package)
3. Copy `.env.example` to `.env`, fill in `SECRET_KEY`, set `USE_MYSQL=1`, `DEBUG=0`, and the `DB_*` vars
4. Export those variables into your shell (or use a systemd `EnvironmentFile=`) and run:
   ```bash
   python manage.py migrate
   python manage.py seed_masters
   python manage.py createsuperuser
   python manage.py collectstatic
   ```
5. Run behind Gunicorn + Nginx as usual for Django (`gunicorn config.wsgi:application`), with Nginx
   serving `/static/` and `/media/` and proxying everything else, HTTPS via Let's Encrypt.

## Project structure

```
leadcrm/
├── config/           settings, urls
├── accounts/         custom User model + roles, login, user management
├── leads/            Lead + all master data (sources, campaigns, courses, stages), views, signals
├── followups/        FollowUp, Note, Activity (timeline)
├── imports/          Excel import wizard + cleaning/normalization rules (imports/cleaning.py)
├── admissions/        Admission
├── payments/         Payment
├── dashboard/        KPIs, charts, source/campaign/employee reports
├── audit/            AuditLog + middleware that attributes changes to the logged-in user
├── templates/         all HTML — each template is self-contained (inline <style>/<script>,
│                      no separate .css/.js files), extends templates/base.html
└── leads/management/commands/
    ├── seed_masters.py        seed default source categories, courses, stages
    └── migrate_excel_data.py  the one-off historical data loader described above
```

## Everyday admin tasks

- Django admin (power-user data editing / bulk fixes): `/django-admin/`
- Reset a user's password: `python manage.py changepassword <username>`
- Re-check for duplicate leads any time: **Lead Management → Duplicate Management**
