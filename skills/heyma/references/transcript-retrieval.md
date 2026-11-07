# Retrieve transcripts with `wax tx`

Use `wax tx` for “get the latest transcript”, “get my transcript”, “last transcript for this project”, or “show today's/yesterday's/this week's transcripts”. It reads the Postgres transcriptions store; it does not scan note filenames, sort filesystem timestamps, rerun project extraction, or sync the store. If results seem absent or stale, check `wax transcripts sync --check` and follow [transcriptions-store](transcriptions-store.md); store lag does not fail a pass.

| Request | Command | Result |
|---|---|---|
| Latest overall | `wax tx` | One latest transcript with content |
| Latest for a project | `wax tx HeyMa` or `wax tx --project HeyMa` | One latest matching transcript with content |
| All today's so far | `wax tx --today` | Metadata list |
| Yesterday | `wax tx --yesterday` | Metadata list |
| This week so far | `wax tx --week` | Metadata list, Monday to now |
| Recent metadata, without a date window | `wax tx --list` or `wax tx -n 8` | All-time metadata list, limited when requested |
| Today for a project | `wax tx --today --project HeyMa` | Project-filtered metadata list |
| Machine-readable result | `wax tx --json --today` or `wax --json tx HeyMa` | Latest includes content; list has `rows` and `truncated` |
| Exact original latest content | `wax tx --raw HeyMa` | Latest only, like `wax transcripts show --raw` |

Date windows use the local calendar in `WAX_TRANSCRIPTS_LOCAL_TZ` (default `America/New_York`): today starts at local midnight and ends now; yesterday runs from its local midnight to today's midnight; this week starts Monday at local midnight and ends now. Dates and latest ordering use the transcript's `effective_at` (recorded time, otherwise captured time, otherwise transcribed time), not store arrival or last-enrichment time. `--today`, `--yesterday`, `--week`, `--list`, and `-n`/`--limit N` select listing mode. `--raw` works only for latest, not lists. Hermes transcripts are hidden by default; add `--include-hermes` to include them. The global `--json` flag works anywhere in the command.

Project filtering uses the same `project-ids` (canonical pjangler IDs) and index-aligned `projects` (display names) that the enabled `project-extraction` pass writes. The lookup resolves project ID, name, or aliases with shared registry hints, then filters stored metadata; it does not classify the query or re-enrich notes. Stored canonical IDs work without the project registry; names and aliases consult the registry so canonical IDs take precedence over colliding names. Unknown or ambiguous projects must be reported/clarified, **never silently replaced by an overall lookup**. Latest without a match exits 2; a valid empty listing explicitly reports no transcripts and exits 0. Human list rows carry stable `#id`; read one with `wax transcripts show '#ID'`.

When answering a natural-language request, infer “this project” from the current project's manifest only if it unambiguously identifies one project; otherwise ask which project. For “latest” return the transcript content and its source/reference, not an unsolicited summary. For a list return the metadata and stable references, not invented transcript content. Do not summarize unless asked. The globally active HeyMa hub links here; no second skill is needed.
