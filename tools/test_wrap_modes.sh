#!/usr/bin/env bash
# Regression test for aesteria_wrap.sh's argument handling.
#
# EXISTS BECAUSE OF A REAL FAILURE (2026-09-19): `--mode` was config-only, so putting it in
# Steam's launch options —
#     aesteria_wrap.sh --mode overlay %command%
# — set the mode and exited 0 without ever starting the game. Steam's process appeared and
# vanished in one second, which is indistinguishable from "the overlay killed the client":
# precisely the wrong signal to get while escalating one notch at a time against an account
# that cannot be replaced. The launch options are a command line like any other; anything
# accepted there must still run the command.

set -uo pipefail
WRAP="$(dirname "$(readlink -f "$0")")/aesteria_wrap.sh"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/aesteria/mode"
SAVED="$(cat "$CONF" 2>/dev/null || echo off)"
fails=0

check() { # name expected actual
  if [[ "$2" == "$3" ]]; then printf '  ok   %s\n' "$1"
  else printf '  FAIL %s\n       expected: %s\n       actual:   %s\n' "$1" "$2" "$3"; fails=$((fails+1)); fi
}

env_for() { "$WRAP" "$@" /usr/bin/env 2>/dev/null | grep -E '^(AESTERIA_OVERLAY|AESTERIA_PROBE_THREAD)=' | sort | tr '\n' ' '; }

echo "aesteria_wrap.sh argument handling"

"$WRAP" --mode off >/dev/null
check "mode=off launches with nothing set" "" "$(env_for)"

"$WRAP" --mode overlay >/dev/null
check "mode from file is applied" "AESTERIA_OVERLAY=1 " "$(env_for)"

"$WRAP" --mode off >/dev/null
check "--mode inline still launches, and applies" "AESTERIA_OVERLAY=1 " "$(env_for --mode overlay)"

check "--mode inline persists for the next launch" "overlay" "$(cat "$CONF")"

"$WRAP" --mode off >/dev/null
check "--mode both inline" "AESTERIA_OVERLAY=1 AESTERIA_PROBE_THREAD=1 " "$(env_for --mode both)"

"$WRAP" --mode bogus /usr/bin/env >/dev/null 2>&1
check "a bad mode is rejected, not launched" "2" "$?"

"$WRAP" >/dev/null 2>&1
check "no command at all is an error (lost %command%)" "2" "$?"

printf '%s\n' "$SAVED" > "$CONF"
if (( fails )); then echo "$fails failure(s)"; exit 1; fi
echo "all checks passed"
