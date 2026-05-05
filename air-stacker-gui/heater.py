"""Omega Platinum series heater controller (Modbus RTU over USB-CDC).

Register addresses match Omega's Platinum Series Communication Manual (M5451).
32-bit floats span two consecutive holding registers in big-endian word order.
"""

from __future__ import annotations

import threading

import minimalmodbus

# Holding-register addresses (decimal). Floats occupy <addr> and <addr+1>.
# References from Omega Platinum Modbus Interface manual (M5458).
REG_PV = 528              # 0x0210 — CURRENT_INPUT_VALUE (process value, float)
REG_SP1_CURRENT = 544     # 0x0220 — CURRENT_SETPOINT_1 (active working copy, float)
REG_SP1_ABSOLUTE = 738    # 0x02E2 — ABSOLUTE_SETPOINT_1 (configured SP1 in NV, float)
REG_PID_OUTPUT = 554      # 0x022A — PID_OUTPUT (0..100%, float)
REG_RUN_MODE = 576        # 0x0240 — RUN_MODE (16-bit enum; writes Control, reads System State)
REG_SP1_MODE = 736        # 0x02E0 — SETPOINT_1_MODE (16-bit enum; 0=Absolute, 1=Deviation, …)
REG_OUTPUT1_MODE = 1025   # 0x0401 — OUTPUT_1_MODE (16-bit enum; 0=Off, 1=PID, 2=On/Off, …)

# RUN_MODE reads echo the Control enum we wrote (manual §3.2.1), not
# the broader System State enum (which lives at SYSTEM_STATUS / 0x0204).
RUN_MODE_LABELS = {
    0: "STOP",
    1: "RUN",
    2: "CANCEL",
    3: "AUTO_ON",
    4: "CONTINUOUS",
}

SETPOINT_MODE_LABELS = {
    0: "ABSOLUTE",
    1: "DEVIATION",
    2: "REMOTE",
    3: "EXTERNAL",
    4: "RAMP_SOAK",
}

OUTPUT_MODE_LABELS = {
    0: "OFF",
    1: "PID",
    2: "ON_OFF",
    3: "RETRANS",
    4: "ALARM_1",
    5: "ALARM_2",
    6: "RAMP_EVT",
    7: "SOAK_EVT",
}


def run_mode_label(code: int) -> str:
    return RUN_MODE_LABELS.get(code, f"UNKNOWN ({code})")


class OmegaPlatinum:
    """Thin Modbus-RTU client for an Omega Platinum controller."""

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

    def _read_float(self, register: int) -> float:
        with self._lock:
            return self._inst.read_float(register, functioncode=3, number_of_registers=2)

    def _read_int(self, register: int) -> int:
        with self._lock:
            return self._inst.read_register(register, functioncode=3)

    def _write_float(self, register: int, value: float) -> None:
        with self._lock:
            self._inst.write_float(register, value, number_of_registers=2)

    # --- safe reads ---
    def process_value(self) -> float:
        return self._read_float(REG_PV)

    def setpoint(self) -> float:
        return self._read_float(REG_SP1_CURRENT)

    def run_mode(self) -> int:
        return self._read_int(REG_RUN_MODE)

    def output(self) -> float:
        return self._read_float(REG_PID_OUTPUT)

    def setpoint_mode(self) -> int:
        return self._read_int(REG_SP1_MODE)

    def output_1_mode(self) -> int:
        return self._read_int(REG_OUTPUT1_MODE)

    # --- writes (use with intent) ---
    def set_setpoint(self, value: float) -> None:
        # ABSOLUTE_SETPOINT_1 lives in NV memory; the controller refreshes the
        # active working copy (CURRENT_SETPOINT_1) from it each loop, so writes
        # to the working copy alone get stomped.
        self._write_float(REG_SP1_ABSOLUTE, value)

    def set_run_mode(self, code: int) -> None:
        with self._lock:
            self._inst.write_register(REG_RUN_MODE, code, functioncode=6)
