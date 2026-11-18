#!/usr/bin/env bash
# test_failed_items_and_ledger_live.sh — doctor.sh checks that read item state and the daemon's fds.
#
# 1. chk_ledger_live must pass when the daemon holds the ledger even if /proc/<pid>/fd is
#    enormous. The old `ls -l fd | grep -q` closed the pipe on the first match, ls died of
#    SIGPIPE, pipefail failed the pipeline, and the check false-FAILed while its own message
#    named the file (waxd held ~30k descriptors on 2026-11-18).
# 2. chk_no_failed_items_in_inbox and chk_recent_recordings_transcribed: three failed newest
#    recordings over ten older completes must FAIL the board; a healthy ledger passes.
#
# Self-contained and read-only: a temp ledger, a child process playing the daemon. Nothing here
# touches the real daemon, ledger or repo.
# Run: bash skills/heyma/subskills/doctor/scripts/tests/test_failed_items_and_ledger_live.sh

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCTOR="$HERE/../doctor.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/wax-doctor-items.XXXXXX")"
CHILDREN=()
cleanup() { for p in "${CHILDREN[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null; done; rm -rf "$TMP"; }
trap cleanup EXIT

fail=0; pass=0
ok()  { pass=$((pass+1)); printf '  ok   %s\n' "$1"; }
bad() { fail=$((fail+1)); printf '  FAIL %s\n' "$1"; }

command -v sqlite3 >/dev/null || { echo "sqlite3 CLI required"; exit 2; }
[ -f "$DOCTOR" ] || { echo "doctor.sh not found at $DOCTOR"; exit 2; }

extract_fn() { sed -n "/^$1() {/,/^}/p" "$DOCTOR"; }
FUNCS="$TMP/funcs.sh"
{
  extract_fn chk_ledger_live
  extract_fn chk_no_failed_items_in_inbox
  extract_fn chk_recent_recordings_transcribed
} > "$FUNCS"
grep -q '^chk_ledger_live() {' "$FUNCS" || { echo "could not extract chk_ledger_live"; exit 2; }
grep -q '^chk_recent_recordings_transcribed() {' "$FUNCS" || { echo "could not extract the new checks"; exit 2; }

# ── 1. chk_ledger_live with an enormous fd table ─────────────────────────────
DB="$TMP/wax.db"; : > "$DB"
# A child that opens the ledger as fd 10 (ls sorts names as text, so it prints FIRST and grep -q
# matches at once) and then ~20000 more descriptors (a 1.3 MB listing, like waxd's leak: smaller ones do not reliably
# trip the race), so ls is still writing long after grep -q has matched and quit.
bash -c '
  ulimit -n 40000 2>/dev/null
  exec 10<"$1"
  for i in $(seq 11 20000); do eval "exec $i<\"$2\""; done
  sleep 60' _ "$DB" /dev/null &
BIG=$!; CHILDREN+=("$BIG")
for _ in $(seq 1 50); do [ "$(ls "/proc/$BIG/fd" 2>/dev/null | wc -l)" -ge 20000 ] && break; sleep 0.2; done
size="$(ls -l "/proc/$BIG/fd" 2>/dev/null | wc -c)"

run_live() { # run_live <pid> -> prints checks output, returns its rc
  ( set -o pipefail; MAIN_PID="$1"; DB="$DB"; . "$FUNCS"; chk_ledger_live )
}

if [ "${size:-0}" -gt 65536 ]; then
  if run_live "$BIG" >/dev/null; then ok "chk_ledger_live passes with a ${size}-byte fd listing (SIGPIPE/pipefail trap)"
  else bad "chk_ledger_live false-FAILed with a ${size}-byte fd listing"; fi
else
  bad "fixture did not produce a large fd listing (${size:-0} bytes)"
fi

# The same check must still FAIL, with a message, when the daemon holds a different file.
OTHER="$TMP/other.db"; : > "$OTHER"
bash -c 'exec 9<"$1"; sleep 60' _ "$OTHER" &
WRONG=$!; CHILDREN+=("$WRONG"); sleep 0.5
if out="$(run_live "$WRONG")"; then bad "chk_ledger_live passed a daemon holding a different ledger"
else
  case "$out" in *"does not hold"*) ok "chk_ledger_live still fails when the daemon holds another file" ;;
                 *) bad "chk_ledger_live failed without its message: $out" ;; esac
fi

