# HeyMa Control Contract v1

This document and the adjacent JSON Schemas are normative architecture
companions. Phase 0 product schemas compose
`provider-status-header-v1.schema.json`; Phase 1 promotes the aggregate and
action schemas into HeyMa's source tree without semantic changes.

## Revision semantics

`instance_id` identifies an installed product instance. `epoch` changes whenever
the provider resets its generation sequence. `generation` increments for every
semantic status, action, or maintenance-lease change and never for a heartbeat-
only write. `updated_at` refreshes at least every five seconds even when
generation is unchanged.

An expected revision matches only when all three fields match. A mismatch rejects
the mutation as `revision_changed`; it is never silently rebased. A terminal
workload remains visible for at least five seconds and one subsequent heartbeat;
its later removal increments generation.

## Provider failure projection

Discovery intent is determined before observation: an explicit component stanza
is required unless it says `required=false`; a built-in component is optional
when unconfigured.

| Condition | Projection without a previous valid row | Projection with a previous valid row |
| --- | --- | --- |
| No discovered target | `absent/unknown/unknown/unknown/unknown`; no provider revision | Same; retirement requires explicit instance acknowledgement if a microphone latch exists |
| Invalid explicit config | `misconfigured/unknown/unknown/unknown/unknown` + `discovery.misconfigured` | Same; do not retain the old instance row |
| Timeout, refused connection, or command transport failure | `present/unreachable/unknown/unknown/unknown` + `provider.unreachable` | Retain old provider data as `present/unreachable/stale`; add attention |
| Response received, but missing/invalid `schema` or invalid against a supported schema | `present/reachable/unknown/unknown/unknown` + `provider.malformed` | Retain old provider data as `present/reachable/stale`; add attention |
| Well-formed header with unknown schema name or major | `present/reachable/unknown/unknown/unknown` + `provider.unsupported` | Retain old provider data as `present/reachable/stale`; add attention |
| Valid response with expired heartbeat | Normalize provider data as `present/reachable/stale`; add `provider.stale` | Replace the previous row with the newer stale row |

The slash-separated tuple is
`presence/reachability/freshness/health/microphone`. Synthetic cold-start rows
have null provider revision and source timestamp, no workloads/actions/queue/
engines, and an attention item. Only a true optional absence has no attention.

## Indicator function

Evaluate these predicates in order:

1. RED when any fresh provider reports an active microphone, or any durable
   microphone latch remains set.
2. YELLOW when any component is not in the neutral set or an operation is
   `accepted`, `rejected`, `failed`, or `timed_out_unknown`.
3. GREEN otherwise.

The neutral set is exactly:

- an optional absent component; or
- a present, reachable, fresh, healthy component with an inactive microphone
  and no actionable warning/error attention.

Each component indicator uses the same function restricted to that component
and its operations. A completed operation is neutral.

## Operation lifecycle

```text
request -> rejected
request -> failed
request -> completed
request -> accepted -> completed
                    -> failed
                    -> timed_out_unknown -> completed
                                         -> failed
```

- `accepted` means the product accepted work but the target state is not yet
  observed. It has an action-specific deadline of at most 120 seconds.
- `completed` means a fresh observation or product result proves the requested
  target state. It remains for one fresh poll and is then removed.
- `rejected` means no side effect started. It remains until locally acknowledged
  or superseded by a successful request for the same component/action.
- `failed` means a side effect was attempted and is known not to have reached the
  target. It follows the same clearing rule as `rejected`.
- `timed_out_unknown` means the caller cannot prove whether the side effect ran.
  It cannot be acknowledged away. Re-observation and the product's request-ID
  query must eventually transition it to `completed` or `failed`.

Operations are in-memory aggregate projections. A tray restart reconstructs
unresolved product work from the product's request-ID/status surface; only the
microphone safety latch persists in HeyMa.

## Action registry

The descriptor schema is closed and validated before display. Product adapters
may omit an unsupported action but cannot mint synonyms for these v1 meanings.

