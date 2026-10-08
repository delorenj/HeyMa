---
title: Final Wax completion and useful transcription notifications
type: feature
created: '2026-10-01'
status: in-progress
baseline_commit: a3fc8538c5f56fb9fd1d56ad7cf88bdc67176f68
route: dispatch
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

Wax currently emits transcription completion before enrichment or slugging, with an invalid, incomplete payload. The active n8n notification workflow sends a started message on completion and cannot publish to its topic. Deliver one trustworthy final completion notification, with effective metadata from all enrichment passes and native full-transcript clipboard copying.

## Boundaries & Constraints

Always: Wax owns archive, ASR, enrichment, finalization, and the durable outbox. Preserve all source audio and existing human metadata. Completion requires every applicable automatic pass to succeed or intentionally skip; failures remain visible/retryable and do not block independent passes or safe audio parking. User approved combining existing source WIP, native-copy payload-limit adjustment, live classification routing, and implementation on October 1.

Never: Move pipeline ownership into n8n, delete audio, invent classification or missing metadata, silently truncate a full-copy action, commit secrets/generated/runtime output, or bulk-backfill historical recordings. No unapproved billing-cap change.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Complete | Verified archive, final note, all automatic passes successful/skipped | One schema-valid completion after final filename/metadata/body and parking; title, summary, slug, classification, duration, S3 references, transcript and dynamic metadata/pass results | Durable transactional outbox receipt |
| Pass failure | One EP fails | Independent EPs continue; audio parks; completion withheld | Persist reason; retry/sweep may later finalize |
| Conditional | Classifier returns monolog/meeting/other | Monolog skips diarization; meeting/other run CUDA Sortformer | Classifier failure conservatively runs diarization, but withholds completion |
| Retry | Same result or repaired withheld item | No duplicate unchanged completion; repaired item finalizes | Stable receipt/event identity |
| Native copy | Complete transcript within transport/client policy | JSON action value is entire unchanged transcript body | Oversize produces truthful warning, never a mislabeled truncated copy |
| Desktop absent/lost | Missing display or GTK child crash | Pipeline/status/outbox remain running; tray unavailable/retries | No native GTK in pipeline parent |

</frozen-after-approval>

## Code Map

- `components/wax/src/wax/ledger.py:347`: premature transition event; SQLite schema and stage health.
- `components/wax/src/wax/passes.py`: registry, result application, dependency rejection, auto/manual runs.
- `components/wax/src/wax/worker.py:283`: backup-first processing, safe parking, selected retries; preserve existing dropoff import.
- `components/wax/src/wax/transcribe_adapter.py:199`, `scripts/transcribe.py:654`: ASR/diarization coupling; persist timed ASR for later EP without repeating Whisper.
- `components/wax/config/passes.d/bin/title-slug`: reusable hosted provider logic; new portable classifier follows same grounded/no-clobber contract.
- `components/wax/bin/wax`: CLI/sweep entry points need shared finalization.
- `components/wax/bin/waxd`, `src/wax/tray.py`, `deploy/systemd/user/waxd.service`: GTK startup/session coupling.
- `components/n8n-workflows/`: durable notification workflow/formatter/tests; live workflow `XHWRrrYCzu4ifdS7`, preserve trigger IDs and received branch.
- `/home/delorenj/code/33GOD/bloodbank/schemas/bloodbank/audio/transcription.completed.json`: canonical wire contract, additive optional metadata fields.
- `/home/delorenj/docker/stacks/monitoring/ntfy/config/server.yml`: supported message-size-limit; publisher/topic ACL currently denies audio.

## Tasks & Acceptance

**Execution:**
- [ ] `ledger.py`, `passes.py`, `events.py`, new finalizer, `worker.py`, `bin/wax` — persist pass outputs/plan, enforce validated dependency/condition ordering, atomic mutation, serialize item operations, remove premature emission, finalize after complete success, include every future EP dynamically.
- [ ] `scripts/transcribe.py`, `transcribe_adapter.py`, new diarization adapter/registry/executable — durable timed ASR, conditional post-ASR CUDA diarization, bounded compare-and-replace body intent, accurate health.
- [ ] `config/passes.d/` — add grounded monolog/meeting/other classifier; gateway migration only after cap decision, no direct inference fallback after cutover.
- [ ] `waxd`, tray integration, unit/tests — isolate GTK from pipeline lifecycle; restore installed daemon after verification.
- [ ] `components/n8n-workflows/` — validate/format final events, succinct title with title/classification/audio length, summary body, native copy action, JSON publish, retry/error visibility, safe credential reference/export; deploy live workflow and fix audio ACL.
- [ ] ntfy configuration — set supported `message-size-limit: 1M`, recreate affected service; size-gate actual encoded request and conservatively bounded clipboard payload.
- [ ] Existing source WIP — repair verified Syncthing acknowledgement/name-validation and remote-microphone fallback defects; retain tests and source, exclude installer/cache/runtime churn.
- [ ] Verification/publication — isolated tests, schema contract, lint/typecheck baseline, live transport/ntfy proof; commit/push affected owners and parent Bloodbank pin.

