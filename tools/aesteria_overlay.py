#!/usr/bin/env python3
"""aesteria_overlay -- external on-screen ESP: boxes over feathers and hidden cubes.

NO INJECTION. A transparent, click-through window the size of the game's
monitor, drawing from /proc/<pid>/mem reads. A wlr-layer-shell OVERLAY surface (GTK4), so it
stays above the borderless game. The game never knows it exists.

TWO SPEEDS, because a full object walk takes seconds and a box must follow the camera:
    discovery  (background thread, ~every 2 s): walk GUObjectArray, keep BP_EnvObj_Common_C
               whose mesh matches a wanted kind, remember each one's ADDRESS and root component
    per frame  (~30 Hz): re-read only the camera and those roots' positions, project, draw
So a newly streamed feather appears within a discovery pass, and a box stays glued to its
object between passes.

WHAT IS DRAWN (edit KINDS to taste)
    feather       Protect_01, only while +0x26c == 1 (uncollected) -- proven 3/3 on 10-01
    hidden cube   HiddenCube_*  -- position proven once (06:05); its state field is NOT known,
                  so every loaded cube is drawn
    --all         every BP_EnvObj_Common_C, grey, labelled by mesh (ore, plants, odium ...)

PROJECTION: aesteria_camera.Camera. Proven on two near-centre points (the cube "just below my
crosshair", a feather "right on my crosshair"). The FOV scale toward the screen edges is the
thing this overlay exists to check: a box that drifts off its object near the edge says so.

DUNGEON ENTRANCES (aesteria_dungeons.py, 103 from the paks) are drawn as triangles: gold = party
dungeon, orange = solo dungeon, teal = Undiscovered Area, violet = garrison, grey "?" = portal of unknown
purpose, sky blue = Kibelisk (teleport artifact), pink = event (Daeva/Dragon/Human NN). 11 positions come from a fallback scan and are
UNVERIFIED.

F9 toggles the COMPLETIONIST ROUTE (aesteria_route.py): one optimised loop through every feather
and dungeon, cut at the nearest unvisited point. The next 6 stops are drawn as a numbered line,
the next one big (edge arrow when off screen). Collected feathers drop out on their own; a
dungeon counts as visited once you come within 15 m of its entrance (snaps/route_visited.json).
Toggle off/on to re-anchor after a detour. --route starts with it on.
F10 marks the NEXT stop done by hand (the operator, 10-02: the route opened on a garrison he had already
cleared -- a cleared dungeon leaves nothing in memory or the paks to read). It is written to
snaps/route_visited.json; delete its line there to bring a stop back.

--cleanse presses the Healing Potion (F2) when a status effect is on you (aesteria_cleanse.py):
reads the HUD's own debuff icons, presses only while the game has focus, and parks a debuff the
potion does not remove. Scroll Lock toggles it. --cleanse-dry logs instead of pressing.

F6 toggles the WING BOOST (aesteria_wingboost.py): 4 m along your flight every 0.75 s while
flying fast and straight -- the measured server budget, ~33 % faster. Status in the HUD line.

Run it from the operator's desktop session (it needs WAYLAND_DISPLAY):
    python3 tools/aesteria_overlay.py [--all] [--screen 2560x1440] [--range 150]
Ctrl-C in the terminal quits.
"""

from __future__ import annotations

import argparse
import json
import struct
import math
import os
import signal

import cairo
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aesteria_camera import Camera                                  # noqa: E402
from aesteria_gobjects import GObjects                              # noqa: E402
from aesteria_live import LiveProcess, load_live_meta               # noqa: E402
from aesteria_player import (find_local_player, read_vector,        # noqa: E402
                             ACTOR_ROOT_COMPONENT, SCENE_RELATIVE_LOCATION)
from aesteria_radar import mesh_name                                # noqa: E402
from aesteria_route import Route, OUT as ROUTE_FILE                  # noqa: E402
import aesteria_mapsync as mapsync                                  # noqa: E402

