# HeyMa inference through the AutomaticAI gateway (2026-10-22)

**Policy:** every HeyMa agent and agent-service LLM call goes through
`https://api.automaticai.io` on a named consumer token. Secrets are `op://`
references only (`op://DeLoSecrets/yeurk5dpqkaarspvsn3cjtmkki/<token-name>` for
gateway tokens). Gateway source: `~/code/client-portal/gateway`
(`~/docker/stacks/ai/newapi` symlinks to it; tokens via `ops/gateway-tokens.py`).

**Result:** the Wax chat passes and the heyma-obsidian CI review are migrated and
proven. Jev (project-extraction) is off OpenRouter, but it runs through the fleet
Jev ingress as a recorded capability exception. The ASR Groq path was deleted.
**heyma-pm (Hermes) is not closed**: it fails the completion gate (see blockers 1-3).

## Consumers

| Consumer | Owner (source) | Route / account | Token | Evidence | Status |
|---|---|---|---|---|---|
| Wax `title-slug`, `classification` (enabled), `ticketable` (disabled) | HeyMa `components/wax/config/passes.d/*.yaml`, `deploy/provision-inference` | `automaticai/auto/fast`. Members: personal glm-5.3-flash, personal glm-5.3, personal claude-sonnet-5.5, damien claude-sonnet-5.5, damien claude-haiku-4.5. Kimi is excluded (400 when temperature != 1). Intelliforia is excluded (team account). gpt-5.5 is excluded (unknown model in the gateway). | `heyma-wax-inference` (id 66) | 8 exhausted items recovered, served by damien haiku (subscription, quota 0). Compiled pipelines re-imported (transcript-standard v3, doc-standard v4). The `wax doctor` `/v1/models` check had failed with 403 (Cloudflare 1010); the HeyMa-Wax User-Agent fixed it. Guard: `chat_pass_routing_test.py`. | **Migrated** (HeyMa 61b934f) |
| Wax `project-extraction` (Jev / TypeSafe) | HeyMa `project-extraction.yaml`, `src/wax/jev.py`; stack `~/docker/stacks/ai/jev-litellm` | Not the gateway. The gateway has no decisions route, so the call goes `https://jev.delo.sh/typesafe/v1/systemone` → jev-litellm → api.typesafe.ai, model pinned to `jev-1.13.0` | jev-litellm virtual key `heyma-wax-project-extraction` ($1/day, 120 rpm), `op://DeLoSecrets/4y3hlfp2dowbzpi6ncp67kdm5i/credential` | Real requests at 10:36:18 and 10:41:25 appear in LiteLLM_SpendLogs under alias `heyma-wax-project-extraction` (21676 prompt tokens, status success). Both doctor checks pass (`jev ingress`, `jev api key`). The key is virtual (403 on `/key/list`). Guard: `JevRoutingTest`. 302 tests pass. | **Off OpenRouter; capability exception.** Pass still `enabled: false` (HeyMa 9987cee, DeLoContainers ba82502) |
| heyma-obsidian CI code review | heyma-obsidian `.github/workflows/code-review.yml`, `.github/scripts/check-gateway-routing.mjs` | `automaticai/personal/claude-opus-5.5` (personal opus/sonnet), `ANTHROPIC_BASE_URL=https://api.automaticai.io`, runner `[self-hosted, Linux, delonet]` (repo-level `delonet-hmob-r1`) | `heyma-obsidian-review` (id 89, 90-day expiry at epoch 1800438981 = 2027-01-20) | Code Review run 39759601274 (PR `tmp/gateway-review-proof`) succeeded at 2026-10-22T09:58:59Z. Guard: `npm run lint` fails on a missing `--model` or a non-`automaticai/` one. | **Migrated** (heyma-obsidian 68a7810, 1cd9f06; HeyMa pins 59cef2d, cdcfa61) |
| ASR `--groq` lane (`scripts/transcribe.py`, `bin/transcribe`) | HeyMa | Deleted. It called api.groq.com directly with a raw `GROQ_API_KEY` and had no callers. ASR stays on local faster-whisper. | none | Deletion verified in the diff. `test_asr_has_no_direct_groq_path` fails on the pre-709dc98 files. | **Removed** (HeyMa 709dc98) |
| Hermes `heyma-pm` agent | `~/.hermes/profiles/heyma-pm/config.delta.yaml` (rendered by `hermes-profile-config.py`), profile `auth.json`; gateway token | Main and fallbacks: `automaticai` (glm-5.3, kimi-2.8). Delegation: auto/subagent. Vision: glm-5.3-flash. 13 aux slots pinned to `automaticai/auto/fast`. | `hermes-heyma-pm` (id 42, scoped to the same 20 routes as hermes-delonet-director, expiry 1798555899 = 2026-12-29) | CLI one-shot with `env -i` printed "pong". The gateway ledger has token-42 rows at 10:36:52Z/10:37:08Z and 10:47:09Z/10:47:13Z (zai-personal glm-5.3, plus a glm-5.3-flash aux row). Render is idempotent (md5 1e36dce0…). Pool cleared and suppressed for openrouter, kimi-coding, zai, copilot, gemini and openai-api. | **Not closed.** Verdict `holds: false`: no regression guard, multiplexed gateway path not proved, direct lane still inheritable. Nothing tracked to commit. |

