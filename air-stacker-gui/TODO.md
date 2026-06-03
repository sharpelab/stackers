# TODO

* Fix heater
  * also manual mode (maybe)
* add recording
* add camera controls (gain, exposure)
* implement "start" - fill out form, write to gdrive, start recording
* clean up `docs/air-stacker-pc.md` once SMC100 has fully replaced the CONEX-CC Z axis (drop "Z and spin" wording, remove the COM3/COM4 Z-vs-spin open question)
* GPIB bus wedge follow-ups (2026-06-03 investigation; see memory `reference_gpib_bus_wedge_recurring`):
  * DONE: 617 `poll_interval_ms` 0 → 50 ms (stop pegging the bus back-to-back)
  * surface sustained GPIB TMO as a banner in the Yoko panel (it sat wedged silently for days in May; right now it only logs warnings)
  * exit-hang teardown fix in `YokoPanel.shutdown()` (drafted, not applied): join must exceed the VISA read timeout, and only `close()` a driver after its worker thread actually exits — else the GUI thread deadlocks on the driver lock the stuck read holds
  * SRQ-driven 617 reads (option, NOT built — paced poll first): 617 supports SRQ (status bit 2 "Reading Done", SRQ mask via `M` cmd). Watchdog would `wait_on_event(VI_EVENT_SERVICE_REQ)` → `read_stb()` (confirm + clear latched SRQ) → `read()` one value, instead of spin-reading. Upside: controller stops holding the bus for ~333 ms waiting on each conversion (frees it for the Yoko); reads only fire when data is ready (fewer mid-handshake timeout-aborts). Downsides: does NOT cut transaction count (adds a serial poll per reading), fiddly on a pre-SCPI 617 over USB-GPIB (must serial-poll to deassert SRQ, handle timeout fallback), and the watchdog must keep reading continuously for safety regardless. Bonus: could also SRQ on Error/Overflow (bits 4/0) to detect the 617's "strange display" hang early — but a full hard-wedge kills the serial poll too (observed 2026-06-03), so that only catches soft errors.