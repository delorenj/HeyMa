# The `wax` CLI and mise wrappers

`wax` is the one operator surface. Every command accepts `--json` at any position (`wax --json catalog` and `wax catalog --json` are equal). Resolve it as `~/HeyMa/bin/wax` (`~/.local/bin/wax` points there and is first on PATH). `/usr/bin/wax` is a stale 2026-09-03 copy: never trust it. The tree below was read from the argparse definition in `components/wax/bin/wax` on 2026-11-05; `components/wax/tests/skill_hub_test.py` fails if a subcommand is missing here.

Exit codes: 0 clean, 2 failure or unhealthy where noted. There is no exit 3 (WAX-DESIGN mentions one; it is not implemented). `wax reset` and `wax pipeline start|stop` do not exist.

## Capture

| Command | What it does |
|---|---|
| `wax rec start [--label L] [--bitrate 32k]` | Begin a capture. Writes the intent sentinel before the first audio byte |
| `wax rec stop [rid]` | Stop and finalize a capture |
| `wax rec cancel [rid]` | Discard a capture |
| `wax rec salvage [rid]` | Recover a capture left by a dead encoder or daemon |
| `wax rec toggle [--label L]` | Start if idle, stop if recording. The hotkey entry; exit 2 when refused |
| `wax rec quiesce` | Idempotent session-shutdown hook; idle is success |
| `wax rec list` | List captures |

The hotkey must call `~/.local/bin/wax-toggle` (symlink to `components/wax/bin/wax-toggle`), never a PATH lookup of `wax rec toggle`.

## State and inspection

| Command | What it does |
|---|---|
| `wax state [stream\|inbox\|both] [--cold]` | Derive a machine's state from disk. `--cold` ignores the live mirror. Exit 2 on `error` or `error-partial` |
| `wax status` | Both machines, queue, pass and diarization health. Exit 2 when unhealthy |
| `wax history [-n 20] [--machine stream\|inbox\|item]` | Append-only transition log |
| `wax items` | Ledger item states, all-time. Includes rows whose file is gone, so it can show hundreds of "pending" beside an empty queue (214 on 2026-11-05) |
| `wax queue [--clear-completed]` | The tray's queue rows; `--clear-completed` removes finished rows |
| `wax doctor [--quiet]` | Resolved config plus a probe of every dependency. Exit 2 fail, 1 warn, 0 clean. Warnings are informational, not a gate |

## Inbox control

| Command | What it does |
|---|---|
| `wax skip <item_id>` | Preserve a queued item without processing it (`skipped/`, reason `operator_skip`) |
| `wax retry [item_id] [--all]` | Requeue preserved failed or suspect inbox items |
| `wax drain [-n 1]` | Process the inbox now, one-shot |
| `wax reconcile --rebuild` | Rebuild the ledger from durable sources. Without `--rebuild` it prints "pass --rebuild" and exits 2. Never run it before the cause is known |
| `wax migrate --plan\|--dry-run\|--apply` | Fold legacy directories into the one inbox. Refuses with none of the three flags |
| `wax archive <file> [--audit]` | Upload to S3 and verify. See [archive](archive.md) |
| `wax transcribe <file>` | Transcribe one file with the sanity gate. See [transcription](transcription.md) |

## Enrichment passes (`wax ep`)