Related fix (not a consumer): `passes.exhausted_at` now clears when a pass
completes or is skipped (HeyMa 25a9e4c). On the live ledger, 12 completed rows
were cleared.

## Open blockers

| # | Blocker | Owner | Next step |
|---|---|---|---|
| 1 | heyma-pm has no regression guard. Concurrent PM-delta rollouts can silently drop the aux pins or the token override. | Hermes template / flume owner | Add a `flume audit` or `hermes-profile-config.py check` rule. It fails when: the generated config lacks `AUTOMATICAI_GATEWAY_KEY=op://…/hermes-heyma-pm`; any model or aux provider is not `automaticai`; or the pool holds non-gateway rows without `suppressed_sources`. Prove it fails on `backups/config.delta.yaml.pre-gateway-hygiene-20261022`, then commit and push. |
| 2 | heyma-pm through the installed multiplexed `hermes-gateway.service` is not proved. The service restarted at 06:32:21 EDT, before the aux pins were written at 06:36. Secrets hydrate once per home per process. | Operator | Drive a real heyma-pm turn (Bloodbank command or kanban dispatch) and show a token-42 row (`aai:hermes-heyma-pm:…`). If the row shows the fleet token, run `systemctl --user reload hermes-gateway.service` and repeat. |
| 3 | A direct lane is still inheritable. The global `~/.hermes/auth.json` holds real openai-codex OAuth rows, and `read_credential_pool()` falls back to them. `HF_TOKEN` is hydrated alongside a global huggingface env row. | Hermes template / flume owner | Suppress or shadow openai-codex and huggingface in the heyma-pm `auth.json`, or stop the global fallback for gateway-only agents. |
| 4 | The heyma-pm aux-pin rationale was wrong: `_discovery_chain_allowed()` already blocks the OpenRouter/Nous chain when the main provider is automaticai. The pins moved compression, curator and background_review off the main model, and the `session_search` pin is a dead slot. | heyma-pm owner | Decide whether background_review and compression should stay on auto/fast, drop the `session_search` pin, and correct the fleet-wide "falls through to OpenRouter" claim. |
| 5 | The fleet base `MISTRAL_API_KEY` ref (`l4cuosmvbn2m2fz4thvv5ya4a4/qxycti4uw3rlerq4pjlfkknj6a`) fails to resolve on every launch. | Hermes fleet base owner | Fix or remove the ref. |
| 6 | `project-extraction` is still `enabled: false`. The 0.6/0.75 thresholds have not been recalibrated on the ingress. | Operator | Dry-run `components/wax/config/passes.d/bin/project-extraction ~/d/Transcripts/<note>.md` on several notes, compare the results to the thresholds, set `enabled: true`, then run `wax ep run project-extraction <item>`. |
| 7 | Jev has no gateway route. This is a capability exception, not a cutover. | Gateway owner | Revisit if the gateway gains a decisions/systemone route, then move project-extraction onto it. |
| 8 | `~/.config/jev-clairic/typesafe.key` holds the jev-litellm master key in plaintext (now also vault item `olonpzrcmxdx52vhoib3uyfzdi`). `typesafe.key.direct.bak` holds the raw TypeSafe key. Both are mode 600. | Fleet Jev tools owner (zcrank-gate, jev-*) | Mint per-tool virtual keys (`mise run keys`), switch the tools to `op://`, then delete both files. |
| 9 | The gateway does not normalize temperature for Kimi (400 when temperature != 1), so Kimi stays out of `heyma-wax-inference`. | Gateway owner | Add temperature normalization for the Kimi route, then re-scope the token. |
| 10 | The `auto` strong tier has a dead `gpt-5.5` member (unknown model). | Gateway owner | Remove it or map it to a real route in `ops/AUTO-ROUTER.md` policy. |
| 11 | Sibling repos and templates still run direct Claude OAuth: CommonProject, pjangler `code-review.yml.jinja`, pilot, deckard, and others. | Each repo owner | Fork one remediation worker per configuration owner, following the heyma-obsidian pattern: a scoped token, the gateway `--model`, and the guard script. |
| 12 | The heyma-obsidian guard checks only `--model`. `--fallback-model claude-sonnet-4-5` passes. | heyma-obsidian | Match every `*-model` flag and require `automaticai/`. |
| 13 | Wax attempt counters never reset. After a recovery, a single failure re-emits `bloodbank.audio.task.exhausted` right away. | HeyMa wax | Count attempts since the last completion, or document the behavior. |
| 14 | One live ledger row still has `exhausted_at` set (diarization, skipped/human_edits), so `wax catalog` shows exhausted=1. | Operator | Run the same UPDATE with `state='skipped'`, or wait for the next skip. |
| 15 | The `heyma-obsidian-review` token expires on 2027-01-20. | Operator | Rotate it beforehand: `python3 ops/gateway-tokens.py rotate heyma-obsidian-review`, then update the repo secret. |
| 16 | Unrelated, left untouched: the `wax doctor` "outbox drained" check FAILs on a row about 21 days old. `~/code/HeyMa` is a stale second checkout, mid-rebase with a UU conflict in `components/wax/bin/waxd`, holding a20976d. The OpenRouter vault item `yydsybdlpernq5j5tcf42hmtsi` is unused by HeyMa. | Operator | Drain or inspect the outbox, decide whether to finish or delete `~/code/HeyMa`, and retire the vault item. |
| 17 | `~/code/DeLoDocs` is not a git repository, so the DeLoDocs copy of this record cannot be committed. | Operator | `git init` DeLoDocs with a remote, or accept it as an uncommitted mirror. |

