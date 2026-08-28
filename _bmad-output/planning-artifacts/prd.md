---
title: Wax Enrichment Pass Control
status: final
created: 2026-08-14
updated: 2026-08-14
inputDocuments:
  - AGENTS.md
  - components/wax/docs/ENRICHMENT-PASSES.md
  - components/wax/docs/WAX-DESIGN.md
  - architecture/architecture-HeyMa-2026-08-12/ARCHITECTURE-SPINE.md
  - architecture/architecture-HeyMa-2026-08-12/MIGRATION-PLAN.md
  - user requirements supplied 2026-08-14
---

# PRD: Wax Enrichment Pass Control

## 0. Document Purpose

This PRD defines the brownfield Wax behavior needed to make transcript enrichment discoverable, configurable, observable, and retryable. It is the product input for BMAD epic and story creation. Existing audio-safety, archival, and enrichment-pass architecture remains authoritative and is not duplicated here.

## 1. Vision

Wax should turn a recording into a trustworthy transcript without requiring the user to remember hidden runtime switches. Diarization is the first dogfooded Enrichment Pass (EP): it is enabled by default from a durable user configuration, its outcome is attached to the Wax Item, and a missing runtime dependency cannot masquerade as successful enrichment.

The same control surface must work for future EPs. The user can discover what Wax knows how to apply, inspect what has or has not been applied to one Wax Item, and enqueue an unapplied EP without reaching into SQLite, editing frontmatter, or invoking an implementation command directly.

## 2. Target User

### 2.1 Jobs To Be Done

- Record audio and receive speaker-attributed transcripts by default.
- Understand whether an EP is available, applied, queued, running, or failed for a specific Wax Item.
- Retry or newly apply an EP through a stable CLI command.
- Persist Wax settings in one predictable user-owned location.

### 2.2 Key User Journeys

- **UJ-1. Jarad records a conversation and receives dialog.** Jarad stops a Wax recording. Wax archives and transcribes it, automatically applies the configured Diarize EP, and publishes a transcript with speaker-attributed dialog. If diarization cannot run, Wax records a visible failed or unavailable EP outcome instead of silently representing the work as complete.
- **UJ-2. Jarad repairs an older transcript.** Jarad runs `wax info --item-id <id>`, sees that Diarize is available but unapplied, enqueues it with `wax job --enrichment-pass diarize --item-id <id>`, and can observe the job reach a terminal state.

## 3. Glossary

- **Wax Item** — A content-addressed audio artifact identified by `wax-item-id`; it may have one Transcript and many EP outcomes.
- **Transcript** — The Markdown document linked to a Wax Item.
- **Enrichment Pass (EP)** — An independently versioned, registered transformation that applies to a supported document or linked source artifact without gating sibling EPs.
- **Diarize** — The EP that transforms a Transcript into speaker-attributed dialog using its linked source audio.
- **Applied EP** — An EP completed successfully for the current registered version on a Wax Item.
- **Available EP** — A registered EP whose applicability and runtime prerequisites permit it to be enqueued for a Wax Item.
- **EP Job** — A durable request for Wax to apply one EP to one Wax Item asynchronously.
- **Canonical Config** — The persistent Wax settings file at `$XDG_CONFIG_HOME/wax/config.toml`, defaulting to `~/.config/wax/config.toml` when `XDG_CONFIG_HOME` is unset.

## 4. Features

### 4.1 Canonical Settings and Default Diarization

**Description:** Wax reads persistent user settings from Canonical Config. Diarize is enabled by default and becomes the first setting exercised through that file. This realizes UJ-1.

#### FR-1: Canonical Wax configuration

Wax loads supported persistent settings from `$XDG_CONFIG_HOME/wax/config.toml`, with `~/.config/wax/config.toml` as the default path.

**Consequences (testable):**

- With no config file, Wax uses documented built-in defaults and does not create or modify the file implicitly.
- A config file under an isolated `XDG_CONFIG_HOME` changes Wax behavior in CLI and daemon processes consistently.
- Invalid values fail with a field-specific diagnostic; they are not silently coerced.
- Existing temporary environment overrides remain compatible for this release, with documented precedence over the persistent value.

#### FR-2: Diarize by default

Wax treats Diarize as enabled for automatic application unless Canonical Config explicitly disables it.

**Consequences (testable):**

- The supported setting is `transcription.diarize = true|false`; its built-in default is `true`.
- A newly transcribed Wax Item reaches `diarized: true` and contains speaker-attributed dialog when the setting is true and prerequisites are healthy.
- Setting `transcription.diarize = false` produces a valid non-diarized Transcript and records Diarize as intentionally not requested, not failed.

#### FR-3: Truthful diarization outcome

Wax exposes the real Diarize outcome for every newly transcribed Wax Item when automatic diarization is enabled.

**Consequences (testable):**

- Missing or broken diarization dependencies cannot be reduced to a warning followed by an apparently successful Diarize outcome.
- The base Transcript and irreplaceable source audio remain preserved when Diarize fails.
- A failed or unavailable Diarize outcome includes an actionable reason and remains eligible for retry after prerequisites are repaired.

### 4.2 EP Discovery and Item Inspection

**Description:** Wax exposes a stable human and machine-readable view of the EP registry and its projection onto one Wax Item. This realizes UJ-2.

#### FR-4: List registered EPs

The user can run `wax ep -l` or `wax ep --list` to list registered EPs.

**Consequences (testable):**

