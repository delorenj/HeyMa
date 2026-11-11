# Passes: how enrichment works

An **enrichment pass** (EP) is a small command that reads a transcript or a
Markdown document and returns declarative JSON that the runner applies. The
runner (`components/wax/src/wax/passes.py`) owns every write, rename, receipt,
retry and event; a pass owns only its answer. The design of record is
`components/wax/docs/ENRICHMENT-PASSES.md`; this page is the operating summary,
verified against `passes.py`, `worker.py`, `finalize.py` and the registry on
2026-11-05. Per-pass rows are in [passes/catalog](catalog.md); the chat-model
route is in [passes/model-chain](model-chain.md); the dictionary pass has its own
page, [passes/dictionary](dictionary.md), and so does the voice registry,
[passes/voice-id](voice-id.md). To author one, load the
create-enrichment-pass sub-skill
([SUBSKILL](../../subskills/create-enrichment-pass/SUBSKILL.md)); its
[contract](../../subskills/create-enrichment-pass/references/contract.md) is the
result envelope of record.

## The shape of a run

```
inbox audio --archive--> S3 --Whisper--> transcript note --passes--> publish events --> park audio --> transcription.completed
```

After transcription ([transcription](../transcription.md)) `worker.process` calls
`passes.run_auto(item)`, which runs every enabled `auto` pass in dependency
order. Each pass is one child process with a hard timeout. A failing pass never
stops a sibling, except through `requires`. After the passes, finished pass
events are published (`finalize.publish_pass_events`), the archived audio is
linked to its transcript, the audio is parked, and `finalize.finalize` writes
`transcription.completed` only when **every planned pass** is `completed` or
`skipped` at the plan's version. Otherwise the item stays `enrichment_pending`
(cause `completion_withheld`) until a retry succeeds.

## What runs today (2026-11-05)

New audio plans come from the compiled **`transcript-standard` v14** pipeline
(nine passes; v13 added `voice-id` on 2026-11-11, v14 re-froze it with the edge moved into the pipeline); documents from **`doc-standard` v10** (classification,
title-slug, project-extraction on the folder `~/d/Inbox/Wax`). Ordering edges
live in the pipeline, not in the registry YAML.

| Step | Pass | Edge in `transcript-standard` |
|---|---|---|
| 1 | `frontmatter-stamp` | none |
| 2 | `classification` | none |
| 3 | `diarization` | registry `after: [classification]`; skips a `monolog` |
| 4 | `dictionary-correction` | after diarization |
| 5 | `title-slug`, `project-extraction`, `clipboard-copy`, `voice-id` | each after `dictionary-correction` |
| any | `call-metadata` | none; skips non-calls with `not_a_call` |

Manual pipelines (all `in_sync`): `title-only` v1, `dictionary-only` v1 and
`untitled-standard` v1 (halt policy; `summarize` then `title-slug`, which
requires `summarize`). `project-extraction` has been enabled since 2026-10-29;
older notes that call it disabled are stale. Compile, freeze and drift rules
are in [pipelines](../pipelines.md); document runs are in
[documents](../documents.md).

## The registry

Passes live in **`components/wax/config/passes.d/*.yaml`** (executables in
`passes.d/bin/<slug>`), resolved from the component root via `component.PASSES`,
never from cwd or a runtime dir. The registry is re-read on every call, so a
YAML edit is live for manual and legacy-registry runs; the Python runner is
imported once and needs `systemctl --user restart waxd`. Compiled pipelines
froze their config at compile time and adopt a change only by
`wax pipeline import <export.json> --name transcript-standard|doc-standard`.

| Field | Meaning |
|---|---|
| `slug`, `version`, `description` | Identity. Bump `version` when behavior or output changes; items rerun the new definition once. A bump is not a backfill. |
| `enabled`, `auto` | `enabled` makes it runnable; `auto` puts it in the post-transcription flow and the item's stored plan. Defaults false. |
| `capabilities` | `[audio]`, `[document]` or both. Absent means audio only. A document run skips a pass without `document` as `inapplicable_capability`. |
| `requires` | Orders **and gates**. The dependent records `dependency_failed` until each requirement is `completed` or `skipped` at the wanted version. An `auto` pass may only require `auto` passes. |
| `after` | **Orders only**; a failed target never stops the dependent. Must name an enabled pass. |
| `when` | All clauses must hold, read from the note's frontmatter at run time; the first unmet one records `skipped`/`condition_unmet`. Operators: `equals`, `not_equals`, `in`, `not_in`, `exists`, `absent`, `empty`, `not_empty`, `length`, `min_length`, `max_length`. |
| `skip_when` | Legacy single clause; a match records `skipped`/`condition_matched`. Prefer `when`. |
| `kind: diarization` | Also skips `explicitly_disabled` when `WAX_DIARIZATION` is `0/false/no/off`. |
| `body_mutation` | `compare-and-replace` authorizes `body_replace`; absent means a returned one fails the pass. |
| `clobber` | Frontmatter keys the pass may overwrite when non-empty. Default none. |
| `emits` | `<entity>.<action>` event types the pass may declare. Default none. |
| `env` | Added to the child's environment; wins over the unit and the shell. The runner sets `WAX_PASS_TIMEOUT_S` after this block. |
| `frontmatter_schema` | Vault taxonomy stamped before the command runs (best effort). |
| `timeout_s` | Hard limit (default 900). A timeout is `timeout`. |
| `command` | argv with `{component_root}`, `{home}`, `{item_id}`, `{md_path}`. |

Ordering is a depth-first walk over `requires` and `after` together; unrelated
passes run alphabetically by slug.

