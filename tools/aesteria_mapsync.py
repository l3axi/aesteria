#!/usr/bin/env python3
"""aesteria_mapsync -- what the WORLD MAP knows: every uncollected feather, every Kibelisk's state.

the operator, 10-02: "the world map displays all unobtained feathers ... collected feathers do not
appear". The overlay alone only learns a feather's state when it streams in (~60 m), and ~270 of
his 405 were collected before it existed -- so the route kept sending him to collected ones.
The map's marker widgets carry the answer for the whole zone at once:

    FWindow_WorldMapInfo_MonolithFragment_C   one per feather (560 live, 10-02 06:22)
        +1104  world position, 3 doubles  (532/560 land within 5 m of a pak spawn point)
        +228   UWidget.Visibility: 4 SelfHitTestInvisible = SHOWN = uncollected,
                                   1 Collapsed = HIDDEN = collected  (179 shown / 381 hidden)
               Not viewport culling: the shown markers are scattered over the unfinished regions.
    FWindow_WorldMapInfo_TeleportArtifact_C   one per Kibelisk (61; 40 match the pak Kibelisks)
        +1104  world position
        +1048  263 on 35 of them, 7 on 5 -- bit 0x100 is taken to mean ACTIVATED. UNPROVEN until
               the operator confirms one of the 5 (Collapsed Hall Of Fame, End Stall, Island Of Spirit,
               Kumrica Cellar, Secret Rendezvous Point) is not activated in-game.

The widgets exist only while the map is open, so a snapshot is saved to snaps/map_state.json and
the overlay re-reads it every time the operator opens the map. A snapshot with fewer than MIN_FEATHER
markers is ignored (map still building).

    python3 tools/aesteria_mapsync.py      # snapshot now (map must be open)
"""
from __future__ import annotations

import json
import math
import struct
import sys
import time
from pathlib import Path

FEATHER_WIDGET = "FWindow_WorldMapInfo_MonolithFragment_C"
KIBELISK_WIDGET = "FWindow_WorldMapInfo_TeleportArtifact_C"
POS, VISIBILITY, KIB_FLAGS = 1104, 228, 1048
COLLAPSED = 1
KIB_ACTIVATED_BIT = 0x100
MIN_FEATHER = 500
OUT = Path(__file__).resolve().parents[1] / "snaps" / "map_state.json"


def snapshot(live, feathers: list[int], kibelisks: list[int]) -> dict | None:
    """Read the marker widgets. None if the map is not (fully) open."""
    if len(feathers) < MIN_FEATHER:
        return None
    out = {"taken": time.strftime("%Y-%m-%d %H:%M"), "feathers": [], "kibelisks": []}
    for a in feathers:
        raw = live.read(a, POS + 24)
        if not raw or len(raw) < POS + 24:
            continue
        x, y, z = struct.unpack_from("<3d", raw, POS)
        if all(map(math.isfinite, (x, y, z))):
            out["feathers"].append({"x": x, "y": y, "z": z, "shown": raw[VISIBILITY] != COLLAPSED})
    for a in kibelisks:
        raw = live.read(a, POS + 24)
        if not raw or len(raw) < POS + 24:
            continue
        x, y, z = struct.unpack_from("<3d", raw, POS)
        flags = struct.unpack_from("<i", raw, KIB_FLAGS)[0]
        if all(map(math.isfinite, (x, y, z))):
            out["kibelisks"].append({"x": x, "y": y, "z": z,
                                     "activated": bool(flags & KIB_ACTIVATED_BIT)})
    return out if len(out["feathers"]) >= MIN_FEATHER else None


def save(state: dict) -> None:
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(OUT)


def load() -> dict | None:
    try:
        return json.loads(OUT.read_text())
    except (OSError, ValueError):
        return None


def done_ids(state: dict | None, nodes: list[dict], radius: float = 500.0) -> tuple[set, set]:
    """(done, open) route ids according to the map: a feather is done when its marker is hidden,
    open when shown; a Kibelisk is done when activated. Nodes with no marker within `radius`
    (5 m) are in neither set -- the caller falls back to what it saw up close."""
    done, still = set(), set()
    if not state:
        return done, still
    grid: dict = {}
    for kind, rows in (("feather", state["feathers"]), ("kibelisk", state["kibelisks"])):
        for r in rows:
            grid.setdefault((kind, round(r["x"] / 2000), round(r["y"] / 2000)), []).append(r)
    for n in nodes:
        if n["kind"] not in ("feather", "kibelisk"):
            continue
        cx, cy = round(n["x"] / 2000), round(n["y"] / 2000)
        best, bd = None, radius if n["kind"] == "feather" else 1000.0
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for r in grid.get((n["kind"], cx + dx, cy + dy), ()):
                    d = math.hypot(r["x"] - n["x"], r["y"] - n["y"])
                    if d < bd:
                        best, bd = r, d
        if best is None:
            continue
        if (n["kind"] == "feather" and not best["shown"]) or \
                (n["kind"] == "kibelisk" and best["activated"]):
            done.add(n["id"])
        else:
            still.add(n["id"])
    return done, still


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from aesteria_gobjects import GObjects
    from aesteria_live import LiveProcess, load_live_meta
    live = LiveProcess(None)
    with live:
        g, meta = load_live_meta(live, OUT.parent / "meta", False)
        go = GObjects(live, meta, g)
        go.refresh()
        fw, kw = [], []
        for _i, a in go.iter_objects():
            c = go.class_name(a)
            if c == FEATHER_WIDGET:
                fw.append(a)
            elif c == KIBELISK_WIDGET:
                kw.append(a)
        st = snapshot(live, fw, kw)
    if not st:
        print(f"map not open ({len(fw)} feather markers) -- open the world map and retry")
        return 1
    save(st)
    shown = sum(f["shown"] for f in st["feathers"])
    act = sum(k["activated"] for k in st["kibelisks"])
    print(f"{len(st['feathers'])} feather markers, {shown} uncollected · "
          f"{len(st['kibelisks'])} Kibelisk markers, {act} activated -> {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
