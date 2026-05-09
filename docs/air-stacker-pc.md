# Air Stacker PC — Reference

Workstation that drives the **Air Stacker** (the simpler in-use stacker, **not** Ranger).

## Connectivity

- **Tailscale**: `air-stacker.tail737ca5.ts.net` (100.92.166.118), Windows, owned by `dgglab@`. *(Rename in the admin panel may still be pending — local hostname pref is set; if DNS still resolves only `stacker.tail737ca5.ts.net`, force-rename via https://login.tailscale.com/admin/machines.)*
- **SSH**: `ssh air-stacker` (alias defined in `~/.ssh/config`). User is `ddg_transfer` — note the typo'd username, two d's at the start. Key auth via `administrators_authorized_keys` (admin user).
- **RustDesk**: service running. Entry on the wiki: [RustDesk Addresses](https://wiki.sharpelab.science/doc/rustdesk-addresses-y3aYDSeXCw) ("Air Stacker" section).
- **VNC**: vncserver service also running.
- **Default SSH shell**: Git Bash (`C:\Program Files\Git\usr\bin\bash.exe`).

## Camera

- **Hardware**: **Flea3 FL3-U3-20E4C** (USB3, **2.0 MP** color Bayer, **1600×1200** native, ~59.5 fps cap). Sole connected camera. Camera node map still reports `DeviceVendorName = "Point Grey Research"` even though FLIR / Teledyne now own the line.
- **Pixel format**: default `BayerBG8`. Camera also exposes `BayerBG12p` / `BayerBG12Packed` / `BayerBG16` (higher bit depth), `Mono8` / `Mono12*` / `Mono16` (grayscale), `RGB8` (on-camera debayer), and several `YCbCr*` variants. Custom GUI uses `BayerBG8` and debayers host-side via OpenCV.
- **Binning**: 2×2 only. Set via `BinningVertical = 2` — the "vertical" name is misleading; on this Flea3 the horizontal node is read-only and slaves to the vertical, so writing 2 to `BinningVertical` gives 2×2 binning (1600×1200 → 800×600). No mode toggle (`BinningVerticalMode` is missing — camera does whatever it does, presumably averaging like-colored Bayer pixels). No 4× option, no decimation. Frame-rate cap stays at 59.47 Hz at both binning settings (sensor-readout-bound, not bandwidth-bound). Probe lives at [`air-stacker-gui/probe_binning.py`](../air-stacker-gui/probe_binning.py).
- **SDK**: FLIR / Teledyne Spinnaker **4.3** (rebranded from "FLIR Systems" → "Teledyne" in the 4.x install path: `C:\Program Files\Teledyne\Spinnaker\`).
- **PySpin**: vendored as a wheel under `air-stacker-gui/vendor/spinnaker_python-4.3.0.190-cp310-cp310-win_amd64.whl`, registered via `[tool.uv.sources]` in `pyproject.toml`. Pinned to Python 3.10 + numpy<2 (Spinnaker 4.3 ships only cp310 wheels and was built against the NumPy 1.x ABI).
- **GenTL producer**: `.cti` files at `C:\Program Files\Teledyne\Spinnaker\cti64\vs2015\` (the `GENICAM_GENTL64_PATH` env var points there). PySpin doesn't need this — only consumers like `harvesters` (no longer used in our GUI; SpinView still does).
- **Diagnostic GUIs**: `SpinView` at `bin64\vs2015\SpinView.exe` (replaces the old `SpinView_WPF.exe`). Useful for live exposure / WB / gamma sliders without launching our app.
- **Legacy / unused** Desktop shortcuts (ignore): FlyCap2 (`Camera.lnk`), IC Capture 2.5, Micro-Manager / ImageJ (`hist but slower fps.lnk`).

## Stage — CONEX-CC

- **Hardware**: Newport CONEX-CC dual-axis: **Z and spin** (not X+Z). Two USB-serial controllers.
- **Ports**: COM3 + COM4 (USB Serial Port). Firmware/driver string: `CONEX-CC 2.0.1`.
- **Existing front-ends on the box**:
  - Vendor GUI: `C:\Newport\Motion Control\CONEX-CC\Bin\64-bit\Newport.CONEXCC.StandAlone.exe` (Desktop: `Stage Controller.lnk`).
  - LabVIEW custom UI: `~/Desktop/CONEX-CC-GUI/` — git remote `https://github.com/caoyuan96421/CONEX-CC-GUI.git`. Likely the day-to-day tool. `.lvproj`/`.lvlps` LabVIEW project. Uses NI-VISA (NI MAX is installed).
- **Protocol**: ASCII serial (Newport CONEX-CC documented protocol). Easy to drive directly without LabVIEW.

## Z stage — SMC100CC (added 2026-05-08)

- **Hardware**: Newport **SMC100CC** (DC servo variant of the SMC100 Series Motion Controller / Driver), single-axis. Daisy-chain capable but the lab uses a single unit currently. Variant identified from `1VE?` reply (`SMC_CC`); the PP variant would identify as `SMC_PP`.
- **Firmware**: Controller-driver version **3.1.2**.
- **Stage**: Newport **LTA-HS** "high-speed" linear translation actuator (DC servo + rotary encoder, ESP-compatible — the SMC100 reads `ZX=3` and pulls config straight from the stage's smart-memory chip on connect). Reported via `1ID?` as `LTA-HS_PN:B0601246985103_UD:062509`. ESP-loaded soft limits **−0.1 mm to +50.1 mm** (`1SL`/`1SR`); encoder unit **3.539 × 10⁻⁵ mm ≈ 35.4 nm/count** (`1SU`). Default velocity 5 mm/s (`1VA`), acceleration 20 mm/s² (`1AC`), homing velocity 2.5 mm/s (`1OH`), home-search type 4 (`1HT`).
- **Safe operating envelope**:
  - **Positive cap: 30 mm (operational limit)** — must not exceed on this rig regardless of what the ESP-loaded `SR` says. Enforced in software only: the upcoming SMC100 driver/panel will take a `position_limits=` kwarg (parallel to `Yoko7651`'s `voltage_limits=`) and the air-stacker caller passes an upper bound of **30.0 mm**. **Do not push this cap to the controller persistently** (i.e. CONFIG-mode `1SR30` → `PW0`) without explicit per-action authorization — see global rule against persistent writes to lab controllers.
- **Connectors** (front face, photo: [`img/smc100.jpeg`](img/smc100.jpeg)): KEYPAD, **RS232C** (DB9), RS485 IN, RS485 OUT, CONFIG. Top face: MOTOR, GPIO, DC OUT, +48V power input.
- **PC link**: RS-232C → USB-RS232 adapter (FTDI VID_0403+PID_6001, unit `FTE75V52A`) → enumerates as **COM5**. Confirmed reachable on 2026-05-08; `1VE?` returns `1VE SMC_CC - Controller-driver version  3. 1. 2`.
- **Protocol**: ASCII over 57600 8N1; addressed commands prefixed with controller index (`1` for first/master), terminated CRLF. `1VE?` = firmware version, `1ID?` = stage ID, `1TS` = state, `1TP` = position, `1RS` = reset. Full command set in the [Command Interface Manual](../air-stacker-gui/manuals/newport-smc100-command-interface.pdf).
- **State machine** (printed on the box): `CONFIG` ↔ `AUTO CONFIG` → `NOT REFERENCED` → `HOMING` → `READY` ↔ `MOVING` / `JOGGING` / `DISABLE`. Faults: `ERROR FE`, `MM0/MM1 ERROR FE`, `HARDWARE FAULT`. Detailed in §5 (Programming) of the [User's Manual](../air-stacker-gui/manuals/newport-smc100-user-manual.pdf).
- **DIP switches** on the back select RS-232 master vs. RS-485 slave; first controller in the chain must be in RS-232 master mode for PC comms.
- **Probe script**: [`air-stacker-gui/probe_smc100.py`](../air-stacker-gui/probe_smc100.py) — sends `1VE?` and reports whether the controller replies. Useful for cabling iteration.
- **Cabling**: Newport's stock PC cable is wired DCE-style on the SMC100 end; a generic DB9 + USB-RS232 dongle (both DTE) needs a **null-modem adapter** to swap TX/RX. Current setup includes the null-modem and is working.
- **Manuals**: [User's Manual](../air-stacker-gui/manuals/newport-smc100-user-manual.pdf) (EDH0206En2060, 02/25 — install, wiring, state machine, ESP stage configuration), [Command Interface Manual](../air-stacker-gui/manuals/newport-smc100-command-interface.pdf) (EDH0311En1023, 12/21 — `Newport.SMC100.CommandInterface.dll` reference, but the ASCII command names match what goes over RS-232).
- **Known good positions**:
  - **Focus: ~27.691 mm** — recorded 2026-05-09 with the current sample stack. Use as a fall-back if focus is lost; expect ~µm-level corrections from the fine-Z piezo from there.

## Z stage — fine (Yokogawa 7651 + NPM140, added 2026-05-08)

- **Topology**: Yokogawa 7651 → Newport **NPM140** piezoelectric micrometer adapter, **direct drive — no amplifier in the loop**. Confirmed with Albert (who installed it).
- **Safe operating envelope**:
  - **Negative floor: −20 V (HARD limit)** — anything below damages the piezo. The 7651 can swing to −30 V on its own, so software *must* clamp at ≥ −20 V.
  - **Positive ceiling: +30 V** — set by the Yoko's own hardware ceiling. The NPM140 itself tolerates up to +130 V, but the Yoko can't reach it, so no overvoltage risk on the high side.
  - Useful Yoko swing on this load: **−20 V to +30 V (50 V)**, giving roughly **47 µm of fine-Z travel** (50 V × 140 µm / 150 V per the datasheet's full −20 → +130 V → 140 µm map).
  - `yoko.py`'s `Yoko7651` constructor takes a `voltage_limits=` kwarg; **piezo callers must pass `(-20.0, 30.0)`** so any `set_voltage` outside that range raises before hitting the bus.
- **Piezo — Newport NPM140** ([`newport-npm140-datasheet.pdf`](../air-stacker-gui/manuals/newport-npm140-datasheet.pdf)):
  - Travel: **140 µm ± 10 %** open-loop over the full −20 → +130 V range (we only see ~47 µm of it via the Yoko).
  - Resolution: 0.1 nm open-loop, rms-noise-limited.
  - Capacitance: **1.7 µF ± 20 %** — sets max dV/dt (current = C·dV/dt).
  - Resonant frequency: 670 Hz unloaded — keep ramps well below this period to avoid mechanical ringing.
  - Max axial load: 100 N. Stiffness: 0.4 N/µm.
  - Variants: NPM140 (open-loop, what we have), NPM140-D (XPS-driver bundle), NPM140SG (with strain-gauge), NPM140SG-D. No strain-gauge feedback in our setup.
  - Mounting: 0.375" (9.5 mm) shank, 22 mm setback from manual-actuator normal position.

### Source — Yokogawa 7651

- **Hardware**: Yokogawa **7651** programmable DC source. Sole GPIB instrument on the box.
- **PC link**: GPIB-USB adapter (vendor TBD — surfaces to NI-VISA as `GPIB0`) → NI-VISA → resource string **`GPIB0::29::INSTR`**. GPIB primary address **29** is set on DIP switches on the back of the 7651; if those get bumped, the device disappears from `pyvisa.ResourceManager().list_resources()` until reconfigured.
- **Currently observed (probe)**: ~+11.40 V DC, status `N` (normal), ~4 mV jitter on the readback. Probe captured on 2026-05-08 with the unit live on the piezo.
- **Protocol — pre-SCPI Yokogawa command set** (full reference: [`yokogawa-7651-user-manual.pdf`](../air-stacker-gui/manuals/yokogawa-7651-user-manual.pdf), IM 7651-01E §6 Communication Functions):
  - Commands are terminated with `;` (CR LF and bare LF are also accepted by the unit; we standardize on `;`). Configure VISA with `write_termination=""` and put the `;` in every command string.
  - Replies are terminated with **CR LF**. Use `read_termination='\n'` and strip residual `\r`.
  - **No `*IDN?`.** Sending `*IDN?` doesn't error, but the reply you read back is just whatever the talker has queued (the live OD-format output) — there is no real identification.
  - **Set commands are write-only** — programmed voltage / current / function / range / output state cannot be queried back. The driver caches them in software.
  - After any setting change you must send **`E;`** (or GPIB `<GET>`) to trigger / apply.
- **Common commands** (from the canonical driver):

  | Command | Meaning |
  |---|---|
  | `F1;` | Set DC voltage source mode |
  | `F5;` | Set DC current source mode |
  | `SA<value>;` | Set output level (e.g. `SA1.5;` → 1.5 V or 1.5 A depending on mode) |
  | `O1;` / `O0;` | Output enable / disable |
  | `E;` | Trigger — apply pending changes |
  | `OD;` | Read current output data (non-perturbing) |

- **OD reply format** (IM 7651-01E §6.2.4 Table 6.10): `NDCV+0.11402E+02` →
  - `N` status (`N` normal · `E` overload — only two values; the manual does not define a separate "error" letter)
  - `DC` mode (the 7651 is DC-only)
  - `V` function (`V` voltage / `A` current)
  - signed mantissa·E·exponent — for the example, +11.402 V

- **Operating range** (IM 7651-01E §1.1.1): voltage ranges 10 mV / 100 mV / 1 V / 10 V / 30 V (full output ±30 V); current ranges 1 mA / 10 mA / 100 mA (full output ±120 mA).
- **Driver**: [`air-stacker-gui/yoko.py`](../air-stacker-gui/yoko.py) — thin pyvisa wrapper. Methods: `open` / `close`, `read_output` (parses OD), `set_voltage` / `set_current` / `set_mode` / `set_output` / `reset`, `ramp_voltage`. Set commands write-only with software cache; `E;` trigger is sent automatically after every set.
- **Probe script**: [`air-stacker-gui/probe_yoko.py`](../air-stacker-gui/probe_yoko.py) — sends `OD;`, decodes the reply, reports the live output. Read-only and safe.
- **Reference driver** (different framework, same protocol): [`~/sharpelab/measurement-env/src/sharpelab_nb/drivers/yokogawa_7651.py`](../../measurement-env/src/sharpelab_nb/drivers/yokogawa_7651.py) — QCoDeS `VisaInstrument` subclass with the same cache-on-set semantics; useful sanity check when extending `yoko.py`.

## Heater

- **Hardware**: **Omega Engineering Platinum** series controller. Device ID `062BE937`, firmware `1.4.0.6`, run mode RUNNING (per the Configurator's Device Information panel).
- **Connection**: USB-CDC virtual COM via Omega's `OmegaVCP.inf` driver — currently enumerated as **COM7**.
- **Protocol**: Modbus RTU at 19200 8N1, slave ID 1 by default. 32-bit IEEE floats span two consecutive holding registers. Manuals in [`air-stacker-gui/manuals/`](../air-stacker-gui/manuals/): [M5458 Modbus interface](../air-stacker-gui/manuals/omega-platinum-m5458-modbus.pdf) (register map + enums) and [M5451 controller user guide](../air-stacker-gui/manuals/omega-platinum-m5451-user-manual.pdf) (front-panel menus, OPER modes including M.CNt manual output / M.INP manual input).
- **Software**: `~/Desktop/Platinum_Firmware_Software_1.4.0.6/` containing `EIP_1.1.5`, `Firmware_1.4.0.6`, `Platinum_Configurator_1.5.2.0`, `USBDriver`. Launcher on Desktop is `Temp Controller.appref-ms` (ClickOnce).
- The earlier "Thermo Scientific Platinum" label in this doc was wrong — it's Omega.

## Defunct / disabled

- The USBTMC SCPI temp/humidity instrument that `~/something.py` used to poll (`SENS1:TEMP:DATA?` / `SENS2:HUM:DATA?` → `spxtr.net/demgraphs/` as `stacker.temp`/`stacker.hum`) is **no longer connected** — pyvisa enumerates no USB instruments. The script and its `something.bat` companion are still on disk but inert.
- Scheduled task **`Wowza`** that re-launched `something.py` at login has been disabled (not deleted) on 2026-05-05.

## Serial port map (current snapshot)

| Port | Device | Purpose |
|------|--------|---------|
| COM1 | Communications Port (legacy) | Unused |
| COM3 | USB Serial Port (FTDI) | CONEX-CC axis A — currently disconnected |
| COM4 | USB Serial Port (FTDI) | CONEX-CC Rotation Stage |
| COM5 | USB Serial Port (FTDI) | Newport SMC100 motion controller (RS-232C) |
| COM7 | USB Serial Device (Omega VCP) | Heater (Omega Platinum, Modbus RTU) |

## Tooling on the box

- **Git Bash** at `C:\Program Files\Git\usr\bin\bash.exe` (default SSH shell).
- **Python 3.10.4** at `C:\Users\ddg_transfer\AppData\Local\Programs\Python\Python310\python.exe`.
- **uv 0.11.8** at `C:\Users\ddg_transfer\.local\bin\uv.exe` (installed 2026-05-04; PATH update pending shell restart).
- **Already pip-installed in user site**: PySide6 6.3.0, NumPy 1.22.3, OpenCV 4.5.5.64, Pillow 10.2.0, pyserial 3.5, pyvisa.
- **NI-VISA / NI MAX** installed.
- **Git** in `C:\Program Files\Git\`.

## Open questions / TBD

- Confirm which of COM3/COM4 is Z vs spin (from CONEX controller IDs or the LabVIEW VI's bindings).
- Add Air Stacker entry to the lab wiki (machine page, beyond the rustdesk listing).
