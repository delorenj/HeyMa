# HeyMa shell: one tray, one status

The shell is the client side of the HeyMa umbrella. It owns no capture, no worker and no state; it asks each product for its status through that product's own CLI or endpoint, and it draws one icon. Design of record: `docs/heyma-shell.md`. Code: `components/heyma-shell/` (package `heyma_shell`, stdlib only). Stories S1 (registry), S2 (one Linux tray) and S5 (the Mac app, below) are built; S3, S4, S6 onward are not.

## What runs

| Piece | What it is |
|---|---|
| `bin/heyma` | Repo-root shim to `components/heyma-shell/bin/heyma`. No arguments or `--page settings\|telephony\|logs` still opens the GTK4 window (it hands over to `components/wax/bin/heyma`). `heyma status [--json]` prints the registry. `heyma tray` runs the tray |
| `heyma-tray.service` | User unit, `PartOf` and `WantedBy` `graphical-session.target`, `Restart=on-failure`. ExecStart is `bin/heyma tray` by absolute path. Template: `components/heyma-shell/deploy/systemd/user/heyma-tray.service`. Quit HeyMa exits 0, so systemd leaves it down; waxd then shows its own tray (below) |
| `waxd.service.d/10-heyma-tray.conf` | Sets `WAX_TRAY=off`: the HeyMa tray owns the icon, so waxd spawns no tray child while `heyma-tray.service` is active (or activating, during a `Restart=` back-off). waxd asks `systemctl --user is-active heyma-tray.service` every 5 s; after two "not running" answers in a row it shows its own tray again and hands the icon back as soon as the unit is active. A recorder never goes without an icon. Without the variable waxd keeps its tray (the default on a host with no shell) |
| `vinyl-tray.service` | Deb-owned, so it is masked, not edited: `systemctl --user mask --now vinyl-tray.service`. Undo with `--keep-vinyl-tray` (or `unmask`). Vinyl setup/doctor know about the mask since vinyl `33198ee`: see Gotchas |

All three are written by `components/wax/deploy/install-systemd-user`. A default run issues only `daemon-reload`, `reenable` and `mask --now`, so it never restarts waxd and is safe mid-recording; waxd reads the drop-in at its next restart. `--no-heyma-tray` is for a host that should keep the Wax tray: it writes none of the three, and on a host that already has them it undoes them (removes the drop-in, `disable --now` and removes `heyma-tray.service`, `unmask` and `start` a masked `vinyl-tray`). waxd shows its own tray within seconds (the owner is gone) and drops `WAX_TRAY=off` at its next restart. `--keep-vinyl-tray` installs the shell but keeps the Vinyl icon, unmasking it if an earlier run masked it. Every undo step checks the disk first, so a re-run is a no-op; `--dry-run` prints them as `# would …`.

## The registry: `heyma status --json`

`heyma.shell.status.v1` is `{schema, generated_at, sections: [...]}`. Each section is `{id, installed, running, health, summary, actions, detail}` with `health` one of `ok | warn | fail | off` (`off` is not degraded). `heyma status` (text or `--json`) exits 0 when every section is `ok` or `off` and 2 when any is `warn` or `fail`, the same contract as `wax status`. The adapters are in `heyma_shell/adapters.py`:

| Product | Calls | Notes |
|---|---|---|
| wax | `var/state.json` for liveness, then `<repo>/bin/wax --json status` (10 s) for content; the live mirror if the CLI cannot answer | The CLI stamps `updated_at` with its own clock and answers just as well with waxd dead, so liveness is the mirror alone: missing or older than 30 s is `running: false`, `fail`, and the CLI is not called. `wax status` exits 2 on anything non-nominal with its JSON intact, so exit 2 is read as a verdict. Colour verdict is `wax.tray.colour_for`, so the row and the icon cannot disagree |
| vinyl | `vinyl status` (8 s) plus `~/.local/state/vinyl/state.json` | CLI is `VINYL_BIN`, then `/usr/bin/vinyl`, then `~/.local/bin/vinyl`, never PATH. Never imports Vinyl code |
| telephony | `<repo>/bin/telephony --json status` (read-only) | `mode: disabled` is `off` |
| phonograph | `GET <url>/healthz` (4 s) | URL is `HEYMA_PHONOGRAPH_URL` (default `https://phonograph.intellifornia.com`); `HEYMA_PHONOGRAPH=off` hides it. Nothing installs on a client, so "installed" means "pointed at a URL" |