## Addendum 2026-10-22 (later): no Anthropic models, and a fallback chain

Operator order: "wax inference: DO NOT use anthropic models. Fail a test if
anything points to sonnet/haiku/etc. Add hyper crush and free models as fallbacks."

**Done (HeyMa).**

- `heyma-wax-inference` (token 66) re-scoped through `deploy/provision-inference`
  to `auto/fast`, `personal/glm-5.3-flash`, `personal/glm-5.3`,
  `hypercharm/glm-5.3`, `hypercharm/minimax-m3` and `automaticai/free`. The
  Damien Haiku, Damien Sonnet and personal Sonnet routes are gone, as is
  `hypercharm/kimi-k3` (Kimi, AAI-55). Read back with `gateway-tokens.py status`.
  Earlier today's recovered runs were served by `damien/claude-haiku-4.5`; that
  can no longer happen.
- Every chat pass tries `auto/fast`, `hypercharm/glm-5.3`, `hypercharm/minimax-m3`,
  `automaticai/free` (`wax.provider.run_chain`; evidence and the note's
  `wax.passes` block name the serving model). See AGENTS.md and
  `components/wax/docs/ENRICHMENT-PASSES.md`.
- `chat_pass_routing_test.py` fails on any Anthropic model value (YAML env, bin
  and library defaults, doctor rows, provision-inference, pipelines, n8n exports,
  frozen definitions). It fails on the previous provision-inference.
- `transcript-standard` v4 and `doc-standard` v5 re-imported from the n8n release
  exports; the dry-run diff showed only the fallback/timeout env and the version.
- `wax doctor` shows the chain, probes each member against `/v1/models`
  (User-Agent `HeyMa-Wax/1.0`), and FAILs if the token scope grants an Anthropic
  route.

**Open: gateway bugs (owner: token-ops, Peter Griffin; not fixable from HeyMa).**
Found with the new scope; the chain falls through them but cannot succeed while
they stand. The `peter-griffin` session was not reachable from the HeyMa agent,
so this table is the handoff.

