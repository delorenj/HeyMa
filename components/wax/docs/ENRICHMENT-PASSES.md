# Wax enrichment passes

An enrichment pass (EP) is an independently versioned command registered by a
YAML file in `config/passes.d/`. The registry is re-read on every call, so a
new or edited YAML and a new executable take effect with no restart; the Python
runner (`src/wax/passes.py`, `finalize.py`) is imported once into `waxd` and
needs `systemctl --user restart waxd`. Write YAML atomically and check
`wax ep list` afterwards: a YAML parse error silently disables that pass, and a
malformed `requires`, `after` or `skip_when` makes `passes.ordered()` raise for
every item.

New recordings follow this order:

1. upload and verify the immutable, content-addressed audio object;
2. transcribe and publish the initial Markdown note;
3. run every enabled pass with `auto: true`, in dependency order;
4. refresh the audio-to-transcript link, re-verify the audio object and park
   the local source;
5. **finalize**: once every planned pass is `completed` or `skipped` at its
   current version, write the one `transcription.completed` event and then
   publish the events the passes declared (see Finalization).

A failed pass is recorded but does not stop its siblings or step 4. It does
withhold step 5: the item becomes `enrichment_pending` until the pass is
retried (`wax ep sweep`, `wax ep run`, or the tray). Automatic runs skip a pass
already completed at the same `version` and the same definition; increasing
`version` causes the new definition to run once.

## Registry contract

```yaml
slug: title-slug
version: 3
description: Grounded title and summary, with a date-prefixed transcript filename.
enabled: true
auto: true
requires: []
after: []
clobber: []
timeout_s: 180
frontmatter_schema: "{home}/d/_vault/Settings/frontmatter-category-map.json"
env:
  WAX_TITLE_MODEL: automaticai/openrouter/google/gemini-3.7-flash
command: ["{component_root}/config/passes.d/bin/title-slug", "{md_path}", "{item_id}"]
```

