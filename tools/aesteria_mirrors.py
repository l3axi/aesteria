#!/usr/bin/env python3
"""Find non-reflected SCALAR fields by cross-object agreement. No ground truth.

THE GAP THIS CLOSES
    aesteria_registry.py recovers every POINTER field of a plain C++ UObject blind,
    which took the system registry off the live-window list. It cannot see scalars,
    so these stayed hand-derived and build-specific:

        AionCharSession +0x0eb8 / +0x0ec0   has_target / target id
        SkillSystem     +0x0598 / +0x05a0   the same pair, mirrored
        player pawn     +0x2e88 / +0x2e90   mirrored again
        character actor +0x26a0             that actor's own object id

    Look at what those have in common: the SAME VALUE is stored in more than one
    independently-locatable object. That redundancy is a signature, and a signature
    needs no ground truth -- we do not have to know the object id to notice that two
    objects agree about it.

THE METHOD
    Give two or more anchor classes and two captures in different states. Keep the
    offset tuples where:

        1. all anchors hold the SAME value as each other, in the "on" capture, and
        2. that value DIFFERS in the "off" capture.

    Condition 2 is what makes it work. Agreement alone returns thousands of hits,
    because small integers like 0, 1 and 2 agree everywhere by accident -- measured:
    4,408 surviving pairs on agreement alone, 52 once the value has to change, and
    exactly the right ones once trivial values are excluded.

VALIDATED 2026-09-29 against the one scalar already known by hand:
    anchors AionObjectManager + the player pawn, captures 07-combat (object id 2537)
    and 01-tutorial-in-world (object id 1298) -- both sessions' ids differ, so the
    id itself is the thing that changed. Recovered:

        AionObjectManager+56  <->  pawn+0x26a0     <- the hand-derived offset
        AionObjectManager+56  <->  pawn+0x2540     <- a second mirror, previously unknown

    It also recovered the local player's object id living on AionObjectManager at
    all, which no earlier pass had noticed.

TWO FILTERS, both of them earned rather than chosen
    --min-value (default 1024): an "identity" is a wide number. Values like 2 or 24
      changing between sessions is ordinary bookkeeping and produced all 50 of the
      false positives in the validation run.
    The UObject header (first 0x28 bytes) is skipped: InternalIndex at +0xc changes
      between sessions and agreed with an unrelated ObjectManager slot, which is a
      true agreement about something that is not a game value.

⚠ THIS FINDS REDUNDANTLY-STORED SCALARS ONLY.
    A value held in exactly one place has no mirror to agree with and is invisible
    here. HP is the live example: it is not merely unmirrored, it is not plainly
    stored at all (see aesteria_hp_ratio.py).

USAGE
    # the validation case
    ./aesteria_mirrors.py --anchor AionObjectManager --pawn \
        --on 07-combat --off 01-tutorial-in-world

    # the targeting pair: nothing targeted vs something targeted
    ./aesteria_mirrors.py --anchor AionCharSession --anchor SkillSystem --pawn \
        --on 06-targeted --off 05-no-target --min-value 1
"""

from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aesteria_snapshot_reader import Snapshot                     # noqa: E402
from aesteria_gobjects import GObjects                            # noqa: E402
import aesteria_player as player                                  # noqa: E402
import aesteria_registry as reg                                   # noqa: E402

CAPTURES = Path(__file__).resolve().parents[1] / "captures"
DEFAULT_META = CAPTURES / "01-tutorial-in-world"

UOBJECT_HEADER = 0x28             # vtable/flags/index/class/name/outer: not game data
PAWN_SIZE_FALLBACK = 14416
MAX_ID = 1 << 24                  # ids above this are not plausible game identities


def anchors_in(src, go: GObjects, classes: list[str],
               include_pawn: bool, g) -> dict[str, tuple[int, int]]:
    """Resolve each anchor class (and optionally the pawn) to (addr, size)."""
    out: dict[str, tuple[int, int]] = {}
    for cls in classes:
        addr = reg.find_instance(go, cls)
        if addr is None:
            raise RuntimeError(f"no live {cls}")
        out[cls] = (addr, reg.object_size(src, go, addr) or 0)
    if include_pawn:
        me = player.find_local_player(src, go, g)
        if not me:
            raise RuntimeError("no local player")
        pawn = me[0]["pawn"]
        out["pawn"] = (pawn, reg.object_size(src, go, pawn) or PAWN_SIZE_FALLBACK)
    return out


