#!/usr/bin/env python3
"""aesteria_hp -- HP, read from the UI the game already draws. Seventh attempt; the first that works.

WHY THE UI (2026-10-01): six attempts hunted the HP *value* (plain ints, floats, ratios, direction
scans) and found nothing usable -- vitals live outside the UE actor and are not stored plainly.
But the game must DISPLAY HP, and UMG widgets are reflected. So read the HUD instead.

OWN HP -- PROVEN LIVE against the operator's screen, 17:07:
    HUD_UserInfo_C (the instance under ...HUD_Layer_C.WidgetTree) ->
        P_Txt_Hp   (+1616, AionTextBlock)  text = "2,943/4,211"   exact current/max
                   text chars: u64(u64(textblock + 0x1b8) + 0x20) -> UTF-16
        P_PBar_Hp  (+1640, AionProgressBar) +0x510 float = 0.6989 = 2943/4211 exactly
    the operator reported 2583/4211 at 17:07:09; 13 s of regen later the text read 2,943 then 3,008.
    ⚠ There are TWO "190_UserInfo" instances. The other one read "3,830/3,830" (stale/other) --
    pick the one whose full name contains "HUD_Layer_C.WidgetTree".
    ⚠ UProgressBar.Percent (+1056) is NOT the live fill on AionProgressBar: it read 1.0 while the operator
    was at 54 %. The AionProgressBar fill is +0x510.

NAMEPLATES (mobs/players) -- PROVEN LIVE 2026-10-01 20:25:
    NamePlate_C (AionWidget_NamePlateChar) -> P_Bar_Hp (+2040, AionProgressBar) -> +0x510
    One mob hit to "50 %" (the operator): plate@b198f560 read 1.0 -> 0.5666 -> 0.5211 -> 0.4634 at the
    SAME timestamps as the target frame (HUD_TargetInfo_C -> P_Prg_HpBar +1520 -> +0x510), over a
    15 s idle baseline in which 40 of 48 bars never moved.
    ⚠ Plates that flip 0.0 <-> 1.0 are hidden/recycled widgets, not HP -- tie a plate to its actor
    before trusting its value:
    ACTOR LINK (SDK-named, 10-01): AionCharacter.NameplateWidgetComponent +0x2810 -> WC_NamePlate_C
    +0x5f0 -> NamePlate_C. Own plate read 0.8235 == HUD 4,149/5,038. Only characters near you carry
    one (42 of 340 loaded in town); friendly town NPCs have no P_Bar_Hp at all.
    Used by `aesteria_overlay.py --hp`.

    python3 tools/aesteria_hp.py [--watch]
"""
from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aesteria_gobjects import GObjects                              # noqa: E402
from aesteria_live import LiveProcess, load_live_meta               # noqa: E402

META = Path(__file__).resolve().parents[1] / "snaps" / "meta"
USERINFO_TXT_HP, USERINFO_PBAR_HP = 1616, 1640
NAMEPLATE_BAR_HP = 2040
AIONPBAR_FILL = 0x510


def f32(live, a):
    return struct.unpack("<f", struct.pack("<I", live.u32(a) or 0))[0]


def text_of(live, textblock) -> str | None:
    p1 = live.u64(textblock + 0x1b8) if textblock else 0
    p2 = live.u64(p1 + 0x20) if p1 else 0
    raw = live.read(p2, 96) if p2 else None
    return raw.decode("utf-16-le", "ignore").split("\x00")[0] if raw else None


def parse_hp(text: str | None):
    try:
        cur, mx = (int(x.replace(",", "")) for x in text.split("/"))
        return cur, mx
    except (AttributeError, ValueError):
        return None


class HP:
    def __init__(self, live, go):
        self.live, self.go = live, go
        self.userinfo, self.plates = None, []
        self.rescan()

    def rescan(self):
        self.go.refresh()
        self.userinfo, self.plates = None, []
        for _i, a in self.go.iter_objects():
            c = self.go.class_name(a) or ""
            if c == "HUD_UserInfo_C" and "HUD_Layer_C.WidgetTree" in self.go.full_name(a):
                self.userinfo = a
            elif c == "NamePlate_C" and not (self.go.object_name(a) or "").startswith("Default__"):
                self.plates.append(a)

    def own(self):
        if not self.userinfo:
            return None
        txt = text_of(self.live, self.live.u64(self.userinfo + USERINFO_TXT_HP))
        bar = self.live.u64(self.userinfo + USERINFO_PBAR_HP)
        return {"text": txt, "hp": parse_hp(txt),
                "fill": f32(self.live, bar + AIONPBAR_FILL) if bar else None}

    def nameplate_fills(self):
        out = []
        for p in self.plates:
            bar = self.live.u64(p + NAMEPLATE_BAR_HP)
            if bar:
                out.append((p, f32(self.live, bar + AIONPBAR_FILL)))
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--watch", action="store_true")
    a = ap.parse_args()
    live = LiveProcess(a.pid)
    with live:
        g, meta = load_live_meta(live, META if META.exists() else None, False)
        hp = HP(live, GObjects(live, meta, g))
        while True:
            o = hp.own()
            fills = [round(v, 3) for _p, v in hp.nameplate_fills() if v == v]
            print(time.strftime("%H:%M:%S"), "you:", o and o["text"],
                  f"(fill {o['fill']:.4f})" if o and o["fill"] is not None else "",
                  "| nameplates:", fills)
            if not a.watch:
                return 0
            time.sleep(1)
            if int(time.time()) % 10 == 0:
                hp.rescan()


if __name__ == "__main__":
    sys.exit(main())
