#!/usr/bin/env python3
"""Dump a UE5 SDK (classes, structs, properties, offsets) from a capture.

This is the artifact the playtest window exists to produce. With it, every later
reader and script can be written and tested offline, with no server.

VALIDATION GATE -- why the numbers can be trusted
    UObject's own size is fixed by the engine at 0x28 bytes, and UField at 0x30.
    So before emitting anything we read PropertiesSize off
    /Script/CoreUObject.Object and check it equals 0x28. If it does not, our
    UStruct offsets are wrong for this build and we say so instead of writing a
    plausible-looking SDK full of wrong offsets -- which is far more dangerous
    than no SDK, because everything downstream would silently inherit the error.

    A second, independent check: every property's type name comes from an
    FFieldClass, and those names are drawn from a small closed set the engine
    defines (BoolProperty, ObjectProperty, StructProperty, ...). A high rate of
    recognised type names means the FField walk is landing on real fields.

LAYOUT (UE 5.3.2, 64-bit, no editor)
    UObject                                    size 0x28
    UField   : UObject { UField* Next; }        Next @ 0x28, size 0x30
    UStruct  : UField {
        UStruct* SuperStruct;                  // +0x30
        UField*  Children;                     // +0x38  (UFunctions)
        FField*  ChildProperties;              // +0x40  (properties)
        int32    PropertiesSize;               // +0x48
        int32    MinAlignment;                 // +0x4C
    }
    FField {
        void*        vtable;                   // +0x00
        FFieldClass* ClassPrivate;             // +0x08
        FFieldVariant Owner;                   // +0x10 (16 bytes)
        FField*      Next;                     // +0x20
        FName        NamePrivate;              // +0x28
        EObjectFlags FlagsPrivate;             // +0x30
    }                                          // size 0x38
    FProperty : FField {
        int32  ArrayDim;                       // +0x38
        int32  ElementSize;                    // +0x3C
        uint64 PropertyFlags;                  // +0x40
        uint16 RepIndex;                       // +0x48
        int32  Offset_Internal;                // +0x4C
    }
    FFieldClass { FName Name; uint64 Id; uint64 CastFlags; ... }   Name @ 0x00
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from aesteria_snapshot_reader import Snapshot                      # noqa: E402
from aesteria_gnames import GNames                                  # noqa: E402
from aesteria_gobjects import (GObjects, load_gnames, find_gobjects,  # noqa: E402
                               UOBJ_CLASS, UOBJ_NAME, UOBJ_OUTER)

# --- UStruct ---
# MEASURED on this build (2026-09-19), not taken from stock UE headers.
#
# Everything from SuperStruct onward sits 0x10 later than a default UE 5.3 build,
# because AION 2 is compiled with
#     USTRUCT_FAST_ISCHILDOF_IMPL == USTRUCT_ISCHILDOF_STRUCTARRAY
# which makes UStruct privately inherit FStructBaseChain:
#     FStructBaseChain** StructBaseChainArray;          // +0x30
#     int32 NumStructBasesInChainMinusOne;              // +0x38 (+4 pad)
# That base is declared after UField, so it lands between UField::Next and
# UStruct's own members.
#
# Proof it is right, from the capture:
#   UClass "Object": +0x58 -> 0x28 == sizeof(UObject); +0x40 (Super) == 0
#   UClass "Struct": +0x40 -> 'Field' (Struct : Field); +0x38 == 2, the true
#                    inheritance depth (Object 0, Field 1, Struct 2)
#   UClass "Object": +0x48 -> 'ExecuteUbergraph', a UFunction, so Children
FIELD_NEXT = 0x28
STRUCT_BASE_CHAIN_ARRAY = 0x30
STRUCT_BASE_CHAIN_DEPTH = 0x38
STRUCT_SUPER = 0x40
STRUCT_CHILDREN = 0x48
STRUCT_CHILD_PROPS = 0x50
STRUCT_PROPS_SIZE = 0x58
STRUCT_MIN_ALIGN = 0x5C

# --- FField / FProperty ---
# MEASURED on this build. Everything after Owner sits 8 bytes earlier than a
# stock UE 5.3 layout, because FFieldVariant here is a TAGGED POINTER (8 bytes)
# rather than {ptr, bool} padded to 16 -- Actor's first property reports
# Owner == 0x13fff9601, which is Actor's address (0x13fff9600) with bIsUObject
# packed into bit 0.
#
# Proof, from the capture:
#   FVector: X/Y/Z are DoubleProperty, ElementSize 8, Offset_Internal 0/8/16,
#            and UStruct.PropertiesSize == 24. All four agree.
#   Actor:   first ChildProperty is 'PrimaryActorTick' at offset 40 (0x28),
#            i.e. immediately after UObject, which is where it must be.
FF_CLASS = 0x08
FF_OWNER = 0x10
FF_NEXT = 0x18
FF_NAME = 0x20
FF_FLAGS = 0x28
FP_ARRAY_DIM = 0x30
FP_ELEM_SIZE = 0x34
FP_FLAGS = 0x38
FP_OFFSET = 0x44
FFC_NAME = 0x00

# The closed set the engine defines. Used as an independent signal that the FField
# walk is landing on real fields rather than on arbitrary heap bytes.
KNOWN_PROPERTY_TYPES = {
    "ByteProperty", "Int8Property", "Int16Property", "IntProperty",
    "Int64Property", "UInt16Property", "UInt32Property", "UInt64Property",
    "FloatProperty", "DoubleProperty", "BoolProperty", "ObjectProperty",
    "WeakObjectProperty", "LazyObjectProperty", "SoftObjectProperty",
    "ClassProperty", "SoftClassProperty", "InterfaceProperty", "NameProperty",
    "StrProperty", "TextProperty", "ArrayProperty", "MapProperty", "SetProperty",
    "StructProperty", "DelegateProperty", "MulticastDelegateProperty",
    "MulticastInlineDelegateProperty", "MulticastSparseDelegateProperty",
    "EnumProperty", "FieldPathProperty", "ObjectPtrProperty",
    "ClassPtrProperty", "OptionalProperty", "Utf8StrProperty",
    "AnsiStrProperty", "VerseFunctionProperty",
}

STRUCT_LIKE_CLASSES = {
    "Class", "ScriptStruct", "Function", "DelegateFunction",
    "SparseDelegateFunction", "Interface", "DynamicClass", "Struct",
}


def is_struct_like(class_name: str | None) -> bool:
    if not class_name:
        return False
    return (class_name in STRUCT_LIKE_CLASSES
            or class_name.endswith("GeneratedClass")
            or class_name.endswith("Class"))


class SDK:
    def __init__(self, snap: Snapshot, go: GObjects, g: GNames):
        self.snap = snap
        self.go = go
        self.g = g
        self.type_name_counter: Counter[str] = Counter()

    def field_class_name(self, field_addr: int) -> str | None:
        fc = self.snap.u64(field_addr + FF_CLASS)
        if not fc or not self.snap.looks_like_pointer(fc):
            return None
        nid = self.snap.u32(fc + FFC_NAME)
        return self.g.by_id(nid) if nid is not None else None

    def properties(self, struct_addr: int, cap: int = 4096) -> list[dict]:
        """Walk the ChildProperties linked list."""
        out: list[dict] = []
        cur = self.snap.u64(struct_addr + STRUCT_CHILD_PROPS)
        seen: set[int] = set()
        while cur and self.snap.looks_like_pointer(cur) and cur not in seen:
            seen.add(cur)
            if len(out) >= cap:
                break
            nid = self.snap.u32(cur + FF_NAME)
            name = self.g.by_id(nid) if nid is not None else None
            tname = self.field_class_name(cur)
            offset = self.snap.i32(cur + FP_OFFSET)
            elem = self.snap.i32(cur + FP_ELEM_SIZE)
            dim = self.snap.i32(cur + FP_ARRAY_DIM)
            flags = self.snap.u64(cur + FP_FLAGS)
            if name is None or tname is None or offset is None:
                break
            if tname:
                self.type_name_counter[tname] += 1
            out.append({"name": name, "type": tname, "offset": offset,
                        "element_size": elem, "array_dim": dim,
                        "flags": flags})
            nxt = self.snap.u64(cur + FF_NEXT)
            if nxt == cur:
                break
            cur = nxt
        return out

    def functions(self, struct_addr: int, cap: int = 4096) -> list[dict]:
        """Walk the Children linked list (UFunction objects)."""
        out: list[dict] = []
        cur = self.snap.u64(struct_addr + STRUCT_CHILDREN)
        seen: set[int] = set()
        while cur and self.snap.looks_like_pointer(cur) and cur not in seen:
            seen.add(cur)
            if len(out) >= cap:
                break
            nid = self.snap.u32(cur + UOBJ_NAME)
            name = self.g.by_id(nid) if nid is not None else None
            if name is None:
                break
            out.append({"name": name, "addr": cur,
                        "class": self.go.class_name(cur)})
            nxt = self.snap.u64(cur + FIELD_NEXT)
            if nxt == cur:
                break
            cur = nxt
        return out

    def struct_info(self, addr: int, index: int) -> dict:
        super_addr = self.snap.u64(addr + STRUCT_SUPER) or 0
        return {
            "index": index,
            "addr": addr,
            "name": self.go.object_name(addr),
            "full_name": self.go.full_name(addr),
            "meta_class": self.go.class_name(addr),
            "super": self.go.full_name(super_addr) if super_addr else None,
            "super_addr": super_addr,
            "properties_size": self.snap.i32(addr + STRUCT_PROPS_SIZE),
            "min_alignment": self.snap.i32(addr + STRUCT_MIN_ALIGN),
            "properties": self.properties(addr),
            "functions": self.functions(addr),
        }

    # ------------------------------------------------------------------ gating

    def ground_truth(self) -> tuple[bool, str]:
        """Two independent checks the engine fixes for us.

        1. sizeof: UObject is 0x28 and UField is 0x30 in every UE5 build.
        2. FStructBaseChain depth: Object=0, Field=1, Struct=2 -- the real
           inheritance depth. This cross-checks a DIFFERENT offset than
           PropertiesSize, so passing both is not one lucky guess twice.
        """
        expect_size = {"/Script/CoreUObject.Object": 0x28,
                       "/Script/CoreUObject.Field": 0x30}
        expect_depth = {"/Script/CoreUObject.Object": 0,
                        "/Script/CoreUObject.Field": 1,
                        "/Script/CoreUObject.Struct": 2}
        wanted = set(expect_size) | set(expect_depth) | {"/Script/CoreUObject.Vector"}
        found: dict[str, tuple[int | None, int | None]] = {}
        vector_addr: int | None = None
        for i, addr in self.go.iter_objects():
            fn = self.go.full_name(addr)
            if fn in wanted and fn not in found:
                found[fn] = (self.snap.i32(addr + STRUCT_PROPS_SIZE),
                             self.snap.i32(addr + STRUCT_BASE_CHAIN_DEPTH))
                if fn == "/Script/CoreUObject.Vector":
                    vector_addr = addr
                if len(found) == len(wanted):
                    break
        lines = []
        ok = True
        for fn in sorted(wanted):
            size, depth = found.get(fn, (None, None))
            bits = []
            if fn in expect_size:
                good = size == expect_size[fn]
                ok = ok and good
                bits.append(f"size={size} (want {expect_size[fn]}) "
                            f"{'PASS' if good else 'FAIL'}")
            if fn in expect_depth:
                good = depth == expect_depth[fn]
                ok = ok and good
                bits.append(f"depth={depth} (want {expect_depth[fn]}) "
                            f"{'PASS' if good else 'FAIL'}")
            lines.append(f"    {fn:<36} " + ", ".join(bits))

        # Third check, and the only one that exercises the FField/FProperty walk:
        # FVector must be exactly three DoubleProperty at 0/8/16. The struct-size
        # and depth checks above cannot detect a wrong property layout at all --
        # that is how a 528-property dump first looked like a success.
        if vector_addr is None:
            ok = False
            lines.append("    /Script/CoreUObject.Vector           NOT FOUND  FAIL")
        else:
            props = self.properties(vector_addr)
            got = [(p["name"], p["type"], p["offset"], p["element_size"])
                   for p in props]
            want = [("X", "DoubleProperty", 0, 8),
                    ("Y", "DoubleProperty", 8, 8),
                    ("Z", "DoubleProperty", 16, 8)]
            good = sorted(got) == sorted(want)
            ok = ok and good
            lines.append(f"    {'/Script/CoreUObject.Vector':<36} "
                         f"properties={got}  {'PASS' if good else 'FAIL'}")
            if not good:
                lines.append(f"      expected (any order): {want}")
        return ok, "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="dump a UE5 SDK from a capture")
    ap.add_argument("capture")
    ap.add_argument("-o", "--out", default=None,
                    help="output dir (default <capture>/sdk)")
    ap.add_argument("--min-props", type=int, default=0,
                    help="only emit structs with at least this many properties")
    ap.add_argument("--filter", default=None,
                    help="only dump structs whose full name contains this")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    cap = Path(args.capture)
    out = Path(args.out) if args.out else cap / "sdk"
    out.mkdir(parents=True, exist_ok=True)

    with Snapshot(cap) as snap:
        print(snap.stats())
        g = load_gnames(snap, cap, verbose=args.verbose)
        meta_path = cap / "gobjects.json"
        if meta_path.exists():
            info = json.loads(meta_path.read_text())
            print(f"GUObjectArray from cache: {info['fuobjectarray']:#x} "
                  f"({info['num_elements']:,} elements)")
        else:
            info = find_gobjects(snap, g, verbose=args.verbose)
            if not info:
                return 1
        go = GObjects(snap, info, g)
        sdk = SDK(snap, go, g)

        print("\nGROUND TRUTH (UStruct offsets for this build):")
        ok, report = sdk.ground_truth()
        print(report)
        if not ok:
            print("\nFAILED: UStruct layout does not match UE 5.3.2 expectations.")
            print("Refusing to emit an SDK -- wrong offsets are worse than none,")
            print("because every reader built on them would inherit the error.")
            return 1

        print("\nwalking objects for struct-like types ...")
        structs: list[dict] = []
        counts: Counter[str] = Counter()
        for i, addr in go.iter_objects():
            cn = go.class_name(addr)
            if not is_struct_like(cn):
                continue
            counts[cn or "?"] += 1
            si = sdk.struct_info(addr, i)
            if args.filter and args.filter.lower() not in (si["full_name"] or "").lower():
                continue
            if len(si["properties"]) < args.min_props:
                continue
            structs.append(si)
            if args.verbose and len(structs) % 5000 == 0:
                print(f"  ... {len(structs)} structs", flush=True)

        total_props = sum(len(s["properties"]) for s in structs)
        total_funcs = sum(len(s["functions"]) for s in structs)
        recognised = sum(n for t, n in sdk.type_name_counter.items()
                         if t in KNOWN_PROPERTY_TYPES)
        all_typed = sum(sdk.type_name_counter.values()) or 1
        rate = recognised / all_typed

        print(f"\n{'='*66}")
        print(f"  structs/classes dumped : {len(structs):,}")
        print(f"  properties             : {total_props:,}")
        print(f"  functions              : {total_funcs:,}")
        print(f"  recognised type names  : {recognised:,}/{all_typed:,} "
              f"({rate*100:.2f}%)")
        print(f"{'='*66}")
        print("  meta-class breakdown:")
        for cn, n in counts.most_common(10):
            print(f"    {cn:<34} {n:>8,}")
        print("\n  top property types:")
        for t, n in sdk.type_name_counter.most_common(12):
            mark = "" if t in KNOWN_PROPERTY_TYPES else "  <-- UNRECOGNISED"
            print(f"    {t:<34} {n:>8,}{mark}")

        if rate < 0.95:
            print(f"\nWARNING: only {rate*100:.1f}% of property type names are "
                  f"recognised engine types.")
            print("The FField offsets may be off for this build. Emitting anyway,")
            print("but treat offsets as unproven until spot-checked.")

        (out / "structs.json").write_text(json.dumps(structs, indent=1))
        print(f"\nwrote {out/'structs.json'}")

        # A flat, greppable index is what actually gets used at 4am.
        with open(out / "offsets.tsv", "w") as fh:
            fh.write("#struct\tsize\tproperty\ttype\toffset\telem_size\tarray_dim\n")
            for s in structs:
                for p in s["properties"]:
                    fh.write(f"{s['full_name']}\t{s['properties_size']}\t"
                             f"{p['name']}\t{p['type']}\t{p['offset']}\t"
                             f"{p['element_size']}\t{p['array_dim']}\n")
        print(f"wrote {out/'offsets.tsv'}")

        summary = {
            "capture": str(cap),
            "engine": "UE5.3.2",
            "gnames_pool": info.get("pool") if isinstance(info, dict) else None,
            "fuobjectarray": info["fuobjectarray"],
            "num_objects": info["num_elements"],
            "structs": len(structs),
            "properties": total_props,
            "functions": total_funcs,
            "type_name_recognition": rate,
            "ground_truth_passed": ok,
        }
        (out / "summary.json").write_text(json.dumps(summary, indent=1))
        print(f"wrote {out/'summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
