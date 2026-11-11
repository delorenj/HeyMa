# n8n: nodes, workflows, releases

n8n here is a **control adapter and webhook plumbing, never an owner**: it invokes the absolute `wax` CLI and owns no pipeline state. Wax owns every artifact mutation, receipt, retry and schedule. n8n never watches files, never runs pass scripts, never archives or transcribes, and **no exported n8n trigger can join a pipeline import** — n8n can never become a second scheduler (Candybar owns all recurring time).

Live instance (2026-11-05): n8n 2.18.4 under PM2 (`pm2`, name `n8n`, pm_id 0, `PM2_HOME=~/.pm2`, cwd `~/.n8n`, port 5678) at `https://n8n.delo.sh`, on the mise node 24.6.0 binary. Restart with `pm2 restart n8n`; readiness at `/healthz/readiness`. There is no Dapr sidecar; event workflows publish NATS-direct via the Bloodbank Publisher node.

## The node package: `components/n8n-nodes-heyma`

Version **0.3.0**, **19 node types** in the manifest (2026-11-11). Installed into n8n as a `file:` link: `~/.n8n/nodes/package.json` has `"n8n-nodes-heyma": "file:../../HeyMa/components/n8n-nodes-heyma"`, so `node_modules/n8n-nodes-heyma` is a **symlink to the working tree** and n8n loads the repo's `dist/` — after any `src` change run `npm run build` in the component and `pm2 restart n8n`. The tarballs in `~/.local/share/wax/packages/` (`0.2.0-1f77a72`, `0.3.0-74755f5`) are archived artifacts, not what is loaded.

| Node | Purpose |
|---|---|
| **HeyMa** (Recording) | `wax rec start` / `rec stop` via `--json`; label ≤ 120 chars; bitrates 16k–64k |
| **WaxEp** (Wax Enrichment Pass) | One node, pass chosen live from `wax catalog --json`. Operations: **Author (Graph Only)** (default; pure authoring, zero side effects) and **Run (Preview One Item)** (`wax ep run`) |
| **WaxPass\*** ×15 (`wax-pass-*`) | Generated per-pass shells of WaxEp — TitleSlug, Classification, DictionaryCorrection, Diarization, VoiceId, CallMetadata, ClipboardCopy, FrontmatterStamp, ProjectExtraction, Summarize, Ticketable, DomainCuration, MemOps, TranscriptionEnhance, Wikification. Regenerate with `npm run nodes:passes` after the pass registry changes |
| **WaxPipeline** | Compile, Configure (authoring), Import Workflow, Show, Scan Documents, Run Document |
| **WaxCalls** | `event` (default; ingest one Telnyx webhook event), `sync` (`--since`, default 2d), `show` (call ref) |

Contract shared by all nodes (`nodes/HeyMa/waxCli.ts`): `execFile` with the **absolute** wax executable and absolute `WAX_ROOT`; PATH lookup is disabled. Every input is regex-validated before spawn. Timeout 1–7200 s. Wax's JSON response is parsed into the n8n item; CLI failures surface as error items under `continueOnFail`.

**Call events travel on stdin, never argv** (max 256 KiB), because a Telnyx event contains phone numbers and recording URLs. The Wax Calls node's `eventSource` is `item` (the whole webhook item; Wax finds the envelope) or `field` (dot path, default `body`). `fetchVoicemail` defaults true (adds `--no-fetch` when false). `executeOnce` defaults **false** on Wax Calls — one webhook delivery is one event; other nodes default true.

Authoring semantics: WaxEp/WaxPass nodes expose two stock `main` outputs — index 0 `continue` (ordering) and index 1 `requires` (gating). `wax pipeline import` maps output 0 → `after` edges and output 1 → `requires` edges. A graph with no connections compiles to no edges. Node basenames must stay PascalCase (`WaxPassTitleSlug.node.ts`): n8n's native loader derives the class name from the manifest basename, and a kebab basename kills the whole package load. `tests/nativeLoader.test.cjs` proves every manifest entry loads.

## Released workflows: `components/n8n-workflows`

Each release script dry-runs by default (prints `unchanged` / `would change`) and `--apply` PUTs the workflow, activates if needed, and verifies. Run them from their own directory (they `import release`); a plain `python3 -I` breaks the import.

