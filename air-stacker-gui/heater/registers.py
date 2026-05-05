"""Omega Platinum Modbus holding-register catalog (M5458 §3).

Single source of truth for register addresses + widths. All other modules
in this package reference these symbols; do not hand-encode addresses.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Width(str, Enum):
    """Register data width per M5458 §2.2."""

    R = "R"  # 16-bit single register
    L = "L"  # 32-bit (two consecutive registers, MSB first)
    F = "F"  # IEEE float (two consecutive registers, sign+exp first)


@dataclass(frozen=True)
class Register:
    addr: int
    mnemonic: str
    width: Width
    nv: bool = False  # writes to NV must be ≥500 ms apart, ≤10/sec (M5458 §3.1)
    note: str = ""


# --- Process / setpoint / output ---

PV = Register(
    0x0210, "CURRENT_INPUT_VALUE", Width.F,
    note="primary input scaled value (process value)",
)
SETPOINT_1 = Register(
    0x0220, "CURRENT_SETPOINT_1", Width.F,
    note="working SP1; runtime write target — never write the NV ABSOLUTE_SETPOINT_1",
)
CONTROL_SETPOINT = Register(
    0x0224, "CONTROL_SETPOINT", Width.F,
    note="setpoint actually used in PID calculations (read for diagnose cross-check)",
)
PID_OUTPUT = Register(
    0x022A, "PID_OUTPUT", Width.F,
    note="PID output 0..100%; manual marks R but Configurator may write here for M.CNt",
)

# --- Run / mode control ---

RUN_MODE = Register(
    0x0240, "RUN_MODE", Width.R,
    note="writes Control enum; reads SystemState enum (asymmetric, M5458 §3.2.1)",
)
SYSTEM_STATUS = Register(
    0x0204, "SYSTEM_STATUS", Width.L,
    note="32-bit enumerated status word; richer than RUN_MODE read",
)
PROCESS_SCALE_ENABLE = Register(
    0x0245, "PROCESS_SCALE_ENABLE", Width.R,
    note="input-side LIVE/MANUAL (M.INP); not the output-side M.CNt hold",
)

# --- Configuration (NV — read at startup, do not write at runtime) ---

SETPOINT_1_MODE = Register(0x02E0, "SETPOINT_1_MODE", Width.R, nv=True)
ABSOLUTE_SETPOINT_1 = Register(
    0x02E2, "ABSOLUTE_SETPOINT_1", Width.F, nv=True,
    note="configured SP1 in NV memory — DO NOT write at runtime",
)
OUTPUT_1_MODE = Register(0x0401, "OUTPUT_1_MODE", Width.R, nv=True)
PID_P = Register(0x02A4, "PID_P", Width.F, nv=True)
PID_I = Register(0x02A6, "PID_I", Width.F, nv=True)
PID_D = Register(0x02A8, "PID_D", Width.F, nv=True)
PID_PERCENT_LOW = Register(0x02AA, "PID_PERCENT_LOW", Width.F, nv=True)
PID_PERCENT_HIGH = Register(0x02AC, "PID_PERCENT_HIGH", Width.F, nv=True)
