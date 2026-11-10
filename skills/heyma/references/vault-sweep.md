# Vault sweep (`wax vault sweep`)

The whole Obsidian vault's tidy: the vault-wide sibling of `wax transcripts prune`. It prunes empty, temp and scratch files, titles or trashes `Untitled*` chains, consolidates copies, and files loose root notes into `Inbox/` and removes empty directories. Code: `components/wax/src/wax/vault_sweep.py` (its docstring holds this table too). Tests: `components/wax/tests/vault_sweep_test.py`.

The vault is the nearest ancestor of `WAX_VAULT` holding `.obsidian/`: on big-chungus `~/code/DeLoDocs` (`~/d`). It is Syncthing-synced and is not a git repo, so the journal is the only undo.

## Running it

```bash
wax vault sweep                 # dry run: lists every planned change, changes nothing
wax vault sweep -v              # also lists held files (young, linked, locked, collisions)
wax --json vault sweep          # counts per action and outcome, plus every item
wax vault sweep --only prune-temp --only untitled
wax vault sweep --apply         # do it; prints the run id
wax vault unsweep --run 20261110T030000Z --dry-run
wax vault unsweep --run 20261110T030000Z
```

Exit codes: 0 clean, 2 on any error or refusal. That includes another apply run holding `var/vault-sweep.lock`, an uncompiled `untitled-standard`, a `WAX_VAULT` outside a vault, an unknown `--only`, and `--limit` below 1 (`0` is refused, never "unlimited"). A refusal never reads as "nothing to do". `unsweep` exits 2 when it skipped anything or restored nothing.

One bad file or one failing step never ends the run. It becomes an `errors` line (exit 2) and the rest carries on, so the run id is always printed and `meta.vault_sweep_last_run` is always written. A file or directory whose name is not valid UTF-8 is listed under `skipped` (printed as `SKIPPED`), not fatal.

Nightly scheduling belongs to Candybar (a manifest runs `wax vault sweep --apply`). Never add a timer, cron or waxd loop for it. `wax doctor` has a "vault sweep ran" probe: PASS before the first apply run, WARN when the last run is over 3 days old or recorded errors.

## Rules

Each file gets at most one action per run, in this order. A held file is claimed and left alone; a report does not claim it.

| Action | Matches | `--apply` does |
|---|---|---|
| `prune-empty` | A zero-byte file of any type (not `__init__.py` or `py.typed`). A `*.md` with a blank body whose frontmatter holds only blank values or creation stamps (`created`, `modified`, `updated`, `created_at`, `updated_at`, `wax`, `wax-item-id`). `aliases`, `tags`, `date`, `id` and `cssclasses` are the author's, so a note carrying one is not empty. An unfilled Templater stub, such as a daily note whose template never expanded (operator order 2026-11-10): at least one unexpanded `<% ... %>` tag, only stamps in the frontmatter, and nothing in the body but headings, tags, empty list items and unchecked tasks that at least 3 stubs in the same folder share. A ticked or `[/]` box, one typed word, or a task only that note has keeps it | Trash, unless another note links to it |
| `prune-temp` | By suffix, a **dropping** (`.tmp .temp .swp .swo .part .partial .crdownload`, or a trailing `~`): trashed whatever it holds. A **backup** (`.bak`, `.bak-*`, `.orig`): trashed only while the original sits beside it, else reported (it may be the only copy). By name, a stem that is wholly `tmp`, `temp` or `tmep` (plus up to 3 digits), `scratch` or `scratchpad` (plus digits), `<x>-scratch` or `<x>_scratch`, or Python's `tmp` plus 8 lowercase chars with a digit: trashed whatever it holds (operator order 2026-11-10: "I don't want any of them"; `wax vault unsweep` undoes it). Only the whole stem counts, so `Learning Scratch.md` or `Build a Database from Scratch.md` never match. Age floor is the larger of min-age and `WAX_VAULT_SWEEP_TEMP_MIN_AGE_S` (7 days) | Trash, unless another note links to it |
| `untitled` | A stem like `Untitled`, `Untitled 2`, `untitled-3` or `UNTITLED CHAT 2024-12-01 15 18 32 1` | A `*.md` that is blank or stamp-only (only `created`, `pipeline-status` and similar auto keys): trash. A `*.md` with content: keep a pre-image, run `untitled-standard` (summarize, then title-slug) and rename to the slug, at most `WAX_UNTITLED_SWEEP_MAX` (50) per run. A slug that is itself untitled is `unsafe_title`; one that differs from a sibling only by case is a `collision`. A default `.base` or empty `.canvas`: trash. Anything else: report |
| `consolidate` | `X copy`, `X copy 2`, `X (1)`, a Syncthing `.sync-conflict-` or a `conflicted copy` beside `X`. A bare `X 1`..`X 9` counts only while `X` itself exists | A copy identical to the survivor (`X`, else the oldest copy; same bytes, or same text after CRLF and trailing-space normalisation): trash. `X` missing and exactly one **Syncthing conflict** file left: rename it to exactly `X`. A lone `X copy` or `X (1)` is a title somebody chose (`Marketing copy`, `Sprint (2)`), so it is reported, never renamed. Differing copies, bare-N orphans and same-folder notes with identical content under different names: report only |
| `organize` | A loose `*.md` in the vault root that is not ALL-CAPS and not Home, Index, Dashboard or README. An empty directory below the top level | Move the note to exactly `Inbox/<name>` (refused on collision, case-insensitively). `rmdir` the directory, deepest first |

