#!/usr/bin/env python3
"""Locate and decode UE5 FNamePool (GNames) inside a capture.

DERIVATION (why this cannot accidentally succeed)
    UE reserves name index 0 as "None" before any game code runs, and
    FNameEntryAllocator hands out its first entry at the very start of block 0.
    So:
      1. find a 2-byte FNameEntryHeader that decodes to len=4, not-wide,
         immediately followed by the ASCII "None";
      2. from there, walk the entry chain -- each entry's header gives the length
         of its string, which gives the address of the next. Real pool blocks
         chain for thousands of entries; a coincidental "None" in some buffer
         does not survive twenty;
      3. the winning address IS Blocks[0], so whoever points at it is
         FNamePool::Entries::Blocks, and FNamePool = that address - 0x10.

    Every step is checked against the next, which is the rule that keeps a
    reader honest: it either resolves real names or reports failure. It never
    writes a guess.

LAYOUT (UE 5.3.2)
    struct FNameEntryHeader {              // 2 bytes
        uint16 bIsWide : 1;
        uint16 LowercaseProbeHash : 5;     // absent in case-preserving builds
        uint16 Len : 10;
    };
    class FNameEntryAllocator {
        FRWLock Lock;                      // +0x00  (SRWLOCK, 8 bytes)
        uint32  CurrentBlock;              // +0x08
        uint32  CurrentByteCursor;         // +0x0C
        uint8*  Blocks[8192];              // +0x10
    };
    FNamePool begins with its FNameEntryAllocator, so GNames+0x10 == Blocks.

    Case-preserving builds prepend a 4-byte FNameEntryId and widen Len to 15
    bits; both hypotheses are tried and the one that chains further wins rather
    than being assumed.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from aesteria_snapshot_reader import Snapshot  # noqa: E402

BLOCK_OFFSET_BITS = 16
BLOCK_SIZE_BYTES = 2 << BLOCK_OFFSET_BITS          # 131072
MAX_BLOCKS = 8192
BLOCKS_OFFSET_IN_POOL = 0x10
ENTRY_STRIDE_ALIGN = 2


@dataclass
class NameLayout:
    """One hypothesis about FNameEntry's shape."""
    name: str
    str_offset: int          # where the characters start, relative to entry
    header_offset: int       # where the 2-byte header sits

    def decode(self, header: int) -> tuple[bool, int]:
        raise NotImplementedError


class LayoutStandard(NameLayout):
    """WITH_CASE_PRESERVING_NAME=0 -- the normal shipping build."""

    def __init__(self):
        super().__init__("standard(len10)", str_offset=2, header_offset=0)

    def decode(self, header: int) -> tuple[bool, int]:
        return bool(header & 1), header >> 6


class LayoutCasePreserving(NameLayout):
    """WITH_CASE_PRESERVING_NAME=1 -- ComparisonId precedes the header."""

    def __init__(self):
        super().__init__("case-preserving(len15)", str_offset=6, header_offset=4)

    def decode(self, header: int) -> tuple[bool, int]:
        return bool(header & 1), header >> 1


LAYOUTS = [LayoutStandard(), LayoutCasePreserving()]


def chain_length(snap: Snapshot, entry_addr: int, layout: NameLayout,
                 max_entries: int = 4096) -> tuple[int, list[str]]:
    """Walk the entry chain from entry_addr; return (count, first few names).

    Stops at the first implausible entry. This is the discriminator: a stray
    "None" in a text buffer yields a chain of 1-3, a real block yields thousands.
    """
    names: list[str] = []
    addr = entry_addr
    count = 0
    while count < max_entries:
        header = snap.u16(addr + layout.header_offset)
        if header is None:
            break
        is_wide, length = layout.decode(header)
        if length == 0 or length > 1024:
            break
        raw = snap.read(addr + layout.str_offset, length * (2 if is_wide else 1))
        if raw is None:
            break
        try:
            text = raw.decode("utf-16-le" if is_wide else "ascii")
        except UnicodeDecodeError:
            break
        # UE names are identifiers, paths and tags -- printable, no control bytes.
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
            break
        count += 1
        if len(names) < 24:
            names.append(text)
        step = layout.str_offset + length * (2 if is_wide else 1)
        step = (step + ENTRY_STRIDE_ALIGN - 1) & ~(ENTRY_STRIDE_ALIGN - 1)
        addr += step
    return count, names


