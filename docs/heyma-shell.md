# HeyMa shell: one client per host

Status: design of record, 2026-11-18. Asked for by Jarad on 2026-11-18 (dictated).
Level (pillar `belsky-doctrine`): the request names Jarad's Linux desktop and his
Mac. The build targets **every HeyMa host**: big-chungus, mommymac, Damian's Mac
Studio, the Omarchy laptop, and any machine added later. Jarad's two machines are
the first users.

## What HeyMa is

HeyMa is the umbrella. It ships five products, and the client is named for the
umbrella, not for any one product. Four turn speech into text; Voxxy (joined
2026-11-23) turns text back into speech:

| Product | Job | Where it runs |
|---|---|---|
| **Wax** | takes a recording (any media file), archives it, transcribes it, runs enrichment passes | `waxd` on big-chungus only (single owner); other hosts *feed* it |
| **Vinyl** | live dictation | `vinyl serve` on a server (big-chungus, or a Mac's own Metal server); `vinyl client`/`daemon` on every desktop |
| **Telephony** | Telnyx calls into Wax, ringer, call log (shadow mode, default off) | big-chungus (inside waxd plus `bin/telephony`) |
| **Phonograph** | live meeting translation (formerly dubplate, repo `~/code/phonograph`) | hosted web app; nothing installs on a client |
| **Voxxy** | text to speech: voice library, cloning, the speaker relay agents and memo replies talk through (repo `~/code/voxxy`) | server at `vox.delo.sh` (GPU engines on big-chungus); the `voxxy` CLI and a `voxxy listen` sink on any desk |

## Corrections to the dictated mental model (verified 2026-11-18)

- **On Linux, Wax already records by itself.** The Wax tray item and Ctrl+\
  (`~/.local/bin/wax-toggle`) both run `wax rec toggle`, and waxd records with its
  own ffmpeg from PulseAudio (`components/wax/src/wax/capture.py`). It does not
  drive an OS recorder. "Wax takes a file, it does not have to record it" is still
  the right contract: every other host feeds Wax files.
- **On a Mac, the recorder really is Voice Memos**, and no bridge exists. Recordings
  reach Wax only by a manual export. Voice Memos keeps recordings in
  `~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings/`
  (macOS 14+; verify on the Mac). That directory is protected by TCC, so the
  process that reads it needs Full Disk Access. Copying it into the Syncthing
  `audio` folder also brings in **iPhone voice memos**, because they reach the Mac
  through iCloud.
- **Phonograph has no service to install on a client.** "Install Phonograph" means
  "point HeyMa at the hosted URL" (and, on the server, mint invites).
- Today there are **three Linux surfaces**: the Vinyl tray (`vinyl-tray.service`,
  GTK3, id `vinyl`), the Wax tray (a child of waxd, GTK3, id `wax`), and the HeyMa
  window (GTK4, `sh.delo.HeyMa`, Wax and Telephony pages only). The Vinyl tray's
  "HeyMa Settings…" opens a fourth window (`vinyl-settings`, `sh.delo.heyma.settings`).
  The Mac `HeyMa.app` is a Vinyl-only menu-bar app whose source lives in the Vinyl
  repo.

## Target experience (the same on Linux and Mac)

One icon. It is red while any microphone is hot (dictating or recording), yellow
when any section is degraded, and green otherwise.

```
HeyMa
  Dictation              on/off      (Vinyl; Ctrl+Opt+D on Mac, Ctrl+Backspace on Linux)
  Recording              on/off      (Wax: waxd capture on Linux, Voice Memos/HeyMa on Mac)
  ───────────
  Wax          ▸  status · queue · open inbox · Enrichment passes ▸ (one checkbox each)
                  · watch folders · recorder hotkey
  Vinyl        ▸  Connected: host:port · server up/down · dictionary on/off · copy trace ref
  Telephony    ▸  mode · ring on/off · silence ringer · recent calls…
  Phonograph   ▸  Open Phonograph · new invite link (server host only)
  ───────────
  Settings…   Setup…   Quit HeyMa
```

A section appears only when its product is installed on, or reachable from, that
host. A vinyl-only laptop shows Dictation plus the Vinyl section. Hiding a section
never stops that product's service.

## Architecture

1. **Component registry, `heyma status --json`.** A small stdlib-only Python CLI
   in this repo (`components/heyma-shell/`, entry `bin/heyma`). Each product has an
   adapter that only calls that product's existing CLI or endpoint:
   `wax --json status`, `vinyl status`, `bin/telephony status --json`, and
   Phonograph `/healthz`. Each adapter returns
   `{id, installed, running, health, summary, actions}`. The tray, the window and
   the setup wizard all read this one shape. HeyMa never imports Vinyl or Wax code
   (the co-location rule), it calls their CLIs.
2. **Linux: one `heyma-tray.service`** (GTK3 AppIndicator, id `heyma`). It reads
   the registry plus the two state mirrors (`var/state.json`,
   `~/.local/state/vinyl/state.json`) for low-latency colour. It replaces
   `vinyl-tray.service` and waxd's tray child. The telephony ringer moves with it.
   It owns no capture and no worker: toggles stay `vinyl toggle` and
   `wax rec toggle`. waxd remains the single owner of Wax work.
3. **One settings window.** The GTK4 HeyMa window gains Vinyl and Phonograph pages
   and absorbs `vinyl-settings`. Wax gets watch-folder and recorder-hotkey settings.
4. **Mac: `HeyMa.app` moves to this repo** (`components/heyma-mac/`), with its own
   version and the same section model, in native Swift. It still runs no Python for
   the shell. Its Wax section is the **Voice Memos bridge**: an FSEvents watcher
   copies finished `.m4a` files (size-stable, never `.composition`, copy and never
   move, a ledger of copied names) into the Mac's Syncthing `audio` send-only
   folder. It detects a missing Full Disk Access grant and deep-links the pane.
5. **Stable code signing.** TCC grants follow the code signature. With today's
   ad-hoc signing, every rebuild can drop the Microphone, Accessibility and Full
   Disk Access grants. A self-signed code-signing certificate (free; key in
   1Password) keeps the designated requirement stable across rebuilds. A Developer
   ID ($99/yr) would also remove the cask's quarantine workaround. That is a
   spending decision for Jarad.
6. **`heyma setup`.** An interactive, idempotent walkthrough: "Install Wax? Vinyl?
   Telephony? Phonograph?" Each step calls the product's own installer
   (`vinyl setup` / `vinyl-server setup` / brew; `components/wax/deploy/install-systemd-user`;
   the Voice Memos bridge plus a Syncthing check on a Mac; telephony readiness only,
   since it stays shadow; the Phonograph URL). It also installs the hotkeys
   (gsettings on GNOME, replacing hand-made dconf bindings) and writes
   `~/.config/heyma/host.toml`, which records what this host runs.
7. **Later: remote Wax status for a Mac.** waxd has no network control plane, so a
   Mac cannot show the Wax queue on big-chungus. A token-authenticated, read-only
   tailnet endpoint would fix that. It is out of the first cut.

## Status

S1 and S2 are built (2026-11-18): `components/heyma-shell/`, `heyma status [--json]`, `heyma tray`, `heyma-tray.service`, `WAX_TRAY=off`. S5 is built (2026-11-18): `components/heyma-mac/` (HeyMa.app 0.5.0, section model, Vinyl 2.7B, Developer ID signed and notarized; the self-signed-certificate idea in item 5 above was superseded by Jarad's Developer ID). The Wax section of the Mac app is the seam S6 fills. Operator route: `skills/heyma/references/shell.md`.

## Joining HeyMa: the contract for a new product

Recorded 2026-11-23 when Voxxy joined (scope:
`~/code/voxxy/_bmad-output/planning-artifacts/voxxy-heyma-integration-scope.md`).
A product gets a HeyMa section when it meets all six:

1. A CLI or endpoint that reports status as JSON with a stable exit code (0 nominal, 2 degraded, 3 or more unreachable or failed). Reading health needs no credentials.
2. One adapter in `heyma_shell/adapters.py` that calls only that CLI or endpoint and returns the shared row shape. An adapter that raises becomes a failed section, never a failed registry (`registry._safe`). Add it to `PRODUCTS`, `ADAPTERS` and `quick_installed`, with a `HEYMA_<PRODUCT>=off` switch.
3. Tray actions call the product's own CLI; HeyMa never does the product's work. A read-only section never turns the icon red: red is a hot microphone only.
4. A `heyma setup` step that calls the product's own installer and records the host's role in `host.toml`.
5. A Mac story when the product has anything to do on a Mac (native, no Python).
6. The product's repo owns its stories; HeyMa owns the adapter, tray row, settings page and setup step.

## Stories (epic: HeyMa shell)

| # | Story | Host | Needs a Mac build |
|---|---|---|---|
| S1 | Component registry and `heyma status --json` (wax, vinyl, telephony, phonograph adapters) | any | no |
| S2 | One Linux tray: `heyma-tray.service` with top toggles, four sections and a combined colour; retire `vinyl-tray` and waxd's tray child; move the ringer | Linux | no |
| S3 | One settings window: Vinyl and Phonograph pages, absorb `vinyl-settings`, Wax watch folder and recorder hotkey | Linux | no |
| S4 | `heyma setup` on Linux: per-product install/enable, hotkeys, `host.toml` | Linux | no |
| S5 | `HeyMa.app` source into this repo, own versioning, section model, stable signing, Vinyl story 2.7B (connected endpoint, restart and server-down hints, press feedback) | Mac | yes |
| S6 | Voice Memos bridge (Wax section and Recording toggle on Mac), Full Disk Access detection | Mac | yes |
| S7 | `heyma setup` on Mac: first-run sheet driving brew, pairing and permissions in order | Mac | yes |
| S8 | Read-only remote Wax status for non-server hosts | any | no |
| S9 | Voxxy section: `voxxy` adapter (`voxxy health --json`), tray submenu with "Say a test line" and "Open Voxxy" (built 2026-11-23) | any | no |
| S10 | "Speak on this desk" toggle: tray row calling `voxxy sink toggle` (Voxxy owns the `voxxy-listen.service` unit) and showing the local unit state (built 2026-11-23) | Linux | no |
| S11 | `heyma setup voxxy`: calls `voxxy setup --json` (server URL, `op://` key reference in Voxxy's own config, optional `--speak-here`) and records the role in `host.toml`. First step of the S4 step registry (`heyma_shell/setup.py`); server role (`voxxy daemon install`) stays manual on a GPU host (built 2026-11-23) | Linux | no |
| S12 | Native Mac sink listener (Swift SSE plus AVPlayer) and Voxxy section in HeyMa.app. After S9 to S11 work on Linux | Mac | yes |

Order: S1 → S2 (the "one icon" Jarad sees on Linux) → S5 + S6 (the Mac
consolidation and Voice Memos intake) → S3, S4, S7 → S8.
