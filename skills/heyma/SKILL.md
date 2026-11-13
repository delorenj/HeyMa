---
name: heyma
description: 'HeyMa / Wax audio pipeline on big-chungus (record, archive, transcribe, enrich). Covers waxd, the wax CLI, Ctrl+\ hotkey, tray and HeyMa window, inbox/dropoff, Syncthing and Telnyx S3 intake, Telnyx calls and voicemail, S3 backup-first archive, whisper, Sortformer diarization, enrichment passes and the model chain, Dictionary.yaml, pipelines and document bindings, the Postgres transcriptions store, Bloodbank events, wax failures tickets, n8n-nodes-heyma, Holocene Wax tab, vinyl boundary. Use for diagnosing or changing any of it. Symptoms: recorded but no transcript, pass failed, tray green but nothing happens, stuck inbox item, missing call, wrong word in transcript. Retrieval: get the latest transcript, get my transcript, last transcript for this project, today''s transcripts, yesterday''s transcripts, this week''s transcripts. Cleanup: delete junk/voicemail/empty transcripts; vault sweep of temp, scratch, untitled and duplicate notes.'
---

# HeyMa (Wax)

HeyMa is the Wax audio pipeline: record, archive, transcribe, enrich. One daemon, `waxd` (systemd user unit `waxd.service`), owns all of it. State lives beside the code under `WAX_ROOT` (default `~/HeyMa`): `stream/`, `inbox/`, `dropoff/`, `archive/`, `var/wax.db`. The `wax` CLI is the operator and automation surface; n8n, Holocene and the tray only invoke it. This hub is the router: read the rules, ask the system, then open exactly one route.

## Rules on every route

- **Backup-first, never delete.** Audio is uploaded to S3 and verified before transcription, and the source is never deleted. Never remove or move audio to "fix" something.
- **Green is not evidence.** Every sub-stage degrades by returning empty. A green tray or a quiet `wax status` proves nothing; ask the ledger.
- **waxd is the single owner.** No second watcher, archiver, transcriber or enricher. n8n never watches files or runs pass scripts. `~/audio/*` is a retired runtime root.
- **No second scheduler.** Candybar owns recurring time. Never publish the n8n authoring workflows, add a systemd timer, or add a cron or sleep loop.
- **Never write `dropoff/`.** It is the Syncthing receive-only feed; waxd only copies out of it. Write `inbox/` instead.
- **No Anthropic model, anywhere** (operator order 2026-10-22). Not in pass env, bin defaults, doctor rows, provisioning or pipelines.
- **No credential values.** `op://` item-UUID references or environment only. Never print a token.
- **Read-only until the cause is known.** No `wax reconcile --rebuild`, restarts or file moves before diagnosis.
- **Code beats docs.** `~/HeyMa` is the deployed checkout. `~/code/HeyMa` (STALE-CHECKOUT.md) and `/usr/bin/wax` (`/opt/wax`, 2026-09-03) are stale. Resolve paths from the running daemon, not from a doc.

## Ask the system first

| Question | Ask |
|---|---|
| Is anything wrong end to end? | `wax doctor` (exit 2 fail, 1 warn). `mise run wax:doctor` |
| What is the daemon doing now? | `wax status` |
| What failed recently? | `wax failures` |
| Why is this item stuck? | `wax ep status <item>`, `wax history`, `wax queue` |
| What is the real item history? | `wax items` (all-time counts, not the live queue) |
| What graph will run? | `wax catalog --json`, `wax pipeline list` |
| What did the daemon log? | `journalctl --user -u waxd.service -n 200` |
| What does the ledger say? | `sqlite3 "file:$HOME/HeyMa/var/wax.db?mode=ro"` |
| Is the transcript store behind? | `wax transcripts sync --check` |
| What is the latest transcript (overall or for a project)? | `wax tx [PROJECT...]` |
| Which transcripts are dated today or yesterday? | `wax tx --today`, `wax tx --yesterday` |
| Which notes in `~/d/Transcripts` are junk (voicemail, empty, "Hello? Hello?")? | `wax transcripts prune` (dry run; `--apply` trashes, `unprune` undoes) |
| What would the vault-wide tidy trash, title, consolidate or move? | `wax vault sweep` (dry run; `--apply` acts, `wax vault unsweep --run` undoes) |
| Did the call land? | `wax calls list`, `wax calls show <ref>` |
| What does the Telephony page show (shadow only)? | `wax --json calls telephony`, `bin/telephony --json status` |

## Routes — read exactly one, then come back

