# sourced: the isolated environment for the live-relabel run
R=/tmp/heyma-validate-heyma-19/real3
ENVARGS=(HOME=/home/delorenj PATH=/usr/bin:/bin TERM=xterm-256color
  WAX_ROOT=$R/root WAX_VAULT=$R/vault TRANSCRIPTS=$R/vault WAX_VOICES_DIR=$R/voices
  WAX_VOICE_NOTIFY=0 WAX_TRANSCRIPTS=0 WAX_TRANSCRIPTS_UNIT_FILE=/nonexistent/waxd.service
  WAX_NATS_HOST=127.0.0.1 WAX_NATS_PORT=9)
