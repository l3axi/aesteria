#!/usr/bin/env python3
"""aesteria_radar -- live terminal radar for world actors, ranged and bearing-relative.

WHAT THIS IS FOR
    the operator's ask: an ESP for Altgard's overworld collectibles ("Empyrean Trace: Altgard", the
    feathers). Drawing them in-world needs the Vulkan overlay, which needs injection, which is
    gated on having a throwaway account. This needs NONE of that -- it is a read-only terminal
    view he can put on a second monitor, and it works today.

    It is also the instrument that settles what turn-by-turn measurement could not. Every
    attempt to catch a collection by snapshotting before and after failed for the same reason:
    between one chat message and the next, 2-5 minutes pass, the operator walks 60-150 m to the next
    collectible, and every object that was baselined streams out and gets pool-recycled. The
    window was never the analysis, it was the round trip. A continuously refreshing view removes
    the round trip entirely -- and more importantly it puts the answer next to HIS EYES, which
    are a better oracle for "is there a feather there" than any amount of inference from me.

THE STATE FIELD, and exactly how much is established
    `BP_EnvObj_Common_C` (1728 bytes) is the interactable family. Evidence, 2026-10-01:
      - two collectibles the operator confirmed collecting both read u32[+0x27c] == 0 right after
      - one he never touched read 2 on reads minutes apart
      - an AionTargetDecalActor sat at the EXACT position of one of them, so the game treats
        this class as targetable
      - all watched fields are stable per object across 8 reads at 1 s (it does not flicker;
        an apparent 0->2 was an object streaming out and a different one taking its position)
    ⚠ SUPERSEDED 10-01 late: +0x27c reads 0-3 across ore, doors and triggers, so it is not a
    feather state -- the mesh column is the discriminator. Original note: the transition itself
    was never observed on one object, and many objects
    read +0x27c == 1, which is unexplained. So this prints the RAW value and calls nothing
    "collected". Judge it against the screen. `--legend` explains the columns.

BEARING IS RELATIVE TO YOUR FACING, and the handedness is UNVERIFIED
    Absolute compass would need to know which way AION 2 calls north, which nobody has measured.
    Relative bearing needs only the player's own yaw, so that is what this shows: 0 deg is dead
    ahead. ⚠ Whether positive means left or right is a handedness assumption -- glance once at a
    known object and flip --swap-lr if it reads mirrored. Stated rather than silently guessed.

SAFETY
    Read-only: /proc/<pid>/mem via pread, no attach, no write, no injection.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aesteria_gobjects import GObjects                              # noqa: E402
from aesteria_live import LiveProcess, load_live_meta               # noqa: E402
from aesteria_entities import sweep                                 # noqa: E402
from aesteria_player import find_local_player                       # noqa: E402

META_CACHE = Path(__file__).resolve().parents[1] / "snaps" / "meta"

# The interactable family and the candidate state field. Kept as flags, not constants in the
# report, because neither is proven and a tool that hardcodes an unproven belief teaches it.
DEFAULT_CLASS = "BP_EnvObj_Common_C"
STATE_OFF = 0x26C          # feather: 1 = available, 2 = collected (3/3 pickups, 10-01)
TEMPLATE_OFF = 0x518       # 0x5dd7 -> 0x5dd8 on every pickup -- looks like a template/variant id
# AionEnvObj's own mesh components (SDK), and the mesh asset each one holds. The asset NAME is
# what tells a feather from an ore node: BP_EnvObj_Common_C is the game's generic interactable,
# and capture 10 holds ore (MediumFafnium_D_02, OreIron), odium (OdSmall), a WarehouseDoor_01
# and a Trigger all under that one class -- with +0x27c reading 0-3 across all of them.
ENVOBJ_SKELETAL = 1072          # AionEnvObj.SkeletalMeshComponent
ENVOBJ_STATIC = 1080            # AionEnvObj.StaticMeshComponent
MESH_ASSET = 1536               # SkinnedMeshComponent.SkinnedAsset == StaticMeshComponent.StaticMesh


def mesh_name(live, go, actor) -> str:
    for comp_off in (ENVOBJ_SKELETAL, ENVOBJ_STATIC):
        comp = live.u64(actor + comp_off)
        asset = live.u64(comp + MESH_ASSET) if comp else 0
        if asset:
            return go.object_name(asset) or "?"
    return "-"

LEGEND = """columns
  dist    metres from you (game units / 100)
  rel     bearing relative to YOUR FACING; 0 = dead ahead, sign = left/right
          (handedness VERIFIED live 10-01: UE is left-handed, +Y is to the right, so a
          positive cross product is RIGHT -- the first version printed it mirrored)
  mesh    the object's mesh asset -- this is what separates a feather from an ore node
  0x26c   feather state: 1 = still there, 2 = collected. Flipped 1->2 on all three pickups
          10-01 and on no idle read. (0x538 was a wrong turn -- it ticks up on its own.)
  0x518   0x5dd7 -> 0x5dd8 on every pickup; probably the object's variant/template id
  z       height difference from you, metres (+ is above)
