"""One-shot snapshot of heater state for troubleshooting.

`diagnose()` returns a `Diagnosis` carrying a wide register snapshot
(everything the Omega Configurator polls every 2 s, plus our own fixed
set). `summary()` renders the at-a-glance block we've always shown,
followed by a full register dump with mnemonics where the M5458 spec
names them and decoded enum values where applicable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import registers as R
from .driver import OmegaPlatinum
from .enums import OutputMode, ProcessMode, SetpointMode, SystemState

# Order matches Configurator's ~2 s poll cycle, plus a few we always want.
# Registers we don't have spec names for keep the raw-address mnemonic.
SNAPSHOT: tuple[R.Register, ...] = (
    R.SYSTEM_STATUS,
    R.PV,
    R.REMOTE_SETPOINT_VALUE,
    R.INPUT_DIGITAL,
    R.SETPOINT_1,
    R.CURRENT_SETPOINT_2,
    R.CONTROL_SETPOINT,
    R.PEAK_VALUE,
    R.VALLEY_VALUE,
    R.PID_OUTPUT,
    R.CURRENT_INPUT_VALID,
    R.ALARM_STATE_LOCAL,
    R.RAMP_SOAK_STATE,
    R.OUTPUT_1_STATE,
    R.OUTPUT_2_STATE,
    R.OUTPUT_3_STATE,
    R.OUTPUT_4_STATE,
    R.OUTPUT_5_STATE,
    R.OUTPUT_6_STATE,
    R.RUN_MODE,
    R.PROCESS_SCALE_ENABLE,
    R.RAMP_SOAK_MODE,
    R.UNKNOWN_0277,
    R.INPUT_SENSOR,
    R.TARE_MODE,
    R.SETPOINT_1_MODE,
    R.ABSOLUTE_SETPOINT_1,
    R.SETPOINT_2_MODE,
    R.OUTPUT_1_MODE,
    R.ALARM_STATE_500,
    R.ALARM_STATE_520,
    R.DB_ANNUNCIATOR_STATE,
)

# Registers whose value can be decoded into a known enum.
ENUM_DECODERS: dict[int, type] = {
    R.RUN_MODE.addr: SystemState,
    R.SETPOINT_1_MODE.addr: SetpointMode,
    R.OUTPUT_1_MODE.addr: OutputMode,
    R.PROCESS_SCALE_ENABLE.addr: ProcessMode,
}


@dataclass
class Diagnosis:
    process_value: float
    setpoint: float
    control_setpoint: float
    output_percent: float
    system_state: SystemState
    system_status: int
    setpoint_mode: SetpointMode
    output_mode: OutputMode
    process_mode: ProcessMode
    snapshot: list[tuple[R.Register, int | float | Exception]] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"PV={self.process_value:.2f}  "
            f"SP={self.setpoint:.2f} (control_sp={self.control_setpoint:.2f})  "
            f"OUT={self.output_percent:.1f}%",
            f"system_state = {self.system_state.name} ({int(self.system_state)})",
            f"system_status = 0x{self.system_status:08x}",
            f"setpoint_mode = {self.setpoint_mode.name}",
            f"output_mode   = {self.output_mode.name}",
            f"process_mode  = {self.process_mode.name}"
            f"  (informational; does not block PID)",
            "",
            "register dump (mnemonic where named in M5458):",
        ]
        for reg, value in self.snapshot:
            lines.append(_format_row(reg, value))
        return "\n".join(lines) + "\n"


def _format_row(reg: R.Register, value: int | float | Exception) -> str:
    addr = f"0x{reg.addr:04x}"
    mnem = reg.mnemonic.ljust(26)
    width = reg.width.value
    if isinstance(value, Exception):
        return f"  {addr}  {mnem} {width}  ERROR: {value}"
    if reg.width is R.Width.F:
        rendered = f"{float(value):.4f}"
    elif reg.width is R.Width.L:
        rendered = f"0x{int(value):08x}"
    else:
        rendered = f"0x{int(value):04x}"
    decoder = ENUM_DECODERS.get(reg.addr)
    if decoder is not None:
        try:
            rendered += f"  ({decoder(int(value)).name})"
        except ValueError:
            rendered += "  (unknown enum value)"
    return f"  {addr}  {mnem} {width}  {rendered}"


def diagnose(heater: OmegaPlatinum) -> Diagnosis:
    """Pull every state-relevant register in one shot.

    Per-register reads are caught individually so a single failure
    doesn't poison the whole dump.
    """
    snapshot: list[tuple[R.Register, int | float | Exception]] = []
    cache: dict[int, int | float] = {}
    for reg in SNAPSHOT:
        try:
            val = heater.read(reg)
            snapshot.append((reg, val))
            cache[reg.addr] = val
        except Exception as e:  # noqa: BLE001 — diag wants to keep going
            snapshot.append((reg, e))

    def _cached(reg: R.Register, fallback):
        return cache.get(reg.addr, fallback)

    return Diagnosis(
        process_value=float(_cached(R.PV, 0.0)),
        setpoint=float(_cached(R.SETPOINT_1, 0.0)),
        control_setpoint=float(_cached(R.CONTROL_SETPOINT, 0.0)),
        output_percent=float(_cached(R.PID_OUTPUT, 0.0)),
        system_state=SystemState(int(_cached(R.RUN_MODE, 0))),
        system_status=int(_cached(R.SYSTEM_STATUS, 0)),
        setpoint_mode=SetpointMode(int(_cached(R.SETPOINT_1_MODE, 0))),
        output_mode=OutputMode(int(_cached(R.OUTPUT_1_MODE, 0))),
        process_mode=ProcessMode(int(_cached(R.PROCESS_SCALE_ENABLE, 0))),
        snapshot=snapshot,
    )
