# Wax control surface — released 2026-10-16

## Open it

- Read-only catalog, graphs, triggers and outcomes: **https://holocene.delo.sh/wax**
- Document graph authoring: **https://n8n.delo.sh/workflow/GVeFgcnYkSq8SbpX**
- Transcription graph authoring: **https://n8n.delo.sh/workflow/XzKNdDixJMrdu9eh**

The n8n workflows are **unpublished authoring graphs**, not active n8n schedulers.
Their Wax Pipeline node declares the trigger and automatic enablement. Wax runs
the compiled graph. Do not publish the n8n workflows to make Wax run them.

## Active scenarios

| Pipeline | Trigger | Passes | Policy |
|---|---|---|---|
| `transcript-standard` | Wax completes transcription, after verified audio archival | frontmatter-stamp, clipboard-copy, classification, title-slug, diarization | Independent siblings; no new dependency gates |
| `doc-standard` | New Markdown path in `~/d/Inbox/Wax`, scanned by waxd every 120 seconds while pipeline processing is enabled | classification → title-slug | Title requires classification; title also requires the classification meaning field to exist |

`~/d/Transcripts` remains the transcript **output** destination. It is not watched
as a second enrichment inbox. The dedicated document folder resolves to
`~/code/DeLoDocs/Inbox/Wax` on this host.

The first scan seeds existing files without processing them. Subsequent **human
edits do not trigger enrichment**; invoke an explicit document run to enrich an
edit. Wax's own writes do not trigger another run. Failed arrivals retry within
the attempt budget; exhaustion stays visible and needs an explicit retry.

Project extraction and ticket extraction remain disabled. Auto/voice scenarios
have not been invented or activated as part of this release.

## CLI first

From this checkout (installed `wax` is also available):

```bash
mise x -- ./bin/wax catalog --json
mise x -- ./bin/wax pipeline list --json
mise x -- ./bin/wax pipeline show doc-standard --json
mise x -- ./bin/wax pipeline bindings --json
mise x -- ./bin/wax ep list --json
mise x -- ./bin/wax ep status <item-id> --json
mise x -- ./bin/wax documents list --json
mise x -- ./bin/wax documents run doc-standard /absolute/path/note.md --json
mise x -- ./bin/wax documents retry doc-standard --json
```

The catalog omits document bodies, executable argv, pass environment and raw
failure detail. Detailed `ep status` is an operator command, not the hosted read
contract. Enabled/automatic availability is distinct from actually running.

## Change a graph

1. Open its n8n authoring workflow. The Wax Pipeline node's **Configure
   (Authoring)** operation owns trigger, folder bindings, enabled toggle and
   independent/halt policy.
2. Add Wax Pass nodes. Their default **Author (Graph Only)** operation has no
   execution side effects and requires no item ID. Choose **Run (Preview One
   Item)** only for a deliberate CLI-backed manual invocation.
3. Draw from the **continue** output for ordering without success gating, or
   from **requires** for gating. Both are stock n8n main ports. `when` and
   `skipWhen` carry declarative frontmatter conditions. A skipped requirement
   currently satisfies convergence; it does not promise output exists. Use a
   downstream meaning-field condition when the output is required.
4. Save and export the workflow JSON to a host-readable file, then compile it:

```bash
mise x -- ./bin/wax pipeline import /absolute/path/export.json --name doc-standard --json
```

Re-importing identical graph/config/registry inputs is a no-op. A changed import
advances its compiled version. Unsupported nodes, expressions, malformed edges,
cycles and missing dependencies fail loudly. Compiled pass configuration is
frozen until re-import; executable code itself still comes from this checkout.
Existing audio plans never migrate implicitly when a graph is changed.

The hosted `exported_snapshot` state means Wax knows the imported n8n export,
**not** that it has compared the current live editor draft. Re-export/re-import
after editing. Registry drift is shown separately; intentional graph-specific
conditions may differ from registry defaults.

Committed active exports:

- `components/n8n-nodes-heyma/examples/doc-standard-release.json`
- `components/n8n-nodes-heyma/examples/transcript-standard-release.json`

The `wax-*-workflow.json` files remain disabled starter examples. Importing a
starter under a different name does not activate it. Keep exactly one enabled
compiled transcription pipeline; ambiguity is visibly refused in favor of the
legacy registry fallback.

## Runtime deployment

Wax: restart `waxd.service` after Python runner changes. The capture encoder is
in a separate systemd scope; an ordinary daemon restart preserves recording.
Verify the capture and worker heartbeat after restarting.

n8n: build and **pack** the package, store its tarball in a durable local package
directory, install that tarball into `~/.n8n/nodes`, then `mise x -- pm2 restart n8n`.
A local directory symlink is not sufficient proof of n8n discovery. The native
PackageDirectoryLoader test verifies all fourteen types and their icons.
The release package is `~/.local/share/wax/packages/n8n-nodes-heyma-0.2.0-1f77a72.tgz`.

Holocene: API is `holocene-api.service`; web is the `holocene-web` service in the
33god-platform Compose project. Its API invokes the absolute Wax CLI and
allowlist-projects responses. CLI failure is DOWN, never an empty healthy view.

## Live proof

A release-check note was added *after* seeding the empty document inbox. The
worker discovered it automatically and completed both passes in one attempt,
with one durable receipt and unchanged body. Later scans did not re-enrich it.
Two audio items also completed all five passes under the compiled transcription
authority. Holocene rendered both graphs, the document receipt and worker
freshness. The pre-existing two failed diarization rows remain visible in the
registry-wide catalog; this release does not hide or bulk-retry that backlog.
