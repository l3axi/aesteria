#!/usr/bin/env python3
"""Offline checks for aesteria_route -- the optimiser against exact answers, and the follower."""
import itertools
import random
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import numpy as np
from aesteria_route import (Route, _dist_matrix, _nearest_neighbour, _or_opt, _two_opt,
                            build_tour, tour_length)

fails = 0
def check(name, ok):
    global fails
    print(("  ok   " if ok else "  FAIL ") + name)
    fails += not ok

def nodes_of(pts):
    return [{"id": str(i), "kind": "feather", "label": str(i), "x": p[0], "y": p[1], "z": p[2]}
            for i, p in enumerate(pts)]

rng = random.Random(7)
# 1. exact optimum on small instances (brute force over every closed tour starting at 0)
worst = 0.0
for trial in range(25):
    pts = [(rng.uniform(0, 1e5), rng.uniform(0, 1e5), rng.uniform(0, 1e4)) for _ in range(8)]
    D = _dist_matrix(np.array(pts))
    best = min(tour_length([0, *p], D) for p in itertools.permutations(range(1, 8)))
    got = tour_length(build_tour(nodes_of(pts), neighbours=7), D)
    worst = max(worst, got / best - 1)
check(f"8-point tours within 3 % of the exact optimum (worst {worst * 100:.2f} %)", worst < 0.03)

# 2. no local move ever makes a tour longer, and every tour is a permutation
pts = [(rng.uniform(0, 1e5), rng.uniform(0, 1e5), 0) for _ in range(120)]
D = _dist_matrix(np.array(pts))
near = np.argsort(D, axis=1)[:, 1:13]
order = _nearest_neighbour(D)
ok_monotone = True
for _ in range(40):
    before = tour_length(order, D)
    moved = _two_opt(order, D, near) | _or_opt(order, D)
    after = tour_length(order, D)
    ok_monotone &= after <= before + 1e-6
    if not moved:
        break
check("2-opt / Or-opt sweeps never lengthen the tour", ok_monotone)
check("tour stays a permutation", sorted(order) == list(range(120)))
check("optimised beats nearest-neighbour on 120 points",
      tour_length(order, D) < tour_length(_nearest_neighbour(D), D))

# 3. the follower: start at the nearest point, walk the loop, skip what is done
line = [(i * 1000.0, 0.0, 0.0) for i in range(10)]          # a straight row, tour 0..9
r = Route(nodes_of(line), list(range(10)))
r.start((3100.0, 0.0, 0.0), set())
up = [n["id"] for n in r.upcoming(set(), 3)]
check("starts at the nearest point", up[0] == "3")
check("walks toward the nearer neighbour", up[1] in ("4", "2"))
done = {"3", up[1]}
nxt = r.upcoming(done, 1)[0]["id"]
check("skips visited points and keeps direction", nxt == ("5" if up[1] == "4" else "1"))
check("remaining counts unvisited", r.remaining(done) == 8)
r.start((0, 0, 0), {str(i) for i in range(10)})
check("nothing left = empty route", r.upcoming(set(str(i) for i in range(10))) == [])

print("FAIL" if fails else "all ok")
sys.exit(1 if fails else 0)
