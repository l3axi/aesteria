#!/usr/bin/env python3
"""aesteria_snapshot -- read-only memory capture of a live AION2 process.

WHY THIS EXISTS
    The AION 2 playtest server is up for one six-hour window. Every UE5 structure
    we need (FNamePool, GUObjectArray, class layouts) is STATIC once the game is
    in-world -- so the winning move is to copy the address space to disk NOW and
    parse it at leisure afterwards. A parser bug at 04:00 is recoverable; a
    process that no longer exists is not.

WHY THIS IS SAFE
    We never attach, never write, never stop the process. We open /proc/<pid>/mem
    read-only and pread() from it. That is a Linux kernel interface; NCGuard
    (bb64.dll) enumerates the Windows PEB module list and has no way to observe
    it. Requires kernel.yama.ptrace_scope=0 (the host: already 0) or CAP_SYS_PTRACE.

USAGE
    aesteria_snapshot.py maps                 # instant, zero-risk: classify regions
    aesteria_snapshot.py probe                # verify we can actually read
    aesteria_snapshot.py dump -o captures/in-world-0340
    aesteria_snapshot.py dump --all -o ...    # include rss==0 regions too

The manifest it writes beside the blobs is what makes the capture parseable
offline: every region's address, perms, backing file and on-disk blob name.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

# A Wine process reserves enormous PROT_NONE ranges and maps the 82 GB of paks.
# Anything bigger than this that is not the main image is almost certainly a
# heap we do not need; record it and move on rather than filling the disk.
DEFAULT_MAX_REGION = 2 * 1024 * 1024 * 1024
CHUNK = 8 * 1024 * 1024

GAME_EXE = "AION2.exe"


@dataclass
class Region:
    start: int
    end: int
    perms: str
    offset: int
    dev: str
    inode: int
    path: str
    rss: int = 0
    dumped: bool = False
    blob: str | None = None
    bytes_read: int = 0
    skipped: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return self.end - self.start

    @property
    def readable(self) -> bool:
        return self.perms[0] == "r"

    @property
    def executable(self) -> bool:
        return self.perms[2] == "x"

    @property
    def anonymous(self) -> bool:
        return not self.path or self.path.startswith("[")


def find_pid(explicit: int | None = None) -> int:
    if explicit:
        return explicit
    hits: list[tuple[int, str]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace"
            )
        except OSError:
            continue
        if GAME_EXE.lower() in cmdline.lower():
            hits.append((int(entry.name), cmdline.strip()))
    if not hits:
        sys.exit(f"no process matching {GAME_EXE!r} found -- is the game in-world?")
    # NCGuard spawns a second 'admin' helper; the game itself is the one with the
    # biggest resident set, so prefer that over guessing on pid order.
    def rss_of(pid: int) -> int:
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
        except OSError:
            pass
        return 0

    hits.sort(key=lambda h: rss_of(h[0]), reverse=True)
    if len(hits) > 1:
        print(f"note: {len(hits)} candidate processes; picking largest RSS", file=sys.stderr)
        for pid, cmd in hits:
            print(f"      pid {pid}  rss {rss_of(pid)//1024} MB  {cmd[:70]}", file=sys.stderr)
    return hits[0][0]


def read_regions(pid: int, with_rss: bool = True) -> list[Region]:
    """Parse /proc/pid/maps, and /proc/pid/smaps for per-region RSS.

    RSS matters: Wine reserves vast address ranges it never touches, and a region
    with rss==0 contains nothing to find. Filtering on it is what keeps a capture
    of a 30 GB address space down to something we can actually copy in minutes.
    """
    regions: list[Region] = []
    src = f"/proc/{pid}/smaps" if with_rss else f"/proc/{pid}/maps"
    current: Region | None = None
    try:
        with open(src, "r") as fh:
            for line in fh:
                parts = line.split()
                # Header line: "7f0000000-7f0001000 rw-p 00000000 00:00 0  [heap]"
                if len(parts) >= 5 and "-" in parts[0] and parts[1][:1] in "r-":
                    lo, _, hi = parts[0].partition("-")
                    try:
                        start, end = int(lo, 16), int(hi, 16)
                    except ValueError:
                        continue
                    current = Region(
                        start=start,
                        end=end,
                        perms=parts[1],
                        offset=int(parts[2], 16),
                        dev=parts[3],
                        inode=int(parts[4]),
                        path=" ".join(parts[5:]) if len(parts) > 5 else "",
                    )
                    regions.append(current)
                elif current is not None and line.startswith("Rss:"):
                    current.rss = int(line.split()[1]) * 1024
    except OSError as exc:
        sys.exit(f"cannot read {src}: {exc}")
    return regions


def classify(regions: list[Region], max_region: int, take_all: bool) -> None:
    """Decide what to copy. Sets .skipped on everything we pass over.

    Keep: anonymous readable memory (the UE5 heaps -- FNamePool, GUObjectArray,
    every UObject), and the game image itself (its .data holds the globals, and
    if the exe is packed on disk the in-memory copy is the only honest one).

    Skip: file-backed mappings we already have on disk (paks, DLLs, wine libs).
    Re-copying 82 GB of pak we can read any time would burn the whole window.
    """
    for r in regions:
        if not r.readable:
            r.skipped = "unreadable"
            continue
        if r.path in ("[vvar]", "[vsyscall]", "[vdso]"):
            r.skipped = "kernel-mapping"
            continue
        is_game_image = GAME_EXE.lower() in r.path.lower()
        file_backed = bool(r.inode) and not r.anonymous
        if file_backed and not is_game_image:
            # An executable file-backed region is a loaded DLL -- worth noting for
            # module discovery, but the bytes are on disk already.
            r.skipped = "file-backed (on disk)"
            continue
        if r.size > max_region and not is_game_image:
            r.skipped = f"larger than max-region ({r.size/1e9:.1f} GB)"
            continue
        if r.rss == 0 and not take_all and not is_game_image:
            r.skipped = "rss=0 (reserved, never touched)"
            continue
        r.dumped = True


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return str(n)


def cmd_maps(args: argparse.Namespace) -> int:
    pid = find_pid(args.pid)
    regions = read_regions(pid)
    classify(regions, args.max_region, args.all)

    keep = [r for r in regions if r.dumped]
    print(f"pid {pid}   {len(regions)} regions   "
          f"mapped {human(sum(r.size for r in regions))}   "
          f"resident {human(sum(r.rss for r in regions))}")
    print(f"would capture: {len(keep)} regions, {human(sum(r.rss for r in keep))} resident "
          f"({human(sum(r.size for r in keep))} mapped)\n")

    # The single most important Phase-1 fact: is the game image executable in
    # place? a packed (e.g. Themida) title unpacks into anonymous memory, which makes
    # every static-analysis technique useless. If AION2.exe's .text is mapped
    # r-xp from its own inode, the opposite is true and our life is easy.
    img = [r for r in regions if GAME_EXE.lower() in r.path.lower()]
    print(f"--- {GAME_EXE} image: {len(img)} mappings ---")
    for r in img:
        print(f"  {r.start:#014x}-{r.end:#014x} {r.perms} "
              f"size {human(r.size):>9} rss {human(r.rss):>9} off {r.offset:#x}")
    exec_img = [r for r in img if r.executable]
    exec_anon = [r for r in regions if r.executable and r.anonymous and r.rss]
    print(f"\n  executable mappings of the image : {len(exec_img)}")
    print(f"  executable ANONYMOUS regions     : {len(exec_anon)} "
          f"({human(sum(r.rss for r in exec_anon))} resident)")
    if exec_img:
        print("  => image is executable in place: static analysis and RVA math WORK.")
    else:
        print("  => image NOT executable: packed/unpacked-to-anon, like a Themida-packed title.")

    if args.verbose:
        print("\n--- loaded modules (file-backed executable) ---")
        seen = set()
        for r in regions:
            if r.executable and r.inode and r.path not in seen:
                seen.add(r.path)
                print(f"  {r.start:#014x} {r.path}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Confirm we can actually pread the process before betting the window on it."""
    pid = find_pid(args.pid)
    regions = read_regions(pid)
    classify(regions, args.max_region, args.all)
    targets = [r for r in regions if r.dumped][:12]
    if not targets:
        print("nothing classified for capture", file=sys.stderr)
        return 1
    try:
        fd = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
    except OSError as exc:
        print(f"FAIL: cannot open /proc/{pid}/mem: {exc}", file=sys.stderr)
        print("      check kernel.yama.ptrace_scope (need 0) or run with CAP_SYS_PTRACE",
              file=sys.stderr)
        return 1
    ok = bad = 0
    try:
        for r in targets:
            try:
                data = os.pread(fd, min(4096, r.size), r.start)
                ok += 1
                print(f"  ok   {r.start:#014x} {r.perms} read {len(data)} B  "
                      f"{r.path or '[anon]'}")
            except OSError as exc:
                bad += 1
                print(f"  FAIL {r.start:#014x} {r.perms} {exc}")
    finally:
        os.close(fd)
    print(f"\n{ok} readable, {bad} failed")
    if ok:
        print("READ ACCESS CONFIRMED -- safe to run 'dump'.")
    return 0 if ok and not bad else 1