META_CACHE = Path(__file__).resolve().parents[1] / "snaps" / "meta"
# Every feather ever seen, kept across runs. The client only streams a feather in within ~60 m,
# so memory reads alone can never show one at 200-300 m. Feathers never move, so a position seen
# once is good forever: after one pass through an area, the overlay draws them at any range.
# Keyed by position rounded to 1 m; "state" is the last +0x26c read (1 available, 2 collected).
KNOWN_FILE = Path(__file__).resolve().parents[1] / "snaps" / "feathers.json"
# Every feather SPAWN POINT in the zone, from the paks (aesteria_godfragments.py): 375 in
# World_D_A. Drawn at any range; hidden once the live read (or feathers.json) says collected.
SPAWNS_FILE = Path(__file__).resolve().parents[1] / "snaps" / "godfragments_world_d_a.json"
# Other spawn groups from the same pass, drawn as diamonds (the operator, 10-01: "add the unknown ones
# ... I might actually find them"). Their positions are UNVERIFIED -- nothing of these was ever
# collected to check against. Yellow = unknown what it is in-game.
EXTRA_FILE = Path(__file__).resolve().parents[1] / "snaps" / "spawns_extra_world_d_a.json"
EXTRA_STYLE = {
    "E_D1_HiddenCube": ("hidden cube", (0.31, 0.78, 1.0, 0.8)),
    # the operator, 10-01: "GodMonolith is where you hand the feathers in".
    "E_D1_GodMonolith": ("feather hand-in", (0.75, 0.55, 1.0, 0.85)),
}
# the operator, 10-01: TreasureBoxRare is "not visible/there in game" -- 56 spawn points, no chests.
# Kept in the JSON (the paks do hold them), just not drawn.
# the operator, 10-02, standing at UmbabaTrace 003 (pak position 2 m off his): a black-and-white
# feathered NPC beside a MauTotem_01 env object -- neither can be interacted with. Scenery.
EXTRA_HIDDEN = {"E_D1_TreasureBoxRare", "E_D1_UmbabaTrace"}
UNKNOWN = (1.0, 1.0, 0.2, 0.9)
DUNGEON_FILE = Path(__file__).resolve().parents[1] / "snaps" / "dungeons_world_d_a.json"
VISITED_FILE = Path(__file__).resolve().parents[1] / "snaps" / "route_visited.json"
DUNGEON_STYLE = {"party": (1.0, 0.78, 0.2, 0.95), "solo": (1.0, 0.55, 0.15, 0.9),
                 "seal": (0.3, 0.9, 0.85, 0.8), "seal+": (0.3, 1.0, 0.9, 0.95),
                 "keep": (0.75, 0.55, 1.0, 0.9), "portal": (0.75, 0.75, 0.8, 0.7),
                 "kibelisk": (0.45, 0.7, 1.0, 0.95), "event": (1.0, 0.45, 0.75, 0.95)}
ROUTE = (0.55, 0.95, 1.0, 0.95)
VISIT_RADIUS, VISIT_DZ = 1500.0, 2000.0          # 15 m across, 20 m up/down
# Walking past an Undiscovered Area does not discover it, and a Kibelisk counts once the map says
# so -- these never auto-complete on proximity; F10 (or the map) marks them.
MANUAL_KINDS = ("seal", "kibelisk", "event")
CHARACTER_MOVEMENT = 968        # Character.CharacterMovement (SDK)
# FACTION (--pvp), read live 10-01: Character.Mesh +960 -> SkeletalMeshComponent.AnimScriptInstance
# +2440 -> AionAnimInstance.PcRace +1430, enum ERace (resolved from the live UEnum):
# 0 None, 1 Light (Elyos), 2 Dark (Asmodian). The test character reads 2 -- black wings, Asmodian. Pooled /
# idle PC actors read 0 and are skipped. An ENEMY is any player whose race is 1|2 and not mine.
CHAR_MESH, MESH_ANIM_INSTANCE, ANIM_PC_RACE = 960, 2440, 1430
RACE_NAME = {1: "Elyos", 2: "Asmodian"}
ENEMY = (1.0, 0.15, 0.25, 0.95)
ENVOBJ = "BP_EnvObj_Common_C"
FEATHER_STATE = 0x26C           # 1 = available, 2 = collected
# AionEnvObj.CapsuleComponent. The root sits on the GROUND and the feather floats above it (the operator,
# 10-01: "the box renders right on the ground where the feather is hovering"). The capsule's
# relative Z (250 on Protect_01) is the game's own idea of where the object is -- box goes there.
ENVOBJ_CAPSULE = 1096
# HP from the nameplate UI (aesteria_hp.py). AionProgressBar fill is +0x510, not Percent.
CHAR_NAMEPLATE_WC, WC_PLATE, PLATE_HP_BAR, AIONPBAR_FILL = 0x2810, 0x5F0, 2040, 0x510

# (label, mesh prefix, colour, needs_state_1, lift in units or None = use the capsule's)
# Feather lift is the operator's eye, 10-01: the capsule's +250 put the box "one notch above half",
# so the visible feather floats at ~1.3 m.
KINDS = [
    ("feather", "Protect_01", (1.0, 0.27, 0.35, 1.0), True, 130.0),
    ("hidden cube", "HiddenCube", (0.31, 0.78, 1.0, 1.0), False, None),
]


def _finite(v) -> bool:
    """A recycled actor's root can read NaN/inf between discovery passes (10-01, live: every
    frame raised `cannot convert float NaN to integer` until the next pass dropped it)."""
    return bool(v) and all(math.isfinite(c) and abs(c) < 1e7 for c in v)


