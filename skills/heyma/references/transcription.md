# Transcription: Whisper, the sanity gate, channel split and diarization

Transcription is two separate steps. **ASR** (faster-whisper, GPU) runs inside
the worker and writes the transcript note plus durable timed ASR. **Diarization**
(Sortformer, CUDA) is a *pass* that runs afterwards and never repeats Whisper.
Verified against `bin/transcribe`, `scripts/transcribe.py` and
`components/wax/src/wax/{transcribe_adapter,sanity,channels,diarization_pass}.py`
on 2026-11-05.

## The ASR step

```
worker.process -> transcribe_adapter.transcribe -> nice -n 15 <transcribe> <audio> --no-diarization --asr-output <logs>/asr.json
```

| Fact | Value |
|---|---|
| Entry point | `~/HeyMa/bin/transcribe`, a self-locating shim. `waxd.service` pins `WAX_TRANSCRIBE` absolutely, so PATH never decides which code runs. `~/.local/bin/transcribe` has been a symlink to the repo shim since 2026-10-18 (an older doc calls it a modified copy; that was true only on 2026-10-17). |
| Engine | `scripts/transcribe.py`, faster-whisper, default model `large-v3`, `beam_size=5`, `vad_filter=True` |
| Device | `--device auto`: CUDA float16 when there is enough free VRAM, else CPU int8. The floor is `TRANSCRIBE_MIN_FREE_VRAM_MB` (default 5000 MB for large-v3, smaller for smaller models), waiting `TRANSCRIBE_VRAM_WAIT_SECS` (30 s) for a transient spike to clear. cuDNN is loaded in-process because `LD_LIBRARY_PATH` is dropped by n8n, mise and uv. |
| Output | Exactly one stdout line (the markdown path); logs go to stderr. Per-item logs live in `logs/<item>/transcription.<attempt>.log`. |
| Landing place | `~/d/Transcripts/` (`WAX_VAULT`, really `~/code/DeLoDocs/Transcripts`), named `YYYYMMDD-HHMMSS-<slug>.md`. |
| Scratch audio never reaches the vault | An ad-hoc `bin/transcribe` run on audio under `/tmp`, `/var/tmp`, `$TMPDIR`, `~/.claude/tmp`, `~/.cache` or a `~/.hermes/**/cache` or `audio_cache` dir is treated as `--ephemeral` for **output placement**: the shim passes `--output ${TRANSCRIBE_EPHEMERAL_DIR:-/tmp/ephemeral_transcripts}/<stem>-<pid>.md` and says `[scratch] ...` on stderr (the file is `.json` under `--json`, as `transcribe.py` names it). The remote branch does the same on both hosts: it tells the remote `transcribe.py` to write into `/tmp/transcribe-staging/` (removed on exit) instead of that host's `~/d/Transcripts`, and copies the transcript back to the ephemeral dir instead of `$TRANSCRIPTS`. `scripts/transcribe.py` itself never reads `$TRANSCRIPTS`: with no `--output` it writes `~/d/Transcripts/<stem>.md`, which is how `tmp*.md` and `proof.md` reached the vault in 2026-11. An explicit `-o/--output` always wins; `--archive-s3`, the fast lane and diarization are unchanged (only `--ephemeral` skips S3). Audio under `$WAX_ROOT` (else the legacy `$WAX_AUDIO_ROOT`, else `~/HeyMa`: `wax/paths.py`'s order; `inbox/`, `stream/`), where waxd works, is never scratch, and waxd's command (`--no-diarization --asr-output`) carries no flag the guard keys on. Pinned by `tests/test_transcribe_scratch_output.py` (root suite) and `components/wax/tests/transcribe_scratch_guard_test.py`. |
| Limits | `MAX_AUDIO_FILE_SIZE_FOR_TRANSCRIPTION` = 300MB and `MAX_AUDIO_DURATION_FOR_TRANSCRIPTION` = 3h, both exclusive; over either the item is archived then skipped ([archive](archive.md)). The adapter repeats the check so a direct call cannot bypass it. |
| Timeout | 24 h for the child process |
| Concurrency | One item at a time (`worker.process` is serialized) |
| Archive mode of the shim | `bin/transcribe --archive-s3` is a legacy standalone mode whose header says transcription "still proceeds" after a failed upload. That is **not** the daemon's path: `worker.process` archives first and stops on failure. |

