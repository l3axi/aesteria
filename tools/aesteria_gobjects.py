#!/usr/bin/env python3
"""Locate UE5 GUObjectArray (FUObjectArray) in a capture and enumerate objects.

STRATEGY -- pointer-chase up from a known object, not a brute-force struct scan.
    6.5 GB of writable memory is too much to walk field-by-field in Python, and a
    shape-only scan invites false positives. Instead we start from something we
    can name and climb:

      1. GNames gives the FName id of a distinctive class, e.g.
         "AionPlayerController". Search for that FName at UObject+0x18.
      2. That object's InternalIndex says which slot it occupies.
      3. Whoever points at the object is FUObjectItem::Object inside a chunk, so
         chunk_base = ptr - (index % 65536) * sizeof(FUObjectItem).
      4. Whoever points at chunk_base is Objects[index / 65536], so the chunk
         pointer array base = that - (index / 65536) * 8.
      5. Whoever points at THAT array is FChunkedFixedUObjectArray::Objects,
         which sits at FUObjectArray+0x10.

THE INVARIANT THAT PROVES IT
    For every slot i, GObjects[i].Object->InternalIndex == i. Checking that over
    hundreds of consecutive live objects cannot succeed by coincidence. If it
    fails, we report failure instead of emitting an SDK built on a wrong base.

LAYOUT (UE 5.3.2)
    class FUObjectArray {
        int32 ObjFirstGCIndex;              // +0x00
        int32 ObjLastNonGCIndex;            // +0x04
        int32 MaxObjectsNotConsideredByGC;  // +0x08
        bool  OpenForDisregardForGC;        // +0x0C
        FChunkedFixedUObjectArray ObjObjects; // +0x10
    };
    class FChunkedFixedUObjectArray {
        FUObjectItem** Objects;   // +0x00
        FUObjectItem*  PreAlloc;  // +0x08
        int32 MaxElements;        // +0x10
        int32 NumElements;        // +0x14
        int32 MaxChunks;          // +0x18
        int32 NumChunks;          // +0x1C
    };
    struct FUObjectItem { UObject* Object; int32 Flags; int32 ClusterRootIndex;
                          int32 SerialNumber; };   // 24 bytes, 8-aligned
    class UObjectBase { void* vtable;  int32 ObjectFlags; int32 InternalIndex;
                        UClass* ClassPrivate; FName NamePrivate;
                        UObject* OuterPrivate; };  // 0x28
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from aesteria_snapshot_reader import Snapshot            # noqa: E402
from aesteria_gnames import GNames, LAYOUTS, find_gnames  # noqa: E402

# --- UObjectBase ---
UOBJ_VTABLE = 0x00
UOBJ_FLAGS = 0x08
UOBJ_INTERNAL_INDEX = 0x0C
UOBJ_CLASS = 0x10
UOBJ_NAME = 0x18
UOBJ_OUTER = 0x20
UOBJ_SIZE = 0x28

# --- FUObjectItem / chunked array ---
ITEM_SIZE = 24
ELEMENTS_PER_CHUNK = 64 * 1024
ARR_OBJECTS = 0x00
ARR_MAX_ELEMENTS = 0x10
ARR_NUM_ELEMENTS = 0x14
ARR_MAX_CHUNKS = 0x18
ARR_NUM_CHUNKS = 0x1C
OBJARRAY_IN_FUOBJECTARRAY = 0x10

# Distinctive, near-certainly-present class names to anchor the chase.
ANCHORS = [
    "AionPlayerController",
    "AionGameInstance",
    "AionCharacter",
    "PlayerController",
    "Engine",
]


def load_gnames(snap: Snapshot, capture: Path, verbose: bool = False) -> GNames:
    """Reconstruct GNames from cached metadata, or re-derive it."""
    meta_path = capture / "gnames.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        meta["_layout"] = next(l for l in LAYOUTS if l.name == meta["layout"])
        g = GNames(snap, meta)
        if g.by_id(0) == "None":
            if verbose:
                print(f"  GNames from cache: pool {meta['pool']:#x}")
            return g
        print("  cached GNames failed ground truth; re-deriving", file=sys.stderr)
    info = find_gnames(snap, verbose=verbose)
    if not info:
        sys.exit("cannot locate GNames; GUObjectArray search needs it to validate")
    return GNames(snap, info)


def build_name_index(g: GNames, wanted: set[str]) -> dict[str, int]:
    """Reverse-lookup only the names we care about (2M entries is a lot to keep)."""
    out: dict[str, int] = {}
    for nid, text in g.iter_all():
        if text in wanted and text not in out:
            out[text] = nid
            if len(out) == len(wanted):
                break
    return out


def valid_uobject(snap: Snapshot, addr: int, g: GNames) -> dict | None:
    """Check UObject shape and return its decoded fields, or None."""
    vt = snap.u64(addr + UOBJ_VTABLE)
    idx = snap.i32(addr + UOBJ_INTERNAL_INDEX)
    cls = snap.u64(addr + UOBJ_CLASS)
    nid = snap.u32(addr + UOBJ_NAME)
    outer = snap.u64(addr + UOBJ_OUTER)
    if None in (vt, idx, cls, nid, outer):
        return None
    if not snap.looks_like_pointer(vt):
        return None
    if idx < 0 or idx > 20_000_000:
        return None
    if cls and not snap.looks_like_pointer(cls):
        return None
    if outer and not snap.looks_like_pointer(outer):
        return None
    name = g.by_id(nid)
    if name is None:
        return None
    return {"addr": addr, "vtable": vt, "index": idx, "class": cls,
            "name_id": nid, "name": name, "outer": outer}


def validate_array(snap: Snapshot, arr_base: int, g: GNames,
                   sample: int = 600) -> dict | None:
    """Validate a FChunkedFixedUObjectArray candidate.

    The InternalIndex == slot invariant is the whole proof. We also require a high
    hit rate rather than perfection: slots can legitimately be null (destroyed
    objects awaiting reuse), but a wrong base produces near-zero agreement.
    """
    objects = snap.u64(arr_base + ARR_OBJECTS)
    max_elements = snap.i32(arr_base + ARR_MAX_ELEMENTS)
    num_elements = snap.i32(arr_base + ARR_NUM_ELEMENTS)
    max_chunks = snap.i32(arr_base + ARR_MAX_CHUNKS)
    num_chunks = snap.i32(arr_base + ARR_NUM_CHUNKS)
    if None in (objects, max_elements, num_elements, max_chunks, num_chunks):
        return None
    if not snap.looks_like_pointer(objects):
        return None
    if not (0 < num_elements <= max_elements <= 100_000_000):
        return None
    if not (0 < num_chunks <= max_chunks <= 4096):
        return None
    # MaxElements must be an exact multiple of the chunk size, and the live chunk
    # count must be exactly what NumElements requires.
    if max_elements != max_chunks * ELEMENTS_PER_CHUNK:
        return None
    expected_chunks = (num_elements + ELEMENTS_PER_CHUNK - 1) // ELEMENTS_PER_CHUNK
    if num_chunks != expected_chunks:
        return None

    checked = matched = null_slots = 0
    step = max(1, num_elements // sample)
    for i in range(0, min(num_elements, sample * step), step):
        chunk_ptr = snap.u64(objects + (i // ELEMENTS_PER_CHUNK) * 8)
        if chunk_ptr is None or not snap.looks_like_pointer(chunk_ptr):
            continue
        item = chunk_ptr + (i % ELEMENTS_PER_CHUNK) * ITEM_SIZE
        obj = snap.u64(item)
        if obj is None:
            continue
        checked += 1
        if obj == 0:
            null_slots += 1
            continue
        got = snap.i32(obj + UOBJ_INTERNAL_INDEX)
        if got == i:
            matched += 1
    live = checked - null_slots
    if live < 20:
        return None
    rate = matched / live
    if rate < 0.90:
        return None
    return {"array": arr_base, "objects": objects, "max_elements": max_elements,
            "num_elements": num_elements, "max_chunks": max_chunks,
            "num_chunks": num_chunks, "checked": checked, "matched": matched,
            "null_slots": null_slots, "match_rate": rate}


def validate_chunk(snap: Snapshot, chunk_base: int, chunk_i: int,
                   probes: int = 64) -> int:
    """Score a candidate chunk WITHOUT another memory scan.

    A real chunk is a dense array of FUObjectItem whose objects self-report their
    global slot. Checking that in place is what lets us avoid a full-memory pass
    per candidate -- the mistake that made the first version do 400 passes.
    """
    base_index = chunk_i * ELEMENTS_PER_CHUNK
    hits = 0
    for slot in range(probes):
        obj = snap.u64(chunk_base + slot * ITEM_SIZE)
        if obj is None:
            break
        if obj == 0:
            continue
        if not snap.looks_like_pointer(obj):
            return 0
        if snap.i32(obj + UOBJ_INTERNAL_INDEX) == base_index + slot:
            hits += 1
        else:
            return 0
    return hits


def find_gobjects(snap: Snapshot, g: GNames, verbose: bool = False) -> dict | None:
    """Climb from a named object to FUObjectArray in a bounded number of passes.

    Each scan is MATERIALIZED before the next begins. Iterating one find()
    generator while starting another inside the loop is what turned four passes
    into hundreds.
    """
    ids = build_name_index(g, set(ANCHORS))
    if verbose:
        for k, v in ids.items():
            print(f"  anchor {k!r} -> name id {v}", flush=True)
    if not ids:
        print("  no anchor names found in GNames", file=sys.stderr)
        return None

    tried_arrays: set[int] = set()
    for anchor in ANCHORS:
        nid = ids.get(anchor)
        if nid is None:
            continue

        # PASS A: the anchor object itself.
        needle = struct.pack("<II", nid, 0)
        objs = [info for hit in snap.find(needle, perms="w", limit=4000)
                if (info := valid_uobject(snap, hit - UOBJ_NAME, g))]
        objs = [o for o in objs if o["index"] > 0]
        if verbose:
            print(f"  [{anchor}] pass A: {len(objs)} plausible UObject(s)", flush=True)
        if not objs:
            continue

        # PASS B: who points at those objects -> candidate chunks. One scan for
        # ALL anchor objects at once, then validate each chunk structurally.
        obj_by_addr = {o["addr"]: o for o in objs}
        chunk_cands: dict[int, int] = {}          # chunk_base -> chunk_i
        for o in objs:
            slot = o["index"] % ELEMENTS_PER_CHUNK
            chunk_i = o["index"] // ELEMENTS_PER_CHUNK
            ptrs = list(snap.find_pointers_to(o["addr"], limit=2000, perms="w"))
            for ptr_addr in ptrs:
                chunk_base = ptr_addr - slot * ITEM_SIZE
                if chunk_base in chunk_cands:
                    continue
                score = validate_chunk(snap, chunk_base, chunk_i)
                if score >= 8:
                    chunk_cands[chunk_base] = chunk_i
                    if verbose:
                        print(f"  [{anchor}] pass B: chunk {chunk_base:#x} "
                              f"(chunk_i={chunk_i}) self-consistent on {score} slots",
                              flush=True)
        if not chunk_cands:
            if verbose:
                print(f"  [{anchor}] pass B: no self-consistent chunk", flush=True)
            continue

        # PASS C: who points at a chunk -> the chunk pointer array.
        array_cands: set[int] = set()
        for chunk_base, chunk_i in chunk_cands.items():
            for cptr in list(snap.find_pointers_to(chunk_base, limit=2000, perms="w")):
                objects_array = cptr - chunk_i * 8
                if snap.u64(objects_array + chunk_i * 8) != chunk_base:
                    continue
                # Chunk 0 slot 0 must be object index 0 -- cheap decisive check.
                first_chunk = snap.u64(objects_array)
                if first_chunk is None or not snap.looks_like_pointer(first_chunk):
                    continue
                if validate_chunk(snap, first_chunk, 0) < 8:
                    continue
                array_cands.add(objects_array)
        if verbose:
            print(f"  [{anchor}] pass C: {len(array_cands)} chunk-array candidate(s)",
                  flush=True)

        # PASS D: who points at the chunk array -> FChunkedFixedUObjectArray.
        for objects_array in array_cands:
            for aptr in list(snap.find_pointers_to(objects_array, limit=2000,
                                                   perms="w")):
                if aptr in tried_arrays:
                    continue
                tried_arrays.add(aptr)
                res = validate_array(snap, aptr, g)
                if res:
                    res["fuobjectarray"] = aptr - OBJARRAY_IN_FUOBJECTARRAY
                    res["anchor"] = anchor
                    res["anchor_object"] = obj_by_addr.get(
                        next(iter(obj_by_addr)), objs[0])
                    if verbose:
                        print(f"  [{anchor}] pass D: VALIDATED array {aptr:#x} "
                              f"rate {res['match_rate']:.3f}", flush=True)
                    return res
                elif verbose:
                    print(f"  [{anchor}] pass D: rejected {aptr:#x}", flush=True)
    return None


class GObjects:
    """Enumerate the object array."""

    def __init__(self, snap: Snapshot, info: dict, g: GNames):
        self.snap = snap
        self.g = g
        self.objects = info["objects"]
        self.num = info["num_elements"]
        self.array = info.get("array")
        self._chunks: dict[int, int] = {}

    def refresh(self) -> None:
        """Re-read the element count and drop cached chunk pointers.

        A capture never changes, so caching both was free. A LIVE array grows as the world
        streams: a radar that kept the count from startup never enumerated anything that
        loaded after it started -- live, 10-01, it printed "0 in range" while standing on a
        feather that a fresh process found at 1.9 m.
        """
        if self.array is not None:
            n = self.snap.i32(self.array + ARR_NUM_ELEMENTS)
            if n and n > 0:
                self.num = n
        self._chunks.clear()

    def chunk(self, i: int) -> int | None:
        if i not in self._chunks:
            p = self.snap.u64(self.objects + i * 8)
            if p is None:
                return None
            self._chunks[i] = p
        return self._chunks[i]

    def raw(self, i: int) -> int | None:
        c = self.chunk(i // ELEMENTS_PER_CHUNK)
        if c is None:
            return None
        return self.snap.u64(c + (i % ELEMENTS_PER_CHUNK) * ITEM_SIZE)

    def object_name(self, addr: int) -> str | None:
        nid = self.snap.u32(addr + UOBJ_NAME)
        return self.g.by_id(nid) if nid is not None else None

    def full_name(self, addr: int, depth: int = 8) -> str:
        """Outer chain, e.g. AionPlayerController /Script/Aion2."""
        parts = []
        cur = addr
        seen = set()
        while cur and depth > 0 and cur not in seen:
            seen.add(cur)
            n = self.object_name(cur)
            if n is None:
                break
            parts.append(n)
            nxt = self.snap.u64(cur + UOBJ_OUTER)
            if nxt is None:
                break
            cur = nxt
            depth -= 1
        return ".".join(reversed(parts))

    def class_name(self, addr: int) -> str | None:
        cls = self.snap.u64(addr + UOBJ_CLASS)
        if not cls:
            return None
        return self.object_name(cls)

    def iter_objects(self):
        for i in range(self.num):
            a = self.raw(i)
            if a:
                yield i, a


def main() -> int:
    ap = argparse.ArgumentParser(description="find UE5 GUObjectArray")
    ap.add_argument("capture")
    ap.add_argument("--json", help="write metadata here")
    ap.add_argument("--list", help="write index<TAB>class<TAB>fullname here")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    cap = Path(args.capture)
    with Snapshot(cap) as snap:
        print(snap.stats())
        g = load_gnames(snap, cap, verbose=args.verbose)
        print(f"\nGNames ready (by_id(0)={g.by_id(0)!r})")
        print("searching for GUObjectArray ...")
        info = find_gobjects(snap, g, verbose=args.verbose)
        if not info:
            print("\nFAILED: no GUObjectArray candidate satisfied "
                  "InternalIndex == slot.")
            print("Writing nothing -- a wrong base would corrupt the whole SDK.")
            return 1

        print(f"\n{'='*66}")
        print(f"  FUObjectArray   {info['fuobjectarray']:#014x}")
        print(f"  ObjObjects      {info['array']:#014x}")
        print(f"  chunk ptr array {info['objects']:#014x}")
        print(f"  NumElements     {info['num_elements']:,} "
              f"/ Max {info['max_elements']:,}")
        print(f"  NumChunks       {info['num_chunks']} / Max {info['max_chunks']}")
        print(f"  anchored on     {info['anchor']}")
        print(f"  INVARIANT       InternalIndex==slot: {info['matched']}"
              f"/{info['checked'] - info['null_slots']} live "
              f"({info['match_rate']*100:.1f}%), "
              f"{info['null_slots']} null slots")
        print(f"{'='*66}")

        go = GObjects(snap, info, g)
        print("\nfirst 15 objects:")
        shown = 0
        for i, addr in go.iter_objects():
            print(f"  [{i:>6}] {addr:#014x}  {go.class_name(addr) or '?':<28} "
                  f"{go.full_name(addr)}")
            shown += 1
            if shown >= 15:
                break

        if args.list:
            n = 0
            with open(args.list, "w") as fh:
                for i, addr in go.iter_objects():
                    fh.write(f"{i}\t{addr:#x}\t{go.class_name(addr) or ''}\t"
                             f"{go.full_name(addr)}\n")
                    n += 1
            print(f"\nwrote {n:,} objects to {args.list}")
        if args.json:
            meta = {k: v for k, v in info.items() if k != "anchor_object"}
            meta["anchor_object"] = {k: v for k, v in info["anchor_object"].items()}
            Path(args.json).write_text(json.dumps(meta, indent=1))
            print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
