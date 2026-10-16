# n8n-nodes-heyma

A private n8n community-node package for controlling HeyMa's Wax audio pipeline.

Wax remains the single owner of recording, archiving, transcription, and enrichment. Every node here is a thin control client: each invokes the canonical `wax` CLI, which keeps capture serialization, sentinels, state transitions, retries, and receipts inside Wax. Nodes never execute pass scripts, never read `passes.d/` or the SQLite ledger, and never schedule anything.

## Nodes

### HeyMa (`Recording`)

- **Start Recording** — starts a Wax capture with an optional label and Opus bitrate.
- **Stop Recording** — stops the active capture, or a specific capture ID, and returns the finalized inbox path and audio metadata.

### Wax Enrichment Pass (`WaxEp`)

Runs one enrichment pass for one item or document through `wax ep run`. The pass list is **not static**: the `Pass` parameter loads options live from `wax catalog --json`, so a new `passes.d/*.yaml` appears in the editor without a package change — disabled and placeholder passes included, so availability is visible at authoring time. A response body with `state: "failed"` (Wax exits zero for operation-level failures) becomes a workflow-visible error item with the pass's `reason_code`.

This is a CLI-backed manual/preview operation, deliberately not a scheduler: automatic execution belongs to the Wax worker, which owns the plan, the attempt budget, and the dead-letter receipts.

**Graph authoring outputs.** The node exposes two output ports: `main` (continue — ordering only) and `requirement` (gated — the target pass `requires` this one). The `When` / `Skip When` parameters carry JSON conditions. `wax pipeline import` compiles all of it into the pipeline definition; the parameters are ignored at execution time.

### Wax Pass nodes (`wax-pass-*`)

One n8n node per registered pass — **Wax Pass · Title Slug**, **Wax Pass · Classification**, … — generated from the shared `WaxPassNodeBase` (itself the generic Wax Enrichment Pass with the pass fixed). A graph authored with per-pass nodes reads as itself in the editor. Regenerate deliberately after the pass registry changes:

```bash
cd /home/delorenj/HeyMa/components/n8n-nodes-heyma
npm run nodes:passes   # reads `wax catalog --json`, emits nodes/wax-pass-<slug>/, updates the manifest
```

Generated files carry an AUTO-GENERATED banner and are committed: the node manifest must stay reviewable and travel-safe.

### Wax Pipeline (`WaxPipeline`)

Encapsulates a whole graph — passes, trigger, folder bindings — behind pipeline-level CLI operations:

- **Compile** — explicitly validate and adopt authored graphs (`wax pipeline compile`); `definition_id` exposes drift until then.
- **Import Workflow** — compile an exported n8n workflow JSON into a versioned pipeline (`wax pipeline import`). This is the graph-authoring path: the export is mapped (wrapper → trigger/folders/policy/enabled, Wax Pass nodes → graph nodes with conditions, `main` connections → continue edges, `requirement` connections → gated edges) and validated — unsupported node types, dynamic expressions, cycles, duplicate passes and dangling requirements are rejected loudly. n8n never executes the imported graph.
- **Show** — one pipeline snapshot: trigger, nodes, edges, bindings, drift (`wax pipeline show`).
- **Scan Documents** — seed new bindings and process new arrivals only, never backfills (`wax documents scan`).
- **Run Document** — manually run a compiled pipeline over one Markdown file (`wax documents run`).

n8n is the authoring surface, the Wax CLI is the compiler, and Wax is the executor. The node never edits YAML and never activates anything: enabling a pipeline stays a Wax-side act.

### Example workflows

`examples/wax-documents-workflow.json` and `examples/wax-transcription-workflow.json` are real n8n exports showing gated (`requirement`) vs continue (`main`) edges, wrapper configuration and annotation sticky notes. Both ship with the wrapper's `enabled` toggle off — importing them compiles but activates nothing:

```bash
wax pipeline import components/n8n-nodes-heyma/examples/wax-documents-workflow.json
wax pipeline import components/n8n-nodes-heyma/examples/wax-transcription-workflow.json
```

## Contract shared by all nodes

- Every operation is one validated CLI invocation — slugs, pipeline names, IDs, and absolute paths are regex-checked before any process starts.
- Absolute paths only for both the Wax executable and `WAX_ROOT`; PATH lookup is never used.
- Wax's JSON response object is parsed into the n8n item output (arrays are rejected); `stderr` is preserved when Wax writes one.
- CLI failures (non-zero exit) and exit-zero operation failures both surface as error items under `continueOnFail`, with exit code, stderr, and stdout attached.

