---
name: heyma-doctor
description: Diagnose and repair HeyMa/Wax incidents in audio or document execution, such as a stalled recording, a missing transcript or speaker labels, a failed or silently empty enrichment pass, archive or outbox errors, a stale worker heartbeat, or a misleading green tray. Use when waxd or a wax command misbehaves, and establish a baseline before changing the runner. Pipeline operation, bindings and n8n belong to the hub references; authoring a new pass belongs to the heyma-create-enrichment-pass sub-skill.
---

# HeyMa doctor (HeyMa hub sub-skill)

Load this file by path from the HeyMa hub (`skills/heyma/SKILL.md`). It owns
**incidents**: something is wrong and you need to find it, fix it and land it.
Normal operation lives in the hub's references.

The pipeline is **Wax**: one daemon (`waxd`) that owns the microphone, the
inbox, the SQLite ledger, transcription, the enrichment passes, the S3 archive,
the event outbox, the document bindings and the tray. It lives at
`components/wax/`. The map is in [system-map](../../references/system-map.md).

> Docs drift; code is truth. If anything you read contradicts the code, trust
> the code and say so. The documents of record are
> `components/wax/docs/WAX-DESIGN.md`, `ENRICHMENT-PASSES.md` and
> `CONTROL-SURFACE.md`.

## Start here: green is not evidence

**Wax's failure mode is degradation, not crashing.** Every value-producing
sub-stage degrades by returning an empty value instead of raising. In the
August 2026 outages `diarize_local()` caught an `ImportError` and returned `[]`,
so `diarized: false` was written as an honest false; `passes.run()` turned a
non-zero exit into a `failed` row without raising, and `worker.process()` marked
the item `complete` 215 ms later. Both outages ran at 100% failure for 5 to 7
days behind a green tray, a `wax status` that printed "no errors" and a journal
with zero waxd lines. The 2026-11-02 chain outage is the same shape: titles kept
landing while four of six model routes were dead
([incident log](references/incident-log.md)).

**Never conclude "it is fine" from a green tray or a quiet journal.** Ask the
ledger (`var/wax.db`, the `passes` and `transcripts` tables) and the doctor.

Today the system can represent most of this, and the doctor leans on it:

- `transcription.completed` is written only when every planned pass is
  `completed` or `skipped` at the plan's version; a failed pass leaves the item
  `enrichment_pending`.
- A pass failure carries a `reason_code` in the ledger and the note's
  `wax.passes.<slug>` block ([reason codes](references/reason-codes.md)). A pass
  that exhausts three attempts emits `bloodbank.audio.task.exhausted`.
- A malformed registry entry quarantines just that pass and its dependents,
  loudly (`wax ep list`, `wax doctor`).
- The worker sweeps failed passes every `WAX_SWEEP_INTERVAL_S` (900 s).
- `wax failures` groups recent failures and `wax failures sweep` files one
  urgent HEYMA ticket per signature ([failures](../../references/failures.md)).
  It is not scheduled, so nothing files a ticket unless someone runs it.

What still collapses quietly: sub-stage values (`diarized: false`), and a
`wax status` summary that reads calm while a value-producing stage is dead.
`tray-colour-honest` in the board exists to catch the tray lying; on 2026-11-05
it did.

## The three shapes: name one before you debug

Every verified defect in this pipeline's history is one of three shapes. Naming
the shape first has repeatedly been faster than reasoning from the symptom.

| Shape | Looks like | Where to look |
|---|---|---|
| 1. An outside observer guessing the state of something it does not own | A size-settle timer guessing at the recorder; `stat()` guessing at ffmpeg; a check resolving PATH instead of the daemon's pin | Replace the guess with the owner's own signal (`Popen.wait()`, `renameat2()`, `/proc/<pid>`). See [capture](../../references/capture.md). |
| 2. The code that ran was not the code that was edited | A drifted path, a second checkout, a stale symlink, a `transcribe` copy on PATH, a frozen plan running under a changed registry | `same-checkout`, the unit's `WAX_TRANSCRIBE` pin, `wax ep replan`. The July 2026 and August 2026 outages were both this. |
| 3. A sub-stage degrading to an empty value with no path from "degraded" to "a human is told" | `diarized: false`, `title-slug` served by the last chain member, a pass `failed` while the item parks `complete` | Add the missing surface: a doctor check, a `wax doctor` probe, a `wax failures` kind. |

A fourth shape would be genuinely new. Write it in the incident log.

## Step 1: CLI first

From the repo root (the checkout is five directories above this file):

