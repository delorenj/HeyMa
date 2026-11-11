# Pipelines

A pipeline is an authored graph compiled, explicitly, into an immutable versioned definition (`pipelines` table in `var/wax.db`, sha256 `definition_id`). Execution never consults the live registry for a compiled pipeline: node commands, env, edges and conditions are frozen at compile. Author sources are either YAML (`components/wax/config/pipelines.d/<name>.yaml`) or an n8n workflow export imported with `wax pipeline import`. Code: `components/wax/src/wax/pipelines.py`. Design: `components/wax/docs/ENRICHMENT-PASSES.md` (sections "Pipelines", "Import", "Bindings").

## Live pipelines (2026-11-05)

| Pipeline | Trigger | Version | Source | State |
|---|---|---|---|---|
| `transcript-standard` | `transcription` | v14 | n8n export, `exported_snapshot` | enabled; 9 passes, independent |
| `doc-standard` | `documents` | v10 | n8n export, `exported_snapshot` | enabled; classification, title-slug, project-extraction |
| `title-only` | `manual` | v1 | YAML, `in_sync` | enabled; title-slug alone |
| `dictionary-only` | `manual` | v1 | YAML, `in_sync` | enabled; dictionary-correction alone |
| `untitled-standard` | `manual` | v1 | YAML, `in_sync` | enabled; summarize then title-slug, policy `halt` |

`transcript-standard` runs frontmatter-stamp, classification, diarization (`when classification not_equals monolog`), dictionary-correction (after diarization), clipboard-copy, title-slug, project-extraction, voice-id (each after dictionary-correction; v13/v14, 2026-11-11) and call-metadata. `doc-standard` showed no registry drift on 2026-11-05 (`drift.state` is `exported_snapshot` with empty detail and no per-node drift, on both n8n-sourced pipelines). When drift does appear it is reported beside the frozen graph and is informational while the frozen and live versions match. Check any of this with `wax pipeline list`, `wax pipeline show <name>`, `wax catalog --json`.

## Triggers and authority

`TRIGGERS = {transcription, documents, manual}`. For new audio, `transcription_authority()` resolves exactly one compiled, enabled `trigger: transcription` pipeline and mints plans from its frozen definition. With none, the legacy registry plan stands. With more than one, the ambiguity is refused and recorded, and the registry plan is used. Keep exactly one.

`transcription.completed` waits for every planned pass, so the plan is the contract for finishing an item.

## Edges and conditions

- `requires` gates: a dependent waits and reports `dependency_failed` if the requirement fails. A skipped requirement counts as satisfied; it does not promise the output exists, so gate on a meaning field with a downstream condition when output is required.
- `after` and the n8n `continue` port only order; they never gate.
- `when` and `skipWhen` carry declarative frontmatter conditions (for example `classification exists`, `classification not_equals monolog`).
- A `requires` edge naming a pass outside the graph fails compile loudly. So do unsupported nodes, unsupported expressions, malformed edges and cycles.
- Policy is `independent` (a failing node never stops siblings) or `halt`.

## Change a pipeline

1. Edit the authoring source: the n8n workflow (see [n8n](n8n.md)) or the YAML.
2. Compile it: `wax pipeline import <export.json> --name <name>` for an n8n export, `wax pipeline compile <name>` for YAML. Identical graph, config and registry inputs are a no-op; a change advances the version.
3. Existing audio plans never migrate on their own. For an unfinished item minted under the old definition: `wax ep replan <item>` (dry run), then `wax ep replan <item> --apply`, then `wax ep run <slug> <item>` per failed pass.
4. Verify with `wax pipeline show <name>`; drift is reported beside the frozen graph.

`wax ep replan` is refused when the item already completed, the authority is ambiguous, the pass set would change, or a completed or skipped pass would change definition. (Older docs say there is no audio replan; that is wrong since the command shipped.)

`wax ep run --live` uses the live registry definition instead of the frozen one, and only for finished items.

## Traps

- **Bare `wax pipeline compile` compiles every YAML file**, replacing the n8n-sourced `transcript-standard` and `doc-standard` with the YAML versions. Always name pipelines.
- **Editing `passes.d` does not change a compiled pipeline.** Pass env, including model chains and `timeout_s`, is frozen. After changing a chain, re-import both release exports (`components/n8n-nodes-heyma/examples/transcript-standard-release.json`, `doc-standard-release.json`) with `--name transcript-standard` and `--name doc-standard`. Only chain and timeout env should differ; check the diff on a ledger copy first.
- **`exported_snapshot` means Wax knows the imported export**, not that it compared the live n8n editor draft. Re-export and re-import after editing in n8n.
- **Importing a `wax-*-workflow.json` starter under another name does not activate it.** The starters are disabled examples.
- **No n8n trigger can join an import.** n8n is never a second scheduler; the Wax Pipeline node declares trigger and enablement only. Do not publish the authoring workflows.
- **Document bindings are new-arrivals-only.** See [documents](documents.md).

## Operator switches

Three independent switches exist; confusing them is common.

| Switch | Command | Effect |
|---|---|---|
| Pass operator switch | `wax ep enable\|disable <slug>` | Writes `var/settings.json`; a disabled pass is `skipped` with `operator_disabled` in every pipeline. Not a failure. `wax ep run` ignores it |
| Pipeline `enabled` | authored in the source | Whether the compiled pipeline is eligible to run automatically |
| Worker toggle | `wax pipeline enable\|disable` | Touches or removes `var/pipeline.enabled`; stops or starts claiming inbox work |

Related: [passes/catalog](passes/catalog.md) for pass state, [passes/model-chain](passes/model-chain.md) for chain changes, [control-surface](control-surface.md) for how Holocene renders the graph.
