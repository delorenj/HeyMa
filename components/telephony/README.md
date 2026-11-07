# Telephony: local shadow simulator

This component is **disabled by default**. It has no Telnyx command transport, no production HTTP endpoint or signature verification, no live call control, and no Jev/network inference. Enabling `shadow` only records local **unverified** event simulations: `would_block` means the call still rings, records and processes normally. No new provider resources, schedules or services are installed. Incoming calls and voicemail remain owned by the existing Wax/n8n/Telnyx paths.

The root `bin/telephony` self-locates the component. All CLI commands support `--json` before the command:

- `telephony --json status`: read-only and offline; reports mode, ringer preference, readiness and latest per-session local decision receipts. Empty receipts **do not prove webhook coverage**.
- `telephony --json config [--mode disabled|shadow] [--ring on|off]`: atomically updates nonsecret local settings. `enforce` is unsupported and rejected. Ring defaults off; setting it does not change Telnyx routing.
- `telephony --json rules list|allow|block|remove [+E164]`: explicit rules; conflicting allow/block numbers rejected. Display separators may be normalized, but country codes are never guessed.
- `telephony --json preview`: read one Telnyx-shaped JSON event from stdin and simulate assessment with no writes.
- `telephony --json event`: read one Telnyx-shaped JSON event from stdin. Disabled mode writes no receipt. Shadow mode saves at most one receipt per event ID for a fresh inbound `call.initiated` with exact session and leg identity. Historical, stale or non-initiation events cannot create a control intent. This is local unverified simulation, **not authenticated production ingress**.

Default config is `$WAX_ROOT/config/telephony.json` (or `TELEPHONY_CONFIG` for isolated tests); receipts live in `$WAX_ROOT/var/telephony/receipts.sqlite3` (state directory override: `TELEPHONY_STATE_DIR`). The runtime config and its `.lock` belong in gitignore; receipt data under ignored `var/`. Input size is limited to 64 KiB; status returns at most 50 latest-per-session decisions, selected by provider occurrence time (not receipt arrival). Every decision carries `occurred_at` and `observed_at`, exact session/leg/event identity, `verified_ingress:false`, `enforced:false`, evidence and provenance. Unknown callers, missing CNAM, failed STIR or absent inference alone are allowed. A true pre-ring/no-ring proof, signed ingress, provider fallback/rollback, and archive suppression require a separately approved live-control phase.
