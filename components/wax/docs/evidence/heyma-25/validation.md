# HEYMA-25 candidate verification (exact-object twin lookup repair)

Branch: `fix/heyma-25-twin-note`, based on `c92675d`.

- Six diagnosed HEYMA-17 pairs remain in `tests/twin_prevention_test.py`. Tests use a temporary ledger, inbox and vault; no live ledger, replay, notes or audio were changed.
- Own-leg CDR joins remain intact, including when the recording session differs; the legless `twin_of` lookup now requires a unique exact object/file mapping before considering its two-second fractional-start, one-second duration and ambiguity guards. An unmapped epoch on an otherwise valid leg yields no twin and cannot stamp the other call's metadata. The exact mapped filename still links; an ordinary own-CDR join still wins.
- A calls/recordings row is not publication. The worker checks an on-disk, unpruned transcript from the exact paired recording key before parking the other copy. If the primary is absent, pending, failed or has a pruned/missing note, the incoming twin can publish one note. Both delivery orders and repeat selection are exercised through `worker.run_once()`.
- A published legless twin persists the exact matched primary `recording_key` in its item row; primary selection consults that provenance, not a new `twin_of()` lookup, so a later own-leg CDR does not create a second note. Pairing uses the recording API's precise start and duration, never the object epoch or intake receipt as a timestamp. A distinct recording on the same leg, even one second away, owns its note.
- Duplicate destinations are journaled before `rename.move_noclobber`; retry finishes a move interrupted after rename, while a pre-move failure restores the inbox path. Identical redelivery parks separately without replacing canonical audio or retranscribing.
- The legacy prune pair fixture now supplies object names, allowing its intended exact-object duplicate behavior to remain testable.
- Focused check: `env -u PYTHONPATH /usr/bin/python3 -m pytest -q tests/twin_prevention_test.py tests/calls_test.py tests/prune_test.py tests/junk_gate_test.py tests/skill_hub_test.py` → **166 passed in 6.35 s**.
- Full Wax suite: `env -u PYTHONPATH /usr/bin/python3 -m pytest -q tests` → **1419 passed in 226.78 s**.
- Prior independent review of `2070812` found one remaining leg-only fallback in `calls.py`; this revision removes it and adds the unmapped-file regression. Earlier quality review found no other issues; independent re-review of this revision remains pending. Do not merge, deploy, replay or prune under this ticket.
