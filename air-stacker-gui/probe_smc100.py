"""Quick reachability check for a Newport SMC100 on a serial port.

The SMC100 master speaks ASCII over RS-232C at 57600 8N1; addressed
commands are prefixed with the controller index (default `1`) and
terminated with CRLF. `1VE?` returns the firmware version and doesn't
change controller state, so it's safe to use as a probe.

Usage:
    uv run python probe_smc100.py            # defaults to COM5 @ 57600
    uv run python probe_smc100.py COM3
    uv run python probe_smc100.py COM5 19200

Exit code: 0 if the SMC100 replies, 1 if silent, 2 if the port itself
won't open.
"""

from __future__ import annotations

import sys
import time

import serial


def probe(port: str, baud: int = 57600, addr: int = 1) -> int:
    print(f"Probing {port} @ {baud} 8N1, address {addr}…")
    try:
        ser = serial.Serial(port, baud, timeout=0.5)
    except serial.SerialException as e:
        print(f"  port error: {e}")
        return 2

    with ser:
        ser.reset_input_buffer()
        ser.write(f"{addr}VE?\r\n".encode())
        time.sleep(0.2)
        resp = ser.read_all().decode(errors="replace").strip()

    if resp:
        print(f"  {addr}VE? -> {resp}")
        print("Connected.")
        return 0

    print("No response.\n")
    print("Things to check:")
    print("  - Power LED on the controller is on")
    print("  - DIP-switch address is 1 (single-controller setup)")
    print("  - Cable: Newport's stock PC cable is wired for DCE-style; a")
    print("    generic DB9 + USB-RS232 dongle (both DTE) needs a null-modem")
    print("    adapter to swap TX/RX")
    print("  - Baud: SMC100 default is 57600 8N1; try 19200/115200 if the")
    print("    controller was reconfigured")
    return 1


def main(argv: list[str]) -> int:
    port = argv[1] if len(argv) > 1 else "COM5"
    baud = int(argv[2]) if len(argv) > 2 else 57600
    return probe(port, baud)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
