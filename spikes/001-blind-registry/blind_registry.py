#!/usr/bin/env python3
"""SPIKE 001 -- can we label non-reflected C++ objects BLIND, on an unknown build?

THE QUESTION
    AION 2's gameplay systems have ZERO reflected properties (plain C++ UObjects,
    invisible to UE reflection). Every offset we own for them -- the AionCharSession
    system registry, the targeting pair, the actor object id -- was derived by hand
    in the one 6-hour playtest window, using differential captures and live toggle
    correlation. Those numbers are build-specific: they will NOT survive the
    launch build, and there is no throwaway account to re-derive them on.

    So: can a pointer field on such an object be named WITHOUT any ground truth,
    by asking GUObjectArray what class the pointee is?

THE MECHANISM
    A subsystem pointer points at a real UObject. Every UObject carries
    ClassPrivate at +0x10, and that class object carries an FName. So for each
    8-byte-aligned slot in the anchor object:

        slot -> u64 -> is it a pointer? -> is the pointee a registered UObject?
             -> read pointee's ClassPrivate -> resolve FName -> that is the label

    No diffing. No ground truth. No live window. Works on any build where GNames
    and GUObjectArray can be found -- and both of those are already located by
    SCAN (aesteria_gnames.find_gnames / aesteria_gobjects.find_gobjects), not by
    hardcoded address, so the whole chain is build-independent by construction.

THE GATE IS THE WHOLE RESULT -- measured, not assumed
    A  pointee address is present in GUObjectArray  -- near-zero false positive
    B  pointee merely passes the UObject shape gate -- PURE NOISE, measured

    Both tiers were reported in the first run to find out which one the known
    answers land in. Every one of the 13 landed in tier A, and all 17 tier B hits
    resolved to no class name at all, with mojibake object names from reading the
    FName pool as if it were a name. So `valid_uobject` on its own is NOT a
    sufficient gate here: array membership is what makes the labelling reliable.
    Tier B is therefore OFF by default (--include-unregistered to see it).

FALSIFIABLE TEST
    Reproduce the AionCharSession registry offsets recorded on build
    AION2/1.0.21.0.2026031801 -- ObjectManager +56, SkillSystem +88,
    TargetingSystem +192, DamageMeter +352, and the rest -- from a banked capture,
    without the algorithm ever being told them. EXPECTED below is used ONLY to
    score the result afterwards; nothing in the scan reads it.

USAGE
    ./blind_registry.py --capture ../../captures/07-combat \
        --meta-from ../../captures/01-tutorial-in-world --expect
    ./blind_registry.py --pid <AION2 pid> --meta-from <capture-with-meta>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))

from aesteria_snapshot_reader import Snapshot                    # noqa: E402
from aesteria_live import LiveProcess, load_live_meta            # noqa: E402
from aesteria_gobjects import GObjects, valid_uobject            # noqa: E402
import aesteria_player as player                                 # noqa: E402

# Ground truth for SCORING ONLY -- build AION2/1.0.21.0.2026031801, derived by
# hand in the 2026-09-19 playtest window. The scan never reads this.
EXPECTED = {
    56: "ObjectManager",
    64: "PlayerActionSystem",
    80: "ItemSystem",
    88: "SkillSystem",
    96: "PartySystem",
    120: "QuestSystem",
    192: "TargetingSystem",
    200: "LootingSystem",
    216: "GatherSystem",
    224: "QuickSlotSystem",
    240: "DestinationSystem",
    248: "ItemAutoUseSystem",
    352: "DamageMeter",
}

DEFAULT_ANCHOR = "AionCharSession"
DEFAULT_WINDOW = 0x10000      # AionCharSession is 38,032 B on the playtest build


def find_anchor(src, go: GObjects, class_name: str) -> int | None:
    """The live instance of a class, skipping the class-default object."""
    for _, addr in go.iter_objects():
        if go.class_name(addr) != class_name:
            continue
        if (go.object_name(addr) or "").startswith("Default__"):
            continue
        return addr
    return None


def registered_addresses(go: GObjects) -> set[int]:
    return {addr for _, addr in go.iter_objects()}


def printable(s: str | None) -> str:
    """Garbage pointees yield UTF-16 mojibake; never let it reach a terminal."""
    if not s:
        return ""
    return "".join(c for c in s if 32 <= ord(c) < 127)[:48]


def scan_pointer_fields(src, go: GObjects, g, anchor: int, window: int,
                        known: set[int], include_unregistered: bool) -> list[dict]:
    """Label every pointer slot in [anchor, anchor+window) by its pointee class."""
    out = []
    for off in range(0, window, 8):
        val = src.u64(anchor + off)
        if val is None:
            break                       # ran off the end of mapped memory
        if not val or not src.looks_like_pointer(val):
            continue
        if val in known:
            tier = "A"
        elif include_unregistered and valid_uobject(src, val, g) is not None:
            tier = "B"
        else:
            continue
        out.append({
            "offset": off,
            "pointee": val,
            "tier": tier,
            "class": printable(go.class_name(val)) or "?",
            "name": printable(go.object_name(val)),
        })
    return out


def score(fields: list[dict]) -> tuple[int, int, list[str]]:
    """Compare against EXPECTED. Tolerant on naming (Aion- prefixes vary)."""
    by_off = {f["offset"]: f for f in fields}
    notes, hits = [], 0
    for off, want in sorted(EXPECTED.items()):
        got = by_off.get(off)
        if got is None:
            notes.append(f"  MISS   +{off:<5} expected {want:<22} -- no labelled pointer")
            continue
        cls = got["class"] or "?"
        # substring both ways: ObjectManager vs AionObjectManager
        ok = cls != "?" and (want.lower() in cls.lower()
                             or cls.lower() in want.lower())
        if ok:
            hits += 1
            notes.append(f"  MATCH  +{off:<5} {cls:<30} tier {got['tier']}")
        else:
            notes.append(f"  WRONG  +{off:<5} expected {want:<22} got {cls} (tier {got['tier']})")
    return hits, len(EXPECTED), notes


def report(src, go, g, args) -> int:
    anchor = find_anchor(src, go, args.anchor)
    if anchor is None:
        print(f"FAILED: no live {args.anchor} found")
        return 1
    print(f"anchor  {args.anchor} @ {anchor:#014x}")

    known = registered_addresses(go)
    print(f"        {len(known)} registered objects in GUObjectArray")

    fields = scan_pointer_fields(src, go, g, anchor, args.window, known,
                                 args.include_unregistered)
    a = sum(1 for f in fields if f["tier"] == "A")
    b = len(fields) - a
    print(f"        {len(fields)} labelled pointer fields "
          f"(tier A {a}, tier B {b}) in {args.window:#x} bytes\n")

    for f in fields[:args.top]:
        print(f"  +{f['offset']:<6} {f['tier']}  {f['pointee']:#014x}  "
              f"{f['class']:<34} {f['name']}")
    if len(fields) > args.top:
        print(f"  ... {len(fields) - args.top} more (raise --top)")

    if args.expect:
        hits, total, notes = score(fields)
        print(f"\nSCORING against {total} hand-derived offsets "
              f"(algorithm never reads these):")
        for n in notes:
            print(n)
        print(f"\n  {hits}/{total} reproduced blind")
        return 0 if hits == total else 2
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--capture", help="read a banked capture")
    ap.add_argument("--pid", type=int, help="read the live game")
    ap.add_argument("--meta-from", required=True,
                    help="capture dir holding gnames.json/gobjects.json")
    ap.add_argument("--anchor", default=DEFAULT_ANCHOR)
    ap.add_argument("--window", type=lambda s: int(s, 0), default=DEFAULT_WINDOW)
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--expect", action="store_true",
                    help="score against the hand-derived offsets")
    ap.add_argument("--include-unregistered", action="store_true",
                    help="also report tier B (measured to be pure noise)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    meta_from = Path(args.meta_from)
    if args.capture:
        cap = Path(args.capture)
        with Snapshot(cap) as snap:
            print(snap.stats())
            g, meta = player.load_meta(snap, cap, meta_from, args.verbose)
            return report(snap, GObjects(snap, meta, g), g, args)
    with LiveProcess(args.pid) as live:
        print(live.stats())
        g, meta = load_live_meta(live, meta_from, args.verbose)
        return report(live, GObjects(live, meta, g), g, args)


if __name__ == "__main__":
    sys.exit(main())
