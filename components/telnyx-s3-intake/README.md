# Telnyx S3 intake

Copies completed native Telnyx MP3 objects into the Wax inbox. Wax/waxd remains
the sole owner of archive, transcription, diarization, enrichment, and its ledger.
This component owns only transport receipts and private staging. It never writes
to `dropoff/` or changes or deletes remote objects.

## Source and dependencies

The source allowlist is enforced in code as well as example configuration:

| Setting | Authorized value |
| --- | --- |
| Endpoint | `https://s3.delo.sh` |
| Region | `us-east-1` |
| Bucket | `recordings` |
| Prefix | `5c224422-c4be-4c68-9b17-e3848499a198/` |

Only `<prefix>YYYY-MM-DD/<name>.mp3` keys with a valid date and an ASCII name
matching `[A-Za-z0-9][A-Za-z0-9_-]{0,150}` qualify. Sidecars, nested paths,
partial names, zero bytes, and objects over the configured limit are excluded.
Objects must be at least `settle_seconds` old before delivery (default 120).
Wax's own backup paths are outside the allowlist.

Requires Python 3.9+, Linux, SQLite support, AWS CLI v2, ffprobe with MP3 support,
and the installed unattended 1Password wrapper. No Python packages are required.
The example pins AWS to `/home/delorenj/.local/bin/aws`, the executable found on
this host; `/usr/local/bin/aws` was absent. The default when `aws` is omitted is
an absolute path resolved by `shutil.which('aws')` at process startup, with
`/usr/local/bin/aws` as a fallback. Prefer the pinned example for a user service.

AWS CLI replaces rclone here because live rclone listings succeeded while GETs
returned S3 403, and an apparent successful copy produced no file. The parent
verified actual AWS GET/HEAD/list access with the dedicated read-only credential.
There is no custom signing implementation or high-level sync operation.

The JSON configuration contains `op://` references only. Credentials are read
into memory and passed to AWS only as `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY`. Ambient AWS credentials, profiles, helpers, proxy,
rclone, and 1Password variables are excluded from AWS and ffprobe environments.
AWS uses empty credential/config files, region `us-east-1`, metadata lookup off,
an empty pager, and one attempt. Each operation sets JSON output, no pager,
10-second connect and 30-second read timeouts; subprocess and whole-run deadlines
also apply. Logs contain fixed failure codes and opaque receipt IDs, never raw
subprocess errors, object paths, environment values, or credentials.

The `op` process receives relevant inherited service-account and Connect
authentication variables. The normal wrapper may also load its existing
bootstrap and resolve Connect automatically. Intake disables caching but does
not force Connect bypass. The wrapper and its bootstrap must work unattended
from the systemd user manager's environment; success in an interactive shell
alone does not establish that. No secret belongs in this config or a new unit
environment file.

Wax's rendered `waxd.service` prepends the installing user's `~/.local/bin` to
`PATH` so enrichment passes resolve the installed unattended `op` wrapper.
Intake separately pins that wrapper with its absolute `op` configuration path.

## Durable paths and delivery

Paths resolve from this checkout through the self-resolving `bin/telnyx-s3-intake`
shim; the current directory does not select the runtime root.

| Path relative to `~/HeyMa` | Purpose |
| --- | --- |
| `var/telnyx-s3-intake/receipts.db` | Durable object identities and run results |
| `var/telnyx-s3-intake/intake.lock` | Nonblocking process lock |
| `var/telnyx-s3-intake/staging/<64hex>.partial` | Private incomplete/prepared audio |
| `inbox/telnyx-<date>-<name>-<receipt-id>.mp3` | Complete flat Wax input |
| `var/wax.db` | Existing Wax ledger, queried read-only during recovery |

State and staging directories must be owned by the user and private (0700);
files use 0600 with umask 0077. Staging and inbox must share a filesystem.
Receipts include endpoint, region, bucket, prefix, key, actual ETag, version when
available, size, and normalized modification time. A changed identity gets a
new receipt and filename.

Delivery proceeds under the lock:

1. LIST under the exact prefix; HEAD each eligible key.
2. Create a staging file exclusively. GET to that named path with `--if-match`
   and `--version-id` when available. A zero exit status alone is insufficient.
3. Require the original regular inode, private mode, positive exact byte count,
   local SHA256/MD5, matching MD5 for a single-part ETag, and a successful ffprobe
   result identifying MP3 audio with a positive finite duration.
4. Preserve source modification time and fsync the file and staging directory.
   Require a post-download HEAD to match the original identity.
5. Commit a `prepared` receipt with SHA256, then publish through an atomic,
   no-clobber hardlink into inbox and fsync its directory.
6. Commit `delivered`, then remove the private staging link.

Restart recovery reconciles prepared receipts against the final file's hash or
Wax's read-only `items.sha256`. Otherwise it publishes the verified staging
file, or retries a missing/corrupt stage from S3. Delivered receipts survive
inbox drain and prevent repeat downloads. Cleanup removes recognized private
regular `.partial` files left before prepare or after delivery; unknown names,
symlinks, and files with public permissions are preserved for operator review.
One failed object does not stop later objects within the remaining run budget;
any object failure makes the overall run fail.

