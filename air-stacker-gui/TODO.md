# TODO

* Fix heater
  * also manual mode (maybe)
* rig-validate: still capture, video recording (libx264 ultrafast), Z step presets, arrow-key jog
* QSV on the rig: encoder works headless but MFX_ERR_DEVICE_FAILED (-17) within ~8 s whenever the GUI renders GL on the same UHD 630 (isolated 2026-07-28) — config forces libx264 ultrafast. Revisit after an Intel driver update; flip config `codec` back to "auto" to retest.
* idle CPU (~1.5 cores at 54 fps live view, py-spy 2026-07-28): PySpin GetNDArray 13%, paintGL 11%, to_rgb debayer 10%, hist ~6% (sharpness now computes at readout cadence). Further wins need pipeline changes — hist could throttle the same way if it ever matters.
* session-level event streams alongside recordings (SESSION_LOG_PROPOSAL.md — deferred from recording v0, as are camera timestamps in timestamps.csv)
* add camera controls (gain, exposure)
* implement "start" - fill out form, write to gdrive, start recording
* clean up `docs/air-stacker-pc.md` once SMC100 has fully replaced the CONEX-CC Z axis (drop "Z and spin" wording, remove the COM3/COM4 Z-vs-spin open question)
