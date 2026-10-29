# Design: Holocene Transcripts tab, four-way Jev classification and harvest ticket links

Date: 2026-10-29. This was read-only work. Nothing was modified, no database was queried, and `px harvest` was not run. Every file and line cited below was opened this session unless I mark it "from the report".

## 0. Verification of the research reports

**Confirmed against code:**
- `bin/classification:14` has `CLASSES = {"monolog","meeting","other"}`. It calls a chat-model chain through `provider.run_model_chain` and does not use Jev.
- An existing frontmatter classification is preserved without a model call (`bin/classification:59-62`).
- `classification.yaml` is `version: 1` with no `clobber`.
- `passes.py:864` hard-rejects any classification outside the three values.
- `extract.py:36` is `CLASSIFICATIONS = ("monolog","meeting","other")`, and `extract.py:475-478` raises `ExtractError` on anything else.
- The DB check at `0001_core.sql:165` is `CHECK (classification IN ('monolog','meeting','other'))`.
- `tx.transcript_current.classification` is plain text with no check (`0001_core.sql:342`).
- `tx.search` takes a free `text[]` for classifications (`0002_search.sql:11,72`), so it needs no change.
- Migrations on disk are 0001 to 0003.
- `db.py:27-30` knows only the `app` and `owner` roles. It has no `ro` role.
- `bin/wax:1761` limits `--classification` to the three values.
- `diarization.yaml:19` has `when: classification not_equals monolog`.
- `jev.py` exposes `choice`, `noul` and `decide`, pinned to `jev-1.13.0`.
- `project-extraction` is the one live Jev caller. It is `enabled: false` in its YAML (from the report). I confirmed the code: P at least 0.6, or 0.75 above 30K chars.
- `HANDLED_KEYS` (`extract.py:361`) does not include `harvest-tickets`, `classification-detail` or `classification-confidence`. All three land in `vault_extra`.
- Harvest facts match the report:
  - Item publish blocks are written at `publish.js:391` and `:410`.
  - The block is `{status, provider, project, board, ref, url, marker, lane, at, here}`.
  - `frontmatter.js` writes only `harvest-tickets`, as a URL list.
  - `defaultOutput` writes to `<outputDir>/<slug>/actionitems.json` (`artifact.js:123-129`).
  - `config.js:540` reads `harvest.outputDir`.
  - The storage defaults are in `config.js:482`.
- Holocene facts match the report:
  - `wax.ts` has the allowlist and cache plus stale/down envelope pattern.
  - `server.ts:49` registers `registerWaxRoutes`.
  - `apps/api/package.json` has no Postgres driver.
  - The test script is an explicit file list (line 13).
  - `usePoll`, `Brand`, `ThemeToggle` and `useTheme` are exported from `hub-app.tsx`.
  - `SourceStrip`, `CopyRef`, `formatDuration` and `sinceSeconds` are exported from `hub/ui.tsx`.

**Corrections to the reports:**
1. **`wax.css` uses `--ink-soft` and `--you`, which do not exist in `hub.css`.** I grepped the tokens. The real ones are `--ink`, `--ink-2`, `--ink-3`, `--pop`, `--working`, `--cleared`, `--broken`, `--stuck`, `--sunken`, `--line` and `--line-strong`. The Wax tab only works because of `var(--ink-soft, var(--ink))` fallbacks. The Transcripts CSS must use `--ink-2` and `--ink-3` directly.
2. **The report's Jev probe was on the old chat-pass route.** The probe confirms the Jev route works. I did not re-run it. Treat its calibration numbers (phone at least 0.93, household 0.64 to 1.0) as unverified and re-measure them in step A-8 below.
3. **"A pass can emit `classification-source` as a marker" needs one caveat.** `_apply_result` (`passes.py:866-872`) writes a key only if it is empty, equal, or listed in `clobber`. A new key such as `classification-detail` is written freely, but it cannot be refreshed on re-runs unless it is added to `clobber`. The design below handles this.
4. **The Hindsight/pass preserve rule makes the backfill awkward.** `build_result` returns early at `bin/classification:59-62` when a value exists. The v2 script therefore needs an explicit `--reclassify` mode, because a plain `wax ep run` cannot change an existing value.

---

## A. Classification

### A.1 Taxonomy

The stored identifiers are `monolog`, `meeting`, `phone_call` and `other`.

| id | Display name | Meaning |
|---|---|---|
| `monolog` | Monologues | One person talking to themself or a recorder: memos, ideas, narration |
| `meeting` | Meetings | A work or business conversation, appointment or interview between adults |
| `phone_call` | Phone calls | A telephone call or voicemail, including IVR menus, robocalls, greetings and telnyx call legs |
| `other` | Other | Family and home life, kids, ambient house audio, and anything else |

`monolog` keeps its existing spelling because it is already stored, in `diarization.yaml`, and in the n8n node. The detail label lives in a separate frontmatter key, `classification-detail`, with values `household`, `no_content`, `unsorted` or absent. The "Other" tab headline is "family and household". The detail key lets the UI show a "household" chip and still hold stray `other` rows.

A missing classification is not a category. It is shown in a fifth, de-emphasized pseudo-view called "Unsorted". The UI offers it as the last toggle only when the count is non-zero.

### A.2 Decision procedure (v2 `bin/classification`)

Rules run in this order. The first one that matches wins.

1. **Empty body.** Skip, as today (`reason_code=empty_document`).
2. **Existing value.** Keep it and make no call, unless `--reclassify` is set (A.6).
3. **Telnyx rule.** If `source-audio` (or the filename) starts with `telnyx-`, write `phone_call` with `classification-source: rule:telnyx`. This costs nothing and covers legs of 3 s with 4 to 7 words.
4. **Floor rule.** If the body has fewer than 15 words and `audio-duration-seconds` is under 10, write `other` with `classification-detail: no_content` and `classification-source: rule:floor`. The telnyx rule runs first, so call legs are exempt.
5. **Jev decision.** One request with two questions.

**Question 1, `kind` (choice):**
- `monolog`: one person talking to themself, a recorder or an assistant, reflecting, dictating, narrating or brainstorming.
- `meeting`: a work or business conversation, appointment or interview between adults, in person or on a video call.
- `phone_call`: a telephone call or voicemail, including IVR menus, robocalls and greetings.
- `household`: family and home life, kids, play, meals, chores, car rides, ambient house audio.
- `no_content`: silence, noise, or no usable speech.

**Question 2, `is_phone` (noul):** is this a recorded telephone call?

**State** is `{"recording": {source, speaker_count, duration_s, word_count}, "transcript": head 6000 chars + "…" + tail 2000 chars}`. The recording metadata is a hint, not a rule. The probe showed speaker count does not separate classes (from the report).