"""


def bearing_rel(px, py, yaw_deg, x, y, swap_lr=False):
    """Signed angle from the player's facing to a target, in degrees."""
    fx, fy = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
    dx, dy = x - px, y - py
    ang = math.degrees(math.atan2(fx * dy - fy * dx, fx * dx + fy * dy))
    return -ang if swap_lr else ang


def one_pass(live, go, g, args) -> bool:
    players = find_local_player(live, go, g)
    if not players:
        print("not in-world (character select / loading) — waiting…")
        return False
    me = players[0]
    px, py, pz = me["location"]
    yaw = me["rotation"][1] if me.get("rotation") else 0.0

    ents = sweep(live, go, me["location"], args.range, False)
    rows = []
    for e in ents:
        if args.cls not in (e["class"] or ""):
            continue
        st = live.u32(e["addr"] + STATE_OFF)
        f268 = live.u32(e["addr"] + TEMPLATE_OFF)
        if args.state is not None and st != args.state:
            continue
        x, y, z = e["location"]
        mesh = mesh_name(live, go, e["addr"])
        if args.mesh and args.mesh.lower() not in mesh.lower():
            continue
        rows.append((e["distance"], st, f268, bearing_rel(px, py, yaw, x, y, args.swap_lr),
                     z - pz, (x, y, z), mesh))
    rows.sort()

    print(f"\n{time.strftime('%H:%M:%S')}  you ({px:.0f}, {py:.0f}, {pz:.0f})  yaw {yaw:+.1f}°"
          f"   {len(rows)} × {args.cls} within {args.range/100:.0f} m")
    if not rows:
        print("   none in range")
        return True
    print(f"   {'dist':>8}  {'rel':>7}  {'0x26c':>5} {'0x518':>6}  {'dz':>7}   {'mesh':<26} position")
    for dist, st, f268, rel, dz, (x, y, z), mesh in rows[:args.top]:
        side = "ahead" if abs(rel) < 12 else (f"{abs(rel):3.0f}° {'R' if rel > 0 else 'L'}")
        s268 = f"{f268:#x}"
        print(f"   {dist/100:7.1f} m  {side:>7}  {st:>5} {s268:>6}  {dz/100:+6.1f} m   "
              f"{mesh[:26]:<26} ({x:.0f},{y:.0f},{z:.0f})")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pid", type=int, nargs="?", const=0, default=None,
                    help="game pid; bare --pid or omitted auto-detects")
    ap.add_argument("--cls", default=DEFAULT_CLASS, help="actor class substring to show")
    ap.add_argument("--range", type=float, default=30000.0, help="units; default 30000 (300 m)")
    ap.add_argument("--state", type=int, default=None,
                    help=f"only show actors whose +{STATE_OFF:#x} equals this")
    ap.add_argument("--mesh", default=None, help="only actors whose mesh asset name contains this")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--watch", type=float, nargs="?", const=3.0, default=None,
                    help="refresh every N seconds (default 3)")
    ap.add_argument("--swap-lr", action="store_true", help="flip the left/right sense")
    ap.add_argument("--legend", action="store_true")
    args = ap.parse_args()
    if args.legend:
        print(LEGEND)
        return 0

    live = LiveProcess(args.pid or None)
    with live:
        g, meta = load_live_meta(live, META_CACHE if META_CACHE.exists() else None, False)
        go = GObjects(live, meta, g)
        if not args.watch:
            one_pass(live, go, g, args)
            return 0
        print(f"radar on pid {live.pid}, refresh {args.watch}s — Ctrl-C to stop")
        try:
            while True:
                # ⚠ The map changes as the world streams; a stale region list silently drops
                # actors that moved into freshly mapped memory.
                live.refresh_regions()
                go.refresh()
                one_pass(live, go, g, args)
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
