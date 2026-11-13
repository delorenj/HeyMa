# HEYMA-18 `wax split`: recording-safe validation under a real waxd (2026-11-13)

Branch `validate/hm18-waxd` (from main b42a385). Every run used a real `components/wax/bin/waxd` and the real `wax`
CLI from this worktree, in an isolated root `/tmp/heyma-validate-hm18/root`. Production waxd (pid 5494), its ledger,
S3, vault, voices, NATS and desktop were never written (see "Production" below).

## Gap matrix

| # | Gap | Verdict | Evidence | Why |
|---|---|---|---|---|
| 1 | Real waxd integration (kill, fence, preserve, hold, unrelated item, late transcript, health, submit) | PROVEN | `10`, `11`, `12`, `20`, `30` | Real waxd, real `wax split` while the target's transcriber child was live. The transcriber is a slow STUB (no whisper); the S3 is a real MinIO container (throwaway, loopback). |
| 2 | LIVE `wax split` under a real daemon without a mic | PROVEN, with a labelled seam | `50` | Real `wax rec start`, real ffmpeg encoder, real stop (`q` on the FIFO) and finalize. Only the audio SOURCE is replaced (`stub-ffmpeg` swaps `-f pulse -i <src>` for a prerecorded fixture at 20x; `stub-systemd-run` execs without a scope). No device was opened. |
| 3 | Pass subprocess in flight when the fence goes up | PROVEN (bug found and fixed) | pytest `PassInFlightWhenFencedTest` (4 tests), real-daemon run `30` | The unit test reproduced a real bug (below). Scenario D repeats it under the real waxd: pass child killed, pass row `skipped/item_split`, no note write, no `task.completed`/`transcription.completed`. |
| 4 | Old-worker hazard, CLI refusal | PROVEN | `40`, `41`, pytest `split_daemon_guard_test` (17 tests) | Empirical: a real pre-fence waxd (`git archive 2ff0b5f^`) ignored a fence and published a transcript; the new CLI refused with exit 2 and changed nothing; `--dry-run` still works. Production's socket/state.json lack the capability today (`41`). |
| 5 | Detection on real archived recordings (read-only, copies) | PARTIAL | `60`-`65` | Ran on 4 named recordings and an 80-recording sweep against the vault transcripts. It found a real false-positive class (fixed, below). After the fix: no boundary on 79/80, 1 remaining false positive, and it finds none of the 15 recordings that look like real overruns. Safe, but with no recall on real data. |

## Bugs found and fixed (regression tests included)

1. **A pass that timed out or hit OSError after the fence went up still recorded `failed`, emitted a task event and wrote the note** (`passes.py`).
   Test `PassInFlightWhenFencedTest` failed `'failed' != 'cancelled'` with the fix stashed, passes with it. Fix: `_drop_late_result`, the same
   rule the normal return path already had (`skipped`, reason `item_split`).
2. **Old waxd + new `wax split` could half-do a split** (gap 4). `wax split` now refuses to halt an item unless the running waxd advertises
   `daemon.capabilities: ["item-fence/1"]` (waxd writes it into the socket snapshot and state.json). Exit 2, "restart waxd first", nothing touched.
   Live capture, `--dry-run`, finished items and hosts with no daemon are unaffected. Docs: `WAX-SPLIT.md`, `references/cli.md`, `references/capture.md`.
3. **Detection cut through real conversation** (gap 5, new). Before: 8 boundaries on 80 real recordings, 6 of them with speaker turns starting 137-927 s after the
   cut (97-1006 words), per the vault transcript (`62`). Cause: a recording that is mostly conversation has a "noise floor" that
   is its own pauses, so soft speech reads as quiet. In live mode that would stop a meeting in progress. Fix: the quiet run's median must sit
   at least `--min-drop-db` (default 20) under the loud level, else `quiet_not_quiet` (exit 3). The synthetic overruns sit about 40 dB under it,
   every false positive was within 17 dB. Tests: `test_soft_speech_in_a_noisy_room_is_not_a_quiet_tail` (also proves the new fixture is a false cut with the
   guard off) and `test_real_overruns_sit_far_under_the_meeting`.

## What the real-recording work shows (gap 5)

* After the fix (`63`): 80 examined, 1 boundary proposed (`06b63f2a19c476d3`, 127 min: the transcript has 97 words after the cut, sparse quiet speech in a quiet
  room, 20 short bursts in 10 min, indistinguishable by level from clicks), 79 no boundary. The remaining false positive is not fixed.
* Recall: 15 of the 80 recordings have a transcript that ends 600+ s before the audio. The detector proposes a boundary for none of them. The two
  that had a boundary before the fix (`2962c521...`, `798731fd...`) had no speech after the cut and are now refused by `min-drop-db`; their
  "quiet" sits only 8.6 and 18.6 dB under the loud level, which is what the 6 real false positives looked like too. The profiles (`64`) show real
  post-meeting audio is not an empty room: bursts to -26..-33 dBFS every few minutes. The documented limitation (noisy tail gets exit 3, never a guess)
  is the common case on this machine's real recordings, not the exception. The synthetic fixtures overstate how often `wax split` will fire.
