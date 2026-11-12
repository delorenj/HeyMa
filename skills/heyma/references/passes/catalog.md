# Pass catalog: one row per `passes.d` registry entry

Every `components/wax/config/passes.d/*.yaml` has exactly one row here, with the
slug and its `enabled` state. A drift-guard test
(`components/wax/tests/skill_hub_test.py`) fails when a slug is missing or the
enabled column disagrees with the YAML, so add a row in the same commit as a new
pass. Read 2026-11-05 from the YAML and `wax ep list`. How passes run is in
[passes/index](index.md).

Columns: **enabled** is the YAML `enabled` (`yes`/`no`). **mode** is `auto` when
the pass is in the post-transcription flow, `manual` when it runs only by name
(`wax ep run`) or in a manual pipeline. **requires** gates (`dependency_failed`);
**after** orders only (pipeline edges are noted below the table).

| Slug | Enabled | Mode | Ver | Capabilities | Requires | Model or route | Writes |
|---|---|---|---|---|---|---|---|
| `frontmatter-stamp` | yes | auto | 1 | audio | none | none | `duration-seconds`, `engine-model`, `word-count` from the ledger |
| `classification` | yes | auto | 2 | document | none | Jev ingress, `jev-1.13.0`, key alias `heyma-wax-classification` | `classification`, `classification-detail`, `classification-confidence`, `classification-source` |
| `diarization` | yes | auto | 1 | audio | none (after `classification`) | local Sortformer on CUDA, no chat model | `diarized`, `diarization-requested`, `diarization-device-requested`, `diarization-device`, speaker-labelled body |
| `dictionary-correction` | yes | auto | 1 | audio, document | none (after `diarization`) | Jev ingress, `jev-1.13.0`, key alias `heyma-wax-dictionary` | respelled body (`body_replace`) |
| `title-slug` | yes | auto | 3 | audio, document | none | gateway chain `WAX_TITLE_*` | `title`, `summary`, `title-slug`; renames an audio transcript |
| `project-extraction` | yes | auto | 1 | document | none | Jev ingress, `jev-1.13.0`, key alias `heyma-wax-project-extraction` | `project-ids`, `projects` |
| `clipboard-copy` | yes | auto | 1 | audio | none | none | nothing in the note (desk effect: clipboard and a sound) |
| `voice-id` | yes | auto | 1 | audio | none (pipeline: after `dictionary-correction`) | local TitaNet-large on CUDA; Jev ingress (`jev-1.13.0`, key alias `heyma-wax-voice-id`) only for speakers the voice cannot settle | `voices` (links to `~/d/Voices` notes), named speaker turns (`body_replace`); the voice registry |
| `call-metadata` | yes | auto | 1 | audio | none | none; Telnyx call-log API | 14 `call-*` keys (direction, outcome, remote number and name, line, forwarding, times, session and leg ids) |
| `summarize` | yes | manual | 1 | document | none | gateway chain `WAX_SUMMARY_*` | `summary` and a `## Summary` section prepended to the body |
| `ticketable` | no | manual | 1 | document | `title-slug`, `project-extraction` | gateway chain `WAX_TICKETS_*` | declares `intake.detected` events (one per ticket) |
| `domain-curation` | no | manual | 1 | none declared | none | none (placeholder) | nothing |
| `mem-ops` | no | manual | 1 | none declared | none | none (placeholder) | nothing |
| `transcription-enhance` | no | manual | 1 | none declared | none | none (placeholder) | nothing |
| `wikification` | no | manual | 1 | none declared | none | none (placeholder) | nothing |

