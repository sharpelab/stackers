"""Probe the Yokogawa 7651's max ``set_voltage`` round-trip rate.

The Yoko's ``SA<v>;E;`` cycle is what bounds our ramp cadence — the
software loop in ``yoko_panel._RampWorker`` does ``set_voltage(); wait;``
in series, so the effective step time is ``hardware_floor + delay``. The
hardware floor is whatever the GPIB write + the unit's internal
processing takes for one SA + E pair.

This script measures it. Run with the air-stacker-gui closed (else the
poll thread competes for the bus and inflates the numbers).

Usage::

    uv run python probe_yoko_speed.py                    # 200 samples
    uv run python probe_yoko_speed.py 500                # custom N
    uv run python probe_yoko_speed.py 200 GPIB0::5::INSTR

The probe sends ``SA0;E;`` repeatedly — output state is unchanged, no
piezo motion regardless of whether the relay is engaged (programmed
level was already 0, or moves to 0 on the first call).
"""

from __future__ import annotations

import statistics
import sys
import time


def main(argv: list[str]) -> int:
    args = argv[1:]
    n = 200
    resource = "GPIB0::15::INSTR"
    if args and args[0].isdigit():
        n = int(args[0])
        args = args[1:]
    if args:
        resource = args[0]

    from yoko import Yoko7651

    print(f"Probing {resource} — {n} set_voltage(0.0) round-trips")
    yoko = Yoko7651(resource)
    yoko.open()
    try:
        # Warm-up — the first set_voltage also sends F1;E; to switch
        # into voltage mode, so its cost is unrepresentative. Burn a
        # few iterations to get past that.
        for _ in range(5):
            yoko.set_voltage(0.0)

        times_ms: list[float] = []
        for _ in range(n):
            t0 = time.perf_counter()
            yoko.set_voltage(0.0)
            times_ms.append((time.perf_counter() - t0) * 1000.0)
    finally:
        yoko.close()

    times_ms.sort()
    median = statistics.median(times_ms)
    print(f"  min:    {times_ms[0]:.2f} ms")
    print(f"  median: {median:.2f} ms")
    print(f"  mean:   {statistics.mean(times_ms):.2f} ms")
    print(f"  p95:    {times_ms[int(n * 0.95)]:.2f} ms")
    print(f"  p99:    {times_ms[int(n * 0.99)]:.2f} ms")
    print(f"  max:    {times_ms[-1]:.2f} ms")
    print(f"  hardware-max cadence (1 / median): {1000.0 / median:.1f} Hz")
    print()
    print(
        "Use this to choose ramp_step_v / ramp_delay_ms in config.toml — "
        "the effective ramp cycle is (median set_voltage time) + ramp_delay_ms, "
        "and V/s = ramp_step_v / cycle_seconds."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