The adapter always passes `--no-diarization`; the transcript is published without
speaker labels, and the `diarization` pass adds them later. The WAX_DIARIZATION
knob is three-state: unset is "default", a truthy value (the unit ships `1`) is
"required" (a degraded run is logged as an error but the transcript still
publishes), a falsy value (`0|false|no|off`) is "disabled" and the pass records
`explicitly_disabled`.

## The sanity gate

After the child exits, `sanity.check` compares an independent `ffprobe` duration
of the audio with the duration faster-whisper says it decoded. It fails closed.
On failure the note is moved to `<stem>.suspect.md`, the item becomes `suspect`,
and the audio stays in the inbox.

| `reason_code` | Meaning |
|---|---|
| `duration_mismatch` | ASR covered less than 95% of the container (tolerance `max(30 s, 5%)`). Born of a 16-second transcript of a 16.5-hour file. |
| `empty_transcript` | Nothing was transcribed. |
| `audio_unprobeable`, `asr_duration_missing`, `asr_duration_unparseable` | Cannot prove coverage, so do not publish. |
| `source_changed` | The audio changed size or mtime while it ran. |

The gate is on container durations, deliberately not on words per minute or the
last segment's end: with VAD on, a three-hour recording that goes quiet after 25
minutes is legitimate. Retry a preserved item with `wax retry [<item>]`.

## Diarization

`diarization` is a normal pass (see [passes/catalog](passes/catalog.md)): kind
`diarization`, `after: [classification]`, `when: classification not_equals
monolog`, timeout 86400 s, `body_mutation: compare-and-replace`. A transcript
classified `monolog` skips with `condition_unmet` (operator order 2026-10-27);
`meeting`, `phone_call`, `other` or a missing classification (a failed pass or a
v2 abstain) still diarizes, so a classifier outage never costs speaker
attribution.