**Acceptance Criteria:**
- Given a recording, when its final automatic enrichment and slug rename finish successfully, then completion is emitted afterwards with the actual final path and aggregate effective metadata, including an unknown future pass.
- Given failed/running/missing planned EP state, when finalization runs, then no completion is emitted; given a successful repair, then exactly one completion is enqueued.
- Given monolog, meeting, other, or classifier failure, when the pipeline schedules diarization, then it skips only monolog (or explicit disablement), otherwise runs CUDA, preserving ASR and independent EPs.
- Given a final event, when n8n publishes, then ntfy title includes title/classification/audio duration and summary is concise, with exact full transcript in native copy for supported sizes.
- Given failed publication or oversize copy, when the workflow handles it, then it reports failure/limitation truthfully rather than successful full-copy delivery.
- Given no desktop, when Wax starts or the tray crashes, then daemon status, worker and outbox remain available.

## Implementation Notes

User explicitly approved the shared paid OpenRouter pool (instead of Wax's old $1/day upstream cap). Provision a dedicated scoped Wax gateway token and migrate title/classifier to `https://api.automaticai.io/v1`, model `automaticai/openrouter/google/gemini-3.7-flash`, no direct-provider fallback.

Implementation ownership: the implementation agent executes this spec sequentially, delegating no overlapping edits. It may use independent research helpers. Follow tool/developer rules: no added comments, no raw secret files, read before editing, lint/typecheck commands when available. The requested new implementation/test files and this spec are authorized. Finish isolated implementation and tests first: during the implementation handoff do not deploy, restart live services, stage, commit, or push. Prepare credential/token references and deployment tooling safely; the coordinating agent will release live deployment/publication after verification. The user approved finishing the live workflow and combining source WIP. Preserve the existing unpublished legacy completion in a suppressed/auditable state and replace only after verifying its final artifacts; do not delete event history. Completion is once per item/processing plan; registry additions/new results are namespaced, not arbitrary top-level collisions. S3 URL uses actual configured alias endpoint/key, and canonical S3 URI remains available; do not make private audio public. Phone/browser copy acceptance is an honest manual limitation, not a reason to invent proof.

Gateway paid route verified: `automaticai/openrouter/google/gemini-3.7-flash`; user approved shared paid pool. Gateway tooling is in `/home/delorenj/docker/stacks/ai/newapi/ops/gateway-tokens.py` (resolve actual location before use); prior acceptance-only reference `op://DeLoSecrets/yeurk5dpqkaarspvsn3cjtmkki/openrouter-acceptance` must not become the consumer credential. New token name `heyma-wax-inference`, allow only that model, persist returned UUID reference. Explicit User-Agent avoids gateway Cloudflare 403. Reuse title provider functions without duplicate network/secret logic for classifier. Do not restart unrelated gateway services.

Live n8n API key reference: `op://DeLoSecrets/5pyy6ct5pi7eaz436g2qkn6pjq/mfiofpxjhlpubqwdzop3ty3eq4`; ntfy n8n token `op://DeLoSecrets/y2bcg7okwrpucnecdswlpcviti/credential`. Existing Ntfy credential ID `CMsMifZmcZA2DGcM` is node-specific; use native Header Auth for JSON POST, resolve credentials in memory. Current workflow active version `9e2a1c16-fdd8-4c8c-a1c0-09e32210fd61`, branches `New Audio File` and `Transcription Complete`; both currently topic audio, preserve received trigger. ntfy user n8n only has lifecycle ACL; add audio grant without removing it, preserve private deny-all model. Durable ntfy owner `/home/delorenj/docker` repo, schema owner Bloodbank is a submodule of `/home/delorenj/code/33GOD` (land parent pin without unrelated changes). ntfy 2.22 supports only `message-size-limit`, JSON request must be less than twice this limit; native copy action `{action: copy,label: Copy full transcript,value: <full body>,clear: false}`. Start clipboard policy at <=256KiB UTF-8 and <=262144 UTF-16 code units, test Unicode encoded boundaries, and emit truthful no-copy warning when exceeded. NATS max_payload 1MiB: aggregate envelope must be bounded, never silently lose/truncate canonical transcript; a huge artifact may use truthful URI/no-copy path.

ntfy Android app >=1.23.0 and web app support copy; browser desktop notification buttons do not. Real-device clipboard acceptance remains a manual check. Existing outbox has one unpublished legacy completion; preserve its evidence and do not blindly publish it as finalized.

## Spec Change Log

## Review Triage Log

## Verification

- `python3 -m pytest tests` from `components/wax`.
- `python3 -m unittest discover -s tests -p test_transcribe_contract.py` from root.
- Node built-in formatter tests; isolated existing remote-audio and Syncthing tests.
- `mise run smoketest:schemas` from Bloodbank.
- Python lint/typecheck via pinned tools, report pre-existing baseline failures; `git diff --check`.
- Full pipeline doctor after daemon install; canonical envelope validation and durable Candystore arrival; authenticated ntfy publish/read-back and n8n execution evidence. Phone/browser copy tested manually by operator if unavailable.
