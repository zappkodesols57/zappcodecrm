"""
SuperAdmin Global Analytics Views Package
Exports:
- superadmin_home: Global executive analytics across all tenants & industries
- nel_card_drilldown_api, live_metrics_api, nelson_module_view, management_home, roles_permissions_view, placeholder_view
"""

from .superadmin_views import (
    superadmin_home,
    nel_card_drilldown_api,
    live_metrics_api,
    nelson_module_view,
    management_home,
    roles_permissions_view,
    placeholder_view,
)

__all__ = [
    "superadmin_home",
    "nel_card_drilldown_api",
    "live_metrics_api",
    "nelson_module_view",
    "management_home",
    "roles_permissions_view",
    "placeholder_view",
]
