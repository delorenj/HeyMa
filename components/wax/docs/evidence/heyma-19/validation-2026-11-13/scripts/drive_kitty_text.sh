#!/bin/bash
cd /tmp/heyma-validate-heyma-19/x
S=/tmp/heyma-validate-heyma-19/fix-kitty2
W=$(./X xdotool search --class kitty | head -1)
./X xdotool windowfocus $W; sleep 0.5
txt() { echo; echo "----- screen text (kitty @ get-text): $1"; env -i PATH=/usr/bin:/bin kitty @ --to unix:/tmp/heyma-validate-heyma-19/x/kitty.sock get-text 2>&1 | sed -e 's/[[:space:]]*$//' | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}' | grep -vE "^$" | head -${2:-12}; }
k() { echo; echo "\$ xdotool key $*   (private display :119, window $W = kitty)"; ./X xdotool key --clearmodifiers "$@"; sleep 0.8; }
txt "open" 12
k Down; txt "after Down" 8
k alt+m; txt "after alt+m (real Alt chord -> ESC m)" 12
k Escape; txt "after Escape" 8
k alt+m; k Return; txt "alt+m, Return: confirm prompt" 12
k n; txt "n" 8
k Escape
echo; echo "\$ xdotool type 'Real Kitty'"; ./X xdotool type --delay 60 "Real Kitty"; sleep 0.5; txt "typed" 8
k Return; sleep 1; txt "after Return (named)" 8
k End; txt "after End" 8
k Home; txt "after Home" 8
k Next; txt "after PageDown (Next)" 8
echo; echo "voice-b00002 note on disk:"; grep -E "^(name|status):" $S/voices/voice-b00002.md
k ctrl+c; sleep 2; cat /tmp/heyma-validate-heyma-19/x/exit-kitty.txt
