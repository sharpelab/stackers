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
- **GenTL producer**: `.cti` files at `C:\Program Files\Teledyne\Spinnaker\cti64\vs2015\` (the `GENICAM_GENTL64_PATH` env var points there). Our GUI uses PySpin directly and doesn't need GenTL — only consumers like `harvesters` (no longer used in our GUI) or SpinView do.
- **Diagnostic GUIs**: `SpinView` at `bin64\vs2015\SpinView.exe` (replaces the old `SpinView_WPF.exe`). Useful for live exposure / WB / gamma sliders without launching our app.
- **Legacy / unused** Desktop shortcuts (ignore): FlyCap2 (`Camera.lnk`), IC Capture 2.5, Micro-Manager / ImageJ (`hist but slower fps.lnk`).

## Secondary camera

- **Hardware**: **Anker PowerConf C200** USB-C UVC webcam. Used as a contextual view of the stage + front-panel instruments alongside the through-objective FLIR feed. Display-only — not part of the science data path.
- **USB enumeration**: `VID_291A`, `PID_3369`, single video interface (`MI_00`). PnP `FriendlyName = "Anker PowerConf C200"`. Enumerates under `Get-PnpDevice -Class Camera`.
- **Native resolutions & framerates** (probed 2026-05-10 via `QMediaDevices.videoInputs()`):

  | Resolution | NV12 / MJPEG | YUYV   |
  |------------|--------------|--------|
  | 2560×1440  | 30 fps       | —      |
  | 1920×1080  | 30 fps       | —      |
  | 1280×720   | 30 fps       | —      |
  |  640×480   | 30 fps       | 30 fps |
  |  640×360   | 30 fps       | 30 fps |
  |  320×240   | 30 fps       | 30 fps |

  `cv2.VideoCapture` + DSHOW forces YUYV and caps at ~18 fps above 1080p. Qt Multimedia / Media Foundation negotiates NV12 or MJPEG and sustains 30 fps at every supported resolution. GUI default: **1280×720 NV12 @ 30 fps**.

- **GUI integration**: opens via `PySide6.QtMultimedia` (`QCamera` + `QMediaCaptureSession` + `QVideoWidget`). Toggle lives on the bottom `StatusBar` of [`air-stacker-gui/main.py`](../air-stacker-gui/main.py); closed by default. Window class: [`webcam_window.py`](../air-stacker-gui/webcam_window.py). Config-parsing + device/format picking: [`webcam.py`](../air-stacker-gui/webcam.py). Design rationale: [`WEBCAM_PIP_PROPOSAL.md`](../air-stacker-gui/WEBCAM_PIP_PROPOSAL.md).

- **Standard UVC controls accessible** (typed in Qt Multimedia where noted): brightness, contrast, saturation, sharpness, gamma, hue, focus + autofocus (`QCamera.setFocusMode` / `setFocusDistance`), exposure + auto (`setExposureMode` / `setManualExposureTime`), white balance + auto (`setWhiteBalanceMode` / `setColorTemperature`), digital zoom (`setZoomFactor`), digital pan/tilt. Gain, backlight, iris, roll are not exposed on this model. AI tracking, HDR, anti-flicker, FoV preset are vendor UVC extension units (XU) — configurable only via the Anker Work utility, not via standard APIs. None of those vendor features are useful for our static-frame monitoring use case; already configured on the box and the GUI doesn't touch them.

- **DirectShow filter conflict**: index 1 under `cv2.CAP_DSHOW` enumerates the Spinnaker DirectShow filter for the FLIR Flea3 (Spinnaker installs one). Don't open by numeric index — pin by `QCameraDevice.id()` or by description substring match. Our GUI does the latter.

- **Probe scripts**: [`probe_webcam.py`](../air-stacker-gui/probe_webcam.py) (cv2 / DSHOW resolution × fps grid), [`probe_webcam_controls.py`](../air-stacker-gui/probe_webcam_controls.py) (cv2 CAP_PROP_* + Qt Multimedia format enumeration). Both read-only and safe to re-run.

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
- **Stage**: Newport **LTA-HS** "high-speed" linear translation actuator (DC servo + rotary encoder, ESP-compatible — the SMC100 reads `ZX=3` and pulls config straight from the stage's smart-memory chip on connect). Reported via `1ID?` as `LTA-HS_PN:B0601246985103_UD:062509`. ESP-loaded soft limits **−0.1 mm to +50.1 mm** (`1SL`/`1SR`); encoder unit **3.539 × 10⁻⁵ mm ≈ 35.4 nm/count** (`1SU`). Default velocity 5 mm/s (`1VA`), acceleration 20 mm/s² (`1AC`), homing velocity 2.5 mm/s (`1OH`), home-search type 4 (`1HT`). The LTA-HS shaft engages the microscope's focus-axis drive — photo: [`img/smc100-focus-drive-shaft.jpeg`](img/smc100-focus-drive-shaft.jpeg).
- **Safe operating envelope**:
  - **Positive cap: 30 mm (operational limit)** — must not exceed on this rig regardless of what the ESP-loaded `SR` says. Enforced in software only: the upcoming SMC100 driver/panel will take a `position_limits=` kwarg (parallel to `Yoko7651`'s `voltage_limits=`) and the air-stacker caller passes an upper bound of **30.0 mm**. **Do not push this cap to the controller persistently** (i.e. CONFIG-mode `1SR30` → `PW0`) without explicit per-action authorization — see global rule against persistent writes to lab controllers.
- **Connectors** (front face, photo: [`img/smc100.jpeg`](img/smc100.jpeg)): KEYPAD, **RS232C** (DB9), RS485 IN, RS485 OUT, CONFIG. Top face: MOTOR, GPIO, DC OUT, +48V power input.
- **PC link**: RS-232C → USB-RS232 adapter (FTDI VID_0403+PID_6001, unit `FTE75V52A`) → enumerates as **COM5**. Confirmed reachable on 2026-05-08; `1VE?` returns `1VE SMC_CC - Controller-driver version  3. 1. 2`.
- **Protocol**: ASCII over 57600 8N1; addressed commands prefixed with controller index (`1` for first/master), terminated CRLF. `1VE?` = firmware version, `1ID?` = stage ID, `1TS` = state, `1TP` = position, `1RS` = reset. Full command set in the [Command Interface Manual](../air-stacker-gui/manuals/newport-smc100-command-interface.pdf).
- **State machine** (printed on the box): `CONFIG` ↔ `AUTO CONFIG` → `NOT REFERENCED` → `HOMING` → `READY` ↔ `MOVING` / `JOGGING` / `DISABLE`. Faults: `ERROR FE`, `MM0/MM1 ERROR FE`, `HARDWARE FAULT`. Detailed in §5 (Programming) of the [User's Manual](../air-stacker-gui/manuals/newport-smc100-user-manual.pdf).
- **DIP switches** on the back select RS-232 master vs. RS-485 slave; first controller in the chain must be in RS-232 master mode for PC comms.
- **Compensation** (probed 2026-05-09, see [`probe_smc100_config.py`](../air-stacker-gui/probe_smc100_config.py)):
  - **`BA` (backlash) = 0 mm** — disabled.
  - **`BH` (hysteresis) = 0 mm** — disabled.
  - Both configurable via the `BA` / `BH` ASCII commands, but only in CONFIGURATION state (`PW1` to enter) and only persisted with `PW0` (flash write, ~10 s). Per the global rule against persistent controller writes, changing these requires explicit per-action authorization.
- **Probe scripts**:
  - [`air-stacker-gui/probe_smc100.py`](../air-stacker-gui/probe_smc100.py) — sends `1VE?` and reports whether the controller replies. Useful for cabling iteration.
  - [`air-stacker-gui/probe_smc100_config.py`](../air-stacker-gui/probe_smc100_config.py) — read-only dump of firmware, stage ID, state, position, soft limits, encoder unit, motion params, BA/BH compensation, FE limit, and JM (keypad enable).
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

- **Hardware**: Yokogawa **7651** programmable DC source. One of two GPIB instruments on the box (the other is a Keithley DMM at GPIB primary 29 — see below).
- **PC link**: GPIB-USB adapter (vendor TBD — surfaces to NI-VISA as `GPIB0`) → NI-VISA → resource string **`GPIB0::15::INSTR`**. GPIB primary address **15** is set on DIP switches on the back of the 7651; if those get bumped, the device disappears from `pyvisa.ResourceManager().list_resources()` until reconfigured. **Historical note (2026-05-10)**: this doc previously listed address 29 — that was wrong, 29 belongs to the Keithley voltmeter on the same bus, and `yoko.py` was talking to the wrong device for weeks. The Keithley happens to emit `NDCV+x.xxxxxE+xx` readings that vaguely match the 7651's `OD;` reply format, which masked the misconfiguration entirely (`SA`/`F`/`O` writes silently did nothing on the Keithley, and OD reads returned the live measured voltage across the piezo, which was always ~0 because the real Yoko's output was off).
- **Sibling on the bus — Keithley 617 electrometer at GPIB0::29**: wired in series with the piezo for current readback (charging current during ramps + leakage at steady state). See its own section below.
- **Currently observed (probe)**: ~+11.40 V DC, status `N` (normal), ~4 mV jitter on the readback. Probe captured on 2026-05-08 with the unit live on the piezo.
- **Protocol — pre-SCPI Yokogawa command set** (full reference: [`yokogawa-7651-user-manual.pdf`](../air-stacker-gui/manuals/yokogawa-7651-user-manual.pdf), IM 7651-01E §6 Communication Functions):
  - Commands are terminated with `;` (CR LF and bare LF are also accepted by the unit; we standardize on `;`). Configure VISA with `write_termination=""` and put the `;` in every command string.
  - Replies are terminated with **CR only** on our unit (the `DL` command selects CR LF / CR / LF / EOI; ours is set to CR-only and persistent across power cycles). Use `read_termination='\r'`. Using `'\n'` will time out on every read.
  - **`OC;` (status byte) and `OS;` (panel settings) return empty replies on our unit** — either a firmware variant or a configuration we haven't probed. The canonical driver still exposes `read_status_code()` / `read_panel_settings()` but treat them as best-effort. For output-on/off state, lean on the software cache; for "is the unit alive at all," `inst.read_stb()` (serial poll) works and returns 0 normally.
  - **No `*IDN?`.** Sending `*IDN?` doesn't error, but the reply you read back is just whatever the talker has queued (the live OD-format output) — there is no real identification.
  - **Set commands are write-only** — programmed voltage / current / function / range / output state cannot be queried back. The driver caches them in software.
  - After any setting change you must send **`E;`** (or GPIB `<GET>`) to trigger / apply.
- **LOCAL / REMOTE state — important**: pressing the front-panel **LOCAL** key drops the unit into LOCS, where setting commands (`F`, `SA`, `O`, `RC`) are silently accepted on the bus but never acted on. The relay won't click, OD keeps reading the quiescent ~0 V, and no error is raised. Only `OD;` and bus-level operations (serial poll) keep working. `yoko.py.open()` asserts REN + addresses the unit as a listener to force REMS on connect; this is sufficient at startup but won't recover if LOCAL is pressed mid-session — the operator has to reconnect (or the GUI panel has to be re-instantiated). If the talker queue also gets stuck (OD; itself times out) call `Yoko7651.recover()` — it sends a Device Clear (DCL); **DCL also turns the unit's OUTPUT OFF**, so only use it when you're ready to re-ramp from 0.
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
- **Driver**: [`air-stacker-gui/yoko.py`](../air-stacker-gui/yoko.py) — thin pyvisa wrapper. Methods: `open` / `close`, `read_output` (parses OD), `set_voltage` / `set_current` / `set_mode` / `set_output` / `reset`, `ramp_voltage`, `safe_disable` (shutdown protocol — ramp to 0 then `O0;`), `recover` (DCL recovery — also turns output off). Set commands write-only with software cache; `E;` trigger is sent automatically after every set.
- **Shutdown protocol — always ramp before disabling output**: callers must use `Yoko7651.safe_disable()` (or `ramp_voltage(0.0)` followed by `set_output(False)`) when the unit is at non-zero voltage. Slamming `O0;` while sitting at V≠0 V opens the relay and dumps the full programmed voltage across the 1.7 µF NPM140 in one step, risking mechanical ringing at the piezo's 670 Hz resonance. `yoko_panel.py.shutdown()` wires this in for the GUI close path.
- **Probe script**: [`air-stacker-gui/probe_yoko.py`](../air-stacker-gui/probe_yoko.py) — sends `OD;`, decodes the reply, reports the live output. Read-only and safe.
- **Reference driver** (different framework, same protocol): [`~/sharpelab/measurement-env/src/sharpelab_nb/drivers/yokogawa_7651.py`](../../measurement-env/src/sharpelab_nb/drivers/yokogawa_7651.py) — QCoDeS `VisaInstrument` subclass with the same cache-on-set semantics; useful sanity check when extending `yoko.py`.

### Voltage readback — Keithley 617 (added 2026-05-10)

The 7651 → NPM140 path is open-loop; the 617 sits **across the piezo terminals** in DCV mode and gives us a real voltage trace. The 617's > 200 TΩ input impedance keeps loading negligible (vs. a 34401A's 10 MΩ, which would discharge the piezo). Two useful regimes:

- **Closed-loop control**: with the Yoko switched to constant-current source, the 617's V reading becomes the feedback signal — Yoko sources `I = C·dV/dt`, 617 reports where V actually is, software stops the source when V hits the target. See yoko_panel.py.
- **Quiescent monitoring**: at any time, the 617 tells us the piezo's actual voltage, decoupled from the Yoko's open-loop set value. Drift, leakage, or a wiring fault all show up here.

Prior incarnation (pre-2026-05-10): 617 was wired **in series with the piezo in AMPS mode** to read charging / leakage current. That topology produced asymmetric mA-scale currents during ramps that suggested wiring problems rather than piezo damage; the rewire to parallel-V was Aaron's fix. The driver / panel handle both modes (display adapts to `Reading.function`), but the steady-state rig is V.

#### Hardware

- **Model**: Keithley **617 Programmable Electrometer**. Sits physically on top of the 7651 in the rack — photo: [`img/keithley-617-yoko-stack.jpeg`](img/keithley-617-yoko-stack.jpeg).
- **Stack-up**: triax input from the rear panel (INPUT HI = red, INPUT LO = black, plus *unconnected* green guard). The 6011 input cable supplied by Keithley is what's wired here.
- **Capabilities** (per [quick-reference manual](../air-stacker-gui/manuals/keithley-617-quick-reference.pdf)):
  - **Volts**: ±200 mV / ±2 V / ±20 V / ±200 V, > 200 TΩ input impedance (vs the 34401A's 10 MΩ — important when the load is capacitive)
  - **Amps**: ±2 pA / ±20 pA / ±200 pA / ±2 nA / ±20 nA / ±200 nA / ±2 µA / ±20 µA / ±200 µA / ±2 mA / ±20 mA — resolution down to **0.1 fA** on the 2 pA range, input voltage burden < 1 mV
  - **Ohms**: 2 kΩ to 200 GΩ
  - **Coulombs**: 200 pC / 2 nC / 20 nC
  - **V-Source**: programmable −102 V to +102 V in 50 mV steps, ±2 mA max (current-limited at ~4 mA). **Not currently wired into the piezo loop** — the V-source is independent of the meter input.
- **Conversion time**: 330 ms — the unit is slow; don't expect µs-resolution traces. For ramp monitoring at 5 V/s, two-to-three readings during a 1-second ramp.
- **Front panel safety**: yellow label "**Enable Zero Check When Connecting/Disconnecting**." Zero Check internally shorts the input (and changes input impedance — see the table in the manual); always toggle it on before touching the cabling.

#### GPIB

- **Resource**: **`GPIB0::29::INSTR`**. Primary address 27 is the manual's default in examples; ours is set to 29 via the front-panel `IEEE "address"` program (Shift + SELECT until display shows IEEE).
- **Protocol — pre-SCPI letter-code commands** ([full reference: §35-39 of the quick-ref](../air-stacker-gui/manuals/keithley-617-quick-reference.pdf), much more detail in the [instruction manual](../air-stacker-gui/manuals/keithley-617-instruction-manual.pdf)):
  - Commands are 1-3 ASCII letters with an optional numeric argument. **`X` is the execute command** — like `E;` on the Yoko, the unit buffers settings until `X` applies them. Concatenate freely: `F1R0T1X` programs Amps + Autorange + Single-Talk-trigger + execute.
  - Default reply terminator is **CR LF** (`Y0`). Configure pyvisa with `read_termination='\r\n'`. The terminator is selectable (`Ym` / `Ymn` / `Y`) — don't change unless you re-record here.
  - **No `*IDN?`**. Sending it raises an IDDC (Invalid Device-Dependent Command) error and the talker silently returns the next OD-style reading. Use enumeration + `U0X` to identify.
  - **Default talker output** = the live measurement, format `NDCA+0.12345E-09` (see Data Format below). Like the 7651, any read without a preceding query just dumps the current reading.
- **Data format** (`*NDCA+0.12345E+06,nnn`):
  - `*` (or `N`/`O`): `N` Normal · `O` Overflow
  - `DCA` = function (`DCA` Amps · `DCV` Volts · `OHM` Ohms · `DCC` Coulombs · `DCX` External Feedback · `VSCR` when reading the V-Source via B4X)
  - `+0.12345` = 5½-digit mantissa
  - `E+06` = exponent
  - `,nnn` = buffer address (only on Data Store reads, B1X)
- **Common commands** for our use case:

  | Command | Meaning |
  |---|---|
  | `F1X` | Amps mode |
  | `F0X` | Volts mode |
  | `R0X` | Autorange |
  | `R1X` … `R10X` | Fixed range (table on p35 of quick-ref — pick the smallest covering your signal for best resolution / speed) |
  | `C0X` / `C1X` | Zero Check off / **on** (input internally shorted) |
  | `Z0X` / `Z1X` | Zero Correct off / on |
  | `U0X` | Next read returns the machine status word |
  | `U1X` | Next read returns the error status word (clears the error STB bit) |
  | `U2X` | Next read returns the data status word |
  | `B0X` | Read mode = live electrometer (default) |
  | `B4X` | Read mode = V-Source readback |
- **Status byte** (serial poll, `inst.read_stb()`): bit 0 Overflow · bit 1 Buffer Full · bit 2 Reading Done · bit 3 Ready · bit 4 Error · bit 6 SRQ-by-617. Power-up default SRQ mask is 70.
- **Machine-status decode** (positions in the `U0X` reply after the `617` prefix): `F RR C Z N T O B G D Q MM K YY`. Each field maps to the corresponding command letter — useful for sanity-checking front-panel state from the bus.

#### Currently observed (2026-05-10, after rewire + `F0R0X`)

- **Function = VOLTS** (DCV), range = Autorange, **Zero Check = OFF**, **Zero Correct = ON**.
- Reading: ~+14 µV with the Yoko output relay open (piezo floating near 0 V) — consistent with a parallel V probe on an undriven piezo.
- Suppress = OFF, V-Source Operate = OFF.
- The function-mode change was done via `F0R0X` over the bus (transient — survives until the next power cycle or front-panel function press). Earlier `*IDN?` / OD; attempts at GPIB 29 had left an IDDC error pending in a prior session; clears on any valid command.

#### Workflow notes

V-mode workflow (current configuration):

1. Warm up at least an hour after power-on (per the manual).
2. Send `F0R0X` if the unit isn't already in VOLTS / Autorange (the front-panel function knob also works).
3. Zero Correct (`Z1X` after a moment in Zero Check) — captures the offset.
4. Zero Check OFF (`C0X`) — readings now reflect the live piezo voltage.
5. When re-cabling: Zero Check back ON before touching the input.

A-mode workflow (legacy series-current rig, no longer in use): `F1R0X` lands in AMPS/auto; same Zero Check / Zero Correct dance. Kept for the historical record in case the wiring gets reverted.

#### Physical interaction needed?

For ongoing operation: **no — everything is bus-driven**. The GPIB command set covers function/range select, Zero Check on/off (`C0X`/`C1X`), Zero Correct (`Z0X`/`Z1X`), suppress, V-Source value/operate, trigger mode, and all reads. The whole "zero / take out of zero check / read current" workflow above can be done from a Python script over GPIB.

Front-panel-only (no bus equivalent):
- Setting the **IEEE-488 primary address** itself — `IEEE "address"` front-panel program (SHIFT + SELECT → ADJUST). Ours is 29; only needs a touch if we change rigs or the address gets bumped.
- Power on/off and physical triax cabling (obviously).
- Calibration entry (and we won't touch that).

So once cabled and on the bus, the 617 is a fully remote instrument. `keithley617.py` + the Yoko panel drive the full measurement loop without anyone reaching over to the rack.

GUI integration shipped: [`keithley617.py`](../air-stacker-gui/keithley617.py) mirrors `yoko.py`'s shape (open() asserts REN, raw `inst.read()`, regex-parsed NDC* replies). The driver intentionally does **not** change function/range/trigger — those track the unit's runtime state (front panel or one-shot bus writes). [`yoko_panel.py`](../air-stacker-gui/yoko_panel.py) polls at 1 Hz and shows the live reading via `Reading.unit` + `Reading.function`, so a DCV → DCA flip on the front panel surfaces as `617 · DCA` instead of being silently mis-labelled.

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
