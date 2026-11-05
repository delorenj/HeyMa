# n8n-workflows — released n8n workflows for HeyMa

Each script here owns one live n8n workflow: it diffs the desired graph against
the live one, prints `unchanged` / `would change`, and only mutates n8n with
`--apply` (PUT, activate if needed, verify). **Dry run is the default.** Run a
script from this directory (`python3 telnyx_calls.py`) — it imports
`release.py`, so isolated mode (`python3 -I`) breaks it.

The boundary is unchanged: n8n invokes the absolute `wax` CLI and owns no
pipeline state, never schedules, never watches files. See
`components/wax/docs/CONTROL-SURFACE.md`.

| Workflow | n8n id | Script | Status (2026-11-05) |
|---|---|---|---|
| On Telnyx DeLoSIP Event | `9BkQCtKxdEtPWLAn` | `telnyx_calls.py` | Live and current (`unchanged`) |
| Transcription Notifications | `XHWRrrYCzu4ifdS7` | `release.py` | **Not applied** — live is still the pre-release shape |

## On Telnyx DeLoSIP Event (`telnyx_calls.py`)

Webhook → Wax Calls node (`event`) → IF `$json.ended` → Bloodbank
`bloodbank.telephony.call.ended` (orderingKey `call:<session>`) + one ntfy line
on topic `telephony`. This is the push half of the call log; the pull half is
the Candybar job `wax-calls-sync` (`wax calls sync` every 10 minutes). Node ids
are pinned in the script so re-applies are stable.

## Transcription Notifications (`release.py`)

Intended shape: formatters (`format-completion.js`, `format-intake.js`) fed by
Bloodbank triggers (`audio.transcription.completed`,
`audio.intake.detected`), publishing to `https://ntfy.delo.sh` with the "Wax
ntfy JSON publisher" header-auth credential. Formatter tests:
`node --test format-completion.test.js format-intake.test.js`.

As of 2026-11-05 the release has **not** been applied: the live workflow is the
4-node pre-release shape (file.received → "Transcription Started";
transcription.completed → "Transcription Complete"), and the dry run refuses
with "workflow changed since review" because the pinned expected versions no
longer match. Spec:
`_bmad-output/implementation-artifacts/spec-final-transcription-notifications.md`.
Consequence until it lands: nothing relays `audio.intake.detected` to ntfy.

## Credentials

n8n API key and the ntfy credential are `op://` references inside the scripts.
Never paste a value into workflow JSON, this repo, or a ticket.

## Verifying what is actually live

Every n8n workflow is exported every 15 minutes to
`~/code/delonet-n8n-workflows/workflows/<slug>--<id>.json` (Candybar job
`delonet-n8n-workflows-sync`). Diff those exports, not the editor's memory.
