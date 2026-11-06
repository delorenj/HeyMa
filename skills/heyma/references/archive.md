# Archive: S3 backup-first, never delete

The audio is the irreplaceable artifact. Wax uploads it to S3 **before**
transcription, proves the upload, and never deletes the source. This is why a
two-week transcription outage in July 2026 lost no audio: everything was
re-runnable from S3. Verified against `components/wax/src/wax/{archive,worker}.py`
on 2026-11-05.

## The rules

| Rule | Detail |
|---|---|
| Backup before transcription | `worker.process` calls `archive.archive` first. Nothing transcribes until the backup is verified. |
| A failed archive stops the item | After 3 failed verifications the source is copied to `recovered/unbacked/`, the item becomes `failed` with cause `archive_failed`, and **transcription does not run**. (An older doc said transcription "proceeds anyway"; that is wrong.) The original stays in the inbox. |
| Verification is never `mc cp`'s exit code | `verify_remote` re-reads the remote object's size and ETag. |
| The source is never deleted | S3 success or not. It is parked only after a live re-verify. |
| Content-addressed keys | A real sha256 names the object, so re-archiving identical bytes is idempotent. |

Bucket `recordings` on the mc alias `delo` (`s3.delo.sh`, `/usr/local/bin/mc`).
Key: `s3://recordings/<mtime YYYY-MM-DD>/<sha12>-<name>`.

## How an upload is verified

`verify_remote(path, key, local)` returns a verdict that records **how** the
bytes were proven, because "verified" with no method is how a 262,144-byte stub
once stood in for a 16.5-hour recording.

| Method | `ok` | When |
|---|---|---|
| `absent` | no | No remote object. |
| `size` | no | Remote size differs from local. |
| `md5` | no | Same size, but the single-part ETag differs from a local MD5. Re-upload. |
| `size` | yes | The gateway returned no ETag, or the ETag is not MD5-shaped. |
| `size+multipart-etag` | yes | ETag has a `-<parts>` suffix (md5 of md5s over server-chosen parts, so only size is comparable). |
| `size+md5` | yes | Single-part ETag equals the local MD5 (read only when the ETag can be an MD5). |

Size always gates. The ledger `backups.method` column stores
`verified-attempt-<N>/<method>` for a fresh upload or `already-present/<method>`
for an idempotent re-archive. There are 3 attempts (`ATTEMPTS`), a
`sleep(2*attempt)` backoff, and a 7200 s `mc cp` timeout.

Two sidecars sit beside each object and are what make
`wax reconcile --rebuild` possible (the ledger can be thrown away and rebuilt
from S3 plus the vault): `<key>.wax.json` and `.by-content/<sha256>.json`. After
transcription `link_transcript` writes the transcript name, title and slug into
the sidecar and tags the object, without renaming (an S3 rename is copy+delete).
The link does not wait for `title-slug` to return a slug: an un-slugged note is
linked with `slugged: false`, so "which audio has no transcript?" stays
answerable from S3 while every chat pass is down.

## What happens after a verified backup

In order inside `worker.process` (it is serialized: one item at a time):

1. **Size gate.** At or over `MAX_AUDIO_FILE_SIZE_FOR_TRANSCRIPTION` (300MB):
   the file is archived first, then moved to `skipped/oversize`, state
   `skipped`, cause `file_too_large_for_transcription`. (The check is strict:
   a file exactly at the ceiling is too large, `config.py`.)
2. **Duration gate.** `ffprobe` duration at or over
   `MAX_AUDIO_DURATION_FOR_TRANSCRIPTION` (3h): archived first, moved to
   `skipped/overduration`, state `skipped`, cause `audio_too_long_for_transcription`.
   File size is not a proxy for compute (a 13.9-hour Ogg at 28.7 kbps is 171 MiB).
3. Transcribe ([transcription](transcription.md)), then the passes
   ([passes/index](passes/index.md)), then link the archived audio to its
   transcript.
4. **Park** the local copy only after a live `remote_size` re-verify matches the
   local size. Mismatch gives `failed` with cause `s3_reverify_failed` and the
   audio stays in the inbox. Parked audio goes to `archive/YYYY/MM` (by mtime);
   a re-dropped copy of something already processed goes to `archive/duplicates`
   with an `already_processed` transition.

`WAX_MIN_FREE_BYTES` (default 5 GiB) is the capture preflight floor, not an
archive setting; see [capture](capture.md).

## Commands

```
wax archive <file>            # upload + verify one file now (idempotent)
wax archive --audit <file>    # report only if the S3 copy is missing, short or checksum-mismatched
wax reconcile [--rebuild]     # rebuild the ledger from durable sources (S3 sidecars + vault)
mc ls delo/recordings/        # what is actually in the bucket
```

## Recovery

| You have | Look in |
|---|---|
| An upload that never verified | `recovered/unbacked/` (a second local copy) and the original, still in `inbox/` |
| Audio that Wax moved aside | `skipped/oversize`, `skipped/overduration`, `quarantine/`, `recovered/` |
| A capture that died | `recovered/orphans/<rid>`, `recovered/canceled/<rid>`; see [capture](capture.md) |
| Syncthing deletions | `dropoff/.stversions/` (365-day staggered) |
| Anything archived | `mc ls delo/recordings/` then `mc cp` it back |

Never `wax reconcile --rebuild` to "see if it clears" during an incident; the
state on disk is the evidence ([the doctor procedure](../subskills/doctor/SUBSKILL.md)).

## Durability, honestly

`s3.delo.sh` is MinIO (`delodrive` container) with `EC:0`, no versioning, and a
bind mount on `~/DeLoDrive`, **the same NVMe partition as the original audio**.
It protects against deletion and corruption, not against losing that disk. The
offsite target is the private R2 bucket `delonet-backups` on the DeLoNET
Cloudflare account (created 2026-11-04, no public URL). **Unverified as of
2026-11-05: whether recordings replicate there.** Check before claiming offsite
protection. The Cloudflare key is a Global API Key (send `X-Auth-Email` with
`X-Auth-Key`, not a bearer token); the credential is
`op://DeLoSecrets/Cloudflare/globalAPIToken`.

The doctor's archive layer (`MinIO alias reachable`, `no unbacked audio on
disk`, `MinIO above the 100 GB floor`, `newest object linked to its transcript`)
is skipped by `doctor.sh --quick`. The one real emergency on the whole board is
irreplaceable audio with no verified copy.
