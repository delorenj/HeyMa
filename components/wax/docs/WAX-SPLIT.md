# `wax split`: recovering a recording left running (HEYMA-18)

Design decisions for `wax split`. The hub (`skills/heyma/references/cli.md`,
`capture.md`) links here; this file is the source of truth for thresholds,
syntax and policy. Code: `src/wax/split.py` (detection, cutting, manifests),
`src/wax/fence.py` (halting one item's in-flight work), `bin/wax` (CLI).

## The problem

A meeting is recorded, the operator forgets to stop, and the capture runs for
hours of silence. The recording is wanted (the meeting) and so is the overrun
(never delete), but the overrun must not be transcribed as one 16 hour file.
`wax split` cuts the audio at a boundary found **from the waveform alone**.
ASR, transcripts, diarization and LLMs are never inputs.

## Command

```
wax split [run] [TARGET] [--dry-run] [--submit SEG]... [detection flags]
wax split submit SPLIT_ID SEG...        # SEG is a 1-based index or `all`
wax split list [SPLIT_ID]               # held splits and their segments
```

`run` is implied: `wax split` and `wax split <target>` mean `wax split run ...`.
`TARGET` is a capture id (`YYYYMMDD-HHMMSS-xxxxxx`), an item id (any unique
prefix of at least 6 hex digits) or a file name or path. With no target: the
active capture if one is recording, else the item the worker is processing; if
both exist, or neither, it refuses and lists them (exit 2).

Exit codes: `0` split done, boundary proposed (`--dry-run`) or already split;
`2` refused or failed; **`3` no usable boundary** (nothing was cut, stopped,
halted, moved or written).

## Detection (waveform only)

1. ffmpeg decodes to mono 8 kHz PCM, streamed (a 16 hour file is never held in
   memory). Frames are 100 ms; frame level is RMS in dBFS (floor -120).
2. Frames are grouped into 1 s blocks; a block's level is the loudest frame in it.
3. **Adaptive noise floor**: `noise` = 10th percentile of block levels. A block
   is *active* when its level exceeds `threshold = max(noise + margin, floor)`.
   `--margin-db` (default 10) and `--floor-dbfs` (default -60) are the flags.
   The room's own hum or HVAC sets `noise`, so an overrun of low-level room
   noise is "quiet", not activity. The loud reference (`loud`) is the 90th
   percentile of the *active blocks only*, never of the whole file: a short
   meeting inside a very long overrun (a stand-up left running overnight) is a
   tiny fraction of the blocks and must still read as activity.
4. If no block is active and the loudest second is at or under `--floor-dbfs`
   the audio is silent or all-quiet: `no_activity`. If no block is active
   otherwise, or `loud - noise < --min-range-db` (default 12), there is no
   contrast between quiet and loud (continuous activity, or a flat hum):
   `no_contrast`.
5. Active runs no longer than `--blip-s` (default 3) with quiet on both sides
   are closed (a click or cough is not activity).
6. The boundary is the **last** quiet run that is at least `--min-quiet-s`
   (default 300, five minutes) long and ends within `--tail-slack-s` (default
   60) of the end of the audio, so a final "oh, still recording" does not hide it.
   The cut is `--pad-s` (default 5) after the quiet run starts, so a trailing
   word or its reverb stays with the first part.
7. At least `--min-active-s` (default 30) of activity must precede the cut.
8. **The quiet run must be far under the meeting.** The run's median level must sit at least
   `--min-drop-db` (default 20) below `loud`. Soft speech in a noisy room is "quiet" to a level threshold,
   because the adaptive floor of a recording that is mostly conversation is its own pauses. Measured on 80
   real recordings (`docs/evidence/heyma-18/validation-2026-11-13/62-*.txt`), before this rule 6 boundaries cut
   through 100+ words of conversation, and every one of them had its "quiet" within 17 dB of `loud`; the
   synthetic overruns sit about 40 dB under it. A recording that fails the rule gets `quiet_not_quiet` (exit 3).

Outcomes when there is no boundary (always exit 3, nothing touched):

| outcome reason | meaning |
|---|---|
| `no_activity` | silent or all-quiet input: nothing in the audio is above the absolute floor |
| `no_contrast` | continuous activity or a flat hum: nothing rises `--margin-db` above the noise floor, or quiet and loud differ by less than `--min-range-db` |
| `no_sustained_quiet` | continuous activity, or only ordinary pauses/short trailing quiet (the longest trailing quiet is reported) |
| `quiet_not_trailing` | a long quiet run exists but real activity follows it for more than the tail slack |
| `too_little_activity` | a long trailing quiet run, but under `--min-active-s` of activity before it |
| `too_short` | shorter than `--min-quiet-s` plus `--min-active-s` |
| `quiet_not_quiet` | a long trailing quiet run, but its median is under `--min-drop-db` below the loud level: soft speech or a noisy room, not an empty one |

Ordinary pauses inside a meeting (seconds to a couple of minutes) can never
trigger a cut: only a quiet run reaching the end of the audio does.

### Limitations (deliberate)

Waveform analysis cannot know what a meeting is. Continuous background noise
(a TV, music, a fan that is louder than speech) reads as activity, so such an
overrun gets exit 3, never a guess. A quiet run in the middle with activity
after it is not a boundary. Only one cut (two parts) is made; it does not trim
leading silence, find several meetings, or edit audio. Cuts are lossless
(`ffmpeg -c copy`, same container), so their edges land on codec page
boundaries: parts can overlap or gap by up to about a second, inside the quiet
run. Each part's real duration is recorded and the sum is checked against the
source (tolerance 2 s per cut) before anything is published.

## Policy

- **Never destructive.** The complete original and every derived part are kept.
  Nothing is deleted, no existing transcript is touched, nothing is bulk
  reprocessed. A failed cut removes only its own partial outputs.
- **Analyze first, change second.** Detection is read-only. Only when a
  boundary exists does anything stop or move. `--dry-run` stops after detection.
- **Derived parts are held** under `skipped/split/<split_id>/` with a manifest
  (`split.json`). They enter processing only by `wax split submit` or
  `--submit`, which copies each into `inbox/` through a hidden temp name and
  an atomic no-clobber rename, so the existing path applies unchanged: the
  worker archives to S3 and verifies *before* transcription. Submitting twice
  is a no-op that reports where the part went.
- **Provenance.** `split.json` records source item or capture, source path,
  source sha256, bytes and duration, detection parameters and measurements,
  and for every part: start and end seconds (requested), actual duration,
  bytes, sha256, mtime (set to the part's own end time) and where it was
  submitted. The ledger table `split_segments` holds the same rows keyed by
  the part's item id, so a transcript can be traced back to its source range.
- **No auto-restart.** A live split ends the capture and never starts another.

## Live capture

1. Detect on a snapshot of the completed segments (the in-flight one is
   skipped). No boundary: exit 3, **the capture keeps recording**.
2. A boundary: under the CLI capture lock (`var/waxd.lock`, the one `wax rec`
   uses) re-check the stream is still `recording` with the same id, then
   orderly-stop (`q` on the encoder FIFO, as `wax rec stop`) and finalize the
   segments into `skipped/split/<id>/` instead of `inbox/`, so the worker never
   sees the unsplit recording. waxd stays the encoder's parent; no second
   watcher is added.
3. Detection is repeated on the finalized file (authoritative), then cut.
   If that second look finds no boundary, the finalized recording is released to
   `inbox/` as an ordinary recording and the outcome is `no_boundary`.

## Recording already in processing

1. Detect on the item's audio (read-only). No boundary: exit 3, processing continues.
2. **Fence.** One row in `item_fences` is written for the item. From then on:
   `ledger.set_item_state` refuses further state changes for it (`Fenced`),
   `worker.next_item` skips it, passes and finalize refuse it, and
   `transcribe_adapter` and `passes.run` check the fence after their child
   returns, before publishing. A late transcript is parked under
   `recovered/split-late/` (never deleted, never in the vault). Only the
   targeted item is fenced; the queue, the worker and other items continue.
3. **Halt.** Child processes of that item (the transcriber, a pass) are tracked
   in `item_children`; `wax split` signals their process trees (TERM, then KILL
   after 5 s). It then waits (bounded, `--halt-timeout-s`, default 180) for the
   item's flock, which the worker holds for the whole item, so getting it proves
   in-flight work has settled. On timeout the fence is lifted and nothing is cut.
4. **Settle.** Under that lock the item row is re-read (it may have progressed or
   moved), the audio's sha256 is verified against the detection run, the parts
   are cut and the manifest written. An unfinished original moves to
   `skipped/split/<id>/` (item state `skipped`, cause `split`); an original
   already parked in `archive/` stays where it is. A `complete` item is not fenced
   and its transcript is untouched.

The fence is permanent for a split item: a worker that picked the item just
before the fence went up can still arrive after the split settled, and must be
refused. Reprocessing the unsplit original is therefore not offered; submit the
parts instead. A re-dropped copy of the original stays in the inbox as a visible
`skipped` item (same content, same item id) and is never processed.

Resume policy: there is none. A split item is not retried automatically;
`wax retry` refuses it (it is `skipped`), and the sweep ignores fenced items.
The operator decides which held parts to submit.

### Old daemon: refused, never half-done

A waxd that was started before the fence existed keeps running the pre-fence worker until it is
restarted. That worker never reads `item_fences`, does not register its transcriber or pass children
(`halt_children` finds nothing to stop) and publishes whatever returns. Run the new `wax split` on an
item such a daemon is processing and the fence is raised for nothing: the CLI waits `--halt-timeout-s`
(default 180 s) on the item flock while the old worker keeps transcribing, then either times out (nothing
cut, exit 2) or, if the item finishes inside the window, cuts a recording whose unsplit transcript has
already been published. A queued item is no safer: the old worker polls the inbox every 5 s and can claim it
between the CLI's checks.

So the halting path checks the daemon first (`fence.require_fence_support`, called from `split._run_item`
before anything is written). A new waxd puts `daemon: {pid, capabilities: ["item-fence/1"]}` in every
snapshot it serves on `var/waxd.sock` and mirrors into `var/state.json`. The CLI finds the running waxd
from `var/waxd.singleton.lock` (the pid is verified against `/proc/<pid>/cmdline`), asks the socket, and
falls back to the mirror only when the socket does not answer and the mirror names that same pid. A running
daemon that does not advertise the capability gets exit 2 and:

```
refusing: the running waxd (pid N) predates the split fence (its status does not advertise item-fence/1), ...
Restart waxd first (`systemctl --user restart waxd` ...), then run `wax split` again.
Nothing was fenced, halted, cut or moved.
```

What the check applies to: an item that needs a halt (any state except `complete` and `skipped`, so
`pending`, `archived`, `transcribed`, `enrichment_pending`, `failed`, `suspect` too, and an unregistered
inbox file). What it never applies to: `--dry-run` and the no-boundary outcome (read-only analysis comes
first), a live capture (nothing is halted; the stop is the same one `wax rec stop` makes), a `complete` or
`skipped` item, a split that is already settled, and a host with no waxd running (there is no worker to
race). There is no override flag.

## Evidence

`docs/evidence/heyma-18/validation-2026-11-13/` is the recording-safe validation under a real waxd (README.md
has the gap matrix). `docs/evidence/heyma-18/` holds the real command output for every case
(`cli-cases.txt`), the live-capture and in-processing runs, the fixture
characteristics, and the pytest results. Tests: `tests/split_test.py`
(detection, cutting, provenance, submit, CLI exit codes), `tests/split_fence_test.py`
(fence, halt, late result, queue isolation, concurrency with a real worker thread, and a pass
subprocess still running when the fence goes up), `tests/split_daemon_guard_test.py` (an old waxd is refused),
`tests/split_live_test.py` (a real ffmpeg encoder as the live capture).

## Not covered

Transcripts already published for a recording that was split after completion
stay as they were. So does the note the transcriber had already written when the
split reached an item in `transcribed` or `enrichment_pending` (a pass was still
running): the pass is stopped and recorded `skipped` / `item_split`, its result is
never applied, but the unsplit transcript is already in the vault (validation
scenario D). A transcript that arrives *after* the fence is parked, never published. The unsplit original is not uploaded to S3 by `wax split`
(archive it with `wax archive <path>` if it only exists locally); parts are
backed up through the normal inbox path when submitted.
