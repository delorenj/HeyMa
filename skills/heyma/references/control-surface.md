# Control surface

Wax owns every artifact mutation, receipt, retry and schedule. The control surface is a set of thin clients over the `wax` CLI: Holocene reads, n8n authors and previews, the tray and HeyMa window act. None of them holds pipeline state. Verified 2026-11-05; the older `components/wax/docs/CONTROL-SURFACE.md` (released 2026-10-16) is stale on pass lists, node counts and the project-extraction status, so treat this file and the CLI as the truth.

## Who may do what

| Surface | Reads | Writes | How |
|---|---|---|---|
| Holocene Wax tab | `wax --json catalog` only | nothing | Read-only. Allowlist projection of the catalog |
| Holocene Transcripts | `wax --json transcripts browse`, `wax --json transcripts search --lexical` | nothing | The `ro` Postgres role; no bodies |
| n8n authoring workflows | catalog (live pass options) | graph authoring only | **Unpublished** authoring graphs; never schedulers |
| n8n adapter nodes | CLI output | manual or preview runs, explicit compiles | Absolute Wax CLI; never executes pass scripts |
| Tray and HeyMa window | `var/state.json` | CLI actions | Own no state |

n8n can never become a second scheduler: no exported n8n trigger can join an import, and the Wax Pipeline node only declares trigger and enablement. See [n8n](n8n.md) for the nodes and releases.

## Holocene

| Piece | Detail |
|---|---|
| Wax route | `GET /api/modules/wax/catalog`, code `~/code/33GOD/holocene/apps/api/src/wax.ts` |
| CLI call | `execFile` of `wax --json catalog` using `WAX_EXECUTABLE` (default `/home/delorenj/HeyMa/bin/wax`), timeout 10 s, cache TTL 4 s |
| On CLI failure | Serves the last good read marked DOWN. Never an empty healthy view |
| Transcript routes | `/api/modules/transcripts/list`, `/search`, `/:id` in `transcripts.ts` |
| Web | `apps/web/app/wax`, `apps/web/app/transcripts`; hosted at `https://holocene.delo.sh/wax` (returns a 307 to auth when unauthenticated) |
| Services | API is the user unit `holocene-api.service` (active); web is the `holocene-web` service in the 33god-platform Compose project |

The tab draws the frozen graph (gated versus continue edges, conditions, drift), recent runs from the durable receipts, per-pass outcome tallies, and worker freshness: live, healthy idle, or down (from the `var/worker-heartbeat.json` heartbeat).

The catalog is allowlist-projected in `components/wax/src/wax/catalog.py`: no `env`, no command argv, no result bodies, no stderr. A key added to the catalog is a key a hosted surface may display, so add keys deliberately. `wax ep status <item>` carries detail and is an operator command, not the hosted contract.

If Holocene shows DOWN: run `wax --json catalog` yourself first. Failure there is a Wax problem; success there means check `holocene-api.service` and `WAX_EXECUTABLE`. "Enabled/automatic" availability is distinct from actually running; read the worker block and recent runs.

## Tray and HeyMa window

The tray is a GTK 3 child process of waxd rendering `var/state.json`. Its menu: Start recording, status, Queue, Open inbox, Settings…, Quit waxd. The HeyMa window (`bin/heyma`, code `components/wax/src/wax/heyma_window.py`) is GTK 4 and libadwaita, single-instance, a collapsible drawer beside a full-width page. Pages and actions:

| Page | Action | CLI |
|---|---|---|
| Settings | Clear finished rows | `wax queue --clear-completed` |
| Settings | Retry failed items | `wax retry` |
| Settings | Pass switches | `wax ep enable\|disable <slug>` |
| Settings | Background worker toggle | `wax pipeline enable\|disable` |
| Logs | Tail | `journalctl --user -f -u waxd.service` (or every Wax unit) |

`components/wax/deploy/install-desktop-entry` writes the `sh.delo.HeyMa` launcher and square icons (the app id is the file name, so the Wayland dock shows the logo); re-run it after moving the checkout. Brand: `components/wax/assets/brand/heyma-logo.png`, accent terracotta.

## Authoring a graph (summary)

1. In the n8n authoring workflow, the Wax Pipeline node's Configure (Authoring) operation owns trigger, folder bindings, enabled toggle and independent/halt policy.
2. Wax Pass nodes default to Author (Graph Only), with no execution side effects and no item id. Run (Preview One Item) is a deliberate CLI-backed manual run.
3. Draw from **continue** to order without gating, from **requires** to gate.
4. Export the workflow JSON and compile it with `wax pipeline import <export.json> --name <name>`.

Full rules, traps and drift semantics: [pipelines](pipelines.md). The two committed active exports are `components/n8n-nodes-heyma/examples/doc-standard-release.json` and `transcript-standard-release.json`. The `wax-*-workflow.json` files are disabled starters.

## Authoring workflows (2026-11-05)

| Workflow | Id | State | Updated |
|---|---|---|---|
| Wax Documents Standard | `GVeFgcnYkSq8SbpX` | inactive (correct) | 2026-10-16 |
| Wax Transcription Standard | `XzKNdDixJMrdu9eh` | inactive (correct) | 2026-11-03 |

Edit at `https://n8n.delo.sh/workflow/<id>`. Active n8n workflows that touch Wax are different things: Transcription Notifications and the Telnyx call workflow, covered in [n8n](n8n.md).

## Runtime deployment

- Wax: restart `waxd.service` after Python changes; the encoder scope survives. Verify capture and the worker heartbeat afterward. See [config-and-deploy](config-and-deploy.md).
- n8n adapter: package version `0.3.0`; tarballs are kept in `~/.local/share/wax/packages/` (`0.2.0-1f77a72`, `0.3.0-74755f5`). On 2026-11-05 `~/.n8n/nodes` references the checkout as a `file:` dependency (a symlink into `components/n8n-nodes-heyma`). The 2026-10-16 doc says a symlink alone is not proof of discovery, so after a rebuild prove it with the native-loader test (`tests/nativeLoader.test.cjs`, run by the package's tests) and then `mise x -- pm2 restart n8n`.
- Holocene: restart `holocene-api.service` after API changes; its web side is in the 33god-platform Compose project.

Related: [system-map](system-map.md), [passes/catalog](passes/catalog.md).
