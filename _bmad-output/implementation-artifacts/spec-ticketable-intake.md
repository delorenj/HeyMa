---
title: Project extraction (Jev) and ticketable intake events
type: feature
created: '2026-10-08'
status: in-progress
source: voice memo 2026_10_08_10_14_11.mp3 (wax item 5eea3d5cd205bb84), and 2026-09-30 / 2026-10-07 memos
---

## Intent

1. **project-extraction** pass: associate every transcript with the pjangler
   projects it talks about, choosing from the pjangler registry, decided by
   TypeSafe's Jev decision model (`typesafe/jev-1.13`). Frontmatter carries the
   pjangler project ID(s), not just names.
2. **ticketable** pass: after `title-slug` (final filename) and
   `project-extraction`, extract ticketable tasks. One object per ticket:
   `{project_id, description, transcript}` — the pjangler ID is enough to look
   up board, provider and PM; `transcript` is the transcript filename after the
   title-slug rename. Stored as note metadata.
3. One Bloodbank event per ticket, `bloodbank.audio.intake.detected`, published
   **after** `bloodbank.audio.transcription.completed` for the same item, and
   delivered to the ntfy `audio` topic.

## Decisions (with evidence)

- Event type `bloodbank.audio.intake.detected`: `intake` = incoming work a PM
  triages (`repo.intake.triaged` closes the loop via `data.intake_id`);
  `ticketable` is not a legal Bloodbank action; `audio.task.*` is already the
  enrichment-pass lifecycle. No validator change needed.
- Jev is called directly at `https://openrouter.ai/api/alpha/decisions` with
  HeyMa's own OpenRouter inference key
  (`op://DeLoSecrets/yydsybdlpernq5j5tcf42hmtsi/credential`, $1/day): the
  AutomaticAI gateway has no decisions route (all candidate paths 404), and the
  gateway itself calls OpenRouter directly for Jev. Documented exception.
- Measured Jev (25 calls, 2026-10-08): one `noul` question per project in a
  single request is accepted (no question cap found; ~32K-token total cap),
  0.3–0.7 s, ~$0.0003 per 5K-char transcript. Threshold 0.6 → precision 0.94,
  recall 0.94; long transcripts (>30K chars) saturate, so 0.75 there. `choice`
  is a softmax (single-label) and is not used for extraction. ±0.06 jitter.
- Registry: `GET http://127.0.0.1:8764/v1/registry` (stdlib HTTP; waxd's PATH
  has no mise shims). 36 projects; 17 have empty descriptions → Wax-side
  `config/project-aliases.yaml` supplies spoken forms / one-line "about".
  HeyMa registered 2026-10-08 as `transcription-queue` (its frozen canonical
  pjangler id) via `POST /v1/index` — never `pj init --apply` (rewrites the
  manifest and the `heyma-pm` agent key).
- Ticket extraction uses the existing gateway route
  (`automaticai/openrouter/google/gemini-3.7-flash`, token
  `heyma-wax-inference`) with a strict JSON schema whose `project_id` is an enum
  of the note's `project-ids`.
- Events are emitted by the runner (host effect), not the pass. Passes declare
  them in the result; `finalize()` publishes them after the completion row.

## Frontmatter contract

| key | owner | shape |
|---|---|---|
| `project-ids` | project-extraction | list of pjangler ids, most probable first; `[]` = ran, none |
| `projects` | project-extraction | list of project names, index-aligned with `project-ids` |
| `ticketable` | ticketable | list of `{project_id, description, transcript}`; `[]` = ran, none |

Both passes use `clobber: []`. A non-empty existing value (human or earlier
run) is preserved and reused without a provider call. Re-run procedure: clear
the key, then `wax ep run <slug> <item>`.

## Result-contract extension: `events` (wax.ep.v1, additive)

```json
"events": [{"type": "intake.detected", "key": "<stable dedup key>", "data": {...}}]
```

- The pass's registry YAML must declare `emits: [<entity>.<action>, ...]`;
  undeclared types fail the pass (`result_apply_failed`) before any mutation.
- `type` `^[a-z][a-z_]*\.[a-z][a-z_]*$`; `key` `^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$`,
  unique per result; `data` an object ≤ 32 KiB, never containing `project`
  (envelope-owned: Wax stamps `data.project = "wax"`). ≤ 50 events per result.
- Stored with the pass result in the ledger; published by `finalize()`:
  - event id `uuid5(WAX_NS, "ep-event:<item>:<slug>:<type>:<key>")` — content
    keyed, independent of plan/attempt, so re-plans and re-runs never duplicate;
    a `pass_events` table records what was enqueued;
  - the runner stamps `item_id`, `transcription_id`, `transcript` (current
    final basename) and `transcript_uri` into `data` at emission;
  - `correlationid`/`causationid` = the completion event id,
    `ordering_key = transcription:<item_id>`;
  - a new completion inserts them in the same transaction, right after the
    completion row (outbox drains in id order → completed, then tickets);
  - an already-finalized item (manual `wax ep run` backfill) gets its pending
    pass events enqueued by the next `finalize()` call;
  - building an envelope never raises out of `finalize()`.

## Intake event payload (`bloodbank.audio.intake.detected`)

Required: `project_id`, `description`, `transcript`, `transcription_id`,
`intake_id` (`<item_id>:<12-hex content hash>`), `index`, `count` (1-based).
Optional: `project_name`, `transcript_uri`, `item_id`. Schema:
`bloodbank/schemas/bloodbank/audio/intake.detected.json`.

## Delivery to ntfy `audio`

n8n workflow `XHWRrrYCzu4ifdS7` gains a `bloodbankTrigger` on
`bloodbank.audio.intake.detected` → "Format Intake Item" →
the shared JSON `httpRequest` publisher (`onError: stopWorkflow`). The same
release fixes the completion branch (formatter never deployed; ntfySend
swallowed a `toDateTime` error). ntfy user `n8n` needs `audio` write access.

## Out of scope / follow-ups

- Aliases upstream into pjangler manifests; fill empty registry descriptions.
- Migrate classification to Jev (`choice` over monolog/meeting/other).
- Gateway passthrough for the decisions API.
- Dead "New Audio File" n8n branch (Wax never emits `file.received`).
