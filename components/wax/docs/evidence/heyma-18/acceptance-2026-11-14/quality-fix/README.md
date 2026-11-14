# HEYMA-18 QUALITY HOLD remediation

**Fix implemented, independently quality-reviewed PASS, and committed on
`fix/heyma-18-acceptance` by a host carrier.** The earlier read-only Git
metadata failure was confined to the Codex sandbox. Neither the independent
quality/spec gates nor this commit approve merging or deployment. No board
write, merge, push, service restart or deployment was made.

Base revision: `c47f22686f4f9d66616fe3b5328e9f22ca3eb7d5`.
The reviewed source/test/doc commit can be verified with
`git rev-parse fix/heyma-18-acceptance`; its SHA cannot be embedded in its
own tree. The independent final quality report is
[`../reviews/final-quality.json`](../reviews/final-quality.json).

## Confirmed defect and bounded correction

PM's `quality-review.md` is correct: the old adapter parks a late note only
after successful exit and stdout. Actual `scripts/transcribe.py` writes the
note before metadata/`Written:` and the shim's stdout. A split can kill the
child during that window and leave an unsplit, untracked vault note.
The earlier acceptance-only "no gap found" conclusion is superseded.

`components/wax/src/wax/transcribe_adapter.py` now:

1. Allocates a unique, mode-0700 `logs/<item>/output-*/` directory **before**
   spawning the child. The existing `--output` interface supplies a private
   note path; child-only `TRANSCRIPTS` and `WAX_VAULT` point there as well.
   No process-global environment is modified.
2. Preserves that exact owned directory under
   `recovered/split-late/output-*/` in a `finally` block on **any fenced exit**,
   including TERM/nonzero/no stdout and timeout/exception exits. Cleanup
   neither scans the vault nor trusts stdout to identify a note to remove.
3. Refuses stdout paths outside that private directory (including symlink
   escapes), rather than adopting/moving a preexisting note.
4. Runs its last fence check, no-clobber vault rename, transcript ledger write
   and state transition in a short SQLite `BEGIN IMMEDIATE` transaction.
   Fence insertion cannot race between that check and publication. The store
   capture hook remains outside the write transaction.
5. Keeps the existing no-clobber rename policy. Failure to publish retains
   private output and fails, instead of falsely treating a private note as
   published. Cross-filesystem vault renames therefore fail closed.

No change to `scripts/transcribe.py` was needed: it already supports
`--output`. Standalone invocations remain unchanged. Custom `WAX_TRANSCRIBE`
implementations must honor the private output destination; deliberately
hardcoded vault writes are not supported or swept afterward.

## Baseline-failing, real-writing regression

`tests/split_fence_test.py::SplitWhileProcessingTest::
test_real_cli_kill_after_write_before_stdout_preserves_only_owned_output`
uses the **actual scripts/transcribe.py main/write_text code**, with only ASR
replaced by deterministic data. It blocks at the first metadata log, after
the note write but before the shim could print stdout. A real
`bin/wax --json split` kills the child while the real worker owns the item.

- Baseline adapter from `git show HEAD:components/wax/src/wax/transcribe_adapter.py`,
  loaded only into the test process: **exit 1, 1 failed/22 deselected**.
  `actual-writer-before.txt` shows the extra `vault/meeting.md`, matching PM's
  defect. No checkout/source flip was used.
- Fixed adapter: **exit 0, 23 passed**, including this regression.
  `actual-writer-after.txt` retains the output.
- Assertions prove no stale/duplicate vault note, byte-preserved preexisting
  dated and unrelated notes, preserved exact child output in recovery, skipped
  original with matching audio hash, two split parts, no transcript ledger
  receipt for the fenced item, and an untouched/unfenced unrelated queue item.
- Additional regressions fence during sanity before publication, serialize a
  competing fence writer against publication, and refuse a stdout claim on a
  preexisting outside note.

The original PM repro hardcodes the vault path in its fake executable and
ignores `--output`. It is retained unchanged. The new regression uses the real
transcriber's existing output interface and the same write-before-stdout
termination ordering; it does not weaken the race or rely on a broad sweep.

## Fresh commands and outputs

Runtime roots (all principal verification under profile scratch):

```
tests:       /home/delorenj/.hermes/profiles/heyma-pm/cache/scratch/q18D6GS
integration: /home/delorenj/.hermes/profiles/heyma-pm/cache/scratch/f18nDtY
python:      /home/delorenj/.hermes/profiles/heyma-pm/cache/scratch/h18OFmN/py/bin/python3
```

Each test command below runs from `components/wax` with this exact prefix:

```sh
/home/delorenj/.hermes/profiles/heyma-pm/cache/scratch/q18D6GS/clean-env \
 /home/delorenj/.hermes/profiles/heyma-pm/cache/scratch/h18OFmN/py/bin/python3
```

`clean-env` uses `env -i`; PYTHONPATH/PYTHONHOME/VIRTUAL_ENV are unset.
HOME/root/vault/voices/locks/socket/temp paths are isolated. Static schema
inputs are copies from the earlier isolated setup, not a shared config repair.

| Command suffix | Exit / actual result | Evidence |
|---|---|---|
| `.../q18D6GS/run-baseline.py` | 1; 1 failed, 22 deselected | `tests/actual-writer-before.txt` |
| `-m pytest tests/split_fence_test.py -q` | 0; 23 passed | `tests/actual-writer-after.txt` |
| `-m pytest tests/split_test.py tests/split_live_test.py tests/split_fence_test.py tests/skill_hub_test.py tests/worker_heartbeat_test.py tests/transcriptions_capture_test.py tests/transcribe_scratch_guard_test.py tests/portability_test.py -q -k 'not test_systemd_render_handles_spaces_and_special_characters'` | 0; 103 passed, 1 deselected, 15 subtests passed | `tests/focused-core.txt` |
| `-m pytest tests -q` (final) | 1; 1194 passed, 28 failed, 14 errors, 22 skipped, 614 subtests passed | `tests/full-final-classification.json`; raw output retained in external worker spool |
| Same focused selection plus `tests/split_daemon_guard_test.py`, without `-k` | 1; 110 passed, 11 failed, 15 subtests passed (10 socket EPERM, 1 systemd user manager) | failure classification in external worker spool |
| `-m pytest tests/skill_hub_test.py -q` (final) | 0; 8 passed | `tests/hub-final.txt` |

Full final traceback classification has **41 socket EPERM reports and one
unavailable systemd user manager**, zero unclassified (`full-final-classification.json`).
The failures are retained, not waived or represented as green.
Parent's separate Python 3.12 baseline full run also remains in the external
spool (27 failed/1227 passed); it is not presented as this fix's verification.
Earlier Python/interpreter/environment failures remain historical evidence.

From the worktree root, the fresh integration command is:

```sh
$QUALITY_INTEGRATION_ROOT/clean-env \
 /home/delorenj/.hermes/profiles/heyma-pm/cache/scratch/h18OFmN/py/bin/python3 \
 components/wax/docs/evidence/heyma-18/acceptance-2026-11-14/run-acceptance.py
```

**Exit 0**, `integration/acceptance.txt`. Actual isolated waxd/CLI/ffmpeg,
three targeted slow/stubborn/pass-in-flight splits, late private note recovery,
unrelated completion, early live exit 3, live dry-run and live split all pass.
Audio input, systemd scope, ASR/pass and local object transport remain labelled
seams; this is not new S3 proof or production encoder-ancestry proof. Historical
actual MinIO ordering proof and the honest 0/15 likely-overrun recall/one
apparent false positive among 80 remain unchanged.

### Disclosed ancillary isolation exception

An optional root-suite command,
`-m pytest tests/test_transcribe_contract.py tests/test_transcribe_scratch_output.py -q`,
was launched before its old scratch-output test's explicit `/tmp` and
`/var/tmp` fixture roots were inspected. It completed **29 passed/22 subtests,
exit 0** before an attempted stop found no matching process (no signal sent).
It used synthetic test files, not production data, but violated the requested
profile-scratch-only fixture placement. Its raw output is retained and
**excluded from compliant isolation proof**. The subsequently isolated
`-m pytest tests/test_transcribe_contract.py -q` reports **7 passed, exit 0**.
The new regression and the affected Wax scratch-guard fixture respect TMPDIR.

## Changed source/test/docs and handback

- Source: `components/wax/src/wax/transcribe_adapter.py`.
- Tests: `split_fence_test.py`, `transcriptions_capture_test.py`,
  `transcribe_scratch_guard_test.py`, `portability_test.py`.
  Existing mocks now model private output; they do not suppress assertions.
- Docs: `components/wax/docs/WAX-SPLIT.md`,
  `skills/heyma/references/{cli,capture,transcription}.md`.
- Evidence runner's late-file lookup supports the private recovery subdirectory;
  historical raw outputs were not rewritten.

External durable report:
`/home/delorenj/HeyMa/agents/hermes/pm/runtime/workers/HEYMA-18/krebs-20261114/quality-fix-report.md`.
That report predates the host carrier commit and final independent quality
PASS; the corrected disposition and review results are recorded above and
in `../reviews/`. Integration into main remains for a separate authorized
executor; no merge, deployment, or board change is implied.