| Field | Meaning |
|---|---|
| `slug`, `description` | Identity. `slug` defaults to the file stem. |
| `version` | Integer, default 1. Bump it whenever behaviour or output semantics change and existing items should rerun. |
| `enabled` | Available to `wax ep run` and, with `auto`, to the flow. Default false. |
| `auto` | Part of the post-transcription flow (and so of the item's stored plan). Default false. |
| `requires` | Other **enabled** passes this one consumes. Orders the pass after them AND gates it: `run()` will not execute it until each is `completed` or `skipped` at the live registry version; otherwise it records `dependency_failed` (ledger row, a real attempt, and a `wax.passes.<slug>` note entry saying `waiting on <dep> (<state>@v<n>)`). An `auto` pass may only require other `auto` passes. |
| `after` | Ordering only. No gate: a failed `after` target never stops its dependent. It must still name an enabled pass, and (unlike `requires`) is not checked against the plan, so pointing it at a non-`auto` pass makes `run_auto` raise. |
| `skip_when` | `{field, equals}` matched against the note's frontmatter at run time; a match records `skipped` / `condition_matched` and runs nothing. |
| `kind` | `diarization` also skips (`explicitly_disabled`) when `WAX_DIARIZATION` is `0/false/no/off`. |
| `body_mutation` | `compare-and-replace` authorizes the `body_replace` result intent; absent means a returned `body_replace` fails the pass. |
| `clobber` | Frontmatter keys the pass may overwrite when non-empty. Default none. |
| `emits` | `<entity>.<action>` event types the pass may declare in `events`. Absent means it may declare none. |
| `env` | Variables added to the child's environment (placeholders expanded). They win over the unit and the shell. |
| `frontmatter_schema` | Vault base taxonomy stamped onto the note before the command runs (best effort, never fatal). |
| `timeout_s` | Hard limit for the command; default 900. A timeout is `timeout`. |
| `command` | argv. Placeholders: `{component_root}`, `{home}`, `{item_id}`, `{md_path}`. |

Ordering is a depth-first walk over `requires` and `after` together; passes with
no dependency between them run alphabetically by slug. Cycles and dependencies
that are missing or disabled raise `PassError`.

The runner holds a per-item lock while the child runs, so a command must not
shell out to a `wax` command that needs the same item. Each run mints a
deterministic command id (`uuid5(WAX_NS, "ep:<item>:<slug>:<attempt>")`) and
issues `bloodbank.cmd.audio.task.start`, mirrored as `task.requested`, then
`task.started` and `task.completed|failed|skipped`, all correlated on that id. A
`dependency_failed` gate or a `skip_when` skip issues none of these: no command
was run.

## Result contract (`wax.ep.v1`)

Commands may log ordinary text, then emit one JSON object as their last stdout
line. They must not edit the note, rename it, or touch the ledger.

```json
{
  "wax_ep_version": 1,
  "frontmatter": {"title": "Modular Transcript Enrichment Passes", "summary": "...", "title-slug": "modular-..."},
  "transcript": {"slug": "modular-transcript-enrichment-passes"},
  "events": [{"type": "intake.detected", "key": "<stable dedup key>", "data": {"...": "..."}}]
}
```

| Key | Effect |
|---|---|
| `frontmatter` | Object of proposals. Applied through `frontmatters set` in one batch. A value lands only if the existing one is empty (`None`, `""`, `[]`, `{}`), identical, or the key is in `clobber`; a non-empty list, `0` and `false` are never replaced silently, and a new `[]` does not erase a non-empty list. Keys must match `[A-Za-z0-9_-]+`. Provenance keys (`wax-item-id`, the `wax` block, `source*`, `captured`, `created_at`, `vault-id`) are runner-owned and rejected. |
| `transcript.slug` | Collision-safe rename to `<date-prefix>-<slug>.md`; `transcripts.md_path` is updated before the pass is recorded, so later passes in the same run receive the new path. |
| `body_replace` | `{sha256, text}`; applied only when `body_mutation: compare-and-replace` and the current body still hashes to `sha256` (human edits win). Dropped from the completion payload. |
| `state` / `reason_code` | A pass may report `{"state": "skipped", "reason_code": "no_project"}`. Its frontmatter is still applied, the skip satisfies `requires` and finalization, and the reason lands in the ledger and the note. |
| `events` | Declared host events; see below. |
| `link_audio` | Historical. Accepted and ignored: the worker refreshes the S3 link after every run regardless of the passes. |

Anything else is ignored. A command that prints no `wax_ep_version` object still
runs and is tracked, but proposes nothing.

A pass that knows why it failed prints `reason_code=<code>` as the **first**
stderr line, matching `^reason_code=([a-z_]+)$` (lowercase and underscores only,
no digits). The runner records it; otherwise the failure is `nonzero_exit`,
`timeout`, `run_error` or, when the result cannot be applied,
`result_apply_failed`. The note keeps its own history in `wax.passes.<slug>`
(`state`, `version`, `attempt`, `command_id`, and `reason_code`/`detail` when
failed or skipped); the entry is replaced wholesale on every run, so a pass that
recovers carries no stale failure text.

### `events`

A result may list up to **50** events. Validation runs before anything touches
the note, so a violation fails only that pass with `result_apply_failed` and
leaves the note byte-identical. Each event is exactly `{type, key, data}`:

- `type` matches `^[a-z][a-z_]*\.[a-z][a-z_]*$` and is listed in the pass's
  registry `emits`. A pass with no `emits`, or an `emits` that is not a list of
  such strings, fails its own runs and nothing else.
- `key` matches `^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$` and is unique within the
  result. It is the identity of the event: choose one that is a function of the
  event's content, not of a counter or the run.
- `data` is a JSON object, at most 32 KiB encoded, with no `project` key (the
  envelope owns `project`, always `wax`).

A pass cannot publish anything itself. It runs before `transcription.completed`
exists, and an event announced from a pass that then fails would describe work
that never happened. Events are stored with the pass result in the ledger and
published by `finalize()`. Events on a skipped or failed result are never
published.

## Finalization

`finalize(item)` (`src/wax/finalize.py`) runs after the audio is parked, from
`wax ep run|run-all|sweep|retry` and from the tray's retry. It is serialized per
item and idempotent.

1. **Gate.** Every planned pass must be `completed` or `skipped` at the plan's
   version and definition. Otherwise nothing is written (`passes_incomplete`,
   naming the first incomplete slug in plan order).
2. **One completion per plan.** The `transcription.completed` envelope carries
   the note's final path, metadata, body (or its URI if over the transport
   limit) and every pass result (minus `body_replace` and `events`). Its id is
   `uuid5(WAX_NS, "completion:<item>:<plan_id>")`; the `completions` primary key
   makes it at-most-once per plan.
3. **Pass events, after it.** Every `completed` pass whose result lists events
   is published, ordered by plan order, then slug, then the pass's own list
   order. Each becomes a `bloodbank.audio.<entity>.<action>` envelope:
   - id `uuid5(WAX_NS, "ep-event:<item>:<slug>:<type>:<key>")`, independent of
     plan and attempt, so a re-plan, a re-run or a backfill never duplicates;
     the `pass_events` table (`event_id` primary key) records what was enqueued
     and the outbox row it became;
   - the runner stamps `item_id`, `transcription_id`, `transcript` (the note's
     basename **at emission**, i.e. after the title-slug rename) and
     `transcript_uri` into `data`; these win over anything the pass supplied;
   - `correlationid` and `causationid` are the completion's event id, and
     `ordering_key` is `transcription:<item>`;
   - envelopes are built before the transaction, and a new completion inserts
     them in the same transaction as the completion row, immediately after it,
     so the outbox (drained in id order) delivers `completed`, then the events;
   - an event that cannot be built or exceeds the envelope limit is dropped with
     a warning and counted in `pass_events_dropped`; building never raises out
     of `finalize()`.
