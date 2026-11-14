# HEYMA-18 acceptance remediation — 2026-11-14

> **Historical first-pass assessment, superseded by the independent quality review.**
> The no-implementation-gap conclusion was incorrect: review reproduced a
> killed-after-write-before-stdout note-publication race. The [quality
> remediation](quality-fix/README.md) contains the source/test/hub correction,
> a baseline-failing real-transcriber regression, and fresh verification.
> The first-pass outputs and base-revision provenance below remain historical
> evidence; they do not establish that the corrected code passed those runs.
>
> **Delivery status:** this bundle is committed on `fix/heyma-18-acceptance`;
> the independent spec and final quality gates passed within their stated
> bounds. Neither gate approves a merge or production deployment. The full
> suite is **not green** in the restricted test environment.

## Revision and isolation

- Base revision for the historical acceptance runs:
  `c47f22686f4f9d66616fe3b5328e9f22ca3eb7d5`. The source fix and
  refreshed tests were verified after that base; see `quality-fix/README.md`.
  Codex could not write Git metadata in its sandbox; the host carrier committed
  the reviewed worktree on the assigned branch. The commit handle is reported
  by `git rev-parse fix/heyma-18-acceptance` (not embedded in its own tree).
- The original acceptance run did not modify runtime source. The subsequent
  quality review found and fixed a runtime adapter defect. Archive/split/worker
  ordering remains unchanged from merged validation commit `5919928`;
  `provenance.json` records historical base blobs and reused proof blobs.
  Superseded `6d70ad2` was neither reused nor merged.
- Final integration runtime:
  `/home/delorenj/.hermes/profiles/heyma-pm/cache/scratch/a185BcL`.
- Independent clean test runtime:
  `/home/delorenj/.hermes/profiles/heyma-pm/cache/scratch/h18OFmN`.
- Initial failed setup attempts, retained without deletion:
  `/home/delorenj/.hermes/profiles/heyma-pm/cache/scratch/heyma18-native-20261114-FCDzPS`.
- Progress:
  `/home/delorenj/HeyMa/agents/hermes/pm/runtime/workers/HEYMA-18/krebs-20261114/progress.md`.

All new Wax commands use isolated HOME, root, ledger, vault, voices, temp
directory, lock and socket paths beneath the PM profile scratch root.
`outputs/00-environment.json` and `clean-env` retain the explicit allowlists;
`env -i` removes PYTHONPATH, PYTHONHOME, VIRTUAL_ENV, credentials, display and
session variables. No production audio, ledger, S3, vault or service was
mutated, no recording device opened, and no board write, deployment, timer,
merge or push was performed.

## Six-row AC matrix

The paths below are real retained files in this worktree; copied command
outputs still name the original scratch artifacts, which remain present.

