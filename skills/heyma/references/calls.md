# Phone calls, voicemail, and the call log

Telnyx (+12154498437, the "DeLoNET SIP" credential connection) is the only source of call audio; the Verizon cell forwards to it. Groundwire recording and the Syncthing `telephony` folder were retired 2026-11-03. Wax owns call identity in `var/wax.db` (`calls`, `call_legs`, `call_recordings`, `call_events`); the code is `components/wax/src/wax/calls.py`. Two idempotent writers feed it:

- **Push:** Telnyx webhook → n8n workflow `9BkQCtKxdEtPWLAn` ("On Telnyx DeLoSIP Event") → the Wax Calls node (`n8n-nodes-heyma.waxCalls`) → `wax calls event` with the event JSON on stdin.
- **Pull:** `wax calls sync`, every 10 minutes via Candybar job `wax-calls-sync` (`*/10 * * * *`, id `01a1a365-819d-72ec-b518-f8de3976dead`, enabled, created 2026-11-03; ~300 invocations, 1 failure as of 2026-11-05).

The `calls` row is always recomputed by `derive()` from legs, recordings and events; nothing patches it in place.

## Telephony page and shadow rules

See [telephony](telephony.md) for the native page, `wax --json calls telephony`, local ringer and the default-off/shadow component. Shadow **would-block is not blocked**: it never changes Telnyx routing, normal recording or Wax audio processing. The existing direct SIP route is unchanged; pre-ring rejection requires a later explicitly approved cutover.

## The CLI

| Command | What it does |
|---|---|
| `wax calls sync [--since 2d] [--no-fetch]` | Pull CDRs, recordings and webhook deliveries from the Telnyx API; derives sessions; fetches pending voicemails unless `--no-fetch`. `--since` accepts `2d`/`6h`/`90m`/ISO. Moves `meta.calls.last_sync`. Never announces. |
| `wax calls event [--file F] [--no-fetch]` | Ingest one Telnyx webhook event (JSON on stdin; exits 2 with `error: stdin is not JSON` on bad input). Announces a newly settled call exactly once. |
| `wax calls list [--limit N] [--outcome O]` | Recent calls. Outcomes: `answered`, `voicemail`, `missed`, `declined`, `no_answer`, `failed`. Excludes RecordVerb second copies. |
| `wax calls show <ref>` | One call by session id, leg id, item id, or file name. |
| `wax calls backfill [--dry-run] [--min-seconds 3]` | Fetch trunk recordings that predate custom S3 storage (attached 2026-10-15T21:18Z): rows with no `object_name` and no `file_name`. Fetches the presigned URL from `GET /recordings/<id>` and writes `inbox/telnyx-<day>-<leg>-<epoch>.mp3`. Skips stubs under `--min-seconds`. |

Errors print `error: ... (<code>)` and exit 2. API codes: `no_api_key`, `telnyx_auth_failed` (401/403), `telnyx_http_error`, `telnyx_unreachable`, `telnyx_bad_response`, `not_an_event`.

## Config

| Knob | Default |
|---|---|
| `WAX_TELNYX_API` | `https://api.telnyx.com/v2` |
| `WAX_TELNYX_API_KEY_OP` | `op://DeLoSecrets/74k7nz3xkryqns4lpf65a323ue/credential` |
| `WAX_TELNYX_LINE`, `WAX_TELNYX_CELL`, `WAX_CALLS_ME` | the DID, the Verizon cell, "Me" |
| `WAX_TELNYX_VOICEMAIL_HOSTS` | `voice-mail-prod.s3.us-east-1.amazonaws.com` (https-only allowlist) |

Note (2026-11-05): the `74k7nz3x…` item above is the working key — the 18:00 sync succeeded with no override. An older memory claiming it was truncated and 401ing (with a fallback at item `32l3qtxf…`) is stale.

## The schedule