def find_gnames(snap: Snapshot, verbose: bool = False) -> dict | None:
    """Find FNamePool. Returns a dict describing it, or None if unproven."""
    best: dict | None = None
    considered = 0

    # "None" lives in writable heap memory, which skips 300 MB of code.
    # NOTE: no trailing NUL in the needle. FNameEntry does not NUL-terminate --
    # the length is in the header and the next entry starts immediately after the
    # characters. Searching for b"None\0" finds 0 of the 1 real hit.
    for hit in snap.find(b"None", perms="w"):
        for layout in LAYOUTS:
            entry = hit - layout.str_offset
            header = snap.u16(entry + layout.header_offset)
            if header is None:
                continue
            is_wide, length = layout.decode(header)
            if is_wide or length != 4:
                continue
            considered += 1
            count, names = chain_length(snap, entry, layout)
            if count < 16:
                continue
            if best is None or count > best["chain"]:
                best = {"block0": entry, "chain": count, "layout": layout.name,
                        "layout_obj": layout, "first_names": names}
                if verbose:
                    print(f"  candidate {entry:#014x} layout={layout.name} "
                          f"chain={count} first={names[:6]}")

    if verbose:
        print(f"  considered {considered} header-plausible 'None' entries")
    if best is None:
        return None

    # Confirm by finding who points at block 0. That pointer is Blocks[0], and
    # FNamePool sits 0x10 before it. Without this the pool base is a guess.
    block0 = best["block0"]
    pool_candidates = []
    for ptr_addr in snap.find_pointers_to(block0, perms="w"):
        pool = ptr_addr - BLOCKS_OFFSET_IN_POOL
        cur_block = snap.u32(pool + 0x08)
        cursor = snap.u32(pool + 0x0C)
        if cur_block is None or cursor is None:
            continue
        if cur_block >= MAX_BLOCKS or cursor > BLOCK_SIZE_BYTES:
            continue
        # Every block pointer up to CurrentBlock must be a live allocation.
        blocks = []
        ok = True
        for i in range(cur_block + 1):
            b = snap.u64(ptr_addr + i * 8)
            if b is None or not snap.looks_like_pointer(b):
                ok = False
                break
            blocks.append(b)
        if not ok or not blocks:
            continue
        pool_candidates.append({"pool": pool, "blocks_addr": ptr_addr,
                                "current_block": cur_block,
                                "current_byte_cursor": cursor,
                                "blocks": blocks})
        if verbose:
            print(f"  pool candidate {pool:#014x} CurrentBlock={cur_block} "
                  f"cursor={cursor} blocks_validated={len(blocks)}")

    if not pool_candidates:
        return None
    # More blocks validated == more of the structure agreed with itself.
    pool_candidates.sort(key=lambda c: c["current_block"], reverse=True)
    chosen = pool_candidates[0]
    best.update(chosen)
    best["block_count"] = chosen["current_block"] + 1
    best.pop("layout_obj", None)
    best["_layout"] = next(l for l in LAYOUTS if l.name == best["layout"])
    return best


