---
stepsCompleted:
  - step-01-validate-prerequisites
  - step-02-design-epics
  - step-03-create-stories
  - step-04-final-validation
inputDocuments:
  - _bmad-output/planning-artifacts/prd.md
  - _bmad-output/planning-artifacts/addendum.md
  - _bmad-output/planning-artifacts/architecture/architecture-HeyMa-2026-08-12/ARCHITECTURE-SPINE.md
  - _bmad-output/planning-artifacts/architecture/architecture-HeyMa-2026-08-12/MIGRATION-PLAN.md
  - _bmad-output/planning-artifacts/architecture/architecture-HeyMa-2026-08-12/contracts/CONTROL-CONTRACT.md
  - components/wax/docs/ENRICHMENT-PASSES.md
  - .agents/skills/create-enrichment-pass/references/contract.md
---

# AudioTranscriptionPipeline - Epic Breakdown

## Overview

This document provides the complete epic and story breakdown for AudioTranscriptionPipeline, decomposing the requirements from the Wax Enrichment Pass Control PRD and the approved architecture and enrichment contracts into implementable stories.

## Requirements Inventory

### Functional Requirements

FR1: Wax loads persistent settings from `$XDG_CONFIG_HOME/wax/config.toml`, defaulting to `~/.config/wax/config.toml`, uses documented defaults when the file is absent, does not create the file implicitly, and reports invalid values precisely.

FR2: Wax enables the Diarize EP automatically by default through `transcription.diarize = true`, honors an explicit false value, and distinguishes intentional disablement from failure.

FR3: Wax records the truthful Diarize outcome for every automatically requested item; missing or broken prerequisites remain visible and retryable while the base Transcript and source audio remain preserved.

FR4: `wax ep -l|--list` lists every registered EP in numbered human-readable form and stable JSON, including display name, description, version, enabled/automatic state, availability, and unavailable reason, while preserving legacy `wax ep` subcommands.

FR5: `wax info -i|--item-id <wax-item-id>` identifies the Wax Item and Transcript, separates applied from available-but-unapplied EPs, and distinguishes queued, running, failed, disabled, unavailable, and completed-version states.

FR6: `wax job -p|--enrichment-pass <ep-slug> -i|--item-id <wax-item-id>` validates and durably enqueues an eligible unapplied EP without executing it synchronously, deduplicates live duplicate requests, rejects an already-applied current version, and permits a newer version.

FR7: The Wax worker atomically claims queued EP Jobs, executes them through the existing independent pass runner, persists observable terminal state across restarts, and preserves version idempotency and sibling isolation.

### NonFunctional Requirements

NFR1: EP discovery, inspection, enqueue, execution, and retry never delete or overwrite source audio, a Transcript, S3 identity metadata, or another irreplaceable artifact.

NFR2: EP Job enqueue, claim, and state transitions are transactionally safe across concurrent daemon and CLI processes.

NFR3: One EP failure never gates a sibling EP, verified source-audio parking, or a later retry.

NFR4: Existing Wax CLI commands, environment overrides, content-addressed item IDs, ledger rebuild behavior, `wax.ep.v1`, and enrichment result semantics remain backward compatible.

NFR5: Automated tests isolate `XDG_CONFIG_HOME`, `WAX_ROOT`, `WAX_VAULT`, `WAX_PASSES_DIR`, the ledger, registry, and derived artifacts; they never read or mutate live operator state.

NFR6: The installed transcription runtime either proves the supported diarization backend importable and usable or exposes Diarize as unavailable/failed before it can be represented as applied.

### Additional Requirements

