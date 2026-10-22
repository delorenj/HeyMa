# Wax enrichment passes

An enrichment pass (EP) is an independently versioned command registered by a
YAML file in `config/passes.d/`. The registry is re-read on every call, so a
new or edited YAML and a new executable take effect with no restart; the Python
runner (`src/wax/passes.py`, `finalize.py`) is imported once into `waxd` and
needs `systemctl --user restart waxd`. Write YAML atomically and check
`wax ep list` afterwards: a YAML parse error silently disables that pass, and a
malformed `requires`, `after`, `skip_when` or `when` gets that pass — and only
it, plus anything gated on it — **quarantined** (`!! QUARANTINED: <reason>` in
`wax ep list`, a `pass registry quarantine` FAIL in `wax doctor`). The rest of
the fleet keeps running; fixing the YAML lifts the quarantine live.

New recordings follow this order:

1. upload and verify the immutable, content-addressed audio object;
2. transcribe and publish the initial Markdown note;
3. run every enabled pass with `auto: true`, in dependency order;
4. **publish what is ready**: a completed pass's declared events are enqueued
   as soon as the pass's own `requires` subgraph is `completed`/`skipped` —
   the rest of the plan does not gate them (a wedged sibling with a 24h
   timeout must not withhold tickets that are done). Correlation anchors to
   the completion event id once one exists, to the pass's command id until
   then;
5. refresh the audio-to-transcript link, re-verify the audio object and park
   the local source;
6. **finalize**: once every planned pass is `completed` or `skipped` at its
   current version, write the one `transcription.completed` event (the
   plan-gated summary fact) and enqueue any pass events still pending — a
   dedup no-op for events step 4 already published.

A failed pass is recorded but does not stop its siblings or step 5. It does
withhold step 6: the item becomes `enrichment_pending` until the pass is
retried. Retries are no longer manual-only: the worker sweeps on a timer
(`WAX_SWEEP_INTERVAL_S`, default 900s) with the same attempt bound as
`wax ep sweep` (default 3, `dependency_failed` exempt). A pass that fails its
final permitted attempt emits **`bloodbank.audio.task.exhausted`** — the
dead-letter signal — exactly once per (item, slug, version); the receipt lives
in `passes.exhausted_at`, and a version bump clears it so a fixed
definition announces afresh (true wherever a run picks up the live
registry — manual `wax ep run` and plan-less legacy items; a planned
item's version is frozen with its plan, so its exhaustion clears only
through the explicit retry paths). Automatic runs skip a pass already
completed at the same `version` and the same definition; increasing
`version` causes the new definition to run once.

## Model chains for chat passes

Operator order, 2026-10-22: **no Anthropic models for Wax inference.** A chat
pass (`title-slug`, `classification`, `ticketable`) names an ordered list: the
primary (`WAX_TITLE_MODEL` / `WAX_TICKETS_MODEL`), then the comma-separated
`*_MODEL_FALLBACKS`. Today: `automaticai/auto/fast`, `automaticai/hypercharm/glm-5.3`,
`automaticai/hypercharm/minimax-m3`, `automaticai/free`. Implemented once, in
`wax.provider.run_chain`, and used by all three passes.

