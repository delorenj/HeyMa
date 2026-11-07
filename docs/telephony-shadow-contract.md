# Telephony shadow delivery contract

Scope approved 2026-11-07: implement the Telephony component and native page **disabled by default, shadow only**. No Telnyx mutations, new resources, billable inference, live routing/hangups, recording changes, audio suppression, n8n activation, service restarts or extra recurring schedules in this release. Enforce mode must be rejected, not a configuration escape hatch. Shadow `would_block` is not a blocked call: it still rings/records/processes normally. Unknown or ambiguous callers are allowed. Preserve all current Wax/Vinyl functionality and the `wax tx` feature landed at 998f62a.

## Worker ownership

1. **Core**: `components/telephony/**`, `bin/telephony`. Own policy/config/event receipt storage, public CLI, core tests and component README. No Wax/UI or hub edits.
2. **Wax integration**: `components/wax/src/wax/calls.py`, new `components/wax/src/wax/telephony.py`, `components/wax/bin/wax`, `components/wax/bin/waxd`, `components/wax/tests/calls_test.py`, new `components/wax/tests/telephony_test.py`. No worker.py/finalize/ledger changes needed for shadow; no UI/core/hub edits.
3. **Presentation**: `components/wax/src/wax/{heyma_window,tray_process,tray,desktop}.py` (tray.py limited to menu/callback), new `components/wax/src/wax/telephony_presentation.py` if useful, presentation/window tests, launcher descriptions. No core/calls/CLI/hub edits.
4. Orchestrator: this document, HeyMa hub references and AGENTS.md, integration repairs coordinated before edits.

Workers share the **same checkout and branch**; preserve each other's files. No worker switches branches, commits, pushes or creates worktrees. Orchestrator lands all owned changes to main after independent spec and quality review. All tests use isolated temporary runtime/config roots, mocked providers and clocks. Use `heavy` for sustained heavy jobs. Never put credentials into files.

## Stable local CLI boundary

Root `bin/telephony` is self-locating. CLI supports `--json` before subcommand, output machine JSON with errors distinguished from valid empty state. Runtime defaults under WAX_ROOT/var/telephony; nonsecret settings under WAX_ROOT/config/telephony.json (or an explicit test override); no generated state tracked.

- `telephony --json status`: no network, read-only, disabled if config absent. Returns `schema: heyma.telephony.status.v1`, `mode: disabled|shadow`, `control_enabled: false`, `ring_enabled: false` by default, `decisions: [...]`, and clear `readiness` explaining live enforcement unavailable. Jev-backed screening is deferred; this release uses deterministic explicit rules only and does not provision or share credentials.
- `telephony --json event`: one Telnyx envelope on stdin, bounded size; **shadow simulation only**, not a verified production webhook endpoint. In disabled mode no decision writes/inference. In shadow mode store idempotent metadata decision receipts; no provider commands. Historical/duplicate/out-of-order events never become call-control intent. Expose `verified_ingress: false` so local simulation is not mistaken for authenticated provider ingress.
- `telephony --json config [--mode disabled|shadow] [--ring on|off]`: validated atomic nonsecret config. Reject enforce/unsupported values.
- `telephony --json rules list|allow|block|remove <E164>` (list has no number); explicit conflicts rejected. Normalized number rules, unknown allowed, missing CNAM/STIR failure alone insufficient to reject. A deterministic explicit blocklist rule can yield **would_block**, never blocked.
- `telephony --json preview`: metadata/event JSON on stdin; evaluates without durable mutations. Unsupported/malformed input errors must not look like authoritative spam.
- Decisions carry `session_id`, `call_leg_id`, optional control ID, `event_id`, `observed_at`, `decision: allow|would_block`, `reason`, `mode: shadow`, `enforced: false`, evidence/provenance. Runtime receipt schema additive is okay; no private DB reads between components.

## Wax integration and snapshot

