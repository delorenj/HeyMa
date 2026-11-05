---
name: heyma-create-enrichment-pass
description: Create, extend, or refactor a portable enrichment pass (EP) for the Wax pipeline. Use when authoring a pass that derives title, summary, slug, tags, classification, entities, or links from Markdown documents or transcripts; declaring document or audio capabilities; defining a passes.d registry entry or the wax.ep.v1 result contract; or testing pass isolation, requires gating, versioning, provenance, and no-clobber behavior. Runtime operation (compiled pipelines, n8n import, retries) is the hub's pipelines and passes references; incidents go to the heyma-doctor sub-skill.
---

# Create an enrichment pass (HeyMa hub sub-skill)

Load this file by path from the HeyMa hub (`skills/heyma/SKILL.md`). It owns
**authoring** a pass. Everything about operating passes lives in the hub:

| Need | Read |
|---|---|
| How passes run: registry, `requires`/`after`, results, retries, sweep, events | [passes/index](../../references/passes/index.md) |
| One row per shipped pass (slug, enabled, capabilities, writes) | [passes/catalog](../../references/passes/catalog.md) |
| A pass that calls a chat model (gateway chain, no-Anthropic rule) | [passes/model-chain](../../references/passes/model-chain.md) |
| Compiled pipelines, freeze semantics, n8n import | [pipelines](../../references/pipelines.md) |
| Document runs, bindings, hygiene | [documents](../../references/documents.md) |
| Events a pass may declare | [events](../../references/events.md) |
| The contract itself (envelope, no-clobber, test matrix) | [references/contract.md](references/contract.md) |
| A pass is failing or silently empty | the doctor sub-skill, [SUBSKILL](../doctor/SUBSKILL.md) |

The design of record is `components/wax/docs/ENRICHMENT-PASSES.md`, with
`components/wax/src/wax/passes.py` as the code. Where this file and the code
disagree, the code wins.

## Goal

Author an EP as a portable, side-effect-free document processor. Treat a
transcript as a document with an optional audio relationship, not as a
separate kind of input. Filesystem renames, ledger updates, archive links,
notifications and pipeline state belong to the runner or a thin, declared
adapter.

Read [references/contract.md](references/contract.md) completely before
implementing. It defines the command boundary, result envelope, capability
semantics, ownership rules and the Wax adapter surface.

## Non-negotiable invariants

- Accept one document path and an optional opaque item ID. Never require audio
  to derive text metadata.
- Read the document; do not mutate, rename, move, delete, upload or tag it from
  the pass process. Emit declarative JSON; the runner applies changes
  atomically.
- Return grounded enrichment only. Never invent missing source facts; never
  fabricate transcript or audio provenance a document does not have.
- Preserve non-empty human or upstream values (including `0`, `false` and
  non-empty lists) unless the registry authorizes the field in `clobber`. Never
  return runner-owned identity or provenance fields (`wax-item-id`, the `wax`
  block, `captured`, `created_at`, `source`, `source-audio`, `source-s3-key`,
  `source-s3-uri`, `source-sha256`, `vault-id`; `_PROTECTED_FRONTMATTER` in
  `passes.py`, verified 2026-11-05).
- Capabilities are an explicit opt-in: `capabilities: [document]`, `[audio]` or
  both. Absent means audio-only. Declare only proven capabilities (step 3).
- Independence is a default, not a law. Siblings keep running past a failed
  pass, but `requires` gates dependents (`dependency_failed`), a pipeline may
  choose `failure_policy: halt`, and a failed pass withholds its item's
  completion until retried. State the gating you mean.
- Report classified failures with `reason_code=<code>` as the first stderr
  line. Codes are lowercase letters and underscores only (the runner's header
  regex is `^reason_code=([a-z_]+)$`).
- No Anthropic model, anywhere, by operator order (2026-10-22). A chat pass uses
  the gateway chain, and `chat_pass_routing_test` fails on any
  claude/sonnet/haiku/opus/fable/anthropic model in pass env, bin or library
  defaults. See [passes/model-chain](../../references/passes/model-chain.md).
