#!/bin/bash
# usage: play_probe.sh <label> <clip> <player argv...>
# The player runs against the PRIVATE pipewire only (pwenv.sh); a recorder on its null sink's monitor
# captures what the player delivered, so the byte/second count is audio that really got decoded and played.
cd /tmp/heyma-validate-heyma-19
label=$1; clip=$2; shift 2
rm -f mon-$label.wav
./pwenv.sh timeout -s INT 10 pw-record --target heyma19-silent-sink -P stream.capture.sink=true --rate 16000 --channels 1 --format s16 mon-$label.wav & RP=$!
sleep 1.5
s=$(date +%s.%N); ./pwenv.sh timeout 30 "$@" "$clip"; rc=$?; e=$(date +%s.%N)
wait $RP 2>/dev/null
echo "== $label: argv=[$*] rc=$rc wall=$(echo "$e - $s" | bc)s"
ls -l mon-$label.wav | awk '{print "monitor capture bytes:", $5}'
ffmpeg -nostdin -i mon-$label.wav -af silencedetect=noise=-50dB:d=0.3 -f null - 2>&1 | grep -E "silence_(start|end)" | sed 's/^.*\] /  /'
