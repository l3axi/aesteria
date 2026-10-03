#!/usr/bin/env python3
"""Positive control for the hpscan engine: plant a known 'HP' and make it find it.

A negative result means nothing without a positive control. Before
this scanner is pointed at a live account's client and the operator spends game actions on it, it
has to prove it can find a value it was never told, in a foreign process, purely from the
DIRECTION the value moved — which is exactly what the real hunt asks of it.

The target process holds its value in a numpy buffer on the heap (so it sits in an anonymous
writable region like a real game object), surrounded by decoys that change in other ways:
a monotonic counter, a jittering float, and a large block of churn. Those are the things a
naive dec/inc scan would keep, so they are the things the control has to eliminate.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aesteria_hpscan as H                                # noqa: E402

TARGET = r'''
import numpy as np, os, sys, time
hp   = np.zeros(1, dtype="<u4"); hp[0] = 1780          # the value to be found
tick = np.zeros(1, dtype="<u4")                        # decoy: monotonic counter
noise = np.zeros(1 << 20, dtype="<u4")                 # decoy: 4 MB of churn
rng = np.random.default_rng(7)
state = open(sys.argv[1], "r")
print(os.getpid(), flush=True)
while True:
    tick[0] += 1
    noise[:] = rng.integers(0, 2**31, size=noise.size, dtype="<u4")
    state.seek(0)
    want = state.read().strip()
    if want == "hit":   hp[0] = 1200
    elif want == "heal": hp[0] = 1650
    elif want == "full": hp[0] = 1780
    elif want == "quit": break
    time.sleep(0.05)
'''


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    statef = tmp / "state"
    statef.write_text("full")
    script = tmp / "target.py"
    script.write_text(TARGET)

    proc = subprocess.Popen([sys.executable, str(script), str(statef)],
                            stdout=subprocess.PIPE, text=True)
    pid = int(proc.stdout.readline().strip())
    time.sleep(0.5)
    print(f"target process {pid} running, HP=1780 among decoys")

    def act(what: str) -> None:
        statef.write_text(what)
        time.sleep(0.4)                 # let the target apply it and churn the decoys

    ok = True
    try:
        scan = H.Scan(pid)
        print(f"regions: {len(scan.regions)}")
        scan.baseline()

        act("hit")
        scan.filter("dec")              # 1780 -> 1200
        act("heal")
        scan.filter("inc")              # 1200 -> 1650
        act("full")
        scan.filter("inc")              # 1650 -> 1780
        act("full")
        scan.filter("same")             # unchanged: kills the counter and the churn

        found = []
        for i, (offs, vals) in scan.cand.get("u32", {}).items():
            base = scan.regions[i][0]
            for o, v in zip(offs, vals):
                found.append((base + int(o) * 4, int(v)))

        print(f"u32 survivors: {len(found)}")
        for a, v in found[:10]:
            print(f"   {a:#x} = {v}")

        if not any(v == 1780 for _, v in found):
            print("FAIL: the planted HP was not among the survivors")
            ok = False
        elif len(found) > 50:
            print(f"FAIL: {len(found)} survivors — the decoys were not eliminated")
            ok = False
        else:
            print(f"PASS: planted HP found, {len(found)} survivor(s), decoys eliminated")
    finally:
        statef.write_text("quit")
        time.sleep(0.3)
        proc.kill()

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
