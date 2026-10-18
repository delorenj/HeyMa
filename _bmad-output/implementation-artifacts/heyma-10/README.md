# HEYMA-10: three Wax items stuck at diarization

| Item | Audio | Before | After |
|---|---|---|---|
| bc81421c7b194b2b | 2658 s, local | diarization failed x3 (CUDA OOM at Sortformer load), exhausted | complete; diarized on cuda:0, 162 speaker turns; transcription.completed published 23:14:38Z |
| e5a727403046d70c | 3669 s, local copy missing | same | audio restored from verified S3 (sha256 == ledger); complete, 131 turns, published 23:16:05Z |
| ab8f6337e387119c | 5835 s, local copy missing | same | audio restored from verified S3 (sha256 == ledger); complete, 202 turns, published 23:18:14Z |

Bounded retry: one attempt per item, sequential, only with >= 3000 MiB free VRAM on two samples and no competing
transcription job; nothing was stopped to make room (6.4-6.5 GB free at each start).

Root cause of "retried but still stuck": `wax ep run` / the tray retry ran under the live registry, not the item's
frozen plan, so finalize refused the result (`passes_incomplete`). Fixed in b40825d; the independent review failed
that commit because the helper insertion moved `@serialized` off `run()` (no per-item lock); fixed in be84af0 with a
lock test. Reviews: `reviews.json` (SPEC FAIL -> fixed, QUALITY PASS_WITH_NOTES).

Notes for the owner:
- bc81's final pass row reads `skipped (human_edits)` at attempt 5: attempt 4 (live definition) wrote the speaker
  labels, attempt 5 (frozen definition) saw the changed body and skipped. The note is correctly diarized.
- Every live plan (94) differs from the live registry; manual runs now follow the frozen plan, like the sweep. A
  disabled/quarantined live pass can therefore be run by hand on a planned item (sweep already behaves this way).
- `wax ep sweep --dry-run` reports nothing for exhausted passes; exhausted items need `wax ep run <slug> <item>`.
- Local archive/2026/02-09 is missing for ~284 items (S3 copies verified); unexplained, not restored here.
- Unrelated `wax doctor` FAILs remain: title-slug model probe HTTP 403 (Cloudflare 1010) and one outbox row
  unpublished since 2026-09-30.
