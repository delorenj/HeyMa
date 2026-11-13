#!/bin/bash
set -u
D=/tmp/heyma-validate-heyma-19; R=$D/real4
WAX=/home/delorenj/HeyMa/.worktrees/hm19-tui-validate/components/wax/bin/wax
T="/usr/bin/tmux -L hm19real4"
shot() { printf '\n===== %s =====\n' "$*"; $T capture-pane -p -t w | sed -e 's/[[:space:]]*$//'; }
digest() { /usr/bin/python3 -I - <<PY
import sqlite3,hashlib,os
c=sqlite3.connect("file:$R/root/var/wax.db?mode=ro",uri=True)
h=hashlib.sha256()
for t in ("voices","voice_sightings","voice_exemplars"):
    for r in c.execute(f"select * from {t} order by 1,2"): h.update(repr(tuple(r)).encode())
print("ledger digest voices+sightings+exemplars:", h.hexdigest()[:16], "voices rows:", c.execute("select count(*) from voices").fetchone()[0])
h=hashlib.sha256()
for f in sorted(os.listdir("$R/voices")): h.update(open("$R/voices/"+f,"rb").read())
print("voices dir copy digest:", h.hexdigest()[:16], "files:", len(os.listdir("$R/voices")))
PY
}
echo "--- before"; digest
$T kill-server 2>/dev/null
ENVV="env -i HOME=/home/delorenj PATH=/usr/bin:/bin TERM=xterm-256color WAX_ROOT=$R/root WAX_VAULT=$R/vault TRANSCRIPTS=$R/vault WAX_VOICES_DIR=$R/voices WAX_VOICE_NOTIFY=0"
$T new-session -d -s w -x 110 -y 30 "$ENVV /usr/bin/python3 $WAX voices tui --no-play --no-sync; echo EXIT=\$? > $D/real4-exit.txt; sleep 20"
sleep 3
shot "1. open (real copy, --no-play --no-sync)"
for i in 1 2 3; do $T send-keys -t w Down; sleep 0.3; done
shot "2. after 3x Down"
$T send-keys -t w M-m; sleep 0.6
shot "3. Alt+M selector (tmux M-m)"
$T send-keys -t w Escape; sleep 0.6
shot "4. Esc cancels"
$T send-keys -t w End; sleep 0.5
shot "5. End (last row)"
$T send-keys -t w PageUp; sleep 0.4
shot "6. PageUp"
$T send-keys -t w C-c; sleep 2
echo; echo "===== exit"; cat $D/real4-exit.txt
echo "--- after"; digest