| Failure | Next member? |
|---|---|
| 429, 402, 5xx, unreachable, timeout | yes |
| unknown model (`model_not_found`, `missing_model`), 400 bad request | yes |
| 403 "This token has no access to model" (outside the token's scope) | yes |
| empty or unparseable content | yes |
| `no_api_key`, 401, any other 403 | **no** (the key is shared by every member) |

`title-slug`/`classification` read `/v1/models` once and skip a member the token
cannot see (recorded as `missing_model`, no request). Router family names the
gateway never lists (`automaticai/free`) are never skipped for being unlisted.
`ticketable` runs the chain per window and relies on the 403 for scope.

Provenance: the result's `evidence` is `{model, chain, fallbacks:[{model,
reason_code, status}]}` (stored with the pass row), and a successful run writes
`model` (and `fell_through` when a member failed) into the note's
`wax.passes.<slug>` block. When the whole chain fails the pass exits with the
PRIMARY's reason code (`provider_rate_limited` if every member was rate limited)
and the detail lists every attempt as `model=reason/status`.

Sizing: per-request timeout x chain length must stay under `timeout_s`
(`title-slug`/`classification`: 60s x 4 = 240s < 270s; `ticketable`: 120s x 4 x
3 windows = 1440s < 1500s). A test enforces it. Compiled pipelines freeze pass
env, so re-import the n8n release exports after changing a chain. The token
scope is `deploy/provision-inference` (see AGENTS.md). Kimi stays out of the
chain until gateway bug AAI-55 closes.

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
timeout_s: 270   # 4 chain members x 60s request timeout, plus the catalog lookup
frontmatter_schema: "{home}/d/_vault/Settings/frontmatter-category-map.json"
env:
  WAX_TITLE_MODEL: automaticai/auto/fast
  WAX_TITLE_MODEL_FALLBACKS: automaticai/hypercharm/glm-5.3,automaticai/hypercharm/minimax-m3,automaticai/free
  WAX_TITLE_REQUEST_TIMEOUT_S: "60"
command: ["{component_root}/config/passes.d/bin/title-slug", "{md_path}", "{item_id}"]
```

| Field | Meaning |
|---|---|
| `slug`, `description` | Identity. `slug` defaults to the file stem. |
| `version` | Integer, default 1. Bump it whenever behaviour or output semantics change and existing items should rerun. |
| `enabled` | Available to `wax ep run` and, with `auto`, to the flow. Default false. |
| `capabilities` | List of media this pass can run over: `audio` (transcript notes) and/or `document` (plain Markdown documents). Absent means `audio` only. A document run replaces an absent list with `["audio"]` and skips the pass with `inapplicable_capability` — the audio flow is unchanged by this field. |
| `auto` | Part of the post-transcription flow (and so of the item's stored plan). Default false. |
| `requires` | Other **enabled** passes this one consumes. Orders the pass after them AND gates it: `run()` will not execute it until each is `completed` or `skipped` at the live registry version; otherwise it records `dependency_failed` (ledger row, a real attempt, and a `wax.passes.<slug>` note entry saying `waiting on <dep> (<state>@v<n>)`). An `auto` pass may only require other `auto` passes. A requirement that is missing, disabled, quarantined or manual-when-auto **quarantines the dependent**, loudly, instead of raising the whole registry down. |
| `after` | Ordering only. No gate: a failed `after` target never stops its dependent. It must still name an enabled pass, and (unlike `requires`) is not checked against the plan, so pointing it at a non-`auto` pass makes `run_auto` raise. |
| `when` | List of clauses `{field: <frontmatter key>, <operator>: <operand>}`; ALL must hold for the pass to run, checked against the note's frontmatter at run time. First unmet clause records `skipped` / `condition_unmet` with the failing clause in `detail` (ledger AND note). Operators: `equals`, `not_equals`, `in`, `not_in`, `exists`, `absent`, `empty`, `not_empty` (flag ops: write `true`), `length`, `min_length`, `max_length` (list/string size; missing field = 0). This is where routing lives — declarative, visible in `wax ep list`, dry-runnable — and it covers the multi-way branches `skip_when` cannot. A malformed clause quarantines the pass. |
| `skip_when` | Legacy single-clause skip-polarity form: `{field, equals}` matched against the note's frontmatter at run time; a match records `skipped` / `condition_matched` and runs nothing. Prefer `when` for anything new. |
| `kind` | `diarization` also skips (`explicitly_disabled`) when `WAX_DIARIZATION` is `0/false/no/off`. |
| `body_mutation` | `compare-and-replace` authorizes the `body_replace` result intent; absent means a returned `body_replace` fails the pass. |
| `clobber` | Frontmatter keys the pass may overwrite when non-empty. Default none. |
| `emits` | `<entity>.<action>` event types the pass may declare in `events`. Absent means it may declare none. |
| `env` | Variables added to the child's environment (placeholders expanded). They win over the unit and the shell. |
| `frontmatter_schema` | Vault base taxonomy stamped onto the note before the command runs (best effort, never fatal). |
| `timeout_s` | Hard limit for the command; default 900. A timeout is `timeout`. |
| `command` | argv. Placeholders: `{component_root}`, `{home}`, `{item_id}`, `{md_path}`. |

Ordering is a depth-first walk over `requires` and `after` together; passes with
no dependency between them run alphabetically by slug. Cycles, missing/disabled
dependencies, malformed fields and auto-requires-manual all quarantine the
offending pass (and its dependents) rather than raising — `ordered()` never
raises on definition content.

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
  envelope owns `project`, always `wax`) — and it must satisfy the Bloodbank
  schema at `$WAX_BLOODBANK_SCHEMAS/bloodbank/audio/<type>.json` (default
  `~/code/33GOD/bloodbank/schemas`). Wax publishes straight to NATS and bypasses
  bb-emit's guard, so the runner IS the schema enforcement point: a violation
  fails the pass with `result_apply_failed` before the note or ledger moves, and
  a type with no schema file fails the same way — schema-first is fail-closed.
  At result time the check relaxes `required` for the fields the runner stamps
  at emission (`item_id`, `transcription_id`, `transcript`, `transcript_uri`,
  `project`); finalize re-validates the fully stamped payload and drops a
  violation with a warning and a `pass_events_dropped` count. `wax doctor`'s
  `pass event schemas` probe pre-flights every enabled pass's `emits`.