def _key(loc) -> str:
    return f"{round(loc[0] / 100)},{round(loc[1] / 100)},{round(loc[2] / 100)}"


class World:
    """Shared between the discovery thread and the paint loop."""

    def __init__(self, live: LiveProcess, go: GObjects, g, args):
        self.live, self.go, self.g, self.args = live, go, g, args
        self.lock = threading.Lock()
        try:
            self.known: dict = json.loads(KNOWN_FILE.read_text())
        except (OSError, ValueError):
            self.known = {}
        try:
            self.spawns = json.loads(SPAWNS_FILE.read_text())
        except (OSError, ValueError):
            self.spawns = []
        try:
            self.extra = [e for e in json.loads(EXTRA_FILE.read_text())
                          if e.get("group") not in EXTRA_HIDDEN]
        except (OSError, ValueError):
            self.extra = []
        self.me = None
        try:
            self.dungeons = json.loads(DUNGEON_FILE.read_text())
        except (OSError, ValueError):
            self.dungeons = []
        try:
            self.route = Route.load(ROUTE_FILE)
        except (OSError, ValueError, KeyError):
            self.route = None
        try:
            self.visited: set[str] = set(json.loads(VISITED_FILE.read_text()))
        except (OSError, ValueError):
            self.visited = set()
        self.route_on = False
        self.debuff_icons: list[int] = []   # the HUD's debuff icon widgets on you (--cleanse)
        self.cleanser = None
        self.map_state = mapsync.load()   # last world-map snapshot (aesteria_mapsync.py)
        self.done: set[str] = set()     # route ids already collected / visited
        self.root = None        # the local pawn's root component, for the wing boost
        self.move_comp = None   # the local pawn's CharacterMovement, so the boost fires only in flight
        self.chars = []         # characters with a nameplate HP bar (--hp)
        self.enemies = []       # enemy-faction players (--pvp): {root, bar}
        self.my_race = 0
        self.boost = None
        self.targets: list[dict] = []   # {addr, root, label, colour, state_gate}
        self.controller = None
        self.status = "discovering…"

    def discover_forever(self):
        while True:
            t0 = time.time()
            try:
                self._discover()
            except Exception as e:                       # a torn read must not kill the loop
                self.status = f"discovery error: {e}"
            time.sleep(max(0.5, 2.0 - (time.time() - t0)))

    def _discover(self):
        live, go = self.live, self.go
        live.refresh_regions()
        go.refresh()
        players = find_local_player(live, go, self.g)
        if not players:
            with self.lock:
                self.targets, self.controller = [], None
            self.status = "not in-world"
            return
        me = players[0]
        found, chars, enemies = [], [], []
        my_race = self._race(me["pawn"]) if (self.args.pvp or self.args.pvp_test) else 0
        dirty = False
        map_f, map_k, debuffs = [], [], []
        for _i, addr in go.iter_objects():
            cname = go.class_name(addr)
            if cname == mapsync.FEATHER_WIDGET:
                map_f.append(addr)
                continue
            if cname == mapsync.KIBELISK_WIDGET:
                map_k.append(addr)
                continue
            if cname == "Slot_AbnormalIcon_C" and self.cleanser is not None:
                fn = go.full_name(addr)
                if "190_UserInfo" in fn and "P_AbnormalListDebuff" in fn:
                    debuffs.append(addr)
                continue
            if (self.args.pvp or self.args.pvp_test) and cname == "BPCh_PcBase_C" and addr != me["pawn"]:
                race = self._race(addr)
                hostile = race != my_race or self.args.pvp_test
                if race in RACE_NAME and my_race in RACE_NAME and hostile:
                    root = live.u64(addr + ACTOR_ROOT_COMPONENT)
                    wc = live.u64(addr + CHAR_NAMEPLATE_WC)
                    plate = live.u64(wc + WC_PLATE) if wc else 0
                    if root:
                        enemies.append({"root": root, "race": race,
                                        "bar": live.u64(plate + PLATE_HP_BAR) if plate else 0})
            if self.args.hp and cname and cname.startswith("BPCh_") and addr != me["pawn"]:
                # HP chain, proven 10-01 against the HUD to 4 decimals:
                # character +0x2810 -> WC_NamePlate_C +0x5f0 -> NamePlate_C +2040 -> bar +0x510
                wc = live.u64(addr + CHAR_NAMEPLATE_WC)
                plate = live.u64(wc + WC_PLATE) if wc else 0
                bar = live.u64(plate + PLATE_HP_BAR) if plate else 0
                root = live.u64(addr + ACTOR_ROOT_COMPONENT)
                if bar and root:
                    chars.append({"root": root, "bar": bar, "pc": cname == "BPCh_PcBase_C"})
                continue
            if cname != ENVOBJ:
                continue
            root = live.u64(addr + ACTOR_ROOT_COMPONENT)
            if not root:
                continue
            loc = read_vector(live, root + SCENE_RELATIVE_LOCATION)
            if not _finite(loc) or sum((loc[k] - me["location"][k]) ** 2 for k in range(3)) \
                    > (self.args.range * 100) ** 2:
                continue
            mesh = mesh_name(live, go, addr)
            kind = next((k for k in KINDS if mesh.startswith(k[1])), None)
            cap = live.u64(addr + ENVOBJ_CAPSULE)
            lift = read_vector(live, cap + SCENE_RELATIVE_LOCATION) if cap else None
            lift = lift[2] if lift and 0 <= lift[2] < 1000 else 0.0
            if kind and kind[0] == "feather":
                st = live.u32(addr + FEATHER_STATE)
                if st in (1, 2):
                    k = _key(loc)
                    if self.known.get(k, {}).get("state") != st:
                        self.known[k] = {"x": loc[0], "y": loc[1], "z": loc[2], "state": st,
                                         "seen": time.strftime("%Y-%m-%d %H:%M")}
                        dirty = True
            if kind:
                found.append({"addr": addr, "root": root, "label": kind[0], "colour": kind[2],
                              "gate": kind[3], "lift": kind[4] if kind[4] is not None else lift})
            elif self.args.all:
                found.append({"addr": addr, "root": root, "label": mesh, "gate": False,
                              "colour": (0.8, 0.8, 0.8, 0.65), "lift": lift})
        if dirty:
            tmp = KNOWN_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.known, indent=0))
            tmp.replace(KNOWN_FILE)
        with self.lock:
            self.targets, self.controller, self.me = found, me["controller"], me["location"]
            self.chars = chars
            self.enemies, self.my_race = enemies, my_race
            self.debuff_icons = debuffs
            self.root = me["root_component"]
            self.move_comp = live.u64(me["pawn"] + CHARACTER_MOVEMENT)
        if map_f and (snap := mapsync.snapshot(live, map_f, map_k)):
            # the world map is open: it lists every uncollected feather in the zone
            mapsync.save(snap)
            self.map_state = snap
        self._update_done(me["location"])
        avail = sum(1 for v in self.known.values() if v["state"] == 1)
        self.status = f"{len(found)} loaded · {avail} uncollected feathers remembered"
        if self.args.pvp or self.args.pvp_test:
            self.status += f" · you {RACE_NAME.get(my_race, '?')}"

    def _update_done(self, me) -> None:
        """Route bookkeeping, once per discovery pass: a feather spawn is done when a collected
        feather was seen within 3 m of it; a dungeon is done once the operator has stood at it."""
        if not self.route:
            return
        # Feathers: the world-map snapshot is the authority (it knows the whole zone); a feather
        # seen collected up close AFTER the snapshot was taken also counts. Without a snapshot,
        # fall back to what the overlay has seen up close.
        taken = (self.map_state or {}).get("taken", "")
        map_done, map_open = mapsync.done_ids(self.map_state, self.route.nodes)
        collected = [(v["x"], v["y"], v.get("seen", "")) for v in self.known.values()
                     if v["state"] == 2]
        done, fresh = set(self.visited) | map_done, False
        for n in self.route.nodes:
            if n["kind"] == "feather" and n["id"] not in done:
                if any(abs(n["x"] - cx) < 300 and abs(n["y"] - cy) < 300
                       and (n["id"] not in map_open or seen > taken)
                       for cx, cy, seen in collected):
                    done.add(n["id"])
            elif (me and n["id"] not in self.visited and n["kind"] not in MANUAL_KINDS
                  and math.hypot(n["x"] - me[0], n["y"] - me[1]) < VISIT_RADIUS
                  and abs(n["z"] - me[2]) < VISIT_DZ):
                self.visited.add(n["id"])
                done.add(n["id"])
                fresh = True
        if fresh:
            tmp = VISITED_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(self.visited)))
            tmp.replace(VISITED_FILE)
        with self.lock:
            self.done = done

    def skip_next(self) -> str | None:
        """F10: mark the next stop done (already cleared, or not wanted) and persist it."""
        if not (self.route and self.route_on):
            return None
        with self.lock:
            stops = self.route.upcoming(self.done, 1)
            if not stops:
                return None
            sid = stops[0]["id"]
            self.visited.add(sid)
            self.done.add(sid)
        tmp = VISITED_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(self.visited)))
        tmp.replace(VISITED_FILE)
        return stops[0]["label"]

    def toggle_route(self) -> bool:
        if not self.route:
            print("no route file -- run tools/aesteria_route.py first")
            return False
        with self.lock:
            self.route_on = not self.route_on
            if self.route_on:
                self.route.start(self.me, self.done)
        return self.route_on

    def _race(self, char: int) -> int:
        mesh = self.live.u64(char + CHAR_MESH)
        anim = self.live.u64(mesh + MESH_ANIM_INSTANCE) if mesh else 0
        raw = self.live.read(anim + ANIM_PC_RACE, 1) if anim else None
        return raw[0] if raw else 0


