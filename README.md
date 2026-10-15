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

## Running it

Linux only. The game runs under Steam/Proton; these tools run beside it, as your own user.

**Requirements**

* Python 3.11+
* `numpy`
* For the overlay only: **GTK 4** + **gtk4-layer-shell** and their GObject bindings
  (Arch: `python-gobject gtk4 gtk4-layer-shell`; Debian/Ubuntu:
  `python3-gi gir1.2-gtk-4.0 gir1.2-gtk4layershell-1.0`)
* Permission to read `/proc/<pid>/mem` of your own game process. On most distributions
  Yama restricts this — either `sudo sysctl kernel.yama.ptrace_scope=0` for the session, or
  run the tool with `sudo`. Nothing here needs root for any other reason.

**The overlay** — the thing most people want:

```sh
python3 tools/aesteria_overlay.py --screen 1920x1080          # set YOUR resolution
python3 tools/aesteria_overlay.py --screen 1920x1080 --hp     # + HP bars
python3 tools/aesteria_overlay.py --screen 1920x1080 --all    # + every interactable, labelled
```

Run the game **borderless**, not exclusive fullscreen. The overlay is a `wlr-layer-shell`
surface, so it needs a Wayland compositor that implements that protocol (sway, Hyprland, KDE
Plasma; **GNOME does not**). It is click-through and never takes focus.

**Without the overlay**, every reader works standalone and prints to the terminal:

```sh
python3 tools/aesteria_radar.py --mesh Protect_01 --state 1 --watch   # uncollected feathers
python3 tools/aesteria_dungeons.py                                    # entrances from the paks
python3 tools/aesteria_route.py                                       # a loop over both
python3 tools/aesteria_hp.py                                          # own + nameplate HP
python3 tools/aesteria_fps.py                                         # frametimes
```

Offsets are per client build. After a patch, re-derive with `tools/aesteria_registry.py` rather
than editing constants by hand — the registry finds classes by shape, so it survives patches
that move things.

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
