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

### Wax Pipeline (`WaxPipeline`)

Encapsulates a whole graph — passes, trigger, folder bindings — behind pipeline-level CLI operations:

- **Compile** — explicitly validate and adopt authored graphs (`wax pipeline compile`); `definition_id` exposes drift until then.
- **Show** — one pipeline snapshot: trigger, nodes, edges, bindings, drift (`wax pipeline show`).
- **Scan Documents** — seed new bindings and process new arrivals only, never backfills (`wax documents scan`).
- **Run Document** — manually run a compiled pipeline over one Markdown file (`wax documents run`).

n8n is the authoring surface, the Wax CLI is the compiler, and Wax is the executor. The node never edits YAML and never activates anything: enabling a pipeline stays a Wax-side act.

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
