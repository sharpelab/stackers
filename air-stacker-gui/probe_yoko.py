"""Quick reachability check for the Yokogawa 7651 piezo driver on GPIB.

The 7651 is the fine-Z piezo source for the Air Stacker. It is a
pre-SCPI vintage Yokogawa instrument:

  - No ``*IDN?`` — set parameters cannot be queried back; the canonical
    driver in ``~/sharpelab/measurement-env`` caches them in software.
  - Commands are terminated with ``;`` (not LF); each write must include
    its own ``;``.
  - Replies are terminated with ``\\r``.
  - ``OD;`` is a non-perturbing read of the current output. Reply format::

        NDCV+0.11402E+02
        ^^^ ^^ ^ ^^^^^^^^^^^^
        |   |  |   `--- mantissa·E·exponent (volts or amps)
        |   |  `------ V / A function (V = voltage mode)
        |   `--------- DC (the 7651 only does DC)
        `------------- N normal · E overload (per IM 7651-01E §6.2.4)

Usage::

    uv run python probe_yoko.py                  # defaults to GPIB0::29::INSTR
    uv run python probe_yoko.py GPIB0::5::INSTR

Exit code: 0 on parsed reply, 1 on no/unparseable reply, 2 on resource error.
"""

from __future__ import annotations

import re
import sys


_OD_RE = re.compile(
    r"^(?P<status>[NE])DC(?P<func>[VA])(?P<value>[+-]?\d+\.\d+E[+-]?\d+)"
)

_STATUS_LABEL = {"N": "normal", "E": "overload"}


def probe(resource: str) -> int:
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


def main(argv: list[str]) -> int:
    resource = argv[1] if len(argv) > 1 else "GPIB0::29::INSTR"
    return probe(resource)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