def draw(world: World, cr, w: int, h: int):
    """One frame, cairo. Called from the GTK draw callback."""
    live = world.live
    with world.lock:
        targets, ctrl = list(world.targets), world.controller
    cr.set_operator(cairo.OPERATOR_CLEAR)
    cr.paint()
    cr.set_operator(cairo.OPERATOR_OVER)
    cr.select_font_face("Maple Mono NF CN")
    cr.set_font_size(14)
    cr.set_source_rgba(1, 1, 1, 0.55)
    cr.move_to(12, h - 12)
    boost = f" · {world.boost.status()}" if world.boost else ""
    cleanse = f" · {world.cleanser.status()}" if world.cleanser else ""
    cr.show_text(f"aesteria · {world.status}{boost}{cleanse}")
    if not ctrl:
        return
    cam = Camera.read(live, ctrl)
    if cam is None:
        return
    loaded = set()
    for t in targets:
        b = read_vector(live, t["root"] + SCENE_RELATIVE_LOCATION)
        if _finite(b):
            loaded.add(_key(b))
    with world.lock:
        known, me = dict(world.known), world.me
    collected = [(v["x"], v["y"]) for v in known.values() if v["state"] == 2]
    # every spawn point from the paks, plus any remembered feather the paks somehow lack
    with world.lock:
        done = set(world.done)
    points = [(e["x"], e["y"], e["z"], e["group"].rsplit("_", 1)[-1].lower()) for e in world.spawns
              if f"F:{e['group']}_{e['idx']}" not in done]
    points += [(v["x"], v["y"], v["z"], "seen") for v in known.values() if v["state"] == 1]
    for (px, py, pz, tag) in points:
        if not me or _key((px, py, pz)) in loaded:
            continue
        if any(abs(px - cx) < 300 and abs(py - cy) < 300 for cx, cy in collected):
            continue
        loc = (px, py, pz + 130.0)
        if sum((loc[i] - me[i]) ** 2 for i in range(3)) > (world.args.range * 100) ** 2:
            continue
        pr = cam.project(loc, w, h)
        if not pr or not (-50 < pr[0] < w + 50 and -50 < pr[1] < h + 50):
            continue
        x, y, depth = pr
        cr.set_source_rgba(1.0, 0.27, 0.35, 0.55)      # dimmer: not loaded yet
        cr.set_line_width(1.5)
        cr.arc(x, y, 7, 0, 6.2832)
        cr.stroke()
        cr.move_to(x + 10, y + 4)
        cr.show_text(f"feather {tag}  {depth / 100:.0f} m")
    with world.lock:
        chars = list(world.chars)
    for ch in chars:
        loc = read_vector(live, ch["root"] + SCENE_RELATIVE_LOCATION)
        if not _finite(loc):
            continue
        fill = struct.unpack("<f", struct.pack("<I", live.u32(ch["bar"] + AIONPBAR_FILL) or 0))[0]
        if not (0.0 < fill <= 1.0):          # 0.0 = hidden/recycled plate, not HP
            continue
        pr = cam.project((loc[0], loc[1], loc[2] + 120.0), w, h)
        if not pr or not (0 < pr[0] < w and 0 < pr[1] < h):
            continue
        x, y, depth = pr
        bw = max(30, min(90, 6000 / max(depth, 1)))
        cr.set_source_rgba(0, 0, 0, 0.55)
        cr.rectangle(x - bw / 2, y, bw, 5)
        cr.fill()
        cr.set_source_rgba(*((0.3, 0.8, 1.0, 0.9) if ch["pc"] else (1.0, 0.35, 0.3, 0.9)))
        cr.rectangle(x - bw / 2, y, bw * fill, 5)
        cr.fill()
        if fill < 0.999:
            cr.move_to(x + bw / 2 + 4, y + 6)
            cr.show_text(f"{fill * 100:.0f}%")
    draw_enemies(world, cam, cr, w, h, me)
    draw_dungeons(world, cam, cr, w, h, me)
    draw_route(world, cam, cr, w, h, me)
    for e in world.extra:
        if not me:
            break
        loc = (e["x"], e["y"], e["z"] + 100.0)
        if sum((loc[i] - me[i]) ** 2 for i in range(3)) > (world.args.range * 100) ** 2:
            continue
        pr = cam.project(loc, w, h)
        if not pr or not (-50 < pr[0] < w + 50 and -50 < pr[1] < h + 50):
            continue
        x, y, depth = pr
        label, colour = EXTRA_STYLE.get(e["group"], ("? " + e["group"].replace("E_D1_", ""), UNKNOWN))
        cr.set_source_rgba(*colour)
        cr.set_line_width(1.5)
        cr.move_to(x, y - 8); cr.line_to(x + 8, y); cr.line_to(x, y + 8); cr.line_to(x - 8, y)
        cr.close_path(); cr.stroke()
        cr.move_to(x + 11, y + 4)
        cr.show_text(f"{label}  {depth / 100:.0f} m")
    for t in targets:
        if t["gate"] and live.u32(t["addr"] + FEATHER_STATE) != 1:
            continue
        base = read_vector(live, t["root"] + SCENE_RELATIVE_LOCATION)
        if not _finite(base):
            continue
        top = (base[0], base[1], base[2] + t["lift"])
        pt, pg = cam.project(top, w, h), cam.project(base, w, h)
        if not pt:
            continue
        x, y, depth = pt
        if not (-50 < x < w + 50 and -50 < y < h + 50):
            continue
        half = max(8, min(60, 3000 / max(depth, 1)))           # nearer = bigger box
        cr.set_source_rgba(*t["colour"])
        cr.set_line_width(2)
        cr.rectangle(x - half, y - half, half * 2, half * 2)
        cr.stroke()
        if pg and t["lift"]:                                   # leader line to the ground anchor
            cr.set_line_width(1)
            cr.move_to(x, y + half)
            cr.line_to(pg[0], pg[1])
            cr.stroke()
        cr.move_to(x - half, y - half - 6)
        cr.show_text(f"{t['label']}  {depth / 100:.0f} m")