def read_scalars(src, addr: int, size: int, min_value: int) -> dict[int, int]:
    """Every 4-byte-aligned u32 in a plausible identity range, past the header."""
    vals = {}
    for off in range(UOBJECT_HEADER, size, 4):
        v = src.u32(addr + off)
        if v is None:
            break
        if min_value <= v < MAX_ID:
            vals[off] = v
    return vals


def agreeing(tables: dict[str, dict[int, int]]) -> dict[tuple, int]:
    """Offset tuples (one per anchor, in key order) whose values are all equal."""
    names = list(tables)
    by_value: list[dict[int, list[int]]] = []
    for n in names:
        inv: dict[int, list[int]] = {}
        for off, v in tables[n].items():
            inv.setdefault(v, []).append(off)
        by_value.append(inv)
    shared = set(by_value[0])
    for inv in by_value[1:]:
        shared &= set(inv)
    out: dict[tuple, int] = {}
    for v in shared:
        for combo in itertools.product(*(inv[v] for inv in by_value)):
            out[combo] = v
    return out


def snapshot_state(capture: Path, meta_from: Path, classes: list[str],
                   include_pawn: bool, min_value: int) -> dict[tuple, int]:
    with Snapshot(capture) as s:
        g, meta = player.load_meta(s, capture, meta_from, False)
        go = GObjects(s, meta, g)
        anc = anchors_in(s, go, classes, include_pawn, g)
        tables = {n: read_scalars(s, a, sz, min_value) for n, (a, sz) in anc.items()}
        return agreeing(tables)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--anchor", action="append", default=[],
                    help="anchor class (repeatable)")
    ap.add_argument("--pawn", action="store_true", help="also use the player pawn")
    ap.add_argument("--on", required=True, help="capture where the value is set")
    ap.add_argument("--off", required=True,
                    help="capture where it differs (unset, or a different value)")
    ap.add_argument("--meta-from", type=Path, default=DEFAULT_META)
    ap.add_argument("--min-value", type=int, default=1024,
                    help="ignore values below this; small ints agree by accident")
    ap.add_argument("--top", type=int, default=40)
    args = ap.parse_args()

    if len(args.anchor) + (1 if args.pawn else 0) < 2:
        print("need at least two anchors (use --anchor twice, or --anchor + --pawn)",
              file=sys.stderr)
        return 2

    names = list(args.anchor) + (["pawn"] if args.pawn else [])
    print(f"anchors: {', '.join(names)}")
    print(f"on={args.on}  off={args.off}  min-value={args.min_value}\n")

    on = snapshot_state(CAPTURES / args.on, args.meta_from, args.anchor,
                        args.pawn, args.min_value)
    print(f"  {len(on):,} agreeing offset tuples in {args.on}")
    off = snapshot_state(CAPTURES / args.off, args.meta_from, args.anchor,
                         args.pawn, args.min_value)
    print(f"  {len(off):,} agreeing offset tuples in {args.off}")

    # Survivors: agreed in `on`, and in `off` either did not agree at all or agreed
    # on a different value. Both are the value having changed, which is the point.
    surv = {k: (v, off.get(k)) for k, v in on.items() if off.get(k) != v}
    print(f"\n{len(surv)} tuple(s) where the agreed value CHANGED:\n")
    for combo, (v_on, v_off) in sorted(surv.items())[:args.top]:
        desc = "  ".join(f"{n}+{o:#x}" for n, o in zip(names, combo))
        print(f"  {desc}    on={v_on}  off={v_off}")
    if len(surv) > args.top:
        print(f"  ... {len(surv) - args.top} more (raise --top)")
    if not surv:
        print("  none -- no scalar is redundantly stored across these anchors in "
              "these two states")
    return 0


if __name__ == "__main__":
    sys.exit(main())