A pass cannot publish anything itself. Events are stored with the pass result in
the ledger and published by `finalize.publish_pass_events()` once the pass's own
`requires` subgraph completes (see Finalization) — no longer held hostage to the
whole plan. Events on a skipped or failed result are never published.

## Finalization

Two publication gates now exist, deliberately separate:

- **`finalize.publish_pass_events(item)`** — the subgraph gate. Every completed
  pass whose transitive `requires` are all `completed`/`skipped` at the wanted
  version gets its declared events enqueued NOW. Called by the worker right
  after `run_auto`, by `wax ep run|run-all|sweep`, and by the tray's retry —
  before, and independent of, plan completion. Correlation: the completion
  event id once one exists, the pass's command id until then (both findable in
  Candystore via `?correlationid=`).
- **`finalize(item)`** — the plan gate. Serialized per item and idempotent;
  runs after the audio is parked, from `wax ep run|run-all|sweep|retry`, from
  the tray's retry, and from the worker.

finalize() itself:

1. **Gate.** Every planned pass must be `completed` or `skipped` at the plan's
   version and definition. Otherwise nothing is written (`passes_incomplete`,
   naming the first incomplete slug in plan order).
2. **One completion per plan.** The `transcription.completed` envelope carries
   the note's final path, metadata, body (or its URI if over the transport
   limit) and every pass result (minus `body_replace` and `events`). Its id is
   `uuid5(WAX_NS, "completion:<item>:<plan_id>")`; the `completions` primary key
   makes it at-most-once per plan.
3. **Pass events.** Every `completed` pass whose result lists events and has
   not already been published by the subgraph gate is enqueued, ordered by plan
   order, then slug, then the pass's own list order. (Events the early path
   already published are a dedup no-op here; events published early precede the
   completion in the outbox, correlated on the pass's command id.) Each becomes
   a `bloodbank.audio.<entity>.<action>` envelope:
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
`attempt < --max-attempts` (default 3), then `run_auto` for each such item
(which reruns everything not completed, in dependency order, ignoring the
bound), then `publish_pass_events` and `finalize`. `dependency_failed` rows are
exempt from the bound: the pass never ran, and the gate lifts when a DIFFERENT
pass succeeds, so they must stay sweepable even after their dependency has used
up its attempts.