4. **Backfill.** A completed item keeps its stored plan, so a pass registered
   later never runs on it by itself. `wax ep run <slug> <item>` runs it by
   hand; the `finalize()` that follows sees the existing completion, enqueues the
   not-yet-published events correlated to it, and returns `pass_events: N`. It
   needs the note to exist (otherwise `pass_events_reason: missing_transcript`
   and the next call retries).

`wax doctor` includes `pass events enqueued`, which warns when a finalized item
has a completed pass whose declared events are not in `pass_events` and names
the `wax ep run` that fixes it.

## Retries

`wax ep sweep` re-runs every enabled pass whose latest attempt `failed` with
`attempt < --max-attempts`, then `run_auto` for each such item (which reruns
everything not completed, in dependency order, ignoring the bound), then
`finalize`. `dependency_failed` rows are exempt from the bound: the pass never
ran, and the gate lifts when a different pass succeeds, so they must stay
sweepable even after their dependency has used up its attempts.

## Audio-to-transcript identity

S3 audio names are never renamed after upload. Object-store "rename" is a
copy-plus-delete operation, which would invalidate the verified key and both
recovery indexes. Instead:

- transcript frontmatter records `source-sha256`, `source-s3-key`, and
  `source-s3-uri`;
- `<audio-key>.wax.json` and `.by-content/<sha256>.json` record the current
  transcript filename, vault-relative path, title, slug, summary, and link time;
- S3 object tags mirror `Transcription`, `ItemId`, `Transcript`, and
  `TitleSlug` without moving audio bytes.

The content ID remains the durable join key even if a person later renames the
note again.

## project-extraction and ticketable

Two passes turn a transcript into pjangler-attributed tickets. Their YAMLs and
executables are `config/passes.d/{project-extraction,ticketable}.yaml` and
`passes.d/bin/`; both use `clobber: []`.

| Pass | Owns | How |
|---|---|---|
| `project-extraction` | `project-ids` (pjangler ids, most probable first; `[]` = ran, none) and `projects` (names, index-aligned) | Candidates come from the pjangler registry (`GET $PJ_REGISTRY_URL/v1/registry`, default `http://127.0.0.1:8764`) plus the spoken forms and one-line descriptions in `config/project-aliases.yaml`, which fills the registry's empty descriptions. Decided by TypeSafe's Jev (`typesafe/jev-1.13`): one `noul` question per project in a single request, a project is kept when P(true) is at least `WAX_PROJECT_MIN_P` (0.6), or `WAX_PROJECT_MIN_P_LONG` (0.75) for transcripts over 30K characters, where scores saturate. |
| `ticketable` | `ticketable`: a list of `{project_id, description, transcript}`; `[]` = ran, none | `requires: [title-slug, project-extraction]`, so `transcript` is the final filename. Extracted through the gateway route `automaticai/openrouter/google/gemini-3.7-flash` (`WAX_TICKETS_*`) with a strict JSON schema whose `project_id` is an enum of the note's `project-ids`. Declares `emits: [intake.detected]` and returns one event per distinct ticket, keyed by a hash of `(project_id, description)`; the event's `intake_id` is `<item>:<first 12 hex of that key>`. If the note has no `project-ids` it reports `skipped` / `no_project`. |

The runner publishes each ticket as `bloodbank.audio.intake.detected` after
`transcription.completed` (schema:
`bloodbank/schemas/bloodbank/audio/intake.detected.json`); n8n relays it to the
ntfy `audio` topic, and the project's PM triages it (`repo.intake.triaged`
closes the loop through `data.intake_id`).

**Jev is the one deliberate bypass of the AutomaticAI gateway.** The gateway has
no decisions route, so `project-extraction` calls
`https://openrouter.ai/api/alpha/decisions` directly with HeyMa's own OpenRouter
**inference** key (`WAX_JEV_API_KEY_OP`, an `op://` reference, $1/day cap). A
management key cannot do inference and answers 401 `User not found`, which
looks like a dead account; `wax doctor`'s `jev api key` probe checks
`is_provisioning_key` and the remaining budget. Never route the ticket model
through that key or the Jev call through the gateway token.

Re-running: a non-empty existing value (human or earlier run) is kept and
reused without a provider call. To redo one, clear the key in the note, then
`wax ep run project-extraction <item>` (and `ticketable` after it). Events keep
their keys, so an unchanged ticket is never published twice; a changed ticket
has a new content hash and is published as a new intake.

`wax doctor` probes what these passes cannot work without: `pjangler registry`
(reachable, and HeyMa's frozen id `transcription-queue` is registered; when it
is not, register it with `POST /v1/index` and the repo's `.project.json`, never
`pj init --apply`, which rewrites the manifest and the `heyma-pm` agent key),
`jev api key`, and `pass events enqueued`.