`wax-calls-sync.service` (template `components/wax/deploy/systemd/user/wax-calls-sync.service`) is a static oneshot — **no `.timer`, by design**; Candybar is the clock. It runs `bin/wax --json calls sync --since 2d` with `TimeoutStartSec=240`, `Slice=batch.slice`, `Nice=10`. Manifest: `components/wax/deploy/candybar/wax-calls-sync/candybar.toml`.

Discrepancy (2026-11-05, reported, not fixed): the manifest carries `enabled = false` (the register-disabled-first state) while the Candybar registry has the job **enabled** and it runs every 10 minutes. If the manifest is ever re-registered from scratch it will come up disabled.

## How a call becomes a row

`derive()` computes the outcome from all evidence:

- `answered` — any real leg answered (completed or with `call_sec`), or a `call.answered` event (outbound, or inbound without voicemail).
- else `voicemail` — a `calls.voicemail.completed` event, or a voicemail leg (`happy_voicemail_deposit`) with `call_sec`.
- else inbound: `declined` if a leg's `hangup_details` ends with "refuse", otherwise `missed`.
- else outbound: `no_answer` for ORIGINATOR_CANCEL / NO_ANSWER / USER_BUSY / NORMAL_CLEARING, otherwise `failed`.
- `in_progress` — no legs, no hangup, no end.

Remote names come from CNAM filtered by `person_name()`: spam/unknown values, all-caps "CITY ST" place names and digit strings are dropped. An outbound call reuses a known name from an earlier inbound call from the same number. The Diversion SIP header sets `forwarded_from` / `forward_reason`. The Verizon forward changed from conditional (no-answer/user-busy) to **unconditional** on 2026-11-05T01:11Z — older notes saying "conditional" are stale.

Feet-era (TeXML, before 2026-10-25) RecordVerb second copies have a 16-digit microsecond epoch in the file name and are never the master recording.

## Voicemail

A voicemail is a `calls.voicemail.completed` event carrying a 7-day WAV link on `voice-mail-prod.s3.us-east-1.amazonaws.com`. It is fetched once per event (under `var/calls-voicemail.lock`), staged to `inbox/.staging/telnyx-vm-*.part`, then moved to `inbox/telnyx-vm-<UTC date>-<session>-<epoch>.wav`. Fetch errors persist in `call_recordings.fetch_error`:

| Code | Meaning | Retry |
|---|---|---|
| `voicemail_empty` | under 64 bytes — a caller who hung up on the beep | **Final, never retried** |
| `voicemail_untrusted_url` | host not on the allowlist | manual |
| `voicemail_expired` | HTTP 403 — the 7-day link died | final in practice |
| `voicemail_http_error` | other HTTP error | next sync retries |
| `voicemail_unreachable` | network failure | next sync retries |

## Announcement

A call is announced exactly once, when it settles: an outbound call at hangup; an answered inbound call at the hangup after the answer; a voicemail when the message lands. Missed and declined calls are never announced. `wax calls event` returns `ended: true` on settlement; the n8n workflow turns that into `bloodbank.telephony.call.ended` (ordering key `call:<session>`) plus one ntfy line on topic `telephony`. The claim is `calls.announced_at`, taken with a conditional `UPDATE ... WHERE announced_at IS NULL`.

Two traps:

- **The sync path never announces.** Only `wax calls event` does. If the webhook path was down (a broken working tree broke it 2026-11-05T18:50Z with `NameError: name 'contextlib' is not defined` in `failures.py`), the sync recovers the rows but those calls stay unannounced.
- The `announced_at` migration backfilled it to `updated_at` for already-ended rows, so `announced_at` means "claimed", not necessarily "announced".

Ledger state 2026-11-05: 140 calls since 2026-10-05; outcomes answered 98 (72 in / 26 out), declined 11, failed 14, missed 10, no_answer 3, voicemail 4. 133 rows have `announced_at`.

## Audio to call