def edge_arrow(cam, cr, w: int, h: int, pt, text: str) -> None:
    """A filled arrow on the screen edge pointing the way to turn toward a world point that is off
    screen or behind the camera. Caller sets the colour."""
    d = [pt[i] - cam.loc[i] for i in range(3)]
    rx = sum(a * b for a, b in zip(d, cam.right))
    uz = sum(a * b for a, b in zip(d, cam.up))
    if sum(a * b for a, b in zip(d, cam.fwd)) <= 0 and abs(rx) < 1:
        rx = 1.0                                     # dead behind: point right
    ang = math.atan2(-uz, rx)
    k = min((w / 2 - 40) / max(abs(math.cos(ang)), 1e-6),
            (h / 2 - 40) / max(abs(math.sin(ang)), 1e-6))
    ax, ay = w / 2 + math.cos(ang) * k, h / 2 + math.sin(ang) * k
    cr.save(); cr.translate(ax, ay); cr.rotate(ang)
    cr.move_to(14, 0); cr.line_to(-8, -9); cr.line_to(-8, 9); cr.close_path(); cr.fill()
    cr.restore()
    cr.move_to(ax - 20, ay + (22 if ay < h / 2 else -16))
    cr.show_text(text)


def draw_dungeons(world: World, cam, cr, w: int, h: int, me):
    """Dungeon / portal entrances from the paks: a triangle and a label, within --range."""
    if not me:
        return
    with world.lock:
        done = set(world.done)
    for e in world.dungeons:
        loc = (e["x"], e["y"], e["z"] + 150.0)
        if sum((loc[i] - me[i]) ** 2 for i in range(3)) > (world.args.range * 100) ** 2:
            continue
        pr = cam.project(loc, w, h)
        if not pr or not (-50 < pr[0] < w + 50 and -50 < pr[1] < h + 50):
            continue
        x, y, depth = pr
        r, g, b, a = DUNGEON_STYLE.get(e["kind"], UNKNOWN)
        if f"D:{e['name']}" in done:
            a *= 0.4                                   # visited: dimmed, not hidden
        cr.set_source_rgba(r, g, b, a)
        cr.set_line_width(2)
        cr.move_to(x, y - 10); cr.line_to(x + 9, y + 6); cr.line_to(x - 9, y + 6); cr.close_path()
        cr.stroke()
        tag = "? " if e["kind"] == "portal" else ""
        cr.move_to(x + 12, y + 5)
        cr.show_text(f"{tag}{e['label']}  {depth / 100:.0f} m")


