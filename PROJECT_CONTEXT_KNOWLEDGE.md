# ZappCode CRM - Nelson Hospital Project Knowledge & Architecture Context

## 1. Overview & Data Provenance
- **Hospital Context**: Nelson Hospital Dhantoli & Nelson Luxe Mother & Child Care Hospital, Wardhmannagar (Multi-tenant Hospital CRM).
- **Core Dataset**: Cleaned dataset in `cleaned_nelson_final.xlsx` (1,293 leads), cleaned from `uncleaned_nelson_data.xlsx`.
- **Created Timestamp**: Every lead has an exact `created_at` timestamp combining `Date` (Inquiry Date) and `Lead Received Time`.

---

## 2. Lead Lifecycle, Status & Dynamic Remark Logic

### **Status Column (`display_status` / `custom_deal_status`):**
1. **Payment Done**: Total billed amount > 0 or deal status is `WON`.
2. **Booked**: Appointment status contains `Booked`, `Confirmed`, or `YES`.
3. **Payment Pending**: Visit / appointment completed (`Complete`, `Done`, `Visited`), but billing is unrecorded / 0.
4. **New**: Unassigned leads (`assigned_to` is null) with no calling notes/status update.
5. **Open**: Active leads assigned to attendants under follow-up.
6. **Lost / Cancelled**: Marked as lost or cancelled.
7. **Awaiting Approval**: Appointment status is awaiting doctor approval.

### **Remark Column (`remark_detail`):**
1. **Payment Done**: Displays total amount in currency format (e.g., `₹47,226`).
2. **Booked**: Displays appointment booked date (e.g., `2026-06-25` or `Slot Scheduled`).
3. **Payment Pending**: Displays `Billing Pending`.
4. **New**: Displays `New Enquiry`.
5. **Open Leads Dynamic Temperature Progression**:
   - **Hot**: Attendant assigned, but no calling remark added yet.
   - **Warm**: Remark 1 is `Call Not Received`, `Call Cut`, `Not Reachable`, or `Ringing`.
   - **Cold**: Remark 2 is also `Call Not Received` / unanswered.
   - **Freeze**: Remark 3 is also `Call Not Received` / unanswered.
   - **Specific Note**: If a discussion note is present (e.g., `Basic Information`, `Call Back for Confirmation`), it displays that note.
6. **Lost / Cancelled**: Displays cancellation reason or latest remark.

---

## 3. Hospital Masters & Configuration Mapping
- **Hierarchy**: `HospitalBranch` -> `HospitalDepartment` -> `HospitalDoctor` <-> `HospitalDisease`
- **Total Departments**: 21 (Pediatrics, Gynaecology, Neurology, Orthopedics, Cardiology, etc.)
- **Total Doctors**: 41 (linked with departments and branch availability)
- **Total Diseases / Medical Conditions**: 111 (mapped to their respective Departments and Doctors for dynamic cascading dropdowns)
- **Script**: `sync_hospital_masters.py`

---

## 4. Admin Dashboard KPIs
- **Header**: Standardized to `Dashboard`.
- **Primary KPIs**:
  1. **Today's New Leads**: Unassigned/untouched new leads created today + Walk-in leads + Organic leads with no campaign.
  2. **Appointments Booked**: Count of booked appointments.
- **Removed from Dashboard**: Conversion rate, Total Revenue, and Detailed Billing collapsible cards.

---

## 5. Billing Configuration
- **Hospital Billing Fields**:
  - `OPD Bill`
  - `IPD Bill`
  - `Pharmacy Bill`
  - `Investigation Bill`
  - `Total Bill` = $\text{OPD} + \text{IPD} + \text{Pharmacy} + \text{Investigation}$
