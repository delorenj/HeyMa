# EP-Chain Adversarial Review — the "Refined Wax Pipeline" vision and its proposed mapping

**Date:** 2026-10-12
**Reviewer role:** EDA architect, asked to attack both sides
**Inputs under review:**

1. **The vision** — `~/quickdrop/wax-ep-diagram.excalidraw` (draw.delo.sh room
   `065d07640c75f2ab5f2a`, exported verbatim): intent-by-bucket ingest, a primed
   transcript, and a conditional EP chain `slugify/title → dialog? → diarize →
   extract project → multiple? → per-project delegate | delegate transcript`,
   plus "Delegate to Grolf" and "Agent Inbox (Holocene Auto)" lanes.
2. **The proposed mapping** — implementing that chain with today's primitives:
   `skip_when` for two-way branches, pass-reported `{"state":"skipped"}` for
   n-way branches, per-branch frontmatter booleans as a scaling pattern,
   `requires`-satisfied-by-`skipped` for convergence, and `emits` event fan-out
   for per-project multiplicity.
3. **The runtime that would host it** — `src/wax/passes.py`, `finalize.py`,
   `events.py`, `worker.py`, `config/passes.d/`, `docs/ENRICHMENT-PASSES.md`.

Findings are severity-rated: **BLOCKER** = cannot spec stories until decided;
**HIGH** = will cause an outage, silent data loss, or a contract you will have
to un-bake; **MEDIUM** = cost/ops burden that compounds; **LOW** = note.

---

## 0. What is already right (do not re-litigate)

Adversarial reviews that only attack are lazy. These survive contact and should
be treated as settled foundations:

- **Transactional outbox with PubAck-gated publication** (`events.py:146-219`).
  Fail-open at the boundary, "delivered means acked", drain in id order.
  Textbook, correctly implemented.
- **Deterministic identity.** `uuid5` command ids (`ep:<item>:<slug>:<attempt>`),
  content-keyed pass-event ids (`ep-event:<item>:<slug>:<type>:<key>`),
  plan-keyed completion id. Replays converge instead of duplicating.
- **The declarative result envelope** (`wax.ep.v1`). Passes propose; the runner
  applies, validates, and owns every mutation. Passes cannot touch the world.
- **Plan snapshotting** (`processing_plans`). What ran on an item is a fact,
  not a config lookup.
- **The vision's instinct to prime the transcript up front** so downstream
  stages are explicit and targeted. That is the correct direction — see V1 for
  the contradiction sitting next to it.

---

## 1. Findings on the vision (the diagram)

### V1 — BLOCKER — The diagram contradicts itself on routing: "intent implicit by bucket" vs "primed with pre-EP frontmatter … explicit targeted EPs"

The left frame draws intent as *implicit in the bucket name*
(`s3://recordings/inbox` vs `s3://recordings/agents/grolf`) while the same
frame annotates that the transcript is primed with pre-EP frontmatter for a
"precise journey through **explicit** targeted EPs". These are opposite
routing philosophies:

- **Implicit-by-bucket** is stringly-typed ambient routing. Renaming a bucket,
  or a producer dropping a file in the wrong one, silently re-routes intent.
  Buckets are storage layout; intent is contract.
- **Explicit priming** requires the producer to know the route at capture
  time — but the bucket lane exists precisely because some producers (a phone
  dropping into Syncthing, an agent inbox) cannot prime anything.

Resolution that keeps both honest: bucket/ingest-lane is a **capture-time
claim**, normalized into one explicit meaning field at transcribe time with
its provenance recorded — e.g. `intent: grolf` plus `intent-source:
bucket|declared|inferred`. Every conditional EP then keys on the *field*,
never on the bucket. The bucket never appears in a condition expression.

### V2 — HIGH — "dialog?" gates diarization on the weaker of the two available signals, pointed in the unsafe direction

The diamond asks "dialog?" and routes Yes → diarize. But:

1. The gate is `classification`, an LLM text judgment (`monolog|meeting|other`),
   while the stage being gated — Sortformer diarization — is the stage that
   *definitively answers the speaker-count question*. The diagram routes on the
   lossy proxy instead of the measurement.
2. The cost asymmetry is inverted. Diarizing a monolog costs GPU minutes and
   yields a trivially correct one-speaker result. Skipping diarization on a
   misclassified meeting yields a **permanently unattributed transcript** — and
   per this repo's own incident history (two week-long silent degradations
   behind a green tray), a silent skip is the failure shape this system keeps
   actually having.

Recommendation: invert the default. Diarize unless there is explicit negative
evidence (duration under a floor, source metadata declaring single speaker),
and let `classification` remain a *description* field. If a gate is kept for
cost reasons, disagreement between classification and diarization's observed
speaker count must be a logged, surfaced anomaly — not a silent branch.

### V3 — BLOCKER — The terminal "delegate" nodes have no contract

"Delegate Transcript to PM", "Extract all project-specific data and delegate
to PM", "Delegate to Grolf" — delegation is drawn as a terminal action with no
statement of what it *is*. Bloodbank has two lanes and the choice is
load-bearing:

- **Event** (`bloodbank.evt.*`): an immutable fact — "delegation was
  requested". Projected to Candystore, auditable, but expresses no
  expectation of execution. Fire-and-forget.
- **Command** (`bloodbank.cmd.*`): targeted intent with idempotency key and a
  lifecycle (`task.requested/started/completed` mirrors — the pattern Wax
  already uses for EP commands). Not projected to Candystore; invisible in
  the audit trail unless mirrored.

The existing ticketable pattern — event `intake.detected` + the PM's
`repo.intake.triaged` closing the loop via `intake_id` — is a deliberate
event-plus-acknowledgment design. That pattern must be chosen **per
delegation target, on paper, before any delegate EP is written**: who acks,
what closes the loop, what happens when the target is down for a week. If
"Delegate to Grolf" has no triage loop, it is a fire-and-forget delegation
that fails exactly like the two silent outages — at one remove, where even
`wax doctor` cannot see it.

### V4 — BLOCKER — "Agent Inbox (Holocene Auto)" is drawn as a peer of AudioFiles, which breaks the pipeline's core invariant

The pipeline's founding guarantee is *the audio is the irreplaceable
artifact*: backup-first, content-addressed, verified, never deleted. Every
invariant check (sanity gate, duration coverage, S3 verify) assumes an audio
file exists. A Holocene-injected item drawn entering beside `AudioFiles` has
no audio. Either:

- the inbox contract grows a second, document-first item shape (and every
  gate learns which shape it is looking at), or
- Holocene items enter **downstream of transcribe**, as documents handed to
  the EP stage directly.

Both are legitimate. The diagram's ambiguity is not. Decide before stories;
this determines whether "item" means one thing or two.

### V5 — MEDIUM — The "multiple?" loop hides per-project failure semantics

The diagram draws a per-project loop (`for each project ⇄ extract &
delegate`). The proposed implementation is one pass emitting N events. That
collapses per-project observability: pass state is per-slug, so "project 2 of
3 failed extraction" has no ledger representation — the pass either completes
(all events publish) or fails (none do; retries republish idempotently, which
is correct but coarse). If per-project failure visibility matters, it must be
carried in the pass's `reason_code` taxonomy and the emitted events' keys,
because the ledger will not give it to you.

---

## 2. Findings on the proposed mapping and the runtime that hosts it

### R1 — BLOCKER — The completion barrier couples every delegation to the slowest sibling, and the only re-drive is a human

`finalize()` refuses to publish `transcription.completed` **and all
pass-declared events** until every planned pass is `completed` or `skipped`
(`finalize.py:212-218`). Diarization carries `timeout_s: 86400`
(`diarization.yaml`). So one wedged pass withholds the item's entire event
surface — the tickets, the delegations, the completion fact.