def draw_route(world: World, cam, cr, w: int, h: int, me):
    """F9: the next 6 stops of the completionist loop."""
    if not (world.route_on and world.route and me):
        return
    with world.lock:
        done = set(world.done)
        stops = world.route.upcoming(done, 6)
        left = world.route.remaining(done)
    cr.set_font_size(18)
    cr.set_source_rgba(*ROUTE)
    if not stops:
        txt = "route complete ✓"
        cr.move_to(w / 2 - cr.text_extents(txt).width / 2, 92)
        cr.show_text(txt)
        cr.set_font_size(14)
        return
    nxt = stops[0]
    dist = math.dist((nxt["x"], nxt["y"], nxt["z"]), me) / 100
    txt = f"route · {left} left · next: {nxt['label']} {dist:.0f} m"
    cr.move_to(w / 2 - cr.text_extents(txt).width / 2, 92)
    cr.show_text(txt)
    cr.set_font_size(14)
    pts = [cam.project((s["x"], s["y"], s["z"] + 130.0), w, h) for s in stops]
    cr.set_line_width(2)
    prev = cam.project(me, w, h)
    for p in pts:                                     # the line: me -> 1 -> 2 -> ...
        if prev and p:
            cr.move_to(prev[0], prev[1]); cr.line_to(p[0], p[1]); cr.stroke()
        prev = p
    for i, (s, p) in enumerate(zip(stops, pts)):
        if not p or not (0 < p[0] < w and 0 < p[1] < h):
            if i == 0:
                edge_arrow(cam, cr, w, h, (s["x"], s["y"], s["z"]), f"{dist:.0f} m")
            continue
        rad = 14 if i == 0 else 9
        cr.set_source_rgba(0, 0, 0, 0.55)
        cr.arc(p[0], p[1], rad, 0, 6.2832); cr.fill()
        cr.set_source_rgba(*ROUTE)
        cr.arc(p[0], p[1], rad, 0, 6.2832); cr.stroke()
        n = str(i + 1)
        cr.move_to(p[0] - cr.text_extents(n).width / 2, p[1] + 5)
        cr.show_text(n)


