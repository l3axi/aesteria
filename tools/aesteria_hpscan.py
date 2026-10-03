#!/usr/bin/env python3
"""aesteria_hpscan — Cheat Engine's scan loop, over /proc/<pid>/mem, with no debugger.

WHY NOT CHEAT ENGINE
    the operator's diagnosis of why HP stayed unsolved is right: every attempt so far compared a
    number he read off the screen against memory captured seconds later, and in an MMO with
    regen, latency and chat round-trips those are never the same instant. The fix is to take
    the human out of the comparison, not to read the number faster.

    But CE is the wrong instrument HERE specifically. CE attaches as a WINDOWS DEBUGGER, and
    a debugger check is the single most universal thing a user-mode anti-cheat does --
    PEB.BeingDebugged, NtQueryInformationProcess(ProcessDebugPort), tool window names. Every
    mechanism AESTERIA has proven live works because it sits BELOW the wine boundary where
    NCGuard cannot look; CE sits above it, in exactly the place NCGuard does look. There is no
    throwaway account for AION 2.

    None of that is needed, because CE's actual method does not require a debugger. The
    method is: snapshot memory, let the value change, keep only the slots that changed the
    right way, repeat. That is a diff over two reads — and reading /proc/<pid>/mem is
    something we have been doing all night without attaching, stopping or being seen.

WHY THIS BEATS KNOWING THE NUMBER
    An "unknown initial value" scan never needs the HP value at all — only its DIRECTION.
    That matters more than convenience here: if AION 2 obfuscates vitals the way some other titles do
    (float bits XORed with a rotating key), the plain value is NOT in memory and no value
    scan can ever find it — but the ENCODED slot still changes when HP changes, so a
    decreased/increased scan still catches it. This is the one method that works whether or
    not the value is encoded.

    Caveat, stated up front: if the key is re-rolled on every write, the encoded bytes move
    unpredictably and `dec`/`inc` lose their meaning. `diff` (changed at all) still works,
    and `same` while HP is genuinely static is then the discriminating filter.

HOW TO USE IT — you drive, the game is the instrument
    python3 tools/aesteria_hpscan.py

        > new            take the baseline. Do this at a moment HP is STABLE.
        (go take a hit)
        > dec            keep slots that went DOWN
        (regen back up, or take another hit)
        > inc / dec      narrow again
        (stand still, HP not moving)
        > same           keep slots that did NOT change — kills counters and timers
        > l              list survivors
        > w <addr>       watch one slot live, to confirm it tracks your bar

    Four or five rounds is normally enough. Alternate dec/inc/same: each is a different
    question, and `same` during stillness is the one that removes clocks and frame counters.

SAFETY
    Read-only. No ptrace attach, no writes, no injection. Pages are read with pread(2) on
    /proc/<pid>/mem, which needs no debugger and never stops the process.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aesteria_netcap import game_pids                      # noqa: E402

# Skip anything bigger than this: UE reserves enormous arenas that are mostly untouched, and
# faulting them in would cost more than it could ever find.
MAX_REGION = 1 << 30
# Value widths carried side by side. The SAME bytes are viewed three ways, so tracking all
# three costs one read, not three -- and each round of filtering costs the operator a game action,
# which is the actually scarce resource.
DTYPES = {"u32": "<u4", "f32": "<f4", "u16": "<u2"}


def regions_for(pid: int, include_file_backed: bool) -> list[tuple[int, int]]:
    """Writable regions worth scanning, resident pages only.

    Rss == 0 means nothing has been touched there; reading it would fault in pages the game
    never asked for, which is both slow and a change to the process's own memory behaviour.
    """
    out: list[tuple[int, int]] = []
    cur: tuple[int, int] | None = None      # the region whose Rss we are waiting for

    for line in open(f"/proc/{pid}/smaps", "r", errors="replace"):
        parts = line.split()
        if not parts:
            continue
        # A map header starts a region: "addr-addr perms offset dev inode [path]".
        # Every other line in smaps is "Key: value kB", so the '-' in field 0 is the tell.
        if "-" in parts[0] and len(parts) >= 5 and parts[1][:1] in "r-":
            cur = None
            lo, hi = parts[0].split("-")
            s, e = int(lo, 16), int(hi, 16)
            perms = parts[1]
            # ⚠ A MISSING pathname means anonymous, but a BRACKETED one -- [heap], [stack] --
            # is a kernel pseudo-path, not a file. Counting fields alone called those
            # file-backed and skipped them, which is what made the positive control fail: numpy
            # puts a small array on the glibc heap, so the planted HP sat in the one writable
            # region the scanner refused to look at. Measured on the live client 2026-10-01:
            # 35.7 MB of resident writable [heap] was being skipped every scan. Small next to
            # 6.6 GB of anonymous, and UE allocates via VirtualAlloc so HP is unlikely to live
            # there -- but it costs nothing to read, and a blind spot nobody chose is the kind
            # that ends up explaining a negative result months later.
            path = parts[5] if len(parts) > 5 else ""
            anonymous = not path or path.startswith("[")
            if "w" not in perms:
                continue
            if not anonymous and not include_file_backed:
                continue
            if e - s > MAX_REGION:
                continue
            cur = (s, e)
        elif cur is not None and parts[0] == "Rss:":
            if int(parts[1]) > 0:               # untouched region: nothing to find in it
                out.append(cur)
            cur = None
    return out


class Scan:
    def __init__(self, pid: int, include_file_backed: bool = False):
        self.pid = pid
        self.fd = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
        self.regions = regions_for(pid, include_file_backed)
        self.base: dict[int, bytes] = {}          # region index -> bytes, until first filter
        # dtype -> {region index: (offsets int64, last values ndarray)}
        self.cand: dict[str, dict[int, tuple[np.ndarray, np.ndarray]]] = {}
        self.rounds = 0

    def read_region(self, i: int) -> bytes | None:
        s, e = self.regions[i]
        try:
            return os.pread(self.fd, e - s, s)
        except OSError:
            return None                            # unmapped mid-scan; drop it

    def baseline(self) -> None:
        self.base.clear()
        self.cand.clear()
        self.rounds = 0
        total = 0
        for i in range(len(self.regions)):
            b = self.read_region(i)
            if b:
                self.base[i] = b
                total += len(b)
        print(f"baseline: {len(self.base)} regions, {total/1e9:.2f} GB "
              f"({total//4/1e6:.0f}M u32 slots)")

    def count(self, name: str) -> int:
        return sum(len(o) for o, _ in self.cand.get(name, {}).values())

    def filter(self, op: str, arg: float | None = None) -> None:
        if not self.base and not self.cand:
            print("no baseline — run `new` first")
            return
        t0 = time.time()
        first = self.rounds == 0
        for name, dt in DTYPES.items():
            width = np.dtype(dt).itemsize
            newc: dict[int, tuple[np.ndarray, np.ndarray]] = {}
            src = range(len(self.regions)) if first else list(self.cand.get(name, {}).keys())
            for i in src:
                buf = self.read_region(i)
                if not buf:
                    continue
                n = len(buf) // width
                if n == 0:
                    continue
                cur_all = np.frombuffer(buf, dt, count=n)
                if first:
                    if i not in self.base:
                        continue
                    ob = self.base[i]
                    m = min(n, len(ob) // width)
                    old = np.frombuffer(ob, dt, count=m)
                    cur = cur_all[:m]
                    offs = np.arange(m, dtype=np.int64)
                else:
                    offs, old = self.cand[name][i]
                    offs = offs[offs < n]
                    old = old[:len(offs)]
                    cur = cur_all[offs]

                with np.errstate(invalid="ignore"):
                    if op == "dec":    keep = cur < old
                    elif op == "inc":  keep = cur > old
                    elif op == "same": keep = cur == old
                    elif op == "diff": keep = cur != old
                    elif op == "eq":   keep = cur == np.array(arg).astype(dt)
                    else:
                        print(f"unknown op {op}"); return
                if name == "f32":      # a NaN compares false everywhere; drop the noise
                    keep &= np.isfinite(cur)
                if keep.any():
                    newc[i] = (offs[keep].astype(np.int64), cur[keep].copy())
            self.cand[name] = newc
        if first:
            self.base.clear()          # the byte baseline has done its job; free 2-3 GB
        self.rounds += 1
        counts = "  ".join(f"{k}={self.count(k):,}" for k in DTYPES)
        print(f"round {self.rounds} [{op}{'' if arg is None else ' '+str(arg)}]: "
              f"{counts}   ({time.time()-t0:.1f}s)")

    def list(self, limit: int = 40) -> None:
        for name in DTYPES:
            c = self.count(name)
            if not c or c > limit:
                print(f"{name}: {c:,} candidates" + ("" if not c else " (too many to list)"))
                continue
            print(f"{name}: {c} candidates")
            for i, (offs, vals) in self.cand[name].items():
                base = self.regions[i][0]
                w = np.dtype(DTYPES[name]).itemsize
                for o, v in zip(offs, vals):
                    print(f"   {base + int(o)*w:#x}  = {v}")

    # ── naming the survivors ───────────────────────────────────────────────────
    # WHY THIS EXISTS (2026-10-01, the first real HP run). A 30-survivor list of bare
    # addresses cannot be read. the operator's run ended with 30 floats in the 1100-1350 band, which
    # looks exactly like a level-18 health pool -- and every one of them was GEOMETRY. Three
    # resolved to StaticMesh 'COM_DranaGrassG04', 'POE_StairA01' and
    # 'StaticMesh_SimplifiedHLODLayer', all at +0xac, in 0x38-strided pairs whose internal
    # deltas were exactly 16.0 and 28.0: an FBoxSphereBounds, not a vital. They survived
    # dec/inc/same because the mesh pool is RECYCLED as the level streams, so the same bytes
    # legitimately went down, then up, then held still.
    #
    # Static-mesh bounds are therefore the dominant false-positive population for a float
    # scan in this game, and -- crucially -- they are IDENTIFIABLE, by the same gate the
    # registry work turned on: GUObjectArray membership. So resolve each survivor's owning
    # UObject and print it. This is deliberately a LABEL and a soft `drop`, never an automatic
    # filter: HP may well live in a non-reflected native struct with no owning UObject at all,
    # and silently discarding unowned slots would throw away the answer.
    ASSET_CLASSES = ("StaticMesh", "SkeletalMesh", "Texture2D", "Texture",
                     "MaterialInstanceConstant", "MaterialInstanceDynamic", "Material",
                     "AnimSequence", "SoundWave", "ParticleSystem", "NiagaraSystem",
                     "StaticMeshComponent", "InstancedStaticMeshComponent")

    def owners(self):
        """Lazily build a sorted [start, end) index of every registered UObject.

        Costs ~2 min on a 776k-object client, so it is built once, on demand, and cached.
        Returns None when the target has no GUObjectArray (the positive control, for one),
        so `who` degrades to plain addresses instead of failing.
        """
        if getattr(self, "_owners", "unset") != "unset":
            return self._owners
        self._owners = None
        # ⚠ REFUSE FAST ON A NON-GAME TARGET. load_live_meta() with no --meta-from scans the
        # whole address space for two UE roots it will never find here, and on a process that
        # has no GUObjectArray it does not error -- it just never finishes. A `who` that hangs
        # is worse than one that declines, so gate on the exe name before paying for the scan.
        if self.pid not in game_pids():
            print("owner index: not an AION2 process — addresses only")
            return None
        try:
            import bisect  # noqa: F401
            from aesteria_registry import (LiveProcess, GObjects, load_live_meta,
                                           object_size)
            live = LiveProcess(self.pid).__enter__()
            g, meta = load_live_meta(live, None, False)
            go = GObjects(live, meta, g)
            spans = []
            for _i, addr in go.iter_objects():
                sz = object_size(live, go, addr)
                if sz:
                    spans.append((addr, addr + sz))
            spans.sort()
            self._owners = (go, spans, [s for s, _ in spans])
            print(f"owner index: {len(spans):,} objects of {go.num:,} registered")
        except Exception as exc:                      # not a UE target, or roots not found
            print(f"owner index unavailable ({exc.__class__.__name__}: {exc})")
        return self._owners

    def own(self, addr: int) -> tuple[str, str, int] | None:
        """(class, name, offset) of the UObject containing addr, or None."""
        o = self.owners()
        if not o:
            return None
        import bisect
        go, spans, starts = o
        i = bisect.bisect_right(starts, addr) - 1
        if i < 0 or not (spans[i][0] <= addr < spans[i][1]):
            return None
        s = spans[i][0]
        return go.class_name(s), go.object_name(s), addr - s

    def who(self, limit: int = 200, drop_assets: bool = False) -> None:
        """List survivors with their owning object; optionally hide asset-owned slots."""
        for name in DTYPES:
            c = self.count(name)
            if not c or c > limit:
                print(f"{name}: {c:,} candidates" + ("" if not c else " (too many; narrow first)"))
                continue
            w = np.dtype(DTYPES[name]).itemsize
            rows, hidden = [], 0
            for i, (offs, vals) in self.cand[name].items():
                base = self.regions[i][0]
                for o, v in zip(offs, vals):
                    a = base + int(o) * w
                    ow = self.own(a)
                    if ow and drop_assets and ow[0] in self.ASSET_CLASSES:
                        hidden += 1
                        continue
                    rows.append((a, v, ow))
            print(f"{name}: {len(rows)} candidate(s)"
                  + (f", {hidden} asset-owned hidden" if hidden else ""))
            for a, v, ow in sorted(rows):
                tag = (f"{ow[0]} '{ow[1]}' +{ow[2]:#x}" if ow else "— no owning UObject —")
                print(f"   {a:#x}  = {v!s:<24} {tag}")

    def drop_assets(self) -> None:
        """Remove every survivor owned by an asset class. Geometry is not a vital."""
        if not self.owners():
            return
        removed = 0
        for name in DTYPES:
            w = np.dtype(DTYPES[name]).itemsize
            newc = {}
            for i, (offs, vals) in self.cand.get(name, {}).items():
                base = self.regions[i][0]
                keep = []
                for k, o in enumerate(offs):
                    ow = self.own(base + int(o) * w)
                    if ow and ow[0] in self.ASSET_CLASSES:
                        removed += 1
                    else:
                        keep.append(k)
                if keep:
                    idx = np.array(keep, dtype=np.int64)
                    newc[i] = (offs[idx], vals[idx])
            self.cand[name] = newc
        counts = "  ".join(f"{k}={self.count(k):,}" for k in DTYPES)
        print(f"dropped {removed} asset-owned slot(s): {counts}")

    def watch(self, addr: int, name: str = "u32", n: int = 20) -> None:
        dt = DTYPES[name]
        w = np.dtype(dt).itemsize
        print(f"watching {addr:#x} as {name} — Ctrl-C to stop")
        try:
            for _ in range(n):
                b = os.pread(self.fd, w, addr)
                print(f"  {np.frombuffer(b, dt)[0]}")
                time.sleep(1.0)
        except (OSError, KeyboardInterrupt):
            pass


HELP = """commands
  new              baseline (take it while HP is STABLE)
  dec / inc        keep slots that went down / up since the last read
  same / diff      keep slots that did not change / did change
  = <n>            keep slots exactly equal to n
  l                list survivors (bare addresses)
  who              list survivors WITH their owning UObject
  geom             drop every survivor owned by an asset class (StaticMesh & co)
  w <hex> [type]   watch one address live
  q                quit