The sweep is also **scheduled**: the waxd worker runs it every
`WAX_SWEEP_INTERVAL_S` (default 900s) whenever the pipeline is enabled, so a
transient provider outage self-heals instead of waiting for a human to type
anything. Both paths share `worker.sweep_failed_passes()`.

A pass that fails at the attempt bound is **exhausted**: the runner stamps
`passes.exhausted_at` and emits one `bloodbank.audio.task.exhausted` event
(schema: `bloodbank/schemas/bloodbank/audio/task.exhausted.json`) naming the
item, slug, attempt, version and final reason. It fires at most once per
(item, slug, version) — a version bump clears the receipt — and never for
`dependency_failed`. This is the pipeline's dead-letter signal; ntfy-router
routes it like any other non-muted event.

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
| `project-extraction` | `project-ids` (pjangler ids, most probable first; `[]` = ran, none) and `projects` (names, index-aligned) | Candidates come from the pjangler registry (`GET $PJ_REGISTRY_URL/v1/registry`, default `http://127.0.0.1:8764`) plus the spoken forms and one-line descriptions in `config/project-aliases.yaml`, which fills the registry's empty descriptions. Decided by TypeSafe's Jev (`jev-1.13.0`, through the fleet Jev ingress): one `noul` question per project in a single request, a project is kept when P(true) is at least `WAX_PROJECT_MIN_P` (0.6), or `WAX_PROJECT_MIN_P_LONG` (0.75) for transcripts over 30K characters, where scores saturate. |
| `ticketable` | `ticketable`: a list of `{project_id, description, transcript}`; `[]` = ran, none | `requires: [title-slug, project-extraction]`, so `transcript` is the final filename. Extracted through the model chain `automaticai/auto/fast` -> Hyper -> `automaticai/free` (`WAX_TICKETS_MODEL` / `WAX_TICKETS_MODEL_FALLBACKS`, the `heyma-wax-inference` token) with a strict JSON schema whose `project_id` is an enum of the note's `project-ids`. Declares `emits: [intake.detected]` and returns one event per distinct ticket, keyed by a hash of `(project_id, description)`; the event's `intake_id` is `<item>:<first 12 hex of that key>`. If the note has no `project-ids` it reports `skipped` / `no_project`. |

The runner publishes each ticket as `bloodbank.audio.intake.detected` after
`transcription.completed` (schema:
`bloodbank/schemas/bloodbank/audio/intake.detected.json`); n8n relays it to the
ntfy `audio` topic, and the project's PM triages it (`repo.intake.triaged`
closes the loop through `data.intake_id`).

**Jev goes through the fleet's Jev ingress, not the AutomaticAI gateway.** The
gateway has no decisions route. `project-extraction` posts to
`https://jev.delo.sh/typesafe/v1/systemone` (`WAX_JEV_URL`): the jev-litellm
LiteLLM proxy (`~/docker/stacks/ai/jev-litellm`), which forwards to TypeSafe's
System One API with the TypeSafe credential injected server-side. HeyMa holds
its own LiteLLM **virtual** key there (`WAX_JEV_API_KEY_OP`, an `op://`
reference; alias `heyma-wax-project-extraction`, $1/day budget, 120 rpm).
Never give the pass the ingress master key or the raw TypeSafe key, never
route the ticket model through the Jev key, and never route the Jev call
through the gateway token.

The model is pinned to `jev-1.13.0` (`WAX_JEV_MODEL`) because the thresholds
above were measured on 1.13; `jev-latest` resolved to `jev-1.14.0` on the
ingress on 2026-10-22. OpenRouter's `typesafe/jev-1.13` spelling 400s on System
One. The wire shape is OpenRouter's old `alpha/decisions` shape minus the body
`id` and `usage.cost`: the request id comes from the `x-typesafe-request-id`
response header, and the result's `evidence.cost` is `null` (with
`evidence.input_tokens` recorded) because the cost is the ingress's to meter.
Read it from LiteLLM's spend log:

```bash
docker exec jev-litellm-postgres psql -U litellm -d litellm -c \
  "select \"startTime\", model, spend, prompt_tokens from \"LiteLLM_SpendLogs\"
   where metadata->>'user_api_key_alias' = 'heyma-wax-project-extraction'
   order by \"startTime\" desc limit 20;"
```

Until 2026-10-22 the pass called OpenRouter's `alpha/decisions` proxy directly;
that account ran out of credits on 2026-10-21. The pass stays disabled until a
calibration re-run on the ingress. To try it without touching a note, run the
executable directly: it only prints the result it would apply.

```bash
components/wax/config/passes.d/bin/project-extraction ~/d/Transcripts/<note>.md
```

Re-running: a non-empty existing value (human or earlier run) is kept and
reused without a provider call. To redo one, clear the key in the note, then
`wax ep run project-extraction <item>` (and `ticketable` after it). Events keep
their keys, so an unchanged ticket is never published twice; a changed ticket
has a new content hash and is published as a new intake.

`wax doctor` probes what these passes cannot work without: `pjangler registry`
(reachable, and HeyMa's frozen id `transcription-queue` is registered; when it
is not, register it with `POST /v1/index` and the repo's `.project.json`, never
`pj init --apply`, which rewrites the manifest and the `heyma-pm` agent key),
`jev ingress` (the URL is the ingress, the model is not OpenRouter's spelling,
and the TypeSafe pass-through answers), `jev api key` (a virtual key, not
blocked, budget left; both probe free endpoints and run, as warnings, while
the pass is disabled), and `pass events enqueued`.

## Document runs

The same runner also enriches **plain Markdown documents** — no audio item, no
`transcripts` row, no rename. A document is content-addressed at first sight:
`doc_id = "doc-" + sha256(file bytes)[:16]`, tracked **per folder binding** in
the `documents` ledger table. The same bytes under a second binding are a
second registration (overlapping pipelines keep separate outcomes); the same
bytes at a new path within one binding are the same document (a scan
re-claims nothing).

Identity survives Wax's own writes: a run's frontmatter output changes the
file hash, so every run records the post-run hash as `current_sha256` on the
same row — the next scan sees its own write as known content, never as a new
arrival. A **human** edit after processing is new CONTENT but not a new
ARRIVAL: the path is already known, so scans leave it alone — `wax documents
run <pipeline> <file>` is the explicit path for edits, registering the edited
bytes as a new document under its own identity (no-clobber frontmatter rules
still protect human values). A retried failure adopts the failed row's
identity instead of minting a fresh `doc_id`, and only while the bytes still
match what failed — a human change supersedes the retry, it is never
consumed silently.

Pass rows key on the binding-scoped key `doc-<sha16>[@<binding-id>]`, so
gating, attempts and outcome tallies behave exactly like audio.

Differences from an audio run, all enforced in `passes.run()`:

- the pass must declare `capabilities: [document]` (with or without `audio`);
  otherwise it records `skipped` / `inapplicable_capability` and satisfies
  ordering exactly like any other skip — a dependency gated on an
  inapplicable pass RUNS, it is never silently dropped;
- the note is the document itself; there is no `transcripts` provenance block,
  no `transcript.slug` rename, and `md_for()` falls back to the document path
  for `{md_path}` placeholders;
- `events` are **rejected**: a document run returns `result_apply_failed` if a
  result declares events. Document enrichment proposes frontmatter (and, with
  `body_mutation`, body edits) only; anything that should become a Bloodbank
  fact waits for the audio flow or an explicit n8n workflow.

The durable receipt is per **(binding, document, pipeline, compiled
definition)** in `document_receipts`: a completed receipt makes a duplicate
arrival a no-op; a `failed`/`halted` receipt counts attempts and
dead-letters at `WAX_DOCUMENT_MAX_ATTEMPTS` (default 3) with `exhausted_at`,
after which scans stop retrying it (visible in the catalog — recent runs
carry the `reason_code` only, never the stored error detail, which can embed
secrets). An exhausted arrival is re-armed explicitly, without ledger
surgery: `wax documents retry [<pipeline>] [--doc <doc-id>]` clears
`exhausted_at` and resets the attempt budget, and the next scan (or an
explicit `wax documents scan <pipeline>`) retries it.

