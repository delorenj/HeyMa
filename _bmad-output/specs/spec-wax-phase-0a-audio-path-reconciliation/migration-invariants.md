# Phase 0A Migration Invariants

This companion resolves the Phase 0A gaps that the adopted architecture and migration documents leave implicit. It supplements rather than replaces their lease, importer, and ten-step cutover contracts.

## 1. Phase Boundary and Authority Bootstrap

Phase 0A implements only the Wax-local authority needed to make the safety migration valid:

- a stable product `instance_id`;
- an `epoch` that changes if the generation sequence is reset;
- a monotonic `generation` for semantic status, action, and lease changes;
- expected-revision checks over all three fields; and
- a token-free maintenance projection in Wax status.

The raw lease token is returned only to its acquiring process. A full `wax.status.v1` document and cross-product provider contract remain Phase 0B. Phase 0A may not claim the later schema merely because it implements this required subset.

Lease state is durable without `waxd`, fsynced under Wax's product-wide action lock, and observable from a daemon-independent CLI. A five-minute lease renews no later than half its TTL. Acquisition rejects unless a fresh local observation proves every Wax inhibitor clear.

## 2. Protected Operation Set

| Operation while a lease is active | Rule |
| --- | --- |
| Capture start from CLI, tray, hotkey, remote command, or toggle-to-start | Reject without the matching lease token |
| Capture stop or quiesce | Allow; it reduces risk and cannot start a new artifact |
| Finalizer or recovery start | Reject without the matching token |
| Queue claim, worker start, or transcription start | Reject without the matching token |
| Import staging or publication into `inbox` | Reject without the matching token |
| Wax service restart, reconfiguration, or path cutover | Reject without the matching token |
| Status, inspection, logs, manifests, and read-only preflight | Allow |
| Lease acquire, renew, release, and lease-owner migration steps | Serialize under the same lock and validate token plus expected revision |

An already-active recording, finalizer, queue item, importer publication, or other Wax operation prevents lease acquisition. This is not a request to interrupt work. Automated acceptance must prove rejection through direct CLI and hotkey paths after `waxd` is stopped, because daemon-only checks do not satisfy CAP-1.

## 3. Lease-Loss and Revision-Change Recovery

Before every mutation, the orchestrator re-observes the active lease and expected `{instance_id, epoch, generation}`. It renews before the half-TTL boundary and before any step whose bounded execution plus safety margin could cross expiry.

If renewal fails, ownership cannot be re-proved, or the authoritative revision changes:

1. start no new mutation and never publish an in-progress staging file;
2. close the current staging output safely, leave source bytes untouched, and journal `aborted_lease_lost` or `aborted_revision_changed`;
3. preserve the last verified configuration and every partial migration artifact for diagnosis;
4. release only if the token remains valid; expiry otherwise removes the barrier; and
5. require a fresh idle preflight and new lease before continuation or rollback.

Rollback after lease loss is itself a protected mutation. It must never continue under an expired token or assume that the machine remained idle while the barrier was absent.

## 4. Import Identity and Existing Wax State

Full SHA-256 identity decides import behavior; filename and mtime never do.

| Observed source | Required result |
| --- | --- |
| New full SHA and unused destination name | Publish one durable inbox copy and create one pending Wax Item |
| New full SHA but destination name collides with different content | Publish under a deterministic no-clobber collision name and create a distinct item |
| Existing full SHA with a valid Wax-managed audio path | Record dropoff import provenance only; do not create a second inbox copy or pending transition |
| Existing full SHA whose prior audio path is absent | Restore at most one collision-safe Wax-managed copy without resetting item state or derived projections |
| Source device/inode/size/mtime changes during copy | Publish nothing; retain no success record and retry only after a new stability window |

For an existing identity, the importer preserves the item row, state, transitions, verified backups, Transcript link, and every enrichment-pass version/outcome. Reconciliation may add source-feed provenance but may not downgrade a completed item, rerun transcription, or repoint a still-valid path. Import records must make restart and repeated observation idempotent.

