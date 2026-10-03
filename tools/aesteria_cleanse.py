#!/usr/bin/env python3
"""aesteria_cleanse -- press the Healing Potion (F2) when a status effect lands. External.

the operator, 10-02: "automatically uses Healing Potion (F2) which removes 1 status effect, when I have a
status effect. I don't have enough capacity to see status effects on top of everything ... currently
I'm not using my Healing Potions at all."

READ (the UI, like aesteria_hp.py -- the game draws it, so the game must know it):
    HUD_Layer_C.WidgetTree.190_UserInfo.WidgetTree.P_AbnormalListDebuff.WidgetTree.Slot_AbnormalIcon_C
    one icon widget per debuff on YOU; UWidget.Visibility (+228) != 1 (Collapsed) = showing.
    Found live 10-02 (1 debuff icon, 4 buff icons under P_AbnormalListBuff). WHICH debuff an icon is
    stays unknown (its brush is a MaterialInstanceDynamic), so every showing icon counts.
    UNPROVEN as a trigger until a live debuff is watched coming and going.

ACT: a real key press through /dev/uinput (the operator has rw on it), so the game sees ordinary input.
    Only while the game window has focus (xprop _NET_ACTIVE_WINDOW -> WM_CLASS) -- a press with
    Telegram focused would type into Telegram.

NOT SPAMMING A POTION ON SOMETHING IT CANNOT REMOVE: after a press, if the debuff count has not
    dropped within VERIFY s, the press is retried once after the cooldown; if that also changes
    nothing, the current count is parked as "stuck" and no press happens until the count changes
    (a NEW debuff arrives, or one expires). Resurrection sickness and the like stay parked.
"""
from __future__ import annotations

import random
import subprocess
import threading
import time

VISIBILITY, COLLAPSED = 228, 1
DEFAULT_COOLDOWN = 12.0      # s between presses; the potion's real cooldown is not measured yet
VERIFY = 2.0                 # s after a press to judge whether it removed something
GAME_CLASSES = ("aion2.exe", "steam_app_")


def game_focused() -> bool:
    """True when the X11 (XWayland) active window is the game. Wayland-native windows (Konsole,
    Telegram) leave _NET_ACTIVE_WINDOW at 0x0, which correctly reads as not focused."""
    try:
        aw = subprocess.run(["xprop", "-root", "_NET_ACTIVE_WINDOW"], capture_output=True,
                            text=True, timeout=1).stdout.split()[-1]
        if aw in ("0x0", "0"):
            return False
        cls = subprocess.run(["xprop", "-id", aw, "WM_CLASS"], capture_output=True, text=True,
                             timeout=1).stdout.lower()
        return any(c in cls for c in GAME_CLASSES)
    except (OSError, subprocess.SubprocessError, IndexError):
        return False


class Cleanser:
    """get_icons() -> current debuff icon widget addresses (from the overlay's discovery pass).
    clock / focused / press are injectable so the state machine is testable offline."""

    def __init__(self, live, get_icons, cooldown: float = DEFAULT_COOLDOWN, dry_run: bool = False,
                 clock=time.monotonic, focused=game_focused, press=None):
        self.live, self.get_icons, self.cooldown, self.dry = live, get_icons, cooldown, dry_run
        self.clock, self.focused = clock, focused
        self._press_fn = press or self._uinput_press
        self.on = True
        self.count = 0
        self.presses = 0
        self.last_press = None      # clock() of the last press
        self.at_press = 0           # debuff count when we pressed
        self.judged = True          # has the last press been judged yet
        self.fails = 0              # presses in a row that removed nothing
        self.stuck = None           # a count we have given up on until it changes
        self._kb = None

    def debuffs(self) -> int:
        n = 0
        for a in self.get_icons():
            raw = self.live.read(a + VISIBILITY, 1)
            if raw and raw[0] != COLLAPSED:
                n += 1
        return n

    def _uinput_press(self) -> None:
        if self.dry:
            print(f"cleanse: (dry run) would press F2 -- {self.count} debuff(s)")
            return
        import evdev
        from evdev import ecodes as e
        if self._kb is None:
            self._kb = evdev.UInput({e.EV_KEY: [e.KEY_F2]}, name="aesteria-cleanse")
            time.sleep(0.3)                  # let the compositor pick the new device up
        self._kb.write(e.EV_KEY, e.KEY_F2, 1)
        self._kb.syn()
        time.sleep(random.uniform(0.05, 0.09))
        self._kb.write(e.EV_KEY, e.KEY_F2, 0)
        self._kb.syn()

    def tick(self) -> None:
        n = self.count = self.debuffs()
        now = self.clock()
        if self.stuck is not None and n != self.stuck:
            self.stuck, self.fails = None, 0          # something changed: worth trying again
        if n == 0:
            self.fails = 0
        if self.last_press is not None and not self.judged and now - self.last_press >= VERIFY:
            self.judged = True                         # did the last potion remove anything?
            self.fails = 0 if n < self.at_press else self.fails + 1
            if self.fails >= 2:
                self.stuck = n                         # two potions, nothing removed: leave it
        if not self.on or n == 0 or self.stuck is not None or not self.judged:
            return
        if self.last_press is not None and now - self.last_press < self.cooldown:
            return
        if not self.focused():
            return
        self.at_press, self.judged = n, False
        self.last_press = now
        self.presses += 1
        self._press_fn()

    def run(self, stop: threading.Event | None = None, period: float = 0.25) -> None:
        while not (stop and stop.is_set()):
            try:
                self.tick()
            except Exception as ex:          # a torn read must not kill the thread
                print("cleanse:", ex)
            time.sleep(period)

    def status(self) -> str:
        if not self.on:
            return "cleanse off (ScrLk)"
        s = f"cleanse ON · {self.count} debuff"
        if self.stuck is not None:
            s += " (not removable)"
        return s + f" · {self.presses} potions"
