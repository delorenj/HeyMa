---
title: Wax pipeline discovery and read-only Holocene surface
type: feature
created: 2026-10-16
status: draft
route: dispatch
review_loop_iteration: 0
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

Discover available enrichment passes, their purpose, actual state and scenario triggers through the Wax CLI and a read-only Holocene Wax tab beside Agents/Projects. Each EP is an n8n node derived from a shared Wax EP base; a Wax pipeline node encapsulates passes and trigger/archive/organization logic. n8n visually composes dependencies, conditions and failure policies.

EPs transform document text/metadata into enriched text/frontmatter. Audio-dependent EPs declare that capability; ordinary documents never acquire fabricated transcript provenance.

## Boundaries & Constraints

**Always:** Wax owns artifact mutations, execution receipts and verified archival/recovery. Keep one graph/retry authority. Preserve audio bytes/identity, historical plans, no-clobber and compare-and-replace protections. Distinguish availability, enabled, automatic, running, skipped, failed and exhausted. Holocene consumes a safe CLI read contract through its API, not YAML/SQLite directly. n8n invokes CLI operations, not pass scripts.

**Never:** Add Holocene editing controls; silently enable passes; expose environment/secrets/bodies/raw stderr in discovery; represent unconfigured scenarios as active. No production cutover, backfill or workflow activation during planning.

## I/O & Edge-Case Matrix

| State | Expected behavior |
|---|---|
| Disabled/placeholder/quarantined EP | Visible with purpose and availability reason |
| A fails, B consumes A | B blocked; independent C may continue |
| B only ordered after A | Both outcomes tracked regardless of A's success |
| Condition unmatched | Visible intentional skip, not completed output |
| Document without audio | Compatible EPs work; audio EP visibly inapplicable |
| Duplicate arrival | One effective run per binding/revision/definition |
| CLI unavailable/stale | Explicit source failure/age, never healthy emptiness |

</frozen-after-approval>

## Open Questions

1. n8n authority: compile supported n8n graphs through the CLI into versioned Wax-executed pipelines (recommended), or execute graphs in n8n with CLI-backed EP nodes? The latter moves graph progression/retry ownership to n8n; do not build both.
2. Folder binding: new arrivals only, or also subsequent human edits? Both exclude Wax's own writes to prevent loops; historical backfill stays explicit.
3. First release: prove transcription and generic vault-document scenarios, displaying Auto/voice only if configured (recommended), or also implement a new Auto voice/intent pipeline? The latter needs a delegation/acknowledgment contract.

## Code Map

HeyMa-relative unless absolute:

- `components/wax/config/passes.d/`: eleven definitions; five enabled+auto. `project-extraction`/`ticketable` implemented but disabled; four placeholders.
- `components/wax/src/wax/passes.py`: registry, quarantine, conditions, ordering, runner; `md_for` requires a transcript row. `_apply_result` assumes audio provenance. `requires` accepts completed OR skipped; `after` only orders.
- `components/wax/src/wax/{worker,finalize,ledger,operations}.py`: post-transcribe auto trigger, 900s retry sweep, stored plans, item locks and publication barriers. Preserve audio recovery.
- `components/wax/bin/wax`: extend `ep_list_rows` and JSON dispatch; description/after/conditions absent. Existing status exposes bodies; create safe summaries.
- `components/n8n-nodes-heyma/nodes/HeyMa/{HeyMa.node,waxCli}.ts`: recording-only adapter; reuse absolute `execFile`. Parser rejects array responses; failed pass may return exit-zero JSON.
- `/home/delorenj/code/33GOD/holocene/apps/api/src/{hub,voice-routes,server}.ts`: read-source/freshness and route patterns.
- Holocene `apps/web/app/{hub/hub-app,voice/voice-app}.tsx`: sibling navigation/polling. Web container cannot read Wax; host API can invoke CLI.

## Tasks & Acceptance

**Execution:**
- [ ] `components/wax/src/wax/catalog.py`, `components/wax/bin/wax` — safe discovery and pipeline snapshots: purposes, capabilities, conditions, edges, bindings, bounded state summaries; compatible existing commands.
- [ ] `components/wax/src/wax/{documents,pipelines}.py` and `{passes,worker,ledger,finalize}.py` — document context and scenario execution under chosen authority; no fake transcript rows.
- [ ] `components/n8n-nodes-heyma/nodes/{WaxEp,WaxPipeline}/`, `package.json` — shared base, per-pass registration, encapsulation and chosen graph integration.
- [ ] Wax `tests/`, n8n adapter `tests/` — isolated document/graph/replay/no-clobber/failure tests.
- [ ] Holocene `apps/api/src/wax*.ts`, `server.ts`, `package.json` — bounded CLI reads, freshness and tests.
- [ ] Holocene `apps/web/app/wax/`, hub/voice navigation — catalog, scenario graphs and truthful outcomes; no editor.
- [ ] `components/wax/docs/ENRICHMENT-PASSES.md`, adapter `README.md`, `AGENTS.md` — binding CLI and approved ownership changes.

**Acceptance Criteria:**
- Given registered EPs, when using CLI/Holocene, then every purpose and availability is visible.
- Given a scenario graph, when inspected, then trigger, inputs, dependencies, conditions, failure policies and actual outcomes are explicit.
- Given Markdown and transcript fixtures, when compatible passes run, then text/frontmatter enrichment preserves human values without inventing source identity.
- Given an n8n composition, when executed, then Wax CLI operations and one graph authority produce durable traceable results.
- Given hosted Holocene, when opening Wax beside Agents/Projects, then active pipelines are read-only and stale/unconfigured sources are honest.

## Implementation Notes

Investigation only; no runtime changes. CLI shows two failed diarization rows; doctor path findings need verification. Holocene has unrelated WIP: isolate implementation. Full-scope integration includes deployment; authority, edit triggers and new Auto behavior await answers.

## Spec Change Log

## Review Triage Log

## Verification

- `mise x -- python3 -m pytest components/wax/tests` — isolated regression suite.
- Adapter build/tests — CLI transport, node contracts and graph fixtures.
- Holocene API tests/web build and hosted browser verification — real CLI-backed data and navigation.
- Temporary-root document/audio proofs before any approved production activation.
