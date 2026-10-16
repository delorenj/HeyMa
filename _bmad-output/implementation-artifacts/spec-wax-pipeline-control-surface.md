---
title: Wax pipeline discovery and read-only Holocene surface
type: feature
created: 2026-10-16
status: done
baseline_commit: 1b6e2ebc43148fb435f875ba3eb12549561be232
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

### Approved decisions (2026-10-16)

The user approved the recommendations and delegated remaining release decisions. n8n is the visual authoring source; the Wax CLI compiles supported graphs into versioned pipelines, and Wax owns graph execution/retries. Folder bindings default to new arrivals only, seed existing files without processing them, ignore Wax output writes and require explicit historical backfill. First release proves existing audio transcription plus ordinary Markdown document enrichment; Auto/voice appears only when actually configured, not as a fabricated active scenario. Authoring changes must be recompiled explicitly and definition identity exposes drift. Native n8n EP execution is a CLI-backed preview/manual operation, not a second active scheduler. Linked audio and notification effects remain capability-specific adapters.

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
- [x] `components/wax/src/wax/catalog.py`, `components/wax/bin/wax` — safe discovery and pipeline snapshots: purposes, capabilities, conditions, edges, bindings, bounded state summaries; compatible existing commands.
- [x] `components/wax/src/wax/{documents,pipelines}.py` and `{passes,worker,ledger,finalize}.py` — document context and scenario execution under chosen authority; no fake transcript rows.
- [x] `components/n8n-nodes-heyma/nodes/{WaxEp,WaxPipeline}/`, `package.json` — shared base, per-pass registration, encapsulation and chosen graph integration.
- [x] Wax `tests/`, n8n adapter `tests/` — isolated document/graph/replay/no-clobber/failure tests.
- [x] Holocene `apps/api/src/wax*.ts`, `server.ts`, `package.json` — bounded CLI reads, freshness and tests.
- [x] Holocene `apps/web/app/wax/`, hub/voice navigation — catalog, scenario graphs and truthful outcomes; no editor.
- [x] `components/wax/docs/ENRICHMENT-PASSES.md`, adapter `README.md`, `AGENTS.md` — binding CLI and approved ownership changes.

**Acceptance Criteria:**
- Given registered EPs, when using CLI/Holocene, then every purpose and availability is visible.
- Given a scenario graph, when inspected, then trigger, inputs, dependencies, conditions, failure policies and actual outcomes are explicit.
- Given Markdown and transcript fixtures, when compatible passes run, then text/frontmatter enrichment preserves human values without inventing source identity.
- Given an n8n composition, when executed, then Wax CLI operations and one graph authority produce durable traceable results.
- Given hosted Holocene, when opening Wax beside Agents/Projects, then active pipelines are read-only and stale/unconfigured sources are honest.

## Implementation Notes

Released: Wax catalog, compiled n8n graphs, document binding worker, shared/per-pass n8n nodes and read-only hosted Holocene tab. Defaults resolved under user delegation: new paths only, explicit human-edit reruns, dedicated ~/d/Inbox/Wax, no historical backfill or new Auto orchestration. Active n8n authoring drafts GVeFgcnYkSq8SbpX and XzKNdDixJMrdu9eh remain unpublished; Wax alone executes their compiled doc-standard v3 and transcript-standard v2. Runtime restarted and capture scope preserved.

Verification: 270 Wax tests (119 subtests), 26 adapter tests including actual installed n8n loader, 63 Holocene API tests, web typecheck/build, real editor configuration and stock two-port wiring. Worker automatically enriched one newly dropped release-check document once; classification/title completed, body preserved and no fabricated audio identity. Two audio items completed all five EPs under compiled authority. Hosted /wax renders graphs, conditions, receipt, scoped outcomes and live worker heartbeat. Screenshot saved under /tmp/opencode; this session cannot visually inspect image pixels, so UI verification used rendered DOM, graph elements and browser console instead. Existing two diarization failures remain visible; no backlog repair was included.

Holocene main bae7724 and parent root f99c99c published and deployed. Shared Git guard false positive fixed separately (9dd3488) with tests. Unrelated WIP in every repository preserved. Durable n8n package path: ~/.local/share/wax/packages/n8n-nodes-heyma-0.2.0-1f77a72.tgz.

## Spec Change Log

## Review Triage Log

Independent review (three lenses), verified against source before fixes. No low-value speculative guard was added; confirmed acceptance defects were repaired and tested.

