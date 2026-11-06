# Incident log: every outage, its signature, and what actually caused it

Read this early. This pipeline breaks in **repeating shapes** (see the three
shapes at the bottom, and [the doctor procedure](../SUBSKILL.md)), and matching
the signature has repeatedly been faster than reasoning from first principles.

Newest first. Add to the top when you resolve something; a troubleshooting
sub-skill that does not learn is just documentation. Entries below 2026-10 were
ported from the retired `heyma-pipeline-doctor` skill and kept verbatim where
the code still matches; where the code has since moved, the entry says so.

---

## 2026-11-05 · HEYMA-12 call-metadata `call_not_found` · *an observer picked the wrong leg*

**Signature.** `wax failures` filed `[wax] pass call-metadata: call_not_found
(1 affected)`. A `telnyx-*` recording had no resolvable call even though the
call log held the call.

**Cause.** The pass resolved the Telnyx CDR session by a precedence that
preferred a broader session over the exact leg that produced the recording.
Fixed in commit `fa0abb7` ("prefer exact-leg CDR session for call metadata").

**Lesson.** Shape 1 (guessing at a thing it does not own). The failure surfaced
only because `wax failures sweep` files one ticket per signature; the pass
itself returns a classified `skipped`/`failed` quietly. See
[failures](../../../references/failures.md).

---

## 2026-11-04 · dictionary-correction false positives · *thresholds were not the cause*

**Signature.** Wrong respellings in transcripts ("Dami and" became Damian, "in
Telephoria", "jangler" turned into "P pjangler"/"project pjangler", spelled-out
addresses mangled) right after the pass shipped.

**Cause.** Not Jev. The false positives came from scan runs that swallowed a
neighbouring function word, `heard:` forms that were fragments of a term, and
spelled addresses. All three became scan rules. The full backfill that day made
3,847 replacements with about 10 false positives, all reverted.

**Lesson.** Check a run's edges and the term's `heard:` list before touching a
threshold. Calibration and the look-alike trick (Skrillex became Skillex at p
0.87 until `Skrillex` was added as its own term) are in
[passes/dictionary](../../../references/passes/dictionary.md).

---

## 2026-11-02 · the chat chain's GLM and DeepSeek members went dead · *a funding decision, not a bug*