| Action ID | Capability | Mode | Target | Disruptive |
| --- | --- | --- | --- | --- |
| `wax.capture.start` | `capture.control` | desired-state | `active` | no |
| `wax.capture.stop` | `capture.control` | desired-state | `inactive` | no |
| `wax.queue.open` | `queue.inspect` | opener | null | no |
| `wax.output.open` | `output.open` | opener | null | no |
| `wax.logs.open` | `logs.open` | opener | null | no |
| `wax.service.restart` | `service.restart` | command | `running` | yes |
| `vinyl.dictation.start` | `dictation.control` | desired-state | `active` | no |
| `vinyl.dictation.stop` | `dictation.control` | desired-state | `inactive` | no |
| `vinyl.dictation.cancel` | `dictation.control` | command | `inactive` | no |
| `vinyl.logs.open` | `logs.open` | opener | null | no |
| `vinyl.service.restart` | `service.restart` | command | `running` | yes |
| `voxxy.engine.select` | `engine.select` | desired-state | selected engine ID | yes |
| `voxxy.health.open` | `health.inspect` | opener | null | no |
| `voxxy.logs.open` | `logs.open` | opener | null | no |
| `voxxy.service.restart` | `service.restart` | command | `running` | yes |

Start/stop actions are explicit. A tray menu item may look like a toggle, but it
chooses one descriptor from a fresh snapshot and never sends `toggle`. Product
actions serialize all callers and deduplicate request IDs.

`voxxy.engine.select` has one required `enum` parameter named `engine_id`, whose
values come from the same fresh snapshot. Opener actions accept no arbitrary
path or URI from the UI; the adapter uses the trusted product-reported target.
Lifecycle actions accept an optional maintenance token only through the internal
adapter call, not as user input.

## Maintenance lease protocol

Each product implements the request/lease structures in
`heyma-actions-v1.schema.json` through its stable CLI or API. The raw token is
returned only to the acquiring client; status exposes the token-free projection
from `provider-status-header-v1.schema.json`.

1. `acquire` takes a unique request ID, owner, reason, and TTL from 30 through
   300 seconds. Under the product-wide lock, it expires any old lease, proves all
   product-defined inhibitors clear, persists and fsyncs the lease, increments
   generation, and returns its token. A refusal returns `state=rejected` with
   null lease/token/timestamps and a stable `code`/redacted `detail`. Repeating
   the request ID is idempotent.
2. Every protected start, restart, reconfiguration, or cutover path—including a
   CLI or hotkey used while the daemon is stopped—takes the same product-wide
   lock and rejects unless no active lease exists or the caller presents its
   token.
3. `renew` presents lease ID and token. It is idempotent, extends expiry by at
   most 300 seconds, fsyncs the lease, and increments generation. The migration
   client renews before half the TTL elapses.
4. `release` presents lease ID and token. It is idempotent, removes the lease
   atomically, and increments generation. Released or expired results retain
   lease identity/timestamps for audit but return no token. Expiry has the same
   state effect.
5. Multi-product acquisition is ordered `wax`, `vinyl`, `voxxy`. On any failure,
   release acquired leases in reverse order. No product mutation starts until
   every lease is active and re-observed.

Lease storage is product-owned and durable independently of the daemon. TTL
expiry prevents a dead migration client from permanently disabling normal use;
an executing migration must stop before its renewal safety margin if renewal
fails.

## Wax `dropoff` importer

Wax owns a continuous reconciler before Syncthing is repointed. It runs at boot
and at least every ten seconds, with filesystem events used only as a latency
hint.

For each allowlisted, non-Syncthing-temporary regular file in `dropoff`, Wax:

1. opens the source without modifying it and copies bytes while computing
   SHA-256 into an inbox-local staging file;
2. verifies source device/inode/size/mtime before and after the copy and retries
   if they changed;
3. fsyncs the staging file and parent directory;
4. deduplicates by the full SHA-256 ledger identity;
5. publishes with atomic no-replace rename, resolving name collisions without
   overwriting; and
6. records the imported content identity before reporting success.

A restart repeats reconciliation safely. Wax never renames, deletes, chmods, or
writes a marker into `dropoff`; Syncthing temporaries, `.stfolder`, and
`.stversions` are excluded. A copied source remains in `dropoff` under Syncthing
ownership.