| Workflow | Id | Release script | Status 2026-11-05 |
|---|---|---|---|
| On Telnyx DeLoSIP Event | `9BkQCtKxdEtPWLAn` | `telnyx_calls.py` | **Live and current** (dry run prints `unchanged`; published version `17c7043e…`, 2026-11-03) |
| Transcription Notifications | `XHWRrrYCzu4ifdS7` | `release.py` | **NOT applied.** Live is still the 4-node pre-release shape; dry run refuses: "workflow changed since review" |

**`9BkQCtKxdEtPWLAn` (calls):** Webhook → Wax Calls (`event`) → IF `$json.ended` → Bloodbank `bloodbank.telephony.call.ended` (orderingKey `call:<session>`) + ntfy topic `telephony`. Disposition mapping: declined → `rejected`; unknown → `failed`.

**`XHWRrrYCzu4ifdS7` (notifications):** live today it is two Bloodbank Trigger → ntfy pairs (`audio.file.received` → "Transcription Started"; `audio.transcription.completed` → "Transcription Complete"). The release adds a completion formatter and an intake branch (`bloodbank.audio.intake.detected` → ntfy). Consequence of it not being applied: **nothing relays intake events to ntfy** (moot while `ticketable` is disabled) and there is no formatted completion message. Formatter code and node tests exist and pass (`node --test format-completion.test.js format-intake.test.js`).

Secrets stay in 1Password (`op://` refs in the release scripts; never inline). The n8n API key and the ntfy credential are referenced, never copied into workflow JSON.

### Authoring workflows are drafts — never publish them

Two **inactive** n8n editor workflows are authoring surfaces for the pipeline exports: `XzKNdDixJMrdu9eh` "Wax Transcription Standard" and `GVeFgcnYkSq8SbpX` "Wax Documents Standard". **Their live editor state lags the committed release exports** (2026-11-05): the transcription draft lacks `dictionary-correction` (transcript-standard v12 has it) and the documents draft lacks `project-extraction` (doc-standard v10 has it). The truth is the committed `components/n8n-nodes-heyma/examples/*-release.json`; re-exporting from the editor drafts would drop passes. Never activate these workflows — their triggers would make n8n a second scheduler.

### Other live workflows touching telephony/audio (not Wax-owned — leave them alone)

- `SdNTSHrlakV5ubhC` "Telnyx voicemail" — Feet-era TeXML, still active, also publishes `bloodbank.telephony.call.*`.
- `OTrzey7C2VKe8dr4` "Telephony: Receive Wax call transcripts" — consumes `bloodbank.audio.transcription.completed`, caches in Redis.
- `2GDEqWFQvm8ncRjQ` "Telephony: Watch calls with [a contact]" — polls Telnyx recordings; since 2026-11-06 Candybar `telephony-damian-call-watch` fires its webhook every minute (was an n8n scheduleTrigger every 30 s).
- `90JBJTQsEY8gNfVF`, `BtrkJ4iIvGEKZA9U`, `WvuJ5MBALOLgXrV6` (inactive) — call-sharing for that contact.
- `R72MPZgl07qZ0ka6` "AAI Meeting Audio Workflow" — uses the HeyMa Start/Stop Recording nodes on a calendar webhook.
- `uDx166slPJMhAbM5` "Handle onTranscriptionReady - Obsidian Enhanced" — legacy subworkflow writing into the transcript vault.
- `PJ2uzBR0Zab9IKbi` "Telnyx SMS inbox" — its held-notification release runs on Candybar `telnyx-sms-release-held` every minute (was an n8n scheduleTrigger every 30 s).

Every n8n workflow is exported every 15 minutes to `~/code/delonet-n8n-workflows/workflows/<slug>--<id>.json` by Candybar job `delonet-n8n-workflows-sync` — the place to diff what is actually live. No n8n workflow keeps its own clock: recurring ones are listed in that repo's `.candybars/` and README ("Recurring workflows run on Candybar").

## Changing a node or a released workflow

1. Edit in `components/n8n-nodes-heyma` (or the release script in `components/n8n-workflows`).
2. `npm run build && npm test && npm run lint && npm run pack:check`.
3. Regenerate pass nodes only when the pass registry changed: `npm run nodes:passes`.
4. `pm2 restart n8n`; check `/healthz/readiness`.
5. Re-apply the workflow release with `python3 <release>.py --apply` from `components/n8n-workflows`.
6. If pass wiring changed, re-import the compiled pipelines: `wax pipeline import ... --name transcript-standard|doc-standard`, then `wax ep replan <item> --apply` for unfinished items — see [pipelines](pipelines.md).