The compensating control is `wax ep sweep`, and it is **manual-only**:
no systemd timer, no waxd-internal scheduler (verified 2026-10-12;
`bin/waxd:228` prints "retry with `wax ep sweep`" as a *hint*). After
`--max-attempts 3` (CLI default, `bin/wax:1239`) a pass is failed forever;
the item sits in `enrichment_pending` (`worker.py:431`); nothing is emitted
to the bus; the tray may or may not be looked at.

For an event-driven system whose downstream value *is* the events, the
emission path currently ends in a single point of silent failure. This is the
finding most likely to bite first, and it should be fixed independently of
any chain work:

1. Per-subgraph publication: a pass's declared events publish when the pass's
   own `requires` subgraph is complete, not when the whole plan is.
   `transcription.completed` stays plan-gated as the summary fact.
2. Dead-letter emission: a pass exhausting its attempts emits a bus event
   (e.g. `audio.task.exhausted`), so stuck items are visible where the rest
   of the fleet actually looks.
3. Sweep becomes a scheduled actor (systemd user timer or waxd-internal
   periodic), with the attempt bound and the dependency_failed exemption
   intact.

### R2 — HIGH — `emits` validates event *shape*, never event *schema*

`_validated_events` (`passes.py:376-431`) checks `{type,key,data}` structure,
key uniqueness, size, and the registry allowlist — and nothing else. The
envelope stamps a `dataschema` URL (`events.py:121`) but no code ever
validates `data` against `bloodbank/schemas/bloodbank/audio/intake.detected.json`
or any other schema. Wax publishes straight to NATS via `natsclient`,
deliberately bypassing bb-emit — whose guard "is what caught the drift
everywhere else" (`events.py:17-23`).

So an LLM-derived delegation payload can drift from schema and still publish;
Candystore projects the fields it recognizes and drops the rest. That is
silent data loss at the projection boundary, and the platform rule is
explicitly schema-first. Chain work adds *more* emitted types
(`delegate.requested`, per-target variants) — each an unvalidated contract.

Fix direction: registry `emits` entries name their schema; the runner
validates `data` against it at result-apply time (fail the pass with
`result_apply_failed` on violation — the machinery exists); a quarantine
lane in the outbox catches anything that slips through.

### R3 — HIGH — Self-reported skip moves routing logic into imperative code where the plan cannot see it

The proposal leans on passes exiting 0 with `{"state":"skipped"}` for any
condition richer than single-field equality. At chain scale this smears the
routing table across N pass executables: `wax ep list` cannot show the
branches, `wax doctor` cannot verify them, and the only way to know why
nothing happened is to read every pass's source. The system's whole
observability posture (ledger rows, `reason_code`, note history) is built on
the runner knowing *why* — imperative self-skip quietly re-opens the
"green but nothing happened" failure family the AGENTS.md warns about.

Fix direction: conditions belong in the registry, evaluated by the runner.
Grow `skip_when` into a real `when:` language — `equals`, `not_equals`,
`in: [...]`, `exists`, `empty`, list-length operators — statically
introspectable and dry-runnable. Reserve pass-reported skip for conditions
only discoverable at runtime (provider says the content was empty), which is
what `ticketable`'s `no_project` actually is.

### R4 — HIGH — The "per-branch boolean flags in frontmatter" pattern contradicts the approved shared-contract split

The proposal suggested a router EP stamp `ep-diarize: true/false`-style flags
onto the note so `skip_when` can key on them. The design direction approved
2026-10-05 is explicit: **notes carry meaning, stable identity and
provenance; workflow routing/dispatch controls and runtime diagnostics are
separated and scoped, not stamped into all notes.** Control flags in vault
frontmatter are control-plane state leaking into the durable document
contract — every vault consumer then has to know which fields are meaning and
which are plumbing.

Conditions should read *meaning* fields (`classification`, `project-ids`,
`intent`) — which legitimately belong in notes — and routing state should
live in the ledger and registry.

### R5 — HIGH — Registry validation has fleet-wide blast radius

