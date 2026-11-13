# System map

The runtime root is `WAX_ROOT` (default `~/HeyMa`, this repo). Code and state share one filesystem, which is what makes `renameat2` between `stream/` and `inbox/` atomic. Facts below were checked against the live host on 2026-11-05.

## Layout

| Path | What |
|---|---|
| `stream/` | In-flight capture: `<rid>.segs/seg-NNNNN.ogg` plus sentinels. A concatenated `.ogg.partial` exists only while finalizing |
| `inbox/` | The one work queue. Plain local directory, recursive |
| `dropoff/` | Syncthing receive-only feed (folder id `audio`, "Transcription Inbox"). waxd only copies out of it |
| `archive/` | Audio parked after S3 verify |
| `skipped/`, `quarantine/`, `recovered/` | Preserved, never processed and never deleted. `skipped/split/<split_id>/` holds a `wax split`: the original, the held parts and `split.json`; `recovered/split-late/` holds a transcript that finished after its item was split ([capture](capture.md#splitting-a-recording-left-running-wax-split)) |
| `Dictionary.yaml` | The custom dictionary, read by `dictionary-correction` every run |
| `var/wax.db` | SQLite WAL ledger: `items`, `backups`, `transcripts`, `passes`, `outbox`, `transitions`, plus `calls`, `call_legs`, `call_recordings`, `call_events`, `documents`, `pipelines` |
| `var/state.json` | Live mirror the tray reads once a second |
| `var/waxd.sock` | Raw status socket |
| `var/waxd.singleton.lock` | The daemon's own singleton flock |
| `var/waxd.lock` | The CLI's `wax rec` lifecycle flock. Not the daemon's lock |
| `var/pipeline.enabled` | Worker toggle (`wax pipeline enable\|disable`) |
| `var/settings.json` | Operator overlay of disabled passes (empty on 2026-11-05) |
| `var/worker-heartbeat.json` | Worker freshness, read by `wax catalog` |
| `var/item-locks/`, `var/doc-locks/` | Per-item and per-document run locks |
| `var/pdf-intake/`, `var/syncthing-audio/`, `var/telnyx-s3-intake/` | Component state |
| `var/logs/` | Component logs |
| `~/d/Transcripts/` | Transcript output (`WAX_VAULT`); really `~/code/DeLoDocs/Transcripts` |
| Postgres `transcriptions`, schema `tx` | Versioned transcript store, `localhost:5432` |

Do not judge `wax.db` by its mtime: it is WAL, so the main file can be old while the `-wal` file is current. Open it read-only: `sqlite3 "file:$HOME/HeyMa/var/wax.db?mode=ro"`.

## Processes and threads

| Unit | Role |
|---|---|
| `waxd.service` (user, enabled, active) | The single owner. Parent of the encoder and every worker. `OnFailure=wax-alert.service` |
| waxd threads | Worker (inbox, passes, 900 s sweep, 120 s document scan), `wax-outbox` (drain every 10 s), `wax-transcriptions` (15 s tick), socket server |
| tray child | `python -m wax.tray_process`, a GTK 3 child of waxd. It renders from `var/state.json` once a second |
| HeyMa window | `bin/heyma`, GTK 4 and libadwaita in its own process, single-instance. It owns no state; every action is the `wax` CLI |
| `wax-capture-guard.service` | Quiesces an active capture before an orderly graphical-session stop. A plain waxd restart does not stop the encoder scope |
| `wax-alert.service` | notify-send plus ntfy on waxd failure |
| `wax-calls-sync.service` | Telnyx reconcile, run by Candybar every 10 minutes |
| `telnyx-s3-intake.service` | Telnyx S3 intake, run by Candybar every minute. Its systemd timer is disabled on purpose |
| `syncthing-audio-policy.service` | Metadata-only: accepts flat uniquely named recordings once, saves permanent exclusions |
| `holocene-api.service` | Holocene API; invokes `wax --json catalog` |

`heyma-whisper.service` appears in old docs and is `not-found` on this host. There is no Wax whisper service; waxd spawns the transcriber. (`holocene-whisper.service` does exist — Holocene's own resident faster-whisper server for car memos on :8766. It owns no Wax pipeline stage.)

## Two state machines

Both are derived from disk by a cold process, so a SIGKILL loses nothing. `wax state --cold` recomputes them.

- **Stream** (capture): `recording`, `finalizing`/`not-ready`, `error-partial`, `error` (orphan partial only), `ready`. `error-partial` comes from `uninstructed_exit`, a failed finalize recorded in `fin.json`, `finalizer_died`, or `finalize_deadline_expired` (deadline 180 s in `sentinel.py`; older docs say 45 s).
- **Inbox**: `ready-and-waiting`, `ready-and-active`, `error`, `stopped`. Syncthing private directories are excluded. Queue grace is 60 s.

Item states in the ledger: `pending`, `archived`, `transcribed`, `enrichment_pending`, `complete`, `failed`, `suspect`, `skipped`. Transitions are written by `worker.py`:

| To | Reason |
|---|---|
| `archived` | `s3_verified` |
| `failed` | `archive_failed`, `transcribe_failed`, `s3_reverify_failed` |
| `enrichment_pending` | `completion_withheld` |
| `skipped` | `operator_skip`, `split` (`wax split` halted the item; the ledger then refuses any later state change for it: table `item_fences`) |
| `transcribed`, `suspect` | set by the transcribe adapter |
| `complete` | set by finalize, once every planned pass is done |

## Tray colors

Red while recording (flashing yellow if the input is silent). Yellow for failed queue items, failed passes, degraded diarization, an outbox backlog over 50, a not-ready or error stream, an inbox error, pending work while not active, or a stopped inbox. Otherwise green. Operator-disabled passes show in the tooltip only. A green tray still proves nothing.

## Shims and pins

| Path | Points to |
|---|---|
| `~/HeyMa/bin/{wax,waxd,transcribe}` | Shims resolving `components/wax` relative to themselves (`wax` also holds Ctrl-C across Python start-up, via perl, so an early Ctrl-C exits 130 quietly) |
| `~/.local/bin/wax` | `~/HeyMa/bin/wax` |
| `~/.local/bin/transcribe` | `~/HeyMa/bin/transcribe` |
| `~/.local/bin/wax-toggle` | `components/wax/bin/wax-toggle` (GNOME `custom3`, `<Control>backslash`) |
| `waxd.service` `WAX_TRANSCRIBE` | Absolute pin; PATH never decides which code runs |

## Where things live

| Subject | Code | Design |
|---|---|---|
| Daemon, CLI, passes | `components/wax/src/wax/`, `bin/{wax,waxd}` | `components/wax/docs/WAX-DESIGN.md` |
| Pass registry | `components/wax/config/passes.d/*.yaml` | `components/wax/docs/ENRICHMENT-PASSES.md` |
| Authored pipelines | `components/wax/config/pipelines.d/*.yaml` | same |
| Transcript store | `components/wax/src/wax/transcriptions/` | `components/wax/docs/TRANSCRIPTIONS-STORE.md` |
| n8n adapter | `components/n8n-nodes-heyma/` | [n8n](n8n.md) |
| n8n releases | `components/n8n-workflows/` | [n8n](n8n.md) |
| Intake components | `components/syncthing-audio/`, `components/telnyx-s3-intake/` | [intake](intake.md) |

WAX-DESIGN is the design of record but is stale in places: the `wax reset` command, `wax pipeline start|stop`, exit code 3, the `wax-ep@` timers, a tray "NOT WORKING" note, and a claim that events are never emitted. The code wins; see [events](events.md) and [cli](cli.md).

Related: [config-and-deploy](config-and-deploy.md) for env vars and units, [capture](capture.md) for the stream machine in depth.
