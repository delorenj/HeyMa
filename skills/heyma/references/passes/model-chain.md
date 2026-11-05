# The model chain: how chat passes reach a model

Three passes call a chat model: `title-slug`, `summarize` and `ticketable`. They
do it through the AutomaticAI gateway at `https://api.automaticai.io/v1` on one
scoped token, trying an **ordered chain** of models until one answers. The
runner for this is `wax.provider.run_chain`
(`components/wax/src/wax/provider.py`). Verified against the code, the pass
YAML, the ledger and the gateway's `/v1/models` on 2026-11-05.

**As of 2026-11-05 the live chain is effectively `automaticai/auto/fast` then
`automaticai/free`.** The four middle members are in the token's scope but
unserved (see "Current state"). Titles still land, which is exactly why this
looks fine from the tray.

## The chain

| # | Member | What it is |
|---|---|---|
| 1 | `automaticai/auto/fast` | the gateway's router (fast tier, then strong tier); the primary |
| 2 | `automaticai/personal/glm-5.3-flash` | subscription z.ai GLM |
| 3 | `automaticai/personal/glm-5.3` | subscription z.ai GLM |
| 4 | `automaticai/deepseek/deepseek-flash` | metered DeepSeek direct |
| 5 | `automaticai/deepseek/deepseek-v4-pro` | metered DeepSeek direct |
| 6 | `automaticai/free` | the free-model router family; never skipped |

It lives in each pass YAML's `env` as `WAX_TITLE_MODEL` plus
`WAX_TITLE_MODEL_FALLBACKS` (and `WAX_SUMMARY_*`, `WAX_TICKETS_*`), with matching
defaults in the bin scripts. The key is
`op://DeLoSecrets/yeurk5dpqkaarspvsn3cjtmkki/heyma-wax-inference` (item UUID,
field `heyma-wax-inference`); the token is scoped to exactly these routes and
nothing else. `classification` left the chain in v2 (2026-10-29): it and
`project-extraction` and `dictionary-correction` go through the Jev ingress on
their own keys ([passes/catalog](catalog.md)). No pass is configured for
OpenRouter, and Hyper is out while the gateway blocks it (AAI-64).

## Fall-through rules

The chain moves to the next member for **provider-side** failures only.

| Result from a member | Reason code | Next member? |
|---|---|---|
| 429 | `provider_rate_limited` | yes |
| 402 | `provider_quota_exceeded` | yes |
| 5xx or other status | `provider_http_error` | yes |
| DNS, connect, read failure | `provider_unreachable` | yes |
| member timeout | `timeout` | yes |
| unlisted at `/v1/models` | `missing_model` (skipped, not called) | yes |
| gateway "model not found" | `model_not_found` | yes |
| 400 | `provider_bad_request` | yes |
| empty, unparseable or truncated content | `provider_bad_response` | yes |
| 403 with detail `model_out_of_scope` ("token has no access to model") | `provider_auth_rejected` | yes |
| 401, or any other 403 | `provider_auth_rejected` | **no**: the key itself is wrong |
| key did not resolve | `no_api_key` | **no** |
| gateway says an Anthropic model served | `anthropic_model_served` | **no**: fails closed |
| member itself names an Anthropic model | `anthropic_model_refused` | skipped, never called |

`503 gateway_restarting` (a gateway deploy; Retry-After about 15 s) is **neither
a fall-through nor a stop at first**: the chain sleeps the Retry-After on the
same member, up to `GATEWAY_PATIENCE_S` = 120 s in total and inside its
deadline, then stops with `provider_http_error` / detail `gateway_restarting` and
marks the remaining members skipped, so a deploy does not spray every route.

**Deadline.** The whole chain runs against one deadline taken from the runner's
`WAX_PASS_TIMEOUT_S`, less `RESERVE_S` = 10 s so the runner never kills a pass
before it reports every member. Each request ends `REQUEST_MARGIN_S` = 1 s before
the deadline; a member with less than `MIN_REQUEST_S` = 2 s left is skipped as
`deadline_exceeded`. The runner's SIGALRM wall clock fires as `timeout`. That is
why `title-slug` has `timeout_s: 390` for six 60-second members plus the catalog
read and `op read`.

**Naming a whole-chain failure.** The reason code of the first member that was
*asked*; `provider_rate_limited` if every asked member was rate limited;
`missing_model` if every member was skipped; `timeout` if `deadline_exceeded`
skips occurred with nothing tried. Detail codes you may see:
`truncated_response`, `malformed_response`, `output_truncated`,
`max_tokens_exceeded`, `model_not_found`, `model_out_of_scope`,
`gateway_restarting`.

