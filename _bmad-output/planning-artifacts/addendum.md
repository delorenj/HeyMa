# Wax Enrichment Pass Control — Brownfield Addendum

This companion preserves implementation evidence and technical constraints that do not belong in the PRD's capability language.

## Confirmed failure mechanism

Recent retained logs under `var/logs/<wax-item-id>/transcription.1.log`, including item `6927a6cd9fe492d7`, show that Wax requests diarization but the active transcription interpreter cannot import `whisperlivekit`. The dedicated environment has stale editable-install metadata for `whisperlivekit 0.2.8` pointing at package sources removed in commit `1d21e8b`; only bytecode cache residue remains. `scripts/transcribe.py` logs the missing dependency, continues, and emits metadata with `"diarized": false`. The user-visible defect is therefore runtime integrity plus silent degradation, not a missing default flag alone.

## Existing extension points to preserve

- `components/wax/src/wax/config.py` currently owns transcription policy defaults through environment values.
- `components/wax/src/wax/transcribe_adapter.py` translates Wax policy into the transcribe CLI and persists the actual `diarized` metadata bit.
- `components/wax/src/wax/passes.py` owns registry loading, execution, version idempotency, result application, event emission, and `wax.passes` frontmatter history.
- `components/wax/src/wax/ledger.py` already stores one current row per `(item_id, ep_slug)` with version, state, attempt, command ID, time, and detail.
- `components/wax/src/wax/worker.py` runs automatic EPs after transcription and preserves EP failure independence.
- `components/wax/bin/wax` already supports `wax ep list|run|run-all|status`; new forms must be additive.

## Recommended implementation boundary

- Parse TOML with Python's standard-library `tomllib`; Wax already requires Python 3.11 or newer.
- Resolve Canonical Config from `XDG_CONFIG_HOME`, falling back to `~/.config`; do not auto-write user configuration.
- Use precedence: explicit operation argument, legacy environment override, Canonical Config, built-in default.
- Remove the deployed/source `waxd.service` hard pin `WAX_DIARIZATION=1`; otherwise the compatibility environment layer permanently shadows `transcription.diarize = false` in Canonical Config.
- Give EP definitions stable user-facing metadata (`display_name`, `description`, `version`, enabled/automatic state) and a runtime availability check separate from registration.
- Reuse the existing `passes` row as the durable current job projection by adding and transactionally claiming a `queued` state unless implementation analysis proves that attempt history requires a separate append-only jobs table. Existing Bloodbank events and Transcript frontmatter remain the attempt audit surfaces.
- Keep automatic and manually queued execution on the same pass runner. A manually requested Diarize repair must use the Wax Item's ledger-linked source audio and Transcript; it must not infer either from a filename.
- The Diarize implementation may share the existing transcription/Sortformer functions, but the EP outcome must be recorded through the generic pass contract. Do not duplicate a second untracked diarization path.
- Close the current post-hoc transformation gap deliberately: timed ASR segments exist only in process memory, registered commands receive no source-audio placeholder, and `wax.ep.v1` cannot replace a document body. The implementation must add a bounded, atomic, document-neutral body mutation and a host-resolved source-audio input, or an equivalent durable intermediate contract. A YAML registration alone cannot satisfy Diarize.

## Safety and compatibility guardrails

- Do not mutate the operator's live `~/.config/wax`, `~/HeyMa`, vault, database, or S3 objects in automated tests.
- Preserve `wax ep list`, `wax ep run`, `wax ep run-all`, and `wax ep status` while adding `wax ep -l|--list`, `wax info`, and `wax job`.
- Preserve the rule that a failed EP does not block sibling EPs or safe source-audio parking.
- Preserve content-addressed Wax Item identity and the existing Transcript/S3 join metadata.
- Do not treat `diarized: false` as a completed Diarize EP when the configured desired state was true.
- Eliminate or explicitly guard the current dual-checkout transcriber resolution (`waxd` runs from `/home/delorenj/HeyMa`, while `PATH` currently resolves `transcribe` from `/home/delorenj/code/HeyMa`). Both happen to share the same commit today, but configuration and runtime-readiness tests must not depend on that coincidence.
