# Heater driver handoff

Pause point on the Air Stacker heater panel. The current
`heater.py` is patched into a working state for setpoint + run/stop, but
the controller still won't produce output ("OPER manual" on the front
panel display, output stuck at 0%). Decision: stop swatting individual
register bugs and rewrite the driver properly when picked back up.

Reference: [Omega Platinum M5458 Modbus Interface manual](../docs/omega-platinum-m5458-modbus.pdf)
(now committed in `docs/`).

## What works

- `REG_PV` (0x0210) — process value reads ✓
- `REG_SP1_CURRENT` (0x0220) — active SP1 read ✓
- `REG_SP1_ABSOLUTE` (0x02E2) — SP1 *write* target ✓ (writes to the
  volatile working copy at 0x0220 alone get stomped each loop; the
  controller refreshes it from the NV absolute value)
- `REG_PID_OUTPUT` (0x022A) — output % read ✓
- `REG_RUN_MODE` (0x0240) — run/stop writes ✓
- UI: setpoint auto-applies on `editingFinished`, Run/Stop buttons
  separate, no Set button.

## What's broken

Controller boots in OPER + manual override. Output % is held at 0
regardless of SP. None of the writes we tried to RUN_MODE flip it
out of manual:

- `START (1)` → state shows IDLE (or AUTO_ON label echoes back), no output
- `AUTO_ON (3)` → state shows AUTO_ON / 6, no output
- `STOP (0)` → STOP, no output (expected)

Steven's note: this is a manual-output hold, separate from RUN/STOP,
toggleable from the front panel on the old Platinum software. We have
not yet found the Modbus register (or magic command sequence) that
clears it.

## Asymmetric `RUN_MODE` reads

Discovered late: reads from `RUN_MODE` (0x0240) sometimes return the
**Control** enum we wrote (0=STOP, 1=START, 3=AUTO_ON) and sometimes
return values from the **System State** enum (e.g. 6=RUN). We've been
flipping the label table back and forth without a clean answer. The
right move is probably to read **`SYSTEM_STATUS`** (0x0204, 32-bit L)
for the displayed state and treat `RUN_MODE` strictly as a control
write target. (`SYSTEM_STATUS` enum is in M5458 §3.2.1.)

## Things to try when picking it back up

1. **Read `SYSTEM_STATUS` (0x0204) for display state**, leave
   `RUN_MODE` write-only in the API. This drops the dual-enum mess.
2. **`PROCESS_SCALE_ENABLE` (0x0245)** — values are LIVE_MODE (0) /
   MANUAL_MODE (1). Description is about *input* scaling but worth
   reading and (carefully) toggling — possibly the hook for the
   manual-vs-live behavior we're seeing.
3. **Read `OUTPUT_1_MODE` (0x0401)** — confirm it's `PID (1)` not
   `OFF (0)`. We added `OmegaPlatinum.output_1_mode()` for this.
4. **Read `SETPOINT_1_MODE` (0x02E0)** — confirm it's `ABSOLUTE (0)`
   so writes to `ABSOLUTE_SETPOINT_1` actually drive PID. Helper
   already exists: `OmegaPlatinum.setpoint_mode()`.
5. **Capture USB serial traffic from the old Platinum software**
   when the user clicks its Manual/Auto toggle. That's the cheapest
   way to find the magic register/sequence — point a serial sniffer
   at the USB-CDC port (or run the app in a VM with logging).
6. **Talk to Steven** for the front-panel key combo and any tribal
   knowledge about the old software's manual-toggle implementation.

## Direction for the rewrite

`heater.py` has accreted comment-on-comment-of-corrections. When picking
back up, suggest a clean redo:

- Single source of truth for register definitions (a dataclass per
  register: address, name, type, RW, NV).
- Separate read API (`status()` returns `SystemState`) from write API
  (`start()`, `stop()`, `set_sp(...)`).
- Explicit handling of NV writes (rate limit, optional verify-readback).
- A `diagnose()` that dumps SP mode, output mode, system status, run
  mode, PV, SP, output% in one shot — for troubleshooting like this.
- Probably move it into its own package (`air_stacker_gui/heater/`)
  with manual transcripts from the M5458 PDF as docstrings on the
  enum classes.

## Recent commits in flight

```
9d06869 heater: use AUTO_ON on Run to bypass manual override
3a68b7f heater: revert run-mode labels to Control enum
0862e4a heater: auto-apply setpoint, add Run button, expand state labels
646d036 heater: correct register map per M5458 manual
34f422d heater: Set enters RUN, add Stop button
552487c add PID output % readout to heater panel
a86f44b add Omega Platinum heater driver and panel
```
