# HEYMA-18 integration proof (2026-11-17)

Base `9c02aaaeeff09ab070a2ecc4252e41860286da1f` (`origin/main`); saved repair `5bb504edd286d5048e97877baca1ea2eb7cf1e64` selectively applied to its owned adapter/tests/docs and reconciled with main's newer junk gate (`4621427`) and thread shutdown repair (`bacc813`). No audio, vault, production ledger, S3 or service was modified. This is a review branch, not a deployed change.

`run-acceptance.py` is the saved acceptance harness, re-executed with Python 3.12, isolated `HOME`, `TMPDIR`, `WAX_ROOT`, vault and voices under the PM profile scratch. Its fake microphone/systemd scope, local object transport, stub transcriber and pass do **not** constitute production S3 or production ASR proof. `proof.json` is a bounded extraction of the actual output, not a synthetic CLI response. Full raw evidence remains at `/home/delorenj/.hermes/profiles/heyma-pm/cache/scratch/h18i/accept/integration/logs/`; the nonsecret command-output/manifest provenance for the earlier verified-S3 seam is in `../validation-2026-11-13/README.md`. All acceptance assertions returned exit 0 (`ACCEPTANCE_ASSERTIONS_PASSED`).

| AC | Evidence / disposition |
|---|---|
| 1 | Fixture recipe and SHA-256 in `proof.json` / `run-acceptance.py`; `wax --json split` selects 640 s from a 2135 s meeting + 1500 s quiet tail, with 0–640 and 640–2135 s held ranges. Neither transcriber nor pass was started to choose the boundary. |
| 2 | Scratch daemon CLI: an active capture stays `recording` when no boundary (exit 3), then `recording` → `ready` after its final split; processed item `archived` → `skipped` after targeted fence; source hashes and held segments verified. |
| 3 | Three real scratch CLI processing cuts halt tracked transcriber/pass, wait for item settlement and leave unrelated item `complete`; stubborn late output is parked outside vault. `split_fence_test.py` checks kill-after-write-before-stdout, serialization, no duplicate publication, no-clobber, and combined junk rejection + split. |
| 4 | Source SHA and segment SHA/ranges verified in manifest/ledger. Held outputs do not auto-transcribe; the worker's archive-then-verified-backup contract is unchanged. This run uses a local object transport seam, **not** a fresh remote S3 upload; see prior independent S3 proof. |
| 5 | Clear long quiet splits; continuous activity `no_contrast`, all quiet `no_activity`, ordinary pauses `no_sustained_quiet` all exit 3 without a cut. Detector limitations remain explicit in `WAX-SPLIT.md`, including historical 0/15 likely real overrun recall and one false cut in 80. |
| 6 | Hub CLI/capture/transcription references updated with behavior. Focused combined test, real CLI output and full suite exits below, not status indicator evidence. |

## Gates

All test runs use `env -i`, Python `/home/delorenj/.local/share/mise/installs/python/3.12/bin/python3`, `-p no:cacheprovider`, isolated scratch runtime; `WAX_BLOODBANK_SCHEMAS` points read-only to the existing schema location. The systemd verifier needed the existing user `XDG_RUNTIME_DIR=/run/user/1000` and `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`. No service was restarted.

- `components/wax`: `python3 -m pytest -q -p no:cacheprovider tests` → **1398 passed**, exit 0 (`full-wax-final.log/.exit`). Initial run without user bus: 1396 passed, 1 systemd verifier failure, exit 1; independently rerun with user bus: 1 passed, exit 0.
- `components/wax`: split fence + heartbeat + hub → **37 passed**, exit 0 (`heartbeat-fence.log/.exit`). Full combined adapter/junk/split focused set → **112 passed**, 1 systemd test deselected, exit 0 (`focused-final.log/.exit`); systemd test separately passed.
- Root suite: `python3 -m pytest -q -p no:cacheprovider tests --ignore=tests/test_codex_gateway.py` → **119 passed**, exit 0 (`root-isolated.log/.exit`); the isolated HOME initially lacked the gateway owner/catalog used at test collection, then read-only symlinks into scratch exposed those prerequisites and `tests/test_codex_gateway.py` → **7 passed**, exit 0 (`root-owner.log/.exit`). These are disjoint, giving 126 root tests verified without mutating other repos.
- Fresh scratch CLI harness → exit 0; 11 `wax --json split` outcomes recorded in `proof.json`, plus manifest, fixture, worker ownership and ledger assertions.

Review boundary: no main merge, deployment, Plane changes or human label decisions. Historical real-world recall remains a documented limitation, not a claim of production accuracy.
