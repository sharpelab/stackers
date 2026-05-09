# CLAUDE.md - Stackers

Files useful for the DGG / Sharpe Lab's stackers.

Python projects use uv.

## Reference docs

- [air-stacker-gui/docs/air-stacker-pc.md](air-stacker-gui/docs/air-stacker-pc.md) — Air Stacker PC machine reference (camera, stage, heater, connectivity). Vendor manuals live alongside it under [air-stacker-gui/manuals/](air-stacker-gui/manuals/).

## Subdirectories

- `air-stacker-gui/` — custom microscope viewer + recorder for the Air Stacker (FLIR Flea3 via harvesters/GenTL).
- `piezo-z/` — controller UI for a piezo controlling the Z stage of the stacker. Interacts with a Yokogawa 7651.
