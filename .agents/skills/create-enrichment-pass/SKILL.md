---
name: create-enrichment-pass
description: Create, extend, or refactor modular enrichment passes (EPs) for text-document pipelines. Use when adding title, summary, slug, tag, classification, entity, link, or other metadata enrichment; making a transcript pass reusable for ordinary documents; defining a pass registry or result contract; or testing pass isolation, versioning, provenance, and no-clobber behavior.
---

# Create Enrichment Pass

## Goal

Build an EP as a portable, side-effect-free document processor. Treat a
transcript as a document with an optional audio relationship, not as a separate
kind of enrichment input. Keep filesystem renames, database updates, archive
links, notifications, and pipeline state in the host runner or a thin adapter.

Before implementing a pass, read [references/contract.md](references/contract.md)
completely. It defines the command boundary, result envelope, ownership rules,
and the Wax compatibility adapter.

## Non-negotiable invariants

- Accept one document path and an optional opaque item ID. Do not require audio.
- Read the document, but do not mutate it from the pass process.
- Emit declarative JSON; let the runner apply changes atomically.
- Return grounded enrichment only. Never invent missing source facts.
- Preserve non-empty human or upstream values unless the registry explicitly
  authorizes a field in `clobber`.
- Never return runner-owned identity, provenance, capture, or archive fields.
- Keep passes independently runnable and independently versioned. One EP failure
  must not prevent sibling EPs from running.
- Put transcript/audio/S3 behavior in an adapter. The enrichment core must also
  work for a normal Markdown or text document.

## Workflow

### 1. Map the host before editing

Locate the current runner, registry, result parser, metadata writer, pass-state
store, and tests. Record:

- supported command placeholders and environment variables;
- result-envelope version and recognized mutation intents;
- protected fields and no-clobber behavior;
- automatic-run ordering, retry semantics, and version skip rules;
- host-only side effects such as rename, ledger updates, or source-object links.

If the host is Wax, also read its current `ENRICHMENT-PASSES.md`, registry
entries, `passes.py`, and at least one pass test. Prefer the live implementation
over stale design documents.

### 2. Define the pass boundary

Write down the pass's inputs, outputs, and owned fields before writing code.
Classify each desired behavior as one of:

1. **Portable enrichment** — values derived from document content, such as
   title, summary, slug, tags, entities, or classification.
2. **Document mutation intent** — rename or metadata updates that the runner
   should apply safely.
3. **Host effect** — transcript ledger changes, audio/S3 linkage, queue state,
   notifications, or another integration-specific action.

Implement category 1 in the shared core. Express category 2 declaratively.
Implement category 3 only in a host adapter. If the pass cannot be described
without embedding host state in the core, redesign the boundary.

### 3. Scaffold the files

Use the bundled generator when the host uses a YAML registry and command
executables:

```bash
python scripts/scaffold_pass.py PASS-SLUG --registry-dir PATH [--host wax]
```

The generator refuses to overwrite existing files. It creates a disabled,
non-automatic registry entry and a side-effect-free Python executable. Inspect
both outputs, then replace the scaffold's enrichment function with grounded
logic. For a different registry format, preserve the same executable contract
and create the host registration manually.

### 4. Implement the portable core

- Name the main function for documents, for example `enrich_document`; avoid
  `enrich_transcript` unless the algorithm genuinely depends on transcript
  structure such as speaker turns.
- Isolate model/network calls behind a small function that can be replaced in
  tests.
- Bound context deterministically and retain semantically useful regions.
- Validate model or parser output before building the result envelope.
- Make reruns idempotent: the same content and configuration should produce the
  same effective metadata.
- Write diagnostics to stderr or before the result; print one compact result
  object as the final stdout line.

### 5. Add only the necessary adapter

Use the portable result directly for metadata-only passes. If the host needs a
rename, source link, database update, or another effect, add the smallest
adapter that translates the portable result into a host-supported intent.

For Wax, keep `transcript.slug` and `link_audio` compatibility in that adapter;
do not make the shared enrichment function read the transcript ledger or call
S3. A generic document runner should be able to omit the adapter and still use
the same enrichment function and frontmatter result.

### 6. Register conservatively

- Start at `version: 1`; increase it whenever behavior or output semantics
  change and existing documents should rerun.
- Start with `enabled: false` and `auto: false` while testing.
- Keep `requires: []` unless ordering is a proven semantic dependency.
- Keep `clobber: []` unless overwriting a specific field is an explicit product
  decision.
- Set a finite timeout and declare model/config values in the registry or
  environment rather than embedding machine-specific paths in the core.

### 7. Prove interchangeability

Test the same portable core against at least:

- an ordinary document fixture with frontmatter and body text;
- a transcript fixture, including transcript-shaped text or speaker labels;
- a document with pre-existing owned values to prove no-clobber behavior;
- malformed, empty, and oversized input;
- invalid model/processor output and timeout/error paths.

Then test the host adapter separately for atomic writes, collision-safe rename,
state recording, and any archive/source linkage. Verify that a failed pass does
not gate another pass.

### 8. Enable and document

Run focused tests, the host suite, and a dry/manual invocation that captures the
final JSON. Enable the pass only after both a generic-document fixture and the
target host fixture pass. If setting `auto: true`, verify its exact lifecycle
position and rerun/skip behavior. Document owned fields, external dependencies,
version changes, and optional host effects.

## Completion report

Report the pass slug/version, portable fields, host adapter effects, registry
state, tests run, and any migrations or reruns required. Explicitly state
whether the same core was exercised against both a document and a transcript.

