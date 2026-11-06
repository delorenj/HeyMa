# Satellites: the components around Wax

Everything here orbits `waxd` but owns no pipeline stage. Repo paths below are code spans because they live outside this hub; component details belong to each component's own docs.

## heyma-obsidian — the vault plugin

`components/heyma-obsidian` (submodule `delorenj/heyma-obsidian`, at 1cd9f06, 2026-10-22; Plane board HMOB). The Obsidian-side window into Wax. **Status: scaffold.** `src/main.ts` (~105 lines) and `src/settings.ts` are still the sample plugin; no Wax calls are wired yet.

Integration contract (its AGENTS.md, v0):

- Invoke the **absolute** `wax` CLI via execFile; never PATH.
- Read transcript enrichment from the vault's own `metadataCache` (`wax.passes.<slug>` frontmatter).
- Watch `var/state.json` for live state.
- Read `config/passes.d/*.yaml` to list passes; **never write them** — the contract predates `wax ep enable|disable` (S17), which now exists as the operator switch (an overlay in `var/settings.json`; a disabled pass records `skipped`/`operator_disabled`). There is no `ep set`. Update the contract to route enable/disable through the CLI.
- Never open `var/wax.db`.

## remote-audio — SSH audio import

`components/remote-audio` (last commit 2026-10-08). `ssh big-chungus` from a Mac imports the Mac's mic and speakers and **switches big-chungus's PipeWire defaults** while the session lives (`ssh-audio.service`, enabled+active since 2026-10-21; state `~/.local/state/ssh-audio/state.json`). Consequence that bites: Wax capture records the Pulse **default** source, so recording over a remote session captures the remote mic — its README says so explicitly. If a hotkey recording has the wrong room's audio, check whether an SSH session was up. See [capture](capture.md).

## heyma-pm — the Hermes desk

`agents/hermes/pm/role.yaml`: agent_id `heyma-pm`, Hermes profile `heyma-pm` (`~/.hermes/profiles/heyma-pm` exists), runtime repo `agent-hm-heyma-pm`, provisioned 2026-08-21. Bloodbank enabled (gateway scope `fleet`); Telegram/Slack deferred. Ticket provider is Plane, identifier **HEYMA**, per `~/HeyMa/.project.json` (project_id `transcription-queue`, workspace `33god`, board `7485039f-85b0-47db-ad99-d11497f043d7`). `wax failures sweep` files one URGENT HEYMA ticket per failure signature through `px` — see [failures](failures.md).

## Hermes STT adapter

`bin/hermes-transcribe-adapter` (symlinked into `~/.local/bin/`), wired in `~/.hermes/.env` as `HERMES_LOCAL_STT_COMMAND` and `~/.hermes/config.yaml` (`stt.provider: local_command`, `diarize: false`, model `base`). It calls `bin/transcribe` with `--no-diarization --ephemeral`, strips the Python env, and writes `<stem>.txt`; ephemeral runs delete the markdown and the input audio. A **fast lane** (`TRANSCRIBE_FAST_LANE_MAX_S`, default 120 s) keeps short ephemeral clips off the GPU queue: tiny/base model, CPU, separate lock `~/.cache/heyma-transcribe-fast.lock`. Historical vault notes from this path carry the transcriptions-store origin `hermes-stt` (335 rows, newest audio 2026-08-22) and are hidden from `wax transcripts browse` unless `--include-hermes` — see [transcriptions-store](transcriptions-store.md).

## Vinyl — the boundary

Vinyl (submodule `components/vinyl` since 2026-11-05, `~/code/vinyl` is a symlink to it; apt package `vinyl` 0.4.0 since 2026-11-06, code in `/usr/lib/vinyl`) is **live dictation**: push-to-talk speech-to-text that types into the focused window. CPU-only, all-local (sherpa-onnx parakeet-tdt-0.6b-v3-int8), no cloud. Modes: `vinyl daemon` (local), `vinyl serve` (TCP 7733 for thin clients), `vinyl client`, `vinyl live` (a frozen meeting-caption prototype, captions on :7734). Live units 2026-11-05: `vinyld`, `vinyld-serve`, `vinyl-tray`, `vinyl-live` active; `vinyld-client` inactive. Hotkey `Ctrl+BackSpace` → `/usr/bin/vinyl-toggle` (journal scope `app-gnome-vinyl\x2dtoggle-*.scope`); the old `custom4` `Ctrl+KP_Divide` binding is dead.

**Vinyl is not Wax and never feeds it.** Different goal (ephemeral dictated text vs archived audio), different pipeline, different state (`~/.local/state/vinyl/`). Only three things put audio into Wax: the `Ctrl+\` hotkey, the phone via Syncthing, and Telnyx — see the triage table in [intake](intake.md). If someone "dictated a lot and no transcripts appeared," they used Vinyl; that is the whole answer. **The one coupling runs from vinyl to HeyMa, not the other way:** vinyl 0.4.0 (installed 2026-11-06) lazily imports `wax.dictionary` / `wax.jev` from `components/wax/src` and respells committed finals against `Dictionary.yaml` (read-only; edit only via `wax dict`), judged by Jev with vinyl's own key `vinyl-dictionary` and a no-network fallback. Opt-outs: `vinyl dictionary off` (no restart), `VINYL_DICTIONARY=0`, `VINYL_DICTIONARY_JEV=0` (no Jev egress, local policy only); absent file or checkout means off. HeyMa still never imports vinyl, and a dictation mishearing fix belongs in `wax dict heard`, not in vinyl's `vinyl fix`/`rewrites.txt` (those keep only spelled-out acronyms and vinyl-only jargon). It never runs in `vinyl live` or dubplate. Details: vinyl's AGENTS.md "Dictionary correction". Vinyl's own "WAX bridge" roadmap line (drop audio into `~/audio/inbox`) is unbuilt and stale — `~/audio` is a retired root.

Vinyl is a Git submodule at `components/vinyl` (branch `main`) for co-location only: its own repo, versions and releases, and HeyMa code never imports it (architecture spine AD-1 amendment, 2026-11-05). Its knowledge lives in its own hub, the `vinyl` skill: `components/vinyl/skills/vinyl/SKILL.md`. Do not init the submodule on tiny-chungus, where the thin client runs from `~/code/vinyl/.venv`.

## Tarballs

`~/.local/share/wax/packages/` holds built artifacts (e.g. `n8n-nodes-heyma-0.2.0-1f77a72.tgz`, `0.3.0-74755f5.tgz`). They are archives for rollback, never the live install — n8n loads the component working tree through a `file:` link; see [n8n](n8n.md).
