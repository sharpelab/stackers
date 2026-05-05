# Air Stacker PC — Reference

Workstation that drives the **Air Stacker** (the simpler in-use stacker, **not** Ranger).

## Connectivity

- **Tailscale**: `air-stacker.tail737ca5.ts.net` (100.92.166.118), Windows, owned by `dgglab@`. *(Rename in the admin panel may still be pending — local hostname pref is set; if DNS still resolves only `stacker.tail737ca5.ts.net`, force-rename via https://login.tailscale.com/admin/machines.)*
- **SSH**: `ssh air-stacker` (alias defined in `~/.ssh/config`). User is `ddg_transfer` — note the typo'd username, two d's at the start. Key auth via `administrators_authorized_keys` (admin user).
- **RustDesk**: service running. Entry on the wiki: [RustDesk Addresses](https://wiki.sharpelab.science/doc/rustdesk-addresses-y3aYDSeXCw) ("Air Stacker" section).
- **VNC**: vncserver service also running.
- **Default SSH shell**: Git Bash (`C:\Program Files\Git\usr\bin\bash.exe`).

## Camera

- **Hardware**: Point Grey Research **Flea3 FL3-U3-20E4C** (USB3, 1.3MP color Bayer, 1280x1024 ~60 fps native). Sole connected camera.
- **SDK**: FLIR Spinnaker **2.3.0.77** (built Nov 25 2020). Installed at `C:\Program Files\FLIR Systems\Spinnaker\`.
- **GUI in use**: `SpinView_WPF.exe` at `bin64/vs2015/SpinView_WPF.exe` (Desktop shortcut: `new camera.lnk`). Used today for exposure / WB / gamma / color sliders.
- **GenTL producer** (relevant for `harvesters`): `.cti` files under `C:\Program Files\FLIR Systems\Spinnaker\cti64\vs2015\`. The env var `GENICAM_GENTL64_PATH` points there.
- **PySpin**: not installed; SDK 2.3.0 is too old to have cp310 wheels. Going with `harvesters` (pure-Python GenTL client) for our custom viewer.
- **Legacy / unused** Desktop shortcuts (ignore): FlyCap2 (`Camera.lnk`), IC Capture 2.5, Micro-Manager / ImageJ (`hist but slower fps.lnk`).

## Stage — CONEX-CC

- **Hardware**: Newport CONEX-CC dual-axis: **Z and spin** (not X+Z). Two USB-serial controllers.
- **Ports**: COM3 + COM4 (USB Serial Port). Firmware/driver string: `CONEX-CC 2.0.1`.
- **Existing front-ends on the box**:
  - Vendor GUI: `C:\Newport\Motion Control\CONEX-CC\Bin\64-bit\Newport.CONEXCC.StandAlone.exe` (Desktop: `Stage Controller.lnk`).
  - LabVIEW custom UI: `~/Desktop/CONEX-CC-GUI/` — git remote `https://github.com/caoyuan96421/CONEX-CC-GUI.git`. Likely the day-to-day tool. `.lvproj`/`.lvlps` LabVIEW project. Uses NI-VISA (NI MAX is installed).
- **Protocol**: ASCII serial (Newport CONEX-CC documented protocol). Easy to drive directly without LabVIEW.

## Heater

- **Hardware**: **Omega Engineering Platinum** series controller. Device ID `062BE937`, firmware `1.4.0.6`, run mode RUNNING (per the Configurator's Device Information panel).
- **Connection**: USB-CDC virtual COM via Omega's `OmegaVCP.inf` driver — currently enumerated as **COM7**.
- **Protocol**: Modbus RTU at 19200 8N1, slave ID 1 by default. 32-bit IEEE floats span two consecutive holding registers. Manuals committed in `docs/`: [M5458 Modbus interface](omega-platinum-m5458-modbus.pdf) (register map + enums) and [M5451 controller user guide](omega-platinum-m5451-user-manual.pdf) (front-panel menus, OPER modes including M.CNt manual output / M.INP manual input).
- **Software**: `~/Desktop/Platinum_Firmware_Software_1.4.0.6/` containing `EIP_1.1.5`, `Firmware_1.4.0.6`, `Platinum_Configurator_1.5.2.0`, `USBDriver`. Launcher on Desktop is `Temp Controller.appref-ms` (ClickOnce).
- The earlier "Thermo Scientific Platinum" label in this doc was wrong — it's Omega.

## Defunct / disabled

- The USBTMC SCPI temp/humidity instrument that `~/something.py` used to poll (`SENS1:TEMP:DATA?` / `SENS2:HUM:DATA?` → `spxtr.net/demgraphs/` as `stacker.temp`/`stacker.hum`) is **no longer connected** — pyvisa enumerates no USB instruments. The script and its `something.bat` companion are still on disk but inert.
- Scheduled task **`Wowza`** that re-launched `something.py` at login has been disabled (not deleted) on 2026-05-05.

## Serial port map (current snapshot)

| Port | Device | Purpose |
|------|--------|---------|
| COM1 | Communications Port (legacy) | Unused |
| COM3 | USB Serial Port | CONEX-CC axis A (Z or spin) |
| COM4 | USB Serial Port | CONEX-CC axis B (Z or spin) |
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