| AC | Assessment | Absolute evidence paths and observation |
|---|---|---|
| 1 | Proven for the deterministic representative acoustic fixture | `/home/delorenj/HeyMa/.worktrees/heyma-18-acceptance/components/wax/docs/evidence/heyma-18/acceptance-2026-11-14/outputs/01-fixtures.json`; `/home/delorenj/HeyMa/.worktrees/heyma-18-acceptance/components/wax/docs/evidence/heyma-18/acceptance-2026-11-14/outputs/03-quiet-split.json`. Default CLI detects quiet beginning at 635 s, cuts at 640 s; no ASR or pass was started during waveform cases (`05-waveform-checks.json`). |
| 2 | Proven with labelled input/scope/transcriber seams | `/home/delorenj/HeyMa/.worktrees/heyma-18-acceptance/components/wax/docs/evidence/heyma-18/acceptance-2026-11-14/outputs/24-live-split.json`; `/home/delorenj/HeyMa/.worktrees/heyma-18-acceptance/components/wax/docs/evidence/heyma-18/acceptance-2026-11-14/outputs/12-slow-verified.json`. Live recording → ready; processing archived → skipped. Full originals retained and hashed. Early live exit 3 leaves recording active (`22-live-no-boundary.json`). |
| 3 | Proven for targeted halt/settlement and late-result protection | `/home/delorenj/HeyMa/.worktrees/heyma-18-acceptance/components/wax/docs/evidence/heyma-18/acceptance-2026-11-14/outputs/12-stubborn-verified.json`; `/home/delorenj/HeyMa/.worktrees/heyma-18-acceptance/components/wax/docs/evidence/heyma-18/acceptance-2026-11-14/outputs/12-passslow-verified.json`. Slow child halted, stubborn child's late note parked, in-flight pass skipped/item_split; no target completion receipt. The isolated runner checks exactly three targeted fences, no tracked children, and an unrelated complete item. Its full ledger snapshots are deliberately excluded from the tracked bundle. |
| 4 | Fresh preservation/provenance; actual S3 ordering reuses unchanged committed proof | `/home/delorenj/HeyMa/.worktrees/heyma-18-acceptance/components/wax/docs/evidence/heyma-18/acceptance-2026-11-14/outputs/25-live-verified.json`; `/home/delorenj/HeyMa/.worktrees/heyma-18-acceptance/components/wax/docs/evidence/heyma-18/validation-2026-11-13/12-scenario-submit-s3-order.txt`. Fresh manifests and segment checks retain source/part hashes and requested ranges. Actual MinIO PutObject → HeadObject → verified backup → transcription ordering is historical proof from `5919928`, not a new S3 run. |
| 5 | Proven for the four required acoustic cases; real-world usefulness remains limited | `/home/delorenj/HeyMa/.worktrees/heyma-18-acceptance/components/wax/docs/evidence/heyma-18/acceptance-2026-11-14/outputs/04-continuous_activity.json`; `/home/delorenj/HeyMa/.worktrees/heyma-18-acceptance/components/wax/docs/evidence/heyma-18/acceptance-2026-11-14/outputs/04-all_quiet.json`; `/home/delorenj/HeyMa/.worktrees/heyma-18-acceptance/components/wax/docs/evidence/heyma-18/acceptance-2026-11-14/outputs/04-meeting_with_pauses.json`. All exit 3, no boundary/cut or source change; clear trailing quiet exits 0. No semantic meeting inference or real-recording recall claim. |
| 6 | Historical command/core regression evidence complete; subsequent fix and independent gates recorded separately | `tests/focused-core.txt` and `tests/failure-classification.json` summarize the base-revision runs. Base core: 64 passed, exit 0; base requested focused: 71 passed/10 socket failures, exit 1; base full: 1190 passed/28 failed/14 errors/22 skipped, exit 1. Raw verbose failures remain in the external worker spool/scratch and were excluded from the bounded tracked bundle. Corrected-source results and independent reviews are recorded in `quality-fix/README.md` and `reviews/`. |

## Fixture characteristics and observed boundaries

`tests/split_fixtures.py` at the base revision synthesizes seeded speech-like
harmonic bursts and breaths over a room-noise bed; these are **not recordings
of human meetings**. Seed 18, mono PCM 16 kHz, then actual ffmpeg Opus/Ogg
48 kHz mono, 32 kbps, matching Wax's capture format. The PCM generation is
deterministic; an Ogg container's identifier need not be repeatable across
encodes. Exact observed file hashes/bytes are in `01-fixtures.json`.

The representative timeline (seconds) is:

```
talk 120, pause 45, talk 150, pause 90, talk 140, pause 30, talk 60, quiet 1500
```

Speech level is -24 dBFS, room bed -62 dBFS; total 2135 s (35m35s).
There are 45/90/30-second ordinary within-meeting pauses, and the clear
25-minute quiet overrun begins at 635 s. With **unmodified defaults**
(minimum quiet 300 s, minimum activity 30 s, pad 5 s, minimum drop 20 dB):

| Case | Duration | Actual exit/outcome | Observation |
|---|---:|---|---|
| Representative meeting + quiet overrun | 2135 s | 0 proposed; 0 split | Quiet starts 635 s, cut 640 s; requested ranges [0,640] and [640,2135], actual part durations 640.020 s and 1495.006 s. |
| Continuous speech-like activity | 1800 s | 3 / no_contrast | No cut, original hash unchanged. |
| All quiet, -66 dBFS bed | 1800 s | 3 / no_activity | No cut, original hash unchanged. |
| Same meeting with ordinary pauses and 40-second trailing quiet | 675 s | 3 / no_sustained_quiet | No cut, original hash unchanged. |

The negative cases compare manifests before/after the command and verify source
hashes; no split is produced. Fixture items are explicitly registered as
complete for CLI resolution without a worker or transcript; that is a harness
setup seam, not evidence that they were previously transcribed. The current
CLI does not resolve arbitrary unregistered paths outside `inbox/`.

## Actual capture, worker, and late-result evidence

