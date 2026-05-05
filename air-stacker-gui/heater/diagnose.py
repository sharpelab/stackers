"""One-shot snapshot of heater state for troubleshooting."""

from __future__ import annotations

from dataclasses import dataclass

from .driver import OmegaPlatinum
from .enums import OutputMode, ProcessMode, SetpointMode, SystemState


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

    def summary(self) -> str:
        return (
            f"PV={self.process_value:.2f}  "
            f"SP={self.setpoint:.2f} (control_sp={self.control_setpoint:.2f})  "
            f"OUT={self.output_percent:.1f}%\n"
            f"system_state = {self.system_state.name} ({int(self.system_state)})\n"
            f"system_status = 0x{self.system_status:08x}\n"
            f"setpoint_mode = {self.setpoint_mode.name}\n"
            f"output_mode   = {self.output_mode.name}\n"
            f"process_mode  = {self.process_mode.name}\n"
        )


def diagnose(heater: OmegaPlatinum) -> Diagnosis:
    """Pull every state-relevant register in one shot."""
    return Diagnosis(
        process_value=heater.process_value(),
        setpoint=heater.setpoint(),
        control_setpoint=heater.control_setpoint(),
        output_percent=heater.output_percent(),
        system_state=heater.system_state(),
        system_status=heater.system_status(),
        setpoint_mode=heater.setpoint_mode(),
        output_mode=heater.output_mode(),
        process_mode=heater.process_mode(),
    )
