"""Keithley 617 reachability + protocol diagnostic.

The 617 sits on GPIB at primary address 29 and reads current through
the piezo (see ``docs/air-stacker-pc.md``). Pre-SCPI vintage Keithley:

  - No ``*IDN?`` — sending one sets the IDDC error bit.
  - Commands are short letter codes; ``X`` is the execute command.
  - Talker's default output is the live measurement at ~3 Hz.

Usage::

    uv run python probe_keithley617.py                    # one reading
    uv run python probe_keithley617.py --status           # U0/U1/U2
    uv run python probe_keithley617.py --zero             # run zero
    uv run python probe_keithley617.py GPIB0::5::INSTR    # custom resource

Default resource is ``GPIB0::29::INSTR``. ``--status`` and the bare
read are non-perturbing; ``--zero`` toggles Zero Check + Zero Correct
to capture the unit's offset — safe but it briefly disconnects the
input (Zero Check shorts the input internally).

NOTE: close ``air-stacker-gui`` before running ``--zero``; the panel's
poll thread will compete for the bus.

Exit codes: 0 = success, 1 = reading parse error, 2 = open / resource
error.
"""

from __future__ import annotations

import sys
import time


def probe(resource: str) -> int:
    """Minimal reachability — open, read one measurement."""
    print(f"Probing {resource}…")
    try:
        from keithley617 import Keithley617, Keithley617Error, format_engineering
    except ImportError as e:
        print(f"  keithley617 import failed: {e}")
        return 2

    k = Keithley617(resource)
    try:
        k.open()
    except Exception as e:  # noqa: BLE001
        print(f"  open failed: {type(e).__name__}: {e}")
        return 2

    try:
        r = k.read()
    except Keithley617Error as e:
        print(f"  read failed: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"  read failed: {type(e).__name__}: {e}")
        return 1
    finally:
        k.close()

    print(
        f"  reading: status={r.status_label}, function={r.function}, "
        f"value={format_engineering(r.value, r.unit)}"
    )
    return 0


def status(resource: str) -> int:
    """Read U0 / U1 / U2 status words."""
    print(f"Status of {resource}…")
    from keithley617 import Keithley617

    k = Keithley617(resource)
    k.open()
    try:
        print(f"  U0 machine: {k.read_machine_status()!r}")
        print(f"  U1 error:   {k.read_error_status()!r}")
        print(f"  U2 data:    {k.read_data_status()!r}")
    finally:
        k.close()
    return 0


def zero(resource: str) -> int:
    """Run the canonical zero-verification sequence and verify."""
    print(f"Zeroing {resource}…")
    from keithley617 import Keithley617, format_engineering

    k = Keithley617(resource)
    k.open()
    try:
        print("  before:", k.read())
        print("  running C1X → Z1X → C0X with 1 s settles…")
        k.zero(settle_s=1.0)
        time.sleep(1.0)
        r = k.read()
        print(
            f"  after:  status={r.status_label}, function={r.function}, "
            f"value={format_engineering(r.value, r.unit)}"
        )
    finally:
        k.close()
    return 0


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    do_status = False
    do_zero = False
    if "--status" in args:
        do_status = True
        args.remove("--status")
    if "--zero" in args:
        do_zero = True
        args.remove("--zero")
    resource = args[0] if args else "GPIB0::29::INSTR"
    if do_zero:
        return zero(resource)
    if do_status:
        return status(resource)
    return probe(resource)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