| When you are… | Read |
|---|---|
| Orienting: layout, units, processes, state machines | [system-map](references/system-map.md) |
| Looking up a `wax` command, flag or `mise run wax:*` task | [cli](references/cli.md) |
| Recording, hotkey, tray, HeyMa window, stream states, `wax split` (a recording left running) | [capture](references/capture.md) |
| Tracing how a file reaches `inbox/` (Syncthing, Telnyx S3, drops) | [intake](references/intake.md) |
| Checking S3 backup, verification, recovery | [archive](references/archive.md) |
| Debugging whisper, diarization, channel split, item states | [transcription](references/transcription.md) |
| Triaging failures, `wax failures`, HEYMA tickets | [failures](references/failures.md) |
| Working on enrichment passes generally | [passes/index](references/passes/index.md) |
| Looking up a pass, its slug, state or requirements | [passes/catalog](references/passes/catalog.md) |
| Changing or debugging the model chain and gateway | [passes/model-chain](references/passes/model-chain.md) |
| Fixing a mis-heard word, `wax dict` | [passes/dictionary](references/passes/dictionary.md) |
| Who is speaking, naming a voice, `wax voices` (`wax voices tui` to name by ear), `~/d/Voices` | [passes/voice-id](references/passes/voice-id.md) |
| Compiling, importing, replanning pipelines | [pipelines](references/pipelines.md) |
| Document bindings, hygiene, PDF intake | [documents](references/documents.md) |
| Vault-wide tidy: temp/scratch/empty files, Untitled chains, copies, empty dirs | [vault-sweep](references/vault-sweep.md) |
| Retrieving the latest transcript, a project's transcript, or today's/yesterday's/this week's transcripts | [transcript-retrieval](references/transcript-retrieval.md) |
| Postgres `tx` store, search, backfill, lag | [transcriptions-store](references/transcriptions-store.md) |
| Phone calls, voicemail, call-metadata, call log | [calls](references/calls.md) |
| Telephony page, local ringing, shadow spam rules (no live hangup) | [telephony](references/telephony.md) |
| Bloodbank events, outbox, `task.exhausted` | [events](references/events.md) |
| Holocene Wax tab, read-only boundary | [control-surface](references/control-surface.md) |
| n8n nodes, authoring workflows, releases | [n8n](references/n8n.md) |
| Env vars, systemd units, provisioning, deploying a change | [config-and-deploy](references/config-and-deploy.md) |
| Sibling components, tarballs, the vinyl boundary | [satellites](references/satellites.md) |
| An incident: something is broken and the cause is unknown | [doctor](subskills/doctor/SUBSKILL.md) |
| Past incidents and how they were found | [incident-log](subskills/doctor/references/incident-log.md) |
| Adding or changing an enrichment pass | [create-enrichment-pass](subskills/create-enrichment-pass/SUBSKILL.md) |

## Common combinations

| Task | Read, in order |
|---|---|
| Recorded but no transcript | doctor, intake, capture, transcription |
| A pass failed or an item is stuck | doctor, failures, passes/catalog, pipelines |
| Changing a model chain | passes/model-chain, pipelines (re-import, then replan), config-and-deploy |
| Adding a pass | create-enrichment-pass, passes/catalog, pipelines, n8n |
| Call missing or not announced | calls, n8n, events |
| New Bloodbank consumer | events, then the `bloodbank-integration` skill |
| Wrong word in a transcript | passes/dictionary, transcriptions-store |
| Shipping a code change | config-and-deploy, cli (`wax:test`), doctor |
| Holocene shows DOWN or stale | control-surface, system-map |

## Not here

| Topic | Go to |
|---|---|
| Vinyl dictation (separate app, own pipeline) | the `vinyl` hub: `components/vinyl/skills/vinyl/SKILL.md` (submodule) |
| Writing or fixing skills, the hub standard | `skillex-skill-registry` skill, `~/code/skillex/docs/skills/hub-skill-standard.md` |
| Generic n8n node, expression or validation help | `n8n-workflow-patterns`, `n8n-node-configuration`, `n8n-mcp-tools-expert` skills |
| Bloodbank subjects, schemas, NATS | `bloodbank-integration` skill |
| Scheduling a recurring job | `candybar` skill |
| The AutomaticAI gateway and its routes | `automaticai-provider-gateway` skill |
| Tickets and project ids | `pilot`, `project-jangler` skills |
| Holocene app code | `~/code/33GOD/holocene/AGENTS.md` |

## Gotchas

- `wax items` counts history, so "214 pending" can sit beside an empty queue → `references/cli.md`
- Bare `wax pipeline compile` replaces the n8n-sourced pipelines with the YAML ones → `references/pipelines.md`
- Editing `passes.d` never changes a compiled pipeline, and existing plans never migrate; re-import, then `wax ep replan` → `references/pipelines.md`
- `operator_disabled` is a skip, not a failure → `references/passes/catalog.md`
- Store lag is not a pass failure; the store never fails a pass → `references/transcriptions-store.md`
- `var/wax.db` is WAL: its mtime lies → `subskills/doctor/SUBSKILL.md`
- waxd holds `var/waxd.singleton.lock`; `var/waxd.lock` is the CLI capture lock → `references/system-map.md`
- As of 2026-11-05 the chain really is `auto/fast`, then free; GLM and DeepSeek members are unlisted for the token → `references/passes/model-chain.md`
- `telnyx-s3-intake` has no timer (deleted 2026-11-10); Candybar runs it → `references/intake.md`
- `wax-calls-sync` manifest says `enabled = false`, the Candybar registry says enabled → `references/calls.md`
- Restart `waxd.service` after `src/` changes; the encoder scope survives → `references/config-and-deploy.md`
- Events that WAX-DESIGN lists but nothing emits, and `wax reset` does not exist → `references/events.md`