RHYTHM -- and `same` is the one that goes wrong (learned the hard way 2026-10-01):
  new     at FULL HP, out of combat, fully regenerated, standing still
  hit     -> dec        (read it while still damaged, before regen undoes it)
  hit     -> dec        two damage rounds beat one damage + one regen: regen is
                         CONTINUOUS, so `inc` after it admits every clock in the process
  full    -> same       ONLY once HP has stopped moving. A `same` taken mid-regen
                         eliminates HP itself, which is how the first run lost it.
  who     read the owners before believing any value. Floats in a plausible HP band
          are usually StaticMesh bounds -- grass and stairs, not health.
"""


def main() -> int:
    pids = game_pids()
    if not pids:
        print("no AION2.exe running")
        return 1
    pid = pids[-1]                     # the last one is the real game process
    scan = Scan(pid)
    print(f"attached read-only to pid {pid}: {len(scan.regions)} resident writable regions")
    print(HELP)
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if not line:
            continue
        cmd, *rest = line.split()
        if cmd in ("q", "quit", "exit"):
            return 0
        if cmd == "new":
            scan.baseline()
        elif cmd in ("dec", "inc", "same", "diff"):
            scan.filter(cmd)
        elif cmd == "=":
            scan.filter("eq", float(rest[0]))
        elif cmd == "l":
            scan.list()
        elif cmd == "who":
            scan.who()
        elif cmd == "geom":
            scan.drop_assets()
        elif cmd == "w":
            scan.watch(int(rest[0], 16), rest[1] if len(rest) > 1 else "u32")
        else:
            print(HELP)


if __name__ == "__main__":
    raise SystemExit(main())
