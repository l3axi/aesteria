#!/usr/bin/env python3
"""aesteria_identify -- name an unknown world actor by making it appear or disappear.

THE PROBLEM THIS SOLVES
    the operator wants an ESP for Altgard's overworld feather collectibles. The blocker is not reading
    positions -- that already works -- it is knowing WHICH actor is a feather. Measured on the
    live client 2026-10-01: every one of the 39 `BP_EnvObj_Common_C` instances in the world
    carries its own class name as its object name, so nothing in a name dump distinguishes a
    collectible from a barrel. The candidate family is right there
    (`BP_EnvObj_Common_C`, `AionEnvObj`, `AionAbsorbObj`, `AionSeqEnvObj`, `InteractiveGroundCapture`)
    and guessing between them is exactly the failure mode that produced 30 floats of grass.

    So do not guess. Use the game as the instrument: enumerate the world, CHANGE ONE THING, and
    enumerate again. Pick a feather up and the actor that vanished is the feather. Walk into
    range of one and the actor that appeared is the feather. That is a differential measurement,
    the same shape as aesteria_mirrors.py, and it yields a class name with no inference at all.

WHY IDENTITY IS THE POSITION, NOT THE ADDRESS
    Objects are pool-recycled as the world streams -- proven live the same night, when three
    addresses that sat inside StaticMesh 'COM_DranaGrassG04' and friends owned nothing at all
    twenty minutes later. So an address is not an identity across two reads. A placed world
    object's POSITION is: it is authored, it does not move, and it survives the pool. Entries are
    therefore keyed on (class, position rounded to DECIMETRES), which tolerates float jitter
    without merging two genuinely distinct props.

USAGE -- two reads, one game action between them
    python3 tools/aesteria_identify.py snap before      # standing near an uncollected feather
    (pick the feather up)
    python3 tools/aesteria_identify.py snap after
    python3 tools/aesteria_identify.py diff before after

    VANISHED entries are the strong signal: one game action removed them. Expect a handful --
    streaming churn adds noise -- so repeat with a second feather and intersect the CLASS names.
    `--only-classes` prints just that intersection-ready class list.

SAFETY
    Read-only. Snapshots are enumerations of an already-running process via /proc/<pid>/mem; no
    attach, no write, no injection. Snapshots land in `snaps/` beside the repo, not /tmp -- tool
    state in a sticky /tmp is a known trap on this box.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aesteria_gobjects import GObjects                              # noqa: E402
from aesteria_live import LiveProcess, load_live_meta               # noqa: E402
from aesteria_entities import sweep                                 # noqa: E402
from aesteria_player import find_local_player                       # noqa: E402

SNAP_DIR = Path(__file__).resolve().parents[1] / "snaps"

# Position quantisation for identity. 10 units = 1 decimetre in this build's 100-units-per-metre
# scale: far below the spacing of two distinct props, far above float noise in a re-read.
GRID = 10.0

# The families a collectible could plausibly belong to, from the live sweep. Used only to ORDER
# the report so the likely answer is read first -- never to filter, because a wrong prior here
# would hide the true positive and that is the whole mistake this tool exists to avoid.
LIKELY = ("EnvObj", "AbsorbObj", "DroppedObj", "HomingDropObj", "Interactive",
          "Gather", "Collect", "Item", "Gimmick", "Trigger")


def key_of(e: dict) -> str:
    x, y, z = e["location"]
    return (f"{e['class']}|{round(x / GRID)}|{round(y / GRID)}|{round(z / GRID)}")


def cached_meta(live, verbose: bool):
    """(g, go_meta), reusing a root cache so a second snap is FAST.

    ⚠ WHY THIS EXISTS, and it is the difference between a readable diff and a useless one.
    Deriving GNames + GUObjectArray from scratch takes ~90 s, so the first feather run put
    **276 seconds** between the two snaps. Measured cost: of 328 actors in the `before` snap
    only 34 were still present in `after` -- the world had churned 90% of its entries, and the
    one object that mattered was buried under 266 vanished bare `Actor`s. The collection itself
    was still visible (HomingDropObj_C appeared 0.2 m away) but the feather was not isolable.
    The signal is not weak; the WINDOW was too wide. Narrowing the window is the whole fix --
    the same lesson as the recurring "a moving value cannot be compared against a stale
    snapshot" trap, which is about the size of the gap and never about the reader.

    load_live_meta() already revalidates a cached root against the live process and re-derives
    on mismatch, so this is safe across a relaunch by construction rather than by promise.
    """
    cache = SNAP_DIR / "meta"
    g, go_meta = load_live_meta(live, cache if cache.exists() else None, verbose)
    if not cache.exists():
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "gnames.json").write_text(json.dumps(
            {k: v for k, v in g.info.items() if not k.startswith("_")}))
        (cache / "gobjects.json").write_text(json.dumps(go_meta))
        print(f"  cached UE roots in {cache} — later snaps skip the ~90 s root scan")
    return g, go_meta


def take(args) -> dict:
    """One live enumeration, reduced to what a diff needs."""
    live = LiveProcess(args.pid)
    with live:
        g, meta = cached_meta(live, args.verbose)
        go = GObjects(live, meta, g)
        players = find_local_player(live, go, g)
        # ⚠ REFUSE WITHOUT AN ORIGIN. A snap with no player cannot be range-filtered and is not
        # comparable to one that was, so saving it only produces a confusing diff later. Hit
        # live 2026-10-01: the client sat at character select (407 actors, 5 CineCameraActor,
        # world unloaded, RSS 9.1 -> 6.5 GB) and the first snap silently saved 386 unbounded
        # entries. The absence of a player is a STATE, not an error -- so say which state.
        if not players:
            raise SystemExit(
                "no local player — the client is not in-world (character select, loading "
                "screen or lobby). Nothing to snapshot; get in-world and retry.")
        origin = players[0]["location"]
        ents = sweep(live, go, origin, args.range, args.verbose)
        return {
            "pid": live.pid,
            "taken_at": time.time(),
            "origin": origin,
            "range": args.range,
            "entries": {key_of(e): {"class": e["class"], "name": e["name"],
                                    "loc": list(e["location"]),
                                    "dist": e["distance"]}
                        for e in ents},
        }


def rank(cls: str) -> int:
    for i, frag in enumerate(LIKELY):
        if frag.lower() in (cls or "").lower():
            return i
    return len(LIKELY)


def report(title: str, keys, src: dict, top: int, only_classes: bool) -> None:
    rows = [src["entries"][k] for k in keys]
    rows.sort(key=lambda r: (rank(r["class"]), r["dist"]))
    counts = Counter(r["class"] or "?" for r in rows)
    print(f"\n{title}: {len(rows)} entr{'y' if len(rows) == 1 else 'ies'}, "
          f"{len(counts)} class(es)")
    for c, n in sorted(counts.items(), key=lambda x: (rank(x[0]), -x[1])):
        star = " <-- candidate family" if rank(c) < len(LIKELY) else ""
        print(f"    {n:>4}  {c}{star}")
    if only_classes:
        return
    for r in rows[:top]:
        x, y, z = r["loc"]
        print(f"      {r['dist']/100:8.1f} m  {r['class']:<34} {r['name']!r:<30} "
              f"({x:9.1f},{y:10.1f},{z:8.1f})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snap", help="enumerate the live world and save it")
    s.add_argument("label")
    s.add_argument("--pid", type=int, default=None)
    s.add_argument("--range", type=float, default=5000.0,
                   help="units around the player; default 5000 (50 m) keeps the diff "
                        "small enough that streaming churn does not bury the answer")
    s.add_argument("-v", "--verbose", action="store_true")

    d = sub.add_parser("diff", help="what vanished and what appeared between two snaps")
    d.add_argument("before")
    d.add_argument("after")
    d.add_argument("--top", type=int, default=20)
    d.add_argument("--only-classes", action="store_true",
                   help="print just the class histogram, for intersecting across runs")

    l = sub.add_parser("list", help="list saved snaps")

    args = ap.parse_args()
    SNAP_DIR.mkdir(exist_ok=True)

    if args.cmd == "list":
        for p in sorted(SNAP_DIR.glob("*.json")):
            d = json.loads(p.read_text())
            o = d.get("origin")
            where = f"({o[0]:.0f},{o[1]:.0f},{o[2]:.0f})" if o else "origin unknown"
            print(f"  {p.stem:<20} {len(d['entries']):>6} entries  "
                  f"{time.strftime('%H:%M:%S', time.localtime(d['taken_at']))}  {where}")
        return 0

    if args.cmd == "snap":
        snap = take(args)
        out = SNAP_DIR / f"{args.label}.json"
        out.write_text(json.dumps(snap))
        o = snap["origin"]
        print(f"\n{len(snap['entries'])} entries within {args.range/100:.0f} m of "
              f"({o[0]:.1f}, {o[1]:.1f}, {o[2]:.1f})")
        print(f"saved {out}")
        return 0

    def load(label: str) -> dict:
        p = SNAP_DIR / f"{label}.json"
        if not p.exists():
            have = sorted(q.stem for q in SNAP_DIR.glob("*.json"))
            raise SystemExit(f"no snap named {label!r}. "
                             + (f"Have: {', '.join(have)}" if have else
                                "No snaps saved yet — run `snap <label>` in-world first."))
        return json.loads(p.read_text())

    a, b = load(args.before), load(args.after)
    if a["pid"] != b["pid"]:
        print(f"⚠ different processes ({a['pid']} vs {b['pid']}) — "
              f"a diff across a relaunch is meaningless")
        return 1
    ka, kb = set(a["entries"]), set(b["entries"])
    moved = 0
    if a.get("origin") and b.get("origin"):
        moved = sum((p - q) ** 2 for p, q in zip(a["origin"], b["origin"])) ** 0.5
        print(f"you moved {moved/100:.1f} m between the two snaps"
              + ("  ⚠ streaming churn will dominate; retake them standing still"
                 if moved > 500 else ""))
    print(f"{len(ka)} before, {len(kb)} after, {len(ka & kb)} in both")

    report("VANISHED (was there, now gone — the strong signal)", ka - kb, a,
           args.top, args.only_classes)
    report("APPEARED (new since the first snap)", kb - ka, b,
           args.top, args.only_classes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
