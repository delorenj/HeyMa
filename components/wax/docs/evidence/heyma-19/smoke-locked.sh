#!/bin/bash
# HEYMA-19 fix-stage evidence: the real curses UI while another process holds the voice registry lock
# (what a voice-id pass does across its LLM call). A merge must not freeze the screen or Ctrl+C.
set -u
S=${1:-/tmp/heyma-19-scratch-locked}
HERE=$(cd "$(dirname "$0")" && pwd)
WAX=$(cd "$HERE/../../.." && pwd)/bin/wax
T="tmux -L heyma19b"
log() { printf '\n===== %s =====\n' "$*"; }
shot() { log "$1"; $T capture-pane -p -t w | sed -e 's/[[:space:]]*$//' | grep -v '^$'; }
rm -rf "$S"; /usr/bin/python3 "$HERE/make_fixture.py" "$S" >/dev/null || exit 1
$T kill-server 2>/dev/null
ENVV="env -i HOME=$HOME PATH=$PATH TERM=xterm-256color WAX_ROOT=$S/root WAX_VAULT=$S/vault TRANSCRIPTS=$S/vault WAX_VOICES_DIR=$S/voices WAX_VOICE_NOTIFY=0 WAX_VOICES_PLAYER=$S/fake-player.sh"
# the lock holder: flock the registry lock for 8 s, like a voice-id pass mid-LLM-call
LOCK="$S/root/var/voices.lock"
( flock -x 9; sleep 8 ) 9>"$LOCK" &
sleep 0.5
$T new-session -d -s w -x 100 -y 22 "$ENVV /usr/bin/python3 $WAX voices tui --no-sync --no-play; echo EXIT=\$? > $S/exit.txt; sleep 30"
sleep 1.5
$T send-keys -t w Tab; sleep 0.3
$T send-keys -t w M-m; sleep 0.5
$T send-keys -t w Enter; sleep 0.4
$T send-keys -t w y; sleep 0.5
shot "1. merge confirmed while the registry lock is held: screen is alive and says it is waiting"
$T send-keys -t w Down; sleep 0.4
shot "2. a key while waiting: ignored with the reason on the status line (screen still redraws)"
$T send-keys -t w C-c
for _ in $(seq 1 30); do $T capture-pane -p -t w | grep -q 'letting it land' && break; sleep 0.1; done
shot "3. Ctrl+C while the lock is still held: the screen closes, the process says it is letting the merge land"
wait
sleep 1.5
log "4. lock released after 8 s: the merge landed and the process exited"; cat "$S/exit.txt"
env -i HOME=$HOME PATH=$PATH WAX_ROOT=$S/root WAX_VAULT=$S/vault TRANSCRIPTS=$S/vault WAX_VOICES_DIR=$S/voices /usr/bin/python3 $WAX voices list
$T kill-server 2>/dev/null
