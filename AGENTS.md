# AGENTS.md

HeyMa is the **Wax** audio pipeline: record → archive → transcribe → enrich.
One daemon owns all of it. n8n may invoke Wax through the private
`n8n-nodes-heyma` control adapter, but it never watches files, archives,
transcribes, or enriches. There is no Fireflies, no `watch_audio.sh`, no
`./ingest`, and no second checkout — if a doc assigns pipeline stages to any of
those, it is stale, and `~/audio/*` is a retired runtime root that nothing reads.

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
| `stream/` | in-flight capture: `<rid>.segs/seg-NNNNN.ogg` + sentinels; a concatenated `.ogg.partial` exists only while finalizing |
| `inbox/` | the one work queue. Plain local dir, safe to write. Recursive — items live at any depth |
| `dropoff/` | Syncthing **receiveonly** device feed. waxd only ever COPIES out of it. Never write here |
| `archive/` | audio parked after S3 verify |
| `skipped/`, `quarantine/`, `recovered/` | preserved, never processed / never deleted |
| `var/wax.db` | SQLite ledger: `items · backups · transcripts · passes · outbox · transitions` |
| `var/{state.json,waxd.sock,waxd.lock}` | live mirror, raw status socket, singleton lock |
| `~/d/Transcripts/` | where transcripts land (`WAX_VAULT`); really `~/code/DeLoDocs/Transcripts` |
| Postgres `transcriptions` (schema `tx`, host cluster `localhost:5432`) | versioned transcript store: one append-only version per transcription / applied pass / backfill / vault edit, hybrid semantic + FTS search over the latest only. Code `components/wax/src/wax/transcriptions/`; design `components/wax/docs/TRANSCRIPTIONS-STORE.md`; capture queue `transcript_snapshots` in `var/wax.db` |
| `components/n8n-nodes-heyma/` | private self-hosted n8n adapter; invokes the absolute Wax CLI and owns no pipeline state |

Syncthing folder ID `audio`, displayed as **Transcription Inbox**, receives into
`dropoff/` on big-chungus. Spokes must be send-only and share it only with the
hub. The metadata-only `syncthing-audio-policy.service` accepts flat, uniquely
named recordings once and saves permanent filename exclusions, so hub deletions
cannot be downloaded again. It never writes audio or owns pipeline stages.
Configuration and verification: `components/syncthing-audio/README.md`.

Telnyx intake reads only the configured native recording prefix through AWS CLI.
`telnyx-s3-intake.service` owns transport receipts and private staging under
`var/telnyx-s3-intake/`, then delivers verified audio into `inbox/`. Wax owns every
pipeline stage; intake never edits `dropoff/`. Configuration, delivery boundaries,
and operator proof: [Telnyx S3 intake](components/telnyx-s3-intake/README.md).

## waxd — the single owner

`waxd.service` (systemd **user** unit, `enabled`+`active`) holds
`flock var/waxd.lock` and is the parent of the encoder and every worker, so
transitions come from `Popen.wait()` and `renameat2()`'s return value, never
from `stat()`. Every capture writes an intent sentinel before the first audio
byte, so state is recomputable from disk by a cold process after a SIGKILL.

- Source: `components/wax/` (`src/wax/`, `bin/{wax,waxd}`, `config/`, `deploy/`).
- `wax-capture-guard.service` quiesces an active capture before an orderly
  graphical-session stop removes D-Bus or PipeWire. A plain `waxd` restart does
  not stop the encoder scope.
- Repo-root `bin/{wax,waxd,transcribe}` are shims that resolve the component
  relative to themselves. `~/.local/bin/transcribe` points at `bin/transcribe`,
  and `waxd.service` pins `WAX_TRANSCRIBE` absolutely — PATH must never get to
  decide which code runs.
