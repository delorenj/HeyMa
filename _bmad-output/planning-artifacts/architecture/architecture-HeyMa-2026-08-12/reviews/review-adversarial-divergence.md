# Adversarial Divergence Review — HeyMa Federated Audio Control Plane

## Scope and verdict

Reviewed [ARCHITECTURE-SPINE.md](../ARCHITECTURE-SPINE.md) as a build substrate,
with [MIGRATION-PLAN.md](../MIGRATION-PLAN.md) used to test release and cutover
behavior. The adopted repository and ownership boundaries are coherent, but the
document is not yet deterministic enough for independent implementations. Teams
can obey every architecture decision and still ship mutually incompatible status
documents, action protocols, discovery behavior, safety decisions, and service
cutovers.

The tiers below group findings by architectural layer; they do not express
severity, priority, or rank.

## Contract tier

- **AD-7 and “The minimum normalized observation” do not define the aggregate
  wire envelope.** One conforming CLI can return a JSON array of observations,
  while another can return `{ "schema": "heyma.control.v1", "components": [...] }`;
  both contain every listed observation field and both can claim the named
  schema. Publish an exact JSON Schema for the top-level `heyma status --json`
  response, including schema discriminator, aggregate timestamp, component
  cardinality, ordering guarantees, and error representation. Otherwise the
  tray, CLI, tests, and future consumers cannot exchange the same snapshot.

- **AD-7 names product schemas but does not specify where or how a producer
  declares one.** A Wax implementer can emit `schema: "wax.status.v1"`, Vinyl can
  emit `schema_version: 1`, and an adapter can look for a media type or nested
  `meta.schema`; all satisfy “owns a named schema” in prose. Define the exact
  discriminator key, value, location, JSON Schema dialect, content type, and
  unsupported-version result for every product surface before Phase 0 can have a
  meaningful exit gate.

- **The normalized observation sketch does not mark fields as required,
  nullable, defaulted, or conditionally present.** Independent adapters can
  represent an absent product by omitting the component, by emitting
  `presence="absent"` with `reachability="unknown"`, or by emitting null activity;
  each reading is compatible with the prose. Define required fields and a
  presence/reachability/health/activity truth table, including whether absent
  components remain in the component list. Without it, aggregate colors and
  component rows will diverge on the most common optional-product case.

- **“Compatible changes are additive” does not constrain new enum members or
  changed semantics.** A producer may add `health="maintenance"`, a new attention
  severity, or reinterpret `progress_pct=100` without removing a field; an older
  adapter that only ignores unknown *fields* still cannot interpret those values.
  Freeze v1 enum sets and semantics, define unknown-enum handling, and state which
  changes require a new major schema. Otherwise a nominally additive independent
  release can break aggregation silently.

- **Cross-field validity is unspecified.** Documents such as
  `presence="absent", reachability="reachable", health="ok"` or
  `microphone_active=true, stale=false, source_updated_at=null` are structurally
  plausible under the sketch, yet different cores will resolve them differently.
  Put relational invariants and invalid-combination behavior in the schema and
  core model, including whether malformed observations preserve a last-known
  snapshot or replace it with an adapter error. Otherwise conforming adapters can
  make opposite safety decisions from identical data.

- **The activity contract repeats the exact ambiguity that caused the prior
  “99% stuck” tray state.** A free-text `label` plus an optional percentage does
  not say whether transcription, diarization, finalization, playback, capture,
  and synthesis are stages; when progress resets; or when 100 means complete.
  Define a stable activity kind, stage enum, lifecycle state, progress unit/basis,
  and terminal rules, keeping human labels presentation-only. Otherwise adapters
  can truthfully publish incompatible progress and the unified tray can again be
  consistently wrong.

- **Queue inspection and engine selection are promised controls but have no
  normalized data model.** The minimum observation only offers opaque `details`,
  so one adapter can put Wax queue entries in `details.queue`, another can expose
  only an `open-queue` action, and a Voxxy adapter can model engines as either
  capabilities or details. Define first-class extension schemas for queue items
  and selectable engines, or explicitly make those rows adapter-owned UI
  extensions with versioned contracts. Otherwise independently built core, tray,
  and adapters cannot implement the adopted authority consistently.

- **`capabilities[]` and action IDs have no registry or namespace grammar.** Two
  compliant adapters can expose `record.toggle` versus `wax.recording.toggle`, or
  use `logs` as a capability in one and an action in another. Publish canonical
  capability/action IDs, ownership, argument schemas, result schemas, and
  evolution rules. Otherwise `actions(snapshot)` cannot be consumed without
  hard-coded adapter knowledge leaking into the core and tray.

