#!/usr/bin/env python3
"""aesteria_iostore -- minimal IoStore (.utoc) reader: enough to verify an AES key.

UE 5.3 FIoStoreTocHeader, version 5 (PerfectHashWithOverflow). Layout after the 144-byte header:
    ChunkIds[entries]*12, OffsetLengths[entries]*10, PerfectHashSeeds[seeds]*4,
    ChunksWithoutPerfectHash[n]*4, CompressionBlocks[blocks]*12,
    CompressionMethodNames[count*len], (Signed: i32 hashSize, toc sig, block sig,
    blocks*20 SHA1), DirectoryIndex[dirSize] (AES-256-ECB when Encrypted), ChunkMetas.
A decrypted directory index starts with FString MountPoint: i32 len, then a NUL-terminated
string. On this game the mount point is "/" (len 2) -- checked on an UNencrypted container,
which is the positive control for every offset above.
"""
from __future__ import annotations
import struct
from pathlib import Path

PAKS = Path("/games/SteamLibrary/steamapps/common/AION2/Aion2/Content/Paks")
F_COMPRESSED, F_ENCRYPTED, F_SIGNED, F_INDEXED = 1, 2, 4, 8


def read_toc(path: Path) -> dict:
    b = path.read_bytes()
    if b[:16] != b"-==--==--==--==-":
        raise ValueError("not a utoc")
    (ver,) = struct.unpack_from("<B", b, 16)
    (hdr, entries, blocks, block_entry_size, n_methods, method_len, block_size, dir_size,
     partitions) = struct.unpack_from("<9I", b, 20)
    container_id, = struct.unpack_from("<Q", b, 56)
    flags = b[80]
    seeds, = struct.unpack_from("<I", b, 84)
    part_size, = struct.unpack_from("<Q", b, 88)
    no_ph, = struct.unpack_from("<I", b, 96)
    off = hdr
    off += entries * 12 + entries * 10 + seeds * 4 + no_ph * 4
    off += blocks * block_entry_size + n_methods * method_len
    if flags & F_SIGNED:
        (hs,) = struct.unpack_from("<i", b, off)
        off += 4 + hs * 2 + blocks * 20
    return {"path": path, "version": ver, "flags": flags, "entries": entries,
            "dir_off": off, "dir_size": dir_size, "dir": b[off:off + dir_size],
            "container_id": container_id}


def aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    d = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return d.update(data) + d.finalize()


def key_opens(key: bytes, toc: dict) -> bool:
    """True when the key turns the directory index into a sane mount-point string."""
    if len(key) != 32 or len(toc["dir"]) < 16:
        return False
    return mount_point_ok(aes_ecb_decrypt(key, toc["dir"][:16]))


def mount_point_ok(blk: bytes) -> bool:
    n = struct.unpack_from("<i", blk, 0)[0]
    printable = lambda bs: all(32 <= c < 127 for c in bs)
    if 1 <= n <= 11:
        return printable(blk[4:4 + n - 1]) and blk[4 + n - 1] == 0
    return 12 <= n <= 512 and printable(blk[4:16])


def an_encrypted_toc() -> dict:
    for p in sorted(PAKS.glob("*.utoc")):
        t = read_toc(p)
        if t["flags"] & F_ENCRYPTED and t["flags"] & F_INDEXED and t["dir_size"] >= 16:
            return t
    raise SystemExit("no encrypted, indexed container found")


if __name__ == "__main__":
    t = an_encrypted_toc()
    print(t["path"].name, "flags", t["flags"], "dir", t["dir_size"], "bytes at", t["dir_off"])
    # sanity: an UNencrypted indexed toc must show the plain mount point at the same offset
    for p in sorted(PAKS.glob("*.utoc")):
        u = read_toc(p)
        if not u["flags"] & F_ENCRYPTED and u["flags"] & F_INDEXED and u["dir_size"] >= 16:
            print("plain control:", p.name, u["dir"][:8], "oracle accepts it:",
                  mount_point_ok(u["dir"][:16]))
            break


