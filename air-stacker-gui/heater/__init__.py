"""Omega Platinum heater driver.

Manuals in `docs/`:
  - omega-platinum-m5458-modbus.pdf  (register map + enums)
  - omega-platinum-m5451-user-manual.pdf  (front-panel menus, OPER modes)
"""

from .diagnose import Diagnosis, diagnose
from .driver import OmegaPlatinum
from .enums import Control, OutputMode, ProcessMode, SetpointMode, SystemState

__all__ = [
    "Control",
    "Diagnosis",
    "OmegaPlatinum",
    "OutputMode",
    "ProcessMode",
    "SetpointMode",
    "SystemState",
    "diagnose",
]
