"""Omega Platinum series enumerated values (M5458 §3.2)."""

from __future__ import annotations

from enum import IntEnum


class Control(IntEnum):
    """Commands written to RUN_MODE (0x0240). M5458 §3.2.1.

    Reads of the same register return SystemState values, not these.
    """

    STOP = 0
    START = 1
    CANCEL = 2
    AUTO_ON = 3
    CONTINUOUS = 4


class SystemState(IntEnum):
    """Decoded reads of RUN_MODE (0x0240). M5458 §3.2.1.

    STANDBY (7) and PAUSE (9) suppress output even when the controller
    appears "running" — useful flags when output is stuck at 0%.
    """

    LOAD = 0
    IDLE = 1
    INPUT_ADJUST = 2
    CONTROL_ADJUST = 3
    MODIFY = 4
    WAIT = 5
    RUN = 6
    STANDBY = 7
    STOP = 8
    PAUSE = 9
    FAULT = 10
    SHUTDOWN = 11
    AUTOTUNE = 12


class SetpointMode(IntEnum):
    """SETPOINT_1_MODE (0x02E0). M5458 §3.2.5."""

    ABSOLUTE = 0
    DEVIATION = 1
    REMOTE = 2
    EXTERNAL = 3
    RAMP_SOAK = 4


class OutputMode(IntEnum):
    """OUTPUT_1_MODE (0x0401). M5458 §3.2.7. PID is the value we want for control."""

    OFF = 0
    PID = 1
    ON_OFF = 2
    RETRANSMISSION = 3
    ALARM_1 = 4
    ALARM_2 = 5
    RAMP_EVENT = 6
    SOAK_EVENT = 7


class ProcessMode(IntEnum):
    """PROCESS_SCALE_ENABLE (0x0245). M5458 §3.2.4.

    Input-side live-vs-manual (M.INP). The output-side manual hold (M.CNt
    in M5451 §6.4) is not documented in M5458; finding it is open work.
    """

    LIVE = 0
    MANUAL = 1
