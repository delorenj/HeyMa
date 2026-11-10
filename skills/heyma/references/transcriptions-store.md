# Transcriptions store

Postgres database `transcriptions`, schema `tx`, on the host cluster (`localhost:5432`; never `postgres.delo.sh` or a LAN IP). It holds one identity per vault note (documents excluded; Hermes STT and legacy notes included) and one append-only version per transcription, completed pass, backfill and detected vault edit. Every version restores byte-for-byte. Search (`tx.search`) reads only head tables, so a hit is always the latest version. Code: `components/wax/src/wax/transcriptions/`. Design of record: `components/wax/docs/TRANSCRIPTIONS-STORE.md` (section G lists where the build followed reality over the plan; F.1 is the backfill runbook).

State on 2026-11-05: schema at migration `0005_harvest_links`, 981 identities, 7951 chunks all embedded, `wax transcripts sync --check` reports `drift: false`. Embedder: local Ollama `qwen3-embedding:8b`, pinned by digest, `halfvec(2048)`; frontmatter-only passes cost zero chunk embeddings.

## The store never fails a pass

`passes.run` and `transcribe_adapter` only enqueue a SQLite snapshot (`transcript_snapshots` in `var/wax.db`) and project best-effort behind a 300 s breaker. They never raise. So a store outage shows as `transcripts lagging|error` in `wax status`, never as a failed pass, and re-running passes cannot fix it: the snapshot is already queued.

## How data arrives

The waxd thread `wax-transcriptions` ticks every 15 s: drain the queue, reconcile the vault every 600 s, embed when no item holds the GPU. The moment an item starts it unloads the embedder (Ollama `keep_alive: 0`, default 2 m) because a resident 5.3 GB model starves Whisper's 5 GB VRAM floor and pushes ASR to CPU.

Configuration is the environment first, then the INSTALLED waxd unit's `Environment=` lines: `WAX_TRANSCRIPTS_DSN`, `*_PASSWORD_OP`, `WAX_TRANSCRIPTS_OWNER_*`, `WAX_EMBED_*`. `WAX_TRANSCRIPTS=0` is the rollback switch. Knobs and defaults: [config-and-deploy](config-and-deploy.md).

## Modules and migrations

| Module | Role |
|---|---|
| `store`, `versioning` | Identity and version writes, byte-exact restore |
| `outbox`, `reconcile` | Snapshot queue, vault reconcile and backfill |
| `search`, `chunking`, `embed`, `extract` | Hybrid semantic plus full-text search and the embedding pipeline |
| `browse` | Holocene's read-only feed. Views are the classifications (`monolog`, `meeting`, `phone_call`, `other`) plus `unsorted` and `all` |
| `harvest` | `px` harvest artifacts linked to `tx.harvest_*` (ticket links) |
| `health`, `gates`, `db`, `config`, `migrate` | Probes, invariants, connection, settings, schema |

Migrations: `0001_core`, `0002_search`, `0003_skips_are_history`, `0004_classification_phone_call`, `0005_harvest_links`. Skipped and failed runs are `tx.pass_run` rows only, not versions.

## Roles and secrets

| Role | Used by | 1Password item (DeLoSecrets) |
|---|---|---|
| `transcriptions_owner` | migrations | `op://DeLoSecrets/xgnmsebepsybgd627rtudh2iuq/password` |
| `transcriptions_app` | waxd and the CLI | `op://DeLoSecrets/dncjuqof2przgk6me6ct6f2xma/password` |
| `transcriptions_ro` | outside readers, Holocene browse | `op://DeLoSecrets/jdt6hd7awwzxd376agaszc7z3u/password` |

DSNs in the unit stay password-less. A placeholder `op://DeLoSecrets/REPLACE_WITH_*_ITEM_UUID/password` in a unit means provisioning has not run.

## Commands

See [cli](cli.md) for every flag. The shapes you use most:

| Need | Command |
|---|---|
| What would projection do | `wax transcripts sync --dry-run --json` |
| Project the queue and reconcile | `wax transcripts sync [--embed]` |
| Parity across disk, ledger and store (exit 2 on drift) | `wax transcripts sync --check` |
| Search | `wax transcripts search "phone with the kids" -n 3 [--classification meeting --origin wax --lexical]` |
| Read one | `wax transcripts show <item\|#id\|note name> [--version N] [--raw]` |
| Versions, pass runs, path history | `wax transcripts history <ref>` |
| Schema | `wax transcripts migrate [--dry-run]` |
| Embed now | `wax transcripts embed [--limit N]` |
| Harvest links | `wax transcripts link-harvest [--report]` |
| Clear junk notes out of the vault | `wax transcripts prune` (dry run), then `--apply`; undo `wax transcripts unprune --run <id>` |

