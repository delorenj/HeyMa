# HEYMA-25 candidate verification (revised after bounded-pair and re-delivery review)

Branch: `fix/heyma-25-twin-note`, based on `c92675d`.

- Six diagnosed HEYMA-17 pairs remain in `tests/twin_prevention_test.py`. All tests use a temporary ledger, inbox and vault; no live ledger, replay, notes or audio were changed.
- Own-leg CDR identity wins over a conflicting recording session. Fractional start times use an indexed widened scan followed by an exact elapsed-time bound (including 2.000 seconds and rejecting 2.001/2.700 seconds); duration and ambiguity guards remain.
- A calls/recordings row is not publication. The worker checks an on-disk, unpruned transcript before parking the other copy. If the primary is absent, pending, failed or has a pruned/missing note, the incoming twin can publish one note; when the primary arrives or retries it is parked. Both delivery orders and repeat selection are exercised through `worker.run_once()`.
- A published legless twin persists the exact matched primary `recording_key` in its item row; primary selection consults that provenance, not a new `twin_of()` lookup, so a later own-leg CDR does not create a second note. Checks are bounded to the pair's recording receipt epoch and the real unpruned note: a separate recording on the same leg an hour later publishes normally, including when it has its own matching twin recording.
- Duplicate destinations are journaled before `rename.move_noclobber`; a run-once retry finishes the skipped transition after an injected exception immediately after the real rename. A failed pre-move attempt restores the inbox path. Identical same-name/same-bytes redelivery after normal parking or between rename crash and recovery is parked separately, preserving the canonical `items.path` and audio, never retranscribing.
- Focused check on revised candidate: `env -u PYTHONPATH /usr/bin/python3 -m pytest -q tests/twin_prevention_test.py tests/calls_test.py tests/prune_test.py tests/junk_gate_test.py tests/skill_hub_test.py` → **160 passed in 8.12 s**.
- Full Wax suite on revised candidate: `env -u PYTHONPATH /usr/bin/python3 -m pytest -q tests` → **1413 passed in 268.08 s**. `git diff --check` passed.

Independent re-review remains pending. Do not merge, deploy, replay or prune under this ticket.
