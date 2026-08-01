# HeyMa

HeyMa is 33GOD's speech pipeline: recording, speech-to-text, transcription,
archival, and enrichment.

Wax is now integrated directly into this repository as HeyMa's recorder and
STT pipeline. Its source, configuration, assets, documentation, and tests live
under `components/wax/`. Stable operator commands remain `bin/wax` and
`bin/waxd`, and its runtime data lives in the ignored project directories
`stream/`, `inbox/`, `var/`, `archive/`, `quarantine/`, and `recovered/`.

The audio is the irreplaceable artifact and is never deleted.

## Commands

```bash
wax status
wax rec start
wax rec stop
wax state stream --cold --json
wax state inbox --cold --json
```

Entering the repository through mise adds `bin/` to `PATH`, so the local
commands run the version integrated here. The daemon is normally managed by
the `waxd.service` user unit. Set `WAX_ROOT` to override the runtime root for
isolated testing or a nonstandard deployment.

See [Wax's design](components/wax/docs/WAX-DESIGN.md) for the architecture, state machines,
recovery guarantees, and operational design.

## Skills

`.agents/skills.json` pins project BMAD skills to the Skillex registry path
`packs/bmad/6.10.2`. CLI-specific skill directories are generated fanouts and
are intentionally ignored. A locally authored verification-lens delta found
during migration is retained under `.skill-overrides/bmad-review/`; it is not
silently mixed into generated vendor content.
