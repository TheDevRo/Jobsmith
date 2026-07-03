"""
auto_apply/adapters — ATS-specific and generic form-filling adapters.

Each adapter implements the ATSAdapter protocol defined in base.py.
"""

from .base      import ATSAdapter
from .greenhouse import GreenhouseAdapter
from .lever      import LeverAdapter
from .linkedin   import LinkedInEasyApplyAdapter
from .workday    import WorkdayAdapter
from .indeed     import IndeedEasyApplyAdapter
from .adzuna     import AdzunaAdapter
from .ashby      import AshbyAdapter
from .generic    import GenericAdapter

# Ordered list used by the orchestrator — most-specific first, generic last.
ALL_ADAPTERS: list[ATSAdapter] = [
    GreenhouseAdapter(),
    LeverAdapter(),
    LinkedInEasyApplyAdapter(),
    WorkdayAdapter(),
    IndeedEasyApplyAdapter(),
    AdzunaAdapter(),
    AshbyAdapter(),
    GenericAdapter(),
]

__all__ = [
    "ATSAdapter",
    "GreenhouseAdapter",
    "LeverAdapter",
    "LinkedInEasyApplyAdapter",
    "WorkdayAdapter",
    "IndeedEasyApplyAdapter",
    "AdzunaAdapter",
    "AshbyAdapter",
    "GenericAdapter",
    "ALL_ADAPTERS",
]
