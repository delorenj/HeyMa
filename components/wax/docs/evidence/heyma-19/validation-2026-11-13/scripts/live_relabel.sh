#!/bin/bash
# usage: live_relabel.sh <label> [extra VAR=value ...]    drives the real TUI (no --no-sync) in tmux against fixture real3
cd /tmp/heyma-validate-heyma-19
label=$1; shift
source scripts/env_real3.sh
ENVARGS+=("$@")
PROD=("/home/delorenj/d/Transcripts/20260713-143714-2026-07-13 17.37.15.md" "/home/delorenj/d/Voices/voice-6e760e.md")
echo "--- production files that must stay untouched (sha256 prefix, mtime)"
for f in "${PROD[@]}"; do echo "$(sha256sum "$f" | cut -c1-16)  $(stat -c '%y' "$f")  $f"; done
echo "--- extra env for this scenario: $*"
WAX=/home/delorenj/HeyMa/.worktrees/hm19-tui-validate/components/wax/bin/wax
T="/usr/bin/tmux -L hm19live"
whole() { $T capture-pane -p -t w | sed -e 's/[[:space:]]*$//' | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}'; }
shot() { printf '\n===== %s =====\n' "$1"; whole | grep -E "${2:-.}"; }
$T kill-server 2>/dev/null; rm -f x/exit-live.txt
$T new-session -d -s w -x 110 -y 30 "env -i ${ENVARGS[*]} /usr/bin/python3 $WAX voices tui --no-play; echo EXIT=\$? > x/exit-live.txt; sleep 120"
sleep 3
shot "1. open: cursor row 0 should be voice-6e760e" '^>|voices,|NAME>'
$T capture-pane -p -t w | grep -qE "^>  voice-6e760e" || { echo "ABORT: cursor is not on voice-6e760e"; $T kill-server; exit 3; }
$T send-keys -t w "Validation Voice"; sleep 0.5
$T send-keys -t w Enter; sleep 1.2
shot "2. Enter: name locked in; relabel runs in the background" 'voice-6e760e|relabel|is Validation|NAME>'
for i in $(seq 30); do sleep 2; L=$($T capture-pane -p -t w | grep -E "relabelling" | head -1 | sed 's/[[:space:]]*$//'); echo "t+$((i*2))s: ${L:-<no relabel progress on screen>}"; [ -z "$L" ] && break; done
echo; echo "===== 3. the whole screen after the relabel finished, still inside the TUI ====="; whole
$T send-keys -t w C-c; sleep 4
echo; echo "===== 4. after Ctrl+C: what the terminal shows (summary, held-back messages) ====="; whole | tail -25
echo; cat x/exit-live.txt