**Mapping and thresholds:**
- Accept the top choice if its probability is at least 0.6 (the project-extraction value).
- `household` and `no_content` both store `classification: other`, and the specific label goes to `classification-detail`.
- Disagreement between the questions means abstain. That is `kind != phone_call` while `is_phone > 0.9`, or `kind == phone_call` while `is_phone < 0.5`.
- Abstain means the pass completes with no `classification`, and `classification-confidence` plus the top two candidates go into `evidence`. The note shows under "Unsorted". Diarization still runs, because an absent value is not `monolog`.
- A transport failure or malformed answer fails the pass with `provider_*` reason codes via `jev._validate`. Retry is handled by the runner's existing attempts.

**Frontmatter written:** `classification`, `classification-detail`, `classification-confidence` (the top probability, three decimals) and `classification-source` (`jev-1.13.0`, `rule:telnyx` or `rule:floor`). Add `classification-detail`, `classification-confidence` and `classification-source` to the YAML `clobber` list so a reclassification can refresh them. The extra keys need no migration. They land in `vault_extra`. The Holocene API reads them from the `transcript_latest` view's `frontmatter` jsonb.

**Key:** a new virtual key, alias `heyma-wax-classification`, minted for this pass and stored in 1Password. Reference it by item UUID in `classification.yaml` as `WAX_JEV_API_KEY_OP`. Do not reuse the project-extraction key. The probe reused it only because no other existed. Cost is about $0.0001 per call (from the report), so a $0.50/day cap is ample.

### A.3 Pass version bump

- `classification.yaml`: `version: 2`. Update the description, remove the `WAX_TITLE_*` chat env, and add `WAX_JEV_API_KEY_OP`, `WAX_JEV_MODEL=jev-1.13.0` and `WAX_JEV_URL`. Drop `timeout_s` from 390 to 60, since the pass makes one 0.5 s call with a 20 s timeout and one retry.
- The script keeps the same output contract (`wax_ep_version: 1`, `frontmatter`, `evidence`). Frozen v1 plans for in-flight items therefore still run the on-disk script safely.
- Re-import both pipelines with `wax pipeline import ... --name transcript-standard` and `--name doc-standard`. Both are `exported_snapshot` v7 and freeze the old version.
- `wax ep list` will show old items as STALE. This is expected and matches the title-slug precedent.

### A.4 Downstream consumers, in rollout order

Order matters. A note with `phone_call` before the migration lands is rejected (`extract.py:476`) or fails the pass (`passes.py:864`).

| # | File and line | Change |
|---|---|---|
| 1 | new `src/wax/transcriptions/migrations/0004_classification_phone_call.sql` | `ALTER TABLE tx.transcript_version DROP CONSTRAINT transcript_version_classification_check; ADD CONSTRAINT ... CHECK (classification IN ('monolog','meeting','phone_call','other'))`. Add `SET LOCAL search_path = tx, public;`. Apply with `wax transcripts migrate`. |
| 2 | `src/wax/transcriptions/extract.py:36` | `CLASSIFICATIONS = ("monolog","meeting","phone_call","other")` |
| 3 | `src/wax/passes.py:864` | Replace the literal tuple with the shared constant. Import `CLASSIFICATIONS` from `transcriptions.extract`, or define it in `frontmatter.py` so both import one source. |
| 4 | `bin/wax:1761` | `choices=["monolog","meeting","phone_call","other"]` |
| 5 | `config/passes.d/bin/classification` and `classification.yaml` | v2 as in A.2 |
| 6 | `config/passes.d/diarization.yaml:19` | Keep `not_equals monolog`. A phone call leg is multi-party. Update the comment: `phone_call` and `other` still diarize. This is an operator call (blocking question 3). |
| 7 | docs | `docs/TRANSCRIPTIONS-STORE.md` lines 165, 251, 1145; `docs/ENRICHMENT-PASSES.md` (classification section); add a dated G.7 bullet recording the amendment, as G.2 requires |
| 8 | `components/n8n-nodes-heyma/nodes/WaxPassClassification/WaxPassClassification.node.ts` | Description string only. Rebuild `dist`. Update the sticky note in `examples/transcript-standard-release.json`. |
| 9 | `components/n8n-workflows/format-completion.js` | No change. It passes the string through. |
| 10 | tests | `tests/classification_empty_test.py`, `passes_chain_test.py`, `diarization_gate_test.py`, `transcriptions_*_test.py` get `phone_call` cases. Add a new `tests/classification_jev_test.py` with a mocked `jev.decide` covering telnyx, floor, accept, abstain, disagreement and preserved values. |
| 11 | `systemctl --user restart waxd` | `passes.py` is imported once. The pass script and YAML are re-read live. |
| 12 | pipelines | Re-import both |

### A.5 Search

The `tx.search` function needs no change. `wax transcripts search --classification phone_call` works after step 4.

### A.6 Backfill and reclassify procedure

A plain re-run does nothing for already-classified notes (the preserve branch). Add a flag to the script: `bin/classification --reclassify <md>`. It skips step 2 only when the existing value has no `classification-source` marker (an unmarked v1 value), or when the marker is `rule:*` or `jev-*` from an older pass version. A value with no marker that a human might have set cannot be distinguished from a v1 model value. v1 never wrote a marker, so every v1 value is unmarked. The operator rule is therefore: unmarked values are overwritable only with `--force-human`, and they are shown in the dry-run diff first.

**Procedure:**
1. Prerequisite: steps A.4 1 to 4 are live.
2. **Dry run.** For each target run `bin/classification --reclassify <md>` directly. It prints the result and writes nothing. Collect a diff table: old value, new value, probability. Inspect the 36 telnyx notes first, then every row where old and new differ.
3. **Apply to wax-origin items (466).** Run `wax ep run classification <item_id> --reclassify-existing` item by item, not `run-all`. `run-all` skips passes that already completed at the current version (`passes.py:1362-1368`), so only the version bump makes items eligible. Ask the operator to approve the diff before overwriting existing `monolog` and `meeting` values that Jev disagrees with. Keep any value Jev agrees with, and write only the marker.
4. **Legacy (75) and hermes-stt (335).** These have no `wax-item-id`, so `wax ep run` cannot reach them. Hermes rows are agent voice commands. Hide them from the tab by default via `origin <> 'hermes-stt'` (blocking question 2 confirms). Classify the 75 legacy notes with a one-off script that calls `bin/classification --reclassify` as a document pass. Defer this to a follow-up if needed.
5. **Project sync.** `wax transcripts sync` projects the changed frontmatter as new `tx.transcript_version` rows. History is append-only, so `wax transcripts history <item>` shows every change.
6. **Verification.**
   - `wax transcripts search "" --classification phone_call -n 50 --json` returns all 36 telnyx rows.
   - A count by classification matches the dry-run table.
   - `tx.check_invariants()` returns zero rows.

