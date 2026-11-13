#!/bin/bash
# Scenario B: the transcriber finishes NORMALLY inside the kill grace window (a genuinely late result).
V=/tmp/heyma-validate-hm18; W=/home/delorenj/HeyMa/.worktrees/hm18-waxd-validate
I=$W/components/wax/docs/evidence/heyma-18/validation-2026-11-13/harness/iso
WAX="$I /usr/bin/python3 $W/components/wax/bin/wax"
ts() { date -u +%H:%M:%S.%N | cut -c1-12; }
q() { /usr/bin/python3 -I -c "
import sqlite3,sys;c=sqlite3.connect('file:$V/root/var/wax.db?mode=ro',uri=True)
for r in c.execute(sys.argv[1]).fetchall(): print(' | '.join(str(x) for x in r))" "$1"; }
echo "vault before: $(ls $V/vault | tr '\n' ' ')"; echo "frontmatters stub calls before: $(wc -l < $V/logs/frontmatters.log)"
echo "[$(ts)] drop stubborn-meeting-overrun.ogg"
echo "source sha256 $(sha256sum $V/fixtures/stubborn-meeting-overrun.ogg | cut -d' ' -f1)"
cp $V/fixtures/stubborn-meeting-overrun.ogg $V/root/inbox/.st.tmp && mv $V/root/inbox/.st.tmp $V/root/inbox/stubborn-meeting-overrun.ogg
for i in $(seq 60); do grep -q "START .*mode=stubborn" $V/logs/transcriber.log 2>/dev/null && break; sleep 1; done
sleep 1
TID=$(q "select item_id from items where path like '%stubborn-meeting-overrun.ogg'" | head -1)
echo "target=$TID state=$(q "select state from items where item_id='$TID'")"; q "select item_id,pid,argv0 from item_children"
echo "[$(ts)] \$ wax split ${TID:0:10}"
$WAX split ${TID:0:10}; echo "[$(ts)] wax split exit=$?"
sleep 12
echo "== transcriber log (stubborn)"; grep stubborn $V/logs/transcriber.log
echo "== waxd.log lines mentioning the target"; grep -i "${TID:0:12}\|stubborn\|late\|fence" $V/logs/waxd.log | cut -c1-300
echo "== items"; q "select substr(item_id,1,12),state,path from items where item_id='$TID'"
echo "== transitions"; q "select at,from_state,to_state,cause_code from transitions where subject='$TID' order by seq"
echo "== passes rows for target (must be none or skipped/cancelled)"; q "select ep_slug,state,reason_code from passes where item_id='$TID'"
echo "== parked late transcript"; find $V/root/recovered -type f | xargs -r ls -la
echo "== vault after: $(ls $V/vault | tr '\n' ' ')  (no stubborn transcript)"
echo "== frontmatters stub calls after: $(wc -l < $V/logs/frontmatters.log)"
echo "== outbox events naming the target"; q "select seq,topic,substr(payload,1,160) from outbox where payload like '%${TID:0:12}%' order by seq" 2>&1 | tail -8
echo "== item row for stuck state: fenced?"; q "select substr(item_id,1,12),split_id from item_fences"
echo "waxd alive: $(test -d /proc/$(cat $V/root/var/waxd.singleton.lock) && echo yes)"
