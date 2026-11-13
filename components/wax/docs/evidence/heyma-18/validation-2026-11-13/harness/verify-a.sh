#!/bin/bash
V=/tmp/heyma-validate-hm18; W=/home/delorenj/HeyMa/.worktrees/hm18-waxd-validate
I=$W/components/wax/docs/evidence/heyma-18/validation-2026-11-13/harness/iso
q() { /usr/bin/python3 -I -c "
import sqlite3,sys;c=sqlite3.connect('file:$V/root/var/wax.db?mode=ro',uri=True)
for r in c.execute(sys.argv[1]).fetchall(): print(' | '.join(str(x) for x in r))" "$1"; }
echo "== transcriber log"; cat $V/logs/transcriber.log
echo "== items"; q "select substr(item_id,1,12),state,path from items"
echo "== item_fences"; q "select substr(item_id,1,12),split_id,reason,created_at from item_fences"
echo "== item_children (must be empty)"; q "select * from item_children"
echo "== transitions (target a8a7e80b4aec)"; q "select at,machine,from_state,to_state,cause_code from transitions where subject like 'a8a7e80b4aec%' order by seq" 2>&1 | head -20
echo "== transitions (unrelated 08d51662097b)"; q "select at,machine,from_state,to_state,cause_code from transitions where subject like '08d51662097b%' order by seq" 2>&1 | head -20
echo "== passes rows"; q "select substr(item_id,1,12),ep_slug,state,reason_code,detail from passes order by updated_at"
echo "== original: where is it, sha256"
find $V/root -name 'slow-meeting-overrun*' -not -name '*part*' -exec sha256sum {} \;
echo "expected a8a7e80b4aec87c41d136a58e10547b04d9856de25348cd75f65f89febb217e0 (fixtures copy: $(sha256sum $V/fixtures/slow-meeting-overrun.ogg | cut -d' ' -f1))"
echo "== hold"; find $V/root/skipped $V/root/recovered -type f | sort | xargs ls -la
echo "== split.json"; cat $V/root/skipped/split/*/split.json
echo "== vault (published transcripts)"; ls -la $V/vault; echo "== frontmatters stub calls"; cat $V/logs/frontmatters.log 2>/dev/null
echo "== daemon health"
$I /usr/bin/python3 $W/components/wax/bin/wax status 2>&1 | head -12
echo "daemon snapshot updated_at x3, 1.2 s apart:"
for i in 1 2 3; do /usr/bin/python3 -I -c "
import socket,json
s=socket.socket(socket.AF_UNIX);s.connect('$V/root/var/waxd.sock');d=b''
while True:
    c=s.recv(65536)
    if not c:break
    d+=c
j=json.loads(d);print(j['updated_at'],j['daemon'],j['queue'])"; sleep 1.2; done
echo "waxd alive: $(cat $V/root/var/waxd.singleton.lock) -> $(test -d /proc/$(cat $V/root/var/waxd.singleton.lock) && echo yes)"
