# HeyMa Federated Control-Plane Migration

This plan is for the repository owner and implementation agents to execute the
federated architecture without interrupting irreplaceable audio work. Complete
the phases in order; each exit gate is the prerequisite for the next phase.

## Decision

Keep Vinyl and Voxxy as standalone products. HeyMa becomes their discovery,
status, safe-control, and desktop-integration plane. Wax stays embedded for the
first migration and adopts the same adapter boundary before any extraction.

| Candidate | Fit | Decision |
| --- | --- | --- |
| Full source monorepo | Atomic commits, but combines unrelated runtimes, deployments, consumers, and release cadence. | Reject |
| Wax + Vinyl source monorepo | Similar desktop use, but current shared code is presentation behavior. | Reject for now |
| Polyrepo plus one unified tray | Matches product and deployment boundaries while removing duplicate UX. | Adopt |
| Git submodules in HeyMa | Adds pointer synchronization without solving contracts or deployment. | Reject |

Revisit a source monorepo only after three product features require atomic
implementation changes across repositories. Tray, adapter, contract-bundle,
and deployment-profile changes do not count.

## Non-negotiable safety gates

- Never restart, reconfigure, or cut over a product unless its authoritative
  endpoint grants a product-owned maintenance barrier after proving all
  action-specific inhibitors are clear.
- Wax disruptive work requires an idle stream, no finalizer, no active queue item,
  and no concurrent Wax operation. Vinyl requires all local/client/serve
  microphone roles inactive. Voxxy requires no protected synthesis.
- Unknown, stale, malformed, unsupported, or unreachable status fails closed.
- No step deletes or relocates recordings, transcripts, product state,
  credentials, models, or voice assets.
- Existing product CLIs and services remain rollback surfaces.
- Killing `heyma-tray` must not affect any product.
- The shadow tray remains read-only until the cutover transaction.

Each phase writes a timestamped evidence bundle beneath
`_bmad-output/implementation-artifacts/heyma-control/`. The bundle contains the
tested contract versions, deterministic test output, sanitized status captured
before and after the phase, deployment manifest, and step journal. A prose
assertion is not an exit gate.

## Phase 0A — Reconcile the live audio-path conflict

This is a separate Wax safety migration, not tray work. Live inspection on
2026-08-12 found Syncthing folder `audio` still mapped to
`~/HeyMa/inbox` as `receiveonly`, while Wax writes completed local recordings
there. Do not deploy around this contradiction.

The target ownership model is:

```text
Syncthing receiveonly -> ~/HeyMa/dropoff
Wax reads/copies only <- ~/HeyMa/dropoff
Wax owns stream       -> ~/HeyMa/stream
Wax owns local queue  -> ~/HeyMa/inbox
```

1. Implement the AD-19 Wax maintenance lease as daemon-independent durable
   state under the Wax action lock. Make every start path—including CLI, tray,
   hotkey, and remote command—reject while another owner holds it.
2. Implement the AD-18 continuous `dropoff` importer and prove stability checks,
   full-content deduplication, collision handling, restart reconciliation,
   atomic publication, and zero source mutation under an isolated `WAX_ROOT`.
3. Record Syncthing folder configuration, service state, Wax status and
   generation, filesystem/device IDs, and SHA-256 manifests for `inbox`,
   `dropoff`, `.stversions`, `stream`, and every migration staging path.
4. Acquire and begin renewing a five-minute Wax lease. Stop `waxd`, then prove
   both direct CLI and hotkey starts reject while the daemon is absent. Abort
   before the renewal margin if a renewal fails.
5. Under the lease, pause Syncthing and stop any remaining Wax worker cleanly.
   Abort if either authoritative generation changes before mutation.
6. Populate `dropoff` from the receive-only corpus by copy, never move; resolve
   name collisions by content identity; fsync; then prove byte conservation
   against the preflight manifest.
7. Repoint Syncthing folder `audio` to `dropoff` as `receiveonly`. Keep the old
   `inbox`, marker, and version history recoverable; do not remove them during
   this phase.
8. Resume Syncthing and prove it neither reverts nor deletes the preserved
   corpus. Confirm no Syncthing folder maps to Wax's local `inbox`.