- Human output is numbered and includes display name plus description, including `1. Diarize - Transform transcript to dialog format with speaker extraction` when it is the first sorted entry.
- Each entry distinguishes enabled, automatic, and currently available state without hiding disabled or unavailable registered EPs.
- `--json` returns stable fields suitable for scripts, including slug, display name, description, version, enabled, automatic, availability, and an unavailable reason when applicable.
- Existing `wax ep list`, `wax ep run`, `wax ep run-all`, and `wax ep status` behavior remains compatible.

#### FR-5: Inspect one Wax Item

The user can run `wax info -i <wax-item-id>` or `wax info --item-id <wax-item-id>` to inspect the Wax Item and its EPs.

**Consequences (testable):**

- Wax rejects an unknown Wax Item with a non-zero exit and clear diagnostic.
- Output identifies the source item and Transcript, then separates applied EPs from available-but-unapplied EPs.
- Queued, running, failed, disabled, unavailable, and completed version states are distinguishable.
- The JSON form is derived from the same registry and ledger data as human output.

### 4.3 Durable EP Job Queue

**Description:** Wax accepts an EP request quickly, persists it, and lets the existing worker execute it under the same independent-pass contract as automatic enrichment. This realizes UJ-2.

#### FR-6: Enqueue an unapplied EP

The user can run `wax job -p <ep-slug> -i <wax-item-id>` or the long-form equivalents `--enrichment-pass` and `--item-id` to enqueue an EP Job.

**Consequences (testable):**

- The command validates the Wax Item, its Transcript, the EP slug, applicability, enabled state, prerequisites, and current applied version before persisting work.
- The command returns after durable enqueue and does not execute the EP synchronously.
- Repeating the same request while queued or running is idempotent and does not create duplicate execution.
- Requesting an already-applied current EP version returns a non-zero no-op diagnostic; a newer registered version remains eligible.

#### FR-7: Execute and report queued EP work

The Wax worker claims queued EP Jobs, executes them one at a time under the existing pass runner, and records terminal results.

**Consequences (testable):**

- State transitions are durable and observable through `wax info` after daemon or CLI restart.
- A failed EP Job records its reason and attempt without blocking unrelated EP Jobs or changing a completed Wax Item into a transcription failure.
- Successful execution updates the existing `passes` ledger row and Transcript `wax.passes` history consistently.
- Existing automatic EP idempotency by `(wax-item-id, EP slug, version)` is preserved.

## 5. Cross-Cutting Non-Functional Requirements

- **NFR-1 — Audio safety:** No EP discovery, inspection, enqueue, or execution path deletes or overwrites source audio, a Transcript, or S3 identity metadata.
- **NFR-2 — Atomicity:** Queue claims and state transitions are transactionally safe across the daemon and concurrent CLI processes.
- **NFR-3 — Independence:** Failure of one EP never gates a sibling EP, verified audio parking, or later retry.
- **NFR-4 — Compatibility:** Existing Wax CLI commands, environment overrides, content-addressed IDs, ledger rebuild behavior, and EP result contract remain operable.
- **NFR-5 — Test isolation:** Config, ledger, registry, vault, and audio-root tests use temporary roots and do not read or mutate the operator's live files.
- **NFR-6 — Runtime readiness:** The installed Wax transcription environment either contains the supported diarization runtime or reports Diarize unavailable before work is represented as applied.

## 6. Non-Goals

- Building a general-purpose scheduler unrelated to EP work.
- Migrating every existing Wax environment variable into TOML in this slice.
- Automatically backfilling every historical non-diarized Transcript.
- Adding tray UI for EP management.
- Changing S3 object keys, source-audio identity, title-slug behavior, or archive-link policy.
- Implementing Phase 0A Syncthing/dropoff migration or HeyMa unified-tray work in this story.

## 7. MVP Scope

### 7.1 In Scope

- Canonical Config loader and `transcription.diarize` with a default of `true`.
- A registered Diarize EP with truthful availability and outcome reporting.
- EP list aliases, item inspection, durable enqueue, and background execution.
- Compatibility and regression tests for existing EP and transcription behavior.

### 7.2 Out of Scope for MVP

- Automatic bulk repair of historical Wax Items.
- Arbitrary job dependencies, priorities, schedules, or cross-product jobs.
- Configuration editing commands or a settings UI.

## 8. Success Metrics

- **SM-1:** In an isolated end-to-end run with supported dependencies, 100% of new recordings using default config publish a speaker-attributed Transcript and a completed Diarize EP outcome. Validates FR-2 and FR-3.
- **SM-2:** With diarization prerequisites removed, 100% of test runs expose Diarize as unavailable or failed with an actionable reason and zero false completed outcomes. Validates FR-3.
- **SM-3:** CLI contract tests cover both short and long requested forms plus existing EP subcommands, with no regression failures. Validates FR-4 through FR-7.
- **SM-C1:** Do not reduce visible failures by silently disabling Diarize or marking non-diarized output complete; correctness outranks green status.

## 9. Open Questions

None blocking. Bulk backfill and migration of additional environment settings are explicitly deferred.

## 10. Assumptions Index

- This is a solo brownfield developer utility; operational correctness and CLI contracts are the appropriate product measures.
- Existing environment values may temporarily override Canonical Config for backward-compatible deployment control.

## 11. Inline Story Seed

### Story-1: Configure, discover, and queue Diarize as a Wax Enrichment Pass

As the Wax operator, I can rely on default diarization, inspect EP state for any Wax Item, and enqueue an unapplied Diarize EP so that transcript enrichment is explicit, repairable, and reusable through one stable CLI model.

Acceptance is defined by FR-1 through FR-7 and NFR-1 through NFR-6.