**Cost and time:** 466 items at about $0.0001 is about $0.05, and at 0.5 s per call it takes a few minutes. Re-measure on the first 20 items before the full run.

### A.7 Hand-labelled calibration set (blocks tuning only)

Before fixing the 0.6 threshold, label about 40 notes by hand (10 per class) and compare. The probe's monolog versus meeting boundary was soft (from the report: 2 of 5 v1 monologs agreed, and the labels are not ground truth). If agreement on monolog is under 80 percent, add a third signal: `speaker_count` after diarization, as a tie-break only. Do this in the dry-run step, not before shipping.

---

## B. Data

### B.1 What exists and what is missing

- `tx.transcript_current.project_ids` exists but is empty everywhere. The `project-extraction` pass is disabled, and 0 of 873 notes have `project-ids`.
- `tx.version_project` and `tx.version_ticket` exist and are empty.
- `tx.version_ticket` belongs to the Wax `ticketable` intake path. It is a different feature, so I leave it alone and do not reuse it.
- No table links a transcript to harvested tickets.

### B.2 Decision: project assignment

Two sources exist, and the tab needs one answer.

**Decision:** project assignment comes from the transcript's own `project_ids`. Enable the Wax `project-extraction` pass and backfill it. Harvest's per-item project is a second, finer signal that is shown on the ticket rows only. It is not used for grouping.

Reasons:
- `project_ids` is the existing, indexed, GIN-searchable column.
- Harvest classifies per item and may produce zero items for a transcript. A transcript that was never harvested would otherwise be ungroupable.
- Grouping must work before any harvest has run, since nothing has been harvested yet (from the report).

**Actions:**
1. Calibrate and enable `project-extraction` (blocking question 4). Its docstring says it needs a calibration re-run on the ingress first. Re-import the pipelines so it joins them.
2. Backfill by running it per item for the 466 wax-origin items (about $0.0003 each, from its docstring). It only writes when the note has no `project-ids`.
3. A transcript with several projects appears in each project's card.
4. A transcript with none goes in an "Unassigned" card.
5. A reindex is not needed. `wax transcripts sync` projects the new frontmatter, and `transcript_current.project_ids` updates on the next version.

### B.3 Decision: harvest ticket links

The Holocene tab needs transcript-to-tickets rows, and the only durable per-item records live in `actionitems.json` files. The decision is a new table in the `tx` schema, written by a Wax subcommand that ingests harvest artifacts.

**Writer:** `wax transcripts link-harvest [PATH...]`, a new Wax subcommand. It runs as `transcriptions_app`. It reads `actionitems.json` files and upserts rows. Wax owns the schema and the projector, pilot has no database dependency, and Holocene stays read-only.

**Not chosen:**
- `px harvest` writing directly to Postgres. Pilot has no Postgres client and runs on other hosts.
- A Bloodbank event path. Pilot emits no harvest events (`docs/harvest.md:1827`, from the report), and Plane task events carry no transcript key.
- Reading the `harvest-tickets` frontmatter alone. It is a bare URL list with no title, project or item id.

**Where artifacts come from:** set `harvest.outputDir` in `~/.config/pilot/config.json` to `~/.local/share/pilot/harvest`. `config.js:540` reads that key and `defaultOutput` honors it. Today artifacts land wherever the command ran, so there is no canonical store. `link-harvest` with no arguments scans that directory. The MinIO copy under `transcripts/<date>/<sha12>/actionitems.json` is a second source, and the command accepts an `s3://` path later.

**Migration `0005_harvest_links.sql`** (owner role):

```sql
SET LOCAL search_path = tx, public;

CREATE TABLE tx.harvest_run (
    harvest_id     text PRIMARY KEY,                 -- artifact.harvest_id
    transcript_id  bigint REFERENCES tx.transcript,  -- NULL = unmatched (kept, hidden from UI)
    match_method   text CHECK (match_method IN ('wax_item_id','body_sha256')),
    status         text NOT NULL CHECK (status IN ('complete','dry-run')),
    document_path  text,
    body_sha256    text,
    wax_item_id    text,
    s3_uri         text,
    artifact_path  text NOT NULL,
    artifact_sha256 text NOT NULL,                   -- idempotency: unchanged file = no-op
    item_count     integer NOT NULL,
    clear_count    integer NOT NULL,
    held_count     integer NOT NULL,
    created_at     timestamptz NOT NULL,             -- artifact.created_at
    updated_at     timestamptz NOT NULL,             -- artifact.updated_at
    ingested_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX harvest_run_transcript ON tx.harvest_run (transcript_id);
CREATE INDEX harvest_run_body       ON tx.harvest_run (body_sha256);

CREATE TABLE tx.harvest_item (
    harvest_id     text   NOT NULL REFERENCES tx.harvest_run ON DELETE CASCADE,
    item_id        text   NOT NULL,                  -- 'h-<12 hex>'
    ordinal        smallint NOT NULL,
    title          text   NOT NULL,
    category       text,
    status         text   CHECK (status IN ('firm','tentative')),
    gate_state     text   NOT NULL CHECK (gate_state IN ('clear','held')),
    project_id     text,                             -- classification.project
    group_kind     text,                             -- board|project|general
    quote          text,                             -- first evidence span, truncated to 400 chars
    PRIMARY KEY (harvest_id, item_id)
);

CREATE TABLE tx.harvest_ticket (
    harvest_id     text   NOT NULL,
    item_id        text   NOT NULL,
    provider       text   NOT NULL,                  -- plane|trello
    board_id       text   NOT NULL,
    publish_status text   NOT NULL CHECK (publish_status IN ('unpublished','sent','published','adopted','blocked','failed')),
    ticket_ref     text,                             -- 'PX-123'
    ticket_url     text,
    project_id     text,                             -- publish.project
    lane           text,
    published_at   timestamptz,
    is_test        boolean NOT NULL DEFAULT false,   -- publish.here
    PRIMARY KEY (harvest_id, item_id, provider, board_id),
    FOREIGN KEY (harvest_id, item_id) REFERENCES tx.harvest_item ON DELETE CASCADE
);
CREATE INDEX harvest_ticket_ref ON tx.harvest_ticket (ticket_ref);

CREATE VIEW tx.transcript_tickets AS
SELECT r.transcript_id, r.harvest_id, r.created_at AS harvested_at,
       i.item_id, i.ordinal, i.title, i.category, i.status AS item_status, i.gate_state,
       i.quote, t.provider, t.board_id, t.publish_status, t.ticket_ref, t.ticket_url,
       t.project_id, t.lane, t.published_at
  FROM tx.harvest_run r
  JOIN tx.harvest_item i USING (harvest_id)
  LEFT JOIN tx.harvest_ticket t USING (harvest_id, item_id)
 WHERE r.transcript_id IS NOT NULL
   AND r.status = 'complete'
   AND coalesce(t.is_test, false) = false;

GRANT SELECT ON tx.harvest_run, tx.harvest_item, tx.harvest_ticket, tx.transcript_tickets TO transcriptions_app, transcriptions_ro;
GRANT INSERT, UPDATE, DELETE ON tx.harvest_run, tx.harvest_item, tx.harvest_ticket TO transcriptions_app;
```

