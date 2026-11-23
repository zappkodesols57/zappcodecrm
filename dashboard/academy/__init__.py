"""
Academy Module Dashboard Views Package
Exports:
- academy_admin_home: Dedicated analytics dashboard for Academy Admin & Managers
- academy_counselor_home: Dedicated counselor & student enrollment dashboard
"""

from .admin_views import (
    academy_admin_home,
)

from .counselor_views import (
    academy_counselor_home,
)

__all__ = [
    "academy_admin_home",
    "academy_counselor_home",
]
