# Telephony shadow delivery status

## Scope
Outcome: a default-off Telephony component, local shadow rules/receipts, joined call-processing snapshot, and native page with optional tray-owned notifications.
Repos: HeyMa only; Vinyl, Bloodbank, n8n releases and live Telnyx configuration unchanged.
Authority: Telephony owns local shadow policy; Wax remains sole audio/call ledger owner; tray owns sound; GTK window only presents/requests CLI operations.
Out of scope: signed production ingress, Jev live inference, actual hangups/routes, archive suppression, main-DID cutover and new billable resources.
Starting main: `998f62a`.

## Coordinated implementation
Three delegated workers implemented nonoverlapping core, Wax projection, and native presentation scopes. Independent specification and quality reviewers inspected the integrated source. No worker committed/pushed or restarted live processes; orchestrator owns integration and landing.

## Verification
- Final post-repair integrated regression: **935 tests passed** (Wax + Telephony), 56.49 seconds, system Python under `heavy`.
- Isolated native GTK smoke: actual window launched in a private D-Bus/Xvfb session, navigated Settings → Telephony, rendered a synthetic caller and truthful shadow banner. A synthetic answer then updated the actual widget labels to Connected / Transcribing without manual refresh. Screenshot inspected; not a live caller or live desktop interaction. Headless portal/GPU warnings did not prevent rendering.
- Isolated public CLI proof: absent config remains absent after status; `enforce` returns exit 2 without writing; explicit block rule previews `would_block`; duplicate event has one stored receipt. No provider/inference requests.
- Runtime readback: disabled, ringer off, control disabled, no shadow receipts.
- Hub lint and diff whitespace checks clean.

## Review repairs verified
- Fork-aware call lifecycle: a sibling SIP hangup no longer ends another live leg; settled replay remains ended.
- Exact distinct-recording completion proof and CDR ownership precedence.
- Two-second coalesced page-visible refresh, canceled on navigation/close with stale-result protection.
- Duplicate test method renamed so both tests collect; test AST duplicate check passed.
- Core replay flag from insert result, not receipt inequality; chronological selection across mixed ISO precision, including legacy receipts.
- Coalesced asynchronous daemon projection keeps observation off the one-second main tick; blocked-reader test proves a single in-flight read.
- Independent specification recheck closed all reported findings; quality findings have deterministic regression coverage.

Verification is complete; this delivery is code-only/default-off. No deployment/cutover is claimed. Normal recording/transcription remains unchanged. No live daemon/tray restart occurred. Advisor consultation was unavailable through the tool surface and is not claimed. The orchestrator lands this slice on root main; Git records its exact publication commit.