- Record hotkey: **`Ctrl+\`** — a GNOME custom keybinding (`custom3`) calling
  `~/.local/bin/wax-toggle`, a symlink to the tracked
  `components/wax/bin/wax-toggle`, which runs the `wax` beside itself (never a
  PATH lookup; `/usr/bin/wax` → `/opt/wax` is a stale 2026-09-03 copy). The
  evdev design in WAX-DESIGN.md was never built.

## The `wax` CLI

```
wax doctor                 # start here — end-to-end diagnosis (mise run wax:doctor)
wax status                 # both state machines, queue, pass + diarization health
wax rec start|stop|toggle|cancel|salvage|list
wax rec quiesce            # idempotent session-shutdown hook; idle is success
wax items | queue | history | state <machine> [--cold]
wax drain                  # process the inbox now, one-shot
wax retry [<item>]         # requeue preserved failed/suspect inbox items (all, or one)
wax ep list|status|run <slug> <item>|run-all <item>|sweep
wax ep replan <item> [--apply]   # move ONE unfinished item's frozen plan onto the current pipeline
wax reconcile [--rebuild]  # rebuild the ledger from durable sources
wax catalog                # safe discovery snapshot (passes + pipelines) for Holocene/n8n
wax pipeline enable|disable|status   # historical worker toggle, unchanged
wax pipeline list|compile|show|bindings   # versioned pipeline graphs (compile is explicit)
wax pipeline import <export.json> [--name]  # compile an authored n8n workflow export
wax documents list|scan|backfill|run|retry  # document registration/bindings/processing; retry re-arms exhausted receipts
wax documents sweep-empty|sweep-untitled [--dry-run] [--json]  # the waxd hygiene jobs, now; notes go to <vault>/.trash
wax transcripts sync [--dry-run|--check|--embed]   # project queued snapshots + reconcile the vault into Postgres
wax transcripts search <query> [--since --tag --classification --origin ... --lexical]  # latest versions only
wax transcripts show <ref> [--version N] [--raw] | history <ref> | migrate [--dry-run] | embed [--limit N]
wax archive | transcribe | migrate | skip | pipeline enable|disable | events
```

mise wrappers: `wax:doctor`, `wax:status`, `wax:sweep`, `wax:retry`, `wax:test`, `wax:logs`,
`wax:transcripts:sync|search|migrate`, `wax:test:pg` (real-Postgres store tests; skip without Docker).

**The transcriptions store never fails a pass.** `passes.run` and
`transcribe_adapter` only enqueue a SQLite snapshot and project best-effort
behind a breaker; waxd's `wax-transcriptions` thread drains, reconciles and
embeds (local Ollama `qwen3-embedding:8b`, pinned by digest, GPU-gated on the
active item, and unloaded from VRAM the moment an item starts so whisper keeps
its 5 GB floor). An outage is `transcripts lagging|error` in `wax status` and seven
`wax doctor` probes. Passwords are `op://` item-UUID references in the waxd
unit (`deploy/provision-transcriptions` creates the items and roles; dry run by
default); rollback is `WAX_TRANSCRIPTS=0`.