**Signature.** `title-slug` evidence shows `automaticai/personal/glm-5.3-flash`,
`personal/glm-5.3`, `deepseek/deepseek-flash` and `deepseek/deepseek-v4-pro` as
`missing_model, skipped`; a direct call returns 503 `model_not_found` ("No
available channel ... under group aai-personal"). Titles still land, served by
`auto/fast` or `automaticai/free`, so nothing looks broken.

**Cause.** Gateway channels 9 (zai-personal) and 25 (deepseek) were disabled on
2026-11-02 14:13 UTC: the z.ai account answered 429 code 1113 (recharge) and
DeepSeek answered 402 (balance -0.01). Both accounts are unfunded.

**Fix.** None in Wax, deliberately. Do not re-scope the token or drop the
members. After the operator funds an account:
`cd ~/code/client-portal/gateway && python3 ops/reconcile-routes.py --account zai-personal|deepseek --apply`,
then re-probe. Still true on 2026-11-05 (verified in the ledger and by
`/v1/models`). `doctor.sh` now warns on it (`title-chain-offered`). See
[passes/model-chain](../../../references/passes/model-chain.md).

---

## 2026-10-22 · transcripts landed with no titles · *a dead OpenRouter account behind a 429*

**Signature.** Recording `9916aa1c9450-20261022-050452-rec.ogg` reached S3 but
produced no usable transcript note. Transcription had succeeded; `title-slug`
and `classification` failed with HTTP 429 from the AutomaticAI gateway, and Wax
only showed "stage failed".

**Cause.** The gateway's OpenRouter account was dead; the passes were riding it.

**Fix.** Passes were re-routed to `automaticai/auto/fast` on the scoped
`heyma-wax-inference` token (commit `61b934f`), the OpenRouter fallbacks and
`OPENROUTER_API_KEY` were removed. No pass is configured for OpenRouter today.
A follow-up trap: changing the
token scope does not move saved pipelines or minted plans. Re-import the n8n
release exports and run `wax ep replan <item> --apply`.

**Lesson.** Shape 3 (a sub-stage degrading with no path to a human), plus the
frozen-plan trap in [pipelines](../../../references/pipelines.md).

---

## 2026-10-18 · HEYMA-10, three items stuck at diarization · *a retry that ran the wrong plan*

**Signature.** Three items (bc81421c7b194b2b, e5a727403046d70c,
ab8f6337e387119c) had `diarization` failed three times (CUDA out of memory at
Sortformer load) and exhausted. Retrying with `wax ep run` or the tray still
left them stuck.

**Cause.** The OOM was a free-VRAM problem. The "retried but still stuck" part
was a runner bug: `wax ep run` and the tray retry ran under the live registry,
not the item's frozen plan, so `finalize` refused the result
(`passes_incomplete`). Fixed in `b40825d`; the review failed that commit
because the helper insertion moved `@serialized` off `run()` (no per-item
lock), fixed in `be84af0` with a lock test.

**Recovery.** One bounded attempt per item, sequentially, only with at least
3000 MiB free VRAM on two samples and no competing transcription job. Two of
the three had no local audio and were restored from S3 after the sha256
matched the ledger. All three completed and published `transcription.completed`.

**Notes.** Manual runs now follow the frozen plan, like the sweep, so a
disabled or quarantined live pass can be run by hand on a planned item.
`wax ep sweep --dry-run` reports nothing for exhausted passes; exhausted items
need `wax ep run <slug> <item>`. The full record is
`_bmad-output/implementation-artifacts/heyma-10/README.md`.

**Lesson.** Shape 2 (the code that ran was not the plan that was minted).

---

## 2026-10-17 · doctor board's same-checkout false positive · *the check resolved PATH, not the daemon's pin*

**Signature.** `doctor.sh` reported `same-checkout` FAIL ("daemon runs from
/home/delorenj/HeyMa but transcribe resolves to /home/delorenj/.local — two
checkouts") while `wax doctor` (the CLI) reported the transcriber probe PASS
and the pipeline was healthy.

**Cause.** `~/.local/bin/transcribe` stopped being a symlink into the repo and
became a modified **copy** of the shim (it has been a symlink to
`~/HeyMa/bin/transcribe` again since 2026-10-18; the check still resolves the
daemon's pin first, so it cannot regress) (with its own hard-coded
`_TRANSCRIBE_DEFAULT_DIR` and an `--ephemeral` fork, 2026-10-12). The board's
`resolved_transcribe()` ran `readlink -f "$(command -v transcribe)"` — pure
PATH resolution — so the "transcriber root" computed as `~/.local` no matter
what the daemon actually runs. The daemon itself was pinned all along
(`Environment=WAX_TRANSCRIBE=/home/delorenj/HeyMa/bin/transcribe` in
`waxd.service`) and never consults PATH. The same wrong root also poisoned
`diarization-imports` and `diarization-cuda`, which look for the venv beside
the resolved transcriber.

**Fix.** Resolution now mirrors `wax doctor`'s `_resolve_transcriber`: the
daemon's `WAX_TRANSCRIBE` pin (read from `/proc/<pid>/environ`) first, then
the canonical repo shim, then PATH — with the root derived explicitly from the
resolved shim (`<shim>/../..`). A pin genuinely outside the repo still fails:
the check keeps its teeth. Regression coverage:
`scripts/tests/test_transcriber_resolution.sh` beside `scripts/doctor.sh`
(fixtures only; never touches the real daemon).

**Lesson.** *A health check that resolves a path differently from the thing it
checks reports fiction.* The 2026-08-12 incident was the code drifting from
the check; this one was the check drifting from the code. Both directions
produce the same false confidence.

**Checks that now catch it:** `same-checkout` (fixed resolution order),
`diarization-imports`, `diarization-cuda`.

**Also recorded:** the doctor board checks the **audio** pipeline only. It has
no visibility into document pipelines (bindings, receipts, scan cadence) or
pipeline compilation — those live in `wax catalog --json` / `wax documents
list`. A green board is not end-to-end proof.

---

## 2026-09-06 · configured recording hotkey did nothing · *dispatcher absent behind a live target*

**Signature.** `Ctrl+\` did nothing even though `custom3` had a non-empty
`<Control>backslash` binding, its command worked from a terminal, the Wax tray
was registered, and the doctor's static `hotkey-bound` check passed after seeing
the expected command. Other GNOME custom shortcuts were inert too.

**Cause.** `org.gnome.SettingsDaemon.MediaKeys.service` had exited cleanly on
2026-08-24 alongside the other GNOME Settings Daemon plugins and had no D-Bus
owner. Its owning `org.gnome.SettingsDaemon.MediaKeys.target` remained active,
so checking the target or the dconf values produced false confidence. The
journal containing the original stop event had already rotated; the surviving
unit timestamps prove the shared exit but not what initiated it.

**Recovery.** Restart the owning target, not the service (the packaged service
sets `RefuseManualStart=true`):

```bash
systemctl --user restart org.gnome.SettingsDaemon.MediaKeys.target
```

**Fix.** The doctor now requires both an active MediaKeys service and its live
D-Bus owner. The binding check separately accepts either the notification
wrapper or a direct `wax rec toggle` command, because command preference is not
evidence that GNOME can dispatch it.

**Checks that now catch it:** `hotkey-dispatcher`, `hotkey-bound`.

## August 22, 2026 · GDM restart stranded a 23-hour capture · *audio graph teardown*

**Signature.** The tray returned yellow after the graphical session restarted.
`wax state` and `wax status` exited 2, and the stream reported
`error-partial (uninstructed_exit)`. The inbox and all pipeline stages were
healthy, but the doctor mislabeled the nonzero state result as `wax-cli`, which
made a capture fault look like a broken control plane.

**Cause.** A privileged `systemctl restart gdm` stopped the graphical user
session. The encoder's transient scope correctly outlived `waxd` by about 12
seconds, but D-Bus, PipeWire, and WirePlumber then stopped. FFmpeg exited when
its Pulse source graph disappeared. This was not a machine reboot: the boot ID
before and after the failure was identical.

**Recovery.** All 1,381 Ogg segments were independently valid: 273,860,321
bytes and 82,851.326 seconds. Wax remuxed them into a 273,651,230-byte recording,
verified its S3 object by size and multipart ETag, and parked the local copy
under `skipped/overduration/`. The original segment set remains under
`recovered/orphans/`, with a matching aggregate SHA-256 fingerprint.

**Fix.** Salvage now moves every original segment and sentinel into
`recovered/orphans/` instead of deleting the segment directory. A dedicated
systemd capture guard runs `wax rec quiesce` before an orderly graphical-session
teardown removes D-Bus or PipeWire. The doctor now separates CLI validity from
stream health and routes `error-partial` directly to the capture playbook.

**Checks that now catch it:** `stream-healthy`, `capture-guard-active`.

## 2026-08-21 · CUDA available, diarization on CPU · *execution overrode its own model loader*

**Signature.** Torch reported CUDA 13.0 and an RTX 3090, and importing the old
backend even allocated ~918 MiB on `cuda:0`, but every production log said
`Running diarization (cpu)`. Long recordings spent tens of minutes diarizing on
CPU despite a healthy GPU runtime.

**Cause.** The backend automatically loaded Sortformer onto CUDA, including one
hidden model at module import. `scripts/transcribe.py` then loaded a second copy
and explicitly moved that model and preprocessor to CPU under a stale
"partial CUDA runtime" workaround. No setting controlled this, and metadata did
not record the actual device.

**Fix.** Replaced the ignored WhisperLiveKit dependency with the tracked,
side-effect-free `wax.diarization_sortformer` adapter; made
`WAX_DIARIZATION_DEVICE=cuda` the strict default; kept ASR and diarization device
policies independent; pinned the rebuild manifest; and added requested/actual
device evidence to logs, transcript frontmatter, and adapter provenance. Both
the installer and doctor now load Sortformer and execute a real streaming CUDA
forward pass.

**Checks that now catch it:** `diarization-device`, `diarization-cuda`, plus the
primary `wax doctor` probes `diarization device policy` and
`diarization cuda runtime`.

---

## 2026-08-15 → 2026-08-19 · title-slug 404 · *dependency deleted off the host*

**Signature.** Transcripts land correctly but keep bare timestamp filenames
(`20260819-103057-rec.md`). Frontmatter shows
`wax.passes.title-slug: {state: failed}` with **no reason**, timestamped ~50 ms
after the pass started. Tray green. `wax status`: "no errors". Journal: empty.

**Cause.** The Ollama model `qwen3.6:latest` pinned by `title-slug.yaml` was
deleted from `/usr/share/ollama/.ollama/models` on 2026-08-15 09:47:53. Every run
POSTed `/api/chat` and got HTTP 404 in ~1 ms.

**Why it took five days.** Three independent failures of disclosure:
`urllib.error.HTTPError` is a subclass of `URLError`, so the body naming the
missing model was swallowed and rendered as the bare string
`HTTP Error 404: Not Found`; `reason_code` could not distinguish a missing model
from a timeout (both exit 1 ⇒ `nonzero_exit`); and no surface read the `passes`
table at all.

**Blast radius beyond titles.** `passes.py` gated `archive.link_transcript` on the
EP returning a slug, so the entire audio↔transcript linkage in S3 stopped too. And
`frontmatter_schema` was applied inside `_apply_result`, reached only on `rc == 0`,
so a remote LLM outage also blocked a purely local, deterministic vault-taxonomy
stamp. **Lesson: never gate a deterministic local step on a remote call's exit code.**

**Fix.** Retired local inference for this pass. The provider is now pure config
(`WAX_TITLE_API_BASE` / `WAX_TITLE_MODEL` / `WAX_TITLE_API_KEY_OP`) against any
OpenAI-compatible endpoint, with a `/models` preflight, `reason_code=missing_model`,
and the provider's own error body preserved.

**Check that now catches it:** `title-model-present`, `no-recent-pass-failures`.

---

## 2026-08-12 → 2026-08-19 · no diarization · *`git reset --hard` in the other checkout*

**Signature.** Every transcript has `diarized: false`. No speaker labels. No error
anywhere the user would look. Ledger: last `diarized=1` at 2026-08-12T13:35:18Z,
then 128 consecutive zeros.

**Cause.** `~/.local/bin/transcribe` pointed at a **second checkout**
(`~/code/HeyMa`), 9 commits behind the tree everyone was editing (`~/HeyMa`). A
`git reset --hard origin/main` there fast-forwarded past commit `1d21e8b`, which
had deleted 63 files under `whisperlivekit/`. The editable install still mapped
`whisperlivekit` → that directory, which now held **0 `.py` files and 18 orphan
`.pyc`**. `diarize_local()` caught the `ImportError` and returned `[]`, so
`diarized: bool([])` was written as an honest `false`.

**Why the preflight did not catch it.** `missing_diarization_dependencies()`
probed `librosa` and `nemo` — both fine. Execution imported a **third** module,
`whisperlivekit.diarization.sortformer_backend_offline`, which was never checked.
The guard passed, up to 3 h of GPU ASR ran to completion, and the import died
afterwards. Measured waste on one job: **29 min 24 s of ASR**.

**Fix at the time.** Restored the subtree from history, moved the runtime into
the deployed tree, pinned `WAX_TRANSCRIBE` in the unit so PATH can never again
choose which code runs, and made the preflight import the real entry point. On
2026-08-21 the private backend was replaced entirely by the tracked owned Wax
adapter, removing this deletion class rather than merely detecting it.

**Lesson.** *A preflight that tests different modules than execution imports is
worse than no preflight* — it converts a hard failure into a silent downgrade and
buys false confidence. Test the actual entry point.

**Checks that now catch it:** `diarization-imports`, `same-checkout`,
`recent-diarized`, `no-diarization-error-in-log`.

---

## 2026-07-01 → 2026-07-14 · nothing transcribed for two weeks · *hardcoded path*

**Signature.** Recordings archived to S3 fine; no transcripts appeared. n8n showed
green executions.

**Cause.** The repo moved from `~/code/33GOD/HeyMa` to `~/code/HeyMa`, but
`bin/transcribe` had `TRANSCRIBE_DIR` hardcoded to the old path, so `cd
$TRANSCRIBE_DIR` failed and `transcribe.py` never ran.

**No audio lost** — the backup-first policy did its job, which is the entire reason
it exists. Everything was re-runnable from S3.

**Lesson.** This is the *same bug* as the 2026-08-12 incident wearing different
clothes: **the pipeline resolving its own code through a path that can drift.**
Both were "the code that ran was not the code that was edited." When something
inexplicably stopped working after a move, a reset, or a rename, check
`same-checkout` first.

---

## 2026-06-29 · a recording was destroyed · *wrote into a receive-only Syncthing folder*

**Cause.** A local writer dropped a file into a Syncthing `receiveonly` folder.
Files added locally to a receive-only folder are treated as divergent and get
reverted/deleted on any reconcile or folder-marker reset.

**Rule that came out of it.** Local writers never write into a receive-only
folder. `dropoff/` is Syncthing's; **`waxd` copies out of it and never writes into
it.** Recordings go to `inbox/`.

---

## Standing traps (not incidents: things that can mislead you)

Re-verified 2026-11-05. Items marked "gone" no longer exist on this machine;
the board still checks them (`no-stale-mirror`, `no-root-passes-d`,
`legacy-dirs-quiet`, `no-stale-design-doc`) so they cannot silently come back.

| Trap | State on 2026-11-05 | Why it matters |
|---|---|---|
| `~/audio/var/state.json` | gone | A 2026-07-30 mirror asserting inbox error and a dead PID, formatted exactly like a live one. `~/audio/*` is a retired runtime root nothing reads. |
| Repo-root `passes.d/` | gone | A registry the runner never loaded. The real one is `components/wax/config/passes.d/`. |
| Repo-root `WAX-DESIGN.md` | gone | Named the retired `~/audio` layout. The only copy is `components/wax/docs/WAX-DESIGN.md`. |
| n8n workflow `r2TUca8smk5HDNZx` | archived 2026-08-19 (not re-checked live) | Was `active: true` with a `localFileTrigger` on `~/audio/inbox`. Any write there triggered an out-of-band transcribe with no ledger row and a duplicate S3 object. n8n owns no pipeline stage; see [n8n](../../../references/n8n.md). |
| `~/.local/bin/transcribe` | symlink to `~/HeyMa/bin/transcribe` since 2026-10-18 | Was a modified copy on 2026-10-17. `waxd.service` pins `WAX_TRANSCRIBE` absolutely, so PATH never decides which code runs. |
| `/usr/bin/wax` to `/opt/wax` | stale 2026-09-03 copy | Never use a PATH lookup for `wax`. Use `~/HeyMa/bin/wax` or `mise run wax:*`. |
| `wax.db` mtime | always misleading | WAL mode leaves it untouched for hours while writes land in `wax.db-wal`. |
| `systemctl status` memory | misleading | Counts reclaimable page cache from transcription children. Use `/proc/<pid>/status`. |
| Green tray | not evidence | See "Wax fails silently by construction" in [the doctor procedure](../SUBSKILL.md). On 2026-11-05 the board itself caught it: `tray-colour-honest` failed with the tray GREEN while two `title-slug` failures sat in the 7-day window. |
| MinIO is one drive | true | `s3.delo.sh` is MinIO on the same NVMe partition as the original audio. It protects against deletion and corruption, not against losing that disk. Offsite is the R2 bucket `delonet-backups`; see [archive](../../../references/archive.md). |
| Ledger rows pointing at missing audio | 214 on 2026-11-05 (`doctor.sh` warns) | They inflate `items.pending` forever while `queue.total` stays 0. Local `archive/2026/02-09` is missing for about 284 items whose S3 copies verified (2026-10-18 note, unexplained). |

## The pattern behind all of it

Every verified defect in this pipeline's history is one of three shapes:

1. **An outside observer guessing the state of something it does not own**
   (a size-settle timer guessing at the recorder, `stat()` guessing at ffmpeg).
2. **The code that ran was not the code that was edited** (a drifted path, a
   second checkout, a stale symlink).
3. **A sub-stage degrading to an empty value with no path from "degraded" to
   "a human is told."**

When you find a new failure, name which of the three it is. If it is a fourth,
that is genuinely new: write it down here.
