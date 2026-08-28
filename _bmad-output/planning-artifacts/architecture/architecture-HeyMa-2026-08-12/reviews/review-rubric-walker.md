# Good-Spine Rubric Review

- **Target:** `ARCHITECTURE-SPINE.md`
- **Companion consulted:** `MIGRATION-PLAN.md`
- **Lens:** complete good-spine checklist
- **Mechanical precheck:** `lint_spine.py` passed with zero findings
- **Verdict:** **CHANGES REQUIRED** — the federated/hexagonal direction is strong,
  but this is not yet a safe, fully convergent build substrate because the
  restart safety invariant is not enforceable and the status/contract seams
  still permit incompatible implementations.

Finding count: **1 critical, 3 high, 3 medium, 0 low**.

## Critical

### C1 — Safety-sensitive actions are neither fail-closed nor race-free

**Evidence**

- AD-12 promises to prevent restarts during active capture, but only says that
  restart is disabled while a component *reports* an active microphone
  (`ARCHITECTURE-SPINE.md:145-152`).
- AD-10 permits stale last-known observations and adapter failures
  (`ARCHITECTURE-SPINE.md:127-136`), while AD-11 makes that cache process-local
  and disposable (`ARCHITECTURE-SPINE.md:138-143`). A stale, unknown,
  unreachable, or cold-start component therefore does not positively report an
  active microphone and can fall through AD-12's guard.
- The companion's stronger invariant forbids restart, reconfiguration, or
  cutover while a microphone is active **or while Wax has an active queue item**
  (`MIGRATION-PLAN.md:20-28`), but the Wax queue precondition and the broader
  action classes do not survive into the spine.
- A menu snapshot is inherently subject to a check/use race: capture can start
  after the tray computes `enabled` and before the command executes. The
  separate `heyma` CLI and `heyma-tray` processes also make “one in-flight per
  component” process-local, so that rule cannot by itself prevent duplicates.

**Why this matters**

Two compliant implementations can both pass AD-12 while one restarts Vinyl
during live dictation or acts on Wax during active pipeline work. The stated
`Prevents` claim is therefore false at the only boundary where data loss is
plausible.

**Disposition:** **Autofix before handoff.**

The spine should bind all of the following:

1. Safety-sensitive actions fail closed unless the relevant state is a **fresh,
   positive idle** observation; stale, unknown, malformed, or unreachable state
   disables them.
2. The adapter re-observes at invocation time; the menu's `enabled` value is a
   UX hint, not authorization.
3. The product-owned public command/API rechecks its authoritative state under
   its own serialization boundary and refuses unsafe execution. This closes the
   polling race that HeyMa cannot close.
4. Product-specific preconditions are explicit. At minimum, Wax restart,
   reconfiguration, and cutover require both microphone idle and no active queue
   item.
5. Either require product-side idempotency/serialization across callers or
   narrow AD-12's duplicate-prevention claim to a single control process.

## High

### H1 — Aggregate status and freshness are not a total deterministic function

**Evidence**

AD-10 says RED for last-known microphone activity, YELLOW for actionable
attention, and GREEN for healthy or optional-absent state
(`ARCHITECTURE-SPINE.md:127-136`). The conventions add `unreachable`, `unknown`,
`degraded`, `error`, `stale`, and required-vs-optional presence
(`ARCHITECTURE-SPINE.md:171-186`), but do not map all combinations. There is no
rule for a required absent component, unreachable/unknown component,
non-actionable `health=error`, an action failure, or a stale previously-idle
component. “Hard deadlines” and “stale” also have no numeric seed, shared source
of configuration, or out-of-order-response rule.

**Divergence permitted**

One adapter can turn transport failure into actionable attention while another
does not; one aggregate implementation can show GREEN for unreachable while
another shows YELLOW. Different timeout/staleness choices can make the CLI and
tray disagree even over the same products. This directly undermines the stated
goal of a consistently accurate tray.

**Disposition:** **Autofix.**

Bind a core-owned timing policy with explicit cold-start defaults and one
configuration source, discard observations older than the current probe
generation, and define a total ordered aggregation table. At minimum, GREEN
must require an explicit neutral/healthy classification; RED microphone
precedence must remain absolute; every other tuple must deterministically map
to YELLOW or GREEN. Add table-driven tests for the full cross-product state
matrix, not only named examples.

