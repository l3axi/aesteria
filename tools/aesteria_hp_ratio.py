#!/usr/bin/env python3
"""HP approach 6, offline: hunt the RATIO rather than the value.

WHY THIS IS NOT APPROACH 5 AGAIN
    Approach 5 searched every writable byte for the literal max-HP values (1513 at
    level 8, 1621 at level 9) as u32/f32/u64/f64, with a stability control, and found
    zero surviving slots. That negative is sound -- the player pawn sits at the SAME
    address in both captures (0x77624030, object id 2537, verified 2026-09-29), so
    the test was not defeated by a reallocation. Max HP really is not a plain number
    at a stable address.

    But nothing has ever searched for the RATIO, and the evidence points straight at
    it. The one place HP was ever observed was the HUD path
    `HUD_UserInfo_C +1552 -> CharResUpdater -> ProgressBar.Percent` -- a PERCENT, not
    a pair of integers. A bar that is bound to a normalised float does not need the
    raw pair to exist anywhere the renderer can see.

    Ground truth gives a three-point signature for free:
        06-targeted    level 8, 631/1513  -> 0.41705      (stability control)
        07-combat      level 8, 631/1513  -> 0.41705
        08-post-levelup level 9, 1621/1621 -> 1.0  exactly

    A slot that holds ~0.417 in two captures and exactly 1.0 in the third is a
    current/max ratio. Requiring all three is what makes the hit rate survivable:
    0.417 alone matches a lot of unrelated floats, and 1.0 matches an enormous
    number, but the CONJUNCTION at one address is narrow.

⚠ THIS CAN ONLY FAIL HONESTLY, NOT CONCLUSIVELY.
    A null result rules out "a normalised current/max float at a stable address". It
    does NOT rule out the ratio being computed on demand, stored per-frame in a
    transient, or encoded. Do not write a null result up as "HP is encoded".

⚠ THE SCREEN-READ TOLERANCE IS PART OF THE EXPERIMENT.
    631 was read off a screen by a human while HP regenerated. The true numerator at
    capture time is unknown within a few HP, so the numerator tolerance is wide by
    necessity (--tol, default 0.01 ≈ ±15 HP) while the full-HP end is exact. Widening
    tolerance costs candidates, not correctness, because the 1.0 constraint does the
    discriminating.

USAGE
    ./aesteria_hp_ratio.py                      # defaults: 06/07 vs 08
    ./aesteria_hp_ratio.py --tol 0.02 --scale pct
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aesteria_snapshot_reader import Snapshot                     # noqa: E402

CAPTURES = Path(__file__).resolve().parents[1] / "captures"

# Ground truth, collected live by the operator in the 2026-09-19 playtest window.
CUR_BEFORE, MAX_BEFORE = 631, 1513
MAX_AFTER = 1621

# UE reserves huge mostly-untouched arenas; faulting them in costs more than it can
# find. Same cap as aesteria_hpscan.
MAX_REGION = 1 << 30


def ratio_targets(scale: str) -> tuple[float, float]:
    """(value expected in the level-8 captures, value expected after the level-up)."""
    r = CUR_BEFORE / MAX_BEFORE                      # 0.4170522...
    full = MAX_AFTER / MAX_AFTER                     # 1.0
    if scale == "pct":
        return r * 100.0, full * 100.0
    return r, full


def scan_for(snap: Snapshot, target: float, tol: float, dtype: str,
             verbose: bool) -> np.ndarray:
    """Every address whose dtype-typed value is within tol of target."""
    itemsize = np.dtype(dtype).itemsize
    out: list[np.ndarray] = []
    for i, seg in enumerate(snap.segs):
        if seg.size > MAX_REGION or seg.size < itemsize:
            continue
        if "r" not in seg.perms:
            continue
        try:
            arr = np.memmap(seg.path, dtype=dtype, mode="r",
                            shape=(seg.size // itemsize,))
        except (OSError, ValueError):
            continue
        with np.errstate(invalid="ignore"):
            hit = np.flatnonzero(np.abs(arr - target) <= tol)
        if hit.size:
            out.append(seg.start + hit.astype(np.int64) * itemsize)
        del arr
    if not out:
        return np.empty(0, dtype=np.int64)
    res = np.concatenate(out)
    if verbose:
        print(f"    {dtype}: {res.size:,} slots within {tol} of {target:.6f}")
    return res


def survives(snap: Snapshot, addrs: np.ndarray, target: float, tol: float,
             dtype: str) -> np.ndarray:
    """Keep addresses that also read ~target in this capture."""
    fmt = {"float32": "f32", "float64": "f64"}[dtype]
    keep = []
    for a in addrs.tolist():
        v = snap.f32(int(a)) if fmt == "f32" else None
        if fmt == "f64":
            raw = snap.read(int(a), 8)
            v = None if raw is None else np.frombuffer(raw, dtype="<f8")[0]
        if v is not None and abs(v - target) <= tol:
            keep.append(a)
    return np.array(keep, dtype=np.int64)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--before", default="07-combat")
    ap.add_argument("--control", default="06-targeted")
    ap.add_argument("--after", default="08-post-levelup")
    ap.add_argument("--scale", choices=("unit", "pct"), default="unit")
    ap.add_argument("--tol", type=float, default=0.01,
                    help="tolerance on the partial-HP end (default 0.01 ~ +-15 HP)")
    ap.add_argument("--full-tol", type=float, default=1e-6,
                    help="tolerance on the full-HP end; keep tight, it discriminates")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    r_partial, r_full = ratio_targets(args.scale)
    tol = args.tol * (100.0 if args.scale == "pct" else 1.0)
    full_tol = args.full_tol * (100.0 if args.scale == "pct" else 1.0)
    print(f"scale={args.scale}  partial={r_partial:.6f}+-{tol}  "
          f"full={r_full:.6f}+-{full_tol}")
    print(f"  {CUR_BEFORE}/{MAX_BEFORE} in {args.before} and {args.control}, "
          f"{MAX_AFTER}/{MAX_AFTER} in {args.after}\n")

    total = 0
    for dtype in ("float32", "float64"):
        print(f"{dtype}:")
        with Snapshot(CAPTURES / args.before) as before:
            cands = scan_for(before, r_partial, tol, dtype, args.verbose)
        print(f"  {cands.size:,} slots ~{r_partial:.4f} in {args.before}")
        if not cands.size:
            continue

        with Snapshot(CAPTURES / args.after) as after:
            cands = survives(after, cands, r_full, full_tol, dtype)
        print(f"  {cands.size:,} of those read {r_full} in {args.after}")
        if not cands.size:
            continue

        with Snapshot(CAPTURES / args.control) as control:
            cands = survives(control, cands, r_partial, tol, dtype)
        print(f"  {cands.size:,} also ~{r_partial:.4f} in the control "
              f"{args.control}")

        for a in cands.tolist()[:40]:
            print(f"    {a:#014x}")
        if cands.size > 40:
            print(f"    ... {cands.size - 40} more")
        total += int(cands.size)

    print(f"\n{total} surviving candidate(s)")
    if not total:
        print("NEGATIVE for a normalised current/max float at a stable address.\n"
              "This does NOT show HP is encoded -- the ratio may be computed on\n"
              "demand or held in a per-frame transient. Next: the direction-based\n"
              "live scan (aesteria_hpscan.py), which works either way.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
