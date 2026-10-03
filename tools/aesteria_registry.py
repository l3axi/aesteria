#!/usr/bin/env python3
"""Label non-reflected C++ UObjects by resolving each pointer field's pointee class.

WHY THIS EXISTS
    AION 2's gameplay systems (AionObjectManager, SkillSystem, TargetingSystem, ...)
    have ZERO reflected properties -- they are plain C++ UObjects, invisible to UE
    reflection, so the SDK dump says nothing about their layout. Every offset we had
    for them was derived by hand in the single 6-hour playtest window, using
    differential captures and live toggle correlation.

    Those numbers are build-specific. They do not survive a client patch, and there
    is no throwaway account for AION 2 -- re-deriving them by hand means spending a real
    account's live time on work a machine can do. This tool does it in seconds instead, carrying nothing across builds.

THE MECHANISM (spike 001, VALIDATED -- see spikes/001-blind-registry/README.md)
    A subsystem pointer points at a registered UObject, and every UObject carries
    ClassPrivate at +0x10 whose FName is its class name. So for each 8-byte-aligned
    slot in the anchor object:

        slot -> u64 -> present in GUObjectArray? -> pointee's ClassPrivate -> FName

    No diffing, no ground truth, no live window. Reproduces all 13 hand-derived
    AionCharSession offsets blind, and finds 82 subsystems where the hand work found
    18. Verified stable across captures in different game states, and verified to
    carry zero constants from the old build (GNames and GUObjectArray are both
    located by scan, never by hardcoded address).

⚠ ARRAY MEMBERSHIP IS THE ENTIRE GATE -- measured, not assumed.
    The obvious cheaper test, "does the pointee pass a UObject shape check"
    (aesteria_gobjects.valid_uobject), admits PURE NOISE: in the spike every one of
    its 17 extra hits resolved to no class name at all, with UTF-16 mojibake object
    names from reading the FName pool as if it were a name. Do not relax this to a
    shape check to catch unregistered subsystems -- it catches garbage instead.

⚠ SCOPE: POINTER FIELDS ONLY.
    Scalars are invisible to this method and remain hand-derived, build-specific, and
    dependent on live re-derivation: has_target / target_id on AionCharSession and its
    mirrors, the character actor's own object id, and HP (unsolved by any method).
    Do not read a green run here as "the layout is recovered".

OBJECT SIZE COMES FROM THE OBJECT, so there is no scan window to guess.
    ClassPrivate(+0x10) -> UStruct::PropertiesSize(+0x58) is the exact allocation
    size. Measured: AionCharSession 38,032 B, matching the size recorded by hand on
    the playtest build. That offset already accounts for this build's
    FStructBaseChain quirk (see aesteria_sdk.py) -- do not substitute stock UE's.
    Bounding each scan by the real size is what makes --depth safe: a fixed window
    would run off the end of the small subsystems (TargetingSystem is 576 B) and
    label whatever allocation happens to sit after them.

USAGE
    # launch-day bring-up: one command, nothing carried from the old build
    ./aesteria_registry.py --pid <AION2 pid> --out registry.tsv --depth 2

    # against a banked capture
    ./aesteria_registry.py --capture ../captures/07-combat \
        --meta-from ../captures/01-tutorial-in-world --out registry.tsv
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aesteria_snapshot_reader import Snapshot                    # noqa: E402
from aesteria_live import LiveProcess, load_live_meta            # noqa: E402
from aesteria_gobjects import GObjects                           # noqa: E402
import aesteria_player as player                                 # noqa: E402

UOBJ_CLASS = 0x10
STRUCT_PROPS_SIZE = 0x58          # UStruct::PropertiesSize, this build's layout

DEFAULT_ANCHORS = ("AionCharSession",)

# A UObject allocation larger than this is not a plausible game object; treat it as
# a failed PropertiesSize read rather than scanning gigabytes.
MAX_PLAUSIBLE_OBJECT = 1 << 22

# Below this many labelled fields on the primary anchor, assume the walk is broken
# rather than the object being small. The playtest build yields 82. A suspiciously
# small count is itself the failing test -- the "528 properties" lesson from the SDK
# extraction.
MIN_FIELDS = 8


class RegistryError(RuntimeError):
    pass


def object_size(src, go: GObjects, addr: int) -> int | None:
    """Exact allocation size via ClassPrivate -> UStruct::PropertiesSize."""
    cls = src.u64(addr + UOBJ_CLASS)
    if not cls or not src.looks_like_pointer(cls):
        return None
    size = src.i32(cls + STRUCT_PROPS_SIZE)
    if size is None or not (0 < size <= MAX_PLAUSIBLE_OBJECT):
        return None
    return size


def find_instance(go: GObjects, class_name: str) -> int | None:
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


def check_self_reference(src, go: GObjects, addr: int, class_name: str,
                         known: set[int]) -> None:
    """Gate: the slot at +0x10 must name this object's own class.

    This is the whole read path in one assertion -- pointer read, GUObjectArray
    membership, ClassPrivate deref, and two FName resolutions -- against ground
    truth the engine fixes rather than something we chose. It only writes a
    root that matches a known-good value.

    Gating on something structurally trivial instead (a struct size, say) would
    pass without ever executing the resolution path, which is exactly how the SDK
    extraction shipped a confident wrong 528-property dump.
    """
    cls_ptr = src.u64(addr + UOBJ_CLASS)
    if not cls_ptr:
        raise RegistryError(f"{class_name}: ClassPrivate is null")
    if cls_ptr not in known:
        raise RegistryError(
            f"{class_name}: ClassPrivate {cls_ptr:#x} is not in GUObjectArray -- "
            "the array root is wrong, or this is not a registered object")
    meta_class = go.class_name(cls_ptr)
    own_name = go.object_name(cls_ptr)
    if meta_class != "Class":
        raise RegistryError(
            f"{class_name}: ClassPrivate's own class reads {meta_class!r}, "
            "expected 'Class' -- GNames or the object array is misresolved")
    if own_name != class_name:
        raise RegistryError(
            f"{class_name}: ClassPrivate names {own_name!r} -- anchor mismatch")


def scan(src, go: GObjects, addr: int, size: int, known: set[int]) -> list[dict]:
    """Label every pointer slot in [addr, addr+size) by its pointee's class."""
    out = []
    for off in range(0, size, 8):
        val = src.u64(addr + off)
        if val is None:
            break                                  # ran off mapped memory
        if not val or val not in known:
            continue
        out.append({
            "offset": off,
            "pointee": val,
            "class": go.class_name(val) or "?",
            "name": go.object_name(val) or "",
        })
    return out


