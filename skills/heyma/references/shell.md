# HeyMa shell: one tray, one status

The shell is the client side of the HeyMa umbrella. It owns no capture, no worker and no state; it asks each product for its status through that product's own CLI or endpoint, and it draws one icon. Design of record: `docs/heyma-shell.md`. Code: `components/heyma-shell/` (package `heyma_shell`, stdlib only). Stories S1 (registry) and S2 (one Linux tray) are built; S3 onward are not.

## What runs

| Piece | What it is |
|---|---|
| `bin/heyma` | Repo-root shim to `components/heyma-shell/bin/heyma`. No arguments or `--page settings\|telephony\|logs` still opens the GTK4 window (it hands over to `components/wax/bin/heyma`). `heyma status [--json]` prints the registry. `heyma tray` runs the tray |
| `heyma-tray.service` | User unit, `PartOf` and `WantedBy` `graphical-session.target`, `Restart=on-failure`. ExecStart is `bin/heyma tray` by absolute path. Template: `components/heyma-shell/deploy/systemd/user/heyma-tray.service`. Quit HeyMa exits 0, so systemd leaves it down; waxd then shows its own tray (below) |
| `waxd.service.d/10-heyma-tray.conf` | Sets `WAX_TRAY=off`: the HeyMa tray owns the icon, so waxd spawns no tray child while `heyma-tray.service` is active (or activating, during a `Restart=` back-off). waxd asks `systemctl --user is-active heyma-tray.service` every 5 s; after two "not running" answers in a row it shows its own tray again and hands the icon back as soon as the unit is active. A recorder never goes without an icon. Without the variable waxd keeps its tray (the default on a host with no shell) |
| `vinyl-tray.service` | Deb-owned, so it is masked, not edited: `systemctl --user mask --now vinyl-tray.service`. Undo with `--keep-vinyl-tray` (or `unmask`). Vinyl 0.5.0 does not know about the mask: see Gotchas |

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

- **Wax**: status line, Queue (retry, skip, open transcript, clear completed), Open inbox, Settings…, Telephony….
- **Vinyl**: status line with endpoint, Copy last trace reference, Vinyl settings… (`vinyl-settings`, found in `/usr/lib/vinyl/bin` or the checkout, for now).
- **Telephony**: status, Silence local ringer, Open Telephony page. The ringer (`wax.telephony_presentation.Ringer`) lives in this process and reads `telephony` from `var/state.json`.
- **Phonograph**: health line, Open Phonograph (`xdg-open`).

Colour: red while dictating or recording (flashing yellow if the mic is silent), yellow if any installed section is degraded, green otherwise. Wax and Vinyl are judged from their state mirrors every second; Vinyl's daemon, Telephony and Phonograph from the registry every 30 s on a worker thread, and a section must read degraded twice in a row before the icon turns yellow (it clears at once). The GLib loop is UI redraw, not a Wax clock.

## Gotchas

- **Two icons after install** means waxd has not restarted since the drop-in landed. Restart it when idle (`bin/wax --json status` shows no recording): `systemctl --user restart waxd`.
- **No icon** after install: `systemctl --user status heyma-tray`, then `journalctl --user -u heyma-tray -n 50`. The tray never blocks dictation or recording; it only reads and shells out.
- The `tray` block in `var/state.json` reads `external: the HeyMa tray owns the icon` and `available: true` when `WAX_TRAY=off` and `heyma-tray.service` is running. That is healthy, not a missing icon. `fallback: heyma-tray.service is inactive, so waxd shows its own tray` means the HeyMa tray was quit or stopped and the Wax icon is back; `systemctl --user start heyma-tray` returns to one HeyMa icon.
- **A Wax icon instead of the HeyMa one** after Quit HeyMa is the fallback above, not a regression. Only a `heyma-tray` run by hand (`bin/heyma tray` outside the unit) leaves two icons, because waxd judges the owner by the unit.
- **`vinyl doctor` FAILs `vinyl-tray.service` ("inactive (dead)") and `vinyl setup` stops at "enable failed"** on a host where the tray is masked. Vinyl 0.5.0 treats its own tray as a hard check and enables it unconditionally (`vinyl/setup.py`, the `enable --now <unit> vinyl-tray.service` step and the doctor unit loop), and systemd refuses to enable a masked unit. Dictation is unaffected. Until Vinyl treats a masked tray as "another shell owns the icon", read that one doctor row as expected, and run `vinyl setup` only with `--no-enable` (it skips the enable step) or after `systemctl --user unmask vinyl-tray.service`, re-masking afterwards. Nothing in HeyMa reads `vinyl doctor`'s exit code.
- The deb's `vinyl-tray` returns on a package upgrade only if the mask is removed; a mask is a symlink in `~/.config/systemd/user`, so upgrades leave it.
- Tests: `cd components/heyma-shell && python3 -m pytest tests`; the shared lifecycle is covered by `components/wax/tests/tray_icon_test.py`.

Related: [control-surface](control-surface.md), [capture](capture.md), [config-and-deploy](config-and-deploy.md), [satellites](satellites.md).