`ordered()` raises `PassError` on any malformed `requires`/`after`/`skip_when`
(`passes.py:127-156`), and `run()` calls `ordered(reg)` on *every* pass run
(`passes.py:577`), as do `ensure_plan` and `run_auto`. One bad YAML file
takes down the entire EP stage for every item. The doc knows this
(`ENRICHMENT-PASSES.md:9` warns about it) but knowing is not a control.
Conditional chains multiply registry complexity — more edges, more
conditions, more chances to brick the stage with a typo.

Fix direction: `wax ep lint` (or a doctor gate) validates the registry at
write time; `ordered()` quarantines the offending pass and keeps the rest of
the fleet running, mirroring how `_validated_events` already checks `emits`
lazily "so a typo in one pass's YAML fails that pass's runs rather than
raising out of ordered() and taking down every item" (`passes.py:380-384`) —
the codebase already knows this pattern; it is applied inconsistently.

Related: `requires` must name **enabled** passes, so an operator setting
`enabled: false` on diarization would raise out of `ordered()` for the whole
fleet under the proposed convergence wiring. `requires` needs an optional
modifier, or the enable-toggle needs to be decoupled from graph validation.

### R6 — MEDIUM — `skipped` conflates three meanings, and convergence depends on never needing to tell them apart

`requires` is satisfied by `completed` **or `skipped`**. But `skipped` means:
"condition did not apply" (`condition_matched`), "administratively off"
(`explicitly_disabled`), and "the pass chose not to" (`no_project`). All
three unlock dependents identically. A dependent that genuinely needs the
dependency's *output* (not merely its absence) must re-check frontmatter
itself — undeclared, so the plan lies about its data dependencies.

