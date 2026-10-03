#!/usr/bin/env python3
"""aesteria_wingboost -- faster flight, kept inside the server's movement budget. External.

WHAT IT DOES: while you fly fast (> 10 m/s) and roughly straight (< 20 deg turn since the last
check), it moves you 4 m along your live velocity every 0.75 s by writing the root component's
position through /proc/<pid>/mem. No injection.

WHY THOSE NUMBERS -- all measured live, 2026-10-01, the operator in flight at ~16 m/s (aesteria_hop.py):
    single steps: 1/3/5 m stick; 6 m stutters (corrected then re-synced); 7.5/10 m are pulled
        back ~1 s later and flight stamina is ZEROED -- a forced landing.
    repeated 4 m: every 2 s, 1 s, 0.75 s clean over 3-4 hops; every 0.5 s corrected + grounded.
    => the check is a BUDGET over time, and 4 m / 0.75 s (+5.3 m/s, ~33 %) sits inside it.
    First full flight on boost: 10 hops, "it went FAST", no correction.

WRITES: RelativeLocation (root+0x148) AND the cached ComponentToWorld translation (root+0x290),
    confirmed by value while moving. Movement reads the cached copy; writing only the relative
    field would be overwritten.
FLIGHT GATE (the operator, 10-01: "it's dismounting me" -- a ground mount on auto-run is fast AND
    straight, so speed alone let the boost fire on it). CharacterMovement.MovementMode (+545),
    recorded live through ride / foot / take-off / fly / land, twice each:
        mount 11.6 m/s = 1 (Walking) · on foot 5.6 m/s = 1 · take-off and landing = 3 (Falling)
        · wing flight 12-16 m/s = 5 (Flying), and ONLY flight.
    So the boost fires in mode 5 and nowhere else; an unreadable mode means no hop.
DIRECTION: the live velocity, never the root's stored yaw -- that read 19 deg while the operator flew at
    ~98 deg, and two early hops went sideways.
"""
from __future__ import annotations

import math
import os
import struct
import threading

from aesteria_player import read_vector, SCENE_RELATIVE_LOCATION, SCENE_COMPONENT_VELOCITY

C2W_TRANSLATION = 0x290
BOOST_DIST = 400.0          # units (4 m)
BOOST_GAP = 0.75            # seconds
MIN_SPEED = 1000.0          # units/s (10 m/s): only boost real flight
MAX_TURN_DEG = 20.0
CHARACTER_MOVEMENT = 968    # Character.CharacterMovement
MOVEMENT_MODE = 545         # CharacterMovementComponent.MovementMode (byte)
MOVE_FLYING = 5


class WingBoost:
    """Toggle on/off; re-reads the root every tick, so a respawn or zone change is followed."""

    def __init__(self, live, get_root, get_move_comp, dist=BOOST_DIST, gap=BOOST_GAP):
        self.live, self.get_root, self.dist, self.gap = live, get_root, dist, gap
        self.get_move_comp = get_move_comp
        self._stop: threading.Event | None = None
        self.hops = 0
        self.skipped = 0

    @property
    def on(self) -> bool:
        return self._stop is not None

    def toggle(self) -> bool:
        if self.on:
            self._stop.set()
            self._stop = None
        else:
            self._stop = threading.Event()
            self.hops = self.skipped = 0
            threading.Thread(target=self._run, args=(self._stop,), daemon=True).start()
        return self.on

    def _run(self, stop: threading.Event):
        fd = os.open(f"/proc/{self.live.pid}/mem", os.O_RDWR)
        last_u = None
        cos_max = math.cos(math.radians(MAX_TURN_DEG))
        try:
            while not stop.is_set():
                root = self.get_root()
                v = read_vector(self.live, root + SCENE_COMPONENT_VELOCITY) if root else None
                sp = math.sqrt(sum(c * c for c in v)) if v else 0.0
                u = tuple(c / sp for c in v) if sp > 0 else None
                straight = u and last_u and sum(a * b for a, b in zip(u, last_u)) > cos_max
                if sp > MIN_SPEED and straight and self.flying():
                    loc = read_vector(self.live, root + SCENE_RELATIVE_LOCATION)
                    if loc:
                        blob = struct.pack("<3d", *(loc[k] + u[k] * self.dist for k in range(3)))
                        os.pwrite(fd, blob, root + SCENE_RELATIVE_LOCATION)
                        os.pwrite(fd, blob, root + C2W_TRANSLATION)
                        self.hops += 1
                else:
                    self.skipped += 1
                last_u = u
                stop.wait(self.gap)
        finally:
            os.close(fd)

    def flying(self) -> bool:
        cmc = self.get_move_comp()
        raw = self.live.read(cmc + MOVEMENT_MODE, 1) if cmc else None
        return bool(raw) and raw[0] == MOVE_FLYING

    def status(self) -> str:
        return (f"boost ON · {self.hops} hops" if self.on else "boost off (F6)")


def listen_hotkeys(bindings: dict, stop: threading.Event | None = None):
    """Passive evdev read of every keyboard (no grab: keys still reach the game).
    bindings: {evdev keycode: callable}. Blocks; run it in a thread."""
    import select
    import evdev
    devs = [evdev.InputDevice(p) for p in evdev.list_devices()]
    devs = [d for d in devs
            if any(k in d.capabilities().get(evdev.ecodes.EV_KEY, []) for k in bindings)]
    while not (stop and stop.is_set()):
        r, _, _ = select.select(devs, [], [], 0.5)
        for d in r:
            try:
                events = list(d.read())
            except OSError:
                continue
            for ev in events:
                if ev.type == evdev.ecodes.EV_KEY and ev.value == 1 and ev.code in bindings:
                    bindings[ev.code]()