def cmd_dump(args: argparse.Namespace) -> int:
    pid = find_pid(args.pid)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    regions = read_regions(pid)
    classify(regions, args.max_region, args.all)
    targets = [r for r in regions if r.dumped]
    planned = sum(r.size for r in targets)

    # Preserve the process's own account of itself alongside the bytes. maps and
    # smaps are how the offline parser turns a blob back into an address space.
    meta_dir = out / "proc"
    meta_dir.mkdir(exist_ok=True)
    for name in ("maps", "smaps", "status", "cmdline", "environ", "stat"):
        try:
            (meta_dir / name).write_bytes(Path(f"/proc/{pid}/{name}").read_bytes())
        except OSError as exc:
            print(f"note: could not save /proc/{pid}/{name}: {exc}", file=sys.stderr)

    print(f"pid {pid} -> {out}")
    print(f"capturing {len(targets)} regions, {human(planned)} mapped "
          f"({human(sum(r.rss for r in targets))} resident)\n")

    try:
        fd = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
    except OSError as exc:
        sys.exit(f"cannot open /proc/{pid}/mem: {exc}")

    started = time.time()
    total = 0
    blobs = out / "regions"
    blobs.mkdir(exist_ok=True)
    try:
        for i, r in enumerate(targets, 1):
            name = f"{r.start:016x}-{r.end:016x}.bin"
            r.blob = f"regions/{name}"
            dest = blobs / name
            written = 0
            try:
                with open(dest, "wb") as fh:
                    pos = r.start
                    while pos < r.end:
                        want = min(CHUNK, r.end - pos)
                        try:
                            data = os.pread(fd, want, pos)
                        except OSError as exc:
                            # Holes are normal: a page can be unmapped between our
                            # read of smaps and now. Record and keep going -- a
                            # partial region is far better than an aborted capture.
                            r.errors.append(f"{pos:#x}: {exc.strerror}")
                            pos += want
                            continue
                        if not data:
                            r.errors.append(f"{pos:#x}: short read")
                            break
                        fh.write(data)
                        written += len(data)
                        pos += len(data)
            except OSError as exc:
                r.errors.append(str(exc))
            r.bytes_read = written
            total += written
            if written == 0:
                dest.unlink(missing_ok=True)
                r.blob = None
                r.dumped = False
                r.skipped = "read returned nothing"
            rate = total / max(time.time() - started, 0.001)
            print(f"\r[{i}/{len(targets)}] {human(total)} @ {human(int(rate))}/s"
                  f"  {r.start:#014x}    ", end="", flush=True)
    finally:
        os.close(fd)

    elapsed = time.time() - started
    print(f"\n\ncaptured {human(total)} in {elapsed:.1f}s")

    manifest = {
        "tool": "aesteria_snapshot",
        "version": 1,
        "pid": pid,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "label": args.label,
        "game_exe": GAME_EXE,
        "pointer_size": 8,
        "bytes_captured": total,
        "elapsed_sec": round(elapsed, 2),
        "regions": [asdict(r) for r in regions],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    errs = sum(len(r.errors) for r in regions)
    print(f"manifest.json written ({len(regions)} regions described, {errs} read holes)")
    print(f"\nCAPTURE BANKED: {out}")
    return 0


def main() -> int:
    # Shared flags live on a parent parser so they work in either position --
    # "maps --pid N" as well as "--pid N maps". At 04:00 nobody should be
    # debugging argument order.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--pid", type=int, help=f"explicit pid (default: find {GAME_EXE})")
    common.add_argument("--max-region", type=int, default=DEFAULT_MAX_REGION,
                        help="skip anonymous regions larger than this")
    common.add_argument("--all", action="store_true",
                        help="also capture regions with rss=0 (reserved but untouched)")
    common.add_argument("-v", "--verbose", action="store_true")

    ap = argparse.ArgumentParser(description=__doc__, parents=[common],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("maps", parents=[common],
                   help="classify regions; answers 'is the image executable'")
    sub.add_parser("probe", parents=[common], help="verify /proc/pid/mem is readable")
    d = sub.add_parser("dump", parents=[common], help="copy memory to disk")
    d.add_argument("-o", "--out", required=True, help="output directory")
    d.add_argument("--label", default="", help="note stored in the manifest")

    args = ap.parse_args()
    return {"maps": cmd_maps, "probe": cmd_probe, "dump": cmd_dump}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
