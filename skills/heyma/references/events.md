# Events

Wax publishes Bloodbank events through a durable outbox. Publishing is fail-open: a NATS outage never breaks a recording. Code: `components/wax/src/wax/events.py` (outbox, envelopes, drain), `passes.py` (pass-declared events, dead-letter), `finalize.py` (`publish_pass_events`). NATS is `WAX_NATS_HOST`, default `127.0.0.1:4222`.

## Delivery contract

- A row is written to the `outbox` table of `var/wax.db` in the same transaction as the state change.
- The `wax-outbox` thread in waxd drains every 10 s. A row is marked published **only on a JetStream PubAck**; "the socket took the bytes" is not delivery.
- Backlog: `wax events` (or `wax events backlog`) prints `{"backlog": N}`; `wax events drain` publishes now. `wax status` and the tray (yellow above 50) surface a backlog; `OUTBOX_STALE_S` is 600 s.
- Grammar: type has 4 tokens `bloodbank.<domain>.<entity>.<action>`, subject has 5 `bloodbank.<evt|cmd|rpy>.<domain>.<entity>.<action>`. There is no `v1` token. Legacy `evt.v1.*` rows stopped on 2026-08-27 but remain in the outbox history. `subject_for()` raises on a 5-token type.
- Candystore subscribes to `bloodbank.evt.>` (wide, version-free). Commands live in a short-lived workqueue, so every command is also mirrored as `task.requested` carrying the same `command_id` (uuid5 in `WAX_NS` from item, slug, attempt). That mirror is how you find the command that invoked a pass.

## What Wax actually emits

Read from the live outbox on 2026-11-05 (subjects `bloodbank.evt.audio.*` unless noted):

| Event | Fires when |
|---|---|
| `session.started`, `session.ended` | Capture begins and ends |
| `file.recorded` | Item enters the ledger as `pending` |
| `file.sent` | Item is `archived` (S3 verified) |
| `file.closed` | Item is `complete` |
| `status.updated` | A state-machine transition |
| `transcription.completed` | Every planned pass is done (plan-gated, the summary fact) |
| `transcription.failed` | Item goes `suspect` or `failed` |
| `task.requested`, `task.started`, `task.completed`, `task.failed`, `task.skipped` | One pass run; paired with `bloodbank.cmd.audio.task.start` |
| `task.exhausted` | A pass used its attempts. Once per (item, slug, version): the dead-letter signal |

Not emitted anywhere in the code, although WAX-DESIGN lists them: `session.failed`, `session.canceled`, `transcription.started`, `heartbeat.recorded`. Do not subscribe expecting them. `bloodbank.audio.intake.detected` is defined and relayed but currently silent, because its only producer, `ticketable`, is disabled (see [passes/catalog](passes/catalog.md)).

## Pass-declared events

A pass may return `events` in its result; its YAML must list them under `emits`, and the data must satisfy the Bloodbank schema. The runner validates against `~/code/33GOD/bloodbank/schemas/bloodbank/audio/<type>.json` (override with `WAX_BLOODBANK_SCHEMAS`) and fails closed. That directory holds `task.exhausted`, `intake.detected`, `transcription.*` and others, but not the session, file or status schemas. A completed pass's events publish as soon as its own `requires` subgraph completes (`finalize.publish_pass_events()`), not behind slow siblings. Document runs reject `events`. `wax doctor` has a probe for unpublished pass events.

## Consumers

| Consumer | Takes | Does |
|---|---|---|
| n8n Transcription Notifications (`XHWRrrYCzu4ifdS7`) | `transcription.completed`, `intake.detected` | ntfy topic `audio` |
| `bloodbank-ntfy-router` container | `task.exhausted` | Routes the dead-letter to ntfy |
| Candystore | `bloodbank.evt.>` | Durable history |
| n8n Telnyx workflow (`9BkQCtKxdEtPWLAn`) | webhook, not Bloodbank | Publishes `bloodbank.telephony.call.ended` plus ntfy topic `telephony`. Wax does not emit it; see [calls](calls.md) |

Order matters: `transcription.completed` must precede `bloodbank.audio.intake.detected`.

## Adding or changing an event

1. Add the schema in the Bloodbank repo first; load the `bloodbank-integration` skill for subjects, validators and `ALLOWED_ENTITIES`.
2. For a pass event, list it under `emits` in the pass YAML and keep the result envelope valid (see [create-enrichment-pass](../subskills/create-enrichment-pass/SUBSKILL.md)).
3. Restart waxd, then check `wax events` for backlog and the outbox for `last_error`.

Inspect the outbox read-only: `sqlite3 "file:$HOME/HeyMa/var/wax.db?mode=ro" "select subject,count(*) from outbox group by 1"`. Unpublished rows have `published_at IS NULL AND suppressed_reason IS NULL`.

Related: [failures](failures.md) for how exhausted passes become tickets, [system-map](system-map.md).
