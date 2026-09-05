"""Dump the SMC100's runtime configuration — read-only.

Queries firmware, stage ID, state, position, soft limits, encoder unit,
motion parameters, backlash/hysteresis compensation, following-error
limit, and keypad enable state. Doesn't change anything on the
controller — pure information gathering.

Most queries work in any state (NOT REFERENCED, READY, MOVING, JOGGING,
DISABLE), so this is safe to run mid-experiment provided no other process
is holding the COM port.

Usage:
    uv run python probe_smc100_config.py            # defaults to COM5 @ 57600
    uv run python probe_smc100_config.py COM3
    uv run python probe_smc100_config.py COM5 57600

Exit code: 0 on success (even if individual queries return errors;
those get inlined as "err: ..." in the output), 2 if the port itself
won't open.
"""

from __future__ import annotations

import sys

from smc100 import SMC100Axis, SMC100Error, error_label, state_label


def _query(axis: SMC100Axis, cmd: str) -> str:
    """Query, or return a 'err: ...' marker on failure."""
    try:
        return axis.query(cmd)
    except SMC100Error as e:
        return f"err: {e}"


def _section(name: str) -> None:
    print()
    print(name)


def main(argv: list[str]) -> int:
    port = argv[1] if len(argv) > 1 else "COM5"
    baud = int(argv[2]) if len(argv) > 2 else 57600

    print(f"SMC100 configuration probe — {port} @ {baud} 8N1, addr 1")

    axis = SMC100Axis(port=port, baud=baud)
    try:
        axis.open()
    except Exception as e:  # noqa: BLE001
        print(f"Couldn't open port: {e}")
        return 2

    try:
        _section("Identification")
        print(f"  Firmware    : {_query(axis, 'VE?')}")
        print(f"  Stage ID    : {_query(axis, 'ID?')}")

        _section("State")
        try:
            sc, ec = axis.state()
            print(f"  State code  : {sc}  ({state_label(sc)})")
            print(f"  Error reg   : {ec}  ({error_label(ec)})")
        except SMC100Error as e:
            print(f"  err: {e}")

        _section("Position")
        print(f"  TP (current): {_query(axis, 'TP?')} mm")
        print(f"  TH (target) : {_query(axis, 'TH?')} mm")

        _section("Soft limits (ESP-loaded)")
        print(f"  SL (lo)     : {_query(axis, 'SL?')} mm")
        print(f"  SR (hi)     : {_query(axis, 'SR?')} mm")

        _section("Encoder")
        su_raw = _query(axis, "SU?")
        try:
            su_val = float(su_raw)
            print(f"  SU          : {su_val:.3e} mm/count  (≈ {su_val * 1e6:.2f} nm)")
        except ValueError:
            print(f"  SU          : {su_raw}")

        _section("Motion parameters")
        print(f"  VA          : {_query(axis, 'VA?')} mm/s   (max velocity)")
        print(f"  AC          : {_query(axis, 'AC?')} mm/s²  (acceleration)")
        print(f"  OH          : {_query(axis, 'OH?')} mm/s   (homing velocity)")
        print(f"  HT          : {_query(axis, 'HT?')}        (home search type)")

        _section("Compensation")
        print(f"  BA          : {_query(axis, 'BA?')} mm     (backlash)")
        print(f"  BH          : {_query(axis, 'BH?')} mm     (hysteresis)")

        _section("Following error")
        print(f"  FE          : {_query(axis, 'FE?')} mm     (limit)")

        _section("Keypad")
        jm_raw = _query(axis, "JM?")
        if jm_raw == "1":
            print("  JM          : 1  (keypad enabled — default at boot)")
        elif jm_raw == "0":
            print("  JM          : 0  (keypad disabled)")
        else:
            print(f"  JM          : {jm_raw}")

    finally:
        axis.close()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
