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
| Item stuck after salvage | `wax drain`, then [passes/index](passes/index.md) | Salvaged audio is an ordinary inbox item. |

The 2026-08-22 incident (a GDM restart stranded a 23-hour capture; all 1,381
segments were valid) is in
[the incident log](../subskills/doctor/references/incident-log.md). The salvaged
recording was then parked under `skipped/overduration/` by the duration cap; see
[transcription](transcription.md).

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

## Where the audio goes next

A finalized recording lands in `inbox/`, the one work queue. From there the
worker archives it to S3 first ([archive](archive.md)), transcribes it
([transcription](transcription.md)), runs the passes
([passes/index](passes/index.md)) and parks it under `archive/YYYY/MM`. Other
ways audio reaches `inbox/` (Syncthing `dropoff/`, Telnyx, PDFs) are in
[intake](intake.md). The doctor's capture layer and its playbook pointer are in
[the doctor procedure](../subskills/doctor/SUBSKILL.md).
