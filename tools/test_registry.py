#!/usr/bin/env python3
"""Integration test for aesteria_registry against a banked capture.

⚠ THIS IS DELIBERATELY NOT A UNIT TEST WITH MOCKS.
    The thing worth trusting is the whole read path -- GNames resolution,
    GUObjectArray membership, ClassPrivate deref, PropertiesSize -- against a real
    process image. A mock would assert that our own constants agree with themselves,
    which is how the SDK extraction shipped a confident wrong 528-property dump
    and how a glibc-shaped fixture once hid a guard no real
    client could pass. So this runs on 8 GB of real
    memory and is skipped, loudly, when the captures are absent.

Run:  ./test_registry.py                  # uses captures/07-combat
      ./test_registry.py --capture <dir>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import aesteria_registry as reg                                   # noqa: E402
from aesteria_snapshot_reader import Snapshot                     # noqa: E402
from aesteria_gobjects import GObjects                            # noqa: E402
import aesteria_player as player                                  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
DEFAULT_CAPTURE = REPO / "captures" / "07-combat"
DEFAULT_META = REPO / "captures" / "01-tutorial-in-world"

# Hand-derived on build AION2/1.0.21.0.2026031801 in the 2026-09-19 playtest window.
# The tool never reads these; they exist only to prove it reproduces them blind.
KNOWN = {
    56: "AionObjectManager", 64: "PlayerActionSystem", 80: "ItemSystem",
    88: "SkillSystem", 96: "PartySystem", 120: "QuestSystem",
    192: "TargetingSystem", 200: "LootingSystem", 216: "GatherSystem",
    224: "QuickSlotSystem", 240: "DestinationSystem", 248: "ItemAutoUseSystem",
    352: "DamageMeter",
}
KNOWN_SESSION_SIZE = 38032
EXPECTED_FIELDS = 82

fails: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}" + (f" -- {detail}" if detail else ""))
        fails.append(label)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", type=Path, default=DEFAULT_CAPTURE)
    ap.add_argument("--meta-from", type=Path, default=DEFAULT_META)
    args = ap.parse_args()

    if not (args.capture / "manifest.json").exists():
        print(f"SKIPPED: no capture at {args.capture}")
        print("  This test needs a real process image; it cannot be mocked "
              "meaningfully. Nothing was verified.")
        return 0

    print(f"capture {args.capture.name}")
    with Snapshot(args.capture) as snap:
        g, meta = player.load_meta(snap, args.capture, args.meta_from, False)
        go = GObjects(snap, meta, g)

        rows, stats = reg.build(snap, go, ("AionCharSession",), 0, False)
        a = stats["anchors"]["AionCharSession"]

        check(a["size"] == KNOWN_SESSION_SIZE, "PropertiesSize is exact",
              f"got {a['size']}, want {KNOWN_SESSION_SIZE}")
        check(a["fields"] == EXPECTED_FIELDS, f"{EXPECTED_FIELDS} labelled fields",
              f"got {a['fields']}")

        by_off = {r["offset"]: r["class"] for r in rows}
        hits = sum(1 for off, want in KNOWN.items()
                   if want.lower() in by_off.get(off, "").lower())
        check(hits == len(KNOWN), f"{len(KNOWN)}/{len(KNOWN)} known offsets blind",
              f"got {hits}")

        # The self-reference gate must FAIL on a wrong anchor, or it proves nothing.
        known_addrs = reg.registered_addresses(go)
        try:
            reg.check_self_reference(snap, go, a["addr"], "SkillSystem", known_addrs)
            check(False, "gate rejects a mismatched anchor name",
                  "it accepted AionCharSession as SkillSystem")
        except reg.RegistryError:
            check(True, "gate rejects a mismatched anchor name")

        # And it must fail on memory that is not an object at all.
        try:
            reg.check_self_reference(snap, go, a["addr"] + 8, "AionCharSession",
                                     known_addrs)
            check(False, "gate rejects a shifted address")
        except reg.RegistryError:
            check(True, "gate rejects a shifted address")

        # MIN_FIELDS must be a real tripwire, not decoration: a tiny subsystem used
        # as a depth-0 anchor should be refused rather than reported as a thin win.
        tiny = next((r["class"] for r in rows if r["class"] == "TargetingSystem"),
                    None)
        if tiny:
            try:
                reg.build(snap, go, (tiny,), 0, False)
                check(False, "MIN_FIELDS refuses a thin anchor",
                      f"{tiny} was accepted at depth 0")
            except reg.RegistryError:
                check(True, "MIN_FIELDS refuses a thin anchor")

    print(f"\n{'FAILED' if fails else 'PASSED'}: "
          f"{len(fails)} failure(s)" if fails else "\nPASSED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