`run-acceptance.py` runs the actual CLI and actual `bin/waxd`, SQLite ledger,
worker, pass runner, ffmpeg encoder/decode/cut and finalization code.
It copies/adapts the committed 2026-11-13 harness into scratch before use;
none of its `/tmp` defaults is used.

Labelled seams:

- **Audio input:** the ffmpeg seam swaps only Pulse input for the prerecorded
  fixture at 20× media speed. No microphone or Pulse server is opened.
- **Systemd scope:** logging/exec stub, no actual service/scope mutation.
- **Transcriber:** slow, stubborn-after-TERM, and fast shell stubs; no Whisper,
  ASR service, LLM or Anthropic pass runs.
- **Pass/frontmatter:** one no-model `fx-stamp` shell pass and logging
  frontmatters stub. No shared pass config is edited.
- **Archive transport:** a local-file `mc` seam, patched only inside the
  isolated daemon launcher. Actual archive.py size/MD5 verification and
  backup/state bookkeeping run, but **no S3 request is made**.
- NATS points at closed loopback port 1; Postgres has no DSN; notifications are
  stubs. No display/session exists.

The isolated waxd (PID 603) advertises `item-fence/1` in `state.json`. Unix
socket creation is denied by the platform, and the daemon explicitly logs its
supported mirror fallback; the split capability guard exercises that fallback.
The slow/stubborn transcriber PIDs 746/1321 and in-flight pass PID 1810 have
PPID 603 in `/proc`. Target states are archived → skipped (slow/stubborn) and
transcribed → skipped (pass). Late transcript output is retained under
`recovered/split-late/`, outside the isolated vault; the pre-fence transcript
for the pass case remains in the vault as documented. The unrelated queue
item completes normally, is not fenced, and its audio is parked, not deleted.

**Ownership limitation:** the live encoder PID 2060 has PPID 1 under the
no-systemd seam (`21-live-encoder-owner.json`). Current `capture.start` forks
from the CLI. This verifies one isolated waxd processing owner and its tracked
worker children, **not** the literal production encoder-parent invariant in
AGENTS.md. Production service/cgroup ancestry was not probed or repaired.
No second watcher, scheduler or worker was introduced.

Live capture first returns exit 3 and remains recording. Dry run then proposes
640 s and remains recording. Applying it orderly-stops/finalizes and returns
recording → ready, no restart, no unsplit inbox item, two held parts and the
complete original. All 162 segments were used; none skipped.
Independent ffprobe reports original **1612.313 s**, parts **640.0095 s** and
**972.3065 s** (sum **1612.316 s**, +0.003 s). The detector's decoded timeline
is 1611.254 s; timestamps/codec-page edges are approximate, not sample-accurate.
The complete original checksum is retained independently of those cut edges.

## Reused proof, not fabricated fresh results

Actual S3 proof is the unchanged, already-committed
`validation-2026-11-13/12-scenario-submit-s3-order.txt` from `5919928`.
It records a part submission copying into inbox, then:

```
20:41:47.701613893Z MinIO PutObject
20:41:47.716920234Z MinIO HeadObject
20:41:47.720Z       backups row: verified-attempt-1/size+md5
20:41:47.755Z       archived transition: s3_verified
20:41:47.861258047Z transcriber START
```

The part's held copy and complete original both remain present in that proof.
`provenance.json` verifies the exact existing file bytes against `git show
5919928:<path>` and records its blob and SHA-256. Relevant runtime source files
have no diff between `5919928` and the tested base, so that ordering contract
has not been replaced by this evidence pass. No new part was submitted to S3.
Prior real socket/old-daemon coverage is retained in the same committed
validation bundle; its split suite reports 69 passed / 13 subtests passed.

## Commands, exits, and full-suite limits

Exact Python executable used:

```
/home/delorenj/.hermes/profiles/heyma-pm/cache/scratch/h18OFmN/py/bin/python3
```

This scratch-only Python 3.13.7 venv uses system-site-packages and read-only
symlinks to already-installed pytest 9.0.3 pure Python modules/dependencies.
Nothing was installed/downloaded or changed in a shared environment. Isolated
HOME initially lacked the static Bloodbank intake schema; only exact copies of
the intake/common JSON schema test inputs were added there (source and copy
hashes in `provenance.json`). No service or credentials were copied.

From the worktree root:

```sh
/home/delorenj/.hermes/profiles/heyma-pm/cache/scratch/a185BcL/clean-env \
  /home/delorenj/.hermes/profiles/heyma-pm/cache/scratch/h18OFmN/py/bin/python3 \
  components/wax/docs/evidence/heyma-18/acceptance-2026-11-14/run-acceptance.py
# exit 0; outputs/acceptance.txt and outputs/*.json
```