| Fact | Value |
|---|---|
| Model | NVIDIA Sortformer, through the tracked, side-effect-free `wax.diarization_sortformer` adapter (the vendored `whisperlivekit` is gone) |
| Inference | NeMo's own `forward_streaming` over the whole file (it owns chunking, context and the speaker cache) with the model card's high-latency batch setting (`chunk_len 340`, right context 40). Mel features are computed on the CPU, because NeMo's GPU STFT costs about 1 GB of VRAM per audio hour; the 83-minute 1878 peaked at 0.97 GB. About 10 s per hour of audio. Never hand-roll `forward_streaming_step` again: the old one-second loop mis-stated its context and overrode `subsampling_factor`, which smeared turns and invented speakers (2026-11-09) |
| Turns | A frame is speech only when a speaker's probability clears 0.5 (`wax.speaker_attribution.turns_from_frames`); silence belongs to nobody |
| Attribution | **Per word, not per segment.** The pass force-aligns the stored ASR text to the audio (`wax.word_alignment`, torchaudio MMS_FA, ~1.2 GB, cached as `~/.cache/torch/hub/checkpoints/mms_fa_ctc_alignment_mling_uroman.pt`), gives each word the speaker it overlaps, absorbs one-word flickers and splits a Whisper segment where the speaker changes. Speakers are numbered by first word, so labels have no gaps. Without the aligner it interpolates word times by length (worse, never fatal); evidence `segments_word_aligned` of `segments` shows which |
| Whisper word timestamps | **Off on purpose.** With 5.2 GB free (above the 5 GB floor) large-v3 decoded fine and OOMed with `word_timestamps=True`; alignment runs after Whisper exits instead |
| Runtime | `~/HeyMa/.venv-diarization` (`DIARIZATION_PYTHON` overrides). Rebuild a deleted or damaged one with `mise run wax:diarization:install` (`components/wax/deploy/install-diarization`) |
| Device | **Required and GPU-strict by default**: `WAX_DIARIZATION=1`, `WAX_DIARIZATION_DEVICE=cuda`. Independent of Whisper's device, so an ASR CPU retry cannot move Sortformer off CUDA |
| Input | The durable timed ASR (`logs/<item>/asr.json`, checked against the item's `source_sha256`). It never repeats Whisper |
| Output | `diarized: true`, `diarization-requested`, `diarization-device-requested`, `diarization-device` (the proven device, with GPU name) and a replaced body. Requested and actual device are recorded |
| Human edits | If the body no longer hashes to the stored `body_sha256`, the pass records `skipped`/`human_edits` and leaves the human text |
| Failure | Raises `diarization_failed` (for example CUDA out of memory at Sortformer load). Needs about 3000 MiB free VRAM for Sortformer plus ~1.3 GB for the aligner (an aligner OOM only degrades to interpolation); on 2026-11-05 the 3090 had 7.6 GB free |
| Doctor proof | `wax doctor` and `doctor.sh` run a **real Sortformer CUDA forward pass**, not an import-only check |

Body edits by passes other than speaker labelling no longer mark a transcript as
diarized (2026-11-04). A recurring trap: with a stale `diarized: false` do not
re-run ASR; run `wax ep run diarization <item>` (a frozen plan, per
[passes/index](passes/index.md)).

Transcripts diarized before 2026-11-09 carry the old loop's turns and
segment-level blocks (two people in one block, the next block starting
mid-sentence). Re-running `diarization` re-renders from `asr.json`, so it
drops dictionary corrections (run `dictionary-correction` after it) and it
skips a hand-edited body (`human_edits`). To compare without touching the
note, load `scripts/transcribe.py` in `.venv-diarization` and call
`diarize_local`, `align_words`, `to_markdown`, as the pass does.

## Dual-channel phone calls

A Telnyx dual-channel recording puts the first leg on channel A and the rest on
channel B, so the carrier already knows who spoke. `scripts/transcribe.py` then
transcribes each channel separately and interleaves by time, which cannot flip a
speaker mid-sentence the way Sortformer over a mono mixdown can
(`components/wax/src/wax/channels.py`).

| Rule | Detail |
|---|---|
| Detect | File name starts `telnyx-` and **not** `telnyx-vm-` (voicemails are mono); exactly two audio channels; **both** channels above -60 dB RMS (old one-sided recordings have ~-90 dB digital silence on channel 1, so they take the mono path) |
| Labels | The call's two parties (`wax.calls.channel_parties`), else `Channel A`/`Channel B` |
| Stamps | `channel-attributed: true` and `diarized: true` on the note (mirrored to `wax.channel_attributed`) |
| Downstream | The `diarization` pass reports `skipped`/`channel_separated` instead of running Sortformer, before it spawns the GPU venv |
| Language | One call is one language: the second channel is pinned to the first's language when its probability is at least 0.6 |
| Rollback | `WAX_CHANNEL_SPLIT=0` |

Call identity and the call log are in [calls](calls.md); Telnyx audio intake is
in [intake](intake.md).

## Failure signatures

| You see | It is | Go to |
|---|---|---|
| Every transcript `diarized: false`, no error anywhere | Diarizer runtime broken or deleted (shape 3), or the transcriber is a second checkout (shape 2) | `doctor.sh --layer transcription`; [incident log](../subskills/doctor/references/incident-log.md) 2026-08-12 and 2026-08-21 |
| Item `suspect`, `*.suspect.md` beside the note | Sanity gate | table above |
| Item `failed` with `transcribe_failed` | The child exited non-zero or produced no path; read `logs/<item>/transcription.<n>.log` | `wax retry <item>` after the cause |
| ASR running on CPU | VRAM floor not met for 30 s (ComfyUI or vexa holding the GPU) | `nvidia-smi`; wait or free the GPU |
| Diarization failed x3, exhausted | CUDA out of memory at Sortformer load | the HEYMA-10 recipe in the incident log: bounded retry only with at least 3000 MiB free |
| Two checkouts reported | The check resolved PATH instead of the daemon's pin | [the doctor procedure](../subskills/doctor/SUBSKILL.md) |

For how the `diarization` pass sits among the others, see
[passes/index](passes/index.md).
