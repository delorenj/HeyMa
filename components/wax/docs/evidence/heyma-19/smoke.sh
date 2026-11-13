#!/bin/bash
# Drive the REAL curses `wax voices tui` inside a private tmux server (-L heyma19) against the scratch
# registry from make_fixture.py, with a fake player. Prints each screen and the evidence between keys.
set -u
S=${1:-/tmp/heyma-19-scratch}
WAX=$(cd "$(dirname "$0")/../../.." && pwd)/bin/wax
T="tmux -L heyma19"
log() { printf '\n===== %s =====\n' "$*"; }
shot() { log "$1"; $T capture-pane -p -t w | sed -e 's/[[:space:]]*$//' | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}'; }
playlog() { printf -- '--- player.log (clip files the fake player was given, in order)\n'; awk '{n=split($3,a,"/"); print $1, "pid="$2, a[n]}' "$S/player.log" 2>/dev/null; }
rm -f "$S/player.log"; $T kill-server 2>/dev/null
ENVV="env -i HOME=$HOME PATH=$PATH TERM=xterm-256color WAX_ROOT=$S/root WAX_VAULT=$S/vault TRANSCRIPTS=$S/vault WAX_VOICES_DIR=$S/voices WAX_VOICE_NOTIFY=0 WAX_VOICES_PLAYER=$S/fake-player.sh"
$T new-session -d -s w -x 100 -y 22 "$ENVV /usr/bin/python3 $WAX voices tui --no-sync; echo EXIT=\$? > $S/exit.txt; sleep 30"
sleep 1.5
shot "1. open: unnamed voice-a00001 highlighted, NAME field focused, clips cycling"
sleep 8
playlog
log "   (player processes alive now: $(pgrep -fc '^sleep 3.737$'))"
$T send-keys -t w Down; sleep 1
shot "2. Down: row 2 (voice-b00002, listening-clip fallback); old player killed, new clips start"
playlog | tail -3
echo "(player processes alive: $(pgrep -fc '^sleep 3.737$') -- the previous voice's was killed, not left to finish)"
$T send-keys -t w Up; sleep 0.5
$T send-keys -t w "Damian Miller"; sleep 0.5
shot "3. typed 'Damian Miller' into NAME (j/k/q are letters here)"
$T send-keys -t w Enter; sleep 1
shot "4. Enter: locked in; row updated; highlight moved to next unnamed voice"
log "   note frontmatter + heading for voice-a00001"; grep -E '^(name|status):|^# ' "$S/voices/voice-a00001.md"
$T send-keys -t w Down; sleep 0.3
$T send-keys -t w Down; sleep 0.3
log "   (cursor now on the named row voice-c00003; Space toggles, j/k move)"
$T send-keys -t w Up; sleep 0.3
$T send-keys -t w Up; sleep 0.3
shot "5. back on an unnamed row (voice-b00002)"
$T send-keys -t w M-m; sleep 0.7
shot "6. Alt+M (ESC m): second selector '+' opens on the most heard named voice; '-' marks the voice being folded in"
$T send-keys -t w Escape; sleep 0.7
shot "7. lone Esc cancels the selector"
$T send-keys -t w M-m; sleep 0.5
$T send-keys -t w Enter; sleep 0.5
shot "8. Alt+M, Enter on the picked row: confirmation prompt"
$T send-keys -t w n; sleep 0.4
shot "9. n: back to the selector (nothing merged)"
$T send-keys -t w Down; sleep 0.2
$T send-keys -t w Enter; sleep 0.4
shot "10. cursor moved onto the original row itself: merge into itself refused"
$T send-keys -t w Escape; sleep 0.5
$T send-keys -t w M-m; sleep 0.4
$T send-keys -t w Enter; sleep 0.4
$T send-keys -t w y; sleep 1
shot "11. Alt+M, Enter, y: voice-b00002 folded into the picked voice (dimmed 'merged' row)"
log "   voice-b00002 note after merge"; grep -E '^(status|merged-into):' "$S/voices/voice-b00002.md"
$T resize-window -t w -x 40 -y 9; sleep 0.8
shot "12. resized to 40x9"
$T resize-window -t w -x 14 -y 4; sleep 0.8
shot "13. resized to 14x4 (no crash)"
$T resize-window -t w -x 100 -y 22; sleep 0.8
$T send-keys -t w C-c; sleep 1.5
log "14. Ctrl+C: quit"; cat "$S/exit.txt"
echo "(player processes left after quit: $(pgrep -fc '^sleep 3.737$' || true))"
$T capture-pane -p -t w | sed -e 's/[[:space:]]*$//' | grep -v '^$' | head -5
$T kill-server 2>/dev/null
log "ledger after the run"
env -i HOME=$HOME PATH=$PATH WAX_ROOT=$S/root WAX_VAULT=$S/vault TRANSCRIPTS=$S/vault WAX_VOICES_DIR=$S/voices /usr/bin/python3 $WAX voices list
log "clip cache (var/voice-clips) and the vault dir hold no clips"; ls "$S/root/var/voice-clips" | wc -l; ls "$S/voices"
