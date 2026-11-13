#!/bin/bash
# Scenario A: real waxd (isolated root), slow target transcribing + an unrelated queued item.
# `wax split <target>` runs while the target's transcriber child is live.
V=/tmp/heyma-validate-hm18; W=/home/delorenj/HeyMa/.worktrees/hm18-waxd-validate
I=$W/components/wax/docs/evidence/heyma-18/validation-2026-11-13/harness/iso
WAX="$I /usr/bin/python3 $W/components/wax/bin/wax"
ts() { date -u +%H:%M:%S.%N | cut -c1-12; }
q() { sqlite3 "file:$V/root/var/wax.db?mode=ro" "$@" 2>/dev/null || /usr/bin/python3 -I -c "
import sqlite3,sys;c=sqlite3.connect('file:$V/root/var/wax.db?mode=ro',uri=True)
for r in c.execute(sys.argv[1]).fetchall(): print('|'.join(str(x) for x in r))" "$1"; }
echo "[$(ts)] drop target (slow-meeting-overrun.ogg) into the isolated inbox"
SHA0=$(sha256sum $V/fixtures/slow-meeting-overrun.ogg | cut -d' ' -f1); echo "source sha256 $SHA0"
cp $V/fixtures/slow-meeting-overrun.ogg $V/root/inbox/.slow.tmp && mv $V/root/inbox/.slow.tmp $V/root/inbox/slow-meeting-overrun.ogg
for i in $(seq 60); do grep -q "START .*mode=slow" $V/logs/transcriber.log 2>/dev/null && break; sleep 1; done
echo "[$(ts)] transcriber started:"; tail -2 $V/logs/transcriber.log
echo "[$(ts)] drop unrelated item (queued behind the target; worker concurrency is 1)"
cp $V/fixtures/unrelated-talk.ogg $V/root/inbox/.unrel.tmp && mv $V/root/inbox/.unrel.tmp $V/root/inbox/unrelated-talk.ogg
sleep 2
q "select substr(item_id,1,12),state,path from items"
TID=$(q "select item_id from items where path like '%slow-meeting-overrun.ogg'" | head -1); UID_=$(q "select item_id from items where path like '%unrelated-talk.ogg'" | head -1)
echo "target=$TID unrelated=$UID_"
echo "children now:"; q "select item_id,pid,argv0 from item_children"
CHILD=$(q "select pid from item_children where item_id='$TID'" | head -1); echo "child pid $CHILD alive: $(test -d /proc/$CHILD && echo yes || echo no)"
echo "[$(ts)] \$ wax split ${TID:0:10}"
$WAX split ${TID:0:10}; RC=$?
echo "[$(ts)] wax split exit=$RC"
echo "child $CHILD alive after: $(test -d /proc/$CHILD && echo yes || echo no)"