`tx.version_ticket` already uses the "ticket" word for the intake path. The new tables are named `harvest_*` to avoid that clash. The default privileges from 0001 already grant `SELECT` to the RO role on new tables, and the explicit GRANT repeats it for the view.

**Ticket state (title, status):** the table stores `ticket_ref`, `ticket_url`, `publish_status` and `lane`. The live board state (open or done) is not copied. The tab shows the stored lane as "as filed". A live-state refresh on expand is deferred, because Plane has no server-side marker search (from the report), and Holocene never talks to the boards. See blocking question 4's default.

**Trello INT (verbatim: false):** the board hides transcript text, but the local artifact keeps the item title and quote. The tab shows the artifact's item title. This is the operator's own data on the operator's own surface, so it is acceptable. Mark such tickets with a small "INT card (text withheld on the board)" note.

### B.4 Matching rule and idempotency

- **Match** `harvest_run.transcript_id` by `document.wax_item_id` first, against `tx.transcript.wax_item_id`. Fall back to `document.body_sha256` against `tx.transcript_current.body_sha256`. If a body sha matches more than one transcript, leave `transcript_id` NULL (an ambiguous match is not guessed).
- **Cross-check from the report:** one note's pilot `body_sha256` equaled Wax's. The report itself says to verify more notes (CRLF, empty frontmatter) before relying on it. The link-harvest acceptance test below does that on every note that has an artifact. Rows that match nothing are kept with a NULL `transcript_id` and listed by `wax transcripts link-harvest --report`.
- **Idempotency:** the primary key is `harvest_id`. For each artifact compute `artifact_sha256`. If the stored value equals it, do nothing. If it differs, in one transaction delete the run's items (the cascade removes tickets) and re-insert them. A re-harvest of the same document produces a new `harvest_id`. The tab shows the newest `complete` run per transcript by default, and older runs sit in a "previous harvests" fold.
- **Dry-run artifacts** are ingested with `status = 'dry-run'` and are excluded by the view. They are kept for diagnostics.
- **Test publishes** (`publish[...].here: true`) are stored with `is_test = true` and are excluded by the view. This covers `TEST-21` to `TEST-23`.
- **Reading safely:** `link-harvest` uses `readArtifact`'s rules. It requires `kind == "px.harvest.actionitems"` and `version == 2`, and it rejects any item whose `gate.state` is not `clear` or `held`.

### B.5 When it runs

1. **Directly after publish:** a one-line follow-up in the operator's workflow. Add `wax transcripts link-harvest` to the end of the `px harvest --publish` runbook. It is not baked into pilot, because pilot must not depend on Wax.
2. **Backstop:** `wax transcripts sync` calls the same ingest over the fixed `outputDir` (default scan on, `WAX_HARVEST_DIR` overrides). This catches hand-run harvests. It is cheap: it stats the files and hashes only the changed ones.
3. **Backfill of already-harvested transcripts:** the research found none (0 of 873 notes carry `harvest-tickets`). The only artifact on disk is the synthetic `/tmp/px-harvest-acceptance/synthetic/out/actionitems.json`, which links no real transcript. The backfill procedure is therefore only a scan: set `outputDir`, run `link-harvest --report`, and confirm zero unmatched rows. New data arrives as the operator runs real harvests.

### B.6 Frontmatter hazard

Each `harvest-tickets` writeback edits the note's frontmatter. That produces a new `reconcile` version with a changed `content_sha256` and an unchanged body sha. Nothing needs changing, and the body-sha match survives. It does add one version row per harvest, which is acceptable.

### B.7 `db.py` read-only role

Add a third role to `_ROLES` in `src/wax/transcriptions/db.py:27-30`:

```python
"ro": (config.ro_dsn, "WAX_TRANSCRIPTS_RO_PASSWORD", config.ro_password_op),
```

with matching `config.ro_dsn` and `config.ro_password_op` readers (env `WAX_TRANSCRIPTS_RO_DSN`, `WAX_TRANSCRIPTS_RO_PASSWORD_OP`). The `ro` role is used only by the new list subcommand. It is also what lets Holocene avoid a Postgres driver (section C.2).

---

## C. Holocene Transcripts tab

### C.1 Decision: how the API reads the data

The report offered two paths. **Decision: the Holocene API `execFile`s a new read-only Wax subcommand, `wax transcripts browse --json`.** It does not connect to Postgres.

Reasons:
- `CONTROL-SURFACE.md:104-106` (from the report) states the contract: the API "invokes the absolute Wax CLI and allowlist-projects responses". Wax's Postgres design names the RO role "for outside readers (Holocene, agents)", but the cleaner reading is that Wax mediates, since it already owns the schema, the projector and a read-only role.
- No driver is added to `apps/api`, and no credential reaches Holocene's environment, which has no Postgres secret today.
- `wax.ts` already gives the exact failure, cache and allowlist pattern. The new module copies it.
- Schema changes stay inside Wax.

The cost is process spawn per cache miss. A 5 s TTL and a single in-flight request cap keep it cheap. The measured corpus is about 870 rows, so a full listing is small enough to return in one response (about 600 KB at 700 bytes per row). If a spawn proves slow, the fallback is the direct `pg` path with the RO role. That is not built now.

The new subcommand is read-only. It connects as the new `ro` role, so even a bug in it cannot write. It does not call `sync`, `embed`, `migrate` or `link-harvest`.

### C.2 New Wax subcommand: `wax transcripts browse`

Add to `bin/wax` (parser beside `tse` at line 1756, dispatch in `transcripts_command` at line 1561) and implement in new `src/wax/transcriptions/browse.py`.

```
wax --json transcripts browse
    [--view monolog|meeting|phone_call|other|unsorted|all]    default all
    [--include-hermes]                                         default off (origin <> 'hermes-stt')
    [--limit N]                                                default 1000, max 2000
    [--since ISO] [--until ISO]
    [--project ID]                                             filter by project_ids
    [--id TRANSCRIPT_ID]                                       one row with tickets (the detail call)
```

