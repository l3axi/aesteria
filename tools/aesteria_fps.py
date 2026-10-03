#!/usr/bin/env python3
"""aesteria_fps -- measure AION 2's frame rate from outside, and cap it live.

WHY: two games on two monitors, one GPU.
An uncapped AION 2 takes the whole card and both games stutter. The client loads no limiter
(no MangoHud, no gamescope), and its own FrameRateLimit only applies through the options menu.

MEASURE (read-only, no injection): UWorld keeps its clocks as plain doubles (TimeSeconds,
RealTimeSeconds, ...). They are not UPROPERTYs, so the SDK does not list them -- they are found
by reading the UWorld twice a second apart and keeping the f64 slots that advanced by the wall
time. One of those, sampled a few thousand times a second, changes once per engine tick: the
number of distinct values per second IS the frame rate.

CAP (one write, `cap N --apply`): UEngine::GetMaxTickRate clamps the tick rate into
SmoothedFrameRateRange when bSmoothFrameRate is set. Both are UPROPERTYs on the GameEngine
object (offsets from the SDK dump, fingerprinted before any write: FixedFrameRate must read
as a sane float right between them). Lower bound = upper bound = N makes it a plain cap rather
than UE's drifting "smoothing". `uncap --apply` puts back exactly what was there, from a saved
copy of the original 24 bytes. t.MaxFPS > 0 would override this; FrameRateLimit=0 leaves it 0.

    aesteria_fps.py measure            fps + the engine's current limiter fields
    aesteria_fps.py cap 90             dry run: what would be written
    aesteria_fps.py cap 90 --apply     write it, then measure
    aesteria_fps.py uncap --apply      restore the saved original bytes
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aesteria_gobjects import GObjects, UOBJ_OUTER                  # noqa: E402
from aesteria_live import LiveProcess, load_live_meta               # noqa: E402
from aesteria_player import find_local_player                       # noqa: E402

META_CACHE = Path(__file__).resolve().parents[1] / "snaps" / "meta"
SAVE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "aesteria" / "fps_orig.bin"

# /Script/Engine.Engine, SDK dump 2026-09-19 (captures/01-tutorial-in-world/sdk/offsets.tsv)
ENGINE_FLAGS = 2764          # uint32 bitfield shared with four unrelated bools (0x01-0x08)
# Masks READ FROM THE LIVE FBoolProperty (FieldMask at FProperty+0x73), 2026-10-03 -- not guessed.
# 0x10 is bForceDisableFrameRateSmoothing: not a UPROPERTY, so the SDK cannot see it, and
# assuming bits 0/1 here would have flipped bCanBlueprintsTickByDefault instead.
BIT_SMOOTH = 0x20
BIT_FIXED = 0x40
BIT_FORCE_OFF = 0x10
KNOWN_BITS = 0x7F
ENGINE_FIXED_RATE = 2768     # float FixedFrameRate
ENGINE_SMOOTH_RANGE = 2772   # FFloatRange: {u8 type, pad3, f32 value} x2 (lower, upper)
BLOCK = 24                   # flags + fixed rate + range
BOUND_NAMES = {0: "exclusive", 1: "inclusive", 2: "open"}
WORLD_SCAN = 0x1400


def setup(pid):
    live = LiveProcess(pid)
    live.__enter__()
    g, meta = load_live_meta(live, META_CACHE if META_CACHE.exists() else None, False)
    go = GObjects(live, meta, g)
    go.refresh()
    return live, go, g


def find_engine(live, go):
    hits = []
    for _, a in go.iter_objects():
        c = go.class_name(a)
        if c and c.endswith("GameEngine") and not (go.object_name(a) or "").startswith("Default__"):
            hits.append((a, c, go.object_name(a)))
    return hits


def find_world(live, go, g):
    me = find_local_player(live, go, g)
    if not me:
        return None
    cur = me[0]["controller"]
    for _ in range(4):                       # controller -> level -> world
        cur = live.u64(cur + UOBJ_OUTER)
        if not cur:
            return None
        if go.class_name(cur) == "World":
            return cur
    return None


def read_block(live, eng):
    b = live.read(eng + ENGINE_FLAGS, BLOCK)
    if not b or len(b) != BLOCK:
        return None
    flags, fixed = struct.unpack_from("<If", b, 0)
    lt, lv, ut, uv = struct.unpack_from("<B3xfB3xf", b, 8)
    return {"raw": b, "flags": flags, "smooth": bool(flags & BIT_SMOOTH), "fixed_on": bool(flags & BIT_FIXED),
            "force_off": bool(flags & BIT_FORCE_OFF),
            "fixed": fixed, "lower": (lt, lv), "upper": (ut, uv)}


def sane(blk):
    """The fingerprint that the SDK offsets still fit this build -- checked before ANY write."""
    if not blk:
        return False, "unreadable"
    if not (1.0 <= blk["fixed"] <= 1000.0):
        return False, f"FixedFrameRate reads {blk['fixed']!r}"
    for name, (t, v) in (("lower", blk["lower"]), ("upper", blk["upper"])):
        if t not in BOUND_NAMES:
            return False, f"{name} bound type {t}"
        if not (0.0 <= v <= 10000.0):
            return False, f"{name} bound value {v!r}"
    if blk["flags"] & ~KNOWN_BITS:
        return False, f"unexpected bits in the flag word: {blk['flags']:#x}"
    return True, "ok"


def describe(blk):
    lt, lv = blk["lower"]
    ut, uv = blk["upper"]
    return (f"flags={blk['flags']:#x} bSmoothFrameRate={blk['smooth']} bUseFixedFrameRate={blk['fixed_on']} "
            f"forceOff={blk['force_off']} "
            f"FixedFrameRate={blk['fixed']:g} range=[{lv:g} {BOUND_NAMES.get(lt, lt)}, "
            f"{uv:g} {BOUND_NAMES.get(ut, ut)}]")


def find_clock(live, world, wait=1.0):
    a = live.read(world, WORLD_SCAN)
    t0 = time.monotonic()
    time.sleep(wait)
    b = live.read(world, WORLD_SCAN)
    dt = time.monotonic() - t0
    if not a or not b:
        return None
    out = []
    for off in range(0, WORLD_SCAN - 8, 8):
        x, = struct.unpack_from("<d", a, off)
        y, = struct.unpack_from("<d", b, off)
        if 0.0 < x < 1e9 and abs((y - x) - dt) < 0.12 * dt:
            out.append((off, y))
    return out


def measure_fps(live, world, off, secs=3.0):
    """Distinct values of a per-tick clock per second. Also returns the worst frame gap seen."""
    fd, addr = live._fd, world + off
    last, n, worst, last_t = None, 0, 0.0, None
    t0 = time.monotonic()
    while True:
        now = time.monotonic()
        if now - t0 >= secs:
            break
        v = os.pread(fd, 8, addr)
        if v != last:
            if last is not None:
                n += 1
                if last_t is not None:
                    worst = max(worst, now - last_t)
                last_t = now
            last = v
    return n / secs, worst * 1000.0


def gpu():
    try:
        base = Path("/sys/class/drm/card1/device")
        return int((base / "gpu_busy_percent").read_text())
    except Exception:
        return -1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["measure", "cap", "uncap"])
    ap.add_argument("fps", nargs="?", type=float)
    ap.add_argument("--pid", type=int)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--secs", type=float, default=3.0)
    a = ap.parse_args()

    live, go, g = setup(a.pid)
    engs = find_engine(live, go)
    if len(engs) != 1:
        print(f"expected one GameEngine object, found {len(engs)}: {engs}")
        return 2
    eng = engs[0][0]
    blk = read_block(live, eng)
    ok, why = sane(blk)
    print(f"engine {engs[0][1]} @ {eng:#x}: " + (describe(blk) if blk else "unreadable"))
    print(f"fingerprint: {why}")
    world = find_world(live, go, g)
    clock = None
    if world:
        cands = find_clock(live, world)
        if cands:
            clock = cands[0][0]
            print(f"world @ {world:#x}: {len(cands)} clock slot(s), using +{clock:#x} "
                  f"(= {cands[0][1]:.1f}s)")
    if clock is None:
        print("no world clock found (not in-world?) -- fps cannot be measured")

    def show(tag):
        if clock is None:
            return None
        fps, worst = measure_fps(live, world, clock, a.secs)
        print(f"{tag}: {fps:.1f} fps over {a.secs:g}s (worst gap {worst:.1f} ms), GPU {gpu()}%")
        return fps

    if a.cmd == "measure":
        show("now")
        return 0

    if not ok:
        print("REFUSING to write: the fingerprint does not fit this build")
        return 3

    if a.cmd == "cap":
        if not a.fps or not (20 <= a.fps <= 500):
            print("cap needs an fps between 20 and 500")
            return 2
        new = struct.pack("<If", (blk["flags"] | BIT_SMOOTH) & ~(BIT_FIXED | BIT_FORCE_OFF), blk["fixed"]) \
            + struct.pack("<B3xfB3xf", 1, a.fps, 1, a.fps)
    else:
        if not SAVE.exists():
            print(f"nothing saved at {SAVE} -- no cap of ours to undo")
            return 2
        new = SAVE.read_bytes()
        if len(new) != BLOCK:
            print("saved block has the wrong size")
            return 2

    print(f"write {BLOCK} bytes @ engine+{ENGINE_FLAGS}: {blk['raw'].hex()} -> {new.hex()}")
    if not a.apply:
        print("dry run -- add --apply to write")
        show("now")
        return 0
    show("before")
    if a.cmd == "cap" and not SAVE.exists():
        SAVE.parent.mkdir(parents=True, exist_ok=True)
        SAVE.write_bytes(blk["raw"])
        print(f"original bytes saved to {SAVE}")
    fd = os.open(f"/proc/{live.pid}/mem", os.O_RDWR)
    try:
        os.pwrite(fd, new, eng + ENGINE_FLAGS)
    finally:
        os.close(fd)
    if a.cmd == "uncap":
        SAVE.unlink()
    time.sleep(1.5)
    print("engine now: " + describe(read_block(live, eng)))
    show("after")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