- Never write a credential into a pass, YAML or test. Use an `op://` reference
  by item UUID in the registry `env`.

## Workflow

### 1. Map the host before editing

From the hub root (`~/HeyMa`), read the current runner and registry:
`components/wax/src/wax/passes.py`, `components/wax/config/passes.d/*.yaml`,
the executables in `components/wax/config/passes.d/bin/`, and at least one pass
test under `components/wax/tests/`. Record the command placeholders, the result
envelope version, protected fields, ordering and retry semantics, and which
effects only the runner may perform. Prefer the live code over any document,
including this one.

### 2. Define the pass boundary

Write down inputs, outputs and owned fields before code. Classify each
behavior as:

1. **Portable enrichment**: values derived from document content (title,
   summary, slug, tags, entities, classification).
2. **Document mutation intent**: rename or metadata updates the runner applies
   safely, expressed declaratively in the result.
3. **Host effect**: ledger changes, audio/S3 linkage, queue state,
   notifications. Only in a host adapter or the runner.

Implement category 1 in the shared core, express category 2 declaratively, and
keep category 3 out of the core. If the pass cannot be described without host
state in the core, redesign the boundary.

### 3. Declare capabilities explicitly

- `[document]`: runs over plain Markdown documents (document bindings). A
  document run skips a pass lacking `document` with `inapplicable_capability`,
  which satisfies ordering like any skip: a dependency gated on a skipped pass
  still runs.
- `[audio]`: transcript notes of audio items. The legacy audio runner does not
  enforce the inverse gate; test declared support and do not infer a denial.
- `[document, audio]`: the same grounded logic truly serves both shapes. Prove
  it against a document fixture and a transcript fixture.

An audio-dependent pass (diarization) declares `audio` and is tested with real
source context, never faked into document success. A notification or effect
pass (clipboard, relay) is a host adapter, not portable text enrichment. The
shipped shapes are in [passes/catalog](../../references/passes/catalog.md).

### 4. Scaffold the files

The generator is `scripts/scaffold_pass.py` (stdlib only, plain interpreter, no
repo task runner). From the hub root:

```bash
python3 skills/heyma/subskills/create-enrichment-pass/scripts/scaffold_pass.py PASS-SLUG \
  --registry-dir components/wax/config/passes.d --host wax \
  [--capability audio] [--capability document] [--description "..."]
```

- The slug starts with a letter and uses only lowercase letters, digits and
  hyphens (`NAME_RE` in `pipelines.py`, which also gates n8n import names).
- Wax scaffolds default to `capabilities: [document]`; repeat `--capability`
  for both or pass `--capability audio` alone.
- It writes `passes.d/<slug>.yaml` and the executable `passes.d/bin/<slug>`,
  refuses to overwrite, and emits a disabled, non-automatic entry whose
  executable is side-effect free and reports failures with a first-line
  `reason_code=`.

Replace the scaffold's enrichment function with grounded logic. The scaffold's
own tests are isolated and stdlib-only:

```bash
cd ~/HeyMa/skills/heyma && python3 -m pytest subskills/create-enrichment-pass/tests -q
```

(17 passed, 6 subtests, 2026-11-05.) `python3 -m unittest discover -s
subskills/create-enrichment-pass/tests` also works.

### 5. Implement the portable core

- Name the main function for documents (`enrich_document`); use
  `enrich_transcript` only when the algorithm depends on transcript structure
  such as speaker turns.
- Isolate model and network calls behind a small function that tests replace.
  For a chat model use `wax.provider.run_chain`, never a bespoke HTTP call, so
  fall-through, deadline and no-Anthropic behavior stay uniform.
- Bound context deterministically and keep semantically useful regions.
- Validate model or parser output before building the result envelope.
- Make reruns idempotent: same content and configuration, same metadata.
- Diagnostics go to stderr (with a `reason_code=<code>` first line for a
  classified failure); one compact result object is the final stdout line.

### 6. Add only the necessary adapter

