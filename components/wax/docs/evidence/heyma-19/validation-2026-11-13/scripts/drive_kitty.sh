#!/bin/bash
cd /tmp/heyma-validate-heyma-19/x
T=${1:-kitty}; C=${2:-$T}
W=$(./X xdotool search --class $C | head -1)
echo "window $W: $(./X xdotool getwindowname $W)"
./X xdotool windowfocus $W; sleep 0.5
echo "focused window now: $(./X xdotool getwindowfocus)"
k() { echo "xdotool key $*"; ./X xdotool key --clearmodifiers "$@"; sleep 0.7; }
snap() { ./X import -window root shot-$T-$1.png; echo "  screenshot shot-$T-$1.png"; }
k Down; snap 02-down
k alt+m; snap 03-altm-selector
k Down; snap 04-selector-down
k Escape; sleep 0.5; snap 05-esc-cancel
k alt+m; k Return; snap 06-confirm-prompt
k n; snap 07-n-back-to-selector
k Escape; sleep 0.4
echo "typing Real Kitty Name"; ./X xdotool type --delay 60 "Real Kitty"; sleep 0.5; snap 08-typed
k Return; sleep 1; snap 09-named
k End; snap 10-end
k Home; snap 11-home
k Next; snap 12-pagedown
