---
id: SPEC-wax-phase-0a-audio-path-reconciliation
companions:
  - migration-invariants.md
  - ../../../AGENTS.md
  - ../../planning-artifacts/architecture/architecture-HeyMa-2026-08-12/ARCHITECTURE-SPINE.md
  - ../../planning-artifacts/architecture/architecture-HeyMa-2026-08-12/MIGRATION-PLAN.md
  - ../../planning-artifacts/architecture/architecture-HeyMa-2026-08-12/contracts/CONTROL-CONTRACT.md
sources: []
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability only — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# Wax Phase 0A Audio-Path Reconciliation

## Why

Wax and Syncthing currently claim incompatible ownership of `~/HeyMa/inbox`: Syncthing maps it as `receiveonly`, while Wax publishes completed local recordings into it. That contradiction has already destroyed a recording and remains live as of 2026-08-15. Phase 0A must establish `dropoff` as the immutable cross-device feed and `stream`/`inbox` as Wax-owned paths through an idle-only, byte-conserving, reversible migration before tray or repository work proceeds.

## Capabilities

- **CAP-1 — Durable Wax maintenance lease**
  - **intent:** An operator can hold a daemon-independent Wax maintenance lease that prevents protected work from starting during migration.
  - **success:** Idle-only acquisition, renewal, release, and expiry are durable, idempotent, token-safe, revision-checked, and observable; CLI, tray, hotkey, worker, importer, service, and remote start paths reject unauthorized protected work even while `waxd` is stopped.

- **CAP-2 — Immutable dropoff import**
  - **intent:** Wax can continuously ingest stable cross-device audio from `dropoff` without treating the receive-only feed as a writable queue.
  - **success:** Boot and runtime reconciliation ignore Syncthing control files, verify source stability, deduplicate by full SHA-256, publish through durable atomic no-replace handoff, survive restart, preserve existing Wax projections, and perform zero mutations in `dropoff`.

- **CAP-3 — Evidence-driven cutover and rollback**
  - **intent:** An operator can execute or reverse the ownership cutover from a durable record of authoritative state and byte identity.
  - **success:** Every mutation is preceded by matching lease/revision proof and a journaled reverse operation; before/after service, Syncthing, filesystem, route, and SHA-256 evidence accounts for every preflight hash, and rollback is idempotent without deleting preserved data.

- **CAP-4 — Machine-verifiable ownership**
  - **intent:** The system can prove which product may write each live audio path before project instructions authorize the new model.
  - **success:** A machine check proves Syncthing `receiveonly` resolves to `~/HeyMa/dropoff`, no Syncthing folder resolves to Wax's local `inbox`, Wax alone publishes to `stream` and `inbox`, and no receive-only directory receives a local write.

- **CAP-5 — Exactly-once route proof**
  - **intent:** The operator can prove both cross-device and local recording routes remain safe and functional after cutover.
  - **success:** One cross-device test recording is imported and processed exactly once, one short local Wax recording appears atomically in `inbox` and is processed exactly once, both source artifacts remain recoverable, and no unrelated watcher or workflow processes either route.

## Constraints

- The current `AGENTS.md` prohibition on local writes to `~/HeyMa/inbox` remains binding until CAP-4 and CAP-5 pass against live configuration; only then may that document change.
- No step may delete, move, overwrite, truncate, chmod, or lose a recording, Transcript, S3 identity, Wax ledger projection, Syncthing version history, credential, model, or voice asset.
- Cutover and rollback fail closed unless Wax is freshly proven idle: no recording, finalizer, active queue item, importer publication, concurrent Wax operation, stale authority, or changing revision.
- `dropoff` is immutable to Wax. Import completion, ledger markers, staging files, and collision handling live outside the Syncthing-owned tree.
- Every mutation requires an active re-observed lease, matching expected revision, durable journal intent, and a verified reverse operation.
- Importer readiness passes isolated restart, stability, collision, deduplication, source-immutability, and crash-boundary tests before Syncthing is repointed.
- Existing full-SHA Wax identity wins over path or filename: duplicate import must preserve item state, Transcript, backup, transition, and EP projections rather than create new pending work.
- Path and workflow decisions use resolved absolute paths plus current identifiers; Phase 0A never edits or disables an unrelated n8n workflow watching `/home/delorenj/audio`.
- Automated tests use isolated Wax and Syncthing roots. Live cutover is a separately authorized runbook execution and must never begin during an active recording.
- A timestamped evidence bundle under `_bmad-output/implementation-artifacts/heyma-control/` is part of the exit gate; prose assertions are insufficient.

## Non-goals

- Building the unified tray or changing tray authority.
- Completing Phase 0B provider schemas beyond the minimal Wax lease identity/revision projection required by CAP-1.
- Moving Wax, Vinyl, or Voxxy source, runtime data, or product ownership boundaries.
- Implementing diarization, enrichment-pass discovery, item inspection, or EP job queuing.
- Broad n8n cleanup or migration of workflows whose resolved paths are outside the Wax root.
- Deleting old inbox markers, `.stversions`, recovery copies, or other rollback surfaces during Phase 0A.

## Success signal

- With an evidence bundle and rollback path already verified, the live machine reports Syncthing `audio` as `receiveonly` at `~/HeyMa/dropoff`, reports no Syncthing mapping to `~/HeyMa/inbox`, survives importer and service restart, conserves every preflight hash, and processes one cross-device drop plus one local recording exactly once without any write to a receive-only path.
