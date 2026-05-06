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
    note: str = ""


# --- Process / setpoint / output ---

PV = Register(
    0x0210, "CURRENT_INPUT_VALUE", Width.F,
    note="primary input scaled value (process value)",
)
REMOTE_SETPOINT_VALUE = Register(0x0214, "REMOTE_SETPOINT_VALUE", Width.F)
INPUT_DIGITAL = Register(0x021E, "INPUT_DIGITAL", Width.R, note="state of digital input pin")
SETPOINT_1 = Register(
    0x0220, "CURRENT_SETPOINT_1", Width.F,
    note="reads as working SP1 (mirrors active SP in ABSOLUTE mode); writes silently dropped",
)
CURRENT_SETPOINT_2 = Register(0x0222, "CURRENT_SETPOINT_2", Width.F)
CONTROL_SETPOINT = Register(
    0x0224, "CONTROL_SETPOINT", Width.F,
    note="setpoint actually used in PID calculations",
)
PEAK_VALUE = Register(0x0226, "PEAK_VALUE", Width.F, note="max PV processed since reset")
VALLEY_VALUE = Register(0x0228, "VALLEY_VALUE", Width.F, note="min PV processed since reset")
PID_OUTPUT = Register(0x022A, "PID_OUTPUT", Width.F, note="PID output 0..100%")
CURRENT_INPUT_VALID = Register(0x022C, "CURRENT_INPUT_VALID", Width.R, note="flag: PV valid")
ALARM_STATE_LOCAL = Register(0x022D, "ALARM_STATE", Width.R)
RAMP_SOAK_STATE = Register(0x022E, "RAMP_SOAK_STATE", Width.R)

OUTPUT_1_STATE = Register(0x0230, "OUTPUT_1_STATE", Width.R)
OUTPUT_2_STATE = Register(0x0231, "OUTPUT_2_STATE", Width.R)
OUTPUT_3_STATE = Register(0x0232, "OUTPUT_3_STATE", Width.R)
OUTPUT_4_STATE = Register(0x0233, "OUTPUT_4_STATE", Width.R)
OUTPUT_5_STATE = Register(0x0234, "OUTPUT_5_STATE", Width.R)
OUTPUT_6_STATE = Register(0x0235, "OUTPUT_6_STATE", Width.R)

# --- Run / mode control ---

RUN_MODE = Register(
    0x0240, "RUN_MODE", Width.R,
    note="reads & writes SystemState (M5458 §3.2.1); register is symmetric, "
         "Control enum from §3.2.1 does not bind here — verified by USB capture",
)
SYSTEM_STATUS = Register(
    0x0204, "SYSTEM_STATUS", Width.L,
    note="32-bit enumerated status word; richer than RUN_MODE read",
)
PROCESS_SCALE_ENABLE = Register(
    0x0245, "PROCESS_SCALE_ENABLE", Width.R,
    note="input-side LIVE/MANUAL (M.INP); MANUAL state does NOT block PID — "
         "capture showed PID running fine with this register reading MANUAL",
)

# --- Configuration / NV setpoint sources ---

SETPOINT_1_MODE = Register(0x02E0, "SETPOINT_1_MODE", Width.R)
ABSOLUTE_SETPOINT_1 = Register(
    0x02E2, "ABSOLUTE_SETPOINT_1", Width.F,
    note="active SP1 source in ABSOLUTE mode; primary write target. "
         "M5458 §3.1 NV constraints: ≥500 ms between writes, ≤10/sec",
)
SETPOINT_2_MODE = Register(0x02E8, "SETPOINT_2_MODE", Width.R)
RAMP_SOAK_MODE = Register(0x0260, "RAMP_SOAK_MODE", Width.R)
INPUT_SENSOR = Register(0x0282, "INPUT_SENSOR", Width.R)
TARE_MODE = Register(0x028D, "TARE_MODE", Width.R)

OUTPUT_1_MODE = Register(0x0401, "OUTPUT_1_MODE", Width.R)
PID_P = Register(0x02A4, "PID_P", Width.F)
PID_I = Register(0x02A6, "PID_I", Width.F)
PID_D = Register(0x02A8, "PID_D", Width.F)
PID_PERCENT_LOW = Register(0x02AA, "PID_PERCENT_LOW", Width.F)
PID_PERCENT_HIGH = Register(0x02AC, "PID_PERCENT_HIGH", Width.F)

# --- Alarm / annunciator (named per spec, not yet decoded) ---

DB_ANNUNCIATOR_STATE = Register(0x05E0, "DB_ANNUNCIATOR_STATE", Width.R)
ALARM_STATE_500 = Register(0x0500, "ALARM_STATE_500", Width.R, note="alarm group at 0x0500")
ALARM_STATE_520 = Register(0x0520, "ALARM_STATE_520", Width.R, note="alarm group at 0x0520")

# --- Configurator-polled but not in the M5458 register table ---

UNKNOWN_0277 = Register(0x0277, "UNKNOWN_0277", Width.F, note="polled by Configurator; not in M5458")
