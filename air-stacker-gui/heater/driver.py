"""Omega Platinum Modbus-RTU client.

References (in `docs/`):
  - omega-platinum-m5458-modbus.pdf  (register map + enums)
  - omega-platinum-m5451-user-manual.pdf  (front-panel menus, OPER modes)
"""

from __future__ import annotations

import threading

import minimalmodbus
from serial import Serial

from . import registers as R
from .enums import OutputMode, ProcessMode, SetpointMode, SystemState


def _require_serial(inst: minimalmodbus.Instrument) -> Serial:
    """Narrow `inst.serial` to non-None.

    minimalmodbus types Instrument.serial as Optional but always wires
    it up at construction. Centralizing the narrow stops type checkers
    from tripping over every per-attribute access.
    """
    s = inst.serial
    if s is None:
        raise RuntimeError("minimalmodbus instrument has no serial port")
    return s


class OmegaPlatinum:
    """Modbus-RTU client for an Omega Platinum CN/DP controller."""

    def __init__(
        self,
        port: str,
        baud: int = 19200,
        slave_id: int = 1,
        timeout: float = 0.5,
    ) -> None:
        self.port = port
        self.baud = baud
        self.slave_id = slave_id
        self.timeout = timeout
        self._inst: minimalmodbus.Instrument | None = None
        self._lock = threading.Lock()

    def open(self) -> None:
        if self._inst is not None:
            return
        inst = minimalmodbus.Instrument(self.port, self.slave_id, mode=minimalmodbus.MODE_RTU)
        serial = _require_serial(inst)
        serial.baudrate = self.baud
        serial.bytesize = 8
        serial.parity = "N"
        serial.stopbits = 1
        serial.timeout = self.timeout
        inst.clear_buffers_before_each_transaction = True
        self._inst = inst

    def close(self) -> None:
        with self._lock:
            if self._inst is not None:
                try:
                    _require_serial(self._inst).close()
                finally:
                    self._inst = None

    # --- low-level typed access ---

    def _require(self) -> minimalmodbus.Instrument:
        if self._inst is None:
            raise RuntimeError("port not open")
        return self._inst

    def read(self, reg: R.Register) -> int | float:
        with self._lock:
            inst = self._require()
            if reg.width is R.Width.F:
                return inst.read_float(reg.addr, functioncode=3, number_of_registers=2)
            if reg.width is R.Width.L:
                return inst.read_long(reg.addr, functioncode=3)
            return inst.read_register(reg.addr, functioncode=3)

    def write(self, reg: R.Register, value: int | float) -> None:
        with self._lock:
            inst = self._require()
            if reg.width is R.Width.F:
                inst.write_float(reg.addr, float(value), number_of_registers=2)
            elif reg.width is R.Width.L:
                inst.write_long(reg.addr, int(value))
            else:
                inst.write_register(reg.addr, int(value), functioncode=6)

    # --- public reads ---

    def process_value(self) -> float:
        return float(self.read(R.PV))

    def setpoint(self) -> float:
        """Read configured ABSOLUTE_SETPOINT_1 (0x02E2).

        Matches the write target in ABSOLUTE mode, so round-trips with
        set_setpoint(). 0x0220 (CURRENT_SETPOINT_1) mirrors this value
        in ABSOLUTE mode but is exposed via diagnose() rather than here.
        """
        return float(self.read(R.ABSOLUTE_SETPOINT_1))

    def control_setpoint(self) -> float:
        return float(self.read(R.CONTROL_SETPOINT))

    def output_percent(self) -> float:
        return float(self.read(R.PID_OUTPUT))

    def output_limit_high(self) -> float:
        return float(self.read(R.PID_PERCENT_HIGH))

    def system_state(self) -> SystemState:
        return SystemState(int(self.read(R.RUN_MODE)))

    def system_status(self) -> int:
        return int(self.read(R.SYSTEM_STATUS))

    def setpoint_mode(self) -> SetpointMode:
        return SetpointMode(int(self.read(R.SETPOINT_1_MODE)))

    def output_mode(self) -> OutputMode:
        return OutputMode(int(self.read(R.OUTPUT_1_MODE)))

    def process_mode(self) -> ProcessMode:
        return ProcessMode(int(self.read(R.PROCESS_SCALE_ENABLE)))

    # --- public writes ---

    def set_setpoint(self, value: float) -> None:
        """Write SP1, dispatching to the right register by SETPOINT_1_MODE.

        In ABSOLUTE mode (the only one we have a USB capture for), the
        active SP source is ABSOLUTE_SETPOINT_1 (0x02E2). Writes to the
        working register CURRENT_SETPOINT_1 (0x0220) are silently dropped
        in this mode. Other modes (DEVIATION, REMOTE, EXTERNAL,
        RAMP_SOAK) raise NotImplementedError until we capture how
        Configurator handles them.
        """
        mode = self.setpoint_mode()
        if mode is SetpointMode.ABSOLUTE:
            self.write(R.ABSOLUTE_SETPOINT_1, value)
        else:
            raise NotImplementedError(
                f"setpoint write in {mode.name} mode not supported"
            )

    def set_output_limit_high(self, value: float) -> None:
        """Write PID_PERCENT_HIGH (0x02AC), the upper PID output clamp."""
        self.write(R.PID_PERCENT_HIGH, value)

    def set_run_mode(self, state: SystemState) -> None:
        """Write a SystemState value to RUN_MODE (0x0240).

        RUN_MODE is symmetric: write SystemState `N`, read SystemState
        `N` back (verified by USB capture of the Omega Configurator).
        """
        self.write(R.RUN_MODE, int(state))

    def run(self) -> None:
        """Engage PID control (SystemState.RUN = 6)."""
        self.set_run_mode(SystemState.RUN)

    def stop(self) -> None:
        """Halt control output (SystemState.STOP = 8)."""
        self.set_run_mode(SystemState.STOP)
