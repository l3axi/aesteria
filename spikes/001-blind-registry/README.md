# SPIKE 001 — blind labelling of non-reflected C++ objects

## Verdict: VALIDATED

**Question:** AION 2's gameplay systems have zero reflected properties, so every
offset we own for them was derived by hand in the single 6-hour playtest window.
Those numbers are build-specific and will not survive the launch build, and there
is no throwaway account to re-derive them on. Can a pointer field on such an
object be named **blind** — no ground truth, no live window — by asking
GUObjectArray what class the pointee is?

**Evidence**

```
$ ./blind_registry.py --capture ../../captures/07-combat \
      --meta-from ../../captures/01-tutorial-in-world --expect

anchor  AionCharSession @ 0x00009a0d0000
        386360 registered objects in GUObjectArray
        82 labelled pointer fields (tier A 82, tier B 0) in 0x10000 bytes

  +32     A  AionGameInstance      +56     A  AionObjectManager
  +40     A  GameWorldSystem       +64     A  PlayerActionSystem
  +48     A  AionGamePlaySystem    +88     A  SkillSystem
  ...                              +672    A  RoleVoteSystem

SCORING against 13 hand-derived offsets (algorithm never reads these):
  MATCH  +56    AionObjectManager    ... MATCH  +352   DamageMeter
  13/13 reproduced blind
```

Three runs:

| Run | Result |
|---|---|
| `07-combat`, reusing playtest meta | **13/13**, all tier A |
| `05-no-target` (different game state) | **13/13** — offsets are state-stable |
| `07-combat`, `--meta-from` an **empty dir** → both roots rescanned from the capture itself | **13/13 in 72 s, zero carried constants** |
| **negative control:** `--anchor AionPlayerController` | **0/13**, despite 186 labelled pointers there |

The third row is the launch-day claim. `load_meta` falls back to
`find_gnames`/`find_gobjects` when no JSON exists, and both of those are pure
scans — so pointing `--meta-from` at an empty directory proves the whole chain
needs **nothing** from the old build. The negative control proves 13/13 is
structure being read, not the scoring manufacturing matches.

**What worked beyond the question asked:** the hand work found 18 subsystems. This
finds **82**, contiguous from +32 to +672 — the registry is a dense pointer table.
Among the ones nobody had catalogued: `LevelUpSystem`, `ResurrectionSystem`,
`DungeonSystem`, `DuelSystem`, `KiskSystem`, `VehicleSystem`, `TradeSystem`,
`ForceSystem`, `GuildSystem`, `DaevanionSystem`.

**What surprised us — the gate is the whole result.** Two tiers were reported on
the first run to find out which one the known answers land in:

- **A** — pointee address present in GUObjectArray
- **B** — pointee merely passes `valid_uobject`'s UObject shape gate

All 13 landed in tier A. All 17 tier-B hits resolved to **no class name at all**,
with UTF-16 mojibake object names from reading the FName pool as if it were a
name. So `valid_uobject` on its own is *not* a sufficient gate — **array
membership is the entire signal.** Tier B is now off by default. This is the same
shape as `aesteria-validation-gates`: a check that passes on structurally trivial
input manufactures confidence rather than earning it.

## Scope — what this does NOT cover

The method labels **pointer** fields. It cannot see scalars, so these remain
hand-derived and build-specific, and still need live re-derivation:

- `AionCharSession +0x0eb8` has_target (u64 bool), `+0x0ec0` target id (u32)
- the `SkillSystem +0x598/+0x5a0` and pawn `+0x2e88/+0x2e90` mirrors
- character actor's own object id at `+0x26a0`
- HP, which is unsolved by any method

It also assumes the anchor **class name** `AionCharSession` is unchanged. That is
content, not layout, so it is likely stable across a version bump — but it is an
assumption, not a measurement, until the launch build is in front of us.

## Recommendation: ship it, as the first step of launch-day bring-up

Promote to `tools/aesteria_registry.py`, rewritten normally (this is spike code),
emitting a TSV the framework layer reads instead of carrying constants in source.
Run order on the launch build: rescan GNames/GUObjectArray → blind registry →
then spend the live window on the scalars and HP, which are the only things that
actually need the operator in-world.

Do not merge this file as-is.