## The tray

GTK3 AppIndicator id `heyma`, title `HeyMa`, run by `/usr/bin/python3` (system gi). The lifecycle is `wax.tray.IndicatorHost` (re-register when the StatusNotifier watcher returns, destroy before rebuild, verify against the watcher), shared with waxd's own tray, as is the Queue submenu (`wax.tray.QueueMenu`). The menu is the static `model.MENU`:

Dictation (runs `vinyl toggle`), Recording (runs `components/wax/bin/wax-toggle`, the hotkey's own entry point, so the notify-send feedback is identical), a separator, the submenus Wax, Vinyl, Telephony and Phonograph (each shown only when its adapter says installed), a separator, Settings… and Quit HeyMa.

- **Wax**: status line, Queue, Open inbox, Settings…, Telephony…. The Queue lists failed and suspect rows as `⚠ Failed: <name> — <cause>: <reason>`. Each opens a submenu with `Why: <evidence>`, Retry, and Skip; Skip becomes an insensitive "no verified backup yet" row for `archive_failed`/`s3_reverify_failed`. Waiting rows get a Skip submenu, completed rows open the transcript, and Clear completed is at the bottom. Below the rows, recordings parked as silent (`snap["silent"]`) appear with their measured level, any `source silent` line, and **Transcribe anyway** (`wax retry <id>`). Every action is a menu row, because dbusmenu hosts (GNOME's AppIndicator) never show tooltips or deliver right-clicks. Every action runs its `wax` command off the UI thread through `wax.desktop.run_wax_action`. A non-zero exit, a timeout or a missing binary raises a `<App>: <action> failed` notification with the CLI's reason (a JSON body's `evidence`/`error`, or a traceback's last line), and re-arms the queue so the greyed row can be tried again. Ledger-only actions count as hung after 120 s; rerun passes is never killed, because passes legitimately run for many minutes. Both trays do this. Each action runs in its own transient systemd user scope (`desktop.own_scope`, `systemd-run --user --scope`), so quitting or restarting a tray, or restarting waxd (whose own tray lives in `waxd.service`), never kills an action it started. Before that, quitting the tray while a Recording-toggle stop was finalizing killed the finalizer (`finalizer_died`, 2026-11-19) and blocked recording until `wax rec salvage`.
- **Vinyl**: status line with endpoint, Copy last trace reference, Vinyl settings… (`vinyl-settings`, found in `/usr/lib/vinyl/bin` or the checkout, for now).
- **Telephony**: status, Silence local ringer, Open Telephony page. The ringer (`wax.telephony_presentation.Ringer`) lives in this process and reads `telephony` from `var/state.json`.
- **Phonograph**: health line, Open Phonograph (`xdg-open`).

Colour: red while dictating or recording (flashing yellow if the mic is silent), yellow if any installed section is degraded, green otherwise. Wax and Vinyl are judged from their state mirrors every second; Vinyl's daemon, Telephony and Phonograph from the registry every 30 s on a worker thread, and a section must read degraded twice in a row before the icon turns yellow (it clears at once). The GLib loop is UI redraw, not a Wax clock.

## Mac app: `components/heyma-mac/`

`HeyMa.app` (`sh.delo.heyma`) is the Mac end of the shell: native Swift, no Python, own `VERSION` (0.5.1), same section model as the Linux tray. It moved here from the Vinyl repo (`components/vinyl/packaging/macos/app`, pointer in `packaging/macos/HEYMA-APP.md`) because it is the HeyMa client, not a Vinyl artifact. It still owns no capture: Vinyl's client LaunchAgent `sh.delo.vinyl.client` owns the mic, RPC and injection.

Menu: Dictation toggle, then Wax, Vinyl, Phonograph, then Settings… and Quit.

- **Wax** is a named seam, `waxSectionItems()` in `main.swift`, empty until the Voice Memos bridge (S6, HEYMA-35) fills it; an empty list adds no item, so there is no dead entry.
- **Vinyl**: `Connected: host:port` from the client's `status` field `server` (client 0.5.1+; on 0.5.0 the app falls back to `~/.config/vinyl/client.json`). If client.json differs from the running endpoint it shows `Restart the client to use X` and a **Restart client** item (`launchctl kickstart -k gui/$UID/sh.delo.vinyl.client`, disabled while dictating). `Server unreachable: X` shows (and the first row then reads `Server: X`, never `Connected:`) when status carries `server_reachable: false` or the client's `client start HOST:PORT: …` error names a socket-level connect failure (refused, timed out, no route, DNS); a busy server, bad token or microphone failure carries the same prefix and is shown as the client error instead. Cancel dictation, Copy trace reference, Open client log. Every NSMenu sets `autoenablesItems = false`, so the `isEnabled` gating is real. Only start/stop/cancel/save/restart set `busy`; the 3 s status poll has its own flag and never refuses a hotkey press.
- **Phonograph**: `GET /healthz` every 30 s and Open Phonograph (`HEYMA_PHONOGRAPH_URL`, `HEYMA_PHONOGRAPH=off`), the same contract as the Linux adapter.
- A Ctrl-Opt-D press the app drops (client unreachable, or a command in flight) beeps and flashes `offline`/`busy` on the status item for 1.6 s (Vinyl story 2.7B).

### Build, sign, notarize, release

```sh
components/heyma-mac/release.sh                  # from big-chungus: build + sign + notarize + staple
components/heyma-mac/release.sh --no-notarize    # sign only
```

`release.sh` rsyncs the directory to a fresh `/tmp/heyma-mac-release-<ver>` on the Mac (`ssh mac`, mommymac) and runs `mac-sign-notarize.sh` there in **one ssh session**, because headless `codesign` only works when `security unlock-keychain` ran in the same session. Stdin carries the secrets, in order: login keychain password, API key id, issuer, the `.p8`; each is an `op read` of an `op://DeLoSecrets/…` reference on big-chungus (keychain `ghbrrlh7q3m2vdbltyxkbrd77y/password`, notary item `7edxjfddpg2e5vy5qjnjxuvoq4`). The key lands in a 0600 temp file that a trap removes; nothing secret is in a repo file, an argv on big-chungus or any output. Then: `build-app.sh` (universal, `--options runtime --timestamp`, identity `Developer ID Application: Jarad DeLorenzo (YJ7W7Z36FP)`, no entitlements file because the app captures no audio and loads no unsigned code), `xcrun notarytool submit --wait`, `stapler staple` and `validate`, `codesign --verify --deep --strict`, `spctl -a -vvv -t exec`, then a **second** `ditto` of the stapled bundle (the submitted zip held no ticket). Output: `dist/HeyMa-<ver>-macos-universal.zip` plus size and sha256. A rejected submission prints the notarytool log and stops; do not retry with another credential.

Publish: `gh release create heyma-v<ver> -R delorenj/vinyl-client-releases --latest=true <zip>`. The tag is **`heyma-v<ver>`**, never `v<ver>` (that namespace is the Vinyl client). Read the asset back anonymously and compare sha256. Then in `~/code/homebrew-tap` bump `Casks/heyma.rb` `version` and `sha256` (the url derives from `version`); the cask has no quarantine workaround and its livecheck matches only `heyma-v<ver>` tags. Tap CI (`test-tap.yml`, `macos-15` and `macos-15-intel`) asserts the Developer ID authority, team `YJ7W7Z36FP`, hardened runtime, a stapled ticket and `spctl` acceptance with the quarantine left in place.

Released: **0.5.1**, 2026-11-18, source `0736f2d` (review fixes: connect-only server-down, real disabled items, `Server:` row, poll never refuses presses), notarization submission `de6558b0-2a19-4272-ba99-117ef987a328` (Accepted), 504,335 bytes, sha256 `9624a7f60dd17f6535a13c61ca46c2a8df7a55a49aedba2f261ec91f65d92b03`; tests: `mise run heyma-mac:test` (`release.sh` runs them first). Earlier: **0.5.0**, 2026-11-18, source `a450c05`, notarization submission `c6d21652-ff32-4c50-b281-5362027a7de1` (Accepted), 503,121 bytes, sha256 `5af898a6d4f06d551c58e1c255fc6d7839ac5ce78b231659844be68b77033efe`. The signing identity's certificate expires 2027-02-01; renew it before then or the next release fails at `find-identity`.

### On mommymac

`brew update && brew upgrade --cask delorenj/tap/heyma` upgrades the app. mommymac's Vinyl client is the **direct install** (`~/.local/share/vinyl`), not a Homebrew formula, so `brew upgrade vinyl-client` does not apply and a Brew client must not be added next to it: upgrade it with the client archive instead (`install.sh` with no flags keeps the interpreter, config and token), then `launchctl kickstart -k gui/$(id -u)/sh.delo.vinyl.client`. `ssh mac` has no Homebrew on PATH; use `/opt/homebrew/bin`. TCC grants (Microphone, Accessibility) follow the signing identity, so every release signed with this Developer ID keeps them; a switch from the earlier ad-hoc builds can ask once. Do not change them from a script.

## Gotchas

- **Two icons after install** means waxd has not restarted since the drop-in landed. Restart it when idle (`bin/wax --json status` shows no recording): `systemctl --user restart waxd`.
- **No icon** after install: `systemctl --user status heyma-tray`, then `journalctl --user -u heyma-tray -n 50`. The tray never blocks dictation or recording; it only reads and shells out.
- The `tray` block in `var/state.json` reads `external: the HeyMa tray owns the icon` and `available: true` when `WAX_TRAY=off` and `heyma-tray.service` is running. That is healthy, not a missing icon. `fallback: heyma-tray.service is inactive, so waxd shows its own tray` means the HeyMa tray was quit or stopped and the Wax icon is back; `systemctl --user start heyma-tray` returns to one HeyMa icon.
- **A Wax icon instead of the HeyMa one** after Quit HeyMa is the fallback above, not a regression. Only a `heyma-tray` run by hand (`bin/heyma tray` outside the unit) leaves two icons, because waxd judges the owner by the unit.
- **A masked `vinyl-tray.service` is expected.** Since vinyl `33198ee` (2026-11-18), `vinyl setup` enables only the dictation unit when the tray is masked and prints `vinyl-tray.service masked: the HeyMa tray owns the icon`, and `vinyl doctor` reports the row `ok` with the same reason. The checkout (`components/vinyl/bin/vinyl`) has it now; the installed deb (`/usr/lib/vinyl`, 0.5.0) gets it with the next Vinyl deb. Until then the deb's `vinyl doctor` still FAILs that row and its `vinyl setup` needs `--no-enable`. Dictation is unaffected either way.
- The deb's `vinyl-tray` returns on a package upgrade only if the mask is removed; a mask is a symlink in `~/.config/systemd/user`, so upgrades leave it.
- Tests: `cd components/heyma-shell && python3 -m pytest tests`; the shared lifecycle is covered by `components/wax/tests/tray_icon_test.py`.

Related: [control-surface](control-surface.md), [capture](capture.md), [config-and-deploy](config-and-deploy.md), [satellites](satellites.md).
