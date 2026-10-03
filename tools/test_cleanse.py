#!/usr/bin/env python3
"""Offline checks for aesteria_cleanse's state machine -- fake debuff icons, fake clock."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from aesteria_cleanse import Cleanser, COLLAPSED

fails = 0
def check(name, ok):
    global fails
    print(("  ok   " if ok else "  FAIL ") + name)
    fails += not ok

class Fake:
    def __init__(self): self.vis = {}
    def read(self, a, n): return bytes([self.vis[a - 228]])
live = Fake()
t = [0.0]
pressed = []
focus = [True]
def setup(n):
    live.vis = {i: 4 for i in range(n)}
def new():
    return Cleanser(live, lambda: list(live.vis), cooldown=10, clock=lambda: t[0],
                    focused=lambda: focus[0], press=lambda: pressed.append(t[0]))
def step(c, secs, dt=0.25):
    end = t[0] + secs
    while t[0] < end:
        c.tick(); t[0] += dt

setup(0); c = new(); step(c, 5)
check("no debuff -> no press", pressed == [])
setup(1); step(c, 1)
check("a debuff -> one press", len(pressed) == 1)
setup(0); step(c, 15)
check("removed -> no more presses", len(pressed) == 1 and c.fails == 0)

pressed.clear(); t[0] = 100; c = new(); setup(1)
step(c, 9)
check("cooldown respected (one press inside 9 s)", len(pressed) == 1)
step(c, 30)
check("unremovable: exactly 2 presses then parked", len(pressed) == 2 and c.stuck == 1)
step(c, 60)
check("parked stays parked while nothing changes", len(pressed) == 2)
setup(2); step(c, 1)
check("a NEW debuff un-parks and presses", len(pressed) == 3 and c.stuck is None)

pressed.clear(); t[0] = 500; c = new(); setup(1); focus[0] = False
step(c, 30)
check("game not focused -> never presses", pressed == [])
focus[0] = True; step(c, 1)
check("focus back -> presses", len(pressed) == 1)

pressed.clear(); t[0] = 900; c = new(); c.on = False; setup(2); step(c, 30)
check("toggled off -> never presses", pressed == [])
live.vis = {0: COLLAPSED, 1: COLLAPSED}; c.on = True; step(c, 5)
check("collapsed icons are not debuffs", pressed == [] and c.count == 0)

print("FAIL" if fails else "all ok")
sys.exit(1 if fails else 0)
