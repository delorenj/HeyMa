#!/bin/bash
# Scenario D: the target's PASS (fx-stamp) child is in flight when `wax split` runs, under the real waxd.
V=/tmp/heyma-validate-hm18; W=/home/delorenj/HeyMa/.worktrees/hm18-waxd-validate
I=$W/components/wax/docs/evidence/heyma-18/validation-2026-11-13/harness/iso
WAX="$I /usr/bin/python3 $W/components/wax/bin/wax"
ts() { date -u +%H:%M:%S.%N | cut -c1-12; }
q() { /usr/bin/python3 -I -c "
import sqlite3,sys;c=sqlite3.connect('file:$V/root/var/wax.db?mode=ro',uri=True)
for r in c.execute(sys.argv[1]).fetchall(): print(' | '.join(str(x) for x in r))" "$1"; }
FM0=$(wc -l < $V/logs/frontmatters.log); OB0=$(q "select max(id) from outbox")
echo "frontmatters stub calls before: $FM0   outbox max id before: $OB0"
echo "[$(ts)] drop passslow-meeting-overrun.ogg"
cp $V/fixtures/passslow-meeting-overrun.ogg $V/root/inbox/.ps.tmp && mv $V/root/inbox/.ps.tmp $V/root/inbox/passslow-meeting-overrun.ogg
for i in $(seq 90); do grep -q "PASS-START.*passslow" $V/logs/pass.log 2>/dev/null && break; sleep 1; done
sleep 1
TID=$(q "select item_id from items where path like '%passslow-meeting-overrun.ogg'" | head -1)
echo "target=$TID state=$(q "select state from items where item_id='$TID'")"
echo "children:"; q "select item_id,pid,argv0 from item_children"
echo "pass.log so far:"; tail -2 $V/logs/pass.log
echo "[$(ts)] \$ wax split ${TID:0:10}"
$WAX split ${TID:0:10}; echo "[$(ts)] wax split exit=$?"
sleep 8
echo "== pass.log"; grep passslow $V/logs/pass.log
echo "== passes rows"; q "select ep_slug,state,reason_code,detail,attempt from passes where item_id='$TID'"
echo "== transitions"; q "select at,from_state,to_state,cause_code from transitions where subject='$TID' order by seq"
echo "== waxd.log"; grep "$TID\|${TID:0:12}\|passslow" $V/logs/waxd.log | cut -c1-300
echo "== frontmatters stub calls after: $(wc -l < $V/logs/frontmatters.log) (before $FM0); new calls:"; tail -n +$((FM0+1)) $V/logs/frontmatters.log
echo "== outbox rows after id $OB0"; q "select id,subject,created_at from outbox where id>$OB0 order by id"
echo "== item_children now"; q "select * from item_children"
echo "== note in vault (transcript published by the transcriber BEFORE the split: state it is in)"; ls -la $V/vault | grep passslow; cat $V/vault/*passslow*.md
echo "== original/hold"; ls $V/root/skipped/split/*${TID:0:8}*/ ; sha256sum $V/root/skipped/split/*${TID:0:8}*/passslow-meeting-overrun.ogg $V/fixtures/passslow-meeting-overrun.ogg
echo "waxd alive: $(test -d /proc/$(cat $V/root/var/waxd.singleton.lock) && echo yes)"
