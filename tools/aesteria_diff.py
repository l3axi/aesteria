#!/usr/bin/env python3
"""Differential field discovery: compare one object across two captures.

WHY THIS TOOL EXISTS
    The AION 2 gameplay systems -- TargetingSystem, AionObjectManager,
    SkillSystem, ItemAutoUseSystem -- are UObject-derived C++ classes with ZERO
    reflected properties. The SDK dump cannot see inside them, because there is
    nothing there for reflection to describe. Their fields can only be found by
    changing one thing in the game and seeing which bytes moved.

    So: capture with no target selected, capture with a target selected, diff the
    TargetingSystem object. A slot that changes from 0 to a pointer at a live
    AionCharacter is the current-target field, and it identifies itself.

HOW TO READ THE OUTPUT
    Changed slots are annotated with what they look like in EACH capture -- a
    pointer to a named object, a plausible float, a small integer. The annotation
    is the evidence; the offset alone proves nothing.

DISCIPLINE THIS ENCODES
    Two captures are two instants, and plenty of fields change between any two
    instants for reasons unrelated to what you were testing (timers, frame
    counters, cached transforms). So:
      - --control lets you pass a THIRD capture taken with the state UNCHANGED.
        Anything that also differs there is noise and gets marked as such. A
        difference only means something next to a control.
      - Prefer the narrowest possible change between the two captures.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from aesteria_snapshot_reader import Snapshot            # noqa: E402
from aesteria_gobjects import GObjects                    # noqa: E402
from aesteria_player import load_meta                     # noqa: E402


def annotate(snap: Snapshot, go: GObjects, raw: bytes, off: int) -> str:
    """Describe an 8-byte slot in whatever way actually fits it."""
    if off + 8 > len(raw):
        return ""
    q = struct.unpack_from("<Q", raw, off)[0]
    i1, i2 = struct.unpack_from("<ii", raw, off)
    f1, f2 = struct.unpack_from("<ff", raw, off)
    bits = [f"u64={q:#x}"]
    if snap.looks_like_pointer(q) and snap.is_mapped(q):
        cls = go.class_name(q)
        nm = go.object_name(q)
        if cls or nm:
            bits.append(f"-> {cls} {nm!r}")
        else:
            bits.append("-> ptr(mapped, unnamed)")
    if q == 0:
        bits.append("(null)")
    if -1_000_000 < i1 < 1_000_000 and i1 != 0:
        bits.append(f"i32[0]={i1}")
    if -1_000_000 < i2 < 1_000_000 and i2 != 0:
        bits.append(f"i32[1]={i2}")
    for lbl, f in (("f32[0]", f1), ("f32[1]", f2)):
        if f != 0 and not math.isnan(f) and not math.isinf(f) and 1e-4 < abs(f) < 1e7:
            bits.append(f"{lbl}={f:.4g}")
    return "  ".join(bits)


def find_object(snap: Snapshot, go: GObjects, class_name: str,
                skip_cdo: bool = True) -> list[int]:
    out = []
    for i, addr in go.iter_objects():
        if go.class_name(addr) != class_name:
            continue
        if skip_cdo and (go.object_name(addr) or "").startswith("Default__"):
            continue
        out.append(addr)
    return out


def load(cap: Path, fallback: Path | None, verbose: bool):
    snap = Snapshot(cap)
    g, meta = load_meta(snap, cap, fallback, verbose)
    return snap, GObjects(snap, meta, g), g


def main() -> int:
    ap = argparse.ArgumentParser(description="diff one object across captures")
    ap.add_argument("capture_a")
    ap.add_argument("capture_b")
    ap.add_argument("--control", help="third capture with the state UNCHANGED "
                                     "relative to A; differences seen here too "
                                     "are noise")
    ap.add_argument("--class", dest="cls", required=True,
                    help="class name of the object to diff, e.g. TargetingSystem")
    ap.add_argument("--addr", type=lambda s: int(s, 0),
                    help="explicit object address instead of searching by class")
    ap.add_argument("--size", type=lambda s: int(s, 0), default=0x800,
                    help="bytes to compare (default 0x800)")
    ap.add_argument("--meta-from", help="borrow GNames/GObjects metadata")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    fallback = Path(args.meta_from) if args.meta_from else None
    snap_a, go_a, _ = load(Path(args.capture_a), fallback, args.verbose)
    snap_b, go_b, _ = load(Path(args.capture_b), fallback, args.verbose)

    if args.addr:
        addrs = [args.addr]
    else:
        addrs = find_object(snap_a, go_a, args.cls)
        if not addrs:
            print(f"no live {args.cls} instance found in {args.capture_a}")
            return 1
    print(f"{args.cls}: {len(addrs)} live instance(s): "
          f"{', '.join(hex(a) for a in addrs)}")

    control_raw: dict[int, bytes] = {}
    if args.control:
        snap_c, _, _ = load(Path(args.control), fallback, args.verbose)
        for a in addrs:
            r = snap_c.read(a, args.size)
            if r:
                control_raw[a] = r
        print(f"control: {args.control}")

    total_changes = 0
    for a in addrs:
        ra = snap_a.read(a, args.size)
        rb = snap_b.read(a, args.size)
        if ra is None or rb is None:
            print(f"\n{a:#x}: not fully readable in both captures, skipping")
            continue
        rc = control_raw.get(a)
        print(f"\n{'='*74}")
        print(f"  {args.cls} @ {a:#x}   comparing {args.size:#x} bytes")
        print(f"{'='*74}")
        changed = noisy = 0
        for off in range(0, min(len(ra), len(rb)) - 7, 8):
            if ra[off:off+8] == rb[off:off+8]:
                continue
            changed += 1
            is_noise = rc is not None and rc[off:off+8] != ra[off:off+8]
            if is_noise:
                noisy += 1
            tag = "  [noise: also differs in control]" if is_noise else ""
            print(f"  +{off:#06x}{tag}")
            print(f"      A  {annotate(snap_a, go_a, ra, off)}")
            print(f"      B  {annotate(snap_b, go_b, rb, off)}")
        print(f"\n  {changed} changed 8-byte slots"
              + (f", {noisy} of them also change in the control "
                 f"({changed-noisy} SIGNAL)" if rc is not None else ""))
        total_changes += changed
    if not args.control:
        print("\nNOTE: no --control given, so every difference here is a "
              "candidate, not a finding.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
