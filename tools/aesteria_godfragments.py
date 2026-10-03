#!/usr/bin/env python3
"""aesteria_godfragments -- every feather (Empyrean Trace) spawn point in a zone, from the paks.

HOW THIS WAS FOUND (2026-10-01, all on ground truth):
    Feathers look server-spawned live (runtime names, outer = persistent level), and the ~60 m
    appearance radius is network relevancy. But the SPAWN POINTS ship in the client: each is an
    `AionSpawnUnitSingle` actor in its own tiny World Partition cell (~2.2 KB), named
    `E_D1_GodFragment_01_Air_<n>` or `..._Ground_<n>`. Proven by searching all 39,812 World_D_A
    cells for three feathers the operator collected that night -- each came back as an exact float64
    coordinate in exactly one such cell -- next to a positive control of three level-placed
    StaticMeshActors, all found the same way. (A first search with only 5,729 cells found neither
    feathers nor controls: the cells span pakchunk81002 AND _s1.._s6.)

    Position = the first float64 triple in world range inside the cell; the three known feathers
    reproduce to within a few units, and the offsets agree across cells. ⚠ Coordinates CAN be
    whole numbers (-40110.0, -92275.0): an "x is fractional" filter once dropped a real one.

    .venv/bin/python tools/aesteria_godfragments.py      (needs snaps/aes_key.hex + pyooz)
        -> snaps/godfragments_world_d_a.json   [{group, idx, x, y, z, cell}]
"""
from __future__ import annotations

import json
import math
import re
import struct
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aesteria_iostore import PAKS, extract, open_container         # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
KEY = ROOT / "snaps" / "aes_key.hex"
OUT = ROOT / "snaps" / "godfragments_world_d_a.json"
# Other collectible-looking spawn groups (the operator, 10-01: "add the unknown ones ... I might actually
# find them"). Same actor type and cell shape as the feathers; drawn in their own colours.
EXTRA_OUT = ROOT / "snaps" / "spawns_extra_world_d_a.json"
# Instance names vary: `E_D1_UmbabaTrace_3`, `E_D1_Daeva04_Trace_01_6`, `E_D1_HiddenCube_02`.
EXTRA_RE = re.compile(rb"\x00(E_D1_(?:HQ_GodFragment_02|Daeva04_Trace|UmbabaTrace|GodMonolith"
                      rb"|HiddenCube|TreasureBoxRare))((?:_\d+)*)\x00")
CONTAINERS = ["pakchunk81002-Windows_0_P.utoc"] + \
    [f"pakchunk81002_s{i}-Windows_0_P.utoc" for i in range(1, 7)]
NAME_RE = re.compile(rb"(E_[A-Za-z0-9]+_GodFragment_[A-Za-z0-9_]+?)_(\d+)\x00")
# Tonight's collected feathers, for the self-check.
KNOWN = [(-50657, -88816), (-36766, -81052), (-28031, -98397), (-40110, -92275)]


def position(d: bytes):
    """The PositionComponent's RelativeLocation, anchored from the END of the cell.

    The spawn actor's component is the last export, so its location sits a fixed distance from
    the end: 54 bytes back on 287 of 546 cells, 78 back when one more 24-byte property follows it
    (the rest). Scanning forward for the first plausible triple instead picked up unrelated
    values on ~5 % of cells and rejected whole-number coordinates -- both measured, both fixed.
    """
    for back in (54, 78, 112, 88):
        o = len(d) - back
        if o < 0:
            continue
        x, y, z = struct.unpack_from("<3d", d, o)
        if (all(map(math.isfinite, (x, y, z))) and 1000 < abs(x) < 4e5 and 1000 < abs(y) < 4e5
                and -3e4 < z < 1e5):
            return o, x, y, z
    return None


def first_triple(d: bytes):
    """Fallback for the extra groups, whose cells are not all the feather shape. UNVERIFIED:
    no collected instance of these groups exists to check it against."""
    for o in range(len(d) - 24):
        x, y, z = struct.unpack_from("<3d", d, o)
        if (all(map(math.isfinite, (x, y, z))) and 1000 < abs(x) < 4e5 and 1000 < abs(y) < 4e5
                and -3e4 < z < 1e5):
            return o, x, y, z
    return None


def main() -> int:
    key = bytes.fromhex(KEY.read_text().strip()[2:])
    found, offsets, extra = [], Counter(), []
    for name in CONTAINERS:
        c = open_container(PAKS / name, key)
        for f in c["files"]:
            if not f.endswith(".umap"):
                continue
            off, ln = c["offlen"][c["files"][f]]
            if ln > 65536:                     # spawn cells are 2-11 KB; skip real geometry
                continue
            d = extract(c, f)
            x = EXTRA_RE.search(d)
            if x and (px := position(d) or first_triple(d)):
                extra.append({"group": x.group(1).decode(), "idx": x.group(2).decode().strip("_"),
                              "x": px[1], "y": px[2], "z": px[3], "cell": Path(f).stem})
            if b"AionSpawnUnit" not in d:
                continue
            m = NAME_RE.search(d)
            if not m:
                continue
            p = position(d)
            if not p:
                continue
            offsets[p[0]] += 1
            found.append({"group": m.group(1).decode(), "idx": int(m.group(2)),
                          "x": p[1], "y": p[2], "z": p[3], "cell": Path(f).stem})
    ok = sum(any(abs(e["x"] - x) < 5 and abs(e["y"] - y) < 5 for e in found) for x, y in KNOWN)
    print(f"{len(found)} GodFragment spawns; self-check {ok}/{len(KNOWN)} known feathers")
    print("groups:", dict(Counter(e["group"] for e in found)))
    print("position offsets:", offsets.most_common(4))
    found.sort(key=lambda e: (e["group"], e["idx"]))
    OUT.write_text(json.dumps(found, indent=0))
    EXTRA_OUT.write_text(json.dumps(sorted(extra, key=lambda e: (e["group"], e["idx"])), indent=0))
    print("extra groups:", dict(Counter(e["group"] for e in extra)), "->", EXTRA_OUT.name)
    print("->", OUT)
    return 0 if ok == len(KNOWN) else 1


if __name__ == "__main__":
    sys.exit(main())
