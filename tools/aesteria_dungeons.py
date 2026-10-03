#!/usr/bin/env python3
"""aesteria_dungeons -- every dungeon / portal entrance in Altgard (World_D_A), from the paks.

the operator, 10-02: "add dungeon entrances to the ESP". Entrances are spawn cells of the same shape as
the feathers (aesteria_godfragments.py): one small World Partition cell per entrance, the spawn
name in its name table, the position anchored from the END of the cell. 103 found on 10-02.

KINDS (from the spawn name; what each one IS in-game is partly a guess -- labelled as such):
    party   E_PD_<Name>_EnterPortal         Draupnir, Fire Temple, Krao Cave, Urugugu Canyon --
                                            the party dungeons the guides name
    solo    E_Dungeon_D_00NN_EnterPortal    numbered dungeons (asset paths: BG/Dungeon/Quest/...)
    seal    E_Seal_Altgard_NNN_EnterPortal  60 "Undiscovered Area" portals (the operator, 10-02, standing at
                                            seal 17: in-game text "Undiscovered Area", effect
                                            Portal_Space_01 -- position confirmed on that one)
    seal+   E_SealAdvanced_*                the one advanced seal
    keep    E_Daeva_Garrision_*_INS_*       the garrison instance
    kibelisk  EnvObj_TeleportArtifact_Altgard_<Place> -- the teleport artifacts (the operator, 10-02:
            "Kibelisk"); the asset set has _Available and _Available_Engraved states, so a
            loaded one shows whether it is activated, but the far-away list is not read yet
    event   E_D1_<Daeva|Dragon|Human>NN_Entrance -- 14 instanced events (the operator, 10-02: "Human02,
            it's the same as the Daeva one")
    portal  everything else (E_D1_FQ_EnterPortal = field-quest portals, Morheim, Fafnite
            Storage) -- UNKNOWN, drawn so the operator can tell us

POSITIONS: "end"-anchored on 96 of 103 (the method proven 4/4 on collected feathers); 7 fall back
to a forward scan ("how": "scan") and are UNVERIFIED.

    python3 tools/aesteria_dungeons.py        # -> snaps/dungeons_world_d_a.json
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aesteria_godfragments import CONTAINERS, KEY, first_triple, position  # noqa: E402
from aesteria_iostore import PAKS, extract, open_container                  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "snaps" / "dungeons_world_d_a.json"
NAME_RE = re.compile(rb"\x00(E_[A-Za-z0-9_]*?(?:EnterPortal|Entrance)[A-Za-z0-9_]*)\x00")
KIBELISK_RE = re.compile(rb"\x00EnvObj_TeleportArtifact_([A-Za-z]+)_([A-Za-z0-9]+)\x00")


def classify(name: str) -> tuple[str, str]:
    """(kind, short label) from a spawn name."""
    if m := re.match(r"EnvObj_TeleportArtifact_[A-Za-z]+_(\w+)", name):
        return "kibelisk", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", m.group(1))
    if m := re.match(r"E_PD_(.+?)_EnterPortal", name):
        return "party", m.group(1).replace("_", " ").replace("Caynon", "Canyon")
    if m := re.match(r"E_Dungeon_D_0*(\d+)_EnterPortal", name):
        return "solo", f"dungeon {m.group(1)}"
    if m := re.match(r"E_SealAdvanced_", name):
        return "seal+", "advanced seal"
    if m := re.match(r"E_Seal_Altgard_0*(\d+)_", name):
        return "seal", f"undiscovered area {m.group(1)}"
    if "Garrision" in name:
        return "keep", "Daeva garrison"
    if m := re.match(r"E_D1_(Daeva|Dragon|Human)(\d+)_Entrance", name):
        return "event", f"{m.group(1)} {int(m.group(2))} event"
    # the instance suffix is the identity only when the stem has no number of its own:
    # E_D1_FQ_EnterPortal_07 -> "FQ 7", E_D1_Dragon05_Entrance_01 -> "Dragon05"
    stem, _, inst = re.sub(r"^E_(D1_)?", "", name).partition("_Ent")
    inst = re.sub(r"^\D*_?", "", inst).lstrip("0") or "1"
    stem = stem.replace("EnterPortal", "").strip("_").replace("_", " ")
    return "portal", stem if re.search(r"\d", stem) else f"{stem} {inst}"


def main() -> int:
    key = bytes.fromhex(KEY.read_text().strip()[2:])
    found = []
    for cname in CONTAINERS:
        c = open_container(PAKS / cname, key)
        for f in c["files"]:
            if not f.endswith(".umap") or c["offlen"][c["files"][f]][1] > 65536:
                continue
            d = extract(c, f)
            m = NAME_RE.search(d) or KIBELISK_RE.search(d)
            if not m:
                continue
            p, how = position(d), "end"
            if not p:
                p, how = first_triple(d), "scan"
            if not p:
                continue
            name = m.group(0).strip(b"\x00").decode() if m.re is KIBELISK_RE else m.group(1).decode()
            kind, label = classify(name)
            found.append({"name": name, "kind": kind, "label": label, "how": how,
                          "x": p[1], "y": p[2], "z": p[3], "cell": Path(f).stem})
    found.sort(key=lambda e: (e["kind"], e["name"]))
    OUT.write_text(json.dumps(found, indent=0))
    print(f"{len(found)} entrances -> {OUT.name}")
    print("kinds:", dict(Counter(e["kind"] for e in found)))
    print("unverified (scan) positions:", sum(e["how"] == "scan" for e in found))
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())