**Evidence.** A pass's `result.evidence` records `{model, served_by, route,
auto, chain, fallbacks}` and one entry per failed or skipped member. The note's
`wax.passes.<slug>` block records `model`, `served_by`, `route` and
`fell_through`. `title-slug` and `summarize` read `/v1/models` once and skip
members it does not list. A pass served by an Anthropic model fails closed and
the runner refuses such a result again before applying it.

## The no-Anthropic rule

**No Anthropic model, anywhere, by operator order (2026-10-22).**
`chat_pass_routing_test` fails on any claude, sonnet, haiku, opus, fable or
anthropic model in pass env, bin and library defaults, the doctor rows,
`deploy/provision-inference`, authored pipelines, n8n exports and frozen
definitions. `provision-inference` refuses to run if one appears (its
`FORBIDDEN` regex also covers `openrouter`). Do not recommend one as a fix.

Kimi is out of Wax's scope until deliberately re-added: Kimi routes 400 on
`temperature != 1` (gateway bug AAI-55). AAI-55 is recorded as fixed on
2026-10-22 in a memory note, but the code docstring and `AGENTS.md` still list
Kimi as out, and a re-add has not been done. Unverified whether it should be.

## `provision-inference`: the scope's single source of truth

```bash
components/wax/deploy/provision-inference            # dry run (default)
components/wax/deploy/provision-inference --apply    # mint or re-scope
```

It owns `MEMBERS` (chain order), mints or re-scopes the `heyma-wax-inference`
consumer in place (key, `op://` item and expiry preserved), saves the key in
1Password itself, and prints nothing secret. A route the gateway catalog does not
carry is warned about and **skipped, not an error**, so re-run it after a gateway
deploy. Out on purpose: Kimi (AAI-55), Hyper (AAI-64), every OpenRouter route
and the auto policy's own free rung (AAI-56), and any team-account route
(transcripts are personal). Its module docstring is stale: it says the chat
passes are "title-slug, classification, ticketable"; today they are `title-slug`,
`summarize` and `ticketable`. Never edit the token by hand.

## Current state (2026-11-05)

| Check | Result |
|---|---|
| `/v1/models` for the token | lists `automaticai/auto/fast` and `automaticai/free` plus free routes; **does not** list the four middle members |
| Ledger evidence for recent `title-slug` runs | the four middle members are `missing_model`, skipped |
| Why | Gateway channels 9 (zai-personal) and 25 (deepseek) were disabled on 2026-11-02 14:13 UTC: z.ai answered 429 code 1113 (recharge), DeepSeek answered 402 (balance -0.01). Accounts unfunded. |
| Recent served-by examples | `@cf/zai-org/glm-5.3-flash`, `nvidia/nemotron-3-ultra-550b-a55b:free` |
| `auto/fast` today | intermittently fails with 429 and timeouts, so the free member really does work |
| Doctor | `title-chain-offered` warns and names the skipped members |

This is a **funding decision**, not a Wax or scope bug. Do not re-scope the
token or drop the members. After the operator funds an account:

```bash
cd ~/code/client-portal/gateway && python3 ops/reconcile-routes.py --account zai-personal --apply
cd ~/code/client-portal/gateway && python3 ops/reconcile-routes.py --account deepseek --apply
```

then re-probe `/v1/models` and re-run the doctor.

## Changing the chain

1. Edit the pass YAML env (`WAX_TITLE_MODEL`, `_FALLBACKS`, and the matching
   bin default) and `MEMBERS` in `provision-inference`; keep them in step.
2. `components/wax/deploy/provision-inference --apply` to re-scope.
3. Compiled pipelines freeze pass env, so re-import the n8n release exports:
   `wax pipeline import <export.json> --name transcript-standard` and
   `--name doc-standard` ([pipelines](../pipelines.md)).
4. An unfinished item minted under the old chain moves only by
   `wax ep replan <item> --apply`, then `wax ep run <slug> <item>`.
5. Run `mise run wax:test` (the routing test) and `wax doctor`.

For which reason codes mean what, see
[reason codes](../../subskills/doctor/references/reason-codes.md); for the
2026-10-22 and 2026-11-02 incidents, the
[incident log](../../subskills/doctor/references/incident-log.md). OpenRouter
"User not found" means a provisioning key was used for inference; check
`is_provisioning_key` is false.
