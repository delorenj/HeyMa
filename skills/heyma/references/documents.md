# Documents

A document run enriches a plain Markdown note, not audio. Documents are content-addressed `doc-<sha16>` per folder binding in the `documents` table of `var/wax.db`; they never become a `transcripts` row. Code: `components/wax/src/wax/documents.py`, `hygiene.py`, `pdf_intake.py`. Design: `components/wax/docs/ENRICHMENT-PASSES.md`.

## What runs

Only passes whose YAML declares `capabilities: [document]` run. Others are `skipped` with `inapplicable_capability`, and a dependency gated on a skipped pass still runs. A document run applies frontmatter and body only, and rejects pass `events`. Identity survives Wax's own writes (`current_sha256`), so Wax's edits never trigger another run. Retries adopt the failed row.

Receipts are per (binding, document, pipeline, compiled definition) with an attempt bound (`WAX_DOCUMENT_MAX_ATTEMPTS`, 3). On exhaustion the row is dead-lettered (`exhausted_at`) and stays visible until `wax documents retry`.

## Bindings

The `documents` pipeline `doc-standard` (v10, 2026-11-05) binds the dedicated folder `~/d/Inbox/Wax`, which resolves to `/home/delorenj/code/DeLoDocs/Inbox/Wax` on this host (binding `doc-standard-Wax-4a02b5d4cb30`). On 2026-11-05 it held 8 documents: 4 processed, 3 gone, 1 failed (exhausted).

| Rule | Detail |
|---|---|
| New arrivals only | The first scan seeds existing files without processing them. A missing folder never claims seeded |
| Cadence | The waxd worker scans every `WAX_DOCUMENT_SCAN_INTERVAL_S` (120 s), while pipeline processing is enabled |
| Human edits do not re-enrich | Invoke `wax documents run <pipeline> <file>` to enrich an edit |
| Stale runs | A run older than `WAX_DOCUMENT_STALE_RUN_S` (900 s) is reclaimed |
| Output vault | `~/d/Transcripts` is a transcript output, never watched as an inbox |

Freshness: `wax pipeline bindings` shows `last_scan_at`; `wax catalog --json` carries a `worker` block from `var/worker-heartbeat.json` (live, healthy idle, or down). `wax documents list [--binding B] [--state S]` shows each row; `wax documents backfill <pipeline>` is the only tool that processes seeded history.

## Hygiene

`hygiene.py` keeps the vault's `Inbox/` (recursive, so `Inbox/Wax`) and `Sandbox/` free of dead notes. Folders: `WAX_DOC_HYGIENE_FOLDERS` (default `~/d/Inbox:~/d/Sandbox`), each inside an Obsidian vault.

| Job | Cadence | Does |
|---|---|---|
| Empty sweep | every `WAX_EMPTY_DOC_SWEEP_INTERVAL_S` (300 s) | Moves empty notes to `<vault>/.trash/` (never unlinked, nothing overwritten); `documents` rows whose file vanished become `gone` |
| Untitled sweep | once per local date from `WAX_UNTITLED_SWEEP_HOUR` (3) | Trashes empty `Untitled*.md`; runs the rest through the manual `untitled-standard` pipeline (summarize, then title-slug) and renames to `<title-slug>.md`. At most `WAX_UNTITLED_SWEEP_MAX` (50) per run. An uncompiled pipeline touches nothing |

Safety rules, all mandatory: `*.md` only; no dot-directories or dot-files; no symlinks; mtime older than `WAX_DOC_HYGIENE_MIN_AGE_S` (300 s); emptiness re-checked under the document run lock right before the move; unparseable frontmatter is never "empty". `trashed` and `gone` rows no longer count in `passes.failed`. `wax doctor` has a "document hygiene" probe. `wax documents sweep-empty` and `wax documents sweep-untitled` run the same logic now and record nothing; use `--dry-run` first. The vault-wide tidy beyond these two folders (temp, scratch, copies, empty dirs, Untitled chains anywhere) is `wax vault sweep`: [vault-sweep](vault-sweep.md).

## PDF intake

`pdf_intake.py` converts PDFs found in a binding's folder into Markdown notes with the `pdf2md` converter, then the normal document run applies. State is `var/pdf-intake/<binding>.json`.

| Limit | Value |
|---|---|
| Minimum file age | 60 s |
| Converter timeout | 300 s |
| Scan budget | 60 s per scan |
| Attempts | 3 |

`WAX_DOCUMENT_PDF_INTAKE=0` disables it. The `WAX_PDF_INTAKE_*` family tunes the limits (see [config-and-deploy](config-and-deploy.md)).

## One-off passes over legacy notes

`components/wax/deploy/run-document-pass` runs a document pass across legacy notes. It is a dry run unless `--apply`. Prefer a pipeline binding for anything recurring.

## Gotchas

- A document run never appears in `wax ep status <item>`; it is not an audio item. Use `wax documents list`.
- The legacy audio board in the doctor skill is not proof of document-binding health. Check `wax pipeline bindings` and `wax documents list --state failed`.
- Naming a pipeline in `wax documents scan <pipeline>` runs it even when it is `enabled: false`.

Related: [pipelines](pipelines.md), [passes/catalog](passes/catalog.md).