* Named recordings (`60`, `61`, `65`): 13 min and 65 min recordings, no boundary (continuous activity, correct). The 3 h file: no boundary
  (`quiet_not_trailing`: a 47 min quiet stretch, then 5 min of activity at the end; plausible from the profile). `4160dc4b...` (24 min): was a false
  boundary at 17:27 with the meeting continuing to 24:26, now `quiet_not_quiet` (9.1 dB).
* Ground truth was the ASR transcript in the vault, read-only, never a detector input. Whisper hallucinates on non-speech, so the false-positive counts
  are an estimate: 4 of 6 cut through 550-1006 words of conversation; `06b63f2a` (97 words) and `4191cd95` (137 words, one repeated phrase) are less certain.
* Production audio was only read, and copied first (`real/`, `real-sweep/` under /tmp). No production file was split.

## Other observations

* Scenario D leaves the unsplit transcript in the vault: the transcriber had already written the note when the pass was halted (item state
  `transcribed`). Documented in `WAX-SPLIT.md` "Not covered". A transcript that arrives after the fence (scenario B) is parked in
  `recovered/split-late/`, never published.
* The pre-fence waxd leaves its transcriber child running when it is stopped (an orphan `fake-transcribe` outlived it; cleaned up). Not a `wax split` issue.
* The first isolated run (run 1, `run1-notes-*`) had no `frontmatters` editor, so the unrelated item ended `enrichment_pending`. The harness then
  gained `stub-frontmatters` (logs its calls, edits nothing) and the scenarios were rerun; the `10`/`11`/`12`/`20`/`30` files are the rerun.

## Isolation and what is a stub

`harness/iso` is `env -i` plus explicit variables; `00b-isolation-proof-with-seam-stubs.txt` prints every path and exits 0 (socket, singleton lock,
ledger, state.json, inbox/stream/archive/skipped/recovered, vault, voices, HOME all under /tmp/heyma-validate-hm18). `00-` is the proof before the
seam stubs existed (it lists systemd-run as absent).

| Thing | Real or stub |
|---|---|
| waxd, `wax` CLI, ledger (SQLite), worker, passes machinery, archive.py, ffmpeg encoder and cutting | real, from this worktree |
| S3 | real MinIO container (loopback port 19123, tmpfs, throwaway credentials, not recorded here) with real `mc`; `mc admin trace` gives request timestamps |
| transcriber | STUB `harness/fake-transcribe` (modes by file name: `slow-` blocks, `stubborn-` finishes after TERM, others 3 s) |
| enrichment pass | STUB `harness/fx-stamp` (the only pass; `passslow-` stays in flight) |
| `frontmatters` editor | STUB `harness/stub-frontmatters` (logs, edits nothing) |
| microphone | not opened; `stub-ffmpeg` feeds a fixture (gap 2 only); `stub-pactl` answers the preflight |
| `systemd-run`, `notify-send`, `canberra-gtk-play` | logging stubs |
| NATS | port 1 (closed); Postgres: no DSN, capture-only; ntfy, 1Password (`op`), display, pulse: absent |

## Production

Read-only throughout (`wax status`, sqlite `mode=ro`, the status socket read, `/proc`). Production pid **5494** is unchanged (same start time,
still the only `bin/waxd`, `item_fences` 0, `item_children` 0, `split_segments` 0, no `skipped/split`, no ledger item with a validation path).
`80` is the baseline, `81` the end state. Two differences are not mine:

* **The stream line changed from `recording` to `ready`.** The operator stopped the production recording with the hotkey at 15:29:26 EST
  (journal: `wax-toggle` launched by gsd-media-keys at 15:29:26, encoder.log `[q] command received`, `82`). My first daemon, encoder or CLI run
  in the isolated root was at 15:36, and nothing I ran before then (pytest in temp roots, read-only production reads) addresses a production path. The recording was finalized and archived normally
  (item `f58d5d0ce69acf78`, 3110 s). The after-status in `81` also prints `not-ready (no_default_source)` because it was run under `env -i` without
  a pulse session; production state.json says `ready`.
* **Ledger row counts rose** (items 934 to 937, outbox, transitions, passes, calls, voices): production traffic, the finalized recording and Telnyx calls.

## Files

`00*` isolation, `10`-`12` scenario A and submit/S3 order, `20` late transcript, `30` pass in flight, `40`/`41` old daemon and production probe,
`50` live capture, `60`-`65` real recordings, `70`/`71` pytest, `80`-`82` production baseline and attribution, `logs/` daemon, transcriber,
pass and stub logs, `harness/` every script used (re-runnable with `harness/iso`).