Fix direction: keep the skip-satisfies-requires convergence semantics (it is
correct for the diagram's Yes/No rejoin), but make the reason taxonomy
first-class, and consider `requires` entries with an explicit
`state: completed` qualifier for the cases where output is truly consumed.

### R7 — MEDIUM — Version bumps cascade expensively through chains

The `requires` gate compares the dependent's record against the dependency's
*live registry version* (`passes.py:584-590`). Bumping `title-slug` to v4
invalidates every downstream dependent per item — `dependency_failed: need
v4` — forcing a full chain re-run, LLM calls included. Today the chain is
two deep; the vision makes it five. The mechanism is deliberate (semantic
version = rerun semantics), but at chain depth it needs a cost model, and
probably a distinction between "definition changed" (the ledger already
records `definition_id`) and "semantics changed".

### R8 — MEDIUM — Single-threaded worker, synchronous passes, 24h timeouts

The worker drain loop processes one item at a time (`worker.py:642-656`), and
passes run synchronously via `subprocess.run` inside it. One long diarization
head-of-line-blocks the whole queue; every conditional EP added lengthens
per-item latency linearly. At one-user scale this may be acceptable — but
delegation implies consumers with freshness expectations ("tickets appear
minutes after I stop talking"). Decide the latency budget, then decide
between pass-level parallelism within an item (the per-item lock already
serializes correctly) or item-level parallelism.

### R9 — MEDIUM — Frozen plans mean the backlog never takes the new route

`ensure_plan` snapshots definitions per item; a completed item keeps its
stored plan forever, and new passes reach it only via manual `wax ep run`
plus event backfill (`finalize.py:152-184`). When the conditional chain
lands, every in-flight and historical item silently stays on the old route
unless an explicit re-plan/migration story is written. That story belongs in
the epic, not in tribal memory.

### R10 — MEDIUM — Emitted delegations have no compensation path

Events are immutable. If `project-extraction` misattributes (Jev at P=0.61
over the 0.6 floor) and `ticketable` files tickets under the wrong project,
nothing can retract the `intake.detected` — there is no `intake.withdrawn`.
For PM tickets the human triage loop is the checkpoint (good — keep it). For
any delegation target *without* a human triage step (Grolf?), false-positive
leakage creates real work in places nobody is watching. Either add a
correction event per delegation type, or document the acceptance decision
explicitly.

---

## 3. Target architecture — the recommendation in one place

1. **Registry-owned declarative conditions.** Replace `skip_when` with a
   `when:` block (`equals`/`not_equals`/`in`/`exists`/`empty`/list-length),
   evaluated by the runner, lintable, dry-runnable. `skip_when` stays as a
   back-compat alias. Pass-reported skip survives only for runtime-discovered
   conditions.
2. **Conditions read meaning fields only** (`classification`, `project-ids`,
   `intent`, duration). No control flags in notes, per the approved
   shared-contract split. `intent` becomes a first-class meaning field with
   `intent-source` provenance; buckets are capture-time claims, never
   condition inputs.
3. **Split the completion barrier.** Per-subgraph event publication now;
   plan-gated `transcription.completed` remains the summary fact.
4. **Dead-letter + scheduled sweep.** Attempt exhaustion emits a bus event;
   sweep runs on a timer. No pipeline stage's failure mode may be "waits for
   a human who doesn't know".
5. **Schema-enforced emission.** `emits` entries carry schema refs; the
   runner validates `data` before enqueue; violations quarantine.
6. **Delegation contracts on paper first.** Per target: event-vs-command, ack
   loop, down-for-a-week behavior. PM = existing `intake.detected` +
   triaged loop. Grolf = decide, likely `bloodbank.cmd.*` via the fleet
   gateway with lifecycle mirrors if execution is expected.
7. **Diarization default inversion.** Gate on explicit negative evidence, not
   a classifier's positive guess; surface classification/diarization
   disagreement as an anomaly.
8. **Registry lint + per-pass quarantine.** One bad YAML disables itself,
   never the fleet.
9. **Concurrency decision with a stated latency budget.** Pass-level
   parallelism within an item is the cheap win; the lock already exists.
10. **Re-plan migration** for the existing backlog when the chain lands.
11. **Correction events** (`intake.withdrawn`) or documented acceptance of
    false-positive leakage per delegation target.

## 4. Open questions that block story-writing

1. Is `dialog?` needed as a gate at all, given diarization answers speaker
   count definitively? (V2)
2. What does "Delegate to Grolf" execute, who acks it, and what is its
   freshness SLA? (V3)
3. Do Holocene-injected items carry audio or not — one item shape or two? (V4)
4. What is the latency budget from recording-stop to ticket/delegation? (R8)
5. Is all-or-nothing pass completion acceptable observability for per-project
   extraction, or does "project 2 of 3 failed" need ledger representation? (V5)
6. Does any delegation target get a human triage checkpoint? If not, is
   false-positive leakage accepted or corrected? (R10)

## Appendix — evidence index

| Claim | Evidence |
|---|---|
| Completion barrier gates all pass events | `finalize.py:212-218`, `ENRICHMENT-PASSES.md:139-141` |
| Sweep is manual; 3-attempt default; item parks in enrichment_pending | `bin/wax:1239`, `bin/waxd:228`, `worker.py:431`; no systemd timer exists |
| Emits validated for shape only | `passes.py:376-431` |
| Wax bypasses the bb-emit schema guard | `events.py:17-23` |
| Registry-wide blast radius of one bad YAML | `passes.py:127-156`, `:577`; `ENRICHMENT-PASSES.md:9` |
| Skip satisfies requires and finalization | `ENRICHMENT-PASSES.md:96`; `passes.py:584-590` |
| Diarization 24h timeout | `config/passes.d/diarization.yaml` |
| Worker is single-threaded, passes synchronous | `worker.py:632-656`, `passes.py:655` |
| Plans frozen per item; backfill manual | `passes.py:159-180`, `finalize.py:152-184` |
| Shared-contract split (no control fields in notes) | design direction approved 2026-10-05 |
| Events publish only from completed passes | `finalize.py:75`; `ENRICHMENT-PASSES.md:130-131` |