Use the portable result directly for metadata-only passes. The runner already
interprets the host intents:

- `transcript.slug` is an **audio-only** adapter intent: a collision-safe rename
  of the transcript note. A document run drops it.
- `link_audio` is historical: accepted and ignored. Do not emit it.
- `events` are the audio flow's contract (registry `emits` allowlist, Bloodbank
  schema validation, runner-side publication). A document run that declares
  events fails itself with `result_apply_failed`. See
  [events](../../references/events.md).

The shared enrichment function never reads the ledger, calls S3 or publishes
events.

### 7. Register conservatively

- Start at `version: 1`. Bump it whenever behavior or output semantics change
  and items should rerun the new definition once. A bump is not a migration and
  not a backfill: minted audio plans never move to a new definition, and
  completed items keep their stored plan. Reaching history is an explicit
  operator action (`wax ep run <slug> <item>`, `wax ep replan <item> --apply`,
  `wax documents backfill`).
- A compiled pipeline freezes the registry configuration (command argv, env,
  edges, conditions, capabilities, version), not the executable bytes. Editing
  an executable changes behavior for any plan invoking that path. Registry YAML
  edits are live for manual and legacy-registry runs; runner Python needs
  `systemctl --user restart waxd`. See [pipelines](../../references/pipelines.md).
- A YAML parse error silently disables the pass, and a malformed
  `requires`/`after`/`when`/`skip_when` quarantines it and its dependents
  (`!! QUARANTINED` in `wax ep list`, a `pass registry quarantine` FAIL in
  `wax doctor`). Write the file atomically and read `wax ep list` back.
- Start with `enabled: false` and `auto: false` while testing.
- Keep `requires: []` unless ordering is a proven semantic dependency; `after`
  orders without gating. Keep `clobber: []` unless overwriting a field is an
  explicit product decision.
- Set a finite `timeout_s`; declare model and config values in the registry
  `env` (keys as `op://` item-UUID references), never machine-specific paths in
  the core.
- A chat pass also needs its route in the token scope: edit
  `components/wax/deploy/provision-inference` (the SSOT) and re-run it, never
  patch the token by hand.
- Add the pass's row to [passes/catalog](../../references/passes/catalog.md).
  The drift-guard test (`components/wax/tests/skill_hub_test.py`) fails when a
  `passes.d` slug is missing from the catalog or its enabled column disagrees
  with the YAML.

### 8. Prove interchangeability

Test the same portable core against:

- an ordinary document fixture with frontmatter and body text;
- a transcript fixture, including speaker labels;
- a document with pre-existing owned values (no-clobber);
- malformed, empty and oversized input;
- invalid model or processor output and timeout paths, each reporting a valid
  first-line `reason_code=`;
- capability behavior: a document run skipping an audio-only pass
  (`inapplicable_capability`) while a dependency gated on it still runs.

Then test the host adapter separately (atomic writes, collision-safe rename,
state recording, archive linkage). Verify the gating you declared: a failed
`requires` target blocks its dependent (`dependency_failed`), an independent
sibling proceeds, a `halt` graph stops at the first failure. The full matrix is
at the end of [references/contract.md](references/contract.md).

### 9. Enable and document

```bash
cd ~/HeyMa/components/wax && python3 -m pytest tests          # or: mise run wax:test
wax ep list                                                    # registered, not quarantined
wax ep run <slug> <item>                                       # one real item, capture the JSON
wax doctor                                                     # registry quarantine + route probes
```

Enable only after both a generic-document fixture and the target host fixture
pass. If setting `auto: true`, verify its lifecycle position and rerun/skip
behavior, then re-import the n8n release exports so the compiled pipeline picks
the pass up (`wax pipeline import <export.json> --name transcript-standard|doc-standard`).
Commit and push the pass, its test and the catalog row together.

## Completion report

Report the pass slug and version, declared capabilities, portable fields, host
adapter effects, registry state, tests run, and any explicit reruns or
backfills required. State whether the same core ran against both a document
and a transcript, and name the gating edges the pass declares.
