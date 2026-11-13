#!/bin/bash
# Gap 2: LIVE `wax split` under the real isolated waxd. The "microphone" is the ffmpeg seam (see stub-ffmpeg):
# real ffmpeg encodes a prerecorded fixture at 20x through real capture.start / stop / finalize.
V=/tmp/heyma-validate-hm18; W=/home/delorenj/HeyMa/.worktrees/hm18-waxd-validate
I=$W/components/wax/docs/evidence/heyma-18/validation-2026-11-13/harness/iso
WAX="$I /usr/bin/python3 $W/components/wax/bin/wax"
ts() { date -u +%H:%M:%S.%N | cut -c1-12; }
q() { /usr/bin/python3 -I -c "
import sqlite3,sys;c=sqlite3.connect('file:$V/root/var/wax.db?mode=ro',uri=True)
for r in c.execute(sys.argv[1]).fetchall(): print(' | '.join(str(x) for x in r))" "$1"; }
echo "[$(ts)] \$ wax status (before)"; $WAX status | head -3
echo "[$(ts)] \$ wax rec start --label livemeeting"; $WAX rec start --label livemeeting; echo "exit=$?"
RID=$(ls $V/root/stream | grep rec.json | head -1 | sed 's/.rec.json//'); echo "rid=$RID"
ENC=$(/usr/bin/python3 -I -c "import json;print(json.load(open('$V/root/stream/$RID.rec.json'))['pid'])")
echo "encoder pid $ENC: $(tr '\0' ' ' < /proc/$ENC/cmdline | cut -c1-260)"
echo "encoder exe: $(readlink /proc/$ENC/exe)"
echo "[$(ts)] \$ wax status (recording)"; $WAX status | head -3
echo "--- early split: nothing quiet yet -> must be no_boundary and the capture must keep recording"
sleep 12
$WAX split; echo "exit=$?"
echo "still recording: $(test -d /proc/$ENC && echo yes), stream: $($WAX status | sed -n 1,2p | tr '\n' ' ')"
echo "--- waiting for the encoder to have captured the meeting and a long quiet tail (fixture at 20x)"
sleep 52
echo "[$(ts)] segments so far: $(ls $V/root/stream/$RID.segs | wc -l) (10 s of media each); encoder alive: $(test -d /proc/$ENC && echo yes)"
echo "[$(ts)] \$ wax split   (no target: the active capture)"
$WAX split; RC=$?; echo "[$(ts)] exit=$RC"
echo "--- after"
echo "encoder alive: $(test -d /proc/$ENC && echo yes || echo no)   stream: $($WAX status | sed -n 1,2p | tr '\n' ' ')"
echo "stream dir: $(ls -A $V/root/stream | tr '\n' ' ')"
echo "inbox (the unsplit recording must NOT be here): [$(ls -A $V/root/inbox | tr '\n' ' ')]"
find $V/root/skipped -type f | sort | xargs ls -la
echo "--- split.json (summary)"
/usr/bin/python3 -I -c "
import json,glob;d=json.load(open(glob.glob('$V/root/skipped/split/*/split.json')[0]))
print({k:d[k] for k in ('split_id','origin','method','original_settled')}); print('source:',d['source']); print('boundary:',d['detection']['boundary'])
for s in d['segments']: print({k:s[k] for k in ('index','kind','start_s','end_s','duration_s','bytes','state')})"
echo "--- ffprobe durations (real files)"; for f in $V/root/skipped/split/*/*.ogg; do echo "$(basename $f) $(ffprobe -v error -show_entries format=duration -of csv=p=0 $f)"; done
echo "--- no second capture was started: rec.json files in stream: $(ls $V/root/stream | grep -c rec.json)"
echo "waxd alive: $(test -d /proc/$(cat $V/root/var/waxd.singleton.lock) && echo yes)"
