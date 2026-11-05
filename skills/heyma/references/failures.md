# Failures: `wax failures`, HEYMA tickets and reason codes by shape

Wax degrades by **recording**, not by crashing: a failed pass, an exhausted
document receipt, a failed item or a stale outbox row sits in `var/wax.db`
where `wax status` counts it and nobody reads it. `wax failures` turns those
into fleet work. It is a deterministic collector over the ledger (no LLM, no
network) plus a sweep that files each recurring failure as **one** urgent ticket
on the HEYMA board through `px`. Verified against
`components/wax/src/wax/failures.py` on 2026-11-05.

## Commands

```
wax failures [list] [--since 7d] [--grace 1h]      # read-only: signatures and their tickets
wax failures sweep [--since 7d] [--grace 1h] [--max 5] [--dry-run]
mise run wax:failures                              # the mise wrapper
```

| Option | Default | Meaning |
|---|---|---|
| `--since` | 7d | Look-back window (`30m`, `1h`, `7d`, `2w`, or bare seconds). |
| `--grace` | 1h | A non-final failure becomes ticket-worthy only after failing this long. waxd sweeps passes every 900 s and scans documents every 120 s, so a failure standing for an hour will not heal itself. |
| `--max` | 5 | New tickets per sweep. What the cap leaves is returned in `capped`, never dropped silently. |
| `--dry-run` | off | Writes nothing and takes no lock; shows the ticket body it would file. |

**It is not scheduled** (`AGENTS.md` says so, and there is no Candybar manifest:
only `wax-calls-sync` exists under `components/wax/deploy/candybar/`). Nothing
files a ticket unless someone runs the sweep. Any recurring schedule must be a
Candybar manifest, not a timer, cron or Hermes job.

## What counts

The collector reads through the definitions Wax already has, in eight kinds:

| Kind | Source | Ticket-worthy reasons | Re-run recipe (`_RERUN_RULE`) |
|---|---|---|---|
| `pass` | failed `passes` rows | the pass's `reason_code`, at the current version, exhausted or past the grace | audio: `wax ep run <slug> <item>`; document: `wax documents run <pipeline> <note>` |
| `document` | failed `document_receipts` with a failed document and no failed pass row to blame | pipeline-level faults (a run crash, a halt) | `wax documents retry <pipeline> --doc <doc_id>` then `wax documents scan <pipeline>` |
| `item` | items `failed` or `suspect` | the transition `cause_code` | `wax retry <item_id>` |
| `recording` | the call log | `fetch_error`, `voicemail_unfetched` | `wax calls sync --since 2d` |
| `outbox` | unpublished event rows | `outbox_stale` | `wax events drain` |
| `snapshot` | transcriptions-store queue | `snapshot_rejected`, `projection_failed` | `wax transcripts sync` |
| `hygiene` | document-hygiene sweeps | `sweep_error` | `wax documents sweep-empty` or `sweep-untitled` |
| `worker` | inbox machine `error` | `worker_exception` always; `stranded_work` only past the grace | the queue draining again |

Never ticket-worthy: `dependency_failed` (its upstream gets the ticket),
`voicemail_empty` (a caller hung up at the beep, final, never retried), call
outcomes such as `CALL_REJECTED` (a person's choice), documents that were
superseded, trashed or vanished, passes that are switched off (registry or
operator), and failures at a version older than the registry's.

A **signature** folds failures by `(kind, slug, reason_code)`; its fingerprint is
`sha256("kind|slug|reason")[:12]`. One signature is one ticket however many
items it affects (up to 20 subjects are listed).

## What the sweep does

| Ledger row (`failure_tickets`, created lazily by the sweep, never by waxd) | Action |
|---|---|
| none | create the ticket (Todo, urgent, labels `bug,wax-failure`) with marker `wax-failure:<fp>.<gen>` in the body |
| `uncertain` (an earlier create failed or timed out) | create again with the **same** marker, so `px` adopts the ticket if it exists |
| `filed`, no new subjects | nothing, no `px` call |
| `filed`, new subjects | `px show`; if open, comment the new subjects; if closed or deleted, file `generation + 1` with a regression note |

A real sweep holds `var/failures-sweep.lock`; a second one does nothing and
says `locked`. A `px` failure never raises: the row stays `uncertain` with
`last_error` and the sweep moves on. The ticket body points the triage agent at
`skills/heyma/subskills/doctor/SUBSKILL.md` (constant `failures.DOCTOR_SKILL`,
checked by `components/wax/tests/skill_hub_test.py`, so moving the doctor
sub-skill fails the suite) and at the re-run recipe. The board's own lanes do
the rest: `heyma-pm` grooms it, a delegated worker fixes it. HEYMA-12
("[wax] pass call-metadata: call_not_found (1 affected)", 2026-11-05) is the
first ticket this produced; see the
[incident log](../subskills/doctor/references/incident-log.md).

## Live state, 2026-11-05

`wax failures` reports 0 signatures in 1w. The doctor board's
`no-recent-pass-failures` check still shows two `title-slug` rows (document runs
`doc-dfdc82fe4e248c41` and `doc-788597991e1603ea`, `provider_bad_response` and
`provider_rate_limited`, attempt 1 on 2026-11-04). **Unexplained:** by the rules
above they look ticket-worthy after the grace, yet the collector does not
surface them. Treat the doctor board as the broader view and read
`sqlite3 -readonly var/wax.db "SELECT ... FROM passes WHERE state NOT IN
('completed','skipped')"` when the two disagree. (Raw rows are the ground truth;
a zero from `wax failures` is a statement about tickets, not about health.)

## Reason codes by shape

A failure's `reason_code` usually tells you which of the three shapes it is.
Full code tables are in
[reason codes](../subskills/doctor/references/reason-codes.md); the shapes are in
[the doctor procedure](../subskills/doctor/SUBSKILL.md).

| Shape | Typical codes | Why |
|---|---|---|
| 1. An observer guessing at something it does not own | `call_not_found`, `uninstructed_exit`, `stranded_work`, `source_changed`, `s3_reverify_failed` | Something inferred state instead of asking the owner. Fix the inference. |
| 2. The code that ran was not the code that was edited | `passes_incomplete` after a manual run, `nonzero_exit` with an import error, `anthropic_model_served`, `diarization_failed` from a missing runtime, `transcribe_failed` from a drifted shim | A drifted path, a frozen plan or a stale checkout. Check `same-checkout` and `wax ep replan`. |
| 3. A sub-stage degrading to an empty value with no path to a human | `provider_bad_response`, `missing_model` (skipped), `empty_transcript`, `human_edits`, `diarized: false` | The failure is real but quiet. This is the class `wax failures` exists to surface. |

Rules of thumb:

- A new code that does not fit any shape is a fourth shape; write it in the
  incident log.
- A transient provider code (`provider_rate_limited`, `timeout`) at attempt 1 is
  usually the sweep's job (`WAX_SWEEP_INTERVAL_S`, 900 s). It becomes a ticket
  only after the grace or on exhaustion.
- An exhausted pass emits `bloodbank.audio.task.exhausted` and is final at once;
  re-arm it with `wax ep run <slug> <item>`.
- Never ticket a symptom the doctor can explain: run `doctor.sh` first and put
  its first failing layer in the ticket.