```bash
mise x -- ./bin/wax doctor          # resolved config + one probe per thing that can rot
mise x -- ./bin/wax status          # both state machines, queue, pass + diarization health
mise x -- ./bin/wax catalog --json  # pipelines, drift, bindings, worker freshness
mise x -- ./bin/wax failures        # recent failures grouped by (kind, slug, reason)
```

`wax doctor` is the component's own probe set: registry quarantine, the chat
route served to the gateway token, the Jev ingress and its keys, pass-event
schemas, the pjangler registry, diarization device policy plus a **real
Sortformer CUDA forward pass**, the daemon's transcriber pin, state-mirror age,
outbox staleness, the call log, document hygiene and the transcriptions store.
Its rules are absolute: it may never report health it has not just verified,
and it never prints a resolved secret. Treat it as informational, not a gate.

Then the layered board:

```bash
skills/heyma/subskills/doctor/scripts/doctor.sh            # about 40 s, read-only, safe mid-recording
skills/heyma/subskills/doctor/scripts/doctor.sh --quick    # skips S3 round-trips and the test suite
```

`scripts/doctor.sh` locates the checkout from its own path (override with
`WAX_REPO`), so it works from any working directory. Flags: `--quick`,
`--layer <name>`, `--only <check-id>`, `--json`, `--verbose`. Exit 0 all pass,
1 at least one FAIL, 2 the environment could not be resolved. Legend:
`✔` pass, `✘` fail, `▲` warn (degraded, not stopped), `○` skipped.

The layers run in **dependency order**; a failure in an early layer marks the
dependent ones `○` instead of reporting a cascade as causes. **Fix the first
failing layer, then re-run.** Do not fix the bottom of the board.

| Layer | Checks (examples) | Playbook |
|---|---|---|
| Daemon, Paths and ledger | `waxd` active, daemon holds the live ledger open, no retired `~/audio/var` mirror | [system-map](../../references/system-map.md), [config-and-deploy](../../references/config-and-deploy.md) |
| Capture | stream recordable, capture guard, hotkey | [capture](../../references/capture.md) |
| Queue and inbox | scheduler enabled, no audio hidden in subdirs, no phantom items | [capture](../../references/capture.md) and [intake](../../references/intake.md) |
| Stage outcomes | no pass failed in 7 d, newest titled and diarized, tray colour honest | [passes/index](../../references/passes/index.md), [transcription](../../references/transcription.md) |
| Enrichment dependencies | registry loads, scripts runnable, provider reachable, model and chain offered, key resolves, frontmatters editor | [passes/index](../../references/passes/index.md), [passes/model-chain](../../references/passes/model-chain.md) |
| Transcription dependencies | same checkout, diarizer imports, CUDA policy and forward pass, checkpoint cached | [transcription](../../references/transcription.md) |
| Archive | MinIO alias, no unbacked audio, free space, transcript linked | [archive](../../references/archive.md) |
| Events | NATS reachable, outbox draining | [events](../../references/events.md) |
| Desktop | tray on the SNI bus, icons, hotkey dispatcher and binding | [capture](../../references/capture.md), [control-surface](../../references/control-surface.md) |
| Deploy and tests | installed unit matches the repo, alert unit, capture guard, test suite | [config-and-deploy](../../references/config-and-deploy.md) |
| Repo hygiene | no decoy `passes.d`, no stale design doc, no legacy `~/audio/inbox` writes | [incident-log](references/incident-log.md) standing traps |

The script prints the matching playbook for the first failing layer at the end.

**Coverage caveat: the board is not end-to-end proof.** It checks the audio
pipeline. It does not cover document pipelines (folder bindings, document
receipts, scan cadence), pipeline compilation, the transcriptions store or the
call log. For those read `wax catalog --json`, `wax documents list` and the hub's
[documents](../../references/documents.md), [pipelines](../../references/pipelines.md),
[transcriptions-store](../../references/transcriptions-store.md) and
[calls](../../references/calls.md). A green board plus a broken document flow is
expected behaviour of the board. If the board and `wax doctor` disagree, the
CLI is authoritative: it runs inside the component against the daemon's resolved
environment, which the board can only approximate from `/proc`.

`scripts/tests/test_transcriber_resolution.sh` pins the resolution order behind
`same-checkout` (daemon pin, then repo shim, then PATH) with fixtures only.

## Step 2: read exactly one playbook

Load the reference for the **first** failing layer (table above), plus:

- [reason codes](references/reason-codes.md): what each `reason_code` means and
  the first move. Read it whenever a pass is `failed` or `skipped`.
- [incident log](references/incident-log.md): **read this early.** Every outage
  this pipeline has had, with its signature and real root cause, plus the
  standing traps that will mislead you.