Per-document exclusion is a real **cross-process `flock`** under
`var/doc-locks/` held for the whole run: a live holder cannot be stolen —
not by a timer, not by a concurrent scan or CLI run — and a crashed holder
releases it with its process (no staleness window). The DB `processing`
state is the *visible* progress marker, claimed only under the lock; a
stale `processing` row (crash mid-run) is re-claimed by the next run that
holds the lock, and a scan only adopts a `processing` retry candidate when
its run lock is actually free (probed, not guessed from timestamps).

## Safe discovery: `wax catalog`

`wax catalog --json` is the one read Holocene (and n8n's loadOptions) consume.
It is a safe projection by construction: every registered pass with its
purpose, `capabilities`, availability **and why** (`available`, `disabled`,
`placeholder`, `quarantined`, `unparseable`), run conditions, dependency edges,
declared emissions, and a bounded outcome tally per state (`completed`,
`failed`, `skipped`, `running`, `exhausted`, `stale` — counts at the current
version, older versions folded into `stale`). Pipelines (below) appear with
authored/compiled identity, drift, the **frozen compiled graph** (nodes with
their authored n8n names, conditions, per-node registry drift; the stored edge
list, never re-derived from the live registry), bindings with scan timing,
bounded `recent_runs` from the durable receipts, and a `worker` freshness
block (heartbeat age, pipeline-enabled state, document scan cadence) that
distinguishes *live*, *healthy idle* and *worker down*. The Never list is
enforced by an allowlist in `src/wax/catalog.py`: no `env`, no command argv,
no result bodies, no stderr — a key added to the catalog is a key a hosted
surface may display, so additions are deliberate.

## Pipelines

A pipeline is an **authored graph** compiled into an immutable, versioned
definition. Author YAML lives in `config/pipelines.d/<name>.yaml`:

```yaml
name: doc-standard
description: Document enrichment standard
trigger: documents          # transcription | documents | manual
enabled: false
failure_policy: independent # independent | halt
folders: [/home/delorenj/d/Inbox/Wax]
passes: [classification, title-slug]
```

A pass entry may also be an object with per-node graph authoring:
`{slug: title-slug, when: [...], requires: [classification], after: [...]}` —
the same mapping an n8n import produces.

**The compiled definition is frozen and is the only thing that executes.**
Each node carries the full runnable pass definition as it stood at compile
time (command, env, edges, conditions, capabilities, version) and the edge
list is stored, not re-derived. A registry edit therefore changes nothing
until an explicit recompile; `registry_drift` in the catalog names exactly
which frozen nodes no longer match the registry.

- **Compile is explicit**: `wax pipeline compile [names…]` (no args = all
  authored). The compiled definition and its sha256 `definition_id` are stored
  in the `pipelines` table; `version` increments when the definition changes.
  Nothing recompiles on its own — `wax pipeline list` reports `drift`
  (authored ≠ compiled) until a human or n8n compiles, and drift is a visible
  badge in Holocene, never an error.
- A `requires` edge naming a pass **outside the graph is a compile error** —
  a missing required dependency is never silently dropped. `after` edges may
  leave the graph (vacuously satisfied) and are recorded as `after_external`.
  Cycles, duplicate passes, unknown slugs and non-automatic passes in a
  `transcription` pipeline are all loud compile failures.
- With exactly one compiled **and enabled** `trigger: transcription` pipeline,
  new audio plans are minted from its frozen definition
  (`pipelines.transcription_authority()`); with none, the legacy
  live-registry plan stands; with more than one, the choice is refused and
  recorded (`ambiguous_transcription_authority`) before falling back — an
  ambiguous graph is never fabricated as the active one. A minted plan
  NEVER migrates — completed or not: an item finishes (or is retried) under
  the graph it was given, because its attempts, exhaustion receipts and
  halt policy all hang off that plan. New items get the new graph; moving
  an unfinished item onto a changed authority is an explicit operator
  decision, never a scan side effect.
- `failure_policy: halt` stops a document run at the first failed node;
  `independent` runs siblings anyway. Either way the run is recorded and only a
  fully-terminal run writes the completed receipt — a failed pass stays
  retryable, and completed passes replay as no-ops on re-run.

## Authoring in n8n: `wax pipeline import`

An exported n8n workflow JSON is a second authoring source — nothing more:

```
wax pipeline import <export.json> [--name slug]
```

- the **Wax Pipeline** wrapper node supplies trigger / folders / failure
  policy / enabled / description;
- **Wax Pass** nodes (the per-pass node types generated from
  `WaxPassNodeBase`, or the generic Wax Enrichment Pass) are the graph nodes,
  with optional `when` / `skipWhen` JSON conditions;
- a **`main`** connection is a continue edge (`after`, ordering only); a
  **`requirement`** connection is a gated edge (`requires`);
- sticky notes are annotations; everything else — native triggers, actions,
  dynamic (`=`-prefixed) parameters, unknown connection types, cycles,
  duplicate passes — is **rejected loudly**. An exported schedule trigger is
  rejected by construction, so n8n can never become a second scheduler.

Import compiles the frozen definition exactly like YAML authoring and stores
the source identity (`n8n`, workflow name, export hash). Re-importing a
changed export is the explicit adopt; `drift` for an n8n-sourced pipeline
reports the workflow identity (authored content lives in n8n, which Wax
cannot read), and `registry_drift` still reports frozen-vs-registry movement.

## Document folder bindings

A binding (`documents` trigger) is **new-arrivals-only**:

- the first scan of an **existing** folder seeds it by registering every
  document already there **without processing** (state `seeded`); a missing
  folder is reported `folder_missing` and never claims to be seeded — the day
  it appears, it seeds then. An **unreadable** file is a `seed_error`: the
  binding stays unseeded and the next scan retries the seed, because claiming
  a folder seeded when a file could not even be hashed is how history goes
  quietly uncovered. Covering seeded history needs an explicit
  `wax documents backfill <pipeline>`.
- **Arrival is a PATH question, not a content question**: a file whose path
  the binding already knows is never an arrival, whatever its bytes. Human
  edits to notes Wax has already seen are deliberately not auto-processed —
  `wax documents run <pipeline> <file>` is the explicit path for edits. A
  genuinely new path whose content the binding already ran (a move) is not
  an arrival either; only new paths with content this binding has never
  processed are picked up.
- The worker scans bindings on its own cadence
  (`WAX_DOCUMENT_SCAN_INTERVAL_S`, default 120s, gated on
  `wax pipeline enable`): a file dropped into a bound folder processes
  without anyone typing a scan, and **failed arrivals retry each scan** until
  they recover or exhaust their attempt budget. The scan is bounded,
  lock-guarded (the per-document flock above) and **isolated per document**:
  one document raising mid-scan is reported as an error result and never
  stops its siblings. The worker's unnamed scan also respects a compiled
  pipeline's `enabled: false` (refused) — while **naming the pipeline**
  (`wax documents scan <pipeline>`) is an explicit operator action that runs
  the compiled graph even while disabled: the toggle gates automatic pickup,
  not a human invoking the pipeline.
- `wax documents list --binding B --state pending|seeded|processed|failed`,
  `wax pipeline bindings`, and the catalog all report binding state: folder
  existence, document counts by state, exhaustion counts, last scan time, and
  the worker heartbeat that says whether anyone is scanning at all.
- `wax documents run <pipeline> <file> [--binding B]` is the explicit manual
  executor; without `--binding` it defers to any binding that already
  completed the same content at the same definition (a duplicate drop is a
  no-op), while bindings never suppress one another.