It runs these queries as `ro` with `statement_timeout = 5000`. Rows come from `tx.transcript t JOIN tx.transcript_current c USING (transcript_id) JOIN tx.transcript_version v ON v.version_id = c.version_id`, with `t.searchable` true and `t.note_missing_at IS NULL` as defaults. The `frontmatter` jsonb comes from the version row and is used only to extract the allowed keys named below, never returned whole.

**Output (versioned, `schema: 1`):**

```json
{
  "schema": 1,
  "generated_at": "2026-10-29T14:03:11Z",
  "store": {"state": "ok", "latest_migration": "0005"},
  "counts": {"monolog": 48, "meeting": 62, "phone_call": 36, "other": 37, "unsorted": 308, "hidden_hermes": 335},
  "rows": [ { ...Row... } ],
  "projects": [ {"project_id": "pilot", "name": "Pilot", "count": 12} ]
}
```

**Row shape:**

```json
{
  "transcript_id": 412,
  "wax_item_id": "2d89c76e10c61b6d",
  "classification": "monolog",
  "classification_detail": null,
  "classification_confidence": 0.97,
  "title": "Plan for the transcripts tab",
  "summary": "…",
  "effective_at": "2026-10-28T21:14:00Z",
  "duration_s": 412.5,
  "word_count": 1180,
  "speaker_count": 1,
  "origin": "wax",
  "source_hint": "rec",
  "tags": ["holocene"],
  "project_ids": ["holocene"],
  "projects": ["Holocene"],
  "language": "en",
  "diarized": false,
  "audio_available": true,
  "harvest": {"state": "harvested", "run_count": 1, "ticket_count": 3, "item_count": 4, "last_harvested_at": "…"}
}
```

`harvest.state` is `none`, `harvested` (at least one ticket) or `no_items` (a complete run with zero items).

**Detail call (`--id N`):** returns one row plus:

```json
"tickets": [
  {"harvest_id": "…", "harvested_at": "…", "item_id": "h-72204a2c0900", "title": "…",
   "category": "…", "item_status": "firm", "gate_state": "clear",
   "quote": "…", "provider": "plane", "board_id": "0711da94-…", "publish_status": "published",
   "ticket_ref": "PX-123", "ticket_url": "https://plane.delo.sh/33god/browse/PX-123/",
   "project_id": "pilot", "lane": "Todo", "published_at": "…"}
],
"pass_history": [{"slug": "classification", "pass_version": 2, "state": "completed", "finished_at": "…"}]
```

**What is deliberately not returned:** the note body, `frontmatter_block`, full `frontmatter`, `note_path`, the S3 URL, and any `dsn`. `search.show` already returns the body, but this subcommand does not copy it. The Holocene tab shows summaries, not transcript text. Showing the body is a later decision (it is personal audio, including family recordings and kids; the Holocene API listens on 0.0.0.0).

**Limit on household privacy:** family recordings appear in the "Other" view with their LLM-written titles and summaries. Those are already visible in the vault. I flag this in blocking question 1 because the summary of a kids' recording rendering on a hosted page is the one visible product decision.

### C.3 New Holocene API module: `apps/api/src/transcripts.ts`

Mirror `wax.ts` line for line in structure.

- Types: `TranscriptRow`, `TranscriptTicket`, `TranscriptsPayload`.
- `readTranscripts(options)` runs `execFile(WAX_EXECUTABLE, ["--json","transcripts","browse", ...flags], {timeout: 15_000, env: {...process.env, WAX_ROOT}, maxBuffer: 8*1024*1024})`.
- Constants: `TRANSCRIPTS_TTL_MS` default 5000 and timeout from `TRANSCRIPTS_TIMEOUT_MS` (default 15000). Reuse `WAX_EXECUTABLE` and `WAX_ROOT`.
- **Allowlists** `ROW_FIELDS`, `TICKET_FIELDS`, `PASS_FIELDS`, `COUNT_KEYS`. A new CLI key is invisible until added. Strings are length-capped (title 300, summary 1200, quote 400). `ticket_url` must start with `https://`, or it is dropped. `classification` must be one of the four known ids, or it is set to null and the row counts as unsorted.
- **Cache:** module-level, keyed by the flag string, with a `resetTranscriptsCache()` test seam. Detail calls cache per id with the same TTL.
- **Failure contract:** a CLI failure with a prior cache returns the last good payload with `source.down = true`, `value: "stale"` and `detail: "last good read; the CLI now fails: …"`. A failure with no cache returns `{source:{down:true, detail}, rows: [], counts: {}}`. It is DOWN, never empty healthy. Raw stderr is trimmed to its first line and capped at 300 chars. The `store.state` from the CLI (for example `unreachable`) is surfaced as `source.detail`.
- **Source envelope:** `{ name: "wax transcripts", value, ageSeconds, maxSeconds, down, detail }`, same shape as `WaxSource`.
- **Routes** (`registerTranscriptRoutes(app)`, registered in `server.ts` directly after line 49):
  - `GET /api/modules/transcripts/list?view=&include_hermes=0|1&project=&since=&until=` returns `TranscriptsPayload`. An invalid `view` returns 400 `{error}`. All values are validated and passed as argv (no shell).
  - `GET /api/modules/transcripts/:id` returns one `TranscriptRow` plus `tickets` and `pass_history`. A non-numeric id returns 400. A missing id returns 404.
  - Both send `Cache-Control: no-store`.
  - Under the existing Next rewrite, `/api/modules/*` needs no web-side proxy code (`next.config.mjs`).
- **Search:** `GET /api/modules/transcripts/search?q=…&view=` calls `wax --json transcripts search --lexical -n 30 --classification …`. `--lexical` avoids an embedding call on every keystroke. It returns `results[]` with the existing fields (`transcript_id`, `display_title`, `snippet`, `score`, `classification`, `effective_at`). The tab maps these ids onto the already-loaded rows, so the same row component renders. The search box debounces at 300 ms and is read-only. The `--classification` choices must include `phone_call` first (A.4 step 4). This is cheap because the CLI route already exists, so I include it. If the page ships without search, nothing else breaks.

### C.4 Web route and files

`apps/web/app/transcripts/`:
- `page.tsx`, modeled on `wax/page.tsx`:
  ```tsx
  import "../hub/hub.css";
  import "./transcripts.css";
  import { TranscriptsApp } from "./transcripts-app";
  export const metadata = { title: "Holocene · Transcripts", description: "Recordings by kind and project, with the tickets harvested from them" };
  export default function Page() { return <TranscriptsApp />; }
  ```