The four placeholders are registered stubs ("TODO, write the script then flip
`enabled`"); they have no `version` key (default 1), no capabilities and no
executable under `passes.d/bin/`.

## Timeouts and live outcomes

`timeout_s` and the outcome tally from `wax ep list` on 2026-11-05. The tally is
OK, FAIL and STALE counts, where OK and FAIL are at the current version and STALE
is at an older one. It is time-sensitive; re-read `wax ep list`.

| Pass | `timeout_s` | OK / FAIL / STALE |
|---|---|---|
| call-metadata | 90 | 102 / 0 / 0 |
| classification | 60 | 557 / 0 / 13 |
| clipboard-copy | 30 | 237 / 0 / 0 |
| diarization | 86400 | 168 / 0 / 0 |
| dictionary-correction | 180 | 786 / 0 / 0 |
| frontmatter-stamp | 60 | 419 / 0 / 0 |
| project-extraction | 120 | 533 / 0 / 0 |
| summarize | 390 | 2 / 0 / 0 |
| title-slug | 390 | 459 / 3 / 69 |
| voice-id | 900 | new 2026-11-11 |
| ticketable, domain-curation, mem-ops, transcription-enhance, wikification | 1800, 900, 900, 900, 900 | 0 / 0 / 0 |

`title-slug` carries the only current failures (rate limits and bad responses
from the chain; see [passes/model-chain](model-chain.md)). Its 390 s is six
chain members at 60 s plus the catalog read and `op read`, inside the runner
deadline.

## Per-pass notes

**Edges that live in the pipeline, not the registry.** In `transcript-standard`
v15, `dictionary-correction` runs after `diarization`, and `title-slug`,
`project-extraction`, `clipboard-copy` and `voice-id` each run after `dictionary-correction`.
The registry YAML for `title-slug` has no `after`. `doc-standard` v10 is
`classification`, `title-slug`, `project-extraction`. See
[pipelines](../pipelines.md).

**`classification` (v2).** Four values: `monolog`, `meeting`, `phone_call`,
`other`. A `telnyx-*` rule decides calls first, a floor rule next, then Jev
(minimum probability 0.6); an abstain stamps `unsorted`. Codes: `empty_document`,
`low_confidence`, `kind_disagrees_with_is_phone`, `invalid_classification`. It
left the gateway model chain in v2 (2026-10-29): it never calls a chat model
through the gateway.

**`diarization`.** Skips a `monolog` (`condition_unmet`), a dual-channel
`telnyx-*` call (`channel_separated`) and a hand-edited body (`human_edits`).
Details are in [transcription](../transcription.md).

**`dictionary-correction`.** Respells the speaker's own words against
`Dictionary.yaml`. Calibration, commands and traps are in
[passes/dictionary](dictionary.md).

**`title-slug` (v3).** Audio runs also rename the note to
`<date>-<slug>.md`; a document keeps its name. Existing non-empty `title`,
`summary` and `title-slug` are preserved. `frontmatter_schema` stamps the vault
taxonomy from `{home}/code/obsops/Settings/frontmatter-category-map.json` before
the command runs. It reads `/v1/models` once and skips unlisted chain members.

**`project-extraction`.** Maps note text to pjangler project ids through Jev
(minimum probability 0.6, 0.75 for long notes) and preserves an existing
non-empty `project-ids`. Enabled 2026-10-29. Codes: `registry_invalid`,
`jev_too_large`.

**`summarize`.** Manual only (`auto: false`); transcripts get their summary from
`title-slug`. Runs in the `untitled-standard` pipeline (summarize, then
`title-slug`, which requires `summarize`) to title the vault's `Untitled*.md`
notes. `body_mutation: compare-and-replace`.

**`ticketable`.** Implemented but disabled. Each ticket becomes one
`bloodbank.audio.intake.detected`, relayed to ntfy `audio`. Skips with
`no_project`. See [events](../events.md).

**`voice-id`.** Fingerprints each speaker (TitaNet in `.venv-diarization`),
matches the voice registry, names known voices' `Speaker N` turns and registers
strangers in `~/d/Voices` with a notification to name them. A call's channel A
is the owner and a contact-named far channel is that contact, by construction.
A speaker the voice cannot settle is named from the transcript's context when
Jev and the voice agree (verdict `context`); a failed Jev call names nobody and
is recorded in the result's `evidence.context.error`. Codes: `audio_missing`, `voice_id_failed`, `no_speech` (skipped). Calibration,
thresholds, the registry and `wax voices` are in [passes/voice-id](voice-id.md).

**`call-metadata`.** Joins a `telnyx-*` recording's file name to the Telnyx call
log and stamps `call-*` keys; skips non-calls with `not_a_call`. Codes:
`call_not_found`, `telnyx_auth_failed`, `telnyx_http_error`, `telnyx_unreachable`.
The Telnyx key is `op://DeLoSecrets/74k7nz3xkryqns4lpf65a323ue/credential`. Call
identity and the call log are in [calls](../calls.md). Its 2026-11-05 lookup fix
(exact-leg CDR session, commit `fa0abb7`) is in the
[incident log](../../subskills/doctor/references/incident-log.md).

**Jev keys.** All four Jev passes (voice-id only for its context step) read an `op://` item-UUID reference from
their YAML env and call the ingress at
`https://jev.delo.sh/typesafe/v1/systemone` (stack
`~/docker/stacks/ai/jev-litellm`, spend in the LiteLLM `LiteLLM_SpendLogs` table
of `jev-litellm-postgres`). They never go through the AutomaticAI gateway, which
has no decisions route. Daily budgets: `heyma-wax-project-extraction` $1, `heyma-wax-voice-id` $0.50,
`heyma-wax-classification` $0.50. Never the ingress master key or the raw TypeSafe
key.

| Key alias | `op://` item UUID (field `credential`) |
|---|---|
| `heyma-wax-classification` | `lmtarsmlr35gqyydjsw245mvra` |
| `heyma-wax-project-extraction` | `4y3hlfp2dowbzpi6ncp67kdm5i` |
| `heyma-wax-dictionary` | `sazfdn52xd3a3uyatrbo43hiei` |
| `heyma-wax-voice-id` | `u4dk64drlzi3fd2q3qzpw3ox24` |
| `heyma-wax-inference` (gateway token for the chat chain) | `yeurk5dpqkaarspvsn3cjtmkki` (field `heyma-wax-inference`) |