def build(src, go: GObjects, anchors: tuple[str, ...], depth: int,
          verbose: bool) -> tuple[list[dict], dict]:
    known = registered_addresses(go)
    rows: list[dict] = []
    seen: set[int] = set()
    stats = {"objects_in_array": len(known), "anchors": {}, "walked": 0}

    # (class_name, addr, depth) queue; anchors resolved by name, children by pointer
    queue: list[tuple[str, int | None, int]] = [(a, None, 0) for a in anchors]

    while queue:
        cls_name, addr, d = queue.pop(0)
        if addr is None:
            addr = find_instance(go, cls_name)
            if addr is None:
                raise RegistryError(f"no live {cls_name} found")
        if addr in seen:
            continue
        seen.add(addr)

        size = object_size(src, go, addr)
        if size is None:
            if d == 0:
                raise RegistryError(f"{cls_name}: cannot read PropertiesSize")
            continue                                # skip an odd child, not fatal
        check_self_reference(src, go, addr, cls_name, known)

        fields = scan(src, go, addr, size, known)
        stats["walked"] += 1
        if d == 0:
            stats["anchors"][cls_name] = {"addr": addr, "size": size,
                                          "fields": len(fields)}
            if len(fields) < MIN_FIELDS:
                raise RegistryError(
                    f"{cls_name}: only {len(fields)} labelled fields in {size} B "
                    f"(expected >= {MIN_FIELDS}); treating the count as a failing "
                    "test rather than reporting a thin result as success")
        if verbose:
            print(f"  [{d}] {cls_name:<30} {addr:#014x} {size:>7} B  "
                  f"{len(fields)} fields")

        for f in fields:
            rows.append({"anchor": cls_name, "anchor_addr": addr, "depth": d, **f})
            if d + 1 <= depth and f["class"] not in ("?", "Class"):
                queue.append((f["class"], f["pointee"], d + 1))

    return rows, stats