9. Start Wax with its native tray still enabled. Release the lease and test one
   cross-device drop and one short local recording. The former must be imported
   exactly once from `dropoff`; the latter must appear atomically in local
   `inbox`.
10. Update `AGENTS.md` only after the live configuration and tests agree. Treat
   n8n workflows by their resolved absolute paths; do not disable an unrelated
   workflow watching `/home/delorenj/audio`.

Exit gate: a machine check proves the ownership model above, all preflight
hashes remain represented, no receive-only directory receives a local write,
the importer survives restart, a durable lease blocks all start paths without
`waxd`, and both ingest routes preserve and process a test recording exactly
once.

## Phase 0B — Freeze provider contracts

Do not move product source or runtime directories. Stable executable installs
must leave running product services untouched during contract work.

1. In each product repository, add JSON Schema 2020-12 and conformance fixtures
   for `wax.status.v1`, `vinyl.status.v1`, or `voxxy.health.v1`.
2. Put the exact root header—`schema`, `instance_id`, `epoch`, `generation`, and
   `updated_at`—on the authoritative status command or endpoint.
3. Designate exactly one authoritative observation surface per product and
   separately test discovery, reachability, freshness, and health semantics.
4. Make Vinyl publish one aggregate over named `local`, `client`, and `serve`
   roles. Its product-level microphone state is the logical OR of every role.
5. Make Voxxy report service availability separately from per-engine health;
   an available fallback engine does not erase degradation of a required engine.
6. Install stable product executables on `PATH`; package Vinyl rather than
   teaching HeyMa its checkout or virtual-environment path.
7. Publish immutable tagged schema/fixture bundles. Provider CI validates live
   output and backward compatibility against the previous tagged bundle.

Exit gate: from a clean shell, every product emits a valid, fresh, headless
status document without implementation imports; Vinyl covers all daemon roles;
and each tagged contract bundle has a recorded digest.

## Phase 1 — Build the control core and diagnostic CLI

Create `apps/tray/` and the HeyMa-owned contracts before GTK behavior.

1. Promote the normative schemas from this architecture workspace without
   semantic changes; split files if desired, then add the provider lockfile and
   compatibility matrix.
2. Implement the adapter port algebra and Wax, Vinyl, and Voxxy adapters with
   bounded, non-overlapping reads and monotonic poll generations.
3. Implement discovery precedence and trust checks. An explicit stanza disables
   fallback; remote endpoints require explicit opt-in and verified HTTPS.
4. Implement the 2-second poll, local/HTTP deadlines, 10-second stale default,
   suspend/resume invalidation, durable microphone latch, and total color table.
5. Implement `heyma status --json`, `heyma components`, read-only action
   discovery, and `heyma doctor` with resolved targets and contract versions.
6. Add table-driven tests for all enum/cross-field combinations, late responses,
   cold restart while active, persistent latches, optional and required absence,
   malformed/oversized payloads, and progress terminal rules.

Exit gate: the JSON Schema suite and full aggregation matrix pass under a fake
clock; randomized product absence, timeout, response order, and malformed input
cannot crash the process or produce a state outside the total color table.

## Phase 2 — Run a read-only shadow tray

Keep both native tray surfaces enabled.

1. Install `heyma-tray` with a distinct shadow indicator ID and a visible
   read-only label.
2. Execute `/usr/bin/python3` explicitly, poll off the GTK thread, coalesce
   overlapping rounds, and verify StatusNotifier watcher re-registration.
3. Compare Wax and Vinyl activity, queue, error, and progress displays against
   authoritative CLI output through idle, active, finalizing, failed, and stale
   transitions.
4. Exercise a stopped service, unreachable Voxxy, partial TTS degradation,
   unsupported schema, malformed JSON, suspend/resume, and tray restart during
   live capture.
5. Prove `kill -9` of `heyma-tray` has no effect on a product process and that a
   persisted RED latch survives the tray restart.

Exit gate: a timestamped transition trace shows no incorrect microphone state,
no false terminal progress, no unbounded polling work, and recovery after every
injected adapter and watcher failure.

## Phase 3 — Add product-atomic daily controls

The shadow tray remains read-only. Exercise new mutations through one explicit
test client until cutover.