- AR1: Use Python standard-library `tomllib`; Wax requires Python 3.11 or newer, so no TOML dependency is added.
- AR2: Configuration precedence is explicit operation argument, compatible environment override, Canonical Config, then built-in default.
- AR3: Remove `WAX_DIARIZATION=1` from the Wax systemd unit when Canonical Config lands so the service does not permanently shadow `transcription.diarize = false`.
- AR4: Resolve or guard the dual-checkout transcriber boundary; Wax must not depend on `/home/delorenj/HeyMa` and `/home/delorenj/code/HeyMa` coincidentally being at the same revision.
- AR5: Diarize availability probes the actual backend import `whisperlivekit.diarization.sortformer_backend_offline`, not only `librosa` and NeMo metadata.
- AR6: Restore a reproducible, declared diarization runtime; stale editable metadata pointing to removed sources cannot satisfy readiness.
- AR7: The Diarize EP is registered with stable slug, display name, description, version, enabled/automatic settings, applicability, and runtime-availability evidence.
- AR8: Keep the EP portable core document-neutral: it consumes supplied normalized document content and returns declarative content-derived output without queue, ledger, object-store, vault, or lifecycle knowledge.
- AR9: A pass executable must not edit, rename, move, delete, upload, or tag its input; the runner owns validated, atomic application and all host effects.
- AR10: Preserve the `wax.ep.v1` portable envelope and existing `frontmatter`, `transcript.slug`, and `link_audio` behavior. Any document-body extension is optional, bounded, validated, and backward compatible.
- AR11: Close the post-hoc Diarize gap explicitly. The implementation must supply ledger-linked source audio and timed ASR data—or safely recompute the latter—and support atomic Transcript-body replacement while preserving frontmatter and detecting concurrent input changes.
- AR12: Resolve source audio from the Wax Item ledger/content identity, never from a filename guess. Missing source audio makes Diarize unavailable with an actionable reason.
- AR13: Preserve source/ingest and Wax-owned protected fields; human/upstream values survive unless the EP definition explicitly permits clobbering.
- AR14: Preserve deterministic command IDs, Bloodbank correlation, version-based skip/rerun behavior, collision-safe rename, immutable S3 keys, and transcript/audio link refresh behavior.
- AR15: The durable queue must claim work atomically, including queued EP work for already archived or completed Wax Items; inbox-audio scanning alone is insufficient.
- AR16: The current `passes` row may remain the current-state projection only if append-only attempt audit remains reconstructable from events; otherwise add a dedicated job-history table rather than claiming overwritten state is history.
- AR17: Legacy `wax ep list|run|run-all|status` and flexible `--json` placement remain covered by regression tests. New CLI no-op and validation failures use explicit non-zero exits without preventing sibling pass execution.
- AR18: Required tests cover absent/valid/invalid config, precedence and daemon/CLI parity; healthy/missing/broken diarization runtime; all requested short/long/JSON CLI forms; duplicate enqueue and atomic claim; restart/retry/new-version behavior; completed-item source resolution; and sibling isolation.
- AR19: The portable EP matrix covers ordinary Markdown and Transcript fixtures, existing owned values, empty/malformed/oversized input, invalid output, timeout, input immutability, runner atomicity, host-adapter effects, and version skip/rerun.
- AR20: A small multi-speaker fixture proves the healthy end-to-end path produces speaker labels, `diarized: true`, and completed EP state; the unavailable path preserves source and Transcript with actionable non-success.
- AR21: Wax remains the owner of the EP registry, queue, execution state, and lifecycle. HeyMa integrations may later consume normalized status but do not read Wax's database or execute private implementation code.
- AR22: Phase 0A Syncthing/dropoff migration, unified-tray work, product-source movement, bulk historical backfill, general scheduling, and migration of unrelated environment settings remain outside this epic.

### UX Design Requirements

None. This scope adds CLI and machine-readable contracts; no UX design artifact was selected.

### FR Coverage Map

FR1: Epic 1 - Load and validate persistent Wax settings from Canonical Config.

FR2: Epic 1 - Apply Diarize automatically by default and honor explicit disablement.

FR3: Epic 1 - Preserve and expose the truthful, retryable Diarize outcome.

FR4: Epic 1 - List registered EPs through the requested human and JSON CLI forms.

FR5: Epic 1 - Inspect applied, available, and in-flight EP state for one Wax Item.

FR6: Epic 1 - Durably enqueue one eligible unapplied EP with duplicate protection.

FR7: Epic 1 - Atomically claim, execute, and report queued EP work across restarts.

## Epic List

### Epic 1: Trustworthy, Operator-Controlled Enrichment

Wax users receive truthful default diarization and can discover, inspect, enqueue, and observe enrichment for any Wax Item through one reusable EP model.

**FRs covered:** FR1, FR2, FR3, FR4, FR5, FR6, FR7

## Epic 1: Trustworthy, Operator-Controlled Enrichment

Wax users receive truthful default diarization and can discover, inspect, enqueue, and observe enrichment for any Wax Item through one reusable EP model.

### Story 1.1: Restore Configurable Default Diarization

As a Wax operator,
I want diarization enabled by default through Canonical Config,
So that new recordings reliably become speaker-attributed dialog without hidden service flags or silent degradation.

**Requirements:** FR1, FR2, FR3

**Acceptance Criteria:**

**Given** no Wax config file or compatible environment override
**When** the CLI or daemon resolves transcription settings
**Then** it uses `$XDG_CONFIG_HOME/wax/config.toml`, falling back to `~/.config/wax/config.toml`
**And** `transcription.diarize` defaults to `true` without creating the file.

