# HeyMa shell: one tray, one status

The shell is the client side of the HeyMa umbrella. It owns no capture, no worker and no state; it asks each product for its status through that product's own CLI or endpoint, and it draws one icon. Design of record: `docs/heyma-shell.md`. Code: `components/heyma-shell/` (package `heyma_shell`, stdlib only). Stories S1 (registry) and S2 (one Linux tray) are built; S3 onward are not.

## What runs

| Piece | What it is |
|---|---|
| `bin/heyma` | Repo-root shim to `components/heyma-shell/bin/heyma`. No arguments or `--page settings\|telephony\|logs` still opens the GTK4 window (it hands over to `components/wax/bin/heyma`). `heyma status [--json]` prints the registry. `heyma tray` runs the tray |
| `heyma-tray.service` | User unit, `PartOf` and `WantedBy` `graphical-session.target`, `Restart=on-failure`. ExecStart is `bin/heyma tray` by absolute path. Template: `components/heyma-shell/deploy/systemd/user/heyma-tray.service` |
| `waxd.service.d/10-heyma-tray.conf` | Sets `WAX_TRAY=off`, so waxd stops spawning its own tray child. Without the variable waxd keeps its tray (the default on a host with no shell) |
| `vinyl-tray.service` | Deb-owned, so it is masked, not edited: `systemctl --user mask --now vinyl-tray.service`. Undo with `unmask` |

All three are written by `components/wax/deploy/install-systemd-user`. It issues only `daemon-reload`, `reenable` and `mask --now`, so it never restarts waxd and is safe mid-recording; waxd reads the drop-in at its next restart. `--no-heyma-tray` skips the unit, the drop-in and the mask on a host that should keep the Wax tray; `--keep-vinyl-tray` installs the shell but leaves the Vinyl icon.

## The registry: `heyma status --json`

`heyma.shell.status.v1` is `{schema, generated_at, sections: [...]}`. Each section is `{id, installed, running, health, summary, actions, detail}` with `health` one of `ok | warn | fail | off` (`off` is not degraded). The adapters are in `heyma_shell/adapters.py`:

| Product | Calls | Notes |
|---|---|---|
| wax | `<repo>/bin/wax --json status` (10 s), falls back to `var/state.json` | Colour verdict is `wax.tray.colour_for`, so the row and the icon cannot disagree. A mirror older than 30 s is `fail`, never green |
| vinyl | `vinyl status` (8 s) plus `~/.local/state/vinyl/state.json` | CLI is `VINYL_BIN`, then `/usr/bin/vinyl`, then `~/.local/bin/vinyl`, never PATH. Never imports Vinyl code |
| telephony | `<repo>/bin/telephony --json status` (read-only) | `mode: disabled` is `off` |
| phonograph | `GET <url>/healthz` (4 s) | URL is `HEYMA_PHONOGRAPH_URL` (default `https://phonograph.intellifornia.com`); `HEYMA_PHONOGRAPH=off` hides it. Nothing installs on a client, so "installed" means "pointed at a URL" |

## The tray

GTK3 AppIndicator id `heyma`, title `HeyMa`, run by `/usr/bin/python3` (system gi). The lifecycle is `wax.tray.IndicatorHost` (re-register when the StatusNotifier watcher returns, destroy before rebuild, verify against the watcher), shared with waxd's own tray, as is the Queue submenu (`wax.tray.QueueMenu`). The menu is the static `model.MENU`:

Dictation (runs `vinyl toggle`), Recording (runs `components/wax/bin/wax-toggle`, the hotkey's own entry point, so the notify-send feedback is identical), a separator, the submenus Wax, Vinyl, Telephony and Phonograph (each shown only when its adapter says installed), a separator, Settings… and Quit HeyMa.

- **Wax**: status line, Queue (retry, skip, open transcript, clear completed), Open inbox, Settings…, Telephony….
- **Vinyl**: status line with endpoint, Copy last trace reference, Vinyl settings… (`vinyl-settings`, found in `/usr/lib/vinyl/bin` or the checkout, for now).
- **Telephony**: status, Silence local ringer, Open Telephony page. The ringer (`wax.telephony_presentation.Ringer`) lives in this process and reads `telephony` from `var/state.json`.
- **Phonograph**: health line, Open Phonograph (`xdg-open`).

Colour: red while dictating or recording (flashing yellow if the mic is silent), yellow if any installed section is degraded, green otherwise. Wax and Vinyl are judged from their state mirrors every second; Vinyl's daemon, Telephony and Phonograph from the registry every 30 s on a worker thread, and a section must read degraded twice in a row before the icon turns yellow (it clears at once). The GLib loop is UI redraw, not a Wax clock.

## Gotchas

- **Two icons after install** means waxd has not restarted since the drop-in landed. Restart it when idle (`bin/wax --json status` shows no recording): `systemctl --user restart waxd`.
- **No icon** after install: `systemctl --user status heyma-tray`, then `journalctl --user -u heyma-tray -n 50`. The tray never blocks dictation or recording; it only reads and shells out.
- The `tray` block in `var/state.json` reads `external: the HeyMa tray owns the icon` and `available: true` when `WAX_TRAY=off`. That is healthy, not a missing icon.
- The deb's `vinyl-tray` returns on a package upgrade only if the mask is removed; a mask is a symlink in `~/.config/systemd/user`, so upgrades leave it.
- Tests: `cd components/heyma-shell && python3 -m pytest tests`; the shared lifecycle is covered by `components/wax/tests/tray_icon_test.py`.

Related: [control-surface](control-surface.md), [capture](capture.md), [config-and-deploy](config-and-deploy.md), [satellites](satellites.md).
