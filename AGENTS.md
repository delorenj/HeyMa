# AGENTS.md

HeyMa is the **Wax** audio pipeline: record → archive → transcribe → enrich.
One daemon owns all of it. There is no n8n, no Fireflies, no `watch_audio.sh`,
no `./ingest`, and no second checkout — if a doc mentions any of those, it is
stale, and `~/audio/*` is a retired runtime root that nothing reads.

> ## Something looks wrong? Load the `heyma-pipeline-doctor` skill FIRST.
> `.agents/skills/heyma-pipeline-doctor/` — or run `mise run wax:doctor`.
> Do not start by reading code. Two week-long outages (a deleted Ollama model, a
> deleted vendored `whisperlivekit/`) both ran at 100% failure behind a green
> tray and a `wax status` that printed no errors, because every sub-stage
> degrades by returning empty instead of failing. **Green is not evidence.**

## Layout

`WAX_ROOT` (default `~/HeyMa`, i.e. this repo) is the runtime root, so state
lives beside the code. All of it is one filesystem, which is what makes
`renameat2` between `stream/` and `inbox/` atomic.

| Path | What |
|---|---|
| `stream/` | in-flight capture: `<rid>.ogg.partial` + sentinels |
| `inbox/` | the one work queue. Plain local dir, safe to write. Recursive — items live at any depth |
| `dropoff/` | Syncthing **receiveonly** device feed. waxd only ever COPIES out of it. Never write here |
| `archive/` | audio parked after S3 verify |
| `skipped/`, `quarantine/`, `recovered/` | preserved, never processed / never deleted |
| `var/wax.db` | SQLite ledger: `items · backups · transcripts · passes · outbox · transitions` |
| `var/{state.json,waxd.sock,waxd.lock}` | live mirror, RPC socket, singleton lock |
| `~/d/Transcripts/` | where transcripts land (`WAX_VAULT`); really `~/code/DeLoDocs/Transcripts` |

## waxd — the single owner

`waxd.service` (systemd **user** unit, `enabled`+`active`) holds
`flock var/waxd.lock` and is the parent of the encoder and every worker, so
transitions come from `Popen.wait()` and `renameat2()`'s return value, never
from `stat()`. Every capture writes an intent sentinel before the first audio
byte, so state is recomputable from disk by a cold process after a SIGKILL.

- Source: `components/wax/` (`src/wax/`, `bin/{wax,waxd}`, `config/`, `deploy/`).
- Repo-root `bin/{wax,waxd,transcribe}` are shims that resolve the component
  relative to themselves. `~/.local/bin/transcribe` points at `bin/transcribe`,
  and `waxd.service` pins `WAX_TRANSCRIBE` absolutely — PATH must never get to
  decide which code runs.
- Record hotkey: **`Ctrl+\`** — a GNOME custom keybinding (`custom3`) calling
  `~/.local/bin/wax-toggle` → `wax rec toggle`. The evdev design in
  WAX-DESIGN.md was never built.

## The `wax` CLI

```
wax doctor                 # start here — end-to-end diagnosis (mise run wax:doctor)
wax status                 # both state machines, queue, pass + diarization health
wax rec start|stop|toggle|cancel|salvage|list
wax items | queue | history | state <machine> [--cold]
wax drain                  # process the inbox now, one-shot
wax ep list|status|run <slug> <item>|run-all <item>|sweep
wax reconcile [--rebuild]  # rebuild the ledger from durable sources
wax archive | transcribe | migrate | skip | pipeline enable|disable | events
```

mise wrappers: `wax:doctor`, `wax:status`, `wax:sweep`, `wax:test`, `wax:logs`.

## S3: backup-first, never-delete

The audio is the irreplaceable artifact. `archive.py` uploads **before**
transcription to `s3://recordings/YYYY-MM-DD/<sha12>-<name>` (mc alias `delo`
= s3.delo.sh), then verifies — never trusting `mc cp`'s exit 0 — with 3
attempts and backoff. Size always gates; a single-part ETag is additionally
compared against a local MD5, and `verify_remote()` records *which* method
proved it, because "verified" with no method is how a 262 KB stub once passed
for a 16.5-hour recording. Keys are content-addressed from a real sha256, so
re-archiving identical bytes is idempotent. **The source is never deleted**,
S3 success or not.

Recovery: `mc ls delo/recordings/`, `recovered/` (S3-failure stash),
`dropoff/.stversions/` (Syncthing 365d staggered).

## Enrichment passes

Registry: **`components/wax/config/passes.d/*.yaml`** — resolved from the
component root via `component.PASSES`, never from cwd or a runtime dir.
Passes are INDEPENDENT: one failing never gates another, and failure is
recorded per-slug with a `reason_code` in both the ledger and the note's
`wax.passes.<slug>` frontmatter block. Enabled today: `frontmatter-stamp`,
`title-slug` (hosted OpenAI-compatible endpoint; key from `op://`, never a
file). `wax ep sweep` retries the ones whose latest attempt failed.

To add one, use the `create-enrichment-pass` skill; see
`components/wax/docs/ENRICHMENT-PASSES.md`.

## Docs & tests

- Design of record: **`components/wax/docs/WAX-DESIGN.md`** (the only copy).
- `cd components/wax && python3 -m pytest tests` — works with no PYTHONPATH
  incantation, or `mise run wax:test`.

## Skills (Skillex)

`.agents/skills/` is a **generated projection** — symlinks into this machine's
skillex checkout and pack cache, rebuilt by `mise run skills:sync` and gitignored
because every entry is an absolute `/home/<user>/…` path. Never author there; the
next sync deletes it.

Canonical skill source lives in the registry at `~/code/skillex/all-skills/<name>/`
(git: `delorenj/skillex`). To add one here: write it there, append
`{"name": …, "source": "file://…"}` to `.agents/skills.json`, then
`mise run skills:sync`. `.agents/skills.json` is machine-local, so the skill's
durable home is the skillex repo — commit and push it there.

This repo's own troubleshooting skill is `heyma-pipeline-doctor`.