# ── extraction (uncompressed or compressed-with-method-0 containers) ─────────────────────────
def _u40be(b, o):
    return int.from_bytes(b[o:o + 5], "big")


def open_container(path: Path, key: bytes | None) -> dict:
    """Parse enough of a utoc to extract files by name: dir index, offsets, blocks."""
    t = read_toc(path)
    b = path.read_bytes()
    hdr, entries = struct.unpack_from("<2I", b, 20)
    blocks, bes, nm, ml, bsize = struct.unpack_from("<5I", b, 28)
    seeds, = struct.unpack_from("<I", b, 84)
    no_ph, = struct.unpack_from("<I", b, 96)
    o = hdr + entries * 12
    offlen = [(_u40be(b, o + i * 10), _u40be(b, o + i * 10 + 5)) for i in range(entries)]
    o += entries * 10 + seeds * 4 + no_ph * 4
    blk = []
    for i in range(blocks):
        e = b[o + i * bes:o + (i + 1) * bes]
        blk.append((int.from_bytes(e[0:5], "little"), int.from_bytes(e[5:8], "little"),
                    int.from_bytes(e[8:11], "little"), e[11]))
    d = t["dir"][: t["dir_size"] // 16 * 16] if t["flags"] & F_ENCRYPTED else t["dir"]
    if t["flags"] & F_ENCRYPTED:
        d = aes_ecb_decrypt(key, d)
    mo = o + blocks * bes
    methods = [b[mo + i * ml:mo + (i + 1) * ml].rstrip(b"\0").decode() for i in range(nm)]
    return {"toc": t, "offlen": offlen, "blocks": blk, "block_size": bsize, "methods": methods,
            "files": _parse_dir_index(d), "key": key, "ucas": path.with_suffix(".ucas")}


def _parse_dir_index(d: bytes) -> dict:
    """FIoDirectoryIndexResource -> {path: chunk_index}."""
    o = 0
    n, = struct.unpack_from("<i", d, o); o += 4 + n            # mount point
    nd, = struct.unpack_from("<i", d, o); o += 4
    dirs = [struct.unpack_from("<4I", d, o + i * 16) for i in range(nd)]; o += nd * 16
    nf, = struct.unpack_from("<i", d, o); o += 4
    files = [struct.unpack_from("<3I", d, o + i * 12) for i in range(nf)]; o += nf * 12
    ns, = struct.unpack_from("<i", d, o); o += 4
    strings = []
    for _ in range(ns):
        ln, = struct.unpack_from("<i", d, o); o += 4
        if ln < 0:
            s = d[o:o - ln * 2].decode("utf-16-le").rstrip("\0"); o += -ln * 2
        else:
            s = d[o:o + ln].decode("latin-1").rstrip("\0"); o += ln
        strings.append(s)
    NONE = 0xFFFFFFFF
    out = {}

    def walk(di, prefix):
        name, child, _sib, ffile = dirs[di]
        here = prefix + (strings[name] + "/" if name != NONE else "")
        f = ffile
        while f != NONE:
            fname, nxt, chunk = files[f]
            out[here + strings[fname]] = chunk
            f = nxt
        c = child
        while c != NONE:
            walk(c, here)
            c = dirs[c][2]
    if dirs:
        walk(0, "")
    return out


def extract(c: dict, path: str) -> bytes:
    off, ln = c["offlen"][c["files"][path]]
    bs = c["block_size"]
    out = bytearray()
    with open(c["ucas"], "rb") as f:
        for bi in range(off // bs, (off + ln + bs - 1) // bs):
            boff, csize, usize, method = c["blocks"][bi]
            f.seek(boff)
            raw = f.read((csize + 15) // 16 * 16)
            if c["toc"]["flags"] & F_ENCRYPTED:
                raw = aes_ecb_decrypt(c["key"], raw)
            if method == 0:
                out += raw[:usize]
            elif c["methods"][method - 1] == "Oodle":
                import ooz                      # pyooz, in the project .venv
                out += ooz.decompress(raw[:csize], usize)
            else:
                raise NotImplementedError(c["methods"][method - 1])
    start = off % bs
    return bytes(out[start:start + ln])