- Symptom shortcuts:

| Symptom | Go to |
|---|---|
| "I transcribed a lot and there are no transcripts" | Check intake before the pipeline: ledger `first_seen`, hotkey scopes, Syncthing. `Ctrl+BackSpace` is vinyl dictation, not Wax (the `vinyl` hub skill, `components/vinyl/skills/vinyl/SKILL.md`). See [intake](../../references/intake.md). |
| Titles missing or odd, chain evidence shows `missing_model` | [passes/model-chain](../../references/passes/model-chain.md) |
| Wrong respellings | [passes/dictionary](../../references/passes/dictionary.md) |
| Item `failed` with `archive_failed` | [archive](../../references/archive.md) (transcription did not run) |
| Stream `error-partial` or stuck `not-ready` | [capture](../../references/capture.md) |
| A call has no metadata or announcement | [calls](../../references/calls.md) |
| An n8n node or workflow misbehaves | [n8n](../../references/n8n.md) |
| Several failures, want tickets | [failures](../../references/failures.md) |
| Authoring or changing a pass | the create-enrichment-pass sub-skill, [SUBSKILL](../create-enrichment-pass/SUBSKILL.md) |

## Step 3: fix, verify, land

1. Apply the smallest fix the playbook names.
2. Re-run the layer that was red (`doctor.sh --layer <layer>`) and/or the
   matching `wax doctor` probe. The check that was red must go green.
3. If you changed anything under `components/wax/src` (the Python runner),
   restart the daemon: `systemctl --user restart waxd.service`. The daemon does
   not hot-reload Python. Live-registry YAML is re-read on each call; compiled
   graphs adopt config changes only through an explicit
   `wax pipeline import ... --name ...`, and existing minted plans stay frozen
   until `wax ep replan <item> --apply`. A plain `waxd` restart preserves the
   encoder scope. Never restart `hermes-gateway` with raw systemctl; use
   `hermes-gateway-restart --reason "<why>"`.
4. Re-run the full `doctor.sh`. Confirm you did not push the failure
   downstream.
5. Commit and push. Never leave a repaired pipeline uncommitted. Run
   `git unpushed` and land what it lists before you report done.

## Rules that keep you from making it worse

- **The audio is the irreplaceable artifact and is never deleted.** Not by you,
  not by a script, not "temporarily". If a check reports unbacked audio, that is
  the only real emergency on the board: stop and resolve it first.
- **Read-only until you know the cause.** Every check in the script is
  read-only on purpose. Do not `wax reconcile --rebuild`, do not restart, do not
  move files to "see if it clears". The state on disk is the evidence.
- **`~/audio/` is retired.** The live root is the repo (`WAX_ROOT`, default
  `~/HeyMa`), so `inbox/`, `stream/`, `var/`, `archive/` are all inside it.
  Anything under `~/audio/` is a leftover and lies convincingly.
- **Resolve paths from the running daemon, never from a doc or your own PATH.**
  `/proc/<pid>/fd` says which ledger is open; `/proc/<pid>/environ` says which
  root and which `WAX_TRANSCRIBE` pin are in effect. Never use a PATH lookup for
  `wax`: `/usr/bin/wax` is a stale copy. Use `~/HeyMa/bin/wax` or `mise run wax:*`.
- **Do not judge the ledger by `wax.db`'s mtime.** WAL mode leaves it untouched
  for hours while every write lands in `wax.db-wal`.
- **Never write a credential into a file, and never print a resolved one.**
  Keys resolve at call time from `op://DeLoSecrets/...` item-UUID references;
  `wax doctor` masks credential-named values.
- **Do not widen a threshold to fix a scan problem.** For dictionary
  false positives look at run edges and `heard:` lists first.
- **No Anthropic model, anywhere,** as a fix for a chain problem. Fund the
  account or wait; do not re-scope the token or drop chain members.

## When everything is green and the user still has a problem

Then the failure is in something the checks do not yet cover, which means the
coverage has a gap, and closing it is part of the fix. Add the check to
`scripts/doctor.sh` in the right layer (or a probe to `wax doctor` in the
component), with a failure hint that names the next step, add a drift test if a
fact can rot, and add the incident to [references/incident-log.md](references/incident-log.md).
A troubleshooting skill that does not learn is just documentation.

The same rule applies to the board itself: **a check that resolves a path
differently from the thing it checks reports fiction** (2026-10-17), and **a
`grep -q` on the right of a pipe under `set -o pipefail` turns a healthy command
into a false FAIL** (found 2026-11-05 in `ep-registry-correct`; capture output
first).