| Command | What it does |
|---|---|
| `wax ep list` | The registry: every pass and its switches |
| `wax ep status [item]` | Per-pass state for one item, or all |
| `wax ep run <slug> <item> [--attempt N] [--live]` | Run one pass. `--live` uses the live registry definition and is for finished items only. Ignores the operator switch |
| `wax ep run-all <item>` | Run every planned pass for an item |
| `wax ep run-selected <item> <slug>...` | Rerun the named failed passes (what the tray's retry row calls) |
| `wax ep replan <item> [--apply]` | Move ONE unfinished item's frozen plan onto the current pipeline. Dry run by default. Refused when the item completed, the authority is ambiguous, the pass set would change, or a completed or skipped pass would change definition |
| `wax ep enable <slug>` | Operator switch on: removes the slug from the `var/settings.json` overlay |
| `wax ep disable <slug>` | Operator switch off: the pass is `skipped` with `operator_disabled` |
| `wax ep sweep [--max-attempts 3] [--slug S] [--dry-run] [--limit N]` | Retry passes whose latest attempt failed. Exit 2 if anything failed |

## Pipelines and catalog

| Command | What it does |
|---|---|
| `wax catalog` | Safe discovery snapshot (passes plus pipelines; no env, bodies or stderr). Holocene's only read |
| `wax pipeline list` | Compiled pipelines with version and enablement |
| `wax pipeline show <name>` | One frozen definition, with registry drift |
| `wax pipeline bindings [name]` | Folder bindings and their last scan |
| `wax pipeline compile [name...]` | Compile authored YAML into an immutable version. No names compiles ALL YAML, replacing n8n-sourced pipelines. Exit 2 on failure |
| `wax pipeline import <export.json> [--name N]` | Compile an authored n8n export. Identical inputs are a no-op |
| `wax pipeline enable` | Historical worker toggle: touches `var/pipeline.enabled` |
| `wax pipeline disable` | Removes `var/pipeline.enabled` (worker stops claiming) |
| `wax pipeline status` | Reports the worker toggle |

Detail: [pipelines](pipelines.md).

## Documents

| Command | What it does |
|---|---|
| `wax documents list [--binding B] [--state S]` | States: pending, processing, processed, failed, seeded, trashed, gone |
| `wax documents scan [pipeline]` | Scan bindings now. Naming a pipeline runs it even when it is `enabled: false` |
| `wax documents backfill <pipeline> [--limit N]` | Explicit history tool for seeded documents |
| `wax documents run <pipeline> <file> [--binding B]` | Enrich one Markdown file now |
| `wax documents retry [pipeline] [--doc doc-<16hex>]` | Re-arm exhausted receipts |
| `wax documents sweep-empty [--dry-run]` | Trash empty notes now; records nothing |
| `wax documents sweep-untitled [--dry-run]` | Title or trash `Untitled*.md` now; records nothing |

Detail: [documents](documents.md).

## Transcript store

| Command | What it does |
|---|---|
| `wax transcripts sync [--dry-run] [--check] [--embed] [--limit N]` | Project queued snapshots and reconcile the vault into Postgres. `--check` exits 2 on drift |
| `wax transcripts search <query>... [-n 10] [--since D] [--until D] [--classification C] [--tag T] [--project P] [--origin O] [--min-duration S] [--max-duration S] [--min-words N] [--lexical]` | Hybrid search over latest versions. Classification: monolog, meeting, phone_call, other. Origin: wax, hermes-stt, legacy |
| `wax transcripts show <ref> [--version N] [--raw]` | One transcript, latest or a version |
| `wax transcripts history <ref>` | Every version of one transcript |
| `wax transcripts migrate [--dry-run]` | Apply schema migrations |
| `wax transcripts embed [--limit N]` | Embed pending chunks |
| `wax transcripts link-harvest [PATH...] [--report] [--dry-run]` | Load px harvest artifacts into `tx.harvest_*`. Default `$WAX_HARVEST_DIR` |
| `wax transcripts browse [--view K] [--include-hermes] [--limit N] [--since D] [--until D] [--project P] [--id N]` | Holocene's read-only feed (ro role, no bodies). `--view` is a classification, `unsorted` or `all`; limit defaults to 1000, max 2000 |

Detail: [transcriptions-store](transcriptions-store.md).

## Failures, events, calls, dictionary

| Command | What it does |
|---|---|
| `wax failures list [--since 7d] [--grace 1h]` | Recent failures grouped by (kind, slug, reason). Read-only; `wax failures` alone does this |
| `wax failures sweep [--since 7d] [--grace 1h] [--max 5] [--dry-run]` | File one urgent HEYMA ticket per signature via `px`. Deduped in `var/wax.db`; not scheduled. Exit 2 on px or collector errors |
| `wax events [backlog\|drain]` | Outbox backlog (default) or publish it now |
| `wax calls sync [--since 2d] [--no-fetch]` | Reconcile CDRs, recordings and webhook deliveries from the Telnyx API |
| `wax calls event [--file F] [--no-fetch]` | Ingest one webhook event (JSON on stdin) |
| `wax calls backfill [--dry-run] [--min-seconds 3]` | Recover call rows from history |
| `wax calls list [--limit 30] [--outcome O]` | Outcome: answered, voicemail, missed, declined, no_answer, failed |
| `wax calls show <ref>` | One call with legs, recordings and events |
| `wax dict add <term> [--about T] [--heard FORM]... [--section S] [--update] [--force] [--dry-run] [--no-scan] [--no-commit]` | Add a term to `Dictionary.yaml` |
| `wax dict heard <term> <form>...` | Add mis-hearings to a term |
| `wax dict remove <term> [--heard]` | Remove a term, or only its heard forms |
| `wax dict list [filter]` | List terms |
| `wax dict show <term> [--no-scan]` | One term and how it fares against the vault |
| `wax dict fix <term> [--dry-run] [--limit N]` | Rewrite existing notes for a term |

Detail: [failures](failures.md), [events](events.md), [calls](calls.md), [passes/dictionary](passes/dictionary.md).

## mise wrappers (`mise.toml` at the repo root)

Run as `mise run <task>`, or in a headless session `mise exec -- ./bin/wax ...`.

| Task | Runs |
|---|---|
| `wax:doctor` | `wax doctor` |
| `wax:status` | `wax status` |
| `wax:sweep` | `wax ep sweep` |
| `wax:retry` (alias `retry`) | `wax retry` |
| `wax:dict` | `wax dict` |
| `wax:failures` | `wax failures` |
| `wax:test` | `python3 -m pytest tests` in `components/wax` |
| `wax:test:pg` | `/usr/bin/python3 -m pytest tests/transcriptions_pg_test.py -q` (real Postgres; skips without Docker) |
| `wax:logs` | `journalctl --user -u waxd.service -n 200 -f` |
| `wax:transcripts:sync` | `wax transcripts sync` |
| `wax:transcripts:search` | `wax transcripts search` |
| `wax:transcripts:migrate` | `wax transcripts migrate` |
| `wax:diarization:install` | `components/wax/deploy/install-diarization` (rebuild the CUDA Sortformer venv) |

AGENTS.md omits `wax:dict`, `wax:failures` and `wax:diarization:install`; this list reflects `mise.toml` on 2026-11-05.
