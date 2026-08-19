---
title: 'Make Wax and cross-device audio ingestion safe and single-owner'
type: 'bugfix'
created: '2026-08-17'
status: 'draft'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/specs/spec-wax-phase-0a-audio-path-reconciliation/SPEC.md'
  - '{project-root}/_bmad-output/specs/spec-wax-phase-0a-audio-path-reconciliation/migration-invariants.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Wax is operational again after an ENOSPC-induced partial recording, but Syncthing and Wax still share `/home/delorenj/HeyMa/inbox`, while live n8n paths and versioning differ from `AGENTS.md`. The repository has no maintenance lease or continuous immutable-dropoff importer, so a live path change could lose, duplicate, or reprocess irreplaceable audio.

**Approach:** Implement the Phase 0A control lease, centrally enforced operation guard, idempotent dropoff importer, and evidence-backed reversible cutover. Prove both cross-device and local recording routes exactly once before declaring the migration complete.

## Boundaries & Constraints

**Always:** Preserve source audio byte-for-byte; archive before transcription; use full SHA-256 identity; copy, fsync, verify, and no-clobber publish; journal intended mutations before applying them; fence state changes with instance, epoch, and expected revision; keep lease tokens out of status output; test against isolated roots and injected adapters; preserve dropoff and all version history.

**Ask First:** Any live Syncthing configuration mutation, service stop/restart, n8n mutation, cutover execution, rollback execution, source deletion, or S3 mutation requires explicit human approval after the preflight evidence is reviewed.

**Never:** Write locally into a receive-only Syncthing folder; activate archived workflow `Yw0WvYW1yAU1QG49` or alter active unrelated workflow `r2TUca8smk5HDNZx`; use the broad legacy `wax migrate` as the cutover executor; test with irreplaceable recordings; mutate dropoff sources or `.stversions`; infer safety from one idle snapshot.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| Lease lifecycle | Valid expected revision, with or without `waxd` | Acquire, renew, and release are durable and idempotent; status exposes sanitized lease state | Reject stale revision, wrong token, expired owner, or concurrent acquire without mutation |
| Protected start | Active lease; CLI, hotkey, worker, importer, recovery, direct Python, or service path | Every protected operation fails closed through one shared guard | Stable reason code; no process, claim, or file publication starts |
| Stable dropoff file | Unchanged device/inode/size/mtime across stability window | Copy to unique staging, fsync, full-hash verify, publish once, record provenance; source unchanged | Preserve staging evidence and retry safely after interruption |
| Unstable or duplicate file | Source changes, symlink/escape, collision, or known full digest | Defer unsafe source; deduplicate known content without regressing ledger projections | No source mutation, overwrite, duplicate item, or false completion |
| Cutover preflight | Pull errors, needed bytes, receive-only changes, inadequate reserve, stale revision, or missing peer | Emit evidence and refuse all configuration mutation | Record failed gate and recovery guidance |
| Approved cutover | Healthy peers, idle window, active lease, complete evidence | Journal first, stop writers, copy/verify preserved data, change ownership, and prove both routes exactly once | Roll back repeatably on any failed postcondition |

</frozen-after-approval>

## Code Map

- `components/wax/src/wax/paths.py:12` -- isolated `WAX_ROOT` and canonical stream/inbox/dropoff/state paths.
- `components/wax/bin/wax:43` -- existing capture-only filesystem lock; extend CLI with lease actions while keeping the guard in library code.
- `components/wax/src/wax/sentinel.py:43` and `procutil.py:57` -- atomic JSON and fsync primitives; harden permissions and temporary naming before reuse.
- `components/wax/src/wax/capture.py:94`, `worker.py:233`, `transcribe_adapter.py:152`, and `components/wax/bin/waxd:68` -- protected entry points that must share one lease guard.
- `components/wax/src/wax/migrate.py:81` and `rename.py:30` -- reusable filtering, hashing, and `RENAME_NOREPLACE`; the one-shot mutation flow itself is excluded.
- `components/wax/src/wax/ledger.py:200` -- content upsert foundation; add full-digest provenance and transactional revision behavior without regressing completed items.
- `components/wax/tests/wax_integration_test.py:39` -- subprocess-isolated `WAX_ROOT` test pattern; all production adapters must be replaced in tests.
- `_bmad-output/planning-artifacts/architecture/architecture-HeyMa-2026-08-12/MIGRATION-PLAN.md` -- cutover order, evidence, rollback, and ownership contract.

## Tasks & Acceptance

**Execution:**
- [ ] `components/wax/src/wax/control.py` and `ledger.py` -- implement durable provider identity, revision-fenced lease lifecycle, sanitized status, and shared fail-closed guard.
- [ ] `components/wax/src/wax/capture.py`, `worker.py`, `transcribe_adapter.py`, `components/wax/bin/wax`, and `components/wax/bin/waxd` -- enforce the guard at every protected library and process boundary.
- [ ] `components/wax/src/wax/importer.py` -- continuously reconcile stable regular files from dropoff through unique staging, full-hash verification, no-clobber publication, and durable provenance.
- [ ] `components/wax/src/wax/phase0a.py` -- add read-only preflight/evidence plus separately approved journaled cutover and rollback using injectable Syncthing, n8n, service, filesystem, and capacity adapters.
- [ ] `components/wax/tests/` -- cover lease races/expiry/revision, daemon absence, all guarded starts, importer crash boundaries, source mutation, duplicates/collisions, preflight refusal, rollback, and exactly-once route proofs.
- [ ] `AGENTS.md` and Wax runbooks -- update operational truth only after live postconditions pass; retain the warning until then.

**Acceptance Criteria:**
- Given `waxd` is stopped and a valid maintenance lease exists, when any protected path starts, then it is rejected before side effects with the same stable reason code.
- Given a stable cross-device file, when importer reconciliation repeats across restarts, then source bytes remain unchanged and exactly one ledger item, archive object, transcript, and completion record exist.
- Given any preflight gate is unhealthy, when Phase 0A is requested, then no live configuration changes and a complete evidence bundle identifies every failed gate.
- Given an approved healthy cutover, when either route is exercised and services restart, then ownership is unambiguous, each fixture completes exactly once, unrelated n8n receives zero executions, and rollback remains possible.

## Spec Change Log

## Design Notes

The implementation boundary is three small units: control/lease, importer, and Phase 0A orchestration. Existing `migrate.py` remains a legacy evidence source, preventing its move-oriented behavior from leaking into the live cutover.

## Verification

**Commands:**
- `PYTHONPATH=components/wax/src python3 -m unittest discover -s components/wax/tests -p '*_test.py'` -- expected: all existing and Phase 0A tests pass without touching production paths.
- `git diff --check` -- expected: no whitespace errors.
- `components/wax/bin/wax --json status` -- expected after approved cutover: sanitized lease projection, no path-ownership conflict, ready stream, and reconciled inbox.