Diarization is required and GPU-strict by default:
`WAX_DIARIZATION=1`, `WAX_DIARIZATION_DEVICE=cuda`. Its device is independent
of Whisper's, so an ASR CPU retry must not move Sortformer off CUDA. The owned
adapter is `components/wax/src/wax/diarization_sortformer.py`; rebuild a deleted
or damaged runtime with `mise run wax:diarization:install`. `wax doctor` proves
the path with a real Sortformer CUDA forward pass, not an import-only check.
The `diarization` pass runs after `classification` and skips a transcript
classified `monolog` (`when`, `condition_unmet`; operator order 2026-10-27). A
meeting, other, or missing classification still diarizes.

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
A failing pass never stops a sibling, except through `requires:` (ordering +
gating; `after:` only orders): a dependent waits with `dependency_failed`. Failure
is recorded per-slug with a `reason_code` in both the ledger and the note's
`wax.passes.<slug>` frontmatter block. Enabled today: `frontmatter-stamp`,
`classification`, `diarization`, `clipboard-copy`, `title-slug`. Every chat
pass (`classification`, `title-slug`, and `ticketable`) calls the AutomaticAI
gateway at `https://api.automaticai.io/v1` on the scoped `heyma-wax-inference`
token (`op://`, never a file), and tries an ordered **model chain**
(`wax.provider.run_chain`): `automaticai/auto/fast`, then the explicit
subscription routes `automaticai/personal/glm-5.3-flash` and
`automaticai/personal/glm-5.3`, then `automaticai/deepseek/deepseek-flash`,
`automaticai/deepseek/deepseek-v4-pro` (metered DeepSeek; Hyper is out while the
gateway blocks it, AAI-64), then `automaticai/free`.
The chain lives in each pass YAML env (`WAX_TITLE_MODEL` +
`WAX_TITLE_MODEL_FALLBACKS`, `WAX_TICKETS_*`) with matching bin defaults. It
moves on for provider-side failures only (429, 402, 5xx, unreachable, timeout,
truncated or garbled responses, unknown model, 400, a 403 that means "outside
the token's scope", empty or unparseable content) and never for `no_api_key` or
a rejected key. The whole chain runs against one deadline taken from the
runner's `WAX_PASS_TIMEOUT_S`, so the runner never kills a pass before it
reports every member. The result's `evidence` names the member asked (`model`),
what the gateway says served it (`served_by`, `route`, `auto`) and each failed
member; the note's `wax.passes.<slug>` block records `model`, `served_by`,
`route` and `fell_through`. A pass served by an Anthropic model fails closed
(`anthropic_model_served`), and the runner refuses such a result again. **No
Anthropic model, anywhere, by operator order (2026-10-22)**: `chat_pass_routing_test`
fails on any claude/sonnet/haiku/opus/fable/anthropic model in pass env, bin and
library defaults, the doctor rows, `deploy/provision-inference`, authored
pipelines, n8n exports and frozen definitions. Kimi stays out until gateway bug
AAI-55 closes. The token's scope is owned by
`components/wax/deploy/provision-inference` (the SSOT; it skips routes the
gateway catalog lacks, so re-run it after a gateway deploy). Compiled pipelines
freeze pass env, so after changing a chain re-import the n8n release exports
(`wax pipeline import ... --name transcript-standard|doc-standard`); an
unfinished item minted under the old chain moves only by
`wax ep replan <item> --apply`, then `wax ep run`. Implemented
but currently disabled: `project-extraction` (pjangler project ids via TypeSafe
Jev) and `ticketable` (tasks per project).
`wax ep sweep` retries the ones whose latest attempt failed; the waxd worker also
sweeps on a timer (`WAX_SWEEP_INTERVAL_S`, default 900s), and a pass that
exhausts its attempts emits `bloodbank.audio.task.exhausted` (the dead-letter
signal) instead of failing silently.

`transcription.completed` waits for every planned pass. A pass can declare
`events` in its result (its YAML must list them in `emits`, and the data must
satisfy the Bloodbank schema — the runner validates, fail-closed); a completed
pass's events publish as soon as its own `requires` subgraph completes
(`finalize.publish_pass_events()`), not behind slow siblings, while the
completion stays plan-gated as the summary fact. `ticketable` becomes one
`bloodbank.audio.intake.detected` per ticket, relayed to ntfy `audio`. Jev does
not go through the AutomaticAI gateway (it has no decisions route); it goes
through the fleet's Jev ingress instead, the jev-litellm LiteLLM proxy at
`https://jev.delo.sh/typesafe/v1/systemone` (stack
`~/docker/stacks/ai/jev-litellm`), which injects the TypeSafe credential
server-side. `project-extraction` holds HeyMa's own virtual key there (alias
`heyma-wax-project-extraction`, $1/day, `op://`; never the ingress master key
or the raw TypeSafe key) and pins `jev-1.13.0`, the build its thresholds were
measured on. Per-request spend is in the ingress's LiteLLM spend log, not the
response. No pass is configured for OpenRouter. The pass stays disabled until a
calibration re-run on the ingress. `wax doctor` probes the registry, the Jev
ingress and that key, the chat-pass route served to the gateway token, and
unpublished pass events.

To add one, use the `create-enrichment-pass` skill; see
`components/wax/docs/ENRICHMENT-PASSES.md`.

## Pipelines, documents, and the control surface

Pass YAML declares `capabilities:` (`audio` and/or `document`). The audio flow
is unchanged; a **document run** (plain Markdown, content-addressed
`doc-<sha16>` per folder binding in the `documents` table, never a
`transcripts` row) only runs `document`-capable passes (others skip with
`inapplicable_capability` — and a dependency gated on a skipped pass runs),
applies frontmatter/body only, and rejects `events`. Identity is durable
against Wax's own writes (`current_sha256`), retries adopt the failed row, and
receipts are per (binding, document, pipeline, compiled definition) with an
attempt bound (`exhausted_at` dead-letters).