## Action and ownership tier

- **The outbound port names methods but not their semantics.** `probe()` and
  `observe()` may be implemented as discovery versus status, or both may perform
  status reads; “typed results” does not define success, absent, unsupported,
  timeout, malformed, rejected, accepted, or completed outcomes. Specify the port
  algebra, side-effect rules, exception containment, and result types. Otherwise
  independently built adapters and core orchestration will compile against
  similarly named methods while disagreeing at runtime.

- **Toggle actions are non-idempotent under the mandated timeout behavior.** A
  Vinyl or Wax process may accept `toggle`, time out before returning, and then be
  toggled back by a retry; both adapter and product obey the allowlist and timeout
  rules. Prefer desired-state `start` and `stop` commands, prohibit automatic
  retry of non-idempotent actions, and carry an idempotency/request ID with a
  queryable outcome. Otherwise a transient timeout can produce the opposite of
  the user’s request.

- **Action completion is undefined.** `invoke()` can report success when a command
  is accepted, when its process exits, or when a later observation reaches the
  requested state. This matters immediately because Voxxy engine selection can
  persist configuration and recreate the core. Define `accepted`, `completed`,
  `rejected`, `timed_out_unknown`, and `failed` semantics plus reconciliation
  behavior. Otherwise one tray re-enables controls while work is still underway
  and another leaves them disabled indefinitely.

- **The restart safety check is a time-of-check/time-of-use guard in HeyMa, not an
  atomic product precondition.** A microphone can become active after the tray
  reads an inactive snapshot but before the restart command executes. Require the
  product’s stable action endpoint to re-check authoritative state atomically and
  reject unsafe restart, optionally using an expected generation token. Without a
  product-side guard, AD-12 cannot actually prevent a restart during capture.

- **“At most one in-flight action per component” cannot be globally enforced by
  the proposed process topology.** `heyma-tray` and the separate `heyma` CLI each
  have only in-memory projection state, so both can dispatch an action at the same
  time while individually obeying the rule. Define the rule as client-local and
  require product-side serialization, or introduce a product-owned operation lock
  and request deduplication. Otherwise simultaneous CLI and tray actions race.

- **Action enablement has no snapshot revision or freshness precondition.** An
  action descriptor derived from an old `snapshot` can remain clickable after the
  product changes state, and “stale” does not by itself say which actions must be
  disabled. Define maximum decision age, action-specific required states, expected
  product generation, and fail-closed behavior for stale/unreachable/unknown
  observations. Otherwise equally conforming clients dispatch different actions
  from the same stale state.

- **The action allowlist does not define argument validation.** Executing argv
  without a shell prevents shell expansion but not CLI option injection, hostile
  paths, arbitrary engine names, unbounded log arguments, or unsafe URLs. Give
  every action a closed argument schema, length limits, enumerated values or
  trusted path roots, and an argv construction rule that terminates options where
  supported. Otherwise a “safe” action can still invoke unintended product CLI
  behavior.

- **The safety model only names microphone activity, leaving non-microphone work
  unprotected.** Voxxy engine selection/restart can interrupt synthesis, and Wax
  restart/cutover can disrupt an active transcription queue even when
  `microphone_active=false`; the migration plan notices Wax’s active queue, but
  the architecture action policy does not. Model active workloads and define
  action-specific disruption preconditions for capture, transcription,
  dictation, and synthesis. Otherwise a compliant tray may destroy in-flight
  work while satisfying AD-12 literally.

- **Timeout does not define process termination or reconciliation.** Killing only
  a CLI parent can leave `systemctl`, Docker Compose, a log follower, or another
  child running after HeyMa reports timeout. Define per-action deadlines, process
  group creation and termination, bounded stdout/stderr, actions such as logs
  that deliberately hand off to a viewer, and post-timeout observation. Otherwise
  “one in flight” becomes false and late side effects surprise the user.

## Discovery and security tier

- **AD-8 does not define discovery precedence.** A host may have a PATH executable,
  a configured command override, a default health endpoint, and an endpoint
  override simultaneously; Wax and Vinyl also expose both state mirrors and CLIs.
  Specify an ordered decision table for command/source selection, whether an
  explicit override suppresses fallback, and how conflicts are surfaced. Without
  it, two conforming installations can control different product instances under
  the same component ID.