- `types.ts`: a hand-mirrored copy of the API types, as Wax does.
- `transcripts-app.tsx`, `transcripts.css`.

**Nav link:** add `<a className="hub-tab" href="/transcripts">Transcripts</a>` after Wax in all four hard-coded navs: `hub/hub-app.tsx:197-204`, `wax/wax-app.tsx:250-257`, `voice/voice-app.tsx:60-67` and `settings/settings-app.tsx:263-270`. Extract a shared `HubTabs({current})` into `hub/ui.tsx` at the same time. It is a small refactor that removes four copies, and it is the clean way to add this tab. I decide to do it, and the plan lists it as its own step so it can be reverted alone.

**Data use:** `usePoll<TranscriptsPayload>("/api/modules/transcripts/list?...", 15_000)`. Rows are fetched once per poll for all kinds (about 870 rows, small), so switching the toggle is instant and client-side. The `view` and `group` selections live in the URL (`?view=monolog&group=project`), like the hub (`readUrl` and `writeUrl` pattern in `hub-app.tsx:19-32`). The expanded row id lives in `?open=<id>`. A reload restores state. The detail call fires on expand, with its own `usePoll`-free `fetch` using `cache: "no-store"`.

### C.5 UI design

**Shell:** the `.hub-root` wrapper with `data-theme`, `Brand`, the shared tabs, `SourceStrip` (one source, `wax transcripts`), and `ThemeToggle`. Under it comes `.hub-body` and `.hub-lede`, with a `.hub-statement` (aria-live) such as "48 monologues, 62 meetings, 36 phone calls, 37 other. 3 of 183 recordings have been harvested." When down, it says "The Wax transcript store is not answering — showing the last good read." and `hub-error` carries API errors, exactly as in `wax-app.tsx:266-274`.

**Controls row** (one line, wraps on phones):
1. **View toggle:** Radix `ToggleGroup` is already a dependency, but the hub uses plain buttons, so use a `role="tablist"` row of buttons styled like `.hub-tab` with counts: `Monologues 48`, `Meetings 62`, `Phone calls 36`, `Other 37`, and `Unsorted 308` shown only when non-zero. The current one has `aria-selected="true"`. Default is Monologues.
2. **Group:** a two-option toggle, `List` | `By project`, stored in `?group=`.
3. **Search box:** a `<input type="search">`, lexical, debounced.
4. **Show hermes voice commands** checkbox, off by default (when on, fetch with `include_hermes=1`).

**List mode:** one row per transcript, newest first by `effective_at`, within the selected view. Each row is a `<article class="tr-row">` with a header button (`aria-expanded`) that toggles the expansion panel.

Row fields common to all views, in the header line: day and time (`Oct 28 · 9:14 pm`), title, duration (`formatDuration`), project chips, harvest badge. A summary clamp (2 lines) follows. A harvest badge reads `3 tickets` (working color), `not harvested` (muted) or `harvested · no items`.

**View-specific fields:**

| View | Extra fields in the row |
|---|---|
| **Monologues (richest)** | Summary (3-line clamp, expandable), duration, word count, **words per minute** (derived), language, tags (chips), project chips, classification confidence if under 0.85 (a "low confidence" mark), diarized flag only if speaker count is over 1 (suspicious for a monolog, shown as "N speakers detected"), source hint, `transcribed` age. In the expanded panel: all of the above in a `dl`, the pass history, a copy button for the Wax item id (`CopyRef`) and the `wax transcripts show <id>` command to copy, and the harvest section. |
| **Meetings** | Title, summary (2 lines), duration, speaker count, project chips, harvest badge |
| **Phone calls** | Title, one-line summary, duration, source hint (`telnyx`, `rec`), harvest badge |
| **Other** | Title, one-line summary, duration, detail chip (`household`, `no content`) |
| **Unsorted** | Title, duration, origin, "classification abstained" or "not yet classified" reason from `classification_confidence` |

Monologues is the richer view because the user asked for it. I decided "more detailed" means: a longer summary, word count and speaking rate, tags, language, confidence and the full metadata `dl` in the expanded panel.

**Expanded panel (click the row):** shows the harvest section on every view. Layout: a heading "Tickets from px harvest" and then one of three states.
- **Harvested with tickets:** a list. Each ticket is a line with the board ref as a link (`PX-123`, `target="_blank" rel="noopener noreferrer"`), the item title, a project chip, the lane as filed, a `firm` or `tentative` mark, and a `held` mark if `gate_state` is `held` (a held item that was never filed shows as `held — not filed`). The evidence quote is shown in a collapsed `<details>`, because quotes can be long and personal. Items with `publish_status` `blocked` or `failed` show that word in the broken color.
- **No harvest run:** the explicit empty state: "Not harvested yet. Run `px harvest <note> --publish`, then `wax transcripts link-harvest`." with the exact note filename and a copy button. The command text is display only; Holocene runs nothing.
- **Harvested, zero items:** "Harvested — no action items found."

While the detail request is in flight the panel shows "Reading tickets…" and, if it fails, a `hub-error` line with the message while keeping the row summary visible.

**Group by project mode:** a grid of square cards, one per project, using the Wax card pattern.
- Grid: `repeat(auto-fill, minmax(300px, 1fr))` with `gap: 14px`, a `grid-auto-rows` unset. Each card has `aspect-ratio: 1 / 1`, `display: flex; flex-direction: column; min-height: 0`, the `2px solid var(--line-strong)` border and `4px 4px 0 var(--shadow)` shadow (the `.wax-card` look from `wax.css:37-46`).
- Card header: project name, the count, and a small harvest tally ("2 harvested"). The body is a scrollable list (`overflow-y: auto; min-height: 0`) of that project's recordings in reverse chronological order, grouped under day labels (`Oct 28`, `Oct 27`). Each line is a compact row: time, title (one line, ellipsis) and a harvest dot. Clicking a line expands it inline in the card (the same expansion panel, narrower), and the card scrolls. An expanded card does not change the grid, so neighbours stay put.
- A transcript with several projects appears in each of its cards. A transcript with none goes in an "Unassigned" card, always last.
- Cards sort by most recent recording first, then by name. The toggle selection still applies: group mode shows only the selected kind (for example, monologues by project). A "All kinds" option appears in group mode, and the kind shows as a small chip per line.
- If `project_ids` is empty for every row (the pass has not run), show one "Unassigned" card and a `hub-placeholder` note: "No recordings have a project yet. Project extraction is not enabled." That prevents a misleading look of broken grouping.

