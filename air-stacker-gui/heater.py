"""Omega Platinum series heater controller (Modbus RTU over USB-CDC).

Register addresses match Omega's Platinum Series Communication Manual (M5451).
32-bit floats span two consecutive holding registers in big-endian word order.
"""

from __future__ import annotations

import threading

import minimalmodbus

# Holding-register addresses (decimal). Each FLOAT occupies <addr> and <addr+1>.
REG_PV = 528          # 0x0210 — process value (current temperature)
REG_SP1 = 544         # 0x0220 — setpoint 1
REG_STATUS = 540      # 0x021C — system status bitfield (16-bit int)
REG_RUN_MODE = 542    # 0x021E — run mode (16-bit int; 0 = STOP, 1 = RUN, …)

RUN_MODE_LABELS = {
    0: "STOP",
    1: "RUN",
    2: "PAUSE",
    3: "WAIT",
    4: "TUNE",
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
        return self._read_float(REG_SP1)

    def status(self) -> int:
        return self._read_int(REG_STATUS)

    def run_mode(self) -> int:
        return self._read_int(REG_RUN_MODE)

    # --- writes (use with intent) ---
    def set_setpoint(self, value: float) -> None:
        self._write_float(REG_SP1, value)