def draw_enemies(world: World, cam, cr, w: int, h: int, me):
    """--pvp: enemy-faction players. On screen = a red bracket over the head with distance (and
    HP when their nameplate is up); off screen or behind = a red arrow on the screen edge pointing
    the way to turn. A banner counts them and names the nearest."""
    live = world.live
    with world.lock:
        enemies = list(world.enemies)
    if not enemies or not me:
        return
    nearest = None
    for en in enemies:
        loc = read_vector(live, en["root"] + SCENE_RELATIVE_LOCATION)
        if not _finite(loc):
            continue
        dist = math.sqrt(sum((loc[i] - me[i]) ** 2 for i in range(3))) / 100
        nearest = dist if nearest is None else min(nearest, dist)
        head = (loc[0], loc[1], loc[2] + 110.0)
        pr = cam.project(head, w, h)
        fill = None
        if en["bar"]:
            f = struct.unpack("<f", struct.pack("<I", live.u32(en["bar"] + AIONPBAR_FILL) or 0))[0]
            fill = f if 0.0 < f <= 1.0 else None
        cr.set_source_rgba(*ENEMY)
        if pr and 0 < pr[0] < w and 0 < pr[1] < h:
            x, y, depth = pr
            s = max(10, min(40, 4000 / max(depth, 1)))
            cr.set_line_width(2.5)
            for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):        # corner brackets
                cr.move_to(x + sx * s, y + sy * s * 0.5); cr.line_to(x + sx * s, y + sy * s)
                cr.line_to(x + sx * s * 0.5, y + sy * s)
            cr.stroke()
            cr.move_to(x + s + 6, y + 5)
            hp = f"  {fill * 100:.0f}%" if fill is not None else ""
            cr.show_text(f"{RACE_NAME[en['race']]}  {dist:.0f} m{hp}")
        else:
            edge_arrow(cam, cr, w, h, head, f"{dist:.0f} m")
    if nearest is not None:
        cr.set_font_size(20)
        cr.set_source_rgba(*ENEMY)
        txt = f"⚔ {len(enemies)} {'enemy' if len(enemies) == 1 else 'enemies'} · nearest {nearest:.0f} m"
        cr.move_to(w / 2 - cr.text_extents(txt).width / 2, 60)
        cr.show_text(txt)
        cr.set_font_size(14)