The `call-metadata` pass (see [passes/catalog](passes/catalog.md)) joins a `telnyx-*` inbox item to its call and stamps 14 `call-*` frontmatter keys; unknown values are absent. Join order for a session: `call_legs`, then recordings, then events. A row with no CDR or no `ended_at` is provisional and triggers a bounded refetch (-10 min to +4 h, no voicemail fetch, no sync advance). Dual-channel (non-voicemail) call audio is transcribed per channel and labeled by party — outbound is (Me, other), inbound is (other, Me); the inbound mapping is inferred and not yet verified against a real answered inbound call (code comment, 2026-11-02) — see [transcription](transcription.md).

## Runbook

Start with `wax calls list` and `wax calls show <ref>`; `wax status` has a calls line (`ok synced N min ago`, or `error` when voicemails are unfetched, call recordings never reached the inbox, or the last sync is over 3 h old), and `wax doctor` probes "call log synced" (WARN > 30 min, FAIL > 3 h or never), "voicemails fetched", "call recordings delivered" (FAIL when a trunk recording ≥ 3 s that ended 15+ min ago in the last 30 d has no `telnyx-*` item and no backfill `file_name`; with no S3 object it is named `<leg> (no S3 object)` and the fix is `wax calls backfill`), and "telnyx api key" (`GET /v2/balance`).

| Symptom | Likely cause | Do this |
|---|---|---|
| Calls show `[1 audio]` in `wax calls list` but no transcript; doctor FAILs "call recordings delivered" | S3 intake stopped. On 2026-11-07 the Candybar job `telnyx-s3-intake` was paused from the Holocene Candybar page and calls went untranscribed for two days | `candybar show telnyx-s3-intake` (state `paused`?), `candybar resume telnyx-s3-intake`, then `bin/telnyx-s3-intake status`. The backlog delivers in one run. See [intake](intake.md). A finding named `<leg> (no S3 object)` means Telnyx never wrote it to our bucket: `wax calls backfill --dry-run`, then without it |
| A missed call shows nothing | Working as designed: missed and declined calls are rows only, never announced, no ntfy | `wax calls list --outcome missed` |
| A voicemail was not fetched | Check `fetch_error`: `voicemail_empty` is final; `voicemail_expired` means the 7-day link died | Otherwise `wax calls sync --since 7d` retries the fetch. Doctor hint: the Telnyx link dies 7 days after the call, so sync promptly |
| A call was not announced | The webhook path missed it (n8n down, workflow error, broken working tree) and only the sync — which never announces — saw it | Confirm in the n8n `9BkQCtKxdEtPWLAn` execution log. Rows recover via `wax calls sync`; the announcement does not. Prevention is the only fix |
| `wax status` calls line says `error` / doctor FAILs "call log synced" | The Candybar job is failing or the sync is stale | `systemctl --user start wax-calls-sync.service`; `candybar show wax-calls-sync -n 5`; then `wax calls sync --since 2d` by hand and read the error code |
| call-metadata reports `call_not_found` | The session isn't in the ledger yet (webhook missed it and the 10-min sync hasn't run), or the filename didn't match `telnyx-<date>-<uuid>-<epoch>` | `wax calls sync --since 2d`, then `wax ep run call-metadata <item>` — or let the sweep retry it. `wax calls show <item-id>` shows what the join sees |
| Old trunk recordings missing from inbox | They predate custom S3 storage (2026-10-15T21:18Z) and were never fetched | `wax calls backfill --dry-run`, then without `--dry-run` (2026-11-05: 10 candidates, 2 stubs skipped, 8 fetched) |
| Telnyx API errors (`telnyx_auth_failed`, `no_api_key`) | Key env or the `op://` item | Confirm `WAX_TELNYX_API_KEY_OP` resolves: `op read "op://DeLoSecrets/74k7nz3xkryqns4lpf65a323ue/credential"`. Never paste the value anywhere |

To force a full resync of a window: `wax calls sync --since 7d` (CDRs, recordings and webhook deliveries are all re-pulled; ingestion is idempotent by event/recording/leg id).
