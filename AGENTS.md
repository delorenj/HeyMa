# AGENTS.md

Project home for the audio transcription workflow.

## Skills (Skillex)

- `./.agents/skills/` is the canonical folder for project-scoped skills.
- `~/.agents/skills/` is the canonical folder for global skills.
- Create and maintain skill source in the appropriate canonical folder; do not
  author canonical skills under `.codex/skills/`.
- For skills specific to this repository, initialize them under
  `./.agents/skills/`. Use the global folder only when global scope is intended.

## Overview

Recordings are transcribed locally with **faster-whisper** (NOT Fireflies anymore —
Fireflies was retired; ignore older docs that mention it / minio public URLs /
`~/d/Inbox`). An n8n workflow watches for new audio files and runs the transcribe
CLI, which archives the source to S3 and writes a markdown transcript.

## Ingest paths (two, both watched by the same n8n workflow)

- `./inbox` — a **Syncthing `receiveonly`** folder for **cross-device drop-off**
  (phone/mac/etc. sync recordings here). Versioning is **enabled** (staggered,
  365d) so a sync deletion is recoverable from `.stversions`.
- `./ingest` — a plain **non-synced** local dir. `~/.local/bin/watch_audio.sh`
  relays finished krecorder recordings from `~/Music` to here.

> ⚠️ Do NOT point local writers (watch_audio.sh, scripts) at `./inbox`. It is
> receive-only; files added locally to a receive-only Syncthing folder are treated
> as divergent and get **reverted/deleted** on any reconcile or folder-marker reset.
> That destroyed a recording on 2026-06-29. Local recordings go to `./ingest`.

## Pipeline (n8n workflow "Inbox → Local Transcribe (Whisper)", id `Yw0WvYW1yAU1QG49`)

1. **Watch Inbox** / **Watch Ingest (local)** — `localFileTrigger` on each dir (`add`).
2. **Call Parse File for Audio** (subworkflow `Bxgua92kxXkycFB4`) — ensures audio-only,
   yields `sourcePath`.
3. **Run Transcribe** — `~/.local/bin/transcribe "$sourcePath" --archive-s3`.
4. **ntfy Notification** — pings `ntfy.delo.sh/transcripts`.

## `transcribe` script — `~/.local/bin/transcribe` → `code/HeyMa/bin/transcribe`

Runs `scripts/transcribe.py` (faster-whisper + diarization) out of
`code/33GOD/HeyMa`. With `--archive-s3` the **backup-first, never-delete** policy:

1. **Archive the source to S3 FIRST**, before transcription, to
   `s3://recordings/YYYY-MM-DD/HHMMSS-<file>` (alias `delo` = s3.delo.sh) and
   **verify** with `mc stat`. The audio is the irreplaceable artifact.
2. If S3 fails: keep the source AND stash a copy in `$TRANSCRIBE_STASH_DIR`
   (`~/audio/recovered`, non-synced). Transcription still proceeds.
3. **The source is NEVER deleted.**
4. Transcribe → write markdown to `~/d/Notes/Transcripts/`.

## Recovery / where things live

- Source audio archive: `mc ls delo/recordings/` (== `delodrive/recordings`, same minio).
- Transcripts: `~/d/Notes/Transcripts/*.md`.
- Local safety stash (S3-failure fallback): `~/audio/recovered/`.
- Syncthing version history for inbox deletions: `~/audio/inbox/.stversions/`.