class GNames:
    """Decode FName indices to text using a located pool."""

    def __init__(self, snap: Snapshot, info: dict):
        self.snap = snap
        self.info = info
        self.layout: NameLayout = info["_layout"]
        self.blocks_addr: int = info["blocks_addr"]
        self.current_block: int = info["current_block"]
        self.current_cursor: int = info["current_byte_cursor"]
        self._block_cache: dict[int, int] = {}
        self._cache: dict[int, str] = {}

    def block(self, i: int) -> int | None:
        if i in self._block_cache:
            return self._block_cache[i]
        if i > self.current_block:
            return None
        b = self.snap.u64(self.blocks_addr + i * 8)
        if b is not None:
            self._block_cache[i] = b
        return b

    def by_id(self, name_id: int) -> str | None:
        """FNameEntryId -> text. Block index in the high bits, offset in the low."""
        if name_id in self._cache:
            return self._cache[name_id]
        block_i = name_id >> BLOCK_OFFSET_BITS
        offset = (name_id & ((1 << BLOCK_OFFSET_BITS) - 1)) * ENTRY_STRIDE_ALIGN
        base = self.block(block_i)
        if base is None:
            return None
        entry = base + offset
        header = self.snap.u16(entry + self.layout.header_offset)
        if header is None:
            return None
        is_wide, length = self.layout.decode(header)
        if length == 0 or length > 1024:
            return None
        raw = self.snap.read(entry + self.layout.str_offset,
                             length * (2 if is_wide else 1))
        if raw is None:
            return None
        try:
            text = raw.decode("utf-16-le" if is_wide else "ascii")
        except UnicodeDecodeError:
            return None
        self._cache[name_id] = text
        return text

    def fname(self, addr: int) -> str | None:
        """Read an FName (ComparisonIndex + Number) at addr and render it."""
        idx = self.snap.u32(addr)
        if idx is None:
            return None
        text = self.by_id(idx)
        if text is None:
            return None
        number = self.snap.u32(addr + 4) or 0
        return text if number == 0 else f"{text}_{number - 1}"

    def iter_all(self):
        """Walk every block sequentially, yielding (id, text) for every entry."""
        for bi in range(self.current_block + 1):
            base = self.block(bi)
            if base is None:
                continue
            limit = self.current_cursor if bi == self.current_block else BLOCK_SIZE_BYTES
            off = 0
            while off < limit:
                entry = base + off
                header = self.snap.u16(entry + self.layout.header_offset)
                if header is None:
                    break
                is_wide, length = self.layout.decode(header)
                if length == 0 or length > 1024:
                    break
                raw = self.snap.read(entry + self.layout.str_offset,
                                     length * (2 if is_wide else 1))
                if raw is None:
                    break
                try:
                    text = raw.decode("utf-16-le" if is_wide else "ascii")
                except UnicodeDecodeError:
                    text = raw.decode("latin-1", "replace")
                yield ((bi << BLOCK_OFFSET_BITS) | (off // ENTRY_STRIDE_ALIGN), text)
                step = self.layout.str_offset + length * (2 if is_wide else 1)
                off += (step + ENTRY_STRIDE_ALIGN - 1) & ~(ENTRY_STRIDE_ALIGN - 1)


def main() -> int:
    ap = argparse.ArgumentParser(description="find and dump UE5 GNames")
    ap.add_argument("capture")
    ap.add_argument("-o", "--out", help="write all names to this file")
    ap.add_argument("--json", help="write pool metadata here")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    with Snapshot(args.capture) as snap:
        print(snap.stats())
        print("\nsearching for FNamePool ...")
        info = find_gnames(snap, verbose=args.verbose)
        if not info:
            print("\nFAILED: no FNamePool candidate survived validation.")
            print("Not writing anything -- a wrong pool would poison every later step.")
            return 1

        print(f"\n{'='*66}")
        print(f"  FNamePool      {info['pool']:#014x}")
        print(f"  Blocks array   {info['blocks_addr']:#014x}")
        print(f"  block 0        {info['block0']:#014x}")
        print(f"  layout         {info['layout']}")
        print(f"  CurrentBlock   {info['current_block']}  "
              f"({info['block_count']} blocks live)")
        print(f"  ByteCursor     {info['current_byte_cursor']}")
        print(f"  chain proven   {info['chain']} entries")
        print(f"{'='*66}")
        print(f"  first names: {', '.join(info['first_names'][:12])}")

        g = GNames(snap, info)
        # Ground truth: index 0 must be "None". If it is not, the pool is wrong
        # no matter how good the chain looked.
        zero = g.by_id(0)
        print(f"\n  GROUND TRUTH  by_id(0) = {zero!r}  "
              f"{'PASS' if zero == 'None' else 'FAIL'}")
        if zero != "None":
            print("  refusing to emit a dump from an unproven pool")
            return 1

        print("\nwalking all blocks ...")
        names = list(g.iter_all())
        print(f"  decoded {len(names)} names")
        interesting = [t for _, t in names if t.startswith("/Script/")]
        print(f"  /Script/ packages: {len(interesting)}  e.g. {interesting[:5]}")

        if args.out:
            with open(args.out, "w") as fh:
                for nid, text in names:
                    fh.write(f"{nid}\t{text}\n")
            print(f"  wrote {args.out}")
        if args.json:
            meta = {k: v for k, v in info.items() if not k.startswith("_")}
            meta["name_count"] = len(names)
            Path(args.json).write_text(json.dumps(meta, indent=1))
            print(f"  wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
