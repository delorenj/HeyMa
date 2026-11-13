#!/bin/bash
# run a command with the PRIVATE pipewire/pulse only (no real audio server reachable)
exec env -i HOME=/home/delorenj PATH=/usr/bin:/bin TERM=dumb \
  XDG_RUNTIME_DIR=/tmp/heyma-validate-heyma-19/pw/xdg \
  PULSE_SERVER=unix:/tmp/heyma-validate-heyma-19/pw/xdg/pulse/native \
  PIPEWIRE_RUNTIME_DIR=/tmp/heyma-validate-heyma-19/pw/xdg "$@"