**Given** Canonical Config contains `[transcription] diarize = false`
**When** Wax transcribes a new item
**Then** it intentionally produces a valid non-diarized Transcript
**And** does not report the disabled operation as a dependency failure.

**Given** Canonical Config and a compatible environment override disagree
**When** Wax resolves the setting
**Then** precedence is operation argument, environment, config, built-in default
**And** the deployed systemd unit no longer hardcodes `WAX_DIARIZATION=1`.

**Given** malformed TOML or a non-boolean diarize value
**When** Wax loads settings
**Then** it reports the exact file and field error
**And** preserves capture capability, queued audio, and all existing artifacts.

**Given** default diarization and a healthy runtime
**When** a multi-speaker fixture is transcribed
**Then** the Transcript contains speaker-attributed dialog and `diarized: true`.

**Given** default diarization and a missing or broken Sortformer backend
**When** transcription runs
**Then** Wax never represents Diarize as successful
**And** preserves the source audio and base Transcript with an actionable non-success reason.

**Given** Wax deployment is installed or updated
**When** runtime readiness is checked
**Then** `whisperlivekit.diarization.sortformer_backend_offline` imports from a reproducible declared environment
**And** Wax cannot silently resolve its transcriber from a different checkout revision.

**Given** automated configuration and diarization tests
**When** they execute
**Then** they use isolated config, audio, vault, and runtime fixtures
**And** never touch the operator's live state.

### Story 1.2: Apply Diarize as a Safe Enrichment Pass

As a Wax operator,
I want Diarize represented by the same EP contract as other document enrichment,
So that I can apply or retry speaker extraction without an untracked special-case pipeline.

**Requirements:** FR2, FR3

**Acceptance Criteria:**

**Given** Wax loads its EP registry
**When** it discovers Diarize
**Then** the registry exposes slug `diarize`, version, display name `Diarize`, description `Transform transcript to dialog format with speaker extraction`, enabled/automatic state, applicability, and runtime availability.

**Given** the interchangeable EP framework
**When** it handles ordinary Markdown or a Transcript
**Then** its portable result and mutation boundary remain document-neutral
**And** Diarize explicitly declares that it requires a Transcript plus linked source audio rather than pretending every document is applicable.

**Given** an applicable Wax Item
**When** the runner prepares Diarize
**Then** it resolves the Transcript and source audio from content-addressed ledger identity
**And** never infers either artifact from its filename.

**Given** Diarize returns transformed dialog
**When** the runner applies the result
**Then** it uses a bounded, validated, backward-compatible document-body intent
**And** atomically replaces only the body after verifying the expected input identity while preserving protected and human-owned frontmatter.

**Given** an older non-diarized Wax Item with a preserved source
**When** the operator runs `wax ep run diarize <wax-item-id>`
**Then** Wax reuses durable timed ASR data when available or safely recomputes it when absent
**And** produces speaker-attributed dialog, `diarized: true`, and a completed Diarize pass record.

**Given** automatic diarization is enabled for a new recording
**When** transcription completes
**Then** Wax applies and records the same registered Diarize EP behavior
**And** does not maintain a second untracked success path or unnecessarily repeat ASR.

**Given** the linked source is missing or the backend is unavailable
**When** applicability is evaluated or execution starts
**Then** Diarize is unavailable or failed with an actionable reason
**And** remains eligible for retry without changing source audio or the existing Transcript.

**Given** invalid, malformed, oversized, timed-out, or nonzero pass output
**When** execution fails
**Then** no partial document mutation occurs
**And** the failure does not block sibling EPs or safe source-audio parking.

**Given** existing registered passes and `wax.ep.v1` results
**When** the new runner handles them
**Then** metadata-only output, `transcript.slug`, `link_audio`, protected-field rules, version idempotency, deterministic command IDs, and immutable S3 keys remain compatible.

**Given** the EP test matrix
**When** it runs under isolated roots
**Then** it proves generic-document compatibility, Transcript applicability, direct-pass input immutability, atomic body application, healthy and unavailable Diarize paths, version reruns, and sibling isolation.

### Story 1.3: Discover and Inspect Enrichment Passes

As a Wax operator,
I want to list registered EPs and inspect their state for a Wax Item,
So that I can understand available, applied, in-progress, and failed enrichment without reading SQLite or frontmatter manually.

**Requirements:** FR4, FR5

**Acceptance Criteria:**

**Given** the Wax EP registry
**When** the operator runs `wax ep -l` or `wax ep --list`
**Then** human output is deterministically numbered by display name
**And** includes `1. Diarize - Transform transcript to dialog format with speaker extraction`.

**Given** registered disabled or runtime-unavailable EPs
**When** EPs are listed
**Then** Wax includes rather than hides them
**And** distinguishes registration, enabled, automatic, and runtime-availability state with an actionable unavailable reason.