- **Absent, unreachable, incompatible, and misconfigured are not separable from
  the discovery rules.** A missing PATH executable is called absent, but the prose
  does not settle whether an explicitly configured but unreachable endpoint, an
  executable returning malformed JSON, or an unsupported schema is absent or
  unhealthy. Define a discovery state machine and make explicit configuration
  establish intent/requiredness. Otherwise aggregate GREEN can conceal a broken
  configured product that another implementation correctly marks YELLOW.

- **Executable discovery on PATH is not a trust boundary.** A user-writable PATH
  entry can supply a counterfeit `wax`, `vinyl`, or `voxxy`; command-array config
  can point anywhere; and no ownership or permission policy is specified for
  `control.toml`. Resolve and pin absolute executables, validate file ownership
  and writability appropriate to the user-service threat model, reject insecure
  config modes/symlinks where warranted, and show the resolved target in
  diagnostics. Argv execution alone does not prevent running the wrong program.

- **Endpoint overrides lack transport and trust policy.** An implementation may
  accept arbitrary HTTP URLs and redirects while another permits only loopback
  HTTPS; both satisfy “documented health endpoints.” Define allowed schemes,
  hosts, redirect behavior, TLS verification, DNS rebinding considerations,
  response byte/depth limits, and whether remote control is opt-in separately
  from remote observation. Otherwise a config typo or hostile response can turn
  the always-running tray into an SSRF or resource-exhaustion client.

- **AD-13 does not explain how an authenticated public action is invoked without
  HeyMa handling authentication.** Voxxy can protect APIs with a product API key;
  “the product owns credentials” is compatible with calling its authenticated CLI
  or with an HTTP adapter needing a token, but those implementations are not
  interchangeable. Define a credential-free adapter route through the product
  CLI, or a named product-owned credential helper/reference protocol that never
  returns secrets to presentation code. Otherwise remote actions either fail or
  quietly duplicate credentials in HeyMa.

- **Redaction is stated as an outcome without a data-handling contract.** Product
  JSON, stderr, logs, paths, URLs, and attention messages can contain secrets,
  terminal controls, Pango markup, or extremely large strings. Define field-level
  allowlisting, size/control-character normalization, UI escaping, structured
  error codes, and a redaction boundary before logging as well as display.
  Otherwise independently built adapters leak or render hostile product output
  differently.

- **`open-output` and logs cross the process boundary without a target policy.** A
  product can report a filesystem path, `file:` URI, web URL, or follow-mode log
  command, and every form could be described as a safe daily action. Define
  trusted output roots, URI schemes, existence/type checks, viewer launch rules,
  and whether logs are snapshots or streams. Otherwise the tray may open an
  attacker-controlled target or hang forever while remaining within AD-4.

## Time, freshness, and aggregation tier

- **“Hard deadlines,” “stale,” and polling have no numbers or ownership.** One
  adapter can use a 500 ms deadline and 2 s stale threshold while another uses 10
  s and 5 minutes; both comply and produce materially different icon states.
  Define default and maximum probe/observe/action deadlines, poll cadence, stale
  thresholds by surface, configuration bounds, jitter, and timeout classification.
  Without this, stack tests cannot assert compatibility.

- **Timestamp fields cannot prevent regression from overlapping observations.**
  `observed_at` and optional `source_updated_at` do not define which clock creates
  them, how clock skew is treated, or how late completion of poll N is rejected
  after poll N+1. Require a core-assigned monotonic poll sequence, product
  generation where available, and explicit suspend/resume behavior. Otherwise a
  slow old response can overwrite a newer inactive or active state.

- **The RED microphone latch cannot survive the prescribed tray restart.** AD-10
  says last-known active remains RED until a positive inactive observation, while
  AD-11 allows only in-memory last-known state and AD-5 makes restarts normal. If
  the tray restarts while capture is active and the first probe times out, it has
  no last-known active fact and may show YELLOW or GREEN. Either require every
  product’s authoritative durable state surface to recover capture state on cold
  start or permit a narrowly scoped durable safety latch. Otherwise the claimed
  safety invariant disappears at exactly the failure boundary it is meant to
  cover.

- **The RED latch has no retirement rule when a component disappears.** After an
  active product is uninstalled, reconfigured to a new instance, or permanently
  fails, a positive inactive observation may never arrive; “optional absent is
  GREEN” conflicts with “last-known active remains RED.” Define identity epochs
  and a deliberate, audited acknowledgement/cold-proof path that distinguishes
  product retirement from transient loss. Otherwise a compliant tray can remain
  permanently red or clear a real active capture arbitrarily.