The filesystem and SQLite commit are separate operations. A crash after
publication followed by Wax consuming the file **before recording its hash** can
cause publication again. Wax's content deduplication is the final boundary.
Multipart ETags do not provide an independent remote MD5: those objects rely on
conditional GET, exact size, identity checks, local hashes, and the media probe.
ffprobe checks media structure; it does not fully decode every audio frame.

## Review and install

No installation or service enablement is performed by the tests or dry-run.
The following commands are for the operator after review. Configuration copies
are no-clobber so an existing local config is preserved.

```sh
cd /home/delorenj/HeyMa
install -d -m 700 ~/.config/heyma ~/.config/systemd/user
cp -n components/telnyx-s3-intake/config.example.json ~/.config/heyma/telnyx-s3-intake.json
chmod 600 ~/.config/heyma/telnyx-s3-intake.json
bin/telnyx-s3-intake dry-run
systemd-analyze --user verify components/telnyx-s3-intake/deploy/telnyx-s3-intake.service components/telnyx-s3-intake/deploy/telnyx-s3-intake.timer components/telnyx-s3-intake/deploy/telnyx-s3-intake-alert.service
install -m 644 components/telnyx-s3-intake/deploy/telnyx-s3-intake*.service components/telnyx-s3-intake/deploy/telnyx-s3-intake.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now telnyx-s3-intake.timer
```

Dry-run performs only credential resolution, LIST, and HEAD; it creates no
runtime directories, receipts, or inbox files. It does not prove GET or Wax
processing. The timer polls every minute with a five-second random delay, a
60-second boot trigger, and persistent calendar catch-up. Each oneshot has a
240-second internal budget and a 270-second systemd timeout. Overlap fails
immediately with exit 75. Failure triggers a best-effort desktop alert; the
journal and CLI status remain the operational evidence on a headless session.

## Operate, disable, and recover

```sh
~/HeyMa/bin/telnyx-s3-intake status
systemctl --user status telnyx-s3-intake.service telnyx-s3-intake.timer
journalctl --user -u telnyx-s3-intake.service -n 100 --no-pager
systemctl --user list-timers telnyx-s3-intake.timer

# Publishes real eligible audio; run only after installation review.
systemctl --user start telnyx-s3-intake.service

# Stop scheduling and any active intake process.
systemctl --user disable --now telnyx-s3-intake.timer
systemctl --user stop telnyx-s3-intake.service
```

`status` is local and read-only; it does not resolve credentials. Never-run,
interrupted/running, failed, or unresolved receipts return nonzero. Success
describes the last intake run, not current remote reachability or Wax health.
Young objects deliberately deferred for settling are not failures.

On `credential_read_failed`, restore the existing wrapper's unattended
service-account/Connect access and retry. Do not add raw credentials to config,
arguments, logs, or units. On `remote_*`, verify the dedicated read-only scope
and connectivity. On `download_*`, `audio_probe_*`, or
`remote_identity_changed`, no new file from that failed attempt was published;
the next run retries that source identity. Other objects can still be delivered.

On `publication_collision`, stop intake, preserve both the existing inbox file
and private staging artifact, investigate the conflicting file, and move it to
a safe operator-controlled location if appropriate. Rerun intake to reconcile;
it will not overwrite a collision. On `staging_entry_invalid`, inspect the
reported opaque receipt's `.partial` for symlinks/permissions before moving it
aside. Automatic cleanup deliberately preserves suspicious entries.

Keep `receipts.db` and prepared stages across upgrades and service restarts.
Do not delete receipts as a retry mechanism: failed receipts already retry, and
forgetting delivered receipts permits source downloads again. Back up the DB
with SQLite's backup facility while intake is stopped, together with staging.
If state is lost, restore it before enabling the timer; a fresh DB cannot know
which remote recordings were delivered. Never modify Wax's DB to repair intake.
Intake does not remove remote audio during any recovery.

## Validation

```sh
python3 -B -W error::ResourceWarning -m unittest discover -s components/telnyx-s3-intake/tests -v
bin/telnyx-s3-intake dry-run --config components/telnyx-s3-intake/config.example.json
```

The hermetic suite uses temporary roots, fake AWS/op/ffprobe executables, and
process exits at the prepare/publication/receipt boundaries. It covers replay,
consumer drain, collisions, cleanup, partial/incorrect downloads, changed
identity, malicious keys, remote failures, deadlines, locking, and environment
isolation. It never patches or writes the live Wax DB.

Verified on 2026-10-15 from the operator environment: all 44 hermetic intake
tests and all 210 Wax tests passed. A real dry-run found 3 eligible candidates.
`systemd-analyze --user verify` outside the sandbox exited 0, with unrelated
preexisting warnings.

An isolated real transport run fetched 3 files with verified SHA256 hashes.
After its inbox was drained, a fresh intake instance using the same receipt DB
delivered 0. The live service reported success with 3 delivered receipts, each
with `attempts=1`; the 60-second timer is enabled.

Live Wax items `828c0910089ac082`, `8bb31e5fd6afd629`, and `3592c67bdf4c4841`
are all `complete`, with existing Markdown transcripts. The first item is speech
with `diarized=1`. The two loopback silence test objects have `word_count=0`;
their diarization passes were skipped with `human_edits`, preserving the pipeline
guard.

This proves transport and downstream Wax processing of existing S3 objects. No
new phone calls were made during this validation; native call recording was
proved earlier.