**Given** `wax ep -l --json` or equivalent global `--json` placement
**When** Wax emits registry data
**Then** each entry contains stable `slug`, `display_name`, `description`, `version`, `enabled`, `automatic`, `available`, and `unavailable_reason` fields
**And** human and JSON output derive from the same registry projection.

**Given** the existing CLI surface
**When** the new list aliases are introduced
**Then** `wax ep list`, `wax ep run`, `wax ep run-all`, and `wax ep status` remain compatible
**And** `--json` remains accepted before or after applicable subcommands.

**Given** a known Wax Item
**When** the operator runs `wax info -i <wax-item-id>` or `wax info --item-id <wax-item-id>`
**Then** output identifies the item, linked source audio, Transcript, and registered EP version state
**And** groups each EP exactly once as applied, available, in progress, disabled, or unavailable.

**Given** a failed retryable EP or a completed older EP version
**When** item information is projected
**Then** the failed EP is shown as failed and eligible for retry
**And** the older completed version is shown with the current version available.

**Given** an intentionally disabled Diarize setting
**When** the item is inspected
**Then** Wax identifies it as intentionally disabled
**And** does not misrepresent it as failed or unavailable due to an error.

**Given** an unknown Wax Item ID
**When** the operator requests information
**Then** Wax exits nonzero with a clear diagnostic
**And** emits no misleading partial item record.

**Given** a known item with missing Transcript or source audio
**When** information is requested
**Then** Wax returns the known item truthfully with the affected EP unavailable and a reason
**And** does not crash or mutate reconciliation state.

**Given** CLI contract tests under isolated roots
**When** short, long, legacy, human, JSON, unknown-item, unavailable, version-upgrade, and malformed-registry cases run
**Then** output and exit codes match the documented contract
**And** no command mutates the item, Transcript, registry, or ledger.

### Story 1.4: Queue and Execute Enrichment Pass Jobs

As a Wax operator,
I want to enqueue an unapplied EP for a Wax Item,
So that enrichment runs durably in the background and survives CLI or daemon restarts.

**Requirements:** FR6, FR7

**Acceptance Criteria:**

**Given** a known Wax Item and eligible EP
**When** the operator runs `wax job -p <ep-slug> -i <wax-item-id>` or the long-form equivalents
**Then** Wax validates the item, Transcript, registered version, enabled state, applicability, and runtime prerequisites
**And** returns only after the EP Job is durably queued without executing it synchronously.

**Given** successful enqueue in human or JSON mode
**When** Wax responds
**Then** it identifies the job, item, EP slug, requested version, and `queued` state
**And** `wax info` immediately projects that EP as in progress.

**Given** the same item, EP, and version is already queued or running
**When** the request is repeated concurrently
**Then** one active EP Job exists
**And** Wax returns that job as an idempotent deduplicated result.

**Given** the current EP version is already completed
**When** the operator requests it again
**Then** Wax returns a documented nonzero no-op diagnostic without creating a job
**And** a newer registered version remains eligible.

**Given** an unknown item, missing Transcript, disabled EP, inapplicable EP, unavailable runtime, or unknown slug
**When** enqueue is attempted
**Then** Wax exits nonzero with a precise reason
**And** writes no partial job or pass state.

**Given** queued EP work and an enabled Wax scheduler
**When** the worker selects work
**Then** it claims exactly one job through an atomic conditional transaction
**And** can process jobs for archived or completed Wax Items independently of inbox-audio scanning.

**Given** an EP Job is claimed
**When** execution begins and ends
**Then** durable job history records queued, running, and completed or failed state with attempt, timestamps, command ID, and bounded detail
**And** the existing `passes` row and Transcript `wax.passes` map remain current-state projections rather than being misrepresented as append-only history.

**Given** the daemon stops while a job is running
**When** Wax restarts
**Then** reconciliation proves an already-applied result complete or safely retries unresolved work with an incremented attempt
**And** never duplicates a committed document mutation.

**Given** one queued EP fails
**When** other eligible work exists
**Then** the failure remains visible and retryable
**And** does not fail the Wax Item, block sibling EP Jobs, interfere with audio archival, or wedge the worker.

**Given** the pipeline is intentionally disabled
**When** an eligible job is enqueued
**Then** the job remains durably queued and visible
**And** starts only after the scheduler is enabled.

**Given** concurrent enqueue, claim, restart, failure, retry, and version-upgrade tests under isolated roots
**When** the suite runs
**Then** it proves single execution, durable recovery, correct exit codes and JSON, current-state consistency, append-only attempt history, and artifact preservation.
