#!/usr/bin/env python3
"""aesteria_netcap — packet capture of a live AION 2 session, scoped to the game.

WHY IT DISCOVERS INSTEAD OF BEING TOLD
    The game's server address is not knowable in advance -- it changes per session, and a zone
    handoff moves it mid-session. Nor is the interface the obvious one: if you route the game
    through a VPN, the client's socket is bound to the tunnel, and capturing on your physical
    NIC records the tunnel's OUTER UDP -- encrypted, and useless for reading the protocol.
    Capturing on the interface the socket is actually bound to gets the game protocol itself.

    So this reads the game process's own sockets out of /proc and builds both the filter and the
    interface from what is genuinely connected right now, instead of asking you to supply either.

WHY THE FILTER IS NARROW BY DEFAULT -- THIS IS A PRIVACY DECISION, NOT A CONVENIENCE ONE
    A blanket capture on that interface would record everything you do online, since a tunnel
    carries all of it. The default filter is the game's own non-TLS endpoints plus the port range
    around them (so a zone handoff is still caught), which excludes your browsing, your mail, and
    the client's own TLS auth/CDN connections. --all makes it wide, deliberately and with a
    warning. Captures are written to your own disk and are never transmitted anywhere.

TIER
    This is a read-only inspector: capture and offline analysis, never replay or injection.
    Capture and offline analysis only.

PRIVILEGE — RUN THIS WITHOUT sudo
    Capture needs CAP_NET_RAW. On the host `dumpcap` carries it as a file capability and is
    root:wireshark 0754; your user must be in the `wireshark` group, so plain
    `aesteria_netcap.py start` is the correct invocation and needs no password.

    Under sudo it is WORSE, not better: dumpcap permanently relinquishes every capability
    once the capture device is open, and a root process without CAP_DAC_OVERRIDE cannot
    write into your own 0755 directory. See capture_cmd() for the full trap. If run under
    sudo anyway, this drops back to SUDO_UID rather than failing.

    A group change only reaches processes started AFTER it, so a long-running shell or agent
    session from before the usermod still cannot exec dumpcap and will fall back to tcpdump.

USAGE
    aesteria_netcap.py sockets       # what the game is connected to, right now
    aesteria_netcap.py print         # the exact capture command, to eyeball or paste
    aesteria_netcap.py start         # run it (needs the privilege above)
    aesteria_netcap.py stop
    aesteria_netcap.py status
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUTDIR = REPO / "captures" / "net"
GAME_EXE = "AION2.exe"

# ⚠ NOT /tmp, and this is not a style preference.
# the host runs with `fs.protected_regular = 1`, which forbids opening a regular file for
# write in a world-writable STICKY directory when the file is owned by someone else -- and
# ROOT IS NOT EXEMPT. So a log or pidfile created here by your user during an unprivileged dry
# run makes the later `sudo ... start` die with a bare PermissionError as root, which reads
# like a nonsense error and sends you looking at sudo, sysctls and the capture stack in turn.
# Keeping this state in a directory we own removes the whole class.
PIDFILE = OUTDIR / "netcap.pid"
LOGFILE = OUTDIR / "netcap.log"

# Ports we deliberately do NOT record: TLS/HTTP carries the account's auth flow and, on this
# tunnel, the operator's ordinary browsing. Neither is readable and neither is ours to keep.
EXCLUDED_PORTS = {80, 443, 53, 123}


def hex_to_addr(field: str) -> tuple[str, int]:
    """Decode a /proc/net/tcp address: little-endian hex word, then :PORT."""
    ip_hex, port_hex = field.split(":")
    raw = bytes.fromhex(ip_hex)
    if len(raw) == 4:
        ip = socket.inet_ntop(socket.AF_INET, raw[::-1])
    else:
        # IPv6 is stored as four little-endian 32-bit words
        words = [raw[i:i + 4][::-1] for i in range(0, 16, 4)]
        ip = socket.inet_ntop(socket.AF_INET6, b"".join(words))
    return ip, int(port_hex, 16)


def game_pids() -> list[int]:
    """Every pid whose argv[0] basename is the game exe.

    argv[0], not the whole cmdline: the launch chain hands the game's full path to
    steam.exe, the proton python3 and the Steam reaper, so their cmdlines all contain
    AION2.exe without being the game.
    """
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmd = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not cmd:
            continue
        argv0 = cmd.split(b"\0", 1)[0].decode("utf-8", "replace")
        base = argv0.replace("\\", "/").rsplit("/", 1)[-1]
        if base.lower() == GAME_EXE.lower():
            out.append(int(entry.name))
    return sorted(out)


def sockets_for(pid: int) -> list[dict]:
    """Established TCP/UDP endpoints owned by this pid, via fd -> inode -> /proc/net/*."""
    inodes = set()
    fd_dir = Path(f"/proc/{pid}/fd")
    try:
        for fd in fd_dir.iterdir():
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
    except OSError:
        return []

    found = []
    for proto in ("tcp", "tcp6", "udp", "udp6"):
        path = Path(f"/proc/{pid}/net/{proto}")
        try:
            lines = path.read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            if len(f) < 10 or f[9] not in inodes:
                continue
            lip, lport = hex_to_addr(f[1])
            rip, rport = hex_to_addr(f[2])
            if rip in ("0.0.0.0", "::") or rport == 0:
                continue                       # listening / unconnected
            found.append({"proto": proto, "local": f"{lip}:{lport}",
                          "local_ip": lip, "remote_ip": rip, "remote_port": rport,
                          "state": f[3]})
    return found


def iface_for_ip(ip: str) -> str | None:
    try:
        data = json.loads(subprocess.run(["ip", "-j", "-4", "addr"],
                                         capture_output=True, text=True, check=True).stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    for link in data:
        for a in link.get("addr_info", []):
            if a.get("local") == ip:
                return link.get("ifname")
    return None


def discover(all_traffic: bool) -> tuple[str, str, list[dict]]:
    pids = game_pids()
    if not pids:
        sys.exit(f"no {GAME_EXE} process found — is the client running?")
    socks: list[dict] = []
    for pid in pids:
        socks.extend(sockets_for(pid))
    if not socks:
        sys.exit(f"{GAME_EXE} is running (pids {pids}) but owns no connected sockets yet — "
                 "wait until it is past the launcher, then retry")

    game = [s for s in socks
            if s["remote_port"] not in EXCLUDED_PORTS
            and not s["remote_ip"].startswith("127.")]
    interesting = socks if all_traffic else game
    if not interesting:
        sys.exit("only TLS/loopback connections are open — the client has not reached a "
                 "game server yet. Retry once you are in the world.")

    local_ips = {s["local_ip"] for s in interesting if not s["local_ip"].startswith("127.")}
    iface = None
    for ip in sorted(local_ips):
        iface = iface_for_ip(ip)
        if iface:
            break
    if not iface:
        sys.exit(f"could not map local address(es) {sorted(local_ips)} to an interface")

    if all_traffic:
        bpf = "ip or ip6"
    else:
        hosts = sorted({s["remote_ip"] for s in game})
        ports = sorted({s["remote_port"] for s in game})
        # A zone handoff moves the server but stays in the same port neighbourhood, so widen
        # to the enclosing /100 of ports; still far narrower than "everything on the tunnel".
        lo, hi = min(ports) // 100 * 100, (max(ports) // 100 + 1) * 100 - 1
        host_expr = " or ".join(f"host {h}" for h in hosts)
        bpf = f"({host_expr}) or (tcp portrange {lo}-{hi})"
    return iface, bpf, socks


def target_identity() -> tuple[int, int, str]:
    """Who the capture should actually run as — never root if we can help it."""
    if os.geteuid() == 0 and os.environ.get("SUDO_UID"):
        uid = int(os.environ["SUDO_UID"])
        gid = int(os.environ.get("SUDO_GID", "0"))
        return uid, gid, pwd.getpwuid(uid).pw_name
    uid = os.getuid()
    return uid, os.getgid(), pwd.getpwuid(uid).pw_name


def can_exec_as(path: str, uid: int, gid: int, name: str) -> bool:
    """Would THAT user be able to execute this? os.access answers for us, not them."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    if st.st_uid == uid:
        return bool(st.st_mode & 0o100)
    try:
        groups = set(os.getgrouplist(name, gid))
    except (OSError, KeyError):
        groups = {gid}
    if st.st_gid in groups:
        return bool(st.st_mode & 0o010)
    return bool(st.st_mode & 0o001)


def capture_cmd(iface: str, bpf: str) -> tuple[list[str], tuple[int, int, str] | None]:
    """The command, plus the identity to drop to before running it (None = run as-is).

    ⚠ NEVER RUN dumpcap UNDER sudo. dumpcap is built to run unprivileged with file
    capabilities (cap_net_raw etc.), and it PERMANENTLY RELINQUISHES every capability once
    the capture device is open, before it touches the output file. Under sudo the uid stays
    0, so what is left is a CAPABILITY-LESS ROOT process -- and for a root process, DAC
    bypass IS CAP_DAC_OVERRIDE. It therefore has strictly FEWER rights than the ordinary
    user: it cannot write into your own 0755 directory, and fails with

        dumpcap: The file to which the capture would be saved ... Permission denied

    which reads like an impossible error for root. Measured here 2026-09-19. So when we are
    root with a SUDO_UID to go back to, we hand the capture back to that user and let
    dumpcap's own capabilities do the work they exist for.
    """
    OUTDIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = OUTDIR / f"aion2-{stamp}.pcap"

    uid, gid, name = target_identity()
    drop = (uid, gid, name) if uid != os.geteuid() else None

    dumpcap = shutil.which("dumpcap")
    if dumpcap and can_exec_as(dumpcap, uid, gid, name):
        # -b filesize is in kB; 20 x 100 MB ring = 2 GB ceiling, oldest overwritten.
        return ([dumpcap, "-i", iface, "-f", bpf, "-s", "0",
                 "-b", "filesize:100000", "-b", "files:20", "-w", str(out)], drop)

    # No usable dumpcap: tcpdump, which genuinely does need root, so do NOT drop.
    return (["tcpdump", "-i", iface, "-s", "0", "-U", "-n", "-Z", name,
             "-C", "100", "-W", "20", "-w", str(out), bpf], None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["sockets", "print", "start", "stop", "status"])
    ap.add_argument("--all", action="store_true",
                    help="capture ALL traffic on the interface, not just the game's. The "
                         "tunnel carries everything, so this records browsing too.")
    ap.add_argument("--iface", help="override the discovered interface")
    args = ap.parse_args()

    if args.action == "status":
        if PIDFILE.exists():
            pid = int(PIDFILE.read_text().strip())
            alive = Path(f"/proc/{pid}").exists()
            print(f"capture pid {pid}: {'RUNNING' if alive else 'dead (stale pidfile)'}")
            for f in sorted(OUTDIR.glob("aion2-*")):
                print(f"  {f.name}  {f.stat().st_size/1e6:.1f} MB")
        else:
            print("no capture running")
        return 0

    if args.action == "stop":
        if not PIDFILE.exists():
            print("no capture running")
            return 0
        pid = int(PIDFILE.read_text().strip())
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"stopped capture pid {pid}")
        except ProcessLookupError:
            print(f"capture pid {pid} was already gone")
        except PermissionError:
            # Only reachable if the capture tool did not drop privileges (tcpdump -Z does,
            # dumpcap does not need to). Say so rather than leaving a live capture running.
            print(f"capture pid {pid} is running as another user — "
                  f"stop it with: sudo kill {pid}", file=sys.stderr)
            return 1
        PIDFILE.unlink(missing_ok=True)
        return 0

    iface, bpf, socks = discover(args.all)
    if args.iface:
        iface = args.iface

    if args.action == "sockets":
        print(f"{GAME_EXE} pids: {game_pids()}")
        for s in socks:
            tag = "GAME" if (s["remote_port"] not in EXCLUDED_PORTS
                             and not s["remote_ip"].startswith("127.")) else "    "
            print(f"  {tag} {s['proto']:5s} {s['local']:22s} -> "
                  f"{s['remote_ip']}:{s['remote_port']}")
        print(f"\ninterface: {iface}")
        print(f"filter:    {bpf}")
        return 0

    cmd, drop = capture_cmd(iface, bpf)
    if args.action == "print":
        print(" ".join(f"'{c}'" if " " in c else c for c in cmd))
        if drop:
            print(f"# will be run as {drop[2]} (uid {drop[0]}), not root — see capture_cmd")
        return 0

    logpath = LOGFILE
    logpath.parent.mkdir(parents=True, exist_ok=True)

    preexec = None
    if drop:
        d_uid, d_gid, d_name = drop
        def preexec():                       # runs in the child, after fork
            os.initgroups(d_name, d_gid)     # so dumpcap gets the wireshark group
            os.setgid(d_gid)
            os.setuid(d_uid)                 # last: irreversible
        print(f"dropping to {d_name} (uid {d_uid}) — dumpcap needs its own "
              f"capabilities, not root")

    with open(logpath, "ab") as log:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log,
                                preexec_fn=preexec)

    # A capture tool without CAP_NET_RAW spawns fine and dies immediately on the interface
    # open. Without this check we would write a pidfile for a corpse and report success --
    # and the first anyone would know is an empty pcap after the session ended.
    time.sleep(1.0)
    if proc.poll() is not None:
        PIDFILE.unlink(missing_ok=True)
        tail = logpath.read_text(errors="replace").strip().splitlines()[-4:]
        print(f"capture FAILED to start (exit {proc.returncode}):", file=sys.stderr)
        for line in tail:
            print(f"  {line}", file=sys.stderr)
        print("\nCapture needs CAP_NET_RAW. Either:\n"
              "  sudo python3 tools/aesteria_netcap.py start        # one-off\n"
              "  sudo usermod -aG wireshark $USER                  # once, then dumpcap\n"
              "                                                     # works unprivileged",
              file=sys.stderr)
        return 1

    PIDFILE.write_text(str(proc.pid))
    # Started under sudo, these come out root-owned, and the next unprivileged run could
    # neither append to the log nor rewrite the pidfile. Hand them back.
    if os.geteuid() == 0 and os.environ.get("SUDO_UID"):
        uid = int(os.environ["SUDO_UID"])
        gid = int(os.environ.get("SUDO_GID", "-1"))
        for p in (PIDFILE, LOGFILE, OUTDIR):
            try:
                os.chown(p, uid, gid)
            except OSError:
                pass
    print(f"capture started (pid {proc.pid}) on {iface}")
    print(f"  filter: {bpf}")
    print(f"  files:  {OUTDIR}/aion2-*.pcap  (ring, 2 GB ceiling)")
    print("  stop:   aesteria_netcap.py stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
