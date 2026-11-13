#!/bin/bash
# usage: term_launch.sh <kitty|alacritty> <scratch-dir>   -- runs the TUI inside a real terminal on private Xvfb :119
D=/tmp/heyma-validate-heyma-19; S=$2
WAX=/home/delorenj/HeyMa/.worktrees/hm19-tui-validate/components/wax/bin/wax
TUI="env -i HOME=/home/delorenj PATH=/usr/bin:/bin TERM=xterm-256color WAX_ROOT=$S/root WAX_VAULT=$S/vault TRANSCRIPTS=$S/vault WAX_VOICES_DIR=$S/voices WAX_VOICE_NOTIFY=0 WAX_VOICES_PLAYER=$S/fake-player.sh /usr/bin/python3 $WAX voices tui --no-sync"
case $1 in
  kitty) CMD="kitty --config NONE -o linux_display_server=x11 -o term=xterm-256color -o remember_window_size=no -o initial_window_width=110c -o initial_window_height=30c -o confirm_os_window_close=0 -o allow_remote_control=socket-only --listen-on unix:$D/x/kitty.sock -o shell=/bin/sh sh -c '$TUI; echo EXIT=\$? > $D/x/exit-kitty.txt; sleep 600'";;
  alacritty) CMD="alacritty -o 'window.dimensions={columns=110,lines=30}' -o 'env.TERM=\"xterm-256color\"' -e sh -c '$TUI; echo EXIT=\$? > $D/x/exit-alacritty.txt; sleep 600'";;
esac
exec env -i HOME=/home/delorenj PATH=/usr/bin:/bin DISPLAY=:119 XDG_RUNTIME_DIR=$D/x/xdg-$1 LIBGL_ALWAYS_SOFTWARE=1 sh -c "$CMD"
