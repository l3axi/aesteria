#!/usr/bin/env python3
"""aesteria_hop -- how far will the server let a flying Daeva move in one step? Measure first.

THE IDEA (2026-10-01): a position write that lands inside a displacement the server already expects
(in another game, a jump, clamped at 260 u). AION 2 has WINGS:
flight and glide are big, fast displacements the server already tolerates, so the slack its
movement check allows a flyer may be far larger than a walker's.

NO INJECTION. ptrace_scope = 0 on the host, so /proc/<pid>/mem is WRITABLE from outside exactly as
it is readable. Only `hop --apply` writes; every other mode is read-only.

HOTKEYS (the real way to run it -- nobody can alt-tab mid-flight):
    python3 tools/aesteria_hop.py hotkeys [--apply] [--dist 100]
    F9 c2w check · F10 measure start/stop · F8 hop. Chimes on each press; log in snaps/hop.log.

THE LADDER -- run it in order, and do not skip a rung:
    measure   sample position + velocity at ~30 Hz: what "normal" looks like walking, gliding,
              flying. The server's check is calibrated against these numbers.
    c2w       find where the root component keeps its WORLD transform. Writing RelativeLocation
              alone probably does nothing: movement reads the cached ComponentToWorld, not the
              relative field. Read-only; finds the copy by value, twice, while you move.
    hop       plan a step of --dist units along your facing. Prints the plan. With --apply it
              writes BOTH copies once, then watches for 3 s: did it stick, or did the server snap
              you back (and to where)? That rubber-band is the whole measurement.

GUARDS: --dist is capped at --max (default 300 u = 3 m) until a smaller step has been seen to
stick. One write per invocation, never a loop. Nothing here runs automatically.
LIVE RESULTS 2026-10-01 (the operator, in flight, open air, writes at root+0x148 and c2w root+0x290):
    1 m x4, 3 m x2, 5 m x2  STUCK -- flight continued from the target, no correction in 3 s.
    7.5 m x1 (16 m/s), 10 m x2 (12, 16 m/s)  REJECTED -- 0.6-1.2 s later the server pulls you back
    by the hop distance (once to the exact start), flight stamina goes to ZERO and you fall.
    Bursts of 4 m: every 2 s, 1 s, 0.75 s CLEAN (sustained, 3-4 hops); every 0.5 s CORRECTED.
    6 m single: corrected then re-synced (stutter, no grounding) -- the edge.
    So the server has a movement check whose limit sits between 5 and 7.5 m per step at flight
    speed, and its penalty is a forced landing. Accepted hops drain stamina too (the operator's eye).
    F11 is the game's fullscreen toggle -- hop is on F8.
"""
from __future__ import annotations

import argparse
import math
import os
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aesteria_gobjects import GObjects                              # noqa: E402
from aesteria_live import LiveProcess, load_live_meta               # noqa: E402
from aesteria_player import (find_local_player, read_vector,        # noqa: E402
                             SCENE_RELATIVE_LOCATION, SCENE_COMPONENT_VELOCITY)

META_CACHE = Path(__file__).resolve().parents[1] / "snaps" / "meta"
C2W_SCAN = range(0x150, 0x400, 8)       # where to look for the ComponentToWorld translation


def setup(pid):
    live = LiveProcess(pid)
    live.__enter__()
    g, meta = load_live_meta(live, META_CACHE if META_CACHE.exists() else None, False)
    go = GObjects(live, meta, g)
    me = find_local_player(live, go, g)
    if not me:
        raise SystemExit("not in-world")
    return live, me[0]


