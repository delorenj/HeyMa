# Config and deploy

The authoritative list of every environment variable Wax reads is `ENV_SPECS` in `components/wax/bin/wax`, and `wax doctor` prints each resolved value with the source that won (process environment, the running daemon's `/proc` environment, or the installed unit). Ask the doctor, not this file, for the live value. Checked 2026-11-05.

## Variables by area

Defaults are used when unset.

| Area | Variables (default) |
|---|---|
| Root and paths | `WAX_ROOT` (`~/HeyMa`; `WAX_AUDIO_ROOT` is a legacy alias), `WAX_VAULT` (`~/d/Transcripts`), `WAX_MIN_FREE_BYTES` (5 GiB), `WAX_SEGMENT_SECONDS` (60), `WAX_COMPONENT_ROOT`, `WAX_PASSES_DIR` (`<component>/config/passes.d`), `WAX_PIPELINES_DIR` |
| Transcription | `WAX_TRANSCRIBE` (pinned absolute in the unit), `WAX_DIARIZATION` (1), `WAX_DIARIZATION_DEVICE` (`cuda`), `WAX_CHANNEL_SPLIT` (1), `MAX_AUDIO_FILE_SIZE_FOR_TRANSCRIPTION` (300MB), `MAX_AUDIO_DURATION_FOR_TRANSCRIPTION` (3h), `DIARIZATION_VENV` (`.venv-diarization`) |
| Archive | `TRANSCRIBE_S3_ALIAS`, `TRANSCRIBE_S3_BUCKET`, `WAX_S3_ENDPOINT` (`https://s3.delo.sh` in the unit), `TRANSCRIBE_STASH_DIR` |
| Bus and logs | `WAX_NATS_HOST` (127.0.0.1), `WAX_NATS_PORT` (4222), `WAX_LOG_LEVEL` (INFO), `WAX_BLOODBANK_SCHEMAS` |
| Chat passes | `WAX_TITLE_*`, `WAX_SUMMARY_*`, `WAX_TICKETS_*`: `_API_BASE`, `_MODEL`, `_MODEL_FALLBACKS`, `_API_KEY_OP`, `_REQUEST_TIMEOUT_S`; `WAX_PASS_TIMEOUT_S` is set by the runner. See [passes/model-chain](passes/model-chain.md) |
| Jev passes | `WAX_JEV_URL`, `WAX_JEV_MODEL` (`jev-1.13.0`), `WAX_JEV_API_KEY_OP`, `WAX_CLASSIFICATION_MIN_P` (0.6), `WAX_PROJECT_MIN_P` (0.6, 0.75 for transcripts over 30K chars), `PJ_REGISTRY_URL` (`http://127.0.0.1:8764`), `WAX_PROJECT_ALIASES` |
| Dictionary | `WAX_DICTIONARY` (`<WAX_ROOT>/Dictionary.yaml`), `WAX_DICTIONARY_MIN_P` (0.5), `_MIN_P_HEARD` (0.4), `_MIN_P_CASE` (0.65), `_MIN_SCORE` (0.82), `_SPOKEN_DOT` (1), `WAX_DICT_COMMIT` (1). See [passes/dictionary](passes/dictionary.md) |
| Worker cadence | `WAX_SWEEP_INTERVAL_S` (900), `WAX_DOCUMENT_SCAN_INTERVAL_S` (120), `WAX_DOCUMENT_STALE_RUN_S` (900), `WAX_DOCUMENT_MAX_ATTEMPTS` (3) |
| Hygiene and PDF | `WAX_DOC_HYGIENE_FOLDERS` (`~/d/Inbox:~/d/Sandbox`), `WAX_DOC_HYGIENE_MIN_AGE_S` (300), `WAX_EMPTY_DOC_SWEEP_INTERVAL_S` (300), `WAX_UNTITLED_SWEEP_HOUR` (3), `WAX_UNTITLED_SWEEP_MAX` (50), `WAX_DOCUMENT_PDF_INTAKE` (`0` disables), `WAX_PDF_INTAKE_*`. See [documents](documents.md) |
| Vault sweep | `WAX_VAULT_SWEEP_MIN_AGE_S` (86400), `WAX_VAULT_SWEEP_TEMP_MIN_AGE_S` (604800), `WAX_VAULT_SWEEP_MAX_BYTES` (16777216), `WAX_VAULT_SWEEP_EXCLUDE` (`agents`). See [vault-sweep](vault-sweep.md) |
| Store | `WAX_TRANSCRIPTS` (1; `0` is the rollback), `WAX_TRANSCRIPTS_DSN`, `_OWNER_DSN`, `_RO_DSN`, the `*_PASSWORD_OP` family, `_TICK_S` (15), `_RECONCILE_INTERVAL_S` (600), `_SYNC_BREAKER_S` (300), `_LAG_FAIL_S` (3600), `_LOCAL_TZ` (`America/New_York`), `_UNIT_FILE`, `WAX_HARVEST_DIR`, `WAX_EMBED_URL` (`http://localhost:11434`), `_BATCH` (8), `_KEEP_ALIVE` (2m), `_TIMEOUT_S` (60). See [transcriptions-store](transcriptions-store.md) |
| Calls | `WAX_TELNYX_VOICEMAIL_HOSTS` (`voice-mail-prod.s3.us-east-1.amazonaws.com`), `WAX_CALLS_ME` (`Me`). See [calls](calls.md) |
| Tools | `WAX_PX` (`~/.local/bin/px`), `WAX_FRONTMATTERS` |
| Secrets | `OP_SERVICE_ACCOUNT_TOKEN` (needed by `op read` in passes; present in the systemd user manager environment) |

Env resolution for the store reads the process environment first, then the INSTALLED waxd unit's `Environment=` lines, so the CLI and the daemon agree.

## Secrets map

Always `op://` item-UUID references, never values, and never a file. Resolved at run time.

| What | Reference |
|---|---|
| Gateway token for title-slug, summarize, ticketable (`heyma-wax-inference`) | `op://DeLoSecrets/yeurk5dpqkaarspvsn3cjtmkki/heyma-wax-inference` |
| Jev key, project-extraction | `op://DeLoSecrets/4y3hlfp2dowbzpi6ncp67kdm5i/credential` |
| Jev key, classification | `op://DeLoSecrets/lmtarsmlr35gqyydjsw245mvra/credential` |
| Jev key, dictionary-correction | `op://DeLoSecrets/sazfdn52xd3a3uyatrbo43hiei/credential` |
| Telnyx API key | `op://DeLoSecrets/74k7nz3xkryqns4lpf65a323ue/credential` |
| Transcripts Postgres owner, app, ro | see [transcriptions-store](transcriptions-store.md) |
| n8n API, ntfy | see [n8n](n8n.md) |

Found a plaintext secret anywhere? Move it into the DeLoSecrets vault, replace it with an `op://` reference, and say so.

## Units and install

`components/wax/deploy/install-systemd-user` renders `waxd`, `wax-alert`, `wax-capture-guard` and `wax-calls-sync` from `deploy/systemd/user/` into `~/.config/systemd/user`, then runs `daemon-reload` and re-enable. It never restarts. `--dry-run` previews. Templates for `waxd.service`: `WAX_SEGMENT_SECONDS=60`, `WAX_DIARIZATION=1`, `WAX_DIARIZATION_DEVICE=cuda`, the absolute `WAX_TRANSCRIBE` pin, `WAX_S3_ENDPOINT`, password-less transcript DSNs, and `OnFailure=wax-alert.service`. The installed `waxd.service` matched its rendered template on 2026-11-05; `waxd.service.d/` is empty.

| Unit | State 2026-11-05 |
|---|---|
| `waxd.service` | enabled, active |
| `wax-capture-guard.service` | enabled, active (exited) |
| `syncthing-audio-policy.service`, `syncthing.service` | enabled |
| `wax-alert`, `wax-calls-sync`, `telnyx-s3-intake`, `telnyx-s3-intake-alert` | static (started by something else) |
| `telnyx-s3-intake.timer` | disabled on purpose; Candybar is the clock |
| `heyma-whisper.service` | not-found; a ghost in old docs |

## Candybar schedules

Candybar is the only clock. `wax-calls-sync` runs `*/10` (~300 runs, 1 failure on 2026-11-05) from `components/wax/deploy/candybar/wax-calls-sync/candybar.toml`, and `telnyx-s3-intake` runs every minute. The only systemd timer on the box is the page-only `candybar-watchdog.timer`. Never add a timer, cron or sleep loop; load the `candybar` skill before scheduling anything.

## Provisioning scripts (`components/wax/deploy/`)

| Script | Does | Default |
|---|---|---|
| `provision-inference` | Single source of truth for the `heyma-wax-inference` gateway token scope. Refuses any Anthropic route; excludes Kimi pending AAI-55; skips routes the gateway catalog lacks | Dry run; `--apply` to write. Re-run after a gateway deploy |
| `provision-transcriptions` | Creates the DeLoSecrets items, roles, database, extensions | Dry run; `--apply` |
| `install-diarization` | Builds `.venv-diarization` with uv and a CUDA smoke test (`mise run wax:diarization:install`) | Runs |
| `install-desktop-entry` | Writes the `sh.delo.HeyMa` launcher and icons | Runs; re-run after moving the checkout |
| `install-systemd-user` | Renders and enables the user units | `--dry-run` available |
| `run-document-pass` | One-off document pass over legacy notes | Dry run; `--apply` |

Never hand-edit a compiled pipeline or the gateway token scope; change the source (the YAML, the n8n export, `provision-inference`) and re-run the script or the import.

## Telephony shadow rollout

The [telephony](telephony.md) component is disabled by default, with desktop ringing opt-in. Its nonsecret config and local shadow receipts do not authorize Telnyx mutation or ASR suppression; `enforce` is refused. Do not activate a webhook, provision Jev keys, change the main-DID assignment, buy resources or alter recording as part of this release. A source commit does not reload the existing daemon/tray; any presentation restart must be coordinated separately. See `docs/telephony-shadow-contract.md` for the implementation/review boundary.

## Test isolation

The suites cannot write the real vault or the real ledger. The shell exports `TRANSCRIPTS=$HOME/d/Transcripts`, and `WAX_VAULT` and `WAX_ROOT` default to `~/d/Transcripts` and `~/HeyMa`, so an unpinned test run once put fixture notes (`a-first.ogg.md`, `b-second.ogg.md`) into the real vault, and every `bin/transcribe` a test spawns with an inherited env would do the same for a non-ephemeral run (the `tmp*.md` and `proof.md` notes came from ad-hoc agent runs that inherited `TRANSCRIPTS` the same way; see [transcription](transcription.md)). Now `components/wax/tests/conftest.py` (the wax suite) and `tests/conftest.py` (the root suite: `bin/transcribe`, the Hermes adapter) pin these to one temp sandbox before `wax` is imported, so in-process paths and every subprocess that inherits `os.environ` land there:

| Pinned | Why |
|---|---|
| `WAX_ROOT`, `WAX_VAULT`, `TRANSCRIPTS` | the ledger (`var/wax.db`), the runtime dirs, the vault, and `bin/transcribe`'s output dir |
| `WAX_DICTIONARY`, `WAX_DOC_HYGIENE_FOLDERS` (wax suite) | `Dictionary.yaml` lives in the root; the hygiene default sweeps `~/d/Inbox` and `~/d/Sandbox` |
| `TRANSCRIBE_STASH_DIR`, `TRANSCRIBE_LOCK_FILE`, `TRANSCRIBE_FAST_LOCK_FILE` (both suites) | `bin/transcribe`'s safety copies and the machine-wide transcription slot |
| `TRANSCRIBE_EPHEMERAL_DIR`, `MC_BIN`, `HERMES_TRANSCRIBE_BIN` (root suite) | scratch transcripts, S3 upload (no client to upload with), the adapter's real wrapper |

Rules for a test:

- Give it its own temp dirs and `patch.object(paths, ...)` for every path it touches: `VAR`, `DB`, `ARCHIVE`, `SKIPPED`, `INBOX`, `VAULT`. An unpatched path lands in the sandbox, never the real tree.
- A subprocess built with a **minimal** env (`env={"WAX_ROOT": ..., "PATH": ...}`) does not inherit the pins and falls back to the real vault. Carry `WAX_VAULT` and `TRANSCRIPTS` too (see `minimal_env()` in `calls_test.py`).
- Never override a pin back to a real path. An autouse fixture runs `isolation_leaks()` before every test and fails it if the vault, ledger or runtime root of the process resolve to `~/d/Transcripts`, its realpath `~/code/DeLoDocs/Transcripts`, or `~/HeyMa/var/wax.db`. A pin that is merely dropped counts too: each unset default (`~/d/Inbox:~/d/Sandbox`, `~/audio/recovered`, `~/.cache/heyma-transcribe*.lock`) is the real path. `isolation_guard_test.py` (wax) and `test_isolation_guard.py` (root) keep that check honest.
- The guard checks where the process *can write*, never what is in the real vault: waxd publishes there while tests run, so a before/after diff would false-positive.

## Shipping a change

1. Run the suite: `mise run wax:test` (or `cd components/wax && python3 -m pytest tests`). It needs no `PYTHONPATH`. The root suite (`bin/transcribe`, the Hermes adapter): `python3 -m pytest tests` from the repo root. Real-Postgres tests: `mise run wax:test:pg`. Anything over a minute goes through `heavy`.
2. **Python under `src/` or `bin/`**: restart `waxd.service`. A plain restart leaves the capture encoder scope running, so it does not cut a recording; still check `wax rec list` first and verify the worker heartbeat after.
3. **Pass YAML or env**: compiled pipelines froze the old values. Re-import both releases (`wax pipeline import ... --name transcript-standard|doc-standard`); see [pipelines](pipelines.md).
4. **Unit file**: `install-systemd-user`, then restart waxd. If `wax status` says the store ticks `disabled`, the unit changed after waxd started.
5. **Gateway scope**: `provision-inference` (dry run, then `--apply`), then `wax ep replan` stuck items.
6. Run `wax doctor` and confirm the change in the ledger. A passing test suite is not a passing pipeline.

The skill itself is guarded by `components/wax/tests/skill_hub_test.py`: it fails when a `wax` subcommand, a pass or the doctor path drifts from the hub. Fix the hub, not the test.

Related: [system-map](system-map.md), [cli](cli.md).
