# Telephony: default-off / shadow component

Telephony is a HeyMa component at `components/telephony/`, separate from Wax’s long-form audio worker and Vinyl’s live dictation. Its first release is **observation and shadow policy only**. It does not hang up callers, change Telnyx routing or recording, suppress audio processing, buy resources, or activate workflows. Enforce mode is unavailable.

**Current phone behavior stays intact.** The main number still uses DeLoNET SIP. A desktop indicator cannot prevent Groundwire ringing on that already-bridging route. True pre-ring rejection requires a separately approved and tested Voice API front door; it is not delivered by this release.

## Policy and truthfulness

- Default is `disabled`; optional `shadow` records what policy **would** do.
- Unknown/ambiguous callers are allowed. Missing CNAM or failed STIR/SHAKEN alone is not proof of spam.
- Explicit allow/block rules are cheap deterministic evidence. Conflicting rules are refused.
- **Would block is not blocked.** Shadow matches retain ordinary ringing, recording, ASR and enrichment; the UI must not show an actual-rejection red X or silently mute them.
- This release has no live Jev inference path or dedicated credential provisioning; shadow policy uses explicit number rules only. Jev-backed reputation judgment remains future work. A future timeout, missing key or malformed judgment must not become a spam verdict.
- Local event simulation is **unverified ingress**, not a public signed webhook endpoint. Do not expose it to the network or describe its receipts as proof that the provider blocked a call.

## Operator surface

Use this checkout’s absolute launchers, never a PATH-selected stale copy:

| Command | Purpose |
|---|---|
| `bin/telephony --json status` | Read-only component mode/readiness and shadow receipts; absent config means disabled |
| `bin/telephony --json config --mode shadow` | Opt into local shadow decisions, not call control |
| `bin/telephony --json config --mode disabled` | Disable policy observation |
| `bin/telephony --json config --ring on` | Opt into desktop ringing; off by default |
| `bin/telephony --json config --ring off` | Disable desktop ringing |
| `bin/telephony --json rules list` | Inspect explicit number rules |
| `bin/telephony --json rules allow +12125550123` | Trust an E.164 number |
| `bin/telephony --json rules block +12125550123` | Mark a number as would-block in shadow |
| `bin/telephony --json rules remove +12125550123` | Remove its explicit rule |
| `bin/telephony --json preview` | Evaluate JSON on stdin without persisting a decision |
| `bin/telephony --json event` | Local shadow event simulation on stdin; never issues provider commands |
| `bin/telephony alert --targets GLOB --state-dir ABSOLUTE_PATH status` | Read offline target-watch identity bindings, policy tone and execution capability; 1Password provider is not configured |
| `bin/telephony alert --targets GLOB --state-dir ABSOLUTE_PATH enable UUID_OR_ALIAS` | Set private offline eligibility for an identity bound by 1Password item UUID; not live alert gating |
| `bin/telephony alert --targets GLOB --state-dir ABSOLUTE_PATH disable UUID_OR_ALIAS` | Clear offline eligibility |
| `bin/telephony alert --targets GLOB --state-dir ABSOLUTE_PATH alias add UUID OPERATOR_SLUG` | Bind a unique private operator alias (remove reverses it); never matches display names or family records |
| `bin/telephony alert --targets GLOB --state-dir ABSOLUTE_PATH tone UUID_OR_ALIAS RINGTONE_ID` | Store a safe opaque ringtone ID, policy only; `default` resets fallback, phone playback not implemented |
| `bin/telephony alert --targets GLOB --state-dir ABSOLUTE_PATH preview email ADDRESS` | Offline canonical target-watch matcher preview with selected tone; also accepts e164, label or platform_user; ambiguous matches refused as eligible |
| `wax --json telephony alert --targets GLOB --state-dir ABSOLUTE_PATH status` | Canonical Wax facade; `--json` and explicit path flags work before or after actions |
| `wax --json calls telephony` | Joined call/screening/processing projection for the native page |
| `heyma --page telephony` | Open the native page, including in an already-open window |

Do not send synthetic events to the live Wax call ledger to test the screen. Use isolated temporary `WAX_ROOT`/component config paths and mocked providers; the component tests illustrate that boundary.

Alert Mode is a separate **offline** slice owned by Bloodbank target-watch (`services/target-watch/target_watch/alert_mode.py`), not Telephony's shadow spam rules. Its CLI requires an explicit target TOML glob and absolute isolated state directory; it never reads family contacts, invokes `op`, subscribes to events, or alters live Jim/all-call behavior. A target may declare `target.identity_ref` as a 1Password item UUID while retaining its existing handles and sink unchanged. The private policy is `STATE/alert-mode/policy.json` (0700 directory, 0600 file). Unique operator aliases are set explicitly with `alias add UUID SLUG`, stored only in this private policy and never inferred from names; unknown/ambiguous aliases fail. A validated lowercase opaque ringtone ID may be stored per identity, with `default` fallback; status and preview expose the selected policy tone separately from `per_identity_phone_ringtone: not_implemented`. `--projection-cache ABSOLUTE_PATH` optionally reads only operator-approved `UUID.json` files in a 0700 directory, with 0600 files shaped `{"identity_ref":"UUID","handles":{"email":["..."]}}`. No contact-fetch, 1Password lookup or cache writer exists. Without the explicit flag projection is `not_configured`; matching SMS/MMS/RCS/calls is an offline canonical handle preview, not verified provider ingress or a per-contact Samsung ringtone. No consumer loads this policy. Do not infer live eligibility from `enabled`. `wax calls alert-mode` remains a legacy CLI alias; use `wax telephony alert` for new commands.

## Runtime and ownership

Nonsecret settings default to `WAX_ROOT/config/telephony.json` (operator-local and gitignored); runtime receipts live under `WAX_ROOT/var/telephony/`. Component documentation defines exact overrides. Generated state is never committed. The existing `wax calls event` path remains canonical call ingestion; its bounded local adapter may hand metadata to Telephony after ledger ingestion. Adapter failure must not lose the call. History/CDR sync never creates live control intent.

The page reads a CLI projection; the existing daemon may mirror it for the existing tray. No GUI database writes, provider secrets, extra watcher or recurring clock. The **tray is the only ringtone owner**, so a closed main window does not create another sound source. New daemon/tray code requires an explicit reload/restart rollout; merely committing it is not proof that the live process loaded it.

## What the page means

Call state and processing state are separate:

- Call: ringing, connected, ended, unknown; shadow reason shown separately.
- Audio: awaiting audio, queued, archiving, transcribing, enriching, complete, error or skipped.
- A hangup is not a finished transcript. A transcript or parked `complete` audio row is not proof all enrichment succeeded. Completion requires the durable plan/finalizer evidence for every associated item.
- Missing recordings, failed passes and stale/unavailable snapshots remain visible.
- Opted-in ringing stops on answer/end, local silence, stale feed or errors. Historical/replayed calls must not start a new ringtone.

## Before future enforcement

Require signed raw-body provider ingress; idempotent control receipts; a proven parked-leg → registered SIP endpoint handoff without dialing back into the public DID; original caller ID, voicemail and dual-channel recordings; and a tested provider-outage fallback/rollback. Show the exact old/new main-DID assignment and obtain explicit approval. Do not repurpose the existing discard-webhook probe applications.

Only then add archive-first suppression for **actually blocked** calls: preserve any received audio, verify backup before parking it, and skip ASR/enrichment. Shadow decisions must never use that gate.

Related: [calls](calls.md), [capture](capture.md), [config-and-deploy](config-and-deploy.md).