**States:**
- **Loading:** statement "Reading the transcript store…".
- **Empty view:** `hub-placeholder` with `hub-label` "Empty view" and the line "No <kind> recordings." shown only when the source is not down.
- **Degraded or down:** the `SourceStrip` shows the glyph, the statement says the last good read is shown, and a down source with no data shows no rows and no empty-placeholder (it is DOWN, not empty).
- **Stale tickets:** if the harvest ingest is older than the newest harvest artifact, there is no signal today, and I do not invent one. Out of scope.

**Phone width:** `@media (max-width: 600px)` as in `hub.css:292`. The toggle row wraps. The project grid becomes one column, and the square aspect ratio is relaxed to `aspect-ratio: auto; max-height: 70vh` so a card is not a page-tall square. Rows stack: title on its own line, meta below. The expanded ticket list is single-column. Nothing scrolls horizontally. The tab strip already wraps on phones.

**Accessibility:** the toggle is a tablist with arrow-key support. Row headers are `<button aria-expanded aria-controls>`. The statement is `aria-live="polite"`. Focus stays on the row after expansion. Colors use hub tokens only, so day and night themes both work.

### C.6 CSS (`transcripts.css`)

Every rule is prefixed `.hub-root` and `tr-` (to avoid colliding with `wax-`). Use only existing tokens: `--surface`, `--sunken`, `--line`, `--line-strong`, `--ink`, `--ink-2`, `--ink-3`, `--pop`, `--working`, `--cleared`, `--broken`, `--shadow`, `--font-mono`. Do not use `--ink-soft` or `--you`, which are not defined. Badges: harvested uses `--cleared`, not-harvested uses `--ink-3`, failed or blocked uses `--broken`, held uses `--stuck`. A `--tr-soft-fail` token is defined per theme the way `wax.css:6-11` defines `--wax-soft-fail`.

---

## D. Implementation plan, ordered

Every step lands on `main` and is pushed (standing rule). Repo roots: HeyMa = `~/HeyMa` (component `components/wax`), pilot = `~/code/33GOD/pilot`, holocene = `~/code/33GOD/holocene`.

### Phase 1: classification (HeyMa)

1. **Migration 0004.** New `src/wax/transcriptions/migrations/0004_classification_phone_call.sql`. Test in `tests/transcriptions_pg_test.py`: insert a `phone_call` version, and assert an unknown value is rejected. **Acceptance:** `wax transcripts migrate --dry-run` lists 0004, and after applying, `check_invariants()` returns zero rows.
2. **Allow-lists.** `extract.py:36`, `passes.py:864` (one shared constant), `bin/wax:1761`. Tests in `transcriptions_extract_test.py` and `passes_chain_test.py:118-164`. **Acceptance:** `wax transcripts search x --classification phone_call` no longer errors.
3. **Jev classification pass v2.** Rewrite `config/passes.d/bin/classification` per A.2 with `--reclassify`. Update `classification.yaml` (version 2, env, clobber). New `tests/classification_jev_test.py`. Update `classification_empty_test.py`. **Acceptance:** `bin/classification <telnyx note> --reclassify` prints `phone_call` with `classification-source: rule:telnyx` and writes nothing.
4. **Key.** Mint `heyma-wax-classification` in the jev-litellm ingress with a cap, store in DeLoSecrets, reference by item UUID in the YAML. **Acceptance:** `op read` of the reference resolves, and a single call returns a valid answer.
5. **Docs and n8n.** `TRANSCRIPTIONS-STORE.md`, `ENRICHMENT-PASSES.md`, the n8n node description, the example JSON and `dist` rebuild. **Acceptance:** `npm test` in `n8n-workflows` passes.
6. **Deploy.** `systemctl --user restart waxd`, then re-import both pipelines. **Acceptance:** `wax catalog --json` shows classification at version 2, with the pipelines referencing it.
7. **Calibration set and dry-run diff** (A.6 step 2, A.7). Produce the diff table. **Acceptance:** all 36 telnyx notes map to `phone_call`, and the operator approves the diff.
8. **Backfill** (A.6 step 3) for wax-origin items, then `wax transcripts sync`. **Acceptance:** `wax transcripts search "" --classification phone_call -n 50` returns at least 36 rows.

### Phase 2: projects (HeyMa)

9. **Enable `project-extraction`** after the calibration re-run (blocking question 4). Flip `enabled: true` in its YAML, re-import pipelines, and backfill per item. **Acceptance:** `tx.transcript_current` has non-empty `project_ids` for the majority of wax-origin rows, and the precision spot check on 20 notes is at least 90 percent.

### Phase 3: harvest links (pilot and HeyMa)

10. **Pilot output directory.** In `~/.config/pilot/config.json` set `harvest.outputDir` to `~/.local/share/pilot/harvest`. No pilot code change. **Acceptance:** `px harvest <note> --dry-run` writes `<outputDir>/<slug>/actionitems.json`. A dry run only, no publish.
11. **Migration 0005** (`0005_harvest_links.sql`) as in B.3. Add to `docs/TRANSCRIPTIONS-STORE.md` as a dated amendment. **Acceptance:** `wax transcripts migrate` applies it, and the RO role can `SELECT` the view.
12. **`link-harvest`.** New `src/wax/transcriptions/harvest.py` plus the `bin/wax` subcommand. Tests: a fixture copied from the synthetic artifact (kept in `tests/fixtures/`), with cases for matching by `wax_item_id`, matching by body sha, an ambiguous match, a re-ingest no-op, a changed artifact replace, a dry-run exclusion and a `here` exclusion. **Acceptance:** running it on the synthetic artifact inserts a run with `transcript_id NULL` (no real transcript) and the view returns zero rows, as expected.
13. **Sync backstop.** `wax transcripts sync` calls the ingest over `WAX_HARVEST_DIR` (default the pilot output directory). Add the env to `ENV_SPECS`. **Acceptance:** `sync --check` reports harvest parity.
14. **RO role plumbing.** `db.py` `ro` role, `config.ro_*`, the unit environment line `WAX_TRANSCRIPTS_RO_DSN` and `WAX_TRANSCRIPTS_RO_PASSWORD_OP=op://DeLoSecrets/jdt6hd7awwzxd376agaszc7z3u/password` in `deploy/systemd/user/waxd.service`. Reinstall the unit. `waxd` does not need the RO role, but the CLI resolves it from the installed unit (`config.py` reads the unit when the env is unset). **Acceptance:** `wax transcripts browse --id 1 --json` runs.

### Phase 4: browse command (HeyMa)

15. **`wax transcripts browse`.** `src/wax/transcriptions/browse.py`, the parser and dispatch in `bin/wax`, and a test in `transcriptions_cli_test.py` against the Docker Postgres fixture the existing `transcriptions_pg_test.py` uses. Cases: each view filter, hermes hidden by default, counts, the detail call with tickets, an unknown id (exit 2), an unreachable store (exit 2 with `error:`). **Acceptance:** `wax --json transcripts browse --view monolog | jq '.rows|length'` equals the monolog count, and the output has no `content`, `note_path` or `frontmatter` keys.

