# Heater driver handoff

Driver rewritten as the `heater/` package (was `heater.py`). The remaining
known-broken behaviour is the front-panel **Manual Mode (M.CNt)** override
holding output at 0%.

References committed in `docs/`:
- [M5458 Modbus interface](../../docs/omega-platinum-m5458-modbus.pdf) — register map + enums
- [M5451 controller user guide](../../docs/omega-platinum-m5451-user-manual.pdf) — front-panel menus, OPER modes (M5451 §6.4 documents M.CNt / M.INP)

## Package layout

```
heater/
  __init__.py    # public API: OmegaPlatinum, Control, SystemState, diagnose, ...
  registers.py   # Register dataclass + every register we touch (single source of truth)
  enums.py       # Control, SystemState, SetpointMode, OutputMode, ProcessMode
  driver.py      # OmegaPlatinum class
  diagnose.py    # one-shot state snapshot
```

Key API decisions:
- **Setpoint writes go to `CURRENT_SETPOINT_1` (0x0220) only.** Never the NV
  `ABSOLUTE_SETPOINT_1` (0x02E2) — per M5458 §3.1 NV registers should only
  be written during configuration.
- **`run()` writes `Control.CONTINUOUS` (4)** — "continuously (repeatedly)
  enabled" reads as steady-state PID, vs `START` (1) / `AUTO_ON` (3) which
  read as one-shot triggers.
- **Reads of `RUN_MODE` (0x0240) decode as `SystemState`**, not `Control`.
  M5458 §3.2.1 defines both enums; the register is asymmetric (write Control,
  read SystemState). The `SystemState.STANDBY` (7) and `PAUSE` (9) values
  are diagnostic gold — both suppress output while the controller is
  technically "in OPER".
- **Low-level `set_control(Control)`** is exposed so we can A/B test other
  enum values from a REPL without code edits.
- **`Diag` button** in the heater panel calls `diagnose()` and prints to
  stdout: PV, SP, control_setpoint, output%, system_state, system_status,
  setpoint_mode, output_mode, process_mode — one shot, one place.

## Open: M.CNt manual-output hold

The front panel "OPER manual" hold at boot is M5451 §6.4 Manual Mode →
M.CNt ("manually vary the control output(s)"). The official Platinum
Configurator GUI exposes this as a button, so it's reachable via *some*
protocol — we just haven't found the register.

What we've ruled out:
- `PROCESS_SCALE_ENABLE` (0x0245) LIVE/MANUAL is **input-side** (M.INP),
  not output-side (M.CNt). M5458 §3.2.4 + register description.
- M5458 has no register named or described as "manual output mode".

Things to try next (in order of cheapness):
1. **`Diag` button while controller is in M.CNt** — capture system_state,
   system_status, output_mode. Will tell us whether the hold maps to a
   known enum value (e.g. STANDBY=7) and is clearable via `set_control()`.
2. **Try writing `PID_OUTPUT` (0x022A) directly.** Manual marks it R but
   the Configurator might write here for M.CNt — read-only labels in
   Omega manuals have been wrong before.
3. **USB-CDC sniff** the Platinum Configurator while clicking Manual/Auto
   — `wireshark + usbmon` on Linux, or Free Serial Port Monitor on the
   Windows box. Cheapest source-of-truth.
4. **Front-panel EXIT** clears M.CNt (M5451 §6.4 last line). Combined with
   `SAFETY_DELAYED_OPER_RUN` (0x02C1) defaulting to *return to last OPER
   mode at power-on*, the controller boots back into M.CNt unless EXIT
   was pressed before power-down. Documenting as the manual-recovery path.

## Recent commits in flight

```
66acb9e heater: stash manual + handoff before driver rewrite
9d06869 heater: use AUTO_ON on Run to bypass manual override
3a68b7f heater: revert run-mode labels to Control enum
0862e4a heater: auto-apply setpoint, add Run button, expand state labels
646d036 heater: correct register map per M5458 manual
```
