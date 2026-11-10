# Intake: how audio reaches `inbox/`

`inbox/` is the one work queue: a plain local, recursive directory under `WAX_ROOT` (`~/HeyMa`). Everything Wax transcribes arrives as a file in `inbox/`, and exactly four sources put one there: the hotkey capture, the Syncthing dropoff feed, the Telnyx S3 intake, and a manual drop. Vinyl dictation is **not** one of them — see [satellites](satellites.md).

The inbox walk (`state.inbox_items()`) is a recursive `os.walk` that prunes `.stfolder`, `.stversions` and every dot-directory (including `.staging`), and skips dot-files and non-media suffixes. Item identity is `sha256[:16]` of the content. There is **no settle guard on the inbox**: a half-written file can be picked up (transcription re-stats the source afterwards via `sanity.source_unchanged`). When dropping manually, write to a dot-name and rename into place.

## Source 1: hotkey capture

`Ctrl+\` → `~/.local/bin/wax-toggle` → `wax rec toggle`. Segments land in `stream/<rid>.segs/`, then finalize into `inbox/` as `<YYYYMMDD-HHMMSS>-rec.ogg`. See [capture](capture.md). Journal scope to check: `app-gnome-wax\x2dtoggle-*.scope`.

## Source 2: Syncthing dropoff (the phone)

Syncthing folder **ID `audio`** (hub-side label `Audio`, not "Transcription Inbox" as older docs say) receives into `dropoff/` on big-chungus. The folder is `receiveonly` (there is no `ignoreDelete` on it); hub deletions are never re-downloaded because the policy service mirrors accepted names into managed `.stignore` exclusion blocks. **waxd only ever copies out of `dropoff/` — never write to it.** Devices sharing it (last seen 2026-11-05): PottyBarron (the phone sender; connected), mommymac (connected), phoneballs (2026-11-01), tiny-chungus (2026-11-02), big-chungus (hub).

`syncthing-audio-policy.service` (enabled+active since 2026-10-21; code `components/syncthing-audio/guard.py`) is metadata-only: it accepts flat, uniquely named recordings once, keeps the seen-name ledger `var/syncthing-audio/received.json` (410 names on 2026-11-05), and mirrors those names into managed `.stignore` blocks so hub deletions are never re-downloaded. Accepted suffixes: ogg, opus, mp3, m4a, wav, flac, aac, mov, mp4, mkv, webm, wma, aiff. It never writes audio and owns no pipeline stage.

The actual copy is `dropoff.py copy_received()`, which runs **inside `worker.run_once()`** — so only when the worker loop is enabled (`wax pipeline status`) or on `wax drain`. It copies only names listed in `received.json`, staging to `inbox/.staging/dropoff-*.part`, verifying sha256, then `move_noclobber` (a name collision becomes `<stem>--<item_id><suffix>`). The item gets `origin='dropoff'` and `orig_name` = the source filename. The phone names recordings `YYYYMMDD HHMMSS.m4a`; transcripts are named by arrival, not recording date.

## Source 3: Telnyx S3 intake (calls)

Telnyx native call recordings land in MinIO; `telnyx-s3-intake` (code `components/telnyx-s3-intake/intake.py`; config `~/.config/heyma/telnyx-s3-intake.json` with `op://` credential refs) polls the configured native-recording prefix, probes the audio, stages it, and delivers into `inbox/` as `telnyx-{date}-{name[:72]}-{receipt id}.mp3`. Receipts live under `var/telnyx-s3-intake/` (81 delivered as of 2026-11-05). It never edits `dropoff/`. Voicemails take a different path — the webhook/`wax calls sync` fetch, not S3 intake; see [calls](calls.md).

**The clock is Candybar, not the systemd timer (S16, verified 2026-11-05).** `telnyx-s3-intake.timer` is loaded but **disabled and inactive**; Candybar job `telnyx-s3-intake` (id `01a17952-2ab7-78f1-a008-f994a0aaf1c7`, `* * * * *` America/New_York, enabled, ~14700 successes since 2026-10-26) starts the oneshot service every minute via the `50-candybar.conf` drop-in. Cutover 2026-10-26 (Candybar migration wave 7.11); the timer is the documented rollback path only. The run itself is bounded: `run_seconds` 240, `TimeoutStartSec` 270, exit 75 on `overlap_lock_busy`.

