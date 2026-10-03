#!/usr/bin/env python3
"""Current target + nearby entities, resolved through the game's object IDs.

THE TARGETING STACK (found 2026-09-19 by live toggle correlation, see
tools/aesteria_watch.py -- a single before/after diff failed three times first)

    AionCharSession +0x0eb8   has_target   (0 / 1)
    AionCharSession +0x0ec0   target object ID
    <character actor> +0x26a0 that actor's OWN object ID

    resolve: scan character actors for id == target_id -> exactly one match
             -> RootComponent -> RelativeLocation -> distance

    The same has/id pair is mirrored at SkillSystem +0x598/+0x5a0 and on the
    player pawn at +0x2e88/+0x2e90. All three moved in lockstep across 127
    observed transitions, so any of them can be read; the session copy is the
    most natural owner.

WHY THE ID LAYER EXISTS
    AION keeps a network game-object identity separate from the UE actor. The
    target is stored as an ID, never as an AActor*, which is why diffing
    TargetingSystem for "a slot that became a pointer to a character" found
    nothing at all. +0x26a0 is the bridge between the two worlds.

    (An earlier scan for the ID on actors came back empty purely because it read
    only the first 0x1200 bytes of each actor. The field is at 0x26a0. When a
    search for a value finds nothing, check the search WINDOW before concluding
    the value is not there.)
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from aesteria_live import LiveProcess, load_live_meta      # noqa: E402
from aesteria_snapshot_reader import Snapshot               # noqa: E402
from aesteria_gobjects import GObjects                      # noqa: E402
import aesteria_player as player                            # noqa: E402

SESSION_HAS_TARGET = 0x0EB8
SESSION_TARGET_ID = 0x0EC0
SKILL_HAS_TARGET = 0x0598
SKILL_TARGET_ID = 0x05A0
PAWN_HAS_TARGET = 0x2E88
PAWN_TARGET_ID = 0x2E90
ACTOR_OBJECT_ID = 0x26A0

# ⚠ MEASURED 2026-09-29, AND IT CONTRADICTS THE HEADER ABOVE.
# In the banked captures the session's target id is at +0x0F20, NOT +0x0EC0, and the
# pawn mirror does not hold it at all:
#     06-targeted   session+0xf20 = 49248   skill+0x5a0 = 49248
#                   session+0xec0 = 0       pawn+0x2e90 = 0
#     05-no-target  all zero
# 49248 resolves to exactly one BPCh_NpcBase_C at 3.4 m, so +0xf20 is the real field.
# aesteria_mirrors.py finds the pair blind (session+0xf20 <-> skill+0x5a0, one hit,
# no false positives), which is how the discrepancy surfaced.
#
# The 0xEC0 value came from a LIVE toggle experiment over 127 transitions, so it is
# not simply a typo, and this is not enough to declare it wrong everywhere. Rather
# than pick one on capture evidence alone, read every candidate and use whichever
# resolves to exactly one actor -- and say so when they disagree. Resolve the
# contradiction on the first live run of the launch build.
SESSION_TARGET_ID_ALT = 0x0F20

# Real in-world characters use the BPCh_ blueprint family. Matching loosely on
# "Character" also catches AionCharacterAction_* and
# AionMovieSceneCharacterActPlaySection, which are not actors at all -- their
# +0x26a0 is unrelated memory and yields absurd ids like 1297765888.
CHARACTER_PREFIXES = ("BPCh_",)
CHARACTER_EXACT = {"AionCharacter", "AionSimpleCharacter"}
MAX_SANE_OBJECT_ID = 1 << 24


def is_character(class_name: str | None) -> bool:
    if not class_name:
        return False
    return (class_name.startswith(CHARACTER_PREFIXES)
            or class_name in CHARACTER_EXACT)


def find_session(src, go: GObjects) -> int | None:
    for i, addr in go.iter_objects():
        if go.class_name(addr) == "AionCharSession":
            if not (go.object_name(addr) or "").startswith("Default__"):
                return addr
    return None


def read_target(src, session: int, skill: int | None = None) -> tuple[int, int, str]:
    """Best available (has, id, which-source) across every known candidate.

    Returns the first candidate holding a nonzero id rather than trusting one
    offset, because the session's +0xEC0 measured 0 while targeting (see the note
    above). A caller that gets source != "session+0xec0" should treat the recorded
    offset as unconfirmed on this build.
    """
    cands: list[tuple[str, int, int]] = [
        ("session+0xec0", src.u64(session + SESSION_HAS_TARGET) or 0,
         src.u32(session + SESSION_TARGET_ID) or 0),
        ("session+0xf20", src.u64(session + SESSION_HAS_TARGET) or 0,
         src.u32(session + SESSION_TARGET_ID_ALT) or 0),
    ]
    if skill:
        cands.append(("skill+0x5a0", src.u64(skill + SKILL_HAS_TARGET) or 0,
                      src.u32(skill + SKILL_TARGET_ID) or 0))
    # Prefer a candidate whose flag agrees with its id; fall back to a plausible id
    # alone, because the id is the field that measured reliable and the flag is not.
    for name, has, tid in cands:
        if has and 0 < tid < MAX_SANE_OBJECT_ID:
            return has, tid, name
    for name, has, tid in cands:
        if 0 < tid < MAX_SANE_OBJECT_ID:
            return has, tid, name
    return 0, 0, "none"


def enumerate_characters(src, go: GObjects, origin=None) -> list[dict]:
    out = []
    for i, addr in go.iter_objects():
        cls = go.class_name(addr)
        if not is_character(cls):
            continue
        oid = src.u32(addr + ACTOR_OBJECT_ID)
        root = src.u64(addr + player.ACTOR_ROOT_COMPONENT)
        loc = None
        if root and src.looks_like_pointer(root):
            loc = player.read_vector(src, root + player.SCENE_RELATIVE_LOCATION)
            if not player.plausible_world_vector(loc):
                loc = None
        if oid is not None and not (0 < oid < MAX_SANE_OBJECT_ID):
            oid = None
        out.append({"addr": addr, "class": cls, "object_id": oid,
                    "location": loc,
                    "distance": math.dist(origin, loc)
                    if (origin and loc) else None})
    out.sort(key=lambda e: e["distance"] if e["distance"] is not None else 9e18)
    return out


def report(src, go, g, top: int) -> int:
    session = find_session(src, go)
    if session is None:
        print("no live AionCharSession found")
        return 1
    me = player.find_local_player(src, go, g)
    if not me:
        print("no local player found")
        return 1
    me = me[0]
    origin = me["location"]
    my_id = src.u32(me["pawn"] + ACTOR_OBJECT_ID)

    sk = None
    for i, addr in go.iter_objects():
        if go.class_name(addr) == "SkillSystem" and not (
                go.object_name(addr) or "").startswith("Default__"):
            sk = addr
            break
    has, tid, src_name = read_target(src, session, sk)

    print(f"  AionCharSession  {session:#014x}")
    print(f"  you              {me['pawn_class']}  id={my_id}  "
          f"({origin[0]:.1f}, {origin[1]:.1f}, {origin[2]:.1f})")
    print(f"  has_target       {has}")
    print(f"  target_id        {tid}" + (f"  ({tid:#x})" if tid else "")
          + f"   [read from {src_name}]")
    if tid and src_name != "session+0xec0":
        print(f"  ⚠ the recorded session offset +0xEC0 held 0 while targeting; "
              f"{src_name} is the field that worked here")

    # Cross-check the mirrors; disagreement means the offsets have drifted.
    mirrors = []
    if sk:
        mirrors.append(("SkillSystem", src.u64(sk + SKILL_HAS_TARGET),
                        src.u32(sk + SKILL_TARGET_ID)))
    mirrors.append(("PlayerPawn", src.u64(me["pawn"] + PAWN_HAS_TARGET),
                    src.u32(me["pawn"] + PAWN_TARGET_ID)))
    for name, h, t in mirrors:
        agree = "agrees" if (h or 0, t or 0) == (has, tid) else "DISAGREES"
        print(f"  mirror {name:<16} has={h} id={t}  {agree}")

    chars = enumerate_characters(src, go, origin)
    named = [c for c in chars if c["object_id"]]
    print(f"\n  {len(chars)} character actors, {len(named)} with a sane object id")

    if tid:  # the id is the evidence; the flag measured unreliable (see read_target)
        match = [c for c in chars if c["object_id"] == tid]
        print(f"\n  TARGET RESOLUTION: {len(match)} actor(s) carry id {tid}")
        for c in match:
            d = f"{c['distance']/100:.1f} m" if c["distance"] is not None else "?"
            loc = c["location"]
            print(f"    {c['addr']:#014x}  {c['class']}  at {d}"
                  + (f"  ({loc[0]:.1f}, {loc[1]:.1f}, {loc[2]:.1f})" if loc else ""))
        if len(match) != 1:
            print("    ^ expected exactly 1; treat this reading as unproven")
    else:
        print("\n  no target selected")

    print(f"\n  nearest {top} characters:")
    for c in chars[:top]:
        d = f"{c['distance']/100:7.1f} m" if c["distance"] is not None else "      ? "
        mark = ""
        if c["addr"] == me["pawn"]:
            mark = "  <== YOU"
        elif tid and c["object_id"] == tid:
            mark = "  <== TARGET"
        print(f"    {d}  id={str(c['object_id'] or '-'):<8} {c['class']:<22}{mark}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="current target and nearby entities")
    ap.add_argument("--capture", help="read a capture instead of the live game")
    ap.add_argument("--pid", type=int)
    ap.add_argument("--meta-from", required=True)
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    meta_from = Path(args.meta_from)
    if args.capture:
        cap = Path(args.capture)
        with Snapshot(cap) as snap:
            print(snap.stats())
            g, meta = player.load_meta(snap, cap, meta_from, args.verbose)
            return report(snap, GObjects(snap, meta, g), g, args.top)
    with LiveProcess(args.pid) as live:
        print(live.stats())
        g, meta = load_live_meta(live, meta_from, args.verbose)
        return report(live, GObjects(live, meta, g), g, args.top)


if __name__ == "__main__":
    sys.exit(main())
