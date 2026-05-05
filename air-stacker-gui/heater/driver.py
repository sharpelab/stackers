"""Omega Platinum Modbus-RTU client.

References (in `docs/`):
  - omega-platinum-m5458-modbus.pdf  (register map + enums)
  - omega-platinum-m5451-user-manual.pdf  (front-panel menus, OPER modes)
"""

from __future__ import annotations

import threading

import minimalmodbus

from . import registers as R
from .enums import Control, OutputMode, ProcessMode, SetpointMode, SystemState


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
        inst.serial.baudrate = self.baud
        inst.serial.bytesize = 8
        inst.serial.parity = "N"
        inst.serial.stopbits = 1
        inst.serial.timeout = self.timeout
        inst.clear_buffers_before_each_transaction = True
        self._inst = inst

    def close(self) -> None:
        with self._lock:
            if self._inst is not None:
                try:
                    self._inst.serial.close()
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
        return float(self.read(R.SETPOINT_1))

    def control_setpoint(self) -> float:
        return float(self.read(R.CONTROL_SETPOINT))

    def output_percent(self) -> float:
        return float(self.read(R.PID_OUTPUT))

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
        """Write the volatile working SP1 (CURRENT_SETPOINT_1, 0x0220).

        Never writes the NV ABSOLUTE_SETPOINT_1 — per the manual, NV registers
        should only be written during configuration.
        """
        self.write(R.SETPOINT_1, value)

    def set_control(self, command: Control) -> None:
        """Low-level RUN_MODE write. Use run()/stop() for the common cases."""
        self.write(R.RUN_MODE, int(command))

    def run(self) -> None:
        """Engage continuous PID control (Control.CONTINUOUS = 4)."""
        self.set_control(Control.CONTINUOUS)

    def stop(self) -> None:
        """Halt control output (Control.STOP = 0)."""
        self.set_control(Control.STOP)