From `components/wax`, each command was prefixed with:

```
/home/delorenj/.hermes/profiles/heyma-pm/cache/scratch/h18OFmN/clean-env \
/home/delorenj/.hermes/profiles/heyma-pm/cache/scratch/h18OFmN/py/bin/python3 -m pytest
```

| Arguments | Exit | Actual result / retained output |
|---|---:|---|
| `tests/split_test.py tests/split_live_test.py tests/split_fence_test.py tests/split_daemon_guard_test.py tests/skill_hub_test.py tests/worker_heartbeat_test.py -q` | 1 | 71 passed, 10 failed, 13 subtests passed; socket failures classified in `tests/failure-classification.json` (raw verbose output remains in external spool). |
| `tests/split_test.py tests/split_live_test.py tests/split_fence_test.py tests/skill_hub_test.py tests/worker_heartbeat_test.py -q` | 0 | 64 passed, 13 subtests passed; `tests/focused-core.txt`. This is a separate targeted run, **not** a green substitute for the requested suite. |
| `tests -q` (before static schema inputs) | 1 | 1167 passed, 51 failed, 14 errors, 22 skipped; raw output remains in external spool. |
| `tests -q` (with static schema inputs) | 1 | 1190 passed, 28 failed, 14 errors, 22 skipped, 614 subtests passed; `tests/failure-classification.json` records classified failures. |
| `tests/skill_hub_test.py -q` (final documentation check) | 0 | 8 passed; `tests/hub-final.txt`. |

Final artifact verification (`outputs/32-final-artifact-verification.json`)
exits 0: all five retained splits still have their complete original and both
part hashes; all 14 absolute matrix file references exist; every retained JSON
file parses. `git diff --check` exits 0.

`tests/failure-classification.json` enumerates all **42** final non-passing
test reports: **41 socket EPERM** (including 14 setup errors), **1 unavailable
systemd user manager**. These are observed tracebacks, not waived failures.
They require a permitted test environment for an independent green full gate.
No network restriction was bypassed and no tests were rewritten or hidden.

Earlier attempts are recorded in the progress spool: system Python without
pytest (exit 1); Python 3.14 without NumPy (collection exit 2); Python 3.12
(socket pathname too long and existing Thread._stop compatibility failures);
and harness-only setup/query corrections. The first integration daemon
exited 0 before the explicitly announced fresh-root rerun. This was not a
production service restart.

## Limits and remaining work

- This waveform heuristic detects **trailing acoustic quiet**, not the meaning
  or end of a meeting. Ordinary pauses in the deterministic fixture do not
  fire; sparse quiet speech or noisy post-meeting activity remain ambiguous.
- Preserve the prior sweep honestly: **0/15 likely overruns found; one apparent
  false positive among 80 recordings**, tuned in-sample. ASR-derived
  end-of-meeting estimates are unreliable ground truth. This pass did not
  repeat that production sweep, relabel it, claim 100% recall, or require
  human meeting labels for acceptance.
- Exit 3 is an explicit non-boundary result. On the live path a post-stop
  authoritative recheck can release the complete unsplit recording to inbox;
  initial no-boundary detection does not stop the capture.
- Parts are held; the unsplit original is not automatically uploaded by split.
  Codec-page boundaries are approximate. Published pre-fence transcripts
  remain; late results are parked. These are existing documented policies.
- Rerun the full/daemon-socket tests on an environment permitting local sockets
  and a systemd user-manager verification context. Production encoder
  ancestry remains unverified here. Independent spec and final quality reports
  are retained under `reviews/`; neither is merge/deploy approval.
- The host carrier committed this evidence and fix on the named feature branch
  after Codex's read-only metadata sandbox prevented staging. Use
  `git rev-parse fix/heyma-18-acceptance` for the verifiable commit handle;
  the commit is not merged, pushed, or deployed.
- Manual cut-time support and held-out semantic tuning remain possible
  follow-ups, not newly implemented features or automatic AC blockers.

Changed paths: this `acceptance-2026-11-14/` evidence bundle,
`components/wax/src/wax/transcribe_adapter.py`, four affected Wax test files,
`components/wax/docs/WAX-SPLIT.md`, and the hub's cli/capture/transcription
references. No board closure, merge, push, deployment, or production write
was performed. Independent reviews are bounded to their recorded scopes.
