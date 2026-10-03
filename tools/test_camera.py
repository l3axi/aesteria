#!/usr/bin/env python3
"""Offline checks for aesteria_camera.project -- signs and axes, no game needed."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from aesteria_camera import Camera

W, H = 1920, 1080
fails = 0
def check(name, ok):
    global fails
    print(("  ok   " if ok else "  FAIL ") + name)
    fails += not ok

c = Camera((0, 0, 0), (0, 0, 0), 90.0, W / H)          # at origin, looking down +X
px, py, _ = c.project((1000, 0, 0), W, H)
check("straight ahead lands on the centre", abs(px - W/2) < 1e-6 and abs(py - H/2) < 1e-6)
check("+Y is to the RIGHT (UE is left-handed)", c.project((1000, 300, 0), W, H)[0] > W / 2)
check("+Z is UP the screen", c.project((1000, 0, 300), W, H)[1] < H / 2)
check("behind the camera is None", c.project((-1000, 0, 0), W, H) is None)
check("45 deg right sits on the right edge at 90 deg HFOV", abs(c.project((1000, 1000, 0), W, H)[0] - W) < 1e-6)
c2 = Camera((0, 0, 0), (0, 90, 0), 90.0, W / H)        # yaw 90: looking down +Y
check("yaw 90 looks down +Y", abs(c2.project((0, 1000, 0), W, H)[0] - W/2) < 1e-6)
c3 = Camera((0, 0, 0), (-30, 0, 0), 90.0, W / H)       # pitched down 30
check("pitch down puts a level point ABOVE centre", c3.project((1000, 0, 0), W, H)[1] < H / 2)
print(f"{'PASS' if not fails else 'FAIL'}  ({fails} failed)")
sys.exit(1 if fails else 0)
