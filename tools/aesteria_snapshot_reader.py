#!/usr/bin/env python3
"""Reconstruct a captured address space so it can be read like a live process.

A capture from aesteria_snapshot.py is thousands of raw blobs plus a manifest.
This turns that back into something with a single sane interface:

    snap = Snapshot("captures/01-tutorial-in-world")
    snap.u64(0x140000000)          # read a pointer
    snap.cstring(addr)             # read a NUL-terminated string
    for addr in snap.find(b"None"): ...

Everything downstream (GNames, GUObjectArray, the SDK dump) is written against
this, which is what lets the heuristics be developed and fixed long after the
game has shut down.
"""

from __future__ import annotations

import bisect
import json
import mmap
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass
class Seg:
    start: int
    end: int
    path: Path
    perms: str
    backing: str

    @property
    def size(self) -> int:
        return self.end - self.start


class Snapshot:
    """Random-access reader over a captured address space.

    Segments are mmapped lazily and cached. A UE5 capture has ~30k of them, so we
    keep an LRU rather than mapping everything up front.
    """

    def __init__(self, path: str | Path, max_maps: int = 256):
        self.root = Path(path)
        manifest_path = self.root / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"no manifest.json in {self.root}")
        self.manifest = json.loads(manifest_path.read_text())
        self.pid: int = self.manifest["pid"]
        self.label: str = self.manifest.get("label", "")

        self.segs: list[Seg] = []
        for r in self.manifest["regions"]:
            if not r.get("dumped") or not r.get("blob"):
                continue
            blob = self.root / r["blob"]
            # bytes_read can be short of the region size when pages were unmapped
            # mid-capture; trust the file, not the region header.
            n = r.get("bytes_read") or 0
            if n <= 0:
                continue
            self.segs.append(
                Seg(start=r["start"], end=r["start"] + n, path=blob,
                    perms=r["perms"], backing=r.get("path") or "")
            )
        self.segs.sort(key=lambda s: s.start)
        self._starts = [s.start for s in self.segs]
        self._maps: dict[int, mmap.mmap] = {}
        self._files: dict[int, object] = {}
        self._lru: list[int] = []
        self._max_maps = max_maps
        # Segments currently being scanned by an active find() generator. Nested
        # searches (find_pointers_to inside a find loop) would otherwise evict the
        # outer scan's mapping out from under it -> "mmap closed or invalid".
        self._pinned: dict[int, int] = {}

    # ---------------------------------------------------------------- internals

    def _map(self, idx: int) -> mmap.mmap:
        m = self._maps.get(idx)
        if m is not None:
            try:
                self._lru.remove(idx)
            except ValueError:
                pass
            self._lru.append(idx)
            return m
        evictable = [i for i in self._lru if i not in self._pinned]
        while len(self._lru) >= self._max_maps and evictable:
            old = evictable.pop(0)
            self._lru.remove(old)
            self._maps.pop(old).close()
            self._files.pop(old).close()  # type: ignore[union-attr]
        fh = open(self.segs[idx].path, "rb")
        m = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        self._files[idx] = fh
        self._maps[idx] = m
        self._lru.append(idx)
        return m

    def _seg_index(self, addr: int) -> int | None:
        i = bisect.bisect_right(self._starts, addr) - 1
        if i < 0:
            return None
        s = self.segs[i]
        return i if s.start <= addr < s.end else None

    # -------------------------------------------------------------------- reads

    def read(self, addr: int, size: int) -> bytes | None:
        """Read size bytes at addr, or None if not fully within one segment.

        Deliberately does NOT stitch across segments: in a captured address space
        adjacent segments are not guaranteed contiguous, and silently splicing
        them would manufacture data that never existed. A caller that needs to
        cross a boundary should ask twice.
        """
        i = self._seg_index(addr)
        if i is None:
            return None
        s = self.segs[i]
        if addr + size > s.end:
            return None
        m = self._map(i)
        off = addr - s.start
        return m[off:off + size]

    def u8(self, a: int) -> int | None:
        b = self.read(a, 1)
        return b[0] if b else None

    def u16(self, a: int) -> int | None:
        b = self.read(a, 2)
        return struct.unpack("<H", b)[0] if b else None

    def u32(self, a: int) -> int | None:
        b = self.read(a, 4)
        return struct.unpack("<I", b)[0] if b else None

    def i32(self, a: int) -> int | None:
        b = self.read(a, 4)
        return struct.unpack("<i", b)[0] if b else None

    def u64(self, a: int) -> int | None:
        b = self.read(a, 8)
        return struct.unpack("<Q", b)[0] if b else None

    def f32(self, a: int) -> float | None:
        b = self.read(a, 4)
        return struct.unpack("<f", b)[0] if b else None

    def cstring(self, a: int, limit: int = 512) -> str | None:
        """Read a NUL-terminated ASCII string, reading within one segment only."""
        i = self._seg_index(a)
        if i is None:
            return None
        s = self.segs[i]
        m = self._map(i)
        off = a - s.start
        end = min(off + limit, s.end - s.start)
        chunk = m[off:end]
        nul = chunk.find(b"\0")
        if nul >= 0:
            chunk = chunk[:nul]
        try:
            return chunk.decode("ascii")
        except UnicodeDecodeError:
            return chunk.decode("latin-1")

    def is_mapped(self, addr: int) -> bool:
        return self._seg_index(addr) is not None

    def perms_at(self, addr: int) -> str | None:
        i = self._seg_index(addr)
        return self.segs[i].perms if i is not None else None

    def looks_like_pointer(self, val: int) -> bool:
        """Cheap plausibility filter before paying for a segment lookup.

        Win64 user space under Wine sits well under 0x8000_00000000, and every
        real structure pointer is 8-byte aligned (UE allocates 16-aligned).
        """
        return 0x10000 < val < 0x7FFF_FFFFFFFF and (val & 0x7) == 0

    # ------------------------------------------------------------------ scanning

    def find(self, pattern: bytes, limit: int = 0,
             perms: str | None = None) -> Iterator[int]:
        """Yield every address whose bytes match pattern.

        perms filters on the captured region's protection, e.g. "rw" to look only
        in writable data (where UE globals live) and skip 300 MB of code.
        """
        found = 0
        for i, s in enumerate(self.segs):
            if perms and not all(c in s.perms for c in perms):
                continue
            m = self._map(i)
            # Pin while we hold a reference: a nested search must not evict us.
            self._pinned[i] = self._pinned.get(i, 0) + 1
            try:
                pos = 0
                # Search the mmap in place. Slicing it into a bytes object first
                # would copy the whole 6.5 GB writable set on every single scan.
                while True:
                    j = m.find(pattern, pos)
                    if j < 0:
                        break
                    yield s.start + j
                    found += 1
                    if limit and found >= limit:
                        return
                    pos = j + 1
            finally:
                n = self._pinned.get(i, 1) - 1
                if n > 0:
                    self._pinned[i] = n
                else:
                    self._pinned.pop(i, None)

    def find_pointers_to(self, target: int, limit: int = 0,
                         perms: str | None = None) -> Iterator[int]:
        """Yield addresses holding a pointer whose value is exactly target.

        This is the workhorse for locating a global: find the data a global points
        at, then find who points at it.
        """
        needle = struct.pack("<Q", target)
        yield from self.find(needle, limit=limit, perms=perms)

    def stats(self) -> str:
        total = sum(s.size for s in self.segs)
        ex = [s for s in self.segs if "x" in s.perms]
        rw = [s for s in self.segs if "w" in s.perms]
        return (f"{self.root.name}: pid {self.pid}, {len(self.segs)} segments, "
                f"{total/1e9:.2f} GB\n"
                f"  executable: {len(ex)} segs, {sum(s.size for s in ex)/1e6:.0f} MB\n"
                f"  writable:   {len(rw)} segs, {sum(s.size for s in rw)/1e9:.2f} GB\n"
                f"  label: {self.label}")

    def close(self) -> None:
        for idx in list(self._maps):
            self._maps.pop(idx).close()
            self._files.pop(idx).close()  # type: ignore[union-attr]
        self._lru.clear()

    def __enter__(self) -> "Snapshot":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        sys.exit("usage: aesteria_snapshot_reader.py <capture-dir>")
    with Snapshot(sys.argv[1]) as snap:
        print(snap.stats())
