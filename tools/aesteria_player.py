#!/usr/bin/env python3
"""Locate the local player and read position/rotation from a capture.

This walks the same chain the engine itself uses, with offsets taken from the
SDK dump rather than found by scanning for plausible floats:

    AionPlayerController (live instance, not the CDO)
      -> AcknowledgedPawn / Pawn     APlayerController+1000 / AController+888
        -> RootComponent             AActor+448
          -> RelativeLocation        USceneComponent+328   (FVector = 3 doubles)
             RelativeRotation        USceneComponent+352
             ComponentVelocity       USceneComponent+400

WHY IT MATTERS THAT WE WALK RATHER THAN SCAN
    Scanning memory for three plausible floats finds dozens of candidates --
    every other player, every prop, every camera. The controller link is what
    makes one of them provably OURS: only the local client holds a
    PlayerController whose AcknowledgedPawn is the character it drives.

VALIDATION -- compare two captures taken at known-different positions:
    aesteria_player.py --compare captures/03-position-A captures/04-position-B
A reader that is actually reading the player must show A stationary and B far
away. A single reading proves nothing: a value only means something next to a
control, and a moving value cannot be checked against a stale snapshot.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from aesteria_snapshot_reader import Snapshot                     # noqa: E402
from aesteria_gnames import GNames, LAYOUTS, find_gnames           # noqa: E402
from aesteria_gobjects import (GObjects, find_gobjects,            # noqa: E402
                               validate_array, UOBJ_CLASS, UOBJ_NAME)

# Offsets measured from the SDK dump of this build (see sdk/offsets.tsv).
CONTROLLER_PAWN = 888
PC_ACKNOWLEDGED_PAWN = 1000
ACTOR_ROOT_COMPONENT = 448
SCENE_RELATIVE_LOCATION = 328
SCENE_RELATIVE_ROTATION = 352
SCENE_COMPONENT_VELOCITY = 400


def read_vector(snap: Snapshot, addr: int) -> tuple[float, float, float] | None:
    """FVector in UE5 is three doubles (24 bytes), not three floats."""
    raw = snap.read(addr, 24)
    if raw is None or len(raw) < 24:
        return None
    return struct.unpack("<ddd", raw)


def plausible_world_vector(v: tuple[float, float, float] | None) -> bool:
    """UE worlds are bounded; NaN/inf and absurd magnitudes are not positions."""
    if v is None:
        return False
    for c in v:
        if math.isnan(c) or math.isinf(c) or abs(c) > 1e9:
            return False
    return True


def load_meta(snap: Snapshot, capture: Path, fallback: Path | None,
              verbose: bool = False) -> tuple[GNames, dict]:
    """Load GNames/GObjects metadata, revalidating whatever we reuse.

    Captures from the same process share addresses, so metadata is reusable --
    but reuse is only safe if it is re-proven against the capture at hand.
    """
    def try_gnames(d: Path) -> GNames | None:
        p = d / "gnames.json"
        if not p.exists():
            return None
        meta = json.loads(p.read_text())
        meta["_layout"] = next(l for l in LAYOUTS if l.name == meta["layout"])
        g = GNames(snap, meta)
        return g if g.by_id(0) == "None" else None

    g = try_gnames(capture) or (try_gnames(fallback) if fallback else None)
    if g is None:
        info = find_gnames(snap, verbose=verbose)
        if not info:
            sys.exit("cannot locate GNames in this capture")
        g = GNames(snap, info)
    if verbose:
        print(f"  GNames ok (by_id(0)={g.by_id(0)!r})")

    def try_gobjects(d: Path) -> dict | None:
        p = d / "gobjects.json"
        if not p.exists():
            return None
        meta = json.loads(p.read_text())
        # Revalidate against THIS capture, do not take the cached word for it.
        res = validate_array(snap, meta["array"], g)
        if res:
            res["fuobjectarray"] = meta["fuobjectarray"]
            return res
        return None

    go_meta = try_gobjects(capture) or (try_gobjects(fallback) if fallback else None)
    if go_meta is None:
        go_meta = find_gobjects(snap, g, verbose=verbose)
        if not go_meta:
            sys.exit("cannot locate GUObjectArray in this capture")
    if verbose:
        print(f"  GUObjectArray ok: {go_meta['num_elements']:,} objects, "
              f"invariant {go_meta['match_rate']*100:.1f}%")
    return g, go_meta


def find_local_player(snap: Snapshot, go: GObjects, g: GNames,
                      verbose: bool = False) -> list[dict]:
    """Return every live PlayerController that drives a pawn with a position."""
    results = []
    for i, addr in go.iter_objects():
        cname = go.class_name(addr)
        if not cname or "PlayerController" not in cname:
            continue
        oname = go.object_name(addr) or ""
        if oname.startswith("Default__"):
            continue  # class default object, not a live controller
        pawn = snap.u64(addr + PC_ACKNOWLEDGED_PAWN)
        via = "AcknowledgedPawn"
        if not pawn or not snap.looks_like_pointer(pawn):
            pawn = snap.u64(addr + CONTROLLER_PAWN)
            via = "Controller.Pawn"
        if not pawn or not snap.looks_like_pointer(pawn):
            continue
        root = snap.u64(pawn + ACTOR_ROOT_COMPONENT)
        if not root or not snap.looks_like_pointer(root):
            continue
        loc = read_vector(snap, root + SCENE_RELATIVE_LOCATION)
        if not plausible_world_vector(loc):
            continue
        results.append({
            "controller_index": i,
            "controller": addr,
            "controller_class": cname,
            "controller_name": oname,
            "pawn": pawn,
            "pawn_class": go.class_name(pawn),
            "pawn_name": go.object_name(pawn),
            "pawn_via": via,
            "root_component": root,
            "root_class": go.class_name(root),
            "location": loc,
            "rotation": read_vector(snap, root + SCENE_RELATIVE_ROTATION),
            "velocity": read_vector(snap, root + SCENE_COMPONENT_VELOCITY),
        })
        if verbose:
            print(f"  controller [{i}] {cname} {oname!r} -> pawn "
                  f"{go.class_name(pawn)} {go.object_name(pawn)!r} via {via}")
    return results


def describe(p: dict) -> str:
    x, y, z = p["location"]
    out = [
        f"  controller   [{p['controller_index']}] {p['controller']:#014x} "
        f"{p['controller_class']}  {p['controller_name']!r}",
        f"  pawn         {p['pawn']:#014x}  {p['pawn_class']}  "
        f"{p['pawn_name']!r}   (via {p['pawn_via']})",
        f"  root comp    {p['root_component']:#014x}  {p['root_class']}",
        f"  LOCATION     X={x:14.3f}  Y={y:14.3f}  Z={z:14.3f}",
    ]
    if p["rotation"]:
        a, b, c = p["rotation"]
        out.append(f"  ROTATION     P={a:14.3f}  Y={b:14.3f}  R={c:14.3f}")
    if p["velocity"]:
        a, b, c = p["velocity"]
        out.append(f"  VELOCITY     X={a:14.3f}  Y={b:14.3f}  Z={c:14.3f}  "
                   f"|v|={math.dist((0,0,0),(a,b,c)):.2f}")
    return "\n".join(out)


def cmd_single(capture: Path, fallback: Path | None, verbose: bool) -> int:
    with Snapshot(capture) as snap:
        print(snap.stats())
        g, meta = load_meta(snap, capture, fallback, verbose)
        go = GObjects(snap, meta, g)
        print("\nwalking for the local player ...")
        found = find_local_player(snap, go, g, verbose)
        if not found:
            print("\nFAILED: no PlayerController resolved to a pawn with a "
                  "plausible position.")
            return 1
        print(f"\n{'='*72}")
        for p in found:
            print(describe(p))
            print()
        print(f"{'='*72}")
        print(f"{len(found)} local player candidate(s)")
    return 0


def cmd_compare(a_dir: Path, b_dir: Path, fallback: Path | None,
                verbose: bool) -> int:
    """The actual proof: same reader, two captures, known movement between them."""
    out = {}
    for label, d in (("A", a_dir), ("B", b_dir)):
        with Snapshot(d) as snap:
            g, meta = load_meta(snap, d, fallback, verbose)
            go = GObjects(snap, meta, g)
            found = find_local_player(snap, go, g, verbose=False)
            if not found:
                print(f"FAILED: no local player found in {d}")
                return 1
            out[label] = found
            print(f"--- {label}: {d.name} ({snap.label}) ---")
            for p in found:
                print(describe(p))
            print()

    # Match candidates by controller address so we compare like with like.
    a_by_ctrl = {p["controller"]: p for p in out["A"]}
    b_by_ctrl = {p["controller"]: p for p in out["B"]}
    shared = sorted(set(a_by_ctrl) & set(b_by_ctrl))
    if not shared:
        print("FAILED: no controller present in both captures -- cannot compare.")
        return 1

    print("=" * 72)
    print("MOVEMENT CHECK  (A standing still, B after running ~30s)")
    verdict_ok = False
    for ctrl in shared:
        pa, pb = a_by_ctrl[ctrl], b_by_ctrl[ctrl]
        ax, ay, az = pa["location"]
        bx, by, bz = pb["location"]
        d = math.dist((ax, ay, az), (bx, by, bz))
        dh = math.dist((ax, ay), (bx, by))
        print(f"\n  controller {ctrl:#014x}  {pa['pawn_class']} "
              f"{pa['pawn_name']!r}")
        print(f"    A  X={ax:12.2f} Y={ay:12.2f} Z={az:12.2f}")
        print(f"    B  X={bx:12.2f} Y={by:12.2f} Z={bz:12.2f}")
        print(f"    delta  horizontal {dh:11.2f}  vertical {bz-az:9.2f}  "
              f"total {d:11.2f}  ({d/100:.1f} m)")
        # A human running for ~30 s covers a long way but not a whole continent.
        if 1000 < dh < 5_000_000:
            print(f"    => PLAUSIBLE PLAYER MOVEMENT. This is the local player.")
            verdict_ok = True
        elif dh <= 1000:
            print(f"    => barely moved; not the pawn that ran "
                  f"(or the capture caught it mid-teleport)")
        else:
            print(f"    => implausibly far; suspect this is not a position")
    print("\n" + "=" * 72)
    if verdict_ok:
        print("VERDICT: position reader CONFIRMED against real movement.")
        return 0
    print("VERDICT: NOT CONFIRMED -- no candidate moved a plausible distance.")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="read the local player from a capture")
    ap.add_argument("capture", nargs="?", help="capture dir")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"),
                    help="two captures at known-different positions")
    ap.add_argument("--meta-from", help="capture dir to borrow GNames/GObjects "
                                        "metadata from (same process only)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    fallback = Path(args.meta_from) if args.meta_from else None
    if args.compare:
        return cmd_compare(Path(args.compare[0]), Path(args.compare[1]),
                           fallback, args.verbose)
    if not args.capture:
        ap.error("give a capture dir or --compare A B")
    return cmd_single(Path(args.capture), fallback, args.verbose)


if __name__ == "__main__":
    sys.exit(main())