## Requirements

- Self-hosted n8n running on the same Linux host and user session as Wax.
- Node.js 22 or newer.
- An active `waxd.service` and an absolute Wax shim path (normally `/home/delorenj/HeyMa/bin/wax`).
- Access to the host's PulseAudio/PipeWire and systemd user session (Recording only; the Wax nodes need just the CLI).

This package intentionally isn't eligible for n8n Cloud: starting a host recorder requires local process execution. A containerized n8n deployment also needs host audio and user-systemd integration; merely bind-mounting the HeyMa directory is not enough.

## Build and test

```bash
cd /home/delorenj/HeyMa/components/n8n-nodes-heyma
npm install
npm test
npm run lint
npm run pack:check
```

The tests use temporary fake Wax executables. They never touch the real microphone, inbox, ledger, or daemon.

## Install in self-hosted n8n

Build the package, then install it into n8n's community-node directory:

```bash
cd /home/delorenj/HeyMa/components/n8n-nodes-heyma
npm run build

mkdir -p ~/.n8n/nodes
cd ~/.n8n/nodes
npm install /home/delorenj/HeyMa/components/n8n-nodes-heyma
```

Restart n8n after installation. In the editor, add **HeyMa** (Recording), **Wax Enrichment Pass**, or **Wax Pipeline**, then choose the operation.

## Configuration

All nodes default to the production paths on this host:

- **Wax Executable:** `/home/delorenj/HeyMa/bin/wax`
- **Wax Root:** `/home/delorenj/HeyMa`
- **Timeout:** 30 seconds for start; 3700 seconds for stop/finalize; 120 seconds for the Wax nodes
- **Execute Once:** enabled

`Execute Once` prevents a multi-item input from accidentally issuing the same lifecycle action more than once. Disable it only when intentionally stopping several explicit capture IDs (Recording) or running a pass across several items (Wax nodes).

## Output

Start Recording returns Wax fields such as `started`, `target`, `source`, and `pid`. Stop Recording returns fields such as `rid`, `path`, `duration_s`, `bytes`, and segment counts. The Wax nodes return the CLI's JSON response object verbatim (`wax ep run` state and reason, compile receipts, pipeline snapshots, document-run results). Every output also includes `operation` where applicable and preserves n8n item pairing.

## Compatibility

Developed and validated against n8n 2.18.4 and the `@n8n/node-cli` 0.44.4 toolchain.

## Deploying the document scenario (exact commands)

Everything below is prepared and verified; the activation steps are yours.
Nothing is activated by building, committing, or importing as written —
`doc-standard` and both example workflows ship disabled.

**1. The folder.** A dedicated inbox, distinct from the transcript vault
(`~/d/Transcripts` is Wax output; binding it would double-enrich):

```bash
mkdir -p ~/d/Inbox/Wax
```

**2. Deploy the new Wax code and restart the daemon** (worker-owned scanning,
frozen pipelines, heartbeat — the running waxd predates all of it):

```bash
cd ~/HeyMa && git pull
systemctl --user restart waxd.service
wax pipeline status        # the drain loop must be enabled for scanning
```

**3. Compile the documents pipeline** (creates the binding, unseeded — an
empty folder seeds as "0 documents" and still never processes history):

```bash
wax pipeline compile doc-standard
wax pipeline show doc-standard
```

**4. Enable and let the worker own it** (or keep it disabled and drive scans
by hand — both are supported states):

```bash
# flip enabled: true in components/wax/config/pipelines.d/doc-standard.yaml, then:
wax pipeline compile doc-standard
wax documents scan doc-standard   # seeds; afterwards the worker scans every 120s
```

**5. Install this adapter into n8n** (per Install above):

```bash
cd ~/HeyMa/components/n8n-nodes-heyma && npm run build
mkdir -p ~/.n8n/nodes && (cd ~/.n8n/nodes && npm install ~/HeyMa/components/n8n-nodes-heyma)
# restart n8n, then the Wax Pass / Wax Pipeline / HeyMa nodes appear in the editor
```

**6. Author or import graphs in n8n** (optional — YAML authoring is equally
first-class):

```bash
wax pipeline import ~/HeyMa/components/n8n-nodes-heyma/examples/wax-documents-workflow.json
```

Then watch it in Holocene's Wax tab (worker heartbeat, graph, recent runs) or:

```bash
wax documents list --state processed
wax catalog --json | jq '."pipelines"'
```

Only enabled, document-capable passes run on documents: today that is
`classification` and `title-slug`. `project-extraction` stays out of
`doc-standard` until it is enabled in the registry.