New stable command: `wax --json calls telephony [--limit N]` returns `schema: heyma.telephony.snapshot.v1`, `generated_at` UTC ISO timestamp, `mode`, `control_enabled: false`, `ring_enabled`, `health` (`ok|degraded|unavailable`), `readiness` string/list, and `calls: [...]`.

Each call: `session_id`, `direction: inbound|outbound`, `from_number`, `to_number`, `remote_name` nullable, `started_at`, `answered_at`, `ended_at`, `ring_event_at` nullable (UTC provider time of an explicit inbound `call.initiated`; never synthesized from `started_at`), `call_state: ringing|connected|ended|unknown`, `screening: {decision: unassessed|allow|would_block, reason, enforced:false}`, `processing: {state: awaiting_audio|queued|archiving|transcribing|enriching|complete|error|skipped|unknown, items:[...], completed_passes, total_passes, failed_passes}`, optional `warnings`.

**Independent spec review requirements:** `ringing`/audible notification requires an explicit fresh inbound initiation event, no answer/end and bounded age; never infer it from generic `in_progress`, CDR timestamps or unanswered history. In shadow describe it as a local call notification, not proven pre-ring screening. Local snapshot `health:ok` is not proof of ingress/webhook coverage: expose distinct ingress/assessment freshness or warnings, and missing/failed adapter receipts stay `unassessed`. Bind receipts by exact session identity (exact leg→session resolution if necessary), never phone-number/time proximity; unmatched/ambiguous records remain unassessed with a warning. Preserve provider occurrence time separately from receipt arrival; select session assessments deterministically by provider event time, and do not let duplicate/out-of-order initiation overwrite a newer decision or resurrect a settled call/ringtone.

No raw phone payloads/credential URLs. Do not infer actual blocked status from declined or shadow decisions. Lack of authoritative finalized proof never reports processing complete; every associated item/plan matters. Empty recording list is awaiting_audio/not-recorded, not successful completion.

Existing `wax calls event` remains canonical ingestion; after successful ledger update invoke local Telephony event adapter (best-effort, bounded subprocess, no caller loss if unavailable). Core disabled by default makes it a no-op. Sync/history repair does not invoke policy. `calls telephony` reads recent calls and local Telephony status via absolute CLI; no Telnyx refresh/Jev on a GUI read. `waxd` adds this projection to existing state mirror under `telephony`, with bounded/coalesced local reads (not network). No new watcher/worker/scheduler. Snapshot failure becomes unavailable/stale rather than healthy empty.

## Presentation

Add Telephony page through existing PAGES/stack/navigation, caller cards + separated call/processing states, shadow banner and reasons, and ringer control through CLI/local adapter only. CLI access may be proxied by `wax calls telephony-config` only if coordinated; simpler presentation can invoke self-locating `bin/telephony config` explicitly. No DB/provider calls from GTK.

Tray is the **only ringtone owner** using existing tick/state mirror, including with window closed. Ringer opt-in only; no new alerts at deployment by default. Shadow `would_block` stays visibly labelled **Would block — not enforced** and must never suppress an otherwise-enabled ringtone, or show actual blocked red X. Include support for future confirmed `enforced:true` blocked data only if contract extends safely; current component never emits it. Prevent historical/startup replay ringing, duplicate sounds, >bounded stale feed ringing; stop on answer/end, local silence, errors and stale snapshot. Keep Wax recording red indicator semantics unchanged. Use pure GI-free helpers and fake playback for tests. No unbounded private sleep loop; GLib presentation cadence may reuse existing tick.

## Required reviews and gates

Independent specification reviewer compares user request + confirmed default-off limitation to implementation (no production enforcement claim). Independent quality reviewer checks implementation correctness, tests, boundaries, command injection/input validation, stale/live alert behavior, concurrency, no accidental live operations. Reviewers do not mutate source. Workers repair verified findings in their own scopes. Orchestrator tests integrated source then lands commits/pushes. Full Wax regression suite, core tests, hub drift, simulated native presentation, and no-network default proofs required. True pre-ring/no-ring proof, signed HTTP ingress and archive suppression remain **not delivered/not enabled** until a later approved live-control phase; document clearly.
