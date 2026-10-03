#!/usr/bin/env python3
"""aesteria_route -- the shortest loop through every feather and dungeon in Altgard.

the operator, 10-02: "a toggle for the fastest path between all feathers and dungeons for my
completionist run".

HOW
    build   one closed TOUR over every feather spawn (aesteria_godfragments.py, 543) and every
            routed entrance (aesteria_dungeons.py): nearest-neighbour seed, then 2-opt and Or-opt
            until no move shortens it. Straight-line 3D distance, because the run is FLOWN.
            Written once to snaps/route_world_d_a.json; collected points are skipped at RUNTIME,
            so the tour never needs rebuilding as he plays.
    follow  Route.start(me) cuts the loop at the nearest unvisited point and walks it in whichever
            direction is shorter from there; Route.upcoming(me, done) is the next k points.
            Re-starting (toggle off/on) re-anchors after a detour.

ROUTED KINDS (default): feathers + party + solo dungeons + the advanced seal + the garrison +
    the 40 Kibelisks (teleport artifacts; the operator 10-02: "not-yet-activated" -- F10 marks one done).
    + the 60 "Undiscovered Area" portals (kind "seal"; the operator 10-02: "add these to the route").
    --all adds every portal kind. What a "Seal" is in-game is
    unknown (aesteria_dungeons.py), so they are not forced into a completionist run unasked.

    python3 tools/aesteria_route.py [--seals | --all]      # build + print the tour length
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

SNAPS = Path(__file__).resolve().parents[1] / "snaps"
FEATHERS = SNAPS / "godfragments_world_d_a.json"
DUNGEONS = SNAPS / "dungeons_world_d_a.json"
OUT = SNAPS / "route_world_d_a.json"
DEFAULT_KINDS = ("party", "solo", "seal+", "keep", "kibelisk", "seal", "event")


def load_nodes(kinds=DEFAULT_KINDS) -> list[dict]:
    nodes = [{"id": f"F:{e['group']}_{e['idx']}", "kind": "feather",
              "label": "feather " + e["group"].rsplit("_", 1)[-1].lower(),
              "x": e["x"], "y": e["y"], "z": e["z"]}
             for e in json.loads(FEATHERS.read_text())]
    nodes += [{"id": f"D:{e['name']}", "kind": e["kind"], "label": e["label"],
               "x": e["x"], "y": e["y"], "z": e["z"]}
              for e in json.loads(DUNGEONS.read_text()) if e["kind"] in kinds]
    return nodes


def _dist_matrix(pts: np.ndarray) -> np.ndarray:
    d = pts[:, None, :] - pts[None, :, :]
    return np.sqrt((d * d).sum(-1))


def tour_length(order: list[int], D: np.ndarray) -> float:
    return float(sum(D[order[i], order[(i + 1) % len(order)]] for i in range(len(order))))


def _nearest_neighbour(D: np.ndarray) -> list[int]:
    n = len(D)
    left = set(range(1, n))
    order = [0]
    while left:
        cur = order[-1]
        nxt = min(left, key=lambda j: D[cur, j])
        order.append(nxt)
        left.remove(nxt)
    return order


def _two_opt(order: list[int], D: np.ndarray, near: np.ndarray) -> bool:
    """One sweep of neighbour-list 2-opt on a closed tour. True if anything improved."""
    n = len(order)
    pos = {c: i for i, c in enumerate(order)}
    improved = False
    for i in range(n):
        a, b = order[i], order[(i + 1) % n]
        for c in near[a]:
            j = pos[c]
            d = order[(j + 1) % n]
            if c == b or d == a:
                continue
            delta = D[a, c] + D[b, d] - D[a, b] - D[c, d]
            if delta < -1e-6:
                lo, hi = sorted(((i + 1) % n, j))
                if (i + 1) % n > j:                 # keep the reversed segment the inner one
                    lo, hi = j + 1, i
                order[lo:hi + 1] = order[lo:hi + 1][::-1]
                pos = {c: k for k, c in enumerate(order)}
                improved = True
                break
    return improved


def _or_opt(order: list[int], D: np.ndarray) -> bool:
    """Move a run of 1-3 points to its cheapest other position. True if anything improved."""
    n = len(order)
    for seg in (1, 2, 3):
        for i in range(n):
            if seg >= n - 2:
                break
            idx = [(i + k) % n for k in range(seg)]
            run = [order[k] for k in idx]
            p, q = order[(i - 1) % n], order[(i + seg) % n]
            gain = D[p, run[0]] + D[run[-1], q] - D[p, q]
            rest = [c for c in order if c not in set(run)]
            best, at, rev = 1e-6, None, False
            for k in range(len(rest)):
                u, v = rest[k], rest[(k + 1) % len(rest)]
                for r, (s, e) in ((False, (run[0], run[-1])), (True, (run[-1], run[0]))):
                    cost = D[u, s] + D[e, v] - D[u, v]
                    if gain - cost > best:
                        best, at, rev = gain - cost, k, r
            if at is not None:
                ins = run[::-1] if rev else run
                order[:] = rest[:at + 1] + ins + rest[at + 1:]
                return True
    return False


def build_tour(nodes: list[dict], neighbours: int = 12, max_rounds: int = 60) -> list[int]:
    pts = np.array([[n["x"], n["y"], n["z"]] for n in nodes], dtype=float)
    D = _dist_matrix(pts)
    near = np.argsort(D, axis=1)[:, 1:neighbours + 1]
    order = _nearest_neighbour(D)
    for _ in range(max_rounds):
        a = _two_opt(order, D, near)
        b = _or_opt(order, D)
        if not (a or b):
            break
    return order


class Route:
    """Runtime side: walk a prebuilt tour, skipping what is already done."""

    def __init__(self, nodes: list[dict], order: list[int]):
        self.nodes, self.order = nodes, order
        self.cursor = None          # index into order
        self.step = 1               # +1 or -1 around the loop

    @classmethod
    def load(cls, path: Path = OUT) -> "Route":
        d = json.loads(path.read_text())
        return cls(d["nodes"], d["order"])

    @staticmethod
    def _d(a, b) -> float:
        return math.dist((a["x"], a["y"], a["z"]), b)

    def start(self, me, done: set[str]) -> None:
        live = [i for i, k in enumerate(self.order) if self.nodes[k]["id"] not in done]
        if not live or me is None:
            self.cursor = None
            return
        self.cursor = min(live, key=lambda i: self._d(self.nodes[self.order[i]], me))
        n = len(self.order)
        ahead = [self._d(self.nodes[self.order[(self.cursor + s * k) % n]], me)
                 for s in (1, -1) for k in (1,)]
        self.step = 1 if ahead[0] <= ahead[1] else -1

    def upcoming(self, done: set[str], k: int = 6) -> list[dict]:
        """The next k unvisited points from the cursor, advancing the cursor past done ones."""
        if self.cursor is None:
            return []
        n, out, i = len(self.order), [], self.cursor
        for _ in range(n):
            node = self.nodes[self.order[i]]
            if node["id"] not in done:
                if not out:
                    self.cursor = i
                out.append(node)
                if len(out) == k:
                    break
            i = (i + self.step) % n
        return out

    def remaining(self, done: set[str]) -> int:
        return sum(1 for n in self.nodes if n["id"] not in done)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--seals", action="store_true", help="also route the 60 Seal portals")
    g.add_argument("--all", action="store_true", help="route every portal kind")
    a = ap.parse_args()
    kinds = DEFAULT_KINDS + (("seal",) if a.seals else ()) + \
        (("seal", "portal") if a.all else ())
    nodes = load_nodes(kinds)
    pts = np.array([[n["x"], n["y"], n["z"]] for n in nodes], dtype=float)
    D = _dist_matrix(pts)
    nn = _nearest_neighbour(D)
    order = build_tour(nodes)
    assert sorted(order) == list(range(len(nodes))), "tour must visit every point exactly once"
    OUT.write_text(json.dumps({"kinds": list(kinds), "nodes": nodes, "order": order}))
    print(f"{len(nodes)} points ({sum(n['kind'] == 'feather' for n in nodes)} feathers) · "
          f"nearest-neighbour {tour_length(nn, D) / 1e5:.1f} km -> optimised "
          f"{tour_length(order, D) / 1e5:.1f} km -> {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
