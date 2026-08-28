# n8n-nodes-heyma

A private n8n community-node package for controlling HeyMa's Wax audio pipeline.

Wax remains the single owner of recording, archiving, transcription, and enrichment. This node is a thin control client: it invokes the canonical `wax` CLI, which keeps capture serialization, sentinels, and state transitions inside Wax.

## Operations

The `Recording` resource currently supports:

- **Start Recording** — starts a Wax capture with an optional label and Opus bitrate.
- **Stop Recording** — stops the active capture, or a specific capture ID, and returns the finalized inbox path and audio metadata.

Both operations execute without a shell. The node requires absolute paths for both the Wax executable and `WAX_ROOT`, and parses Wax's JSON response into the n8n item output.

## Requirements

- Self-hosted n8n running on the same Linux host and user session as Wax.
- Node.js 22 or newer.
- An active `waxd.service` and an absolute Wax shim path (normally `/home/delorenj/HeyMa/bin/wax`).
- Access to the host's PulseAudio/PipeWire and systemd user session.

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

Restart n8n after installation. In the editor, add **HeyMa**, choose `Recording`, then choose `Start Recording` or `Stop Recording`.

## Configuration

The node defaults to the production paths on this host:

- **Wax Executable:** `/home/delorenj/HeyMa/bin/wax`
- **Wax Root:** `/home/delorenj/HeyMa`
- **Timeout:** 30 seconds for start; 3700 seconds for stop/finalize
- **Execute Once:** enabled

`Execute Once` prevents a multi-item input from accidentally issuing the same lifecycle action more than once. Disable it only when intentionally stopping several explicit capture IDs.

## Output

Start Recording returns Wax fields such as `started`, `target`, `source`, and `pid`. Stop Recording returns fields such as `rid`, `path`, `duration_s`, `bytes`, and segment counts. Every output also includes `operation` (`start` or `stop`) and preserves n8n item pairing.

## Compatibility

Developed and validated against n8n 2.18.4 and the `@n8n/node-cli` 0.44.4 toolchain.