Wax never renames, deletes, chmods, writes a marker into, or records completion inside `dropoff`. Staging is inbox-local, fsynced before atomic no-replace publication, and not treated as an item until publication succeeds.

## 5. Cutover Journal and Rollback

The evidence bundle captures before mutation:

- resolved Syncthing folder IDs, paths, types, versioning, marker paths, device membership, config revision, and service state;
- Wax status, inhibitor set, minimal provider revision, service state, and active lease projection;
- resolved n8n workflow IDs and watched absolute paths;
- filesystem/device IDs for `inbox`, `dropoff`, `stream`, `.stversions`, and staging roots; and
- sorted SHA-256 manifests that distinguish content identity from path aliases.

Each mutable step records `planned`, `started`, `applied`, `verified`, and, if used, `rolled_back`; its entry includes expected revision, lease ID without token, exact prior value, exact intended value, reverse operation, and generated evidence. The journal is fsynced before the mutation it authorizes.

Rollback replays verified applied entries in reverse under a fresh or still-valid lease. It restores Syncthing mapping and service state, leaves all copied corpora and version history recoverable, and is safe to repeat. It never tries to restore data by deleting the newer copy. A manifest mismatch halts both forward progress and automatic rollback until a fresh byte inventory explains the difference.

The old `inbox`, its Syncthing marker, and version history remain recoverable throughout Phase 0A. Population of `dropoff` is copy-and-verify, never move. Existing `dropoff` content is merged by full SHA without overwrite.

## 6. Ordered Gates

1. **Implementation gate:** maintenance lease and continuous importer pass isolated tests, including daemon-absent entry points and crash/restart boundaries.
2. **Inventory gate:** live Wax is idle; all service/config/path/workflow identities and byte manifests are recorded.
3. **Lease gate:** a five-minute lease is acquired, renewed, re-observed, and proven to block direct CLI and hotkey starts while `waxd` is absent.
4. **Quiescence gate:** Wax worker and Syncthing are cleanly paused; authoritative revisions still match inventory.
5. **Corpus gate:** `dropoff` contains verified copies representing every receive-only source hash; collision and duplicate decisions are journaled.
6. **Configuration gate:** Syncthing folder `audio` resolves to `~/HeyMa/dropoff`, remains `receiveonly` with versioning preserved, and no folder resolves to Wax `inbox`.
7. **Resumption gate:** Syncthing resumes without reverting or deleting preserved corpus; importer restart remains idempotent.
8. **Route gate:** after Wax resumes and the lease is released, one cross-device drop and one local recording are each observed and processed exactly once.
9. **Instruction gate:** only after all earlier gates pass may `AGENTS.md` describe `dropoff` as the receive-only feed and `inbox` as Wax-owned local queue.

The live machine was recording during spec creation on 2026-08-15. That observation is a concrete inhibitor, not authorization to stop recording or begin this runbook.

## 7. Machine Exit Checks

The exit command or test suite must fail unless all of these are true:

- Syncthing folder `audio` resolves absolutely to `~/HeyMa/dropoff`, is `receiveonly`, and retains the intended staggered versioning;
- no Syncthing folder resolves to `~/HeyMa/inbox`;
- importer source-immutability monitoring reports zero local writes beneath `dropoff`;
- every preflight content hash remains represented in a preserved location and every path collision retains both distinct hashes;
- restarting importer and `waxd` creates no duplicate item, pending transition, Transcript, backup, or EP execution;
- direct CLI, tray/hotkey, worker, importer publication, and service mutation obey the active lease;
- the cross-device fixture is imported and processed once;
- the local fixture appears atomically in `inbox` and is processed once; and
- the evidence bundle contains the preflight, mutation journal, reverse operations, postflight, test output, sanitized status, and final manifests.

Workflow inspection keys on resolved absolute paths. A workflow that watches `/home/delorenj/audio` is outside this cutover even if its display name resembles the Wax workflow.