**Two ways a bad YAML bites.** A YAML parse error **silently disables** that
pass (it simply disappears from the registry). A malformed
`requires`/`after`/`when`/`skip_when`, a cycle, a missing or disabled
requirement, or an `auto` pass requiring a manual one **quarantines** the pass
and its dependents (`!! QUARANTINED` in `wax ep list`; a `pass registry
quarantine` FAIL in `wax doctor`; `quarantined` in the `run_all` report, no
ledger row). After any edit, read `wax ep list` back.

## Results, ownership and the transcriptions store

A pass prints one JSON object as its last stdout line (`wax.ep.v1`):
`frontmatter` proposals, optional `state`/`reason_code` skip, audio-only
`transcript.slug`, `body_replace {sha256, text}` and `events`. The runner applies
frontmatter with **no-clobber** (a value lands only if the existing one is empty
`None`/`""`/`[]`/`{}`, identical, or in `clobber`), rejects protected provenance
keys, and ignores `link_audio`. A pass reports a classified failure with
`reason_code=<code>` as the first stderr line (`^reason_code=([a-z_]+)$`). All of
that is spelled out in the
[contract](../../subskills/create-enrichment-pass/references/contract.md); the
codes are in [reason codes](../../subskills/doctor/references/reason-codes.md).

State is recorded twice: in the ledger (`passes` table: `item_id`, `ep_slug`,
`state`, `attempt`, `command_id`, `version`, `reason_code`, `detail`,
`definition_id`, `exhausted_at`) and in the note's `wax.passes.<slug>` block
(`state`, `at`, `version`, `command_id`, `attempt`, `reason_code`/`detail`, and
on success `model`, `served_by`, `route`, `fell_through`).

Every **applied** pass becomes a version in the transcriptions store
(`pass:<command_id>`); a skip or failure is only a `pass_run`. A re-run that
changes nothing records `no_change`, and a frontmatter-only pass costs zero
embeddings. See [transcriptions-store](../transcriptions-store.md).

## Retries, the sweep and exhaustion

| Mechanism | Behavior |
|---|---|
| Manual | `wax ep run <slug> <item>` runs one pass for one item under the item's **frozen plan** (not the live registry) and ignores the operator switch. `wax ep run-all <item>` runs every enabled pass. `wax ep status [item]` shows recorded outcomes. |
| Sweep | `wax ep sweep` re-runs every enabled pass whose latest attempt `failed` with `attempt < --max-attempts` (default 3), then `run_auto` per item, `publish_pass_events` and `finalize`. The waxd worker does the same every `WAX_SWEEP_INTERVAL_S` (900 s) via `worker.sweep_failed_passes()`, so a transient provider outage self-heals. |
| `dependency_failed` | Exempt from the bound: the pass never ran, and the gate lifts when a *different* pass succeeds. |
| Exhaustion | At attempt >= 3 the runner stamps `passes.exhausted_at` and emits `bloodbank.audio.task.exhausted` once per (item, slug, version); a version bump clears the receipt. This is the dead-letter signal. `wax ep sweep --dry-run` reports nothing for exhausted passes; they need `wax ep run <slug> <item>`. |
| Replan | An unfinished item minted under an old plan moves only by `wax ep replan <item> --apply`, then `wax ep run`. Completed items keep their stored plan. |

**The operator overlay.** `wax ep disable|enable <slug>` writes
`var/settings.json` `passes.disabled` (empty on 2026-11-05, last updated
2026-11-03T00:20:32Z). A disabled pass records `skipped`/`operator_disabled`,
which satisfies `requires`, so dependents still run and finalize completes. It is
never silent: `wax status` prints a `passes off` line and `wax ep list` flags the
row. Items it skipped stay skipped after re-enabling.

## Events

A pass may declare `events` (its YAML must list the type in `emits`). The
runner validates fail-closed: at most 50, a type regex, the `emits` allowlist and
the Bloodbank schema at `$WAX_BLOODBANK_SCHEMAS/bloodbank/audio/<type>.json`
(default `~/code/33GOD/bloodbank/schemas`). Pass events publish as soon as the
pass's own `requires` subgraph completes, with id
`uuid5(WAX_NS, "ep-event:<item>:<slug>:<type>:<key>")`, not behind slow siblings.
`transcription.completed` stays plan-gated, one per plan, id
`uuid5(WAX_NS, "completion:<item>:<plan_id>")`. A **document** run rejects
`events`. Each run also issues `bloodbank.cmd.audio.task.start` and the
`task.requested/started/completed|failed|skipped` chain, correlated on
`uuid5(WAX_NS, "ep:<item>:<slug>:<attempt>")`. Full schemas and relays are in
[events](../events.md).

## Gotchas

- **Registry versus plan.** Editing `passes.d` changes nothing for an item
  governed by a compiled pipeline. Use `wax pipeline import` for new items and
  `wax ep replan --apply` for unfinished ones.
- **Executable bytes are not frozen.** Editing `passes.d/bin/<slug>` changes
  behavior for every plan that invokes it.
- **A pass is not a place for a host effect.** Renames, ledger writes and S3
  linkage belong to the runner. The one exception is `voice-id`'s own registry
  (the `voice*` tables and `~/d/Voices`), see [passes/voice-id](voice-id.md).
- **No Anthropic model anywhere** (operator order 2026-10-22). Chat passes use
  `wax.provider.run_chain` ([passes/model-chain](model-chain.md)); Jev passes
  (`classification`, `project-extraction`, `dictionary-correction`) go through
  the Jev ingress on their own virtual keys, never the gateway.
- **An empty note is `skipped`, not `failed`** (`empty_document`).
- **Never edit pass YAML or compiled pipelines to "see if it clears"** during an
  incident; read the ledger first ([the doctor procedure](../../subskills/doctor/SUBSKILL.md)).
