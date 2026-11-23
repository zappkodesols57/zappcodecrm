"""
Hospital Module Dashboard Views Package
Exports:
- hospital_admin_home: Dedicated analytics dashboard for Hospital Admin & Managers
- nel_card_drilldown_api: KPI drilldown card API for Hospital Admin
- telecaller_home: Dedicated dashboard for Hospital Lead Attendants & Telecallers
- telecaller_tab_data_api, telecaller_search, telecaller_appointments, telecaller_my_leads, etc.
- doctor_home: Dedicated dashboard for Hospital Doctors
- doctor_appointments, doctor_patient_review
"""

from .admin_views import (
    hospital_admin_home,
    nel_card_drilldown_api,
)

from .telecaller_views import (
    telecaller_home,
    telecaller_tab_data_api,
    telecaller_search,
    telecaller_appointments,
    telecaller_my_leads,
    telecaller_new_enquiries,
    telecaller_today_team_activity,
)

from .doctor_views import (
    doctor_home,
    doctor_appointments,
    doctor_patient_review,
)

__all__ = [
    "hospital_admin_home",
    "nel_card_drilldown_api",
    "telecaller_home",
    "telecaller_tab_data_api",
    "telecaller_search",
    "telecaller_appointments",
    "telecaller_my_leads",
    "telecaller_new_enquiries",
    "telecaller_today_team_activity",
    "doctor_home",
    "doctor_appointments",
    "doctor_patient_review",
]