Operator commands: `bin/telnyx-s3-intake {run|dry-run|status}`.

**A paused job fails silently.** From 2026-11-07 13:07 to 2026-11-09 19:02 EST the job was paused from the Holocene Candybar page. Calls still synced and announced, but 25 recordings never reached `inbox/`. `candybar list` hides paused jobs by default (`--all` shows them). The doctor probe "call recordings delivered" now catches this from the ledger. Fix: `candybar resume telnyx-s3-intake`. Incident: [incident-log](../subskills/doctor/references/incident-log.md).

## Source 4: manual drop

Copy any media file into `inbox/` (at any depth). Respect the dot-name-then-rename rule above.

## After the inbox

From here it is the standard item pipeline: archive (S3 backup-first, [archive](archive.md)), then transcription ([transcription](transcription.md)). `skipped`/`failed`/`suspect` items stay in the inbox until an operator acts (`wax retry`); `complete` duplicates are parked to `archive/duplicates/`, never deleted.

**Phantom rows (2026-11-05):** 214 `pending` ledger rows point at paths that no longer exist (189 under `inbox/202*`, 13 `inbox/ASR*`, 7 `inbox/voi*`, 2 under retired `~/audio/`, plus 3 one-off drops: `test-1769351396.mp3`, `neos_problems.m4a`, `IntelliforiaPivot.m4a`). They are history, not a queue: `wax items` counts them, `wax queue` does not. `wax reconcile` without `--rebuild` refuses (exit 2) and has **no `--dry-run`** — older doctor notes citing one are stale.

## "Recorded but no transcript" triage

Work top to bottom; stop at the first row that matches. The ledger is ground truth: `sqlite3 "file:$HOME/HeyMa/var/wax.db?mode=ro"` → `select item_id, origin, state, first_seen from items order by first_seen desc limit 10;`

| Source | Check | If it fails |
|---|---|---|
| Hotkey (`Ctrl+\`) | `journalctl --user -t wax-toggle` / scope `app-gnome-wax\x2dtoggle-*.scope` has a recent run; `wax rec list` shows the capture; `stream/` has no orphaned `<rid>.segs/` | `wax rec salvage <rid>` for an orphaned capture; verify the keybinding still points at `~/.local/bin/wax-toggle` (a symlink into the repo — never a PATH `wax`) |
| Vinyl dictation (`Ctrl+KP_Divide`) | **Vinyl is not Wax and never feeds it.** Scope `app-gnome-vinyl\x2dtoggle-*.scope`, `~/.local/state/vinyl/state.json` | Expected: no Wax item. See [satellites](satellites.md) |
| Phone via Syncthing | Syncthing REST `stats/folder` `lastFile` for folder `audio`; hub `received.json` gained the name; then `items` with `origin='dropoff'` | Device offline or sender not send-only → fix the share. Name already in `received.json` but excluded → it is a re-send of a deleted file, by design. Copied but no item → the worker loop is off: `wax pipeline status`, or `wax drain` once |
| Telnyx call recording | `wax calls list` / `wax calls show`; `call_recordings` row with `file_name` set; `bin/telnyx-s3-intake status` last run success | `wax calls sync --since 2d`; `wax calls backfill --dry-run` for pre-2026-10-15 recordings; check the Candybar job `telnyx-s3-intake` |
| Telnyx voicemail | `call_recordings` voicemail row: `file_name` vs `fetch_error` | `voicemail_empty` is final (hangup at the beep); `voicemail_expired` means the 7-day link died; otherwise `wax calls sync --since 7d` retries. See [calls](calls.md) |
| Manual drop | File is in `inbox/`, not a dot-file, media suffix, and `wax queue` sees it | Dot-prefixed name → invisible forever, rename it. Non-media suffix → ignored. Written in place slowly → may have been picked up half-written; `sanity.source_unchanged` catches this at transcription |
| Any source, item exists | `wax ep status <item>`, `wax history <item>` | Item-level failure, not intake: go to [failures](failures.md) and the [doctor](../subskills/doctor/SUBSKILL.md) |
