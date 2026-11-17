# HEYMA-25 candidate verification (pre-review)

Branch: `fix/heyma-25-twin-note`, based on `c92675d`.

- Six diagnosed HEYMA-17 pairs were read from the live calls ledger in read-only SQLite mode and copied as leg, session, UTC recording start and duration into `tests/twin_prevention_test.py`. No live ledger writes, replay or note changes were performed.
- The isolated `JunkGateHarness` uses a temporary root, ledger and vault. It invokes `worker.run_once()` with a fake transcriber and S3 edge. For each pair it checks call linkage, the persistent duplicate marker, skipped audio bytes, the primary note bytes, no extra transcript row, no extra transcriber invocation or vault note, and repeat selection.
- Missing and ambiguous matches are tested as ordinary recordings. A calls-ledger exception fails open, and a previously transcribed twin is left untouched.
- Focused check: `pytest -q tests/twin_prevention_test.py tests/calls_test.py tests/prune_test.py tests/junk_gate_test.py tests/skill_hub_test.py` → **149 passed in 6.35 s**.
- Full Wax suite on the final candidate: `pytest -q tests` → **1402 passed in 212.73 s**. `git diff --check` passed.

Independent spec and quality review are pending. Do not merge, deploy, replay or prune under this ticket.