| # | Bug | Evidence |
|---|---|---|
| 18 | `automaticai/hypercharm/*` routes have no channel on the live gateway. | Explicit `hypercharm/glm-5.3` and `hypercharm/minimax-m3` answer 503 `model_not_found`, "No available channel ... under group aai-personal". The catalog and `/v1/models` list them. |
| 19 | `auto/fast` picks the channel-less metered Hyper member over a healthy subscription member and does not walk past it. | 3 of 3 `auto/fast` requests: 503 for `hypercharm/glm-5.3`, while `personal/glm-5.3-flash`, called explicitly, answers 200. |
| 20 | `automaticai/free` picks channel-less members, including a Kimi one. | 503 "No available channel for model automaticai/nvidia-free/kimi-k3". |
| 21 | `automaticai/free` is granted to the token but never listed by `/v1/models`. | Wax special-cases it (`VIRTUAL_MODELS`); a listing that reflects the grant would be cleaner. |
| 22 | `ops/AUTO-ROUTER.md` still says "never a metered route". | Stale after f533cae0. |

Until 18-20 close, the live chain fails end to end (every member 503) and a pass
exits `provider_http_error` with all four attempts listed. A temporary rung such
as `automaticai/personal/glm-5.3-flash` (works today, non-Anthropic) is an
operator decision; it was not added.

**Live proof of the fall-through** (pass script on a copy in /tmp, primary
`automaticai/nope/none`): `title-slug` and `classification` each fell through the
unknown primary (400) and `hypercharm/*` (503) and were served by
`automaticai/personal/glm-5.3-flash`, evidence naming it. `wax ep run` against
real items was not done: completed items already carry title, summary and
classification (the passes preserve them without a model call), and a failing
gateway would only record a failed attempt.

## Addendum 2026-10-22 (12:20Z): live outage, GLM routes in the chain

At 11:50Z every chain member answered 503 on the live gateway: `auto/fast` picked
the channel-less `hypercharm/glm-5.3`, `hypercharm/*` had no channel in group
`aai-personal` (AAI-61), and `automaticai/free` picked the channel-less
`nvidia-free/kimi-k3`. Explicit `automaticai/personal/glm-5.3-flash` and
`personal/glm-5.3` answered 200. The only failure since 11:00Z was item
`53760477d56588dd` (title-slug and classification, both `provider_http_error`).

**Done (HeyMa, no gateway or token change).**

- Chain is now `auto/fast`, `personal/glm-5.3-flash`, `personal/glm-5.3`,
  `hypercharm/glm-5.3`, `hypercharm/minimax-m3`, `automaticai/free` everywhere
  (pass YAMLs, bin defaults, `bin/wax`, docs, tests). Hyper and free remain fallbacks.
- One monotonic deadline per pass run (runner timeout via `WAX_PASS_TIMEOUT_S`,
  less a 10s reserve); per-request timeout is the remaining budget minus 1s under
  a wall-clock limit. `timeout_s` 270 -> 390 (ticketable 1500 -> 1800).
- `http.client.HTTPException` (IncompleteRead, BadStatusLine, RemoteDisconnected)
  no longer kills title-slug/classification with a traceback: it maps to
  `provider_bad_response`/`provider_unreachable` and falls through (local fake
  server tests send a short body and a garbled status line).
- Provenance: evidence and the note's `wax.passes` block carry `served_by` (the
  response `model`), `route` and the auto decision header; failed router members
  record the route they picked. An Anthropic `served_by`/`route` fails closed as
  `anthropic_model_served`, with no fall-through, and the runner refuses it again.
- A total chain failure takes its reason code from the first member actually
  asked; `missing_model` only when every member was skipped.
- `transcript-standard` v5 and `doc-standard` v6 imported (dry-run on a ledger
  copy first; only `WAX_TITLE_MODEL_FALLBACKS` and `timeout_s` changed).
- New `wax ep replan <item> [--apply]`: a minted plan never migrates, so the
  failed item (minted under v4) could not recover under the fixed chain until its
  plan was explicitly moved. Refused for completed items, changed pass sets, or
  passes that already finished under the old definition. Applied to
  `53760477d56588dd`, then `wax ep sweep`: classification and title-slug completed,
  `served_by = glm-5.3-flash` (route `personal/glm-5.3-flash`, `auto/fast`
  fell through on 429), item `complete`.
- `wax doctor` gained `inference chain serves`: a live walk of the chain that
  reports which member serves and what the gateway says served it.

**Observed afterwards (token-ops).** By 12:20Z `auto/fast` answered 429 instead of
503, and the token's `/v1/models` listing no longer showed `hypercharm/*` (3
routes listed: `auto/fast`, `personal/glm-5.3`, `personal/glm-5.3-flash`;
`automaticai/free` answered a 1-token request). Wax skips unlisted members and
the chain still lands on GLM. The token scope was not touched from HeyMa.