# ── 2. item-state checks against a temp ledger ───────────────────────────────
LEDGER="$TMP/items.db"
mkdb() {
  rm -f "$LEDGER"
  sqlite3 "$LEDGER" "
    CREATE TABLE items(item_id TEXT PRIMARY KEY, path TEXT, state TEXT, first_seen TEXT, updated_at TEXT,
      duplicate_of_call_session_id TEXT, twin_of_recording_key TEXT);
    CREATE TABLE transitions(seq INTEGER PRIMARY KEY AUTOINCREMENT, machine TEXT, subject TEXT, cause_code TEXT);
    CREATE TABLE transcripts(item_id TEXT PRIMARY KEY);"
}
add_item() { # add_item <id> <state> <minutes-ago> <has-transcript 0|1> [cause] [file 0|1]
  local id="$1" st="$2" ago="$3" tx="$4" cause="${5:-}" file="${6:-1}" path="$TMP/inbox-$1.m4a"
  [ "$file" = 1 ] && : > "$path"
  sqlite3 "$LEDGER" "INSERT INTO items(item_id,path,state,first_seen,updated_at)
    VALUES('$id','$path','$st',strftime('%Y-%m-%dT%H:%M:%SZ','now','-$ago minutes'),strftime('%Y-%m-%dT%H:%M:%SZ','now','-$ago minutes'));"
  [ -n "$cause" ] && sqlite3 "$LEDGER" "INSERT INTO transitions(machine,subject,cause_code) VALUES('item','$id','$cause');"
  [ "$tx" = 1 ] && sqlite3 "$LEDGER" "INSERT INTO transcripts VALUES('$id');"
  return 0
}
SQ() { sqlite3 -readonly "$LEDGER" "$1" 2>/dev/null; }
runchk() { ( set -o pipefail; SQ() { sqlite3 -readonly "$LEDGER" "$1" 2>/dev/null; }; . "$FUNCS"; "$1" ); }

# healthy: 10 completes with transcripts
mkdb; for n in $(seq 1 10); do add_item "ok$n" complete $((n*30)) 1; done
runchk chk_no_failed_items_in_inbox >/dev/null; rc=$?
[ "$rc" = 0 ] && ok "no failed items: no-failed-items-in-inbox passes" || bad "no-failed-items-in-inbox rc=$rc on a healthy ledger"
runchk chk_recent_recordings_transcribed >/dev/null; rc=$?
[ "$rc" = 0 ] && ok "healthy ledger: recent-recordings-transcribed passes" || bad "recent-recordings-transcribed rc=$rc on a healthy ledger"

# a fresh failed item (any cause) warns (rc 2), with the next command in the message
mkdb; for n in $(seq 1 10); do add_item "ok$n" complete $((n*30+60)) 1; done
add_item bad1 failed 2 0 weird_new_cause
out="$(runchk chk_no_failed_items_in_inbox)"; rc=$?
if [ "$rc" = 2 ] && [[ "$out" == *"weird_new_cause"* && "$out" == *"wax retry"* ]]; then ok "a fresh failed item warns, names its cause and the next command (cause-agnostic)"
else bad "fresh failed item: rc=$rc out=$out"; fi

# one old enough fails
mkdb; add_item bad1 failed 90 0 transcribe_failed
runchk chk_no_failed_items_in_inbox >/dev/null; rc=$?
[ "$rc" = 1 ] && ok "a failed item older than an hour FAILs" || bad "old failed item rc=$rc (want 1)"

# a durability cause fails at once
mkdb; add_item bad1 failed 1 0 s3_reverify_failed
runchk chk_no_failed_items_in_inbox >/dev/null; rc=$?
[ "$rc" = 1 ] && ok "a missing-backup failure FAILs immediately" || bad "durability failure rc=$rc (want 1)"

# a failed row whose audio is gone is history, not an inbox item
mkdb; add_item ghost failed 500 0 transcribe_failed 0
runchk chk_no_failed_items_in_inbox >/dev/null; rc=$?
[ "$rc" = 0 ] && ok "failed rows without audio on disk are not inbox items" || bad "ghost row counted: rc=$rc"

# THE incident: 3 failed newest, 10 older completes -> the board fails
mkdb; for n in $(seq 1 10); do add_item "ok$n" complete $((n*30+120)) 1; done
add_item f1 failed 60 0 transcribe_failed; add_item f2 failed 50 0 transcribe_failed; add_item f3 failed 40 0 transcribe_failed
runchk chk_recent_recordings_transcribed >/dev/null; rc=$?
[ "$rc" = 1 ] && ok "3 failed newest + 10 older completes FAIL recent-recordings-transcribed" || bad "incident shape rc=$rc (want 1)"

# fewer than half transcribed fails; a lone failure among good ones does not
mkdb; for n in $(seq 1 3); do add_item "ok$n" complete $((n*30+200)) 1; done
for n in $(seq 1 7); do add_item "no$n" failed $((n+5)) 0 transcribe_failed; [ "$n" = 4 ] && add_item "mid" complete 1 1; done
runchk chk_recent_recordings_transcribed >/dev/null; rc=$?
[ "$rc" = 1 ] && ok "under half of the newest ten transcribed fails" || bad "under-half rc=$rc (want 1)"
mkdb; for n in $(seq 1 9); do add_item "ok$n" complete $((n*30+10)) 1; done; add_item lone failed 400 0 transcribe_failed
runchk chk_recent_recordings_transcribed >/dev/null; rc=$?
[ "$rc" = 0 ] && ok "one old failure among good recordings does not fail the board" || bad "lone failure rc=$rc (want 0)"

# in-flight and skipped items are not outcomes
mkdb; for n in $(seq 1 6); do add_item "ok$n" complete $((n*30+10)) 1; done
add_item p1 pending 1 0; add_item p2 pending 1 0; add_item s1 skipped 1 0; add_item s2 skipped 1 0; add_item s3 skipped 1 0
runchk chk_recent_recordings_transcribed >/dev/null; rc=$?
[ "$rc" = 0 ] && ok "pending and skipped items are not counted as missing transcripts" || bad "in-flight counted: rc=$rc"

printf '%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
