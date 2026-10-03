#!/usr/bin/env python3
"""Enumerate every positioned actor in a capture, ranged from the local player.

This is the foundation for ESP, targeting and any "what is near me" logic. It
reuses the validated player walk to establish an origin, then sweeps
GUObjectArray for actors that have a RootComponent with a plausible position.

WHAT MAKES AN ENTRY TRUSTWORTHY
    An actor qualifies only if AActor+448 (RootComponent) points at something
    whose RelativeLocation is finite and in-world. That rejects the CDOs, the
    abstract classes and the half-torn-down objects that make a naive object
    sweep look full of ghosts.

WHAT THIS DELIBERATELY DOES NOT CLAIM
    A capture is one instant. Counts and distances here describe that instant and
    nothing else -- objects are pool-recycled as the world streams, which is why
    capture 04 holds 472k objects against capture 01's 408k. Treat a pointer as
    valid only for the capture it came from.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from aesteria_snapshot_reader import Snapshot                      # noqa: E402
from aesteria_gobjects import GObjects                              # noqa: E402
from aesteria_player import (load_meta, find_local_player,          # noqa: E402
                             read_vector, plausible_world_vector,
                             ACTOR_ROOT_COMPONENT, SCENE_RELATIVE_LOCATION,
                             SCENE_RELATIVE_ROTATION)

# Class-name fragments worth calling out. Derived from the live name dump, not
# guessed: BPCh_* is the character blueprint family this build uses.
INTERESTING = ("Ch_", "Character", "Pawn", "Npc", "NPC", "Monster", "Mob",
               "Player", "Pc", "Item", "Drop", "Gather", "Portal", "Vehicle")


def classify(class_name: str | None) -> str:
    if not class_name:
        return "unknown"
    for frag in INTERESTING:
        if frag in class_name:
            return frag
    return "other"


def sweep(snap: Snapshot, go: GObjects, origin, limit_range: float | None,
          verbose: bool = False) -> list[dict]:
    """Walk every object; keep the ones that resolve to a world position."""
    out: list[dict] = []
    scanned = 0
    for i, addr in go.iter_objects():
        scanned += 1
        root = snap.u64(addr + ACTOR_ROOT_COMPONENT)
        if not root or not snap.looks_like_pointer(root):
            continue
        loc = read_vector(snap, root + SCENE_RELATIVE_LOCATION)
        if not plausible_world_vector(loc):
            continue
        # A root component must itself be a component; this rejects the large
        # number of unrelated objects that happen to hold a pointer at +448.
        root_cls = go.class_name(root)
        if not root_cls or "Component" not in root_cls:
            continue
        dist = math.dist(origin, loc) if origin else 0.0
        if limit_range is not None and dist > limit_range:
            continue
        out.append({
            "index": i, "addr": addr,
            "class": go.class_name(addr),
            "name": go.object_name(addr),
            "root": root, "root_class": root_cls,
            "location": loc,
            "rotation": read_vector(snap, root + SCENE_RELATIVE_ROTATION),
            "distance": dist,
        })
        if verbose and len(out) % 2000 == 0:
            print(f"  ... {len(out)} positioned actors ({scanned} scanned)",
                  flush=True)
    out.sort(key=lambda e: e["distance"])
    return out


def open_source(args):
    """Snapshot or live process. LiveProcess is a Snapshot-compatible read-only view.

    WHY --pid EXISTS (2026-10-01). This was capture-only, which was right during the playtest:
    one six-hour window, and the winning move was to copy the address space and parse it later.
    It is the wrong shape now. The retail client stays up for hours, and an 8 GB capture per look
    is not something an ESP can be built on -- worse, a capture only answers "what was near me
    when I took it", and the first feather hunt failed for exactly that reason: capture 10 was
    taken in a city, so the overworld collectibles were never streamed in to be found.
    """
    if args.pid is not None:
        from aesteria_live import LiveProcess                       # noqa: PLC0415
        live = LiveProcess(args.pid or None)
        print(f"live pid {live.pid}")
        return live
    snap = Snapshot(Path(args.capture))
    print(snap.stats())
    return snap


def load_for(src, args):
    """(g, meta) for whichever source kind `src` is."""
    fallback = Path(args.meta_from) if args.meta_from else None
    if isinstance(src, Snapshot):
        return load_meta(src, Path(args.capture), fallback, args.verbose)
    from aesteria_live import load_live_meta                        # noqa: PLC0415
    return load_live_meta(src, fallback, args.verbose)


def main() -> int:
    ap = argparse.ArgumentParser(description="enumerate positioned actors")
    ap.add_argument("capture", nargs="?", help="banked capture; omit with --pid")
    ap.add_argument("--pid", type=int, nargs="?", const=0, default=None,
                    help="read the LIVE game instead of a capture "
                         "(bare --pid auto-detects)")
    ap.add_argument("--meta-from", help="borrow GNames/GObjects metadata "
                                        "(same process only)")
    ap.add_argument("--range", type=float, default=None,
                    help="only actors within this many units of the player")
    ap.add_argument("--match", action="append", default=None,
                    help="only actors whose class or name contains this "
                         "(repeatable, case-insensitive)")
    ap.add_argument("--top", type=int, default=40, help="rows to print")
    ap.add_argument("-o", "--out", help="write all entries as TSV")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    if not args.capture and args.pid is None:
        ap.error("give a capture directory or --pid")

    with open_source(args) as snap:
        g, meta = load_for(snap, args)
        go = GObjects(snap, meta, g)

        players = find_local_player(snap, go, g)
        if not players:
            print("no local player found; distances will be absolute")
            origin = None
            me = None
        else:
            me = players[0]
            origin = me["location"]
            print(f"\nlocal player: {me['pawn_class']} at "
                  f"({origin[0]:.1f}, {origin[1]:.1f}, {origin[2]:.1f})")

        print("\nsweeping for positioned actors ...")
        ents = sweep(snap, go, origin, args.range, args.verbose)
        print(f"  {len(ents):,} positioned actors")

        by_kind = Counter(classify(e["class"]) for e in ents)
        print("\n  by kind:")
        for k, n in by_kind.most_common():
            print(f"    {k:<14} {n:>7,}")

        cls_counts = Counter(e["class"] or "?" for e in ents)
        print("\n  most common classes:")
        for c, n in cls_counts.most_common(15):
            print(f"    {c:<44} {n:>6,}")

        # Characters are what a bot actually cares about.
        chars = [e for e in ents
                 if e["class"] and ("Ch_" in e["class"]
                                    or "Character" in e["class"]
                                    or "Pawn" in e["class"])]
        print(f"\n  {len(chars)} character-like actors; nearest 20:")
        for e in chars[:20]:
            x, y, z = e["location"]
            mine = "  <-- YOU" if me and e["addr"] == me["pawn"] else ""
            print(f"    {e['distance']/100:8.1f} m  {e['class']:<28} "
                  f"{e['name']!r:<30} ({x:9.1f},{y:10.1f},{z:8.1f}){mine}")

        # --match is how you hunt for a specific actor family without knowing its class name
        # up front: sweep once, then grep the live world by substring. Matching on class OR
        # name matters because this build gives most actors a name identical to their class,
        # but the interesting ones (placed level actors) carry a real designer-authored name.
        if args.match:
            needles = [m.lower() for m in args.match]
            hits = [e for e in ents
                    if any(n in (e["class"] or "").lower()
                           or n in (e["name"] or "").lower() for n in needles)]
            print(f"\n  --match {args.match}: {len(hits)} hit(s)"
                  + (f", nearest {args.top}:" if hits else ""))
            for e in hits[:args.top]:
                x, y, z = e["location"]
                print(f"    {e['distance']/100:8.1f} m  {e['class']:<34} "
                      f"{e['name']!r:<34} ({x:9.1f},{y:10.1f},{z:8.1f})")

        if args.out:
            with open(args.out, "w") as fh:
                fh.write("#index\taddr\tclass\tname\troot_class\tx\ty\tz\tdist\n")
                for e in ents:
                    x, y, z = e["location"]
                    fh.write(f"{e['index']}\t{e['addr']:#x}\t{e['class']}\t"
                             f"{e['name']}\t{e['root_class']}\t{x:.3f}\t{y:.3f}\t"
                             f"{z:.3f}\t{e['distance']:.3f}\n")
            print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
