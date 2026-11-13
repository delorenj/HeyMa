# Capture: recording, the hotkey, sentinels, salvage and the guard

Recording is owned by `waxd`. It starts one `ffmpeg` in its own transient
systemd scope, writes 60-second Opus segments into `stream/`, and on stop
finalizes them into one `.ogg` in `inbox/`. State is recomputable from disk by a
cold process after a SIGKILL. Verified against `components/wax/src/wax/{capture,
sentinel,state}.py` on 2026-11-05. Design of record:
`components/wax/docs/WAX-DESIGN.md`.

## Commands

```
wax rec start [--label L] [--bitrate 32k]   # begin a capture (hotkey trigger when run by wax-toggle)
wax rec stop [rid]                          # orderly stop + finalize into inbox/
wax rec toggle                              # under a lock: recording -> stop; ready -> start; else refused (exit 2)
wax rec cancel [rid]                        # stop and discard: segments go to recovered/canceled/<rid>
wax rec salvage [rid]                       # rescue an error-partial capture into inbox/
wax rec quiesce                             # idempotent session-shutdown hook; idle is success
wax rec list                                # rid, segment count and bytes for each capture still on disk
wax state [stream|inbox|both] [--cold]      # derive the state machines from disk (cold is the default)
wax split [run] [TARGET] [--dry-run]        # cut a recording left running at the end of the meeting (below)
wax split submit SPLIT_ID SEG...            # release held parts into inbox/
wax split list [SPLIT_ID]                   # held splits
```

There is **no `wax reset` command**; older notes that mention one are wrong.
`wax state` and `wax status` exit 2 when the stream is `error` or
`error-partial`, which is a capture fault and not a broken CLI.

## The stream state machine

`wax state stream --json` returns `state`, `clause`, `cause_code` and
`evidence`. Five states:

| State | Meaning | `cause_code` examples |
|---|---|---|
| `ready` | No sentinels, no partials, preflight ok. | none |
| `recording` | `rec.json` present, no `.stop`, encoder alive. | none |
| `not-ready`, clause `a` | A stop was requested and the finalizer is alive within its deadline. Wait. | `finalizing` |
| `not-ready`, clause `b` | The stream is clean but the preflight failed. The cause is the first problem. | `no_default_source`, `default_source_absent`, `disk_unreadable`, `disk_low`, `not_writable:stream`, `not_writable:inbox` |
| `error-partial` | A capture exists but ended wrongly. | `uninstructed_exit`, `finalizer_died`, `finalize_deadline_expired`, or the `fin.json` reason (default `finalize_failed`) |
| `error` | Capture artifacts with no `rec.json`, older than 10 s. A human must adjudicate. | `orphan_partial` |

The clause values are `"a"` and `"b"`; older docs spelled them `a-finalizing`
and `b-incapable`, and the cause codes `inbox_unwritable` and
`stream_unwritable`. Those names are stale.

**`wax rec start` refuses unless the stream is `ready`**: it raises "stream is
'<state>' (<cause>), refusing to start". An older claim that start "never
refuses because of residue" is wrong (2026-11-05). So a leftover partial
blocks recording until you clear it with `salvage` or `cancel`.

The inbox machine is separate (`ready-and-waiting`, `ready-and-active` including
queued work, `error` with `stranded_work`, `stopped`/`scheduler_disabled`). It is
about processing, not capture; see [intake](intake.md) and
[system-map](system-map.md).

## Sentinels: the evidence that outlives the daemon

Two writes bracket every capture, both fsynced with the directory entry:

| File in `stream/` | Written | Means |
|---|---|---|
| `<rid>.rec.json` | before the first audio byte | Identity of the capture (pid, source, codec, bitrate, segment length). A partial can never exist without it. |
| `<rid>.stop` | before any signal is sent | An instructed stop, with `owner_pid` and `deadline_epoch`. Its absence beside a dead encoder is the definition of `error-partial`. |
| `<rid>.fin.json` | when finalization ends | Durable outcome (`ok` or a `reason`). |
| `<rid>.segs/seg-NNNNN.ogg` | continuously | 60-second Opus segments (`WAX_SEGMENT_SECONDS`). |
| `<rid>.ogg.partial` | only while finalizing | The concatenation; it exists only for the length of the finalize. |