def cmd_measure(live, me, secs: float, stop=lambda: False):
    root = me["root_component"]
    print(f"sampling {secs:.0f} s at ~30 Hz — walk, then glide, then fly")
    t0, last, rows = time.time(), None, []
    while time.time() - t0 < secs and not stop():
        p = read_vector(live, root + SCENE_RELATIVE_LOCATION)
        v = read_vector(live, root + SCENE_COMPONENT_VELOCITY)
        t = time.time()
        if p and last:
            dt = t - last[0]
            step = math.dist(p, last[1])
            rows.append((t - t0, step / dt if dt else 0, math.hypot(*v[:2]) if v else 0,
                         v[2] if v else 0, p[2]))
        last = (t, p)
        time.sleep(1 / 30)
    if not rows:
        return
    for i in range(0, len(rows), max(1, len(rows) // 20)):
        t, obs, hv, vz, z = rows[i]
        print(f"  t={t:5.1f}s  observed {obs / 100:6.1f} m/s   velocity-field horiz "
              f"{hv / 100:6.1f} m/s  vz {vz / 100:+6.1f}   z {z:8.0f}")
    obs = sorted(r[1] for r in rows)
    print(f"observed speed m/s: median {obs[len(obs) // 2] / 100:.1f}  p95 "
          f"{obs[int(len(obs) * .95)] / 100:.1f}  max {obs[-1] / 100:.1f}")


def cmd_c2w(live, me):
    """Offsets inside the root component whose 3 doubles equal RelativeLocation -- twice,
    ~1 s apart, so a coincidental match on one read cannot survive (you should be moving)."""
    root = me["root_component"]

    def hits():
        loc = read_vector(live, root + SCENE_RELATIVE_LOCATION)
        out = set()
        for o in C2W_SCAN:
            if o == SCENE_RELATIVE_LOCATION:
                continue
            v = read_vector(live, root + o)
            if v and loc and all(abs(a - b) < 0.5 for a, b in zip(v, loc)):
                out.add(o)
        return out, loc
    a, la = hits()
    time.sleep(1.0)
    b, lb = hits()
    moved = la and lb and math.dist(la, lb) > 10
    both = sorted(a & b)
    print(f"moved between reads: {'yes' if moved else 'NO -- move and re-run, a still read proves nothing'}")
    print("ComponentToWorld translation candidates:", [hex(o) for o in both])
    return both[0] if both and moved else None


def cmd_hop(live, me, dist: float, cap: float, apply: bool, c2w: int | None):
    if dist > cap:
        raise SystemExit(f"--dist {dist} exceeds --max {cap}; raise --max only after a smaller hop stuck")
    root = me["root_component"]
    loc = read_vector(live, root + SCENE_RELATIVE_LOCATION)
    # Direction = where you are actually GOING. The root's stored rotation is not it: live 10-01
    # it read yaw 19 deg while the operator flew at ~98 deg, so two "forward" hops went sideways. Use the
    # live velocity (3D, so a climbing flight hops along the climb); camera yaw when standing still.
    v = read_vector(live, root + SCENE_COMPONENT_VELOCITY) or (0.0, 0.0, 0.0)
    speed = math.sqrt(sum(c * c for c in v))
    if speed > 100:
        u, how = tuple(c / speed for c in v), f"velocity {speed / 100:.1f} m/s"
    else:
        from aesteria_camera import Camera                          # noqa: PLC0415
        cam = Camera.read(live, me["controller"])
        u, how = (cam.fwd[0], cam.fwd[1], 0.0) if cam else (1.0, 0.0, 0.0), "camera yaw"
        n = math.hypot(u[0], u[1]) or 1.0
        u = (u[0] / n, u[1] / n, 0.0)
    tgt = tuple(loc[i] + u[i] * dist for i in range(3))
    heading = math.degrees(math.atan2(u[1], u[0]))
    print(f"from {tuple(round(c) for c in loc)}  heading {heading:.0f}° ({how})  "
          f"-> {tuple(round(c) for c in tgt)}  ({dist / 100:.1f} m)")
    print(f"writes: RelativeLocation @ root+{SCENE_RELATIVE_LOCATION:#x}"
          + (f", ComponentToWorld @ root+{c2w:#x}" if c2w else " only (no --c2w given!)"))
    if not apply:
        print("dry run -- add --apply to write")
        return
    fd = os.open(f"/proc/{live.pid}/mem", os.O_RDWR)
    blob = struct.pack("<3d", *tgt)
    os.pwrite(fd, blob, root + SCENE_RELATIVE_LOCATION)
    if c2w:
        os.pwrite(fd, blob, root + c2w)
    os.close(fd)
    t0 = time.time()
    print("written; watching 3 s:")
    while time.time() - t0 < 3:
        p = read_vector(live, root + SCENE_RELATIVE_LOCATION)
        print(f"  +{time.time() - t0:4.2f}s  {tuple(round(v) for v in p)}  "
              f"from target {math.dist(p, tgt) / 100:6.2f} m   from start {math.dist(p, loc) / 100:6.2f} m")
        time.sleep(0.2)


# ── hotkeys: the operator can't alt-tab to a terminal mid-flight ─────────────────────────────────────
LOG = Path(__file__).resolve().parents[1] / "snaps" / "hop.log"
SOUND = {"ok": "/usr/share/sounds/freedesktop/stereo/complete.oga",
         "start": "/usr/share/sounds/freedesktop/stereo/message.oga",
         "warn": "/usr/share/sounds/freedesktop/stereo/dialog-warning.oga"}


def chirp(kind: str):
    import subprocess
    f = SOUND.get(kind)
    if f and Path(f).exists():
        subprocess.Popen(["paplay", f], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Tee:
    """Everything printed also lands in snaps/hop.log, for later review."""

    def __init__(self, path: Path):
        self.f, self.out = open(path, "a", buffering=1), sys.stdout

    def write(self, s):
        self.out.write(s)
        self.f.write(s)

    def flush(self):
        self.out.flush()
        self.f.flush()


def cmd_hotkeys(live, me, a):
    """Passive evdev read of every keyboard -- keys STILL reach the game (no grab).
        F9   c2w check: two reads 1 s apart; be moving when you press it
        F10  start / stop a speed recording (press, walk/glide/fly, press again)
        F6   boost toggle: --dist every --gap s while flying fast (>10 m/s) and straight (<20 deg)
        F7   burst: --burst hops of --dist, --gap s apart (fixed step, or a budget over time?)
        F8   hop --dist forward along your facing (only if started with --apply)
    A chime confirms each press; results go to the terminal and snaps/hop.log."""
    import select
    import threading
    import evdev
    sys.stdout = Tee(LOG)
    print(f"\n==== hotkeys session {time.strftime('%Y-%m-%d %H:%M:%S')}  dist={a.dist}  "
          f"c2w={hex(a.c2w) if a.c2w else None}  apply={a.apply}")
    kbds = [evdev.InputDevice(p) for p in evdev.list_devices()]
    kbds = [d for d in kbds if evdev.ecodes.KEY_F9 in d.capabilities().get(evdev.ecodes.EV_KEY, [])]
    print("listening on:", ", ".join(d.name for d in kbds), "— F9 c2w · F10 measure · F8 hop · F7 burst · F6 boost")
    state = {"c2w": a.c2w, "measuring": None, "busy": False}

    def run(fn, *args):
        if state["busy"]:
            chirp("warn")
            return
        state["busy"] = True

        def go():
            try:
                fn(*args)
            except Exception as e:                     # never let a torn read end the session
                print(f"!! {type(e).__name__}: {e}")
                chirp("warn")
            finally:
                state["busy"] = False
        threading.Thread(target=go, daemon=True).start()

    def do_c2w():
        print(f"\n[{time.strftime('%H:%M:%S')}] F9 c2w")
        off = cmd_c2w(live, me)
        if off:
            state["c2w"] = off
            print(f"   -> using c2w {hex(off)} for hops")
        chirp("ok" if off else "warn")

    def do_burst():
        print(f"\n[{time.strftime('%H:%M:%S')}] F7 burst {a.burst} x {a.dist / 100:.1f} m, gap {a.gap}s")
        if not state["c2w"]:
            print("   no c2w offset yet -- press F9 while moving first")
            chirp("warn")
            return
        if not a.apply:
            print("   dry run (start with --apply)")
            return
        cmd_burst(live, me, a.dist, a.max, state["c2w"], a.burst, a.gap)
        chirp("ok")

    def toggle_boost():
        if state.get("boost"):
            state["boost"].set()
            state["boost"] = None
            print(f"[{time.strftime('%H:%M:%S')}] F6 boost OFF")
            chirp("ok")
            return
        if not state["c2w"] or not a.apply:
            print("   boost needs a happy F9 first and --apply")
            chirp("warn")
            return
        stop = threading.Event()
        state["boost"] = stop
        print(f"\n[{time.strftime('%H:%M:%S')}] F6 boost ON  {a.dist / 100:.1f} m every {a.gap}s")
        chirp("start")
        threading.Thread(target=boost_loop, args=(live, me, a.dist, state["c2w"], a.gap, stop),
                         daemon=True).start()

    def do_hop():
        print(f"\n[{time.strftime('%H:%M:%S')}] F8 hop")
        if not state["c2w"]:
            print("   no c2w offset yet -- press F9 while moving first")
            chirp("warn")
            return
        cmd_hop(live, me, a.dist, a.max, a.apply, state["c2w"])
        chirp("ok")

    while True:
        r, _, _ = select.select(kbds, [], [], 0.5)
        for d in r:
            for ev in d.read():
                if ev.type != evdev.ecodes.EV_KEY or ev.value != 1:
                    continue
                if ev.code == evdev.ecodes.KEY_F9:
                    chirp("start")
                    run(do_c2w)
                elif ev.code == evdev.ecodes.KEY_F6:
                    toggle_boost()
                elif ev.code == evdev.ecodes.KEY_F7:
                    chirp("start")
                    run(do_burst)
                elif ev.code == evdev.ecodes.KEY_F8:
                    chirp("start")
                    run(do_hop)
                elif ev.code == evdev.ecodes.KEY_F10:
                    if state["measuring"] is None:
                        stop = threading.Event()
                        state["measuring"] = stop
                        print(f"\n[{time.strftime('%H:%M:%S')}] F10 measure START")
                        chirp("start")
                        threading.Thread(target=lambda: (cmd_measure(live, me, 600, stop.is_set),
                                                         chirp("ok")), daemon=True).start()
                    else:
                        state["measuring"].set()
                        state["measuring"] = None
                        print(f"[{time.strftime('%H:%M:%S')}] F10 measure STOP")


def cmd_burst(live, me, dist: float, cap: float, c2w: int, n: int, gap: float):
    """n hops of `dist`, `gap` seconds apart, then watch. Fixed step or a budget over time?"""
    if dist > cap:
        raise SystemExit(f"--dist {dist} exceeds --max {cap}")
    root = me["root_component"]
    start = read_vector(live, root + SCENE_RELATIVE_LOCATION)
    fd = os.open(f"/proc/{live.pid}/mem", os.O_RDWR)
    t0, targets = time.time(), []
    for i in range(n):
        loc = read_vector(live, root + SCENE_RELATIVE_LOCATION)
        v = read_vector(live, root + SCENE_COMPONENT_VELOCITY) or (0.0, 0.0, 0.0)
        sp = math.sqrt(sum(c * c for c in v)) or 1.0
        u = tuple(c / sp for c in v) if sp > 100 else (1.0, 0.0, 0.0)
        tgt = tuple(loc[k] + u[k] * dist for k in range(3))
        blob = struct.pack("<3d", *tgt)
        os.pwrite(fd, blob, root + SCENE_RELATIVE_LOCATION)
        os.pwrite(fd, blob, root + c2w)
        targets.append(tgt)
        print(f"  hop {i + 1}/{n} at +{time.time() - t0:4.2f}s  {dist / 100:.1f} m @ {sp / 100:.1f} m/s")
        if i < n - 1:
            time.sleep(gap)
    os.close(fd)
    last = targets[-1]
    while time.time() - t0 < gap * (n - 1) + 3:
        p = read_vector(live, root + SCENE_RELATIVE_LOCATION)
        print(f"  +{time.time() - t0:4.2f}s  from last target {math.dist(p, last) / 100:6.2f} m   "
              f"from start {math.dist(p, start) / 100:6.2f} m")
        time.sleep(0.2)


def boost_loop(live, me, dist: float, c2w: int, gap: float, stop):
    """Wing boost: one `dist` hop along the velocity every `gap` s while flying fast and straight.
    Measured live 10-01 at 16 m/s: 4 m / 2 s, 4 m / 1 s and 4 m / 0.75 s all clean, sustained over
    3-4 hops; 2 x 4 m / 0.5 s was corrected and grounded. Skips a beat (never doubles up) when
    slow (< 10 m/s) or turning (> 20 deg since the last check), so a turn never fires a hop
    sideways into a wall."""
    root = me["root_component"]
    fd = os.open(f"/proc/{live.pid}/mem", os.O_RDWR)
    last_u, n = None, 0
    try:
        while not stop.is_set():
            v = read_vector(live, root + SCENE_COMPONENT_VELOCITY) or (0.0, 0.0, 0.0)
            sp = math.sqrt(sum(c * c for c in v))
            u = tuple(c / sp for c in v) if sp > 0 else None
            straight = u and last_u and sum(a * b for a, b in zip(u, last_u)) > math.cos(math.radians(20))
            if sp > 1000 and straight:
                loc = read_vector(live, root + SCENE_RELATIVE_LOCATION)
                tgt = tuple(loc[k] + u[k] * dist for k in range(3))
                blob = struct.pack("<3d", *tgt)
                os.pwrite(fd, blob, root + SCENE_RELATIVE_LOCATION)
                os.pwrite(fd, blob, root + c2w)
                n += 1
            last_u = u
            stop.wait(gap)
    finally:
        os.close(fd)
        print(f"   boost off after {n} hops")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=["measure", "c2w", "hop", "hotkeys"])
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--secs", type=float, default=20)
    ap.add_argument("--dist", type=float, default=100, help="units (100 = 1 m)")
    ap.add_argument("--max", type=float, default=300)
    ap.add_argument("--c2w", type=lambda s: int(s, 0), default=None, help="offset from `c2w`")
    ap.add_argument("--apply", action="store_true", help="actually write (hop only)")
    ap.add_argument("--burst", type=int, default=2, help="F7: hops per burst")
    ap.add_argument("--gap", type=float, default=0.5, help="F7: seconds between burst hops")
    a = ap.parse_args()
    live, me = setup(a.pid)
    if a.mode == "hotkeys":
        try:
            cmd_hotkeys(live, me, a)
        except KeyboardInterrupt:
            print("\nhotkeys stopped")
    elif a.mode == "measure":
        cmd_measure(live, me, a.secs)
    elif a.mode == "c2w":
        cmd_c2w(live, me)
    else:
        cmd_hop(live, me, a.dist, a.max, a.apply, a.c2w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