mise: `wax:transcripts:sync`, `wax:transcripts:search`, `wax:transcripts:migrate`, `wax:test:pg`.

## Diagnosis

`wax status` prints `transcripts <state> (queued N, embed pending M)`.

| State | Meaning |
|---|---|
| `disabled` | No DSN, or `WAX_TRANSCRIPTS=0` |
| `ok` | Drained and current |
| `lagging` | Queue older than 600 s, last tick could not connect, no or stale waxd tick, or waxd ticks `disabled` because the unit changed after it started (restart waxd) |
| `error` | Queue older than `WAX_TRANSCRIPTS_LAG_FAIL_S` (3600), embed backlog older than 6 h, or a model digest mismatch. `wax status` exits 2 |

`wax doctor` has seven `transcripts *` probes: store reachable and schema current, projection drained, parity, invariants, embeddings current, embedder digest, and the vector index. The index probe fails only when the canonical ANN plan cannot use `chunk_vector_current_hnsw`; its detail says whether `tx.search` (at its own `ef_search = 200`) plans HNSW or an exact scan.

## Pruning junk notes

`wax transcripts prune` (`src/wax/prune.py`) moves voicemails, header-only and
silent notes, self-declared "no content" notes, one-to-three-word misfires and
"Hello? Hello?" calls from `~/d/Transcripts` to `<vault>/.trash/Transcripts/`.
It believes the title and summary (the passes already read the transcript),
but every title/summary rule only judges short notes, so a long meeting that
mentions a voicemail is never touched. Structure (`call-outcome: voicemail`,
a `telnyx-vm-*` source, a call that opens on the voicemail greeting) marks a
voicemail at any length, except a long one: over 60 spoken words
(`LONG_VOICEMAIL_MIN_WORDS`) with a title that says what the message is about
(`prune.meaningful_title`: not a file name, and not itself "voicemail",
"greeting", "empty" or an automated-system title) is a real person's message
and is kept, even when its summary says "voicemail" (operator order
2026-11-10). That keeps long sales pitches too. Short messages still go, and
`--keep voicemail` spares every voicemail. `body-lost` (a body far smaller than the word count recorded
for it, in the note's `word-count` or `transcripts.word_count`) is checked first,
reported and kept: that is a clobbered transcript to recover (the ASR JSON in
`var/logs/<item>/asr.json`, or `wax transcripts history`), not junk. `wax doctor`
runs the same rule (one definition, `prune.body_lost_claim`) as its read-only
"transcript notes intact" probe, so a destroyed body is found without anyone
running a prune; see the [doctor](../subskills/doctor/SUBSKILL.md) sub-skill.

What a prune writes: the note moves (rename, no-clobber, inode-verified; never
unlinked; audio untouched), `transcripts.pruned_at` is stamped so the item's
leftover failed passes leave `wax status`, `wax doctor`, `wax failures` and the
sweep, document rows at the path become `trashed`, and one line per note goes to
`var/prune.jsonl` with the run id. The store is not written: the reconcile sweep
(run once at the end of an applied prune, else waxd's 10-minute tick) stamps
`note_missing_at`, so browse, Holocene and `wax tx` drop the note while
`wax transcripts show/history/search` still find it. `unprune` moves the note
back, clears `pruned_at`, and the next sweep un-stamps the store.

## Provisioning a fresh host

`components/wax/deploy/provision-transcriptions` is a dry run by default; `--apply` creates the 1Password items, roles, database and extensions and fills the unit placeholders. Then commit the unit, run `deploy/install-systemd-user`, `wax transcripts migrate`, restart waxd, and follow the F.1 backfill runbook.

## Do not

- Re-run passes to "fix" store lag.
- Point tests, a scratch ledger or a copied vault at the real DSN: versions are append-only and cannot be deleted. The Wax suite pins `WAX_TRANSCRIPTS_UNIT_FILE` to a nonexistent path for this reason.
- Write a password into the unit, a `.env` or any file.
- Change the embedding model by environment: it is a migration (new model row, shadow re-embed, swap).

Related: [control-surface](control-surface.md) for the browse feed, [passes/dictionary](passes/dictionary.md) for vault edits that become versions.