| Finding | Verdict and disposition |
|---|---|
| Worker heartbeat bypassed by enabled continue | High, fixed with dedicated heartbeat and driven ticks/long-run tests |
| Wrapper config absent from n8n editor | High, Configure authoring fields and native editor verified |
| Non-stock requirement port could not connect | High, two stock main outputs indexed continue/requires; actual editor confirms connection |
| Audio dependencies used live versions | High, frozen dependency versions passed, registry-bump regression |
| Sweep ran live definitions before frozen replay | High, stored plan definitions reused; sweep regression |
| Imported versions downgraded/repeated imports incremented | Medium, monotonic version floor and normalized no-op test |
| Scoped document key regex rejected Wax folder case | Medium, scoped key transport accepts actual binding |
| Disabled manual-scan documentation false | Medium, explicit named run/scan allowed while automatic pickup disabled |
| Concurrent registration race | Medium, upsert/reselect and duplicate regression |
| Per-document scan exception aborted siblings | Medium, document/binding isolation test |
| Recent receipt errors could expose raw stderr | High, reason_code-only Wax/API projection plus planted-secret tests |
| Bare compile adopts audio authority | Low, explicit compile semantics documented; release compiled deliberately |
| Frozen availability source label | Low, source context shown; disabled catalog text not a runtime control |
| Symlink cycle traversal hypothesis | Maybe-false, no demonstrated reachable failure in pathlib traversal; no added complexity |
| Long scans could outlast heartbeat | High, dedicated beat thread verified during run |
| Unreadable seed silently seeded | Medium, seed_error leaves unseeded and reports failures |
| Registration error exit-zero shape | Medium, explicit run errors exposed and scan statuses preserved |
| Time-based run claim could steal live work | High, whole-run cross-process flock plus concurrent test |
| Exhausted documents unrecoverable | Medium, documents retry re-arms receipts explicitly |
| Relative folder accidentally resolved | Medium, validate raw absolute or home-qualified input before resolve |
| No-wires n8n export inherited registry edges | High, explicit imported edge mode; zero-edge regression |
| Import replacing YAML source | Low, explicit named import deliberately adopts source; snapshots report provenance |
| Malformed connection groups silently dropped | Medium, reject malformed shapes/indexes and test |
| Wrong node output array arity | Medium, two-output shape test; authoring has no CLI execution |
| Unknown pipeline operation unclear error | Low, explicit operation error path |
| Generator multiline descriptions invalid | Medium, escaped generator fixture and native loader test |
| Documentation version claim | Medium, version normalization/increment corrected |
| Concurrent-scan safety claim | Medium, upsert and flock now substantiate it |
| Holocene absent from HeyMa diff | False, separate Holocene commits and live deployment verified |
| Compiled graph leak boundary untested | Medium, compiled secret fixtures plus receipt and API tests |
| Shipped example exports not import-tested | Medium, all example exports covered against registry fixture |
| Mixed authored/registry edges dropped gates | High, per-node inheritance explicit and tested |
| Node execute parameter mapping untested | Medium, author/run/configure execute tests and live graph import; transport branches tested |
| Generated parity absent | Medium, native loader verifies manifest/source; generator test and real registry generation used |
| Worker disabled/enabled cadence untested | High, driven tick and liveness regressions |
| Native loader invalid generated basenames | High, PascalCase file/class match; actual PackageDirectoryLoader covers all 14 types/icons |
| Catalog global outcomes presented as pipeline | High, pipeline-scoped counts at frozen version and two-pipeline tests |
| n8n drift claimed in-sync without source read | Medium, exported_snapshot state reports unknown current editor drift |
| Known human edits treated as arrivals | High, new-path default tested; explicit edit run retained |
| Unfinished audio plans silently migrated | High, all minted plans preserved; new plans adopt changed graph |
| Authoring nodes demanded item IDs | Medium, default author-only operation removes invalid preview inputs; native editor proof |

Matrix coverage: catalog_test purpose/availability/leak checks; pipelines_test dependency failure/order-only/halt; n8n_import_test conditions; documents_test no-audio/no-clobber/replay/concurrency/own-writes; Holocene wax.test.ts DOWN-source/cache freshness. Every named suite ran green.

## Verification

- `mise x -- python3 -m pytest components/wax/tests` — isolated regression suite.
- Adapter build/tests — CLI transport, node contracts and graph fixtures.
- Holocene API tests/web build and hosted browser verification — real CLI-backed data and navigation.
- Temporary-root document/audio proofs before any approved production activation.
