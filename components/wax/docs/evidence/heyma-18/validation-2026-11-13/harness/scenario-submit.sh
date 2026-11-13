#!/bin/bash
V=/tmp/heyma-validate-hm18; W=/home/delorenj/HeyMa/.worktrees/hm18-waxd-validate
I=$W/components/wax/docs/evidence/heyma-18/validation-2026-11-13/harness/iso
WAX="$I /usr/bin/python3 $W/components/wax/bin/wax"
q() { /usr/bin/python3 -I -c "
import sqlite3,sys;c=sqlite3.connect('file:$V/root/var/wax.db?mode=ro',uri=True)
for r in c.execute(sys.argv[1]).fetchall(): print(' | '.join(str(x) for x in r))" "$1"; }
SID=$(ls $V/root/skipped/split | head -1)
echo "\$ wax split list"; $WAX split list
echo "\$ wax split submit $SID 1"; $WAX split submit $SID 1; echo "exit=$?"
for i in $(seq 90); do q "select state from items where path like '%part1of2%'" | grep -q "^complete\|enrichment_pending\|failed" && break; sleep 1; done
sleep 3
echo "== part items"; q "select substr(item_id,1,16),state,path from items where path like '%part%'"
echo "== part transitions"; q "select at,from_state,to_state,cause_code from transitions where subject in (select item_id from items where path like '%part1of2%') order by seq"
echo "== backups row for the part"; q "select * from backups where item_id in (select item_id from items where path like '%part1of2%')"
echo "== transcriber log (part)"; grep part1of2 $V/logs/transcriber.log
echo "== S3 trace, requests mentioning the part, in time order"
/usr/bin/python3 -I - <<PY
import json
for l in open('$V/logs/s3-trace.jsonl'):
    try: j=json.loads(l)
    except Exception: continue
    if 'part1of2' in j.get('path','') and j.get('api','').startswith('s3.') and j.get('api') not in ('s3.GetBucketLocation',):
        print(j['time'], j['api'], j['path'], j['statusCode'], 'size=%s' % j.get('size'))
PY
echo "== split.json segment 1 afterwards"; /usr/bin/python3 -I -c "
import json,glob;d=json.load(open(glob.glob('$V/root/skipped/split/*/split.json')[0]));print(json.dumps(d['segments'][0],indent=1))"
echo "== hold still has the part (copy, not move)?"; ls -la $V/root/skipped/split/$SID/
echo "== inbox/archive location of the submitted part"; find $V/root/inbox $V/root/archive -name '*part*'
echo "== vault"; ls $V/vault