- **The aggregate icon policy lacks a complete precedence table.** Attention
  severity values and ordering are undefined, `health=error` can coexist with no
  actionable attention, and the prose only says YELLOW depends on “actionable
  attention.” Define the color for every presence/reachability/health/stale/
  microphone/attention combination and establish which fields adapters versus
  core may derive. Otherwise two cores can label the same normalized observations
  GREEN and YELLOW while both follow AD-10.

- **Polling concurrency has no overlap or backpressure rule.** “Poll concurrently
  off the GTK thread” allows a new polling round to begin while a timed-out round
  still owns a subprocess or HTTP connection. Define one observation operation in
  flight per adapter, cancellation boundaries, late-result discard, bounded
  worker resources, and coalescing after resume. Otherwise a missing product can
  accumulate work and eventually freeze or exhaust the tray despite per-call
  deadlines.

## Release, verification, and cutover tier

- **Independent releases have no compatibility window or negotiation rule.** The
  plan adds three v1 surfaces and then builds HeyMa, but it does not define minimum
  product versions, how long old schema majors remain supported, which side ships
  first, or how unsupported capability versions affect controls. Publish a
  compatibility matrix and product capability handshake, and require adapters to
  retain fixtures for every supported producer release. Otherwise a routine
  independent update can remove controls or misclassify status despite all local
  tests passing.

- **Consumer-driven tests are not actually distributed to providers.** A product
  can test its own interpretation of `wax.status.v1` while HeyMa tests a frozen
  fixture, and both CI suites pass after their semantics diverge. Make the product
  schema an immutable published artifact, validate real provider output against
  it, run HeyMa adapter tests against tagged provider fixtures or binaries, and
  define who approves contract evolution. Otherwise AD-14 detects syntax drift
  only after integration.

- **The Phase 4 idle gate is observational rather than transactional.** Each line
  can be true when checked, then a hotkey can start Wax or Vinyl before Wax’s
  no-tray restart or native-tray disable. Add a cutover procedure that temporarily
  inhibits new UI-initiated starts without stopping active work, rechecks
  authoritative generations immediately before each mutation, and aborts on any
  change. Otherwise a perfectly executed checklist can still restart during a
  recording.

- **Shadow and native control surfaces can race once Phase 3 adds actions.** The
  plan keeps native trays enabled through parity verification but does not say
  whether the shadow tray remains read-only during action testing or how duplicate
  controls are serialized. Keep the shadow indicator explicitly read-only until
  the cutover transaction, or rely on product-side idempotency/serialization and
  label which control surface owns each action. Otherwise two apparently valid
  trays can issue opposing toggles.

- **Service restart ownership is ambiguous.** AD-3 permits only stable product
  CLIs/public APIs, while the structural diagram and migration steps speak in
  concrete systemd units; an adapter might call `systemctl --user restart` directly
  and another might call `wax service restart`. Require lifecycle actions to pass
  through a product-owned command/API that knows its safety checks, and treat unit
  names as deployment details rather than contract. Otherwise repository
  extraction or unit renaming breaks a supposedly stable adapter and can bypass
  product guards.

- **Rollback does not restore an exact recorded deployment state.** “Save unit
  definitions and enabled states” omits active state, drop-ins, environment,
  no-tray configuration location, masked/static units, indicator IDs, and partial
  failure between steps. Define a preflight manifest, atomic file installation,
  step journal, post-step assertions, reverse operations, and an idempotent retry
  procedure. Otherwise rollback can produce duplicate trays, no tray, or an
  unexpectedly restarted daemon while still following the prose.

- **Several exit gates are not machine-verifiable.** “Every combination,” “never
  disagrees,” “feature and safety parity,” and “no safety-critical stale display”
  do not name fixtures, timing bounds, acceptable transitions, or evidence to
  retain. Turn each gate into a versioned test matrix with deterministic clocks,
  injected timeouts/malformed payloads, concurrent action cases, cold tray
  restart during capture, suspend/resume, and cutover rollback. Otherwise two
  teams can declare the same phase complete using incompatible interpretations of
  success.

## Closure criterion

The spine is ready for parallel implementation when it adds normative schemas
and transition tables for status, discovery, freshness, aggregation, actions, and
results; makes safety preconditions product-atomic; defines a compatibility and
trust policy; and converts migration gates into executable evidence. Until then,
the architecture is a strong ownership decision but an incomplete interoperability
contract.
