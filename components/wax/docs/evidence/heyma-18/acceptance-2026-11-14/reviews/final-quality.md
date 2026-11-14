# HEYMA-18 final independent quality gate

**Verdict: PASS — bounded quality scope only. No findings. No merge approval.**

Reviewed 2026-11-14, 08:15–08:18 EST. Worktree: `/home/delorenj/HeyMa/.worktrees/heyma-18-acceptance`; HEAD `c47f22686f4f9d66616fe3b5328e9f22ca3eb7d5` plus uncommitted changes. This supersedes the absent-vault HOLD in `quality-rereview.json`, not the parent's broader acceptance gate.

## Correction and safety

- **QRR-1 resolved.** `components/wax/src/wax/transcribe_adapter.py:341–350` now creates the configured vault and missing parents with `mkdir(parents=True, exist_ok=True)`, inside `_publication`, after the sanity and final fence checks and before no-clobber publication. The fresh absent-vault regression creates `configured/new/vault`, publishes one note, records exactly its transcript row, and leaves the item `transcribed` (`tests/split_fence_test.py:330–351`).
- Private staging is still allocated before spawn; `TRANSCRIPTS`, `WAX_VAULT`, and explicit `--output` still target the private attempt. Fence recovery still moves only that attempt's directory. The stdout path confinement check and SQLite publication/fence serialization remain intact (`transcribe_adapter.py:201–233,266–295`).
- **AC3 maintained in the tested paths:** real CLI split kills the already-written, pre-stdout transcriber; worker returns fenced; no new unsplit vault note or transcript row appears; the owned note is recovered identically; exact-name and unrelated preexisting notes and an unrelated pending item's audio/state/fence remain untouched. A second test fences during sanity; a third confirms the fence INSERT waits for publication's SQLite transaction.
- **AC4 maintained in the tested paths:** kill-before-stdout regression asserts original audio hash unchanged and two split segments. Independent collision probe also confirms original audio hash unchanged. `worker.py`, `archive.py`, and `split.py` have no uncommitted diff; worker still archives before invoking the adapter (`worker.py:334–356,419–425`). No production S3 upload or verification was rerun or claimed.

## Fresh isolated execution

Python: `/home/delorenj/.local/share/mise/installs/python/3.12/bin/python3` (3.12.11); pytest 7.4.4. Both invocations used `env -i`, `PATH=/home/delorenj/.local/share/mise/installs/python/3.12/bin:/usr/bin:/bin`, isolated HOME/root/vault/TMPDIR under the short profile-scratch directory `/home/delorenj/.hermes/profiles/heyma-pm/cache/scratch/ftVGz`, `PYTHONDONTWRITEBYTECODE=1`, and `WAX_BLOODBANK_SCHEMAS=/home/delorenj/code/33GOD/bloodbank/schemas`. No credentials inherited. Full reproducible environment and arguments are in the companion JSON.

From `components/wax`:

```text
python3 -m pytest -p no:cacheprovider tests/split_fence_test.py -vv -k 'publication_creates_an_absent_configured_vault or real_cli_kill_after_write or fence_during_sanity or publication_and_fence' --junitxml=$S/tests.xml

test_fence_during_sanity_parks_private_note_before_publication PASSED
test_publication_and_fence_insert_are_serialized PASSED
test_publication_creates_an_absent_configured_vault PASSED
test_real_cli_kill_after_write_before_stdout_preserves_only_owned_output PASSED
4 passed, 20 deselected in 5.97s
exit 0
```

Independent fifth check, from `/home/delorenj/HeyMa`: explicit Python above running `$S/collision-probe.py`, **exit 0**. It uses the actual repository-root `bin/transcribe` and `scripts/transcribe.py` parser/writer with ASR-only seam. Exact-name collision publishes `20261114-081720-meeting-1.md` without altering the existing exact-name or unrelated note; vault count 3, transcript rows 1, private notes 0, original SHA256 unchanged, store hook called once. The four pytest tests use real ledger/filesystem/process handling, the actual CLI implementation, and ASR/archive/desktop/model seams; they do not exercise production ASR or S3.

`git diff --check`: **exit 0**. `git diff --exit-code -- components/wax/src/wax/worker.py components/wax/src/wax/split.py components/wax/src/wax/archive.py`: **exit 0**.

## Reviewed identities and evidence

Hashes verified unchanged before and after these tests:

| File | SHA256 |
|---|---|
| `components/wax/src/wax/transcribe_adapter.py` | `505cb3c7bd435eb82e89d54fe565074af2e276dd9b42407a690b1ef4bec58109` |
| `components/wax/tests/split_fence_test.py` | `84e098935f5da67b95dc77e15146d0a861a946e9129443e327e973011b5cdc19` |
| `bin/transcribe` | `c0e787ef956e82e61858ddbc596e252ccad64c1b0baa393e3b1a3d0aa7f76b60` |
| `scripts/transcribe.py` | `2454f2653cc40e469abc2cfc8838c470c0fbbe5808527588d39e829d24fae889` |

Raw logs, JUnit, probe source, and collision JSON are in `$S`; exact results and hashes are also embedded durably in `final-quality.json`, since scratch is temporary.

## Boundaries and writes

No new defect found in this correction. No full-suite rerun, live capture, real S3, production ASR, remote SSH transcribe, or crash-atomicity claim. Broader original-feature spec PASS and previously documented detector recall limitations remain the parent's evidence, not fresh findings from this bounded check. Hub tool loading was quarantined; tracked hub/router and capture route were read directly without changing trust settings.

Created only `final-quality.md`, `final-quality.json`, and isolated scratch probe/log/runtime artifacts. No source, production, board, service, or Git-metadata writes. Parent retains the final acceptance/carrier/merge decision.
