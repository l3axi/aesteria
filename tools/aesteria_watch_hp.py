#!/usr/bin/env python3
"""Find HP by its shape: a value that falls monotonically toward zero.

WHY NOT THE TOGGLE TRICK
    aesteria_watch.py finds two-state fields by making them alternate. HP is not
    two-state, so that signature does not apply. But HP has a signature of its
    own that is just as hard to fake: while you kill a mob, its current HP falls
    step by step and ends at 0, and it NEVER goes up. Across a whole fight, very
    little else in a client does that.

WHAT IT LOOKS FOR, per 4-byte slot, as u32 and as f32:
  - DECLINING : >=3 distinct values, never increases, ends at or near zero.
                This is the kill signature -- strongest evidence.
  - BOUNDED   : changes repeatedly but stays inside a fixed range, and a nearby
                slot holds a constant that looks like that range's ceiling.
                This is the current/max pair -- how HP is almost always stored.
  - CONSTANT neighbours of a declining slot are reported alongside it, because
                max-HP sits next to current-HP far more often than not.

It follows the CURRENT TARGET, re-resolving it each tick via the targeting stack
(AionCharSession +0xec0 -> actor with that id at +0x26a0), so it keeps working
across mob deaths and retargets rather than watching one dead pointer.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from aesteria_live import LiveProcess, load_live_meta      # noqa: E402
from aesteria_gobjects import GObjects                      # noqa: E402
import aesteria_player as player                            # noqa: E402
import aesteria_target as tgt                               # noqa: E402

WATCH_BYTES = 0x3000


def analyse(series: dict[int, list], label: str, out: list[str]) -> None:
    """Classify each slot's value history."""
    declining, bounded, constants = [], [], {}
    for off, vals in series.items():
        if len(vals) < 4:
            continue
        uniq = len(set(vals))
        if uniq == 1:
            constants[off] = vals[0]
            continue
        if uniq < 3:
            continue
        # Ignore anything that looks like a counter: strictly climbing forever.
        non_increasing = all(b <= a for a, b in zip(vals, vals[1:]))
        non_decreasing = all(b >= a for a, b in zip(vals, vals[1:]))
        lo, hi = min(vals), max(vals)
        if non_increasing and lo <= max(1, hi * 0.05):
            declining.append((off, vals, uniq))
        elif not non_decreasing and 0 <= lo and hi < 1 << 31:
            # changes both ways inside a range -- current HP regenerating/taking
            # damage looks like this
            bounded.append((off, vals, uniq, lo, hi))

    if declining:
        out.append(f"\n  === {label}: DECLINING TO ZERO (kill signature) ===")
        for off, vals, uniq in sorted(declining, key=lambda d: -d[2])[:12]:
            head = ", ".join(str(v) for v in vals[:6])
            tail = ", ".join(str(v) for v in vals[-3:])
            out.append(f"    +{off:#07x}  {uniq:>3} distinct: {head} ... {tail}")
            for probe in (off - 8, off - 4, off + 4, off + 8):
                if probe in constants and constants[probe] > 0:
                    out.append(f"        neighbour +{probe:#07x} CONSTANT "
                               f"{constants[probe]}  <- possible max")
    if bounded:
        out.append(f"\n  === {label}: BOUNDED, CHANGES BOTH WAYS ===")
        for off, vals, uniq, lo, hi in sorted(bounded, key=lambda d: -d[2])[:12]:
            head = ", ".join(str(v) for v in vals[:6])
            out.append(f"    +{off:#07x}  {uniq:>3} distinct, range {lo}..{hi}: "
                       f"{head} ...")
            for probe in (off - 8, off - 4, off + 4, off + 8):
                if probe in constants and constants[probe] >= hi > 0:
                    out.append(f"        neighbour +{probe:#07x} CONSTANT "
                               f"{constants[probe]}  <- possible max")
    if not declining and not bounded:
        out.append(f"\n  === {label}: nothing matched. ===")


def main() -> int:
    ap = argparse.ArgumentParser(description="find HP by its decline to zero")
    ap.add_argument("--meta-from", required=True)
    ap.add_argument("--pid", type=int)
    ap.add_argument("--seconds", type=float, default=90.0)
    ap.add_argument("--interval", type=float, default=0.2)
    ap.add_argument("-o", "--out")
    args = ap.parse_args()

    with LiveProcess(args.pid) as live:
        print(live.stats())
        g, meta = load_live_meta(live, Path(args.meta_from), False)
        go = GObjects(live, meta, g)
        session = tgt.find_session(live, go)
        me = player.find_local_player(live, go, g)
        if session is None or not me:
            print("could not resolve session/player")
            return 1
        pawn = me[0]["pawn"]
        print(f"  session {session:#x}   pawn {pawn:#x}")

        # id -> actor address, built once; mobs we fight are already spawned.
        id_map: dict[int, int] = {}
        for i, a in go.iter_objects():
            if tgt.is_character(go.class_name(a)):
                oid = live.u32(a + tgt.ACTOR_OBJECT_ID)
                if oid and 0 < oid < tgt.MAX_SANE_OBJECT_ID:
                    id_map[oid] = a
        print(f"  {len(id_map)} characters indexed by object id")
        print(f"\n>>> ATTACK AND KILL A MOB. Take a few hits too. "
              f"{args.seconds:.0f}s <<<\n")

        pawn_series: dict[int, list] = {}
        targ_series: dict[int, dict[int, list]] = {}
        prev_pawn = prev_targ = None
        cur_target_addr = None
        seen_targets = []
        t_end = time.time() + args.seconds
        samples = 0
        while time.time() < t_end:
            raw = live.read(pawn, WATCH_BYTES)
            if raw is not None:
                if prev_pawn is None:
                    for off in range(0, len(raw) - 3, 4):
                        pawn_series[off] = [struct.unpack_from("<I", raw, off)[0]]
                else:
                    for off in range(0, len(raw) - 3, 4):
                        v = struct.unpack_from("<I", raw, off)[0]
                        s = pawn_series.setdefault(off, [])
                        if not s or s[-1] != v:
                            s.append(v)
                prev_pawn = raw

            tid = live.u32(session + tgt.SESSION_TARGET_ID) or 0
            addr = id_map.get(tid)
            if addr:
                if addr != cur_target_addr:
                    cur_target_addr = addr
                    if addr not in targ_series:
                        targ_series[addr] = {}
                        seen_targets.append((tid, addr))
                    prev_targ = None
                raw = live.read(addr, WATCH_BYTES)
                if raw is not None:
                    st = targ_series[addr]
                    for off in range(0, len(raw) - 3, 4):
                        v = struct.unpack_from("<I", raw, off)[0]
                        s = st.setdefault(off, [])
                        if not s or s[-1] != v:
                            s.append(v)
                    prev_targ = raw
            samples += 1
            time.sleep(args.interval)

        out = [f"{samples} samples", f"targets seen: "
               + ", ".join(f"id={t} @ {a:#x}" for t, a in seen_targets)]
        analyse(pawn_series, f"PLAYER PAWN {pawn:#x}", out)
        for tid, addr in seen_targets:
            analyse(targ_series[addr], f"TARGET id={tid} @ {addr:#x}", out)
        report = "\n".join(out)
        print(report)
        if args.out:
            Path(args.out).write_text(report)
            print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