def write_tsv(path: Path, rows: list[dict]) -> None:
    with path.open("w") as fh:
        fh.write("anchor\tanchor_addr\tdepth\toffset\tpointee\tclass\tname\n")
        for r in rows:
            fh.write(f"{r['anchor']}\t{r['anchor_addr']:#x}\t{r['depth']}\t"
                     f"{r['offset']}\t{r['pointee']:#x}\t{r['class']}\t{r['name']}\n")


def write_provenance(path: Path, stats: dict, g, meta: dict, source: str,
                     rows: list[dict]) -> None:
    """Addresses are build-specific and recorded for audit only.

    Nothing downstream may read them: the whole point is that a later build
    re-runs this tool rather than reusing a number from it.
    """
    path.write_text(json.dumps({
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": source,
        "rows": len(rows),
        "gnames_pool": getattr(g, "info", {}).get("pool"),
        "gobjects_array": meta.get("array"),
        "objects_in_array": stats["objects_in_array"],
        "objects_walked": stats["walked"],
        "anchors": {k: {"addr": hex(v["addr"]), "size": v["size"],
                        "fields": v["fields"]}
                    for k, v in stats["anchors"].items()},
        "note": "addresses are build-specific; re-run this tool after any patch",
    }, indent=1) + "\n")


def run(src, go, g, go_meta, args, source: str) -> int:
    try:
        rows, stats = build(src, go, tuple(args.anchor), args.depth, args.verbose)
    except RegistryError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1

    for name, a in stats["anchors"].items():
        print(f"{name} @ {a['addr']:#014x}  {a['size']} B  "
              f"{a['fields']} labelled pointer fields")
    if args.depth:
        print(f"{stats['walked']} objects walked, {len(rows)} rows total")

    if args.out:
        out = Path(args.out)
        write_tsv(out, rows)
        prov = out.with_suffix(".json")
        write_provenance(prov, stats, g, go_meta, source, rows)
        print(f"wrote {out} ({len(rows)} rows) and {prov}")
    else:
        for r in rows:
            if r["depth"] == 0:
                print(f"  +{r['offset']:<6} {r['class']:<34} {r['name']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--capture", help="read a banked capture")
    ap.add_argument("--pid", type=int, help="read the live game")
    ap.add_argument("--meta-from", help="capture dir with gnames/gobjects json; "
                                       "omit to scan both roots from scratch")
    ap.add_argument("--anchor", action="append", default=None,
                    help="anchor class (repeatable); default AionCharSession")
    # ⚠ Depth is near-worthless for the subsystems, measured on 07-combat: walking
    # all 82 adds only 195 rows, and the median subsystem has exactly TWO pointer
    # fields -- its own ClassPrivate and a back-pointer to AionCharSession.
    # AionGameInstance (34) is the only rich child. Their interesting state is
    # scalar or inline, which is this method's documented blind spot, so do not
    # expect --depth to map them. The registry itself is the payload.
    ap.add_argument("--depth", type=int, default=0,
                    help="also label discovered subsystems, N levels deep "
                         "(thin: most have only Class + parent)")
    ap.add_argument("--out", help="write TSV here (plus a .json provenance file)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    if not args.anchor:
        args.anchor = list(DEFAULT_ANCHORS)

    meta_from = Path(args.meta_from) if args.meta_from else None
    if args.capture:
        cap = Path(args.capture)
        with Snapshot(cap) as snap:
            g, meta = player.load_meta(snap, cap, meta_from, args.verbose)
            return run(snap, GObjects(snap, meta, g), g, meta, args, str(cap))
    with LiveProcess(args.pid) as live:
        g, meta = load_live_meta(live, meta_from, args.verbose)
        return run(live, GObjects(live, meta, g), g, meta, args,
                   f"live pid {live.pid}")


if __name__ == "__main__":
    sys.exit(main())
