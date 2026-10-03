#!/usr/bin/env python3
"""Live differential recorder: find a field by making it TOGGLE.

WHY THIS BEATS DIFFING TWO CAPTURES
    A single before/after diff of a combat state changes hundreds of slots --
    VFX handles, animation state, counters, timers -- and a candidate that
    survives one control is still mostly luck. We spent three diffs learning that
    the hard way; the best candidate (AionCharSession+0xf20) went null->value on
    targeting and then back to NULL during combat, which killed it.

    So instead: make the operator toggle ONE game state repeatedly while we poll.
    A field that means "current target" must alternate between exactly two values
    in lockstep with the toggling. Nothing else in a running MMO client does that.
    Frame counters climb, timers drift, VFX handles churn randomly -- none of them
    alternate cleanly N times between two fixed values.

    This is the same principle as bracketing the position reader, just applied
    repeatedly: correlation across many trials instead of one.

USAGE
    Start it, then target and clear the SAME mob about once a second:

        aesteria_watch.py --meta-from captures/01-tutorial-in-world \\
                          --seconds 60 --interval 0.25

Reads only. /proc/<pid>/mem, O_RDONLY, no injection.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from aesteria_live import LiveProcess, load_live_meta      # noqa: E402
from aesteria_gobjects import GObjects                      # noqa: E402
import aesteria_player as player                            # noqa: E402

# What to watch, and how far into each. Sizes are generous -- watching spare
# bytes past the end of an object costs a little memory and nothing else.
WATCH_CLASSES = [
    ("AionCharSession", 0x9500),
    ("TargetingSystem", 0x4000),
    ("AionObjectManager", 0x2000),
    ("SkillSystem", 0x1000),
    ("LootingSystem", 0x800),
]


def resolve_targets(live, go, g, verbose: bool):
    """Find one live instance of each watched class, plus the player pawn."""
    want = {c for c, _ in WATCH_CLASSES}
    sizes = dict(WATCH_CLASSES)
    found: dict[str, tuple[int, int]] = {}
    for i, addr in go.iter_objects():
        cn = go.class_name(addr)
        if cn in want and cn not in found:
            if (go.object_name(addr) or "").startswith("Default__"):
                continue
            found[cn] = (addr, sizes[cn])
            if len(found) == len(want):
                break
    players = player.find_local_player(live, go, g)
    if players:
        p = players[0]
        found["PlayerPawn"] = (p["pawn"], 0x4000)
        found["PlayerController"] = (p["controller"], 0x2000)
    if verbose:
        for k, (a, sz) in found.items():
            print(f"  watching {k:<20} {a:#014x}  {sz:#x} bytes")
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description="find a field by toggling it")
    ap.add_argument("--pid", type=int)
    ap.add_argument("--meta-from", required=True)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--interval", type=float, default=0.25)
    ap.add_argument("--min-toggles", type=int, default=4,
                    help="minimum alternations to report a slot")
    ap.add_argument("-o", "--out", help="write the full report here")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    with LiveProcess(args.pid) as live:
        print(live.stats())
        g, meta = load_live_meta(live, Path(args.meta_from), args.verbose)
        go = GObjects(live, meta, g)
        targets = resolve_targets(live, go, g, True)
        if not targets:
            print("nothing to watch")
            return 1

        print(f"\nPolling every {args.interval}s for {args.seconds}s.")
        print(">>> TARGET AND CLEAR THE SAME MOB, about once a second. <<<\n")

        # history[key][offset] = list of values seen, in order
        history: dict[str, dict[int, list[int]]] = {k: {} for k in targets}
        samples = 0
        t_end = time.time() + args.seconds
        last: dict[str, bytes] = {}
        while time.time() < t_end:
            for key, (addr, size) in targets.items():
                raw = live.read(addr, size)
                if raw is None:
                    continue
                prev = last.get(key)
                if prev is not None and len(prev) == len(raw):
                    for off in range(0, len(raw) - 7, 8):
                        if prev[off:off+8] != raw[off:off+8]:
                            v = struct.unpack_from("<Q", raw, off)[0]
                            history[key].setdefault(off, []).append(v)
                last[key] = raw
            samples += 1
            time.sleep(args.interval)

        print(f"{samples} samples taken\n")

        lines = []
        for key in targets:
            hits = []
            for off, vals in history[key].items():
                uniq = Counter(vals)
                # THE SIGNATURE: a field driven by a two-state toggle takes a
                # small number of DISTINCT values, and takes each of them more
                # than once. Counters and timers take a new value every sample;
                # random churn takes many values once each.
                if len(uniq) > 4:
                    continue
                repeated = sum(1 for v, n in uniq.items() if n >= 2)
                if repeated < 2:
                    continue
                if len(vals) < args.min_toggles:
                    continue
                hits.append((off, uniq, len(vals)))
            if not hits:
                continue
            addr, _ = targets[key]
            lines.append(f"\n=== {key} @ {addr:#x} ===")
            for off, uniq, n in sorted(hits, key=lambda h: -h[2])[:25]:
                vals_s = ", ".join(
                    f"{v:#x}(x{c})" for v, c in uniq.most_common(4))
                note = ""
                for v in uniq:
                    if v and live.looks_like_pointer(v) and live.is_mapped(v):
                        cn = go.class_name(v)
                        if cn:
                            note = f"   -> {cn}"
                            break
                lines.append(f"  +{off:#07x}  {n:>3} changes, "
                             f"{len(uniq)} distinct: {vals_s}{note}")
        report = "\n".join(lines) if lines else \
            "\nNo slot alternated between a small set of values.\n" \
            "Either the toggling did not happen, or the state is not held as a\n" \
            "simple two-value field in the watched objects."
        print(report)
        if args.out:
            Path(args.out).write_text(report)
            print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