What the dry run lists is what `--apply` does: a survivor trashed earlier in the same run is treated as gone in both.

## Hard exclusions

These are checked in one choke point (`Sweep.refusal`, plus the walk's pruning), at plan time and again under the document's run lock right before each move:

- Dot-files and dot-directories (`.obsidian`, `.trash`, `.git`).
- `_`-prefixed directories (`_vault` templates, obsops `_triage` lanes).
- Symlinks. They are never followed or moved, and a file whose real parent leaves the vault is refused.
- `Transcripts/` and the real `WAX_VAULT` (`wax transcripts prune` owns them).
- `Inbox/Wax` and every folder a document pipeline is bound to (`wax pipeline bindings`).
- The Obsidian template folders.
- `WAX_VAULT_SWEEP_EXCLUDE` (`:`-separated, vault-relative; default `agents`).
- Code projects. Below the vault root, a directory holding `.git`, `package.json`, `pyproject.toml`, `mise.toml`, `__init__.py`, `node_modules`, `requirements.txt` or a similar marker is skipped whole. `node_modules`, `.venv`, `venv`, `__pycache__` and `target` are never entered.
- Files whose mtime **or ctime** is younger than `WAX_VAULT_SWEEP_MIN_AGE_S` (86400), or larger than `WAX_VAULT_SWEEP_MAX_BYTES` (16 MiB). A file Syncthing just wrote, or somebody just put back, is new whatever its mtime says.
- A file the sweep trashed or moved earlier that somebody put back (`unsweep`, or by hand out of `.trash`): same inode, mtime and size as a journal line. It is kept, not swept again, until its content changes.
- A document whose run lock a live pipeline run holds.
- The link guard. A temp file, copy, untitled, empty or root note is held if anything links to it: a wikilink (table-escaped `[[x\|alias]]` too), a markdown link, an HTML `href`/`src`, a canvas `file`, a quoted name in a `.base`, or a quoted path or file name in an `.obsidian` plugin or bookmark config (the UI-state files `workspace*.json` and `graph.json` and the recent-files plugin's open history do not count; a bare word such as `"Untitled"` is a label). Links are matched on the target's base name, a config path on its exact vault path, and every `bookmarks.json` entry is an exact vault path (a bookmark `scratch.md` is the vault-root file, not every scratch.md). Linkers are read leniently and in full, however large or oddly encoded. A linked file is never trashed, renamed or moved.

## Journal and undo

Every change is a move or an `rmdir`. Nothing is unlinked or overwritten:

- Trash goes to `<vault>/.trash/<relpath>`, with `-1`, `-2` suffixes when needed.
- Renames and moves are exact, using `renameat2(RENAME_NOREPLACE)`.

Each change is appended to `var/vault-sweep.jsonl` (with fsync) BEFORE the move. A line carries the run id, action, op (`trash`, `rename`, `move`, `rmdir`, or for the untitled pipeline `preimage` and `rewrote`), `from`, `to`, inode, size, mtime and sha256, and the document rows with their prior state.

`.trash` is in the vault's `.stignore`, so Syncthing peers see a trashed file as a deletion; the only copy lives in this host's `<vault>/.trash`. Do not empty it casually.

A file written between the plan and the move is put back, and a `reverted` line is added. A failed move adds an `aborted` line.

Ledger effects:

- Trashed documents become `trashed`.
- Moved and renamed documents are re-pointed.
- `meta.vault_sweep_last_run` records the last apply run.

`wax vault unsweep --run RUN` replays the run newest first:

- It moves a file back only when `to` is still the same inode (never over a file now at the original path).
- It restores document states and paths.
- It recreates removed directories.
- It puts a note the untitled pipeline rewrote (`summary`, `title-slug`, the `## Summary` section) back byte for byte, with its old mtime, from the pre-image under `<vault>/.trash/.vault-sweep/<run>/`. The pre-image is written and journaled before the pipeline runs. The restore only happens while the note is still exactly what the pipeline wrote; a note edited since keeps its edits, `unsweep` reports it as skipped (exit 2) and the pre-image stays for you to merge by hand. This is the one place an unsweep overwrites a file.

Each undone change is journaled as a `restore` line, and the next sweep keeps the restored file.

## Overlap with the other sweeps

- `wax transcripts prune` owns `Transcripts/`. The vault sweep never enters it.
- waxd's hygiene sweeps ([documents](documents.md)) keep `Inbox/` and `Sandbox/` clean every few minutes. The vault sweep also covers those folders, behind a longer age floor. Both go through the document run locks.
