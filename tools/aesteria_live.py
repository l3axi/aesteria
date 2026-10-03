#!/usr/bin/env python3
"""Read the LIVE AION 2 process with the same API as a capture.

LiveProcess duck-types Snapshot, so every tool written against a capture works
unchanged on a running game -- the player walk, the entity sweep, the diff. That
was the point of putting a real interface on the snapshot reader instead of
passing raw bytes around.

STILL READ-ONLY, STILL NO INJECTION. We open /proc/<pid>/mem O_RDONLY and pread.
No ptrace attach, no writes, no stopping the process. NCGuard enumerates the
Windows PEB module list and cannot see this.

USAGE
    aesteria_live.py state   --meta-from captures/01-tutorial-in-world
    aesteria_live.py watch   --meta-from captures/01-... --interval 1
    aesteria_live.py nearby  --meta-from captures/01-... --range 5000

WHY --meta-from IS SOUND HERE (and when it is not)
    GNames and GUObjectArray live at fixed addresses for the life of a process, so
    metadata from a capture of the SAME pid applies exactly. It is revalidated on
    load anyway (by_id(0)=='None' plus the InternalIndex==slot invariant). Across a
    RESTART the exe is unpacked to a fresh base and the metadata is void -- the
    tool will fail its own validation and re-derive rather than read nonsense.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from aesteria_snapshot import find_pid, read_regions, classify, DEFAULT_MAX_REGION  # noqa: E402
from aesteria_gnames import GNames, LAYOUTS, find_gnames               # noqa: E402
from aesteria_gobjects import GObjects, validate_array, find_gobjects  # noqa: E402
import aesteria_player as player                                       # noqa: E402
import aesteria_entities as entities                                   # noqa: E402

CHUNK = 1 << 20


class LiveProcess:
    """Snapshot-compatible read-only view of a running process."""

    def __init__(self, pid: int | None = None, label: str = "live"):
        self.pid = find_pid(pid)
        self.label = label
        self._fd = os.open(f"/proc/{self.pid}/mem", os.O_RDONLY)
        self.refresh_regions()

    def refresh_regions(self) -> None:
        """Re-read the map. The address space changes as the world streams."""
        regions = read_regions(self.pid)
        classify(regions, DEFAULT_MAX_REGION, take_all=False)
        self._ranges = sorted((r.start, r.end, r.perms)
                              for r in regions if r.readable)
        self._starts = [r[0] for r in self._ranges]

    # ------------------------------------------------------------------- reads

    def _range_of(self, addr: int):
        import bisect
        i = bisect.bisect_right(self._starts, addr) - 1
        if i < 0:
            return None
        s, e, p = self._ranges[i]
        return (s, e, p) if s <= addr < e else None

    # os.pread takes a C off_t, so an address at or above 2**63 raises OverflowError rather
    # than the OSError this class already handles. A capture never produces one -- its regions
    # are fixed and bounded -- but a LIVE process does: the address space streams underneath
    # the reader, so a pointer sampled mid-tear can be any 64-bit pattern at all. Live-hit
    # 2026-10-01 walking actors while the world streamed; it crashed the sweep outright, which
    # is the worst possible failure for a tool whose whole job is tolerating a moving target.
    # Read: the SAME lesson as the recurring "a moving value cannot be compared against a stale
    # snapshot" trap, one level down -- here it is the POINTER that went stale mid-read.
    MAX_ADDR = 1 << 63

    def read(self, addr: int, size: int) -> bytes | None:
        if addr < 0 or addr >= self.MAX_ADDR or size <= 0:
            return None
        try:
            data = os.pread(self._fd, size, addr)
        except (OSError, OverflowError, ValueError):
            return None
        return data if len(data) == size else None

    def u8(self, a):
        b = self.read(a, 1)
        return b[0] if b else None

    def u16(self, a):
        b = self.read(a, 2)
        return struct.unpack("<H", b)[0] if b else None

    def u32(self, a):
        b = self.read(a, 4)
        return struct.unpack("<I", b)[0] if b else None

    def i32(self, a):
        b = self.read(a, 4)
        return struct.unpack("<i", b)[0] if b else None

    def u64(self, a):
        b = self.read(a, 8)
        return struct.unpack("<Q", b)[0] if b else None

    def f32(self, a):
        b = self.read(a, 4)
        return struct.unpack("<f", b)[0] if b else None

    def cstring(self, a: int, limit: int = 512) -> str | None:
        raw = self.read(a, limit)
        if raw is None:
            for smaller in (256, 128, 64, 32, 16):
                raw = self.read(a, smaller)
                if raw is not None:
                    break
        if raw is None:
            return None
        nul = raw.find(b"\0")
        if nul >= 0:
            raw = raw[:nul]
        return raw.decode("ascii", "replace")

    def is_mapped(self, addr: int) -> bool:
        return self._range_of(addr) is not None

    def perms_at(self, addr: int):
        r = self._range_of(addr)
        return r[2] if r else None

    def looks_like_pointer(self, val: int) -> bool:
        return 0x10000 < val < 0x7FFF_FFFFFFFF and (val & 0x7) == 0

    # ----------------------------------------------------------------- scanning

    def find(self, pattern: bytes, limit: int = 0, perms: str | None = None):
        found = 0
        for start, end, p in self._ranges:
            if perms and not all(c in p for c in perms):
                continue
            pos = start
            carry = len(pattern) - 1
            while pos < end:
                want = min(CHUNK, end - pos)
                try:
                    data = os.pread(self._fd, want, pos)
                except OSError:
                    pos += want
                    continue
                if not data:
                    break
                j = 0
                while True:
                    j = data.find(pattern, j)
                    if j < 0:
                        break
                    yield pos + j
                    found += 1
                    if limit and found >= limit:
                        return
                    j += 1
                # Overlap so a match straddling a chunk edge is not missed.
                pos += max(1, len(data) - carry)

    def find_pointers_to(self, target: int, limit: int = 0,
                         perms: str | None = None):
        yield from self.find(struct.pack("<Q", target), limit=limit, perms=perms)

    def stats(self) -> str:
        total = sum(e - s for s, e, _ in self._ranges)
        return (f"live pid {self.pid}: {len(self._ranges)} readable ranges, "
                f"{total/1e9:.2f} GB mapped")

    def close(self):
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def load_live_meta(live: LiveProcess, meta_from: Path | None, verbose: bool):
    """Reuse capture metadata if it still validates against the live process."""
    g = None
    if meta_from and (meta_from / "gnames.json").exists():
        meta = json.loads((meta_from / "gnames.json").read_text())
        meta["_layout"] = next(l for l in LAYOUTS if l.name == meta["layout"])
        cand = GNames(live, meta)
        if cand.by_id(0) == "None":
            g = cand
            if verbose:
                print(f"  GNames reused from {meta_from.name}: "
                      f"pool {meta['pool']:#x}  (revalidated)")
        else:
            print("  cached GNames failed live validation -- re-deriving "
                  "(process restarted?)", file=sys.stderr)
    if g is None:
        info = find_gnames(live, verbose=verbose)
        if not info:
            sys.exit("cannot locate GNames in the live process")
        g = GNames(live, info)
        _save_meta(meta_from, "gnames.json", info)

    go_meta = None
    if meta_from and (meta_from / "gobjects.json").exists():
        meta = json.loads((meta_from / "gobjects.json").read_text())
        res = validate_array(live, meta["array"], g)
        if res:
            res["fuobjectarray"] = meta["fuobjectarray"]
            go_meta = res
            if verbose:
                print(f"  GUObjectArray reused: {res['num_elements']:,} objects, "
                      f"invariant {res['match_rate']*100:.1f}% (revalidated)")
        else:
            print("  cached GUObjectArray failed live validation -- re-deriving",
                  file=sys.stderr)
    if go_meta is None:
        go_meta = find_gobjects(live, g, verbose=verbose)
        if not go_meta:
            sys.exit("cannot locate GUObjectArray in the live process")
        _save_meta(meta_from, "gobjects.json", go_meta)
    return g, go_meta


def _save_meta(meta_dir: Path | None, name: str, info: dict) -> None:
    """Write a re-derived root back to the cache. Without this, every launch after a client
    restart re-derived both roots from scratch (~1 min each, 10-01) because the stale cache
    failed validation every time and was never replaced."""
    if not meta_dir:
        return
    out = {k: v for k, v in info.items() if not k.startswith("_")}
    if "_layout" in info and "layout" not in out:
        out["layout"] = info["_layout"].name
    try:
        meta_dir.mkdir(parents=True, exist_ok=True)
        tmp = meta_dir / (name + ".tmp")
        tmp.write_text(json.dumps(out, indent=1))
        tmp.replace(meta_dir / name)
    except (OSError, TypeError) as e:
        print(f"  (could not refresh {name}: {e})", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="read the live AION 2 process")
    ap.add_argument("cmd", choices=["state", "watch", "nearby"])
    ap.add_argument("--pid", type=int)
    ap.add_argument("--meta-from", help="capture dir to reuse GNames/GObjects "
                                        "metadata from (same pid)")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--count", type=int, default=15, help="watch samples")
    ap.add_argument("--range", type=float, default=5000.0)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    meta_from = Path(args.meta_from) if args.meta_from else None
    with LiveProcess(args.pid) as live:
        print(live.stats())
        g, meta = load_live_meta(live, meta_from, args.verbose)
        go = GObjects(live, meta, g)

        if args.cmd == "state":
            found = player.find_local_player(live, go, g, args.verbose)
            if not found:
                print("no local player found")
                return 1
            for p in found:
                print(player.describe(p))
            return 0

        if args.cmd == "watch":
            # Resolve the controller ONCE, then re-read just the position each
            # tick. Re-walking 470k objects per sample would sample slower than
            # the player moves, which is the trap that a live readout exists to
            # avoid in the first place.
            found = player.find_local_player(live, go, g)
            if not found:
                print("no local player found")
                return 1
            p = found[0]
            root = p["root_component"]
            print(f"\nwatching {p['pawn_class']} root {root:#x} "
                  f"every {args.interval}s\n")
            print(f"  {'time':>8}  {'X':>12} {'Y':>12} {'Z':>10}  "
                  f"{'yaw':>8}  {'step':>8}")
            prev = None
            for n in range(args.count):
                loc = player.read_vector(live, root + player.SCENE_RELATIVE_LOCATION)
                rot = player.read_vector(live, root + player.SCENE_RELATIVE_ROTATION)
                if loc is None:
                    print("  (unreadable)")
                else:
                    step = math.dist(prev, loc) if prev else 0.0
                    yaw = rot[1] if rot else float("nan")
                    print(f"  {time.strftime('%H:%M:%S')}  {loc[0]:12.1f} "
                          f"{loc[1]:12.1f} {loc[2]:10.1f}  {yaw:8.1f}  "
                          f"{step:8.1f}")
                    prev = loc
                if n + 1 < args.count:
                    time.sleep(args.interval)
            return 0

        if args.cmd == "nearby":
            found = player.find_local_player(live, go, g)
            origin = found[0]["location"] if found else None
            ents = entities.sweep(live, go, origin, args.range, args.verbose)
            chars = [e for e in ents if e["class"] and
                     ("Ch_" in e["class"] or "Character" in e["class"])]
            print(f"\n{len(ents)} actors within {args.range} units; "
                  f"{len(chars)} character-like:\n")
            for e in chars[:30]:
                x, y, z = e["location"]
                print(f"  {e['distance']/100:7.1f} m  {e['class']:<26} "
                      f"({x:9.1f},{y:10.1f},{z:8.1f})")
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