`rid` is `YYYYMMDD-HHMMSS-<6 hex>`. The finalizer has `FINALIZE_DEADLINE_S` =
**180 s** (older docs say 45 s). Past the deadline, or with a dead finalizer,
the stream becomes `error-partial`. Transitions come from `Popen.wait()` and
`renameat2()`'s return value, never from `stat()`, which is why `stream/` and
`inbox/` must be one filesystem.

## Recovery

| Situation | Do | What happens |
|---|---|---|
| `error-partial` and the audio matters | `wax rec salvage <rid>` | Segments are concatenated into `inbox/<stem>-salvaged.ogg`; every original segment and sentinel moves to `recovered/orphans/<rid>`. It never deletes evidence. |
| `error-partial` and the audio does not matter | `wax rec cancel <rid>` | Segments move to `recovered/canceled/<rid>`; nothing reaches the inbox. |
| Stream `error`, `orphan_partial` | Read the artifacts named in `evidence`, then salvage or move them by hand | Wax refuses to guess; this needs a human. |
| `not-ready` clause b | Fix the named preflight problem (a PipeWire source, free space, permissions) | Clears on the next state read. |
| A recording ran for hours after the meeting | `wax split --dry-run`, then `wax split` | Cut at the end of the meeting from the waveform; parts are held until `wax split submit`. Exit 3 means no safe boundary |
| Item stuck after salvage | `wax drain`, then [passes/index](passes/index.md) | Salvaged audio is an ordinary inbox item. |

The 2026-08-22 incident (a GDM restart stranded a 23-hour capture; all 1,381
segments were valid) is in
[the incident log](../subskills/doctor/references/incident-log.md). The salvaged
recording was then parked under `skipped/overduration/` by the duration cap; see
[transcription](transcription.md).

## Splitting a recording left running (`wax split`)

You forgot to stop, and the capture ran for hours after the meeting. `wax split`
finds the end of the meeting **from the waveform alone** (ffmpeg to mono PCM,
RMS in dBFS, an adaptive noise floor; ASR, transcripts, diarization and LLMs are
never inputs) and cuts there. It works on three kinds of target:

| Target | What happens |
|---|---|
| The capture still recording | Detect on the completed segments first. No boundary: exit 3 and **the capture keeps recording**. A boundary: under the CLI capture lock (`var/waxd.lock`) it orderly-stops the encoder (`q`, as `wax rec stop`) and finalizes into `skipped/split/<rid>/` instead of `inbox/`, re-detects on the finalized file, then cuts. It never restarts a capture |
| An item in processing (`pending`, `archived`, `transcribed`, `enrichment_pending`) | Detect first (read-only). A boundary: fence **this item only** (`item_fences`), stop its tracked child processes (the transcriber, a pass) as process trees, wait for the item's flock (the worker holds it for the whole item, so getting it proves in-flight work settled; `--halt-timeout-s` bounds the wait, and on timeout the fence is lifted and nothing is cut), then cut and mark the item `skipped` (cause `split`). A late worker result is refused at every commit point; a transcript the child already wrote is moved to `recovered/split-late/`, never into the vault. The queue and other items are untouched |
| A queued, finished or archived item | Same cut. A `complete` item is not fenced and its transcript is untouched |

`wax split --dry-run` reports the proposed boundary and changes nothing.

