#!/usr/bin/env bash
# aesteria_wrap.sh — the ONE thing pasted into AION 2's Steam launch options.
#
#   Steam -> AION 2 Playtest -> Properties -> Launch Options:
#       /path/to/aesteria/tools/aesteria_wrap.sh %command%
#
# Pasting it changes NOTHING by itself: the wrapper is a pass-through until a mode is set.
# The mode can be set either way, and BOTH work — pick whichever suits the moment:
#
#   in the launch options, applied to that launch and remembered:
#       .../aesteria_wrap.sh --mode overlay %command%
#
#   or from a terminal, so changing it never means editing Steam settings mid-window:
#
#       aesteria_wrap.sh --mode off            # pass-through (default)
#       aesteria_wrap.sh --mode overlay        # Vulkan layer only
#       aesteria_wrap.sh --mode probe          # LD_PRELOAD probe, passive (identify only)
#       aesteria_wrap.sh --mode probe-thread   # probe + a heartbeat thread of our own
#       aesteria_wrap.sh --mode both           # overlay + probe-thread
#       aesteria_wrap.sh --show                # what the next launch will do
#
# ESCALATE ONE NOTCH PER LAUNCH. There is no throwaway account for this
# game, so a real account takes every test. Two changes in one launch means a bad result cannot be attributed to either.
#
# Order that keeps every result interpretable:
#   1. `make -C src/inject/vk_overlay smoke`  — layer draws over vkcube (positive control)
#   2. `make -C src/inject selftest`          — probe loads + threads on a local process
#   3. --mode overlay        -> reach the world. Layer tolerated?
#   4. --mode probe          -> reach the world. A foreign .so tolerated?
#   5. --mode probe-thread   -> reach the world. Our own thread tolerated?
#   6. --mode both
# Steps 1-2 are already green; they cost nothing and are what make a later failure mean
# "the game rejected it" rather than "our code was broken".

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OVERLAY_DIR="$REPO/src/inject/vk_overlay"
PROBE_SO="$REPO/src/inject/libaesteria_probe.so"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/aesteria"
MODE_FILE="$CONF_DIR/mode"
LAUNCH_LOG="/tmp/aesteria-launch.log"

read_mode() { [[ -r "$MODE_FILE" ]] && tr -d '[:space:]' < "$MODE_FILE" || echo off; }

MODE_FROM_ARGV=""

case "${1:-}" in
  --mode)
    want="${2:-}"
    case "$want" in
      off|overlay|probe|probe-thread|both) ;;
      *) echo "usage: $0 --mode {off|overlay|probe|probe-thread|both} [command...]" >&2; exit 2 ;;
    esac
    mkdir -p "$CONF_DIR"
    printf '%s\n' "$want" > "$MODE_FILE"
    shift 2
    # ⚠ `--mode X` must work BOTH ways, because putting it straight into Steam's launch
    # options is the obvious way to write it:
    #     aesteria_wrap.sh --mode overlay %command%
    # This used to set the mode and `exit 0`, so Steam's process appeared and vanished in
    # one second and the game never started — indistinguishable from "the overlay broke the
    # client", which is the worst possible way for a launch to fail while you are escalating
    # one notch at a time. With arguments following, set the mode and CARRY ON into them.
    if [[ $# -eq 0 ]]; then
      echo "aesteria: next launch mode = $want"
      exit 0
    fi
    MODE_FROM_ARGV="$want"
    ;;
  --show)
    echo "mode:        $(read_mode)"
    echo "mode file:   $MODE_FILE"
    echo "overlay .so: $([[ -f "$OVERLAY_DIR/libaesteria_overlay.so" ]] && echo present || echo MISSING)"
    echo "probe .so:   $([[ -f "$PROBE_SO" ]] && echo present || echo MISSING)"
    echo "layer manifest registered: $(
        [[ -e "$HOME/.local/share/vulkan/implicit_layer.d/aesteria_overlay.json" ]] \
          && echo yes || echo "no (make -C src/inject/vk_overlay install)")"
    exit 0 ;;
  -h|--help)
    sed -n '2,30p' "$0"; exit 0 ;;
esac

MODE="$(read_mode)"

# A launch with no command is a mistake worth catching loudly rather than silently doing
# nothing: it means the launch options lost %command%.
if [[ $# -eq 0 ]]; then
  echo "aesteria_wrap.sh: no command given. Steam launch options must end with %command%" >&2
  exit 2
fi

case "$MODE" in
  overlay|both)
    # VK_ADD_IMPLICIT_LAYER_PATH *adds* a search path; VK_LAYER_PATH would REPLACE the
    # default one and hide gamescope's WSI layer, MangoHud and the Steam overlay along with
    # it. Belt and braces with `make install`, since pressure-vessel imports host implicit
    # layers from the standard dir but is not obliged to honour an added path.
    export VK_ADD_IMPLICIT_LAYER_PATH="$OVERLAY_DIR${VK_ADD_IMPLICIT_LAYER_PATH:+:$VK_ADD_IMPLICIT_LAYER_PATH}"
    export AESTERIA_OVERLAY=1
    ;;
esac

case "$MODE" in
  probe|probe-thread|both)
    export LD_PRELOAD="${LD_PRELOAD:+$LD_PRELOAD:}$PROBE_SO"
    export AESTERIA_PROBE_MAPS=1
    ;;
esac

case "$MODE" in
  probe-thread|both) export AESTERIA_PROBE_THREAD=1 ;;
esac

# Record what this launch actually had enabled. Attribution after the fact depends on it:
# "the client died at +11 s" is only evidence if you know what was loaded at the time.
{
  echo "--- $(date '+%F %T') launch, mode=$MODE${MODE_FROM_ARGV:+ (from launch options)}"
  echo "    AESTERIA_OVERLAY=${AESTERIA_OVERLAY:-unset} AESTERIA_PROBE_THREAD=${AESTERIA_PROBE_THREAD:-unset}"
  echo "    LD_PRELOAD=${LD_PRELOAD:-unset}"
  echo "    cmd: $*"
} >> "$LAUNCH_LOG"

exec "$@"
