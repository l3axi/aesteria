#!/usr/bin/env python3
"""Level-up differential for vitals: find max HP by the one number a level-up moves.

WHY THIS TOOL EXISTS
    Four earlier approaches to HP failed.
    A level-up is the narrowest differential this game hands us for free: max HP
    changes, current HP refills to it, and almost nothing else about the character
    moves. So a slot that reads the old max in the pre-level-up captures and the
    new max in the post-level-up capture is max HP, and it identifies itself.

    Ground truth collected live by the operator:
        level 8 -> 631/1513   (captures 06-targeted, 07-combat)
        level 9 -> 1621/1621  (capture  08-post-levelup)

RESULT, 2026-09-19 -- NEGATIVE, AND THE NEGATIVE IS THE POINT
    Zero surviving slots at u32, f32, u64 or f64. And the counts explain why
    value-hunting was never going to work here: the literal 1621 occurs ~7,700-7,900
    times in EVERY capture, including the level-8 ones, so the value carries no
    signal on its own. 1513 likewise occurs ~7,600 times per capture, and 93% of
    those addresses still hold 1513 after the level-up -- they are unrelated small
    integers that happen to share the value.

    So max HP is not a plain integer at a stable address, which puts it in the same
    category as XOR-obfuscated vitals seen in other games: find the CONTAINER by its shape, or
    find HP in the game-object layer behind AionObjectManager. Both remain open.

    Keep this tool. When the container hypothesis produces a candidate, this is the
    cheapest way to confirm it: the candidate must decode to 1513 in 07 and 1621 in
    08 from the same chain.

USAGE
    aesteria_hp_levelup.py --before 07-combat --control 06-targeted \
                           --after 08-post-levelup --before-value 1513 \
                           --after-value 1621
"""

from __future__ import annotations

import argparse
import struct
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from aesteria_snapshot_reader import Snapshot            # noqa: E402

CAPTURES = Path(__file__).resolve().parent.parent / "captures"

# Every width a UE5/MMO codebase plausibly stores a vital in. Searching only u32
# would let a float-backed stat hide, and the scan is cheap enough not to guess.
ENCODINGS: dict[str, tuple[str, int]] = {
    "u32": ("<I", 4),
    "f32": ("<f", 4),
    "u64": ("<Q", 8),
    "f64": ("<d", 8),
}


def resolve(name: str) -> Snapshot:
    path = Path(name)
    if not path.exists():
        path = CAPTURES / name
    return Snapshot(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--before", required=True, help="capture at the OLD level")
    ap.add_argument("--after", required=True, help="capture at the NEW level")
    ap.add_argument("--control", help="second capture at the OLD level; a slot that "
                                      "does not also hold the old value here was "
                                      "never stable and is rejected")
    ap.add_argument("--before-value", type=float, required=True)
    ap.add_argument("--after-value", type=float, required=True)
    ap.add_argument("--census", action="store_true",
                    help="also report how common each value is per capture -- the "
                         "check that tells you whether the value means anything")
    args = ap.parse_args()

    before, after = resolve(args.before), resolve(args.after)
    control = resolve(args.control) if args.control else None

    pids = {"before": before.pid, "after": after.pid}
    if control:
        pids["control"] = control.pid
    print("pids: " + "  ".join(f"{k}={v}" for k, v in pids.items()))
    if len(set(pids.values())) != 1:
        # Different runs mean different ASLR bases; comparing addresses across them
        # would produce confident nonsense rather than an error.
        print("!! captures come from different processes -- addresses are not "
              "comparable. Stopping.")
        return 2

    for name, (fmt, width) in ENCODINGS.items():
        try:
            pat_b = struct.pack(fmt, _coerce(fmt, args.before_value))
            pat_a = struct.pack(fmt, _coerce(fmt, args.after_value))
        except (struct.error, ValueError):
            continue

        hits = list(before.find(pat_b, perms="w"))
        moved: list[int] = []
        stayed = 0
        for addr in hits:
            if after.read(addr, width) != pat_a:
                if after.read(addr, width) == pat_b:
                    stayed += 1
                continue
            if control is not None and control.read(addr, width) != pat_b:
                continue
            moved.append(addr)

        print(f"{name}: {len(hits)} slots held {args.before_value:g} before; "
              f"{stayed} still do after (unrelated); "
              f"{len(moved)} moved to {args.after_value:g}"
              + (" and survived the control" if control else ""))
        for addr in moved[:40]:
            seg = before.segs[before._seg_index(addr)]
            print(f"  {addr:#x}  perms={seg.perms}  "
                  f"backing={seg.backing or 'anon'}")
            for label, snap in (("before", before), ("after", after)):
                ctx = snap.read(addr - 32, 96)
                if ctx and len(ctx) >= 96:
                    words = struct.unpack_from("<24I", ctx)
                    print(f"    {label}: " + " ".join(str(w) for w in words))
        if len(moved) > 40:
            print(f"  ... {len(moved) - 40} more")

    if args.census:
        print("\ncensus -- how often each value simply occurs:")
        for value in (args.before_value, args.after_value):
            pat = struct.pack("<I", int(value))
            for label, snap in (("before", before), ("after", after),
                                ("control", control)):
                if snap is None:
                    continue
                n = sum(1 for _ in snap.find(pat, perms="w"))
                print(f"  u32 {value:g} in {label} ({snap.root.name}): {n}")

    return 0


def _coerce(fmt: str, value: float):
    return value if fmt in ("<f", "<d") else int(value)


if __name__ == "__main__":
    raise SystemExit(main())