**Pipelines** (`config/pipelines.d/*.yaml`, or an n8n workflow export via
`wax pipeline import`) are authored graphs compiled — explicitly — into
immutable versioned definitions (`pipelines` table, sha256 `definition_id`).
The compiled definition is **frozen**: node commands/env/edges/conditions as
they stood at compile; execution and the catalog graph never consult the live
registry for a compiled pipeline (registry drift is reported beside it). A
`requires` edge naming a pass outside the graph fails compile loudly. With
exactly one compiled+enabled `trigger: transcription` pipeline, NEW audio
plans come from its frozen definition; with none, the legacy registry plan
stands; ambiguity is refused and recorded. Document bindings are
new-arrivals-only (a missing folder never claims seeded) and the worker scans
them on its own cadence (`WAX_DOCUMENT_SCAN_INTERVAL_S`, default 120s) with
scan retries for failed arrivals; `wax documents backfill` is the explicit
history tool.

**Document hygiene** (`src/wax/hygiene.py`, run by the waxd worker): the vault's
`Inbox/` (recursive, so `Inbox/Wax`) and `Sandbox/` (`WAX_DOC_HYGIENE_FOLDERS`,
each must sit inside an Obsidian vault) are kept free of dead notes. Every
`WAX_EMPTY_DOC_SWEEP_INTERVAL_S` (300s) empty notes are moved to the vault's
`<vault>/.trash/` (never unlinked; nothing in the trash is overwritten), and
`documents` rows whose file vanished become `gone`. Once per local date from
`WAX_UNTITLED_SWEEP_HOUR` (3), empty `Untitled*.md` are trashed and the rest run
through the manual `untitled-standard` pipeline (summarize, then title-slug)
and are renamed to `<title-slug>.md`; an uncompiled pipeline touches nothing.
Safety is not optional: `*.md` only, no dot-directories or dot-files, no
symlinks, mtime older than `WAX_DOC_HYGIENE_MIN_AGE_S` (300s), emptiness
re-checked under the document run lock right before the move, and unparseable
frontmatter is never "empty". `trashed`/`gone` document rows no longer count in
`passes.failed`. `wax doctor` has a "document hygiene" probe; the CLI forms run
now and record nothing.

**The control surface boundary**: Holocene's Wax tab is read-only and reads one
thing — `wax catalog --json` through the Holocene API, which allowlist-projects
it (no env, command bodies, or stderr can pass through) and reports a DOWN
source on CLI failure rather than healthy emptiness. It draws the frozen graph
(gated vs continue edges, conditions, drift), recent runs from the durable
receipts, and worker freshness (live / healthy idle / down via the worker
heartbeat). n8n (`components/n8n-nodes-heyma` — HeyMa, WaxEp, generated
per-pass `wax-pass-*` nodes, WaxPipeline) invokes the same CLI for
manual/preview operations, explicit compiles and workflow-import compiles; it
never executes pass scripts and no exported n8n trigger can join an import —
n8n can never become a second scheduler. Wax owns every artifact mutation,
receipt, retry, and schedule.

## Docs & tests

- Design of record: **`components/wax/docs/WAX-DESIGN.md`** (the only copy).
- `cd components/wax && python3 -m pytest tests` — works with no PYTHONPATH
  incantation, or `mise run wax:test`.

## Skills (Skillex)

`.agents/skills/` is a **generated discovery root**. Never author there or
replace foreign content by hand; Skillex owns activation. Canonical definitions
live in `~/code/skillex/all-skills/<name>/` (Git: `delorenj/skills`), the `all-skills`
submodule of `delorenj/skillex`. Commit/push definitions there, then advance the
Skillex parent pin. Machine-local `.agents/skills.json` selects names.

Use the installed `skillex` CLI directly (via `mise x -- skillex …` when needed),
not a `skills:*` mise task. Preview `skillex enable skill <name> --project <repo>
--dry-run --json` and inspect sync conflicts before applying. Preserve foreign
skills/host-owned system packages; do not force a root migration to add one skill.

- `wax-pipeline`: CLI-first audio/document pipeline operations, provenance,
  compiled graphs, n8n authoring and read-only Holocene.
- `create-enrichment-pass`: portable EP implementation, capabilities, result
  envelope, no-clobber and tests; runtime integration routes to `wax-pipeline`.
- `heyma-pipeline-doctor`: incident diagnosis/recovery; its legacy audio board is
  not proof of document-binding health.

If activation is blocked, load these canonical paths explicitly and report the
blocker rather than copying skill bodies into the generated root. In particular,
this checkout still has a preserved foreign `create-enrichment-pass` directory;
until Skillex can safely reconcile it, read
`~/code/skillex/all-skills/create-enrichment-pass/SKILL.md` explicitly instead of
trusting that older local discovery copy. The corresponding pipeline entry is
`~/code/skillex/all-skills/wax-pipeline/SKILL.md`.
