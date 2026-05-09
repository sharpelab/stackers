"""Yokogawa 7651 reachability + protocol diagnostic.

The 7651 is the fine-Z piezo source for the Air Stacker. It is a
pre-SCPI vintage Yokogawa instrument:

  - No ``*IDN?`` — set parameters cannot be queried back; the canonical
    driver in ``yoko.py`` caches them in software.
  - Commands are terminated with ``;`` (not LF); each write must include
    its own ``;``.
  - Replies are terminated with ``\\r`` (CR/LF default per DL0).
  - ``OD;`` is a non-perturbing read of the current output.
  - ``OC;`` returns ``STS1=<n>`` — a status byte where bit 3 = "previous
    communication command error" and bit 5 = "output ON". Querying after
    a write tells us whether the unit accepted the command.
  - ``OS;`` returns a panel-setting summary (raw, used for inspection).

Usage::

    uv run python probe_yoko.py                      # OD-only reachability
    uv run python probe_yoko.py --diagnose           # full protocol walk
    uv run python probe_yoko.py GPIB0::5::INSTR      # custom resource

Default resource is ``GPIB0::29::INSTR``. The ``--diagnose`` walk asks
before doing anything that would change the unit's output level. The
non-disruptive first step writes the live OD value back via ``SA<v>;E;``,
so if the unit accepts SA at all you'll see ``last_cmd_err=0`` after.

NOTE: the air-stacker-gui must be CLOSED while running --diagnose, since
it would compete for the GPIB resource.

Exit codes: 0 = success, 1 = command error / unparseable reply,
2 = resource error / not message-based.
"""

from __future__ import annotations

import re
import sys
import time


_OD_RE = re.compile(
    r"^(?P<status>[NE])DC(?P<func>[VA])(?P<value>[+-]?\d+\.\d+E[+-]?\d+)"
)

_STATUS_LABEL = {"N": "normal", "E": "overload"}


def probe(resource: str) -> int:
    """Minimal reachability check — opens the resource and reads OD;."""
    print(f"Probing {resource}…")
    try:
        import pyvisa
        from pyvisa.resources import MessageBasedResource
    except ImportError as e:
        print(f"  pyvisa not installed: {e}")
        return 2

    try:
        rm = pyvisa.ResourceManager()
        inst = rm.open_resource(resource)
    except Exception as e:
        print(f"  resource error: {type(e).__name__}: {e}")
        return 2

    if not isinstance(inst, MessageBasedResource):
        print(f"  resource {resource!r} is not message-based")
        inst.close()
        return 2

    inst.timeout = 1500
    inst.write_termination = ""  # 7651 commands carry their own ';'
    inst.read_termination = "\r"

    try:
        resp = inst.query("OD;").strip()
    except Exception as e:
        print(f"  query error: {type(e).__name__}: {e}")
        return 1
    finally:
        try:
            inst.close()
        except Exception:
            pass

    if not resp:
        print("  no response")
        return 1

    print(f"  OD; -> {resp!r}")

    m = _OD_RE.match(resp)
    if not m:
        print("  (could not parse — unexpected reply format)")
        return 1

    unit = "V" if m["func"] == "V" else "A"
    value = float(m["value"])
    status = _STATUS_LABEL.get(m["status"], m["status"])
    print(f"  decoded: status={status}, mode=DC{unit}, output={value:+.6f} {unit}")
    print("Connected.")
    return 0


def _report(label: str, yoko) -> None:
    """Print OC + OS + OD snapshot under a label. Each query is independent
    so a failure on one doesn't blow up the others."""
    from yoko import YokoError  # noqa: F401  (re-export not strictly needed)

    try:
        sb = yoko.read_status_code()
        flags = []
        if sb.output_on:
            flags.append("OUTPUT_ON")
        if sb.output_unstable:
            flags.append("UNSTABLE")
        if sb.last_cmd_err:
            flags.append("LAST_CMD_ERR")
        if sb.program_running:
            flags.append("PROG_RUNNING")
        if sb.program_setting:
            flags.append("PROG_SETTING")
        if sb.cal_mode:
            flags.append("CAL_MODE")
        if sb.ic_card_in:
            flags.append("IC_CARD_IN")
        if sb.cal_switch:
            flags.append("CAL_SWITCH")
        print(f"  [{label}] OC raw={sb.raw} flags={'|'.join(flags) or '-'}")
    except Exception as e:
        print(f"  [{label}] OC error: {type(e).__name__}: {e}")
    try:
        os_str = yoko.read_panel_settings()
        print(f"  [{label}] OS {os_str!r}")
    except Exception as e:
        print(f"  [{label}] OS error: {type(e).__name__}: {e}")
    try:
        r = yoko.read_output()
        print(f"  [{label}] OD {r.value:+.6f} {r.function} ({r.status_label})")
    except Exception as e:
        print(f"  [{label}] OD error: {type(e).__name__}: {e}")


def diagnose(resource: str) -> int:
    """Walk through F1/SA/E with OC checks at each step.

    Step 1 is non-disruptive: programs SA to the live OD value (no output
    movement). If the unit accepts SA at all, OC after step 1 will show
    last_cmd_err=0. Anything beyond step 1 prompts before changing levels.
    """
    from yoko import Yoko7651, YokoError

    print(f"Diagnosing {resource}…")
    print("(Make sure air-stacker-gui is closed — it'll fight for the GPIB.)")
    yoko = Yoko7651(resource)
    try:
        yoko.open()
    except Exception as e:
        print(f"  open failed: {type(e).__name__}: {e}")
        return 2

    try:
        _report("initial", yoko)

        try:
            r = yoko.read_output()
        except YokoError as e:
            print(f"  OD read failed: {e}")
            return 1
        if r.function != "V":
            print(f"  unit is in {r.function} mode, not V. Aborting.")
            return 1

        print(f"\n--- step 1 (non-disruptive): SA{r.value:+.4f};E (= live OD) ---")
        try:
            yoko.set_voltage(r.value)
        except YokoError as e:
            print(f"  set_voltage raised: {e}")
            return 1
        time.sleep(0.3)
        _report("after SA(live)", yoko)
        print(
            "  ^ if last_cmd_err=0 here, the unit IS accepting our SA;E pair.\n"
            "    if last_cmd_err=1, we have a syntax / range / mode problem."
        )

        ans = input(
            "\nProceed with disruptive test (±0.05 V wiggle)? [y/N] "
        ).strip().lower()
        if ans != "y":
            return 0

        target = r.value + 0.05
        print(f"\n--- step 2: SA{target:+.4f};E (+0.05 V wiggle) ---")
        try:
            yoko.set_voltage(target)
        except YokoError as e:
            print(f"  set_voltage raised: {e}")
            return 1
        time.sleep(0.5)
        _report(f"after SA{target:+.4f}", yoko)

        print(f"\n--- step 3: SA{r.value:+.4f};E (back to start) ---")
        try:
            yoko.set_voltage(r.value)
        except YokoError as e:
            print(f"  set_voltage raised: {e}")
            return 1
        time.sleep(0.5)
        _report("after step-back", yoko)

        return 0
    finally:
        yoko.close()


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    do_diagnose = False
    if "--diagnose" in args:
        do_diagnose = True
        args.remove("--diagnose")
    resource = args[0] if args else "GPIB0::29::INSTR"
    if do_diagnose:
        return diagnose(resource)
    return probe(resource)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