### H2 — AD-14's cross-repository drift prevention is not enforceable

**Evidence**

AD-7 makes each product the owner of its schema, while AD-14 gives HeyMa local
fixtures and each product local provider tests (`ARCHITECTURE-SPINE.md:100-107`,
`162-169`). The structural seed contains only HeyMa's normalized snapshot schema
(`ARCHITECTURE-SPINE.md:238-240`). Nothing binds how a producer's authoritative
schema reaches HeyMa, how a same-major compatibility check runs in producer CI,
or how supported versions are retired.

**Divergence permitted**

A product can change its `*.v1` response and update its own test while HeyMa's
copied fixture remains stale. Both repositories remain green until the change
reaches the live tray. Explicitly rejecting a future `v2` avoids silent parsing,
but does not fulfill AD-14's stronger claim that contract drift cannot reach
production.

**Disposition:** **Autofix.**

Choose one enforceable handshake: for example, products publish immutable
versioned JSON schemas and validate same-major backward compatibility in CI;
HeyMa pins supported schema artifacts and tests adapters against producer
fixtures. Also bind a minimal compatibility/deprecation rule (supported version
matrix and rollout order). Keep the implementation polyrepo; this does not
require a shared Python SDK.

### H3 — The two cited Wax brownfield sources conflict and the spine does not reconcile them

**Evidence**

The spine cites both `AGENTS.md` and `components/wax/docs/WAX-DESIGN.md`
(`ARCHITECTURE-SPINE.md:17-22`) without naming a precedence or recording the
disagreement:

- `AGENTS.md` describes `./inbox` as Syncthing receive-only, local writers using
  `./ingest`, and n8n as the live watcher.
- `WAX-DESIGN.md` and `components/wax/src/wax/paths.py` describe Wax as live,
  `./inbox` as its local canonical queue, `./dropoff` as receive-only, and the
  n8n/ingest path as retired.
- A live `wax status --json` during review showed Wax owning stream/inbox state,
  corroborating the newer Wax implementation, while both `inbox/.stfolder` and
  `dropoff/.stfolder` still exist on disk.

**Why this matters**

This is exactly the sort of brownfield ambiguity a spine must ratify rather than
pass downstream. A migration agent can comply with one cited source and violate
the other, potentially restoring a retired writer or placing local recordings
in a receive-only Syncthing folder.

**Disposition:** **Discuss, then autofix the authoritative project context.**

Establish the observed current ingest topology before finalization, update or
supersede the stale source, and carry the resulting Wax preservation rules as
explicit inherited constraints (never-delete, archive-before-transcribe, and no
data-path changes as part of the tray migration). The control-plane spine need
not re-document Wax internals, but it cannot cite contradictory operational
truths silently.

## Medium

### M1 — The common action/capability contract is too underspecified for independent adapters

**Evidence**

The conventions name action IDs, `enabled`, and `disabled reason`, while the
port accepts `invoke(action_id, arguments)` and merely promises “typed results”
(`ARCHITECTURE-SPINE.md:183-184`, `201-203`). `capabilities[]`, argument shapes,
outcome states, stable error codes, cancellation, and timeout outcomes are not
bound. AD-12's “explicit allowlist” does not say whether arbitrary arguments
may be interpolated into configured command arrays.

**Divergence permitted**

Wax, Vinyl, and Voxxy adapters can expose mutually incompatible descriptors and
return semantics, forcing GTK and CLI code to special-case products or allowing
unvalidated user input to reach argv.

**Disposition:** **Autofix.**

Add the minimum core-owned action descriptor and result invariants: stable ID,
capability, fixed/validated input schema, enabled/disabled reason, synchronous
accepted/rejected/failed outcome with stable error code, and redacted detail.
Require adapters to map only validated fields into fixed argv slots; command
overrides are arrays, not templates evaluated as shell or arbitrary argv.

### M2 — The steady-state operational envelope is only partially covered

**Evidence**

The spine chooses a user service, graphical-session attachment, and no lifecycle
coupling (`ARCHITECTURE-SPINE.md:247-262`), and the migration companion provides
cutover/rollback gates. It does not decide or defer the tray's restart policy,
startup behavior with malformed configuration, logging/diagnostic sink,
configuration reload semantics, packaging/update ownership, or what constitutes
the tray's own health. These are operational/environmental choices at initiative
altitude, especially for an always-running control plane.

