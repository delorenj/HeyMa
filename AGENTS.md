# AGENTS.md

HeyMa is the **Wax** audio pipeline: record → archive → transcribe → enrich.
One daemon, `waxd`, owns all of it. n8n, Holocene and the tray only invoke the
`wax` CLI; none of them watches files, archives, transcribes or enriches. There
is no Fireflies, no `watch_audio.sh`, no `./ingest`, and no second checkout
(`~/code/HeyMa` is retired: `STALE-CHECKOUT.md`). `~/audio/*` is a retired Wax
runtime root (Vinyl still keeps its models in `~/audio/models`).

## Start with the hub skill

**Everything operational lives in the `heyma` hub skill: `skills/heyma/SKILL.md`.**
It holds the rules, an ask-the-system table, and one route per situation
(capture, intake, archive, transcription, passes and the model chain, the
dictionary, pipelines, documents, the transcripts store, calls and voicemail,
events, n8n, Holocene, config and deploy, satellites). Read the router, then
exactly one route.

> **Something looks wrong?** Run `wax doctor` (or `mise run wax:doctor`), then
> follow `skills/heyma/subskills/doctor/SUBSKILL.md`. Do not start by reading
> code. Two week-long outages ran at 100% failure behind a green tray and a
> clean `wax status`, because every sub-stage degrades by returning empty.
> **Green is not evidence.**

## Rules that hold everywhere

- **Backup-first, never delete.** Audio is uploaded to `s3://recordings/` and
  verified (size, plus ETag/MD5 when single-part, with the method recorded)
  before transcription. The source is never deleted, S3 success or not.
- **Single owner.** `waxd.service` (systemd user unit) is the parent of the
  encoder and every worker; transitions come from `Popen.wait()` and
  `renameat2()`, never `stat()`. Do not add a second watcher or worker.
- **Candybar is the only clock.** No systemd timers, cron, n8n schedules or
  sleep loops for Wax work (`wax-calls-sync` and `telnyx-s3-intake` run on
  Candybar).
- **Never write `dropoff/`.** It is Syncthing receive-only; waxd copies out of
  it. Write `inbox/`.
- **No Anthropic model in any Wax pass, anywhere** (operator order 2026-10-22);
  `chat_pass_routing_test` enforces it.
- **Credentials are `op://` item-UUID references** in units and config, never values.
- **PATH never decides which code runs.** Repo-root `bin/{wax,waxd,transcribe}`
  are self-locating shims; `waxd.service` pins `WAX_TRANSCRIBE`; the hotkey
  (`Ctrl+\`) runs `~/.local/bin/wax-toggle`, a link to the tracked
  `components/wax/bin/wax-toggle`. `/usr/bin/wax` (`/opt/wax`) is a stale copy.
- **The transcripts store never fails a pass**; lag shows in `wax status` and `wax doctor`.
- **Compiled pipelines are frozen.** Changing `passes.d` or a model chain means
  re-importing the n8n release exports and `wax ep replan <item> --apply` for
  unfinished items.

## Layout

`WAX_ROOT` (default `~/HeyMa`, this repo) is the runtime root, one filesystem so
`renameat2` between `stream/` and `inbox/` is atomic.

| Path | What |
|---|---|
| `stream/` | in-flight capture segments and sentinels |
| `inbox/` | the one work queue (recursive) |
| `dropoff/` | Syncthing receive-only device feed (folder `audio`) |
| `archive/`, `skipped/`, `quarantine/`, `recovered/` | parked audio; preserved, never deleted |
| `Dictionary.yaml` | words ASR mangles; edit with `wax dict` |
| `var/wax.db` | SQLite ledger (items, passes, outbox, calls, documents, …) |
| `var/state.json`, `var/waxd.sock` | live mirror and status socket; waxd's singleton is `var/waxd.singleton.lock` |
| `~/d/Transcripts/` | where transcripts land (`WAX_VAULT`) |
| Postgres `transcriptions` (schema `tx`) | versioned transcript store with hybrid search |
| `components/wax/` | the daemon, CLI, passes (`config/passes.d`), pipelines, docs, tests |
| `components/n8n-nodes-heyma/`, `components/n8n-workflows/` | n8n adapter nodes and released workflows |
| `components/telnyx-s3-intake/`, `components/syncthing-audio/` | intake services |
| `components/heyma-obsidian/`, `components/remote-audio/` | satellites |
| `components/vinyl/` | Vinyl dictation, a submodule for co-location only (not Wax; own hub `components/vinyl/skills/vinyl/`); `~/code/vinyl` symlinks here |
| `components/telephony/` | Default-off/shadow call-policy component; local Telephony page/projection, no live rejection/routing or audio suppression. Operator route: `skills/heyma/references/telephony.md` |
| `skills/heyma/` | the hub skill (this repo's only skill) |

## Docs & tests

- Design of record: `components/wax/docs/WAX-DESIGN.md` (July-era sections are
  historical; the hub is current). Pass contract:
  `components/wax/docs/ENRICHMENT-PASSES.md`.
- `cd components/wax && python3 -m pytest tests`, or `mise run wax:test`.
- `components/wax/tests/skill_hub_test.py` keeps the hub honest: every `wax`
  subcommand must appear in `skills/heyma/references/cli.md`, every pass (with
  its enabled state) in `references/passes/catalog.md`, and every link must
  resolve. **Change the hub in the same commit as the behaviour.**

## Skills (skillex ADR-0002)

This repo owns exactly one skill, the hub at `skills/heyma/`. It is a
**repo-owned link source**: `all-skills/sources.toml` declares
`[[source]] name = "heyma", mode = "link"`, and skillex links
`.agents/skills/heyma -> ../../skills/heyma`. The checkout is mapped per machine
in `~/.config/skillex/sources.local.toml` (`HeyMa = "/home/delorenj/HeyMa"`).

- Author HeyMa knowledge **in `skills/heyma/`**, as a reference
  (`references/<topic>.md`) or a sub-skill (`subskills/<name>/SUBSKILL.md`,
  never a nested `SKILL.md`). Never add a second top-level skill here, and never
  put HeyMa knowledge in `~/code/skillex/all-skills/`.
- `wax-pipeline`, `heyma-pipeline-doctor` and `create-enrichment-pass` in
  all-skills are redirect stubs to this hub, kept until nothing selects them.
- `.agents/skills/` is a generated discovery root: never author there. Use the
  `skillex` CLI (`skill lint --path skills/heyma`, `sync`); standard:
  `~/code/skillex/docs/skills/hub-skill-standard.md`.