1. Add product desired-state endpoints (`start`, `stop`, and explicit `cancel`),
   request-ID deduplication, expected-revision checks, cross-client locking, and
   the AD-19 maintenance protocol. Each product exposes request-ID reconciliation
   so aggregate operations can recover after a tray restart.
2. Add action-specific inhibitor reporting. At minimum, Wax covers capture,
   finalization, queue work, and concurrent operations; Vinyl covers all roles;
   Voxxy covers protected synthesis.
3. Implement the closed action registry and argument validation. Engine IDs come
   only from a fresh engine list; open-output targets stay within product-owned
   trusted roots and allowed URI schemes.
4. Implement action results and timeout reconciliation. A
   `timed_out_unknown` mutation is observed, never retried automatically.
5. Add Wax no-tray operation without changing capture, queue, socket, state, or
   audio-path ownership. Confirm Vinyl remains fully operable with its tray
   stopped.
6. Collapse Wax's launcher and transcriber onto one canonical checkout or
   versioned installation under its maintenance barrier. The deployed
   two-checkout arrangement is not an acceptable release boundary.
7. Implement logs as a bounded tail or external viewer launch, never as a
   follow process occupying the action slot.
8. Keep reset, purge, delete, credentials, and destructive storage operations
   absent.

Exit gate: concurrent native/CLI/HeyMa calls prove product-side serialization
and idempotency; stale and changing revisions reject disruptive actions; every
allowed action reaches its observed target state; every excluded action is
absent from the descriptors.

## Phase 4 — Cut over the desktop indicator transactionally

Prepare an exact deployment manifest containing unit file content, drop-ins,
enablement, masking, static-unit and active states, environment, tray configuration,
indicator IDs, installed file hashes, and product revisions. Install files
atomically and journal every mutation plus its reverse operation.

1. Acquire product maintenance barriers. Each grant proves protected workloads
   idle and blocks new starts without stopping existing work. If any grant fails,
   release all grants and abort.
2. Re-observe every provider under its barrier and verify the expected instance,
   epoch, and generation immediately before each mutation.
3. Install and start `heyma-tray.service`; verify StatusNotifier registration,
   all component rows, provider contract versions, and GREEN idle state.
4. Enable Wax no-tray mode through its product-owned command. Disable only
   `vinyl-tray.service`; leave Vinyl engine/server services untouched.
5. Enable aggregate actions, release maintenance barriers, then exercise one
   short Wax recording, one Vinyl dictation, and one Voxxy health and
   engine-selection cycle.
6. Stop the aggregate tray and prove every native CLI still controls its product.
   Restart the aggregate tray and verify state/latch recovery.

Rollback replays the journal in reverse: disable aggregate actions, reacquire
maintenance barriers, restore exact native-tray configuration and unit states,
stop the aggregate tray, assert product health, then release barriers. The
rollback is idempotent and never restores product data because no product data
was changed.

Exit gate: both cutover and rollback pass from a clean preflight manifest under
injected failure after every mutation step, without duplicate indicators, lost
controls, product restart during work, or deployment-state drift.

## Phase 5 — Production soak

Operate the unified tray for at least 30 days and through:

- login/logout and suspend/resume;
- StatusNotifier watcher disappearance and return;
- tray crash and restart during Wax and Vinyl microphone activity;
- Wax archive, transcription, and enrichment failure;
- Vinyl local and remote-client activity;
- Voxxy partial engine degradation and total unreachability;
- action timeout with late product completion; and
- independent product updates through every supported contract version.

Track control-plane defects and contract drift. Do not move product source.

Exit gate: the retained traces show no safety-critical false state, no product
outage caused by the tray, no unbounded work, no contract drift escaping CI,
and no product release requiring a coordinated source release.

## Phase 6 — Optional Wax extraction

After the soak, decide independently whether Wax benefits from its own product
repository. If yes:

1. Split `components/wax/` with history preserved.
2. Install its stable CLI and service from the new repository.
3. Keep `wax.status.v1`, action IDs, runtime paths, S3 policy, ingest ownership,
   and adapter behavior unchanged through the move.
4. Update only HeyMa's compatibility matrix and installation/discovery profile.

If extraction creates no independent release or deployment benefit, leave Wax
embedded. Repository symmetry is not an architectural requirement.
