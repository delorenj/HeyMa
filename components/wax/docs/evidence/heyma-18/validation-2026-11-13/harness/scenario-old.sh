#!/bin/bash
# Gap 4 empirically: a REAL waxd running the PRE-FENCE code (git archive of 2ff0b5f^, no fence.py) in the isolated
# root, driven by the NEW cli.
V=/tmp/heyma-validate-hm18; W=/home/delorenj/HeyMa/.worktrees/hm18-waxd-validate
I=$W/components/wax/docs/evidence/heyma-18/validation-2026-11-13/harness/iso
WAX="$I /usr/bin/python3 $W/components/wax/bin/wax"
ts() { date -u +%H:%M:%S.%N | cut -c1-12; }
q() { /usr/bin/python3 -I -c "
import sqlite3,sys;c=sqlite3.connect('file:$V/root/var/wax.db?mode=ro',uri=True)
for r in c.execute(sys.argv[1]).fetchall(): print(' | '.join(str(x) for x in r))" "$1"; }
LOG0=$(wc -l < $V/logs/transcriber.log)
echo "old waxd: pid $(cat $V/root/var/waxd.singleton.lock)  argv: $(tr '\0' ' ' < /proc/$(cat $V/root/var/waxd.singleton.lock)/cmdline)"
echo "pre-fence code has no fence module: $(test -e $V/oldcode/components/wax/src/wax/fence.py && echo FOUND || echo 'no fence.py'); item_fences mentions in its worker/ledger: $(grep -c item_fences $V/oldcode/components/wax/src/wax/worker.py $V/oldcode/components/wax/src/wax/ledger.py | tr '\n' ' ')"
echo "--- what the old daemon advertises (socket, then state.json)"
/usr/bin/python3 -I -c "
import socket,json
s=socket.socket(socket.AF_UNIX);s.connect('$V/root/var/waxd.sock');d=b''
while True:
    c=s.recv(65536)
    if not c:break
    d+=c
j=json.loads(d);print('socket  daemon =',j.get('daemon','<absent>'))
print('state.json daemon =',json.load(open('$V/root/var/state.json')).get('daemon','<absent>'))"
echo
echo "=== PART 1: the hazard the guard prevents. A fence raised under the old worker (inserted with the NEW code's raise_fence) is ignored."
cp $V/fixtures/unrelated-talk.ogg $V/root/inbox/.u.tmp && mv $V/root/inbox/.u.tmp $V/root/inbox/unrelated-talk.ogg
for i in $(seq 60); do tail -n +$((LOG0+1)) $V/logs/transcriber.log | grep -q "START.*unrelated" && break; sleep 0.3; done
UID_=$(q "select item_id from items where path like '%unrelated-talk.ogg'" | head -1)
echo "[$(ts)] target $UID_ state=$(q "select state from items where item_id='$UID_'") transcriber running; children tracked by the old worker: [$(q 'select count(*) from item_children' 2>&1 | tail -1)]"
$I env WAX_COMPONENT_SRC=$W/components/wax/src /usr/bin/python3 -I -c "
import sys;sys.path.insert(0,'$W/components/wax/src')
from wax import fence,ledger
print('raise_fence ->',fence.raise_fence('$UID_','hazard-demo'))"
sleep 12
echo "[$(ts)] item_fences: $(q 'select substr(item_id,1,12),split_id from item_fences')"
echo "transcriber log since start:"; tail -n +$((LOG0+1)) $V/logs/transcriber.log
echo "item state: $(q "select state from items where item_id='$UID_'")  (the fence was ignored: the old worker kept going)"
echo "vault: $(ls $V/vault | tr '\n' ' ')   <- a transcript was PUBLISHED for a fenced item"
echo "transitions:"; q "select at,from_state,to_state,cause_code from transitions where subject='$UID_' order by seq"
echo
echo "=== PART 2: the new CLI against the old daemon, item in processing -> refused"
LOG1=$(wc -l < $V/logs/transcriber.log)
cp $V/fixtures/slow-meeting-overrun.ogg $V/root/inbox/.s.tmp && mv $V/root/inbox/.s.tmp $V/root/inbox/slow-meeting-overrun.ogg
for i in $(seq 60); do tail -n +$((LOG1+1)) $V/logs/transcriber.log | grep -q "START.*mode=slow" && break; sleep 0.5; done
sleep 1
TID=$(q "select item_id from items where path like '%slow-meeting-overrun.ogg'" | head -1)
CH=$(q "select pid from item_children where item_id='$TID'")
FIND=$(pgrep -f "fake-transcribe.*slow-meeting" | head -1)
echo "target $TID state=$(q "select state from items where item_id='$TID'"); transcriber pid (pgrep) $FIND alive=$(test -d /proc/$FIND && echo yes); tracked in item_children: [${CH}]  (old worker does not register children)"
SHA0=$(sha256sum $V/root/inbox/slow-meeting-overrun.ogg | cut -d' ' -f1)
echo "[$(ts)] \$ wax split ${TID:0:10}"
$WAX split ${TID:0:10}; RC=$?; echo "[$(ts)] exit=$RC"
echo "--- nothing changed:"
echo "fences: [$(q "select item_id from item_fences where item_id='$TID'")]  state: $(q "select state from items where item_id='$TID'")  transcriber still alive: $(test -d /proc/$FIND && echo yes)"
echo "original still in inbox, sha256 unchanged: $(test "$(sha256sum $V/root/inbox/slow-meeting-overrun.ogg | cut -d' ' -f1)" = "$SHA0" && echo yes)"
echo "skipped/split exists: $(test -e $V/root/skipped/split && echo yes || echo no)"
echo
echo "=== PART 3: paths the guard must NOT block, same old daemon"
echo "\$ wax split ${TID:0:10} --dry-run"; $WAX split ${TID:0:10} --dry-run; echo "exit=$?"
echo "fences after dry-run: [$(q "select item_id from item_fences where item_id='$TID'")]"