**Disposition:** **Autofix minimally or explicitly defer with revisit gates.**

A compact rule is enough: define who installs/updates `heyma-tray`, systemd
restart behavior, journald as the diagnostic sink, and fail-soft behavior for a
bad optional adapter/config stanza. Keep product service operations product-owned.

### M3 — “System Python” needs an executable-path rule on this host

**Evidence**

The stack accurately records the deployed `/usr/bin/python3` baseline as
CPython 3.13.7 with PyGObject 3.50.0 and GTK 3.24.50
(`ARCHITECTURE-SPINE.md:205-216`). During review, however, `python3` on `PATH`
resolved to mise Python 3.14.4 and could not import `gi`; `/usr/bin/python3`
resolved to 3.13.7 and loaded the recorded GIR stack. Existing Wax entry points
already encode the load-bearing convention with `#!/usr/bin/python3`, explicitly
rejecting `/usr/bin/env python3`.

**Divergence permitted**

“System-Python process” can be read as an environment choice while a new
launcher still uses `env python3`, yielding a tray that fails before it can show
status.

**Disposition:** **Autofix.**

State that installed GTK entry points and the systemd unit execute
`/usr/bin/python3` explicitly. Clarify that the version table is a verified host
baseline (or define supported ranges) rather than a promise to exact-pin mutable
distribution packages.

## Low

No low-only findings. Minor prose or seed preferences should remain owned by
the code rather than inflate the spine.

## Complete Checklist Walk

| Good-spine criterion | Result | Review judgment |
| --- | --- | --- |
| Fixes the real divergence points for the level below and misses none | **Fail** | Repository ownership, dependency direction, state ownership, discovery, and process topology converge well. Safety preconditions, total aggregation, cross-repo contract enforcement, and the action/result seam remain open (C1, H1, H2, M1). |
| Every AD Rule is enforceable and actually prevents its stated divergence | **Fail** | AD-10 lacks total/timed semantics; AD-12 cannot enforce its safety or duplicate-prevention claims; AD-14 uses independent copies without a verification handshake. |
| Nothing under Deferred could let two current units diverge | **Pass** | Each deferred item is outside v1 or has a useful revisit condition. None is required to build the three first-party adapters and tray. |
| Named technology is verified-current | **Pass with clarification** | The listed desktop stack matches `/usr/bin/python3` and installed GIR/systemd versions on the target host. M3 is about binding the executable and baseline semantics, not a false version. |
| Ratifies rather than contradicts the brownfield codebase | **Fail pending reconciliation** | The chosen adapter/process boundaries match current Wax, Vinyl, and Voxxy shapes, but the cited Wax source-of-truth conflict is unresolved (H3). |
| Covers capabilities from a driving spec | **N/A** | No PRD/spec is listed as an input. The declared capabilities are mapped to modules and ADs. |
| Preserves inherited parent-spine invariants | **N/A** | No parent spine is declared. |
| Every initiative-owned dimension is decided, deferred, or open | **Partial** | Paradigm, repositories, dependencies, state mutation/ownership, contracts, security boundary, testing, and desktop topology are present. The steady-state operational envelope is incomplete (M2), and timing is named but not decided (H1). |

## What Is Already Strong

- The named paradigm is compact and materially useful; the diagram, dependency
  rule, and ports all reinforce it.
- The adopted polyrepo decision is crisp and consistent with independent
  deployments and runtimes rather than aesthetic repository symmetry.
- Product state authority versus HeyMa's disposable projection is unusually
  clear and prevents a second source of truth.
- Optional-product absence, adapter-scoped failure, no aggregate daemon, and no
  product lifecycle coupling establish a resilient failure boundary.
- The bounded non-destructive tray authority and phased shadow/cutover/rollback
  plan are strong foundations once C1 is made authoritative at invocation time.

## Gate Recommendation

Do not mark the spine `final` until C1 and the three high findings are resolved.
M1 and M3 are small, clear edits suitable for the same pass. M2 can be satisfied
by one terse operational rule or an explicit Deferred entry with a pre-build
revisit condition.
