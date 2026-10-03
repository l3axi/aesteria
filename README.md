# aesteria

Research tooling for **AION 2** on Linux (Steam / Proton): reading the game's state from outside the
process, plus a Vulkan overlay layer to draw it.

Everything here was built by measuring the live client, not by assuming. Where a result is not
proven live the source says so.

## What's in it

- `tools/` — Python tools that read `/proc/<pid>/mem` from outside the process (no injection):
  UObject/GNames/GObjects discovery, a blind class registry that survives client patches, entity and
  player readers, HP read from the UI, a feather/dungeon radar and route solver, a frame-rate meter
  and live cap, and an overlay driver.
- `src/inject/` — a passive `LD_PRELOAD` probe and a Vulkan implicit layer (overlay) with their Makefiles.
- `spikes/` — throwaway experiments, with their findings.

## Safety notes

- Tools are read-only unless a mode says `--apply`. Writes go through `/proc/<pid>/mem`.
- Offsets and layouts are build-specific. Re-derive them after a client patch (`aesteria_registry.py`).
- Launch through `tools/aesteria_wrap.sh`: it is a pass-through until you set a mode, and it escalates
  one step per launch so a bad result stays attributable.
- This runs against a live online game. You are responsible for what that means for your account.

## Tests

Offline tests live in `tools/test_*.py` and `tools/test_wrap_modes.sh`. `test_registry.py` needs a real
process capture and skips (loudly) without one.

## Licence

MIT. See `LICENSE`.