### Phase 5: Holocene

16. **Shared tabs.** Add `HubTabs` to `hub/ui.tsx`, and replace the four hard-coded navs. **Acceptance:** `pnpm --filter @holocene/web typecheck` passes and every tab still shows with the right `aria-current`.
17. **API module.** `apps/api/src/transcripts.ts` and `transcripts.test.ts`. Register in `server.ts` after line 49. Add the test file to the `test` script at `apps/api/package.json:13`. The tests follow `wax.test.ts`: a fake executable script in a temp directory that emits canned JSON. Cases: allowlist strips unknown keys (including a planted `content`, `note_path` and `frontmatter`), invalid classification becomes null, non-https ticket URL dropped, CLI failure with and without prior cache, TTL, invalid view returns 400, and a non-numeric id returns 400. **Acceptance:** `pnpm --filter @holocene/api test` and `typecheck` pass.
18. **Web tab.** `page.tsx`, `types.ts`, `transcripts-app.tsx`, `transcripts.css`. **Acceptance:** `pnpm --filter @holocene/web typecheck` passes.
19. **Deploy.** `mise run deploy` in holocene (runs `deploy:api` then `deploy:web`). Do not restart `hermes-gateway`. **Acceptance:** `curl -s 127.0.0.1:4000/api/modules/transcripts/list | jq '.source, .counts'` shows `live`, and `curl -fsSI https://holocene.delo.sh/transcripts` returns 200 (through the SSO wall, so use the authenticated tab for the page itself).
20. **Visual check** on `https://holocene.delo.sh/transcripts` with the `kapture` skill (the route is behind Google SSO, so use the authenticated tab). Check, in both themes and at phone width: each of the four views, list and project modes, expansion on a harvested and a non-harvested row, and the down state (stop `waxd`'s store access by pointing `WAX_ROOT` at an empty directory for one local API test run, not in production). **Acceptance:** screenshots outside Git, no console errors, and no horizontal scroll at 390 px.

### Phase 6: first real harvest (operator action, outside this build)

21. Run `px harvest <note> --publish` on one real monologue, then `wax transcripts link-harvest`. **Acceptance:** the row in the Transcripts tab expands to show the real tickets. This is the only way to prove the chain end to end, and it needs the operator's go-ahead because it files real tickets.

### Tests summary

- HeyMa: `tests/classification_jev_test.py`, the extended extract and passes tests, `transcriptions_pg_test.py` (0004, 0005, browse, link-harvest), and `transcriptions_cli_test.py`.
- Holocene: `transcripts.test.ts` in the explicit test list.
- Pilot: no code change. The harvest artifact version check is in `readArtifact`, so `link-harvest` mirrors that rule in its own test.

---

## E. Blocking questions

1. **Should family recordings (the "Other" view) show their generated titles and summaries on the hosted page?** The Holocene API listens on 0.0.0.0 and the web route sits behind Google SSO. The summaries of kids and home recordings would then be on a web page. **Recommended default:** show titles only, with no summary, for rows whose `classification_detail` is `household`, and show summaries for everything else. It is a one-line change in the row component.
2. **Hide the 335 `hermes-stt` rows (agent voice commands) and leave the 75 legacy notes in "Unsorted"?** **Recommended default:** hide hermes by default with the checkbox to reveal them, and classify the 75 legacy notes later with the one-off script.
3. **Keep diarizing `phone_call` notes?** The current rule diarizes everything except monologs, and the 2026-10-27 order covers only monologs. **Recommended default:** keep diarizing phone calls. Call legs are multi-party and diarization is cheap on 0 to 109 s audio.
4. **Is the `project-extraction` calibration re-run approved, and is a stored lane ("as filed") enough for ticket state?** Project grouping is empty until that pass is enabled, and its docs say a calibration re-run on the ingress is required first. Live board state (open or done) would need a Holocene-to-Plane read, which I did not design. **Recommended default:** approve the calibration re-run now (cost about $0.0003 per note) and ship the tab showing the lane as filed. Live state can be a follow-up that uses `px` read-only (`PILOT_READ_ONLY=1`).

---

## Key file references

- Classifier: `/home/delorenj/HeyMa/components/wax/config/passes.d/bin/classification` (line 14, 44-46, 54-66), `classification.yaml`, `diarization.yaml:19`.
- Allow-lists: `/home/delorenj/HeyMa/components/wax/src/wax/passes.py:864`, `src/wax/transcriptions/extract.py:36,361,475-478`, `bin/wax:1761`.
- Jev client: `/home/delorenj/HeyMa/components/wax/src/wax/jev.py:55-59,128`.
- DB: `/home/delorenj/HeyMa/components/wax/src/wax/transcriptions/migrations/0001_core.sql:165,342,426-437,474-484`, `0002_search.sql:11,72,202`, `db.py:27-30`.
- Holocene: `/home/delorenj/code/33GOD/holocene/apps/api/src/wax.ts` (pattern), `server.ts:49`, `apps/api/package.json:13`, `apps/web/app/wax/{page.tsx,wax-app.tsx,wax.css}`, `apps/web/app/hub/{hub-app.tsx,ui.tsx,hub.css}`.
- Harvest: `/home/delorenj/code/33GOD/pilot/src/harvest/{artifact.js,publish.js,frontmatter.js}`, `src/config.js:482,540`.
---

## F. Operator decisions (2026-10-29, orchestrator; supersede section E)

1. **Family recordings show titles and summaries.** This is a single-operator homelab behind the operator's
   own Google SSO; no privacy redaction for `household` rows.
2. **Hermes STT rows are hidden by default** behind the "Show hermes voice commands" checkbox. The 75 legacy
   notes are classified in this build (one-off document-pass run), not deferred.
3. **Phone calls keep diarizing.** Only `monolog` skips diarization.
4. **`project-extraction` is calibrated and enabled now**, and backfilled for wax-origin items (cost is
   negligible). Tickets show the lane "as filed"; a live board-state read is a follow-up.
5. **Reclassification is authorized.** The operator asked that Jev put every recording in the right
   category, so existing v1 model values are overwritten by the v2 pass after the dry-run diff is recorded
   in this doc (no per-row approval gate). Values carrying a human marker, if any are found, are kept.
6. **No real `px harvest --publish` is run by the build.** Filing tickets on boards is the operator's call;
   the tab ships with the "Not harvested yet" state and the link-harvest ingest proven on fixtures.
7. **Deviations are recorded here** as dated bullets, as in TRANSCRIPTIONS-STORE.md section G.