def run_layer_shell(world: World, w: int, h: int) -> int:
    """A wlr-layer-shell surface on the OVERLAY layer: KWin stacks it above fullscreen and
    borderless windows, which an ordinary keep-above XWayland window is not (the operator, 10-01: the
    first X11 build was hidden whenever AION 2 had focus). Empty input region = click-through."""
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import GLib, Gtk, Gtk4LayerShell as LS

    # NON_UNIQUE: a second launch must not silently hand off to a running instance and exit 0 --
    # that made two "clean" test runs (10-01) prove nothing.
    from gi.repository import Gio
    app = Gtk.Application(application_id="dev.aesteria.overlay",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)

    def activate(app):
        win = Gtk.Window(application=app)
        LS.init_for_window(win)
        LS.set_layer(win, LS.Layer.OVERLAY)
        LS.set_namespace(win, "aesteria-overlay")
        LS.set_keyboard_mode(win, LS.KeyboardMode.NONE)
        LS.set_exclusive_zone(win, -1)
        for edge in (LS.Edge.LEFT, LS.Edge.RIGHT, LS.Edge.TOP, LS.Edge.BOTTOM):
            LS.set_anchor(win, edge, True)
        css = Gtk.CssProvider()
        css.load_from_string("window { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(win.get_display(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        area = Gtk.DrawingArea()
        area.set_draw_func(lambda _a, cr, aw, ah: draw(world, cr, aw, ah))
        win.set_child(area)
        win.present()
        win.get_surface().set_input_region(cairo.Region())        # clicks fall through
        GLib.timeout_add(33, lambda: (area.queue_draw(), True)[1])

    app.connect("activate", activate)
    from gi.repository import GLibUnix
    GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda: (app.quit(), False)[1])
    print("overlay on the wlr-layer-shell OVERLAY layer — Ctrl-C to quit")
    return app.run([])


def main() -> int:
    # gtk4-layer-shell must be linked before libwayland-client, so it has to be preloaded.
    lib = "/usr/lib/libgtk4-layer-shell.so"
    if lib not in os.environ.get("LD_PRELOAD", "") and Path(lib).exists():
        os.environ["LD_PRELOAD"] = f"{lib} {os.environ.get('LD_PRELOAD', '')}".strip()
        os.execv(sys.executable, [sys.executable] + sys.argv)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pid", type=int, nargs="?", const=0, default=None)
    ap.add_argument("--screen", default="2560x1440", help="game resolution WxH (borderless)")
    ap.add_argument("--range", type=float, default=400.0, help="metres")
    ap.add_argument("--all", action="store_true", help="also draw every other interactable")
    ap.add_argument("--no-boost", action="store_true", help="disable the F6 wing boost hotkey")
    ap.add_argument("--pvp", action="store_true",
                    help="mark enemy-faction players: bracket + distance on screen, edge arrow off it")
    ap.add_argument("--pvp-test", action="store_true",
                    help="positive control: mark your OWN faction as enemies, to check the drawing")
    ap.add_argument("--cleanse", action="store_true",
                    help="press the Healing Potion (F2) when a status effect is on you")
    ap.add_argument("--cleanse-dry", action="store_true",
                    help="like --cleanse but only log what it would press")
    ap.add_argument("--route", action="store_true",
                    help="start with the completionist route on (F9 toggles it)")
    ap.add_argument("--hp", action="store_true",
                    help="HP bars over every mob (red) and player (blue) with a nameplate")
    args = ap.parse_args()
    w, h = (int(v) for v in args.screen.lower().split("x"))

    live = LiveProcess(args.pid or None)
    live.__enter__()
    g, meta = load_live_meta(live, META_CACHE if META_CACHE.exists() else None, False)
    go = GObjects(live, meta, g)
    world = World(live, go, g, args)
    threading.Thread(target=world.discover_forever, daemon=True).start()
    import evdev
    from aesteria_wingboost import WingBoost, listen_hotkeys

    def flip_route():
        print(f"route {'ON' if world.toggle_route() else 'off'}")
    def skip():
        print(f"route: marked done -> {world.skip_next()}")
    keys = {evdev.ecodes.KEY_F9: flip_route, evdev.ecodes.KEY_F10: skip}
    if args.cleanse or args.cleanse_dry:
        from aesteria_cleanse import Cleanser
        world.cleanser = Cleanser(live, lambda: world.debuff_icons, dry_run=args.cleanse_dry)
        threading.Thread(target=world.cleanser.run, daemon=True).start()

        def flip_cleanse():
            world.cleanser.on = not world.cleanser.on
            print(f"cleanse {'ON' if world.cleanser.on else 'off'}")
        keys[evdev.ecodes.KEY_SCROLLLOCK] = flip_cleanse
    if args.route:
        def _arm():                                    # needs a position first
            while world.me is None:
                time.sleep(0.5)
            flip_route()
        threading.Thread(target=_arm, daemon=True).start()
    if not args.no_boost:
        # F6 toggles the wing boost (aesteria_wingboost.py: measured 4 m / 0.75 s budget).
        world.boost = WingBoost(live, lambda: world.root, lambda: world.move_comp)

        def flip():
            on = world.boost.toggle()
            print(f"wing boost {'ON' if on else 'off'}")
        keys[evdev.ecodes.KEY_F6] = flip
    threading.Thread(target=listen_hotkeys, args=(keys,), daemon=True).start()
    return run_layer_shell(world, w, h)


if __name__ == "__main__":
    sys.exit(main())
