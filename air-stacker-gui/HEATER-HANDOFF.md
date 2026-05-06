# Heater driver handoff

Driver lives as the `heater/` package (was `heater.py`). A USB capture of
the Omega Platinum Configurator on 2026-05-06 resolved the prior "stuck
at 0%" mystery and **overturned several assumptions baked into this doc**;
see "What the capture proved" below before changing the driver.

References (in `docs/`):
- [M5458 Modbus interface](../../docs/omega-platinum-m5458-modbus.pdf) — register map + enums
- [M5451 controller user guide](../../docs/omega-platinum-m5451-user-manual.pdf) — front-panel menus, OPER modes (M5451 §6.4 documents M.CNt / M.INP)

Capture file: `~/Desktop/heater_usb_capture.pcapng` on the air-stacker PC.
USBPcap, link type 249, slave ID 1, device VID 0x2a72 PID 0x0400.

## Package layout

```
heater/
  __init__.py    # public API: OmegaPlatinum, SystemState, diagnose, ...
  registers.py   # Register dataclass + every register we touch
  enums.py       # SystemState, SetpointMode, OutputMode, ProcessMode
                 #   (Control enum slated for removal — see findings)
  driver.py      # OmegaPlatinum class
  diagnose.py    # one-shot state snapshot
```

## What the USB capture proved (2026-05-06)

Captured Configurator's full session: connect, edit SP twice, press Run,
press Stop. Three findings supersede the previous "Key API decisions":

1. **Run = `WRITE 0x0240 = 6`. Stop = `WRITE 0x0240 = 8`.**
   Configurator writes SystemState values directly to RUN_MODE. The
   "Control" enum (STOP=0, START=1, CANCEL=2, AUTO_ON=3, CONTINUOUS=4)
   we encoded from M5458 §3.2.1 — and the asymmetric-register theory
   built on it — does not match observed protocol. The register behaves
   symmetrically: write SystemState value `N`, read back SystemState
   value `N`. Our `run()` was writing 4 (CONTINUOUS) and the readback of
   4 was decoding as `SystemState.MODIFY`, which is why the controller
   sat in setup-edit mode rather than OPER RUN. Either the M5458 Control
   enum belongs to a different register we never identified, or it was
   a misread of the spec — TBD on a re-read of §3.2.1.

2. **Setpoint writes go to `ABSOLUTE_SETPOINT_1` (0x02E2)**, not
   `CURRENT_SETPOINT_1` (0x0220). In `SETPOINT_1_MODE = ABSOLUTE` (the
   default in this controller's config), 0x02E2 is the active SP source;
   writes to 0x0220 are silently dropped. The "never write NV" rule was
   over-strict — Configurator writes 0x02E2 freely at human-typing rate.
   M5458 §3.1's NV constraints (≥500 ms between writes, ≤10/sec) still
   apply, but `editingFinished` semantics already meet them.

   `setpoint()` reads of 0x0220 returned the right value because in
   ABSOLUTE mode 0x0220 mirrors the active SP for reads — that's why
   the diag dump always *looked* correct after a Configurator-issued
   change.

3. **No M.CNt-clearing register is needed.** The "stuck at 0%" was
   purely our wrong Run command. Once `0x0240 = 6` is written, PID
   engages, OUT follows error toward SP — that's it. The capture shows
   no toggling of any register related to M.CNt. `PROCESS_SCALE_ENABLE =
   MANUAL` (which `diagnose()` still surfaces) is a red herring; PID
   runs fine with that register reading MANUAL.

## Driver state after the capture-driven cleanup

- `run()` writes 6 (`SystemState.RUN`); `stop()` writes 8
  (`SystemState.STOP`). Both go through `set_run_mode(SystemState)`,
  which is the new low-level API for RUN_MODE.
- `set_setpoint()` dispatches on `SETPOINT_1_MODE`. ABSOLUTE writes go
  to 0x02E2; other modes raise `NotImplementedError` until we have a
  capture for them. `setpoint()` reads 0x02E2 to round-trip cleanly.
- `Control` enum and `set_control()` deleted — the §3.2.1 Control enum
  doesn't bind to RUN_MODE; it applies to "Write 1 to ..." trigger
  registers we don't currently use.
- `Register.nv` flag deleted (it was doc-only and inaccurate as a
  "do-not-write" marker now that we write 0x02E2 freely).
- `diagnose()` snapshots Configurator's full polling set; spec-named
  registers get mnemonics, the rest stay as raw addresses
  (`UNKNOWN_0277`).
- `PROCESS_SCALE_ENABLE = MANUAL` is annotated in the summary as
  informational; it does not block PID.

## Configurator polling pattern (for reference)

Every ~2 s Configurator reads, in order:

```
0x0240 RUN_MODE                  (1 reg)
0x02E0 SETPOINT_1_MODE           (1 reg)
0x02E8 ?                         (1 reg)
0x0260 ?                         (1 reg)
0x0204 SYSTEM_STATUS             (2 regs, L)
0x022C ?                         (1 reg, returned 0x0001)
0x0210 PV                        (2 regs, float)
0x0214 ?                         (2 regs, float — read 0.39 in capture)
0x022A PID_OUTPUT                (2 regs, float)
0x0277 ?                         (2 regs, float — read 0.0)
0x0220 CURRENT_SETPOINT_1        (2 regs, float)
0x0222 ?                         (2 regs, float)
0x0226 ?                         (2 regs, float — read 167.6)
0x0228 ?                         (2 regs, float — read 18.55)
0x05E0, 0x0500, 0x0520           (1 reg each, returned 0)
0x0230..0x0235                   (1 reg each, returned 0)
0x021E, 0x0282, 0x028D           (1 reg each, returned 0)
```

The unannotated addresses are useful breadcrumbs if we ever need to
explore further (e.g. alarm state, autotune progress).

## Recent commits in flight

```
777e35a gui: add bottom-left FPS overlay on camera view
ff1841d conex: fix TS response parse, decode error register
8ddbfed gui: camera options panel, move heater right, set continuous capture
23855dc heater: rewrite as package, add controller user manual
66acb9e heater: stash manual + handoff before driver rewrite
9d06869 heater: use AUTO_ON on Run to bypass manual override   (superseded)
3a68b7f heater: revert run-mode labels to Control enum         (to be undone)
```