What you get back: the selected recording, the parts with their time ranges, the
capture state before and after (live), and the processing disposition (item
state before and after, whether it was fenced, what was halted, where the
original is). Derived parts are **held**: they enter processing only through
`wax split submit` (or `--submit`), which copies them into `inbox/` so the usual
path applies (archive and verify in S3, then transcribe). The complete original
and every part are kept; nothing is deleted and no transcript is overwritten.
Each split has `split.json` (source item or capture, source sha256, detection
parameters, each part's start and end seconds, sha256, and where it went) and
`split_segments` ledger rows.

**No boundary is a result, not a failure of nothing**: continuous activity or a
flat hum (`no_contrast`), silent or all-quiet input (`no_activity`), only
ordinary pauses (`no_sustained_quiet`), a long quiet followed by more meeting
(`quiet_not_trailing`), too little before it (`too_little_activity`) or a short
file (`too_short`) all exit 3 and touch nothing. Limits: a loud overrun (TV,
music, a noisy fan) reads as activity and gets exit 3; one cut only; cut edges
are lossless stream copies, so they land on codec pages (up to about a second of
overlap or gap inside the quiet run). The `session ended` event of a held
finalization still names `inbox_path` although the file is in the hold.
Full design, thresholds and policy: `components/wax/docs/WAX-SPLIT.md`;
evidence: `components/wax/docs/evidence/heyma-18/`. Not covered: the unsplit
original is not uploaded to S3 by `wax split` (`wax archive <path>` if it exists
only locally).

## The capture guard

`wax-capture-guard.service` (user unit, enabled and active on 2026-11-05) is a
oneshot, `PartOf` `graphical-session.target`, whose `ExecStop` runs
`~/HeyMa/bin/wax rec quiesce --json` with `TimeoutStopSec=10min`. An orderly
graphical-session stop (which removes D-Bus and PipeWire, killing ffmpeg's
Pulse source) therefore finalizes an active capture first. A plain `waxd`
restart does not stop the encoder scope, because the encoder is a transient
scope that outlives the daemon by design.

`quiesce` stops only a `recording` or `not-ready` stream, treats `ready` as
success, and refuses other states. The doctor's `capture-guard-active` check
requires the unit to be active and to invoke this checkout's `rec quiesce`.

## The record hotkey

| Piece | Value (verified 2026-11-05) |
|---|---|
| Binding | GNOME custom keybinding `custom3`, "Wax record toggle", `<Control>backslash` (`Ctrl+\`) |
| Command | `/home/delorenj/.local/bin/wax-toggle`, a symlink to the tracked `components/wax/bin/wax-toggle` |
| What it runs | the `wax` beside its own resolved path, never a PATH lookup (`/usr/bin/wax` is a stale 2026-09-03 root-owned copy) |
| Feedback | a desktop notification for every outcome, including refusals |
| Dispatcher | `org.gnome.SettingsDaemon.MediaKeys.service`; if the shortcut does nothing, restart the owning *target*, not the service |

```bash
dconf read /org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom3/binding
systemctl --user restart org.gnome.SettingsDaemon.MediaKeys.target   # the service has RefuseManualStart=true
```

A second binding, `vinyl` (`<Control>BackSpace`, `/usr/bin/vinyl-toggle`,
"Vinyl dictation"), is **not Wax**. "I dictated a lot and there are no
transcripts" means check intake and the vinyl hub skill first
(`components/vinyl/skills/vinyl/SKILL.md`), not the pipeline. The evdev hotkey
design in WAX-DESIGN.md was never built.

## HeyMa window: Telephony is separate from recording

The native window adds a Telephony page beside Settings and Logs (`heyma --page telephony`), described in [telephony](telephony.md). It is a default-off/shadow call-policy view, not the Wax microphone. Local ringing is opt-in and owned by the existing tray; shadow would-block matches are not actually rejected or muted. The Wax red recording indicator keeps its original capture meaning.

## Where the audio goes next

A finalized recording lands in `inbox/`, the one work queue. From there the
worker archives it to S3 first ([archive](archive.md)), transcribes it
([transcription](transcription.md)), runs the passes
([passes/index](passes/index.md)) and parks it under `archive/YYYY/MM`. Other
ways audio reaches `inbox/` (Syncthing `dropoff/`, Telnyx, PDFs) are in
[intake](intake.md). The doctor's capture layer and its playbook pointer are in
[the doctor procedure](../subskills/doctor/SUBSKILL.md).
