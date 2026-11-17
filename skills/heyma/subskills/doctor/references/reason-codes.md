# Reason codes: what a failed or skipped pass is telling you

Lead with the rule: **read the `reason_code`, then the shape, then the playbook.**
Every pass attempt records a `reason_code` in the ledger (`passes` table) and in
the note's `wax.passes.<slug>` block. This table replaces the pre-chain version,
which predated the model chain and listed none of the provider codes (verified
against `components/wax/src/wax/passes.py`, `provider.py`, `jev.py` and
`config/passes.d/bin/*`, 2026-11-05).

Where to look first:

```bash
wax failures --since 7d                              # grouped by (kind, slug, reason)
sqlite3 -readonly var/wax.db "SELECT item_id,ep_slug,state,attempt,reason_code,substr(detail,1,120) FROM passes ORDER BY updated_at DESC LIMIT 20;"
wax ep status <item>                                 # every pass attempt for one item
```

How the header works: a pass that knows why it failed prints `reason_code=<code>`
as the first stderr line (regex `^reason_code=([a-z_]+)$`, so no digits). Without
a valid header the runner classifies from the exit path. A timeout is a timeout
no matter what stderr said.

## Runner codes (set by `passes.py`, not by the pass)

| Code | State | Meaning | First move |
|---|---|---|---|
| `nonzero_exit` | failed | Pass exited non-zero with no valid header. | Read stderr in `detail`; the pass hid its reason. Fix the pass to emit a header. |
| `timeout` | failed | `WAX_PASS_TIMEOUT_S` (the pass's `timeout_s`) elapsed. Wins over any stderr header. | For a chat pass see the chain deadline below; for diarization see [transcription](../../../references/transcription.md). |
| `run_error` | failed | The pass raised an unclassified exception. | Read `detail`; usually a bug or a missing dependency. |
| `result_apply_failed` | failed | The result could not be applied: protected key, bad envelope, document run declaring `events`, schema-invalid event. | Fix the result; see [contract](../../create-enrichment-pass/references/contract.md). |
| `anthropic_model_served` | failed | The gateway says an Anthropic model served the call. Fails closed; the runner refuses the result again before apply. | The no-Anthropic rule was breached. See [passes/model-chain](../../../references/passes/model-chain.md). |
| `dependency_failed` | failed (waiting) | A `requires` target is not `completed` or `skipped`. Never ticket-worthy; exempt from the attempt bound. | Fix the upstream pass; the dependent follows. |
| `inapplicable_capability` | skipped | A document run met a pass lacking `document`. Satisfies ordering. | Expected. |
| `operator_disabled` | skipped | `wax ep disable <slug>` (var/settings.json `passes.disabled`). | `wax ep enable <slug>`. `wax ep run` ignores the switch. |
| `explicitly_disabled` | skipped | The diarization kill switch (`WAX_DIARIZATION=0`). | Intended only when the operator chose it. |
| `condition_unmet` | skipped | A `when` clause did not hold (for example diarization on a `monolog`). | Expected. |
| `condition_matched` | skipped | The legacy `skip_when` clause matched. | Expected. |
| `halt_policy` | not run | A `failure_policy: halt` pipeline stopped at the first failure (reported as `halted_after`). | Fix the first failing node. |
| `quarantined` | not run | Malformed `requires`/`after`/`when`/`skip_when`, or a missing/disabled requirement. Reported by `run_all`, no ledger row. | `wax ep list` shows `!! QUARANTINED`; `wax doctor` has a `pass registry quarantine` FAIL. |

Exhaustion: at attempt >= 3 (`DEFAULT_MAX_ATTEMPTS`) the runner stamps
`passes.exhausted_at` and emits `bloodbank.audio.task.exhausted` once per (item,
slug, version). `wax ep sweep` stops retrying it; `wax ep run <slug> <item>` or
`wax documents retry` re-arms it.

## Provider codes (gateway chat passes: `title-slug`, `summarize`, `ticketable`)

These come from `provider.py`. The first column also appears as the chain
member's `reason_code` in the evidence. **Fall through** means the chain moves
to the next member.

| Code | Cause | Falls through? | First move |
|---|---|---|---|
| `provider_rate_limited` | HTTP 429. | yes | Usually transient (`auto/fast` 429s at times). If every asked member 429s the whole chain takes this code. |
| `provider_quota_exceeded` | HTTP 402: the key's spend limit or an upstream balance is exhausted. | yes | A funding decision, not a Wax bug. See the 2026-11-02 incident. |
| `provider_http_error` | 5xx or any other status; also `gateway_restarting` after the patience window. | yes (not for `gateway_restarting`, which stops the chain) | Check the gateway. |
| `provider_unreachable` | DNS, connect or read failure. | yes | Network or gateway down. |
| `timeout` | A member exceeded its slice of the deadline, or the SIGALRM wall clock fired. | yes | Compare `REQUEST_TIMEOUT_S` with `timeout_s`. |
| `missing_model` | The pre-flight `/v1/models` did not list the member (skipped), or the whole chain was skipped. `automaticai/free` is never skipped. | yes | Normal for the unfunded GLM/DeepSeek members today. |
| `model_not_found` | The gateway returned a model-not-found error. | yes | Same as above; run `reconcile-routes` after funding. |
| `provider_bad_request` | HTTP 400, including detail `max_tokens_exceeded`. | yes | Reduce input or output budget. |
| `provider_bad_response` | Empty, unparseable or truncated content. Detail codes: `truncated_response`, `malformed_response`, `output_truncated`. | yes | A model quirk; the next member usually answers. |
| `provider_auth_rejected` | HTTP 401 or 403. A 403 with detail `model_out_of_scope` ("token has no access to model") **falls through**; any other 401/403 does **not**. | only `model_out_of_scope` | Re-run `deploy/provision-inference`. For an OpenRouter-style `User not found`, check `is_provisioning_key`. |
| `no_api_key` | The `op://` key did not resolve. Never falls through. | no | `op whoami`; confirm the item UUID. |
| `anthropic_model_refused` | A chain member named an Anthropic model; skipped, never called. | n/a | Remove the member; `chat_pass_routing_test` should have caught it. |
| `deadline_exceeded` | Less than `MIN_REQUEST_S` (2 s) left before the deadline; the member is skipped. | n/a | Chain deadline equals the runner's `WAX_PASS_TIMEOUT_S` less a 10 s reserve. |
| `jev_too_large` | A Jev call (`project-extraction`) was too big for the model; the caller sends less state. | n/a | Pass-specific; see below. |

How a whole-chain failure is named: the reason code of the first **asked**
member; `provider_rate_limited` if all asked members were rate limited;
`missing_model` if every member was skipped; `timeout` if `deadline_exceeded`
skips occurred with nothing tried. The result's `evidence` lists every member
with its outcome. Full chain semantics are in
[passes/model-chain](../../../references/passes/model-chain.md).

## Per-pass codes

| Pass | Codes | Notes |
|---|---|---|
| `title-slug`, `summarize`, `classification`, `dictionary-correction`, `project-extraction` | `empty_document` (skipped) | An empty note is `skipped`, not `failed` (operator rule 2026-10-27). |
| `classification` | `low_confidence`, `kind_disagrees_with_is_phone`, `invalid_classification`; abstain stamps `unsorted` | Jev on its own key; floor and telnyx rules run first. |
| `project-extraction` | `registry_invalid`, `jev_too_large` | `registry_invalid` is the pjangler registry. |
| `ticketable` | `no_project` (skipped) | Disabled today. |
| `dictionary-correction` | `dictionary_missing`, `dictionary_invalid`, plus Jev provider codes | See [passes/dictionary](../../../references/passes/dictionary.md). |
| `diarization` | `diarization_failed`, `human_edits` (skipped: a human changed the body after labelling), `channel_separated` (skipped: dual-channel `telnyx-*`) | The old "CUDA out of memory at Sortformer load" failure shows as `diarization_failed`. |
| `voice-id` | `audio_missing`, `voice_id_failed`, `no_speech` (skipped) | `voice_id_failed` carries the GPU child's stderr tail; see [passes/voice-id](../../../references/passes/voice-id.md). |
| `call-metadata` | `not_a_call` (skipped), `call_not_found`, `telnyx_auth_failed`, `telnyx_http_error`, `telnyx_unreachable` | Telnyx key is an `op://` reference by item UUID. |
| `clipboard-copy` | `clipboard_unavailable`, plus runner codes | No current display or all clipboard backends failed/timed out. A successful user-manager query authoritatively replaces display/auth keys; absent X11 means no inherited X11 fallback. Failed queries retain explicit inherited context and per-user Wayland discovery, never guessed X11. Read the backend summary in detail; restore the desktop context before a targeted retry. This is a desk effect, so preserve clipboard context. |
| `frontmatter-stamp` | runner codes only | |

## Item, capture and archive causes (not pass codes)

These are transition `cause_code` values on the item or stream machines, shown
by `wax state` and `wax items`. Details live in
[capture](../../../references/capture.md) and
[archive](../../../references/archive.md).

| Cause | Where | Meaning |
|---|---|---|
| `archive_failed` | item `failed` | S3 backup did not verify after 3 attempts. The source is copied to `recovered/unbacked/` and **transcription does not run**. |
| `s3_reverify_failed` | item `failed` | The live re-verify before parking found a size mismatch; audio stays in the inbox. |
| `file_too_large_for_transcription` | item `skipped` | At or over `MAX_AUDIO_FILE_SIZE_FOR_TRANSCRIPTION` (300MB; the check is strict, so exactly 300MB is blocked); archived first, then moved to `skipped/oversize`. |
| `audio_too_long_for_transcription` | item `skipped` | At or over `MAX_AUDIO_DURATION_FOR_TRANSCRIPTION` (3h); archived first, moved to `skipped/overduration`. |
| `transcribe_failed` | item `failed` | The transcriber raised. Audio stays in the inbox. |
| `completion_withheld` | item `enrichment_pending` | A planned pass is not done, so `transcription.completed` waits. |
| `stranded_work` | inbox `error` | Work claimed with no live owner. |
| `finalizing` | stream `not-ready` (clause a) | A recording is being finalized (concatenated and moved to `inbox/`); wait, do not intervene. |
| `uninstructed_exit`, `finalizer_died`, `finalize_deadline_expired`, `finalize_failed` | stream `error-partial` | The encoder exited without an instruction, or finalizing failed or ran past 180 s. |
| `orphan_partial` | stream `error` | Segments or a partial with no `rec.json`, older than 10 s. |
| `no_default_source`, `default_source_absent`, `disk_unreadable`, `disk_low`, `not_writable:stream`, `not_writable:inbox` | stream `not-ready` (clause b) | The first failing capture preflight. |
