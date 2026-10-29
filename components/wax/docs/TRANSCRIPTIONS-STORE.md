# Design: the `transcriptions` Postgres store for Wax

**Status:** design of record, ready to implement. Nothing was written, created or restarted during this work.
**Verified against:** HeyMa `5b20896`, live reads on 2026-10-28.
**Paths:** relative to `/home/delorenj/HeyMa/components/wax` unless they are absolute.

---

## 0. Facts I checked before deciding

| Fact | How it was checked |
|---|---|
| Host Postgres 17.10. pgvector 0.8.0, pg_trgm 1.6, btree_gin and citext are available. There is no `transcriptions` database yet. | `psql -h 127.0.0.1 -U delorenj` |
| pgvector supports `halfvec_ip_ops` and `halfvec_cosine_ops` for HNSW, and has `l2_norm(halfvec)`, `l2_normalize(halfvec)` and `binary_quantize(halfvec)`. `halfvec` storage is `e` (external). | `pg_proc` / `pg_opclass` in `hindsight` |
| The embedding model is `qwen3-embedding:8b` with digest `64b933495768fbd3b87c20583d379728a07471e0c66733a9df87cd1901b3c44b`, served by Ollama on `localhost:11434`. | `/api/tags` |
| System Python is 3.13.7 with psycopg 3.2.6 (C implementation) and pytest 7.4.4. It has no `psycopg_pool` and no `pgvector` module. mise Python 3.14.4 has no psycopg. | import probes |
| MariaDB is not installed (`which mariadb mysql` finds nothing). | shell |
| The daily dump already enumerates every database: `~/backups/postgres/dbs/<db>_YYYYMMDD.dump` includes `idealscenario`, `hindsight`, `pg_globals` and others. | `ls` |
| Outbox history exists that the backfill can import: 876 `evt.audio.task.completed` events plus 173 legacy `evt.v1.audio.task.completed`. Ledger: 460 `transcripts` rows. | read-only `sqlite3` |
| `passes.run` mutates the note in this order: `_apply_base_schema` (`passes.py:1147`), `_apply_result` (`:1171`, which stages, swaps and renames at `:925-944`), then `_write_pass_note` (`:1248-1251`). The early skip and failure writers are at `:1059-1067` and `:1119-1122`. Only the latest attempt survives in `passes` (`_record`, `:432-467`, upsert on `(item_id, ep_slug)`). | source |
| `transcribe_adapter.transcribe` merges provenance (`:308-348`), writes `transcripts` (`:350-368`), then sets state `transcribed` (`:369-370`). | source |
| `worker_enrichment_test.py:43-52` mocks `ledger.connect` with an ordered `side_effect`. `portability_test.py:101-111` mocks `transcribe_adapter.ledger.connect`. So hooks must not go into `worker.py`, and must survive a MagicMock ledger. | source |
| `chat_pass_routing_test.py:176-190` scans literals in `src/wax/*.py` and `bin/wax` for Anthropic model names. The default model here, `qwen3-embedding:8b`, contains none. | source |

---

## A. Engine: Postgres 17 + pgvector 0.8.0 + pg_trgm, on the existing host cluster

Decision: use a new database, `transcriptions`, on the host cluster (`localhost:5432`). It gets its own schema `tx` and three roles. I am not using MariaDB.

1. **It already runs here and is already looked after.** The cluster runs pgvector 0.8.0 in production (`hindsight`, which has partial HNSW indexes at about 23 ms for top-20 over 64k rows). It is in the nightly per-database dump. The system Python that `waxd` uses (`#!/usr/bin/python3`, `bin/wax:1`) already has psycopg 3.2.6. MariaDB would be a new server to run, back up and monitor, for no capability gain.
2. **System-versioned tables are the wrong abstraction.** MariaDB `WITH SYSTEM VERSIONING` versions rows by transaction time. Each version here has to say which pass made it, with which command_id, model and attempt. It also has to dedupe re-runs and hold history that was never observed live (backfill). That is an explicit, append-only `transcript_version` table with an enforced head pointer. Once you have it, temporal tables add nothing.
3. **Search needs features only Postgres has here:**
   - `halfvec` HNSW up to 4,000 dimensions, so a 2048-dimension Qwen vector can be indexed.
   - Weighted `tsvector` ranking with `ts_rank_cd`.
   - Trigram fuzzy matching on titles.
   - All three fused in one SQL function, in one round trip, under one transaction.
4. **Other options I rejected:**
   - Qdrant would be a second store to keep consistent. That is worth it at 10^7 vectors, not at about 7.4k.
   - SQLite with sqlite-vec has no multi-reader server for agents and Holocene, and you asked for Postgres.
   - VectorChord, pgvectorscale and ParadeDB BM25 are not installed (see the infra report).

Cluster tuning (`shared_buffers`, `random_page_cost`) is not needed at this size, so I am leaving it untouched. The hot set is about 70 MB and stays in the 62 GB OS page cache.

---

## B. Schema

### B.0 Modelling decisions

| Decision | Choice | Why |
|---|---|---|
| Scope | Every note in `WAX_VAULT` (868 today). Not Wax "documents". | You asked for transcriptions as an entity; the 334 Hermes STT notes and 78 legacy clips are transcripts too. Documents (`doc-<sha16>@binding`, `~/d/Inbox`) never get a `transcripts` row (HeyMa `AGENTS.md`, "Pipelines, documents"). The hooks gate on `passes.is_document` (`passes.py:573-576`) and `wax_item_id` has a CHECK of `^[0-9a-f]{16}$`. |
| Identity | `transcript_id bigint` primary key. `wax_item_id` is UNIQUE and nullable. `note_relpath` (relative to the vault) is UNIQUE. | 413 notes have no Wax identity. Paths relative to the vault avoid the ambiguity between `~/d` and `~/code/DeLoDocs`. |
| What a "version" is | One immutable row per transcription, per applied pass (completed or skipped), per backfill and per detected external edit. Failed runs go to `pass_run` only. | This follows "one version per enrichment pass applied" literally. Body text is stored once per distinct content, so a frontmatter-only version costs one small row and zero embeddings. |
| Re-runs | If the headhas the same `produced_by`, the same `pass_version` and the same `content_sha256`, no version is minted. A `pass_run` row is still recorded, with `outcome='no_change'`. | A retry that changed nothing is not a new state of the transcript. Re-delivering the same snapshot is a no-op through `UNIQUE (transcript_id, idempotency_key)`. |
| Idempotency keys | `transcription:{item}:a{attempt}:{note_sha[:16]}`, `pass:{command_id}`, `skip:{item}:{slug}:v{ver}:a{attempt}`, `depfail:{item}:{slug}:v{ver}:a{attempt}`, `backfill:{note_sha}`, `reconcile:{content_sha}` | `command_id` is uuid5 over `ep:<item>:<slug>:<attempt>` (`events.py:157-159`), so it is unique for each attempt. A when-skip issues no command (`passes.py:1114-1124`), so it gets a synthetic key. |
| Content hashing | Four hashes: <br>• `note_sha256` over the full bytes <br>• `body_sha256` = `sha256(body.encode())` <br>• `meta_sha256` over canonical JSON of the frontmatter minus `wax.passes` <br>• `content_sha256` = `sha256(body_sha:meta_sha)` | `body_sha256` uses the same definition as Wax's own `body_sha256` (`transcribe_adapter.py:365`, `passes.py:923`). Excluding `wax.passes` means pass timestamps alone never look like a content change. |
| Exact reconstruction | Store `frontmatter_block`, the exact text before the body, so that note = `frontmatter_block || body`. Verify this against `note_sha256` on every insert. | The vault is not in git and Wax keeps no prior snapshot (lifecycle report, section 2). This makes every version restorable byte-for-byte. |
| Head pointer | `transcript.current_version_id` is advanced by an AFTER INSERT trigger. `version_no` is assigned by a BEFORE INSERT trigger under `FOR UPDATE`. `transcript_current` has a deferred composite FK to `(transcript_id, current_version_id)`. | "Latest = most recent pass" then becomes a database invariant rather than a code convention. A projector bug fails at commit. |
| Append-only | `transcript_version` and `transcript_body` have triggers that block UPDATE and DELETE, and the app role has no UPDATE or DELETE grant on them. | Postgres enforces immutability itself, the same AD-3 style IdealScenario uses. |
| Backfill | Every note with no store identity gets exactly one `backfill` version. Pass history is imported into `pass_run` from the outbox `task.*` events and from ledger `passes`. Note contents are not reconstructed. | Earlier note states are unrecoverable except for fragments: `passes.result` keeps only the latest attempt, and `asr.json` exists for 147 items. Fabricated intermediate versions would be worse than an honest single backfill version. |
| Vectors | Separate `chunk_current` and `chunk_vector_current` tables that hold the head body only, swapped transactionally. Each has one unconditional HNSW index. No partial index. | A mutable `is_current` flag would churn HNSW on every pass and force every query to repeat the predicate. A body changes rarely (diarization, summarize), so the swap is rare too. |
| Text search | English `tsvector` generated column with GIN on chunks. A weighted document `tsvector` and a trigram index on titles live on `transcript_current`. | 621 of the 643 notes that record a language say `en`. The 21 `nn` values are misdetections of short English clips. |

### B.1 Bootstrap (superuser, once). Run by `deploy/provision-transcriptions --apply`; this is not a migration.

```sql
-- psql as postgres (sudo -n -u postgres psql -v ON_ERROR_STOP=1), SQL piped on stdin; passwords
-- come from `op read` inside the provisioning script and are never in argv or a file.
CREATE ROLE transcriptions_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE transcriptions_app   LOGIN PASSWORD :'app_pw';
CREATE ROLE transcriptions_ro    LOGIN PASSWORD :'ro_pw';
CREATE DATABASE transcriptions OWNER transcriptions_owner TEMPLATE template0 ENCODING 'UTF8';
REVOKE ALL ON DATABASE transcriptions FROM PUBLIC;
GRANT CONNECT ON DATABASE transcriptions TO transcriptions_app, transcriptions_ro;
ALTER ROLE transcriptions_owner IN DATABASE transcriptions SET search_path = tx, public;
ALTER ROLE transcriptions_app   IN DATABASE transcriptions SET search_path = tx, public;
ALTER ROLE transcriptions_ro    IN DATABASE transcriptions SET search_path = tx, public;
\connect transcriptions
CREATE EXTENSION IF NOT EXISTS vector;    -- untrusted: superuser only (vector.control has no trusted=true)
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

`pg_hba.conf` already has `host 127.0.0.1/32 scram-sha-256`, so no change is needed there. The DSN host is always `localhost`, never `postgres.delo.sh` or a LAN IP.

### B.2 `src/wax/transcriptions/migrations/0001_core.sql` (applied as `transcriptions_owner`)

```sql
CREATE SCHEMA IF NOT EXISTS tx;
SET LOCAL search_path = tx, public;

-- ---------------------------------------------------------------- embedding model registry
CREATE TABLE tx.embedding_model (
    model_id            smallint    PRIMARY KEY,
    name                text        NOT NULL,
    digest              text        NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    dims                integer     NOT NULL CHECK (dims = 2048),   -- halfvec(2048) columns below
    instruction_version smallint    NOT NULL,
    query_instruction   text        NOT NULL,
    request_options     jsonb       NOT NULL,
    is_active           boolean     NOT NULL DEFAULT false,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name, digest, dims, instruction_version)
);
CREATE UNIQUE INDEX embedding_model_one_active ON tx.embedding_model ((true)) WHERE is_active;
INSERT INTO tx.embedding_model
    (model_id, name, digest, dims, instruction_version, query_instruction, request_options, is_active)
VALUES (1, 'qwen3-embedding:8b',
        '64b933495768fbd3b87c20583d379728a07471e0c66733a9df87cd1901b3c44b', 2048, 1,
        'Given a search query, retrieve relevant passages from the user''s voice-memo and meeting transcripts',
        '{"options":{"num_ctx":2048},"truncate":true,"keep_alive":"30m","dimensions":2048}', true);

-- ---------------------------------------------------------------- content-addressed bodies
CREATE TABLE tx.transcript_body (
    body_sha256          text        PRIMARY KEY CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
    body                 text        NOT NULL,
    char_count           integer     NOT NULL,
    byte_count           integer     NOT NULL,
    word_count           integer     NOT NULL,
    token_estimate       integer     NOT NULL,          -- ceil(bytes/3.7); corpus: 9.07 MB / 2.40M cl100k tokens
    h1_source_name       text,                          -- '# Transcription: <file>'
    has_speaker_labels   boolean     NOT NULL,
    speaker_count        smallint    NOT NULL DEFAULT 0,
    speaker_turn_count   integer     NOT NULL DEFAULT 0,
    has_summary_section  boolean     NOT NULL DEFAULT false,
    legacy_source_path   text,                          -- '- **Source**: `…`' bullets (210 notes)
    legacy_date_local    timestamptz,                   -- '- **Date**:' parsed in WAX_TRANSCRIPTS_LOCAL_TZ
    legacy_duration_s    numeric(12,3),                 -- '172m 44s' -> 10364
    legacy_language      text,
    legacy_model         text,                          -- 'large-v3'
    legacy_backend       text,                          -- 'faster-whisper'
    legacy_diarization   text,
    created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tx.body_speaker (
    body_sha256    text          NOT NULL REFERENCES tx.transcript_body,
    speaker_label  text          NOT NULL,              -- 'Speaker 2' | 'Gershon'
    turn_count     integer       NOT NULL,
    word_count     integer       NOT NULL,
    first_start_s  numeric(12,3),
    last_start_s   numeric(12,3),
    PRIMARY KEY (body_sha256, speaker_label)
);

-- ---------------------------------------------------------------- identity: one row per transcript
CREATE TABLE tx.transcript (
    transcript_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    wax_item_id           text UNIQUE CHECK (wax_item_id ~ '^[0-9a-f]{16}$'),
    origin                text NOT NULL CHECK (origin IN ('wax','hermes-stt','legacy','test')),
    searchable            boolean GENERATED ALWAYS AS (origin <> 'test') STORED,
    audio_source_hint     text,          -- otter|telnyx|clip|rec|hermes|NULL (filename-derived)
    ledger_origin         text,          -- items.origin (inbox|dropoff|archive|probe)
    ledger_state          text,          -- items.state at last observation
    ledger_created_at     timestamptz,   -- transcripts.created_at
    source_sha256         text CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    audio_s3_bucket       text,
    audio_s3_key          text,
    audio_s3_uri          text GENERATED ALWAYS AS (
                              CASE WHEN audio_s3_bucket IS NOT NULL AND audio_s3_key IS NOT NULL
                                   THEN 's3://' || audio_s3_bucket || '/' || audio_s3_key END) STORED,
    audio_https_url       text,          -- unsigned path-style (bucket is private: presign on read, never store presigned)
    audio_bytes           bigint,
    audio_format          text,          -- lower(extension)
    backup_verified_at    timestamptz,
    backup_method         text,
    source_audio_original_path text,     -- 'source-audio' (path at transcription time; may be gone)
    source_audio_name     text,
    audio_local_path      text,          -- items.path (current archive location)
    recorded_at           timestamptz,   -- parsed from the audio NAME (490/868 parse)
    recorded_precision    text CHECK (recorded_precision IN ('datetime','date')),
    captured_at           timestamptz,   -- 'captured' = audio mtime, NOT recording time
    captured_precision    text CHECK (captured_precision IN ('datetime','date')),
    transcribed_at        timestamptz,
    effective_at          timestamptz GENERATED ALWAYS AS (coalesce(recorded_at, captured_at, transcribed_at)) STORED,
    audio_duration_s      numeric(12,3), -- container (ffprobe)
    asr_duration_s        numeric(12,3), -- whisper-reported
    duration_ratio        numeric(8,5),
    language              text,
    language_probability  real,
    engine_model          text,
    transcription_backend text,
    asr_word_count        integer,
    asr_segment_count     integer,
    asr_path              text,
    processing_seconds    real,
    diarization_requested_at_transcribe        boolean,
    diarization_degraded                       boolean,
    diarization_device_requested_at_transcribe text,
    diarization_device_at_transcribe           text,
    vault_root            text   NOT NULL,                 -- '/home/delorenj/d/Transcripts' (Wax's own spelling)
    note_relpath          text   NOT NULL UNIQUE,          -- canonical note, relative to vault_root
    note_path             text GENERATED ALWAYS AS (vault_root || '/' || note_relpath) STORED,
    note_size             bigint,
    note_mtime_ns         bigint,
    note_missing_at       timestamptz,                     -- never deleted; content stays searchable
    current_version_id    bigint,
    first_seen_at         timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT transcript_head_key UNIQUE (transcript_id, current_version_id)
);
CREATE INDEX transcript_effective_at     ON tx.transcript (effective_at);
CREATE INDEX transcript_origin_effective ON tx.transcript (origin, effective_at);
CREATE INDEX transcript_duration         ON tx.transcript (audio_duration_s);
CREATE INDEX transcript_source_sha       ON tx.transcript (source_sha256);
CREATE INDEX transcript_relpath_trgm     ON tx.transcript USING gin (note_relpath gin_trgm_ops);

-- ---------------------------------------------------------------- append-only versions
CREATE TABLE tx.transcript_version (
    version_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transcript_id          bigint  NOT NULL REFERENCES tx.transcript,
    version_no             integer NOT NULL,              -- trigger-assigned
    idempotency_key        text    NOT NULL,
    producer_kind          text    NOT NULL CHECK (producer_kind IN ('transcription','pass','backfill','reconcile')),
    produced_by            text    NOT NULL,              -- 'transcription' | <pass slug> | 'backfill' | 'reconcile'
    pass_version           integer,
    pass_attempt           integer,
    pass_state             text CHECK (pass_state IN ('completed','skipped')),
    pass_reason_code       text,                          -- skip reason only
    command_id             uuid,
    definition_id          text,
    plan_id                text,
    pipeline_name          text,
    pipeline_definition_id text,
    model                  text,
    served_by              text,
    route                  text,
    fell_through           jsonb,                         -- [{model, reason_code}]
    evidence               jsonb,                         -- passes.result.evidence (body_replace stripped)
    changed_fields         text[]  NOT NULL DEFAULT '{}',
    pass_duration_s        real,
    observed_at            timestamptz NOT NULL,          -- snapshot capture time
    created_at             timestamptz NOT NULL DEFAULT now(),
    note_relpath           text    NOT NULL,              -- path at this version (renames visible)
    note_sha256            text    NOT NULL,
    note_bytes             integer NOT NULL,
    frontmatter_block      text    NOT NULL,              -- note == frontmatter_block || body
    frontmatter            jsonb   NOT NULL,              -- full parsed frontmatter, JSON-safe
    meta_sha256            text    NOT NULL,
    body_sha256            text    NOT NULL REFERENCES tx.transcript_body,
    content_sha256         text    NOT NULL,
    -- extracted top-level frontmatter at this version
    schema_version         smallint,
    asset_kind             text,
    specialist             text,
    source                 text,
    title                  text,
    title_slug             text,
    summary                text,
    classification         text CHECK (classification IN ('monolog','meeting','other')),
    diarized               boolean,
    diarization_requested  boolean,
    diarization_device_requested text,
    diarization_device     text,
    stamped_duration_s     numeric(12,3),                 -- 'duration-seconds'
    stamped_word_count     integer,                       -- 'word-count' (ASR count, not body)
    fm_language            text,
    fm_engine_model        text,
    fm_transcription_backend text,
    fm_audio_duration_s    numeric(12,3),                 -- 'audio-duration-seconds'
    fm_transcribed_at      timestamptz,
    fm_captured_at         timestamptz,
    fm_source_audio        text,
    fm_source_sha256       text,
    fm_source_s3_key       text,
    fm_source_s3_uri       text,
    fm_wax_item_id         text,
    tags                   text[] NOT NULL DEFAULT '{}',  -- '' -> {}
    labels                 text[] NOT NULL DEFAULT '{}',
    project_ids            text[] NOT NULL DEFAULT '{}',
    projects               text[] NOT NULL DEFAULT '{}',
    obsidian_note_status   text[] NOT NULL DEFAULT '{}',
    pipeline_status        text[] NOT NULL DEFAULT '{}',  -- Obsidian plugin noise; kept, never a signal
    vault_id text, content_role text, metadata_quality text, pillar text, triage_release text,
    wiki_candidate text, wiki_action text, decision_id text, domain text, subdomain text,
    category text, subcategory text, route_path text, route_confidence text, route_reason text,
    hitl_reason text, description text,
    modified_at            timestamptz,
    last_processed_at      timestamptz,
    triage_pass            date,
    last_triage_pass       text,
    vault_extra            jsonb NOT NULL DEFAULT '{}',   -- every key not mapped above
    CONSTRAINT transcript_version_no_key   UNIQUE (transcript_id, version_no),
    CONSTRAINT transcript_version_idem_key UNIQUE (transcript_id, idempotency_key),
    CONSTRAINT transcript_version_pair_key UNIQUE (transcript_id, version_id)
);
CREATE INDEX transcript_version_command  ON tx.transcript_version (command_id) WHERE command_id IS NOT NULL;
CREATE INDEX transcript_version_produced ON tx.transcript_version (produced_by, created_at);
CREATE INDEX transcript_version_body     ON tx.transcript_version (body_sha256);

ALTER TABLE tx.transcript ADD CONSTRAINT transcript_current_version_fk
    FOREIGN KEY (transcript_id, current_version_id)
    REFERENCES tx.transcript_version (transcript_id, version_id) DEFERRABLE INITIALLY DEFERRED;

-- version_no + head pointer + immutability are database invariants
CREATE FUNCTION tx.version_assign() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM 1 FROM tx.transcript WHERE transcript_id = NEW.transcript_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'transcript % does not exist', NEW.transcript_id;
    END IF;
    SELECT coalesce(max(version_no), 0) + 1 INTO NEW.version_no
      FROM tx.transcript_version WHERE transcript_id = NEW.transcript_id;
    RETURN NEW;
END $$;
CREATE TRIGGER transcript_version_assign BEFORE INSERT ON tx.transcript_version
    FOR EACH ROW EXECUTE FUNCTION tx.version_assign();

CREATE FUNCTION tx.version_advance() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE tx.transcript SET current_version_id = NEW.version_id, updated_at = now()
     WHERE transcript_id = NEW.transcript_id;
    RETURN NULL;
END $$;
CREATE TRIGGER transcript_version_advance AFTER INSERT ON tx.transcript_version
    FOR EACH ROW EXECUTE FUNCTION tx.version_advance();

CREATE FUNCTION tx.forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER transcript_version_immutable BEFORE UPDATE OR DELETE ON tx.transcript_version
    FOR EACH ROW EXECUTE FUNCTION tx.forbid_mutation();
CREATE TRIGGER transcript_body_immutable BEFORE UPDATE OR DELETE ON tx.transcript_body
    FOR EACH ROW EXECUTE FUNCTION tx.forbid_mutation();

-- ---------------------------------------------------------------- list-valued children (per version)
CREATE TABLE tx.version_tag (
    version_id bigint   NOT NULL REFERENCES tx.transcript_version,
    source     text     NOT NULL CHECK (source IN ('tags','labels')),
    ordinal    smallint NOT NULL,
    tag        text     NOT NULL,
    PRIMARY KEY (version_id, source, ordinal)
);
CREATE INDEX version_tag_tag ON tx.version_tag (tag);

CREATE TABLE tx.version_project (            -- project-extraction (disabled today; same order as `projects`)
    version_id  bigint   NOT NULL REFERENCES tx.transcript_version,
    ordinal     smallint NOT NULL,
    project_id  text     NOT NULL,
    name        text,
    probability real,                        -- from pass evidence when present
    PRIMARY KEY (version_id, ordinal)
);
CREATE INDEX version_project_project ON tx.version_project (project_id);

CREATE TABLE tx.version_ticket (             -- ticketable (disabled today)
    version_id         bigint   NOT NULL REFERENCES tx.transcript_version,
    ordinal            smallint NOT NULL,
    project_id         text,
    description        text     NOT NULL,
    transcript_excerpt text,
    PRIMARY KEY (version_id, ordinal)
);

CREATE TABLE tx.version_pass_state (         -- the note's wax.passes.<slug> block, exploded
    version_id   bigint NOT NULL REFERENCES tx.transcript_version,
    slug         text   NOT NULL,
    state        text   NOT NULL,
    at           timestamptz,
    pass_version integer,
    attempt      integer,
    command_id   uuid,
    reason_code  text,                       -- NULL when state='completed' (pre-fix stale keys)
    detail       text,
    model        text,
    served_by    text,
    route        text,
    fell_through jsonb,
    PRIMARY KEY (version_id, slug)
);

-- ---------------------------------------------------------------- path history
CREATE TABLE tx.transcript_path (
    transcript_id    bigint NOT NULL REFERENCES tx.transcript,
    note_relpath     text   NOT NULL,
    role             text   NOT NULL CHECK (role IN ('canonical','alias')),  -- alias = stale duplicate copy
    first_seen_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at     timestamptz NOT NULL DEFAULT now(),
    first_version_id bigint REFERENCES tx.transcript_version,
    PRIMARY KEY (transcript_id, note_relpath)
);
CREATE INDEX transcript_path_relpath ON tx.transcript_path (note_relpath);

-- ---------------------------------------------------------------- pass executions (incl. failed)
CREATE TABLE tx.pass_run (
    pass_run_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transcript_id   bigint NOT NULL REFERENCES tx.transcript,
    idempotency_key text   NOT NULL UNIQUE,
    slug            text   NOT NULL,
    pass_version    integer,
    attempt         integer,
    command_id      uuid,
    state           text   NOT NULL CHECK (state IN ('completed','skipped','failed')),
    reason_code     text,
    detail          text,
    definition_id   text,
    plan_id         text,
    pipeline_name   text,
    model           text,
    served_by       text,
    route           text,
    fell_through    jsonb,
    evidence        jsonb,
    changed_fields  text[] NOT NULL DEFAULT '{}',
    duration_s      real,
    finished_at     timestamptz NOT NULL,
    version_id      bigint REFERENCES tx.transcript_version,
    outcome         text CHECK (outcome IN ('version','no_change','not_applied')),  -- NULL = imported, unknown
    source          text NOT NULL CHECK (source IN ('hook','outbox','ledger'))
);
CREATE INDEX pass_run_transcript  ON tx.pass_run (transcript_id, finished_at);
CREATE INDEX pass_run_slug_state  ON tx.pass_run (slug, state, finished_at);

-- ---------------------------------------------------------------- head materialization
CREATE TABLE tx.transcript_current (
    transcript_id    bigint  PRIMARY KEY REFERENCES tx.transcript,
    version_id       bigint  NOT NULL,
    version_no       integer NOT NULL,
    produced_by      text    NOT NULL,
    body_sha256      text    NOT NULL REFERENCES tx.transcript_body,
    content_sha256   text    NOT NULL,
    display_title    text    NOT NULL,             -- coalesce(title, note filename stem)
    title            text,
    title_slug       text,
    summary          text,
    classification   text,
    tags             text[]  NOT NULL DEFAULT '{}',   -- tags ∪ labels, deduped
    tags_text        text    NOT NULL DEFAULT '',     -- python-joined (array_to_string is not IMMUTABLE)
    project_ids      text[]  NOT NULL DEFAULT '{}',
    diarized         boolean,
    speaker_count    smallint NOT NULL DEFAULT 0,
    word_count       integer NOT NULL,              -- body words
    chunk_count      integer NOT NULL DEFAULT 0,
    doc_input        text,                          -- 'Title: …\nSummary: …\nTags: …' or NULL
    doc_input_sha256 text,
    doc_tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')),   'A') ||
        setweight(to_tsvector('english', coalesce(summary, '')), 'B') ||
        setweight(to_tsvector('english', tags_text),             'C') ||
        setweight(to_tsvector('english', display_title),         'D')) STORED,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT transcript_current_is_head FOREIGN KEY (transcript_id, version_id)
        REFERENCES tx.transcript (transcript_id, current_version_id) DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX transcript_current_classification ON tx.transcript_current (classification);
CREATE INDEX transcript_current_tags          ON tx.transcript_current USING gin (tags);
CREATE INDEX transcript_current_projects      ON tx.transcript_current USING gin (project_ids);
CREATE INDEX transcript_current_doc_tsv       ON tx.transcript_current USING gin (doc_tsv);
CREATE INDEX transcript_current_title_trgm    ON tx.transcript_current USING gin (display_title gin_trgm_ops);

-- ---------------------------------------------------------------- current chunks (head body only)
CREATE TABLE tx.chunk_current (
    chunk_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transcript_id   bigint  NOT NULL REFERENCES tx.transcript_current ON DELETE CASCADE,
    body_sha256     text    NOT NULL,
    chunker_version smallint NOT NULL,
    ordinal         integer NOT NULL,
    kind            text    NOT NULL CHECK (kind IN ('transcript','summary')),
    char_start      integer NOT NULL,              -- offsets into transcript_body.body
    char_end        integer NOT NULL,
    text            text    NOT NULL,              -- verbatim markdown slice (display)
    search_text     text    NOT NULL,              -- normalized; EXACT embedding input
    search_sha256   text    NOT NULL,              -- sha256(search_text) = embedding_cache key
    token_estimate  integer NOT NULL,
    start_s         numeric(12,3),                 -- first turn timestamp in chunk
    end_s           numeric(12,3),                 -- next chunk's first turn timestamp
    speakers        text[]  NOT NULL DEFAULT '{}',
    turn_count      integer NOT NULL DEFAULT 0,
    tsv             tsvector GENERATED ALWAYS AS (to_tsvector('english', search_text)) STORED,
    UNIQUE (transcript_id, ordinal)
);
CREATE INDEX chunk_current_tsv        ON tx.chunk_current USING gin (tsv);
CREATE INDEX chunk_current_search_sha ON tx.chunk_current (search_sha256);

CREATE TABLE tx.chunk_vector_current (
    chunk_id      bigint   PRIMARY KEY REFERENCES tx.chunk_current ON DELETE CASCADE,
    transcript_id bigint   NOT NULL,
    model_id      smallint NOT NULL REFERENCES tx.embedding_model,
    embedding     halfvec(2048) NOT NULL,
    embedded_at   timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE tx.chunk_vector_current ALTER COLUMN embedding SET STORAGE PLAIN;  -- no TOAST hop on exact scans
CREATE INDEX chunk_vector_current_transcript ON tx.chunk_vector_current (transcript_id);
CREATE INDEX chunk_vector_current_hnsw ON tx.chunk_vector_current
    USING hnsw (embedding halfvec_ip_ops) WITH (m = 16, ef_construction = 128);

CREATE TABLE tx.doc_vector (
    transcript_id bigint   PRIMARY KEY REFERENCES tx.transcript_current ON DELETE CASCADE,
    input_sha256  text     NOT NULL,
    model_id      smallint NOT NULL REFERENCES tx.embedding_model,
    embedding     halfvec(2048) NOT NULL,
    embedded_at   timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE tx.doc_vector ALTER COLUMN embedding SET STORAGE PLAIN;
CREATE INDEX doc_vector_hnsw ON tx.doc_vector
    USING hnsw (embedding halfvec_ip_ops) WITH (m = 16, ef_construction = 64);

-- ---------------------------------------------------------------- embedding cache (no ANN index)
CREATE TABLE tx.embedding_cache (
    model_id     smallint NOT NULL REFERENCES tx.embedding_model,   -- implies name+digest+dims+instruction
    input_sha256 text     NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),  -- sha256 of the EXACT model input
    input_kind   text     NOT NULL CHECK (input_kind IN ('chunk','doc','query')),
    input_chars  integer  NOT NULL,
    embedding    halfvec(2048) NOT NULL CHECK (abs(l2_norm(embedding) - 1) < 0.005),
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (model_id, input_sha256)
);

-- ---------------------------------------------------------------- convenience views
CREATE VIEW tx.transcript_latest AS
SELECT t.*,
       v.version_no, v.producer_kind, v.produced_by, v.pass_version, v.command_id,
       v.model, v.served_by, v.route, v.observed_at AS version_observed_at,
       v.title, v.title_slug, v.summary, v.classification, v.diarized,
       v.tags, v.labels, v.project_ids, v.projects, v.frontmatter,
       v.note_sha256, v.content_sha256, v.body_sha256,
       b.body AS content, b.word_count AS body_word_count, b.speaker_count
  FROM tx.transcript t
  JOIN tx.transcript_current c USING (transcript_id)
  JOIN tx.transcript_version v ON v.version_id = c.version_id
  JOIN tx.transcript_body    b ON b.body_sha256 = v.body_sha256;

CREATE VIEW tx.transcript_history AS
SELECT v.transcript_id, v.version_id, v.version_no, v.producer_kind, v.produced_by, v.pass_version,
       v.pass_attempt, v.pass_state, v.command_id, v.model, v.served_by, v.changed_fields,
       v.note_relpath, v.body_sha256, v.content_sha256, v.observed_at,
       (v.body_sha256 IS DISTINCT FROM lag(v.body_sha256) OVER w) AS body_changed
  FROM tx.transcript_version v
WINDOW w AS (PARTITION BY v.transcript_id ORDER BY v.version_no);

-- ---------------------------------------------------------------- invariant checker (doctor)
CREATE FUNCTION tx.check_invariants()
RETURNS TABLE (check_name text, transcript_id bigint, detail text)
LANGUAGE sql STABLE SET search_path = tx, public AS $$
    SELECT 'head_is_max_version', c.transcript_id, c.version_no || ' < ' || m.max_no
      FROM tx.transcript_current c
      JOIN (SELECT transcript_id, max(version_no) AS max_no
              FROM tx.transcript_version GROUP BY transcript_id) m USING (transcript_id)
     WHERE c.version_no <> m.max_no
    UNION ALL
    SELECT 'chunks_match_head_body', ch.transcript_id, 'chunk body ' || left(ch.body_sha256, 12)
      FROM tx.chunk_current ch
      JOIN tx.transcript_current c USING (transcript_id)
     WHERE ch.body_sha256 <> c.body_sha256
    UNION ALL
    SELECT 'vectors_active_model', v.transcript_id, 'model ' || v.model_id
      FROM tx.chunk_vector_current v
     WHERE v.model_id <> (SELECT model_id FROM tx.embedding_model WHERE is_active)
    UNION ALL
    SELECT 'version_without_current', t.transcript_id, 'current_version_id set, no transcript_current row'
      FROM tx.transcript t
     WHERE t.current_version_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM tx.transcript_current c WHERE c.transcript_id = t.transcript_id)
$$;

-- ---------------------------------------------------------------- grants (least privilege)
REVOKE ALL ON SCHEMA tx FROM PUBLIC;
GRANT USAGE ON SCHEMA tx TO transcriptions_app, transcriptions_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA tx TO transcriptions_app, transcriptions_ro;
GRANT INSERT, UPDATE ON tx.transcript, tx.transcript_current, tx.transcript_path TO transcriptions_app;
GRANT INSERT ON tx.transcript_version, tx.transcript_body, tx.body_speaker, tx.version_tag,
                tx.version_project, tx.version_ticket, tx.version_pass_state, tx.pass_run,
                tx.embedding_cache TO transcriptions_app;
GRANT INSERT, DELETE ON tx.chunk_current, tx.chunk_vector_current TO transcriptions_app;
GRANT INSERT, UPDATE, DELETE ON tx.doc_vector TO transcriptions_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA tx TO transcriptions_app;
GRANT EXECUTE ON FUNCTION tx.check_invariants() TO transcriptions_app, transcriptions_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA tx GRANT SELECT ON TABLES TO transcriptions_ro;
```

The app role has `UPDATE` on `tx.transcript` because `FOR UPDATE` in `version_assign` requires it, and so does the advance trigger. The app role cannot alter history. Even the owner hits the immutability trigger.

### B.3 Coalescing rules (implemented in `extract.py`; the first non-null source wins)

General rules:
- Naive timestamps are read as UTC. The corpus confirmed this: all 235 naive `transcribed-at` values matched `wax.transcribed_at` within 120 s.
- A bare date becomes midnight UTC with `*_precision='date'`.
- `''`, `[]` and `{}` become NULL.
- Filename wall-clock times and legacy `**Date**` bullets use `WAX_TRANSCRIPTS_LOCAL_TZ`, default `America/New_York`.

| Column | Source order |
|---|---|
| `source_sha256` | `source-sha256` → ledger `items.sha256` → `asr.json.source_sha256` |
| `audio_s3_bucket` / `audio_s3_key` | ledger `backups` (newest `verified_at`) → parse `source-s3-uri` → `source-s3-key` (strip a leading `recordings/`, bucket `recordings`) |
| `audio_https_url` | `finalize.audio_url(bucket, key)` (`finalize.py:15-23`), cached per process. `WAX_S3_ENDPOINT` is set in the unit so the projector never shells out to `mc`. |
| `audio_bytes` | `backups.bytes` → `items.bytes` |
| `backup_verified_at` / `backup_method` | `backups` |
| `source_audio_original_path` | `source-audio` → legacy `**Source**` |
| `source_audio_name` | `wax.source_audio` → basename(`source-audio`) → `items.orig_name` → basename(legacy Source) → body H1 name |
| `audio_format` | extension of `source_audio_name`, lowercased |
| `audio_local_path` / `ledger_origin` / `ledger_state` | `items.path` / `items.origin` / `items.state` |
| `recorded_at` | regexes over `source_audio_name`, in this order: <br>• `YYYY_MM_DD_HH_MM_SS` <br>• `YYYYMMDD-HHMMSS` (also covers `clip_N__…` and `…-rec`) <br>• `recording_YYYYMMDD_HHMMSS` <br>• `YYYY-MM-DD HH.MM.SS` <br>• `telnyx-YYYY-MM-DD` (precision `date`) |
| `captured_at` | `captured` |
| `transcribed_at` | `transcribed-at` → `wax.transcribed_at` → ledger `transcripts.created_at` → legacy `**Date**` |
| `audio_duration_s` | `wax.audio_duration_s` → ledger `transcripts.audio_duration` → `items.duration_s` → `duration-seconds` |
| `asr_duration_s` | `audio-duration-seconds` → ledger `asr_duration` → `asr.json.result.duration` → legacy `**Duration**` (`Xm Ys`) |
| `duration_ratio` | `wax.duration_ratio` → ledger |
| `language` | `language` → `asr.json.result.language` → legacy bullet → `.meta.json` |
| `language_probability` | `asr.json` only |
| `engine_model` | `engine-model` → ledger `engine_model` → legacy `**Model**` (before ` (`) → `.meta.json` |
| `transcription_backend` | `transcription-backend` → `asr.json.result.backend` → legacy `**Model**` (in parentheses) → `.meta.json` |
| `asr_word_count` | ledger `word_count` → `word-count` → `.meta.json` |
| `asr_segment_count` | `len(asr.json.result.segments)` → `.meta.json segment_count` (22 sidecars) |
| `asr_path` / `processing_seconds` / `ledger_created_at` | ledger `transcripts` |
| `diarization_*_at_transcribe` / `diarization_degraded` | the `wax.*` block (a snapshot at transcription time) |
| Version `diarized` / `diarization_*` | top-level keys only, because the diarization pass rewrites them (`diarization.yaml:21`) |
| Version `tags` | list → as is; string `''` → `{}`; any other string → `{s}`. `labels` goes into its own column, and both are written to `version_tag`. `transcript_current.tags` = tags ∪ labels. |
| `version_pass_state.reason_code` / `detail` | NULL when `state='completed'` (stale keys written before `passes.py:966-972`) |
| `fell_through` | `"m1=r1; m2=r2"` → `[{"model":"m1","reason_code":"r1"},…]` (split on `; `, then `rsplit('=',1)`). For hook versions, prefer `evidence.fallbacks`. |
| `origin` | 1. `test` if the body has fewer than 3 words (H1 and legacy header excluded) **and** there is no duration evidence (no `audio-duration-seconds`, no `wax.audio_duration_s`, no ledger row). This catches `a-first.ogg.md`, `b-second.ogg.md` and the 17-byte note. <br>2. `wax` if there is a `wax-item-id` or the ledger `md_path` maps to the note. <br>3. `hermes-stt` if `source-audio` starts with `/tmp/hermes-local-stt-` or contains `/.claude/tmp/hermes_voice` or `/.hermes/`. <br>4. Otherwise `legacy`. |

Every other frontmatter key goes into `vault_extra` unchanged. The full parsed frontmatter is also kept in `frontmatter`. Nothing is dropped.

---

## C. Semantic-search indexing

### C.1 Chunking (`chunking.py`, pure, `CHUNKER_VERSION = 1`)

- **Token estimate:** `max(1, ceil(len(s.encode()) / 3.7))`. This is deterministic and needs no tokenizer; the corpus ratio is 3.78 bytes per cl100k token. Overshoot is harmless because the model runs at `num_ctx=2048`, four times the cap.
- **Pre-strip:** remove the first `# Transcription: <file>` H1, and the legacy `- **Key**: value` bullet block up to the following `---`. Both are recorded on `transcript_body`, not chunked.
- **A `## Summary` section** becomes its own `kind='summary'` chunk. It is never merged with turns.
- **Units:**
  - **Speaker turns:**
    - Pattern: `^\*\*(?P<label>Speaker \d+|[A-Z][a-z]+(?: [A-Z][a-z]+){0,2})(?: \((?P<ts>\d{1,3}:\d{2}(?::\d{2})?)\))?:\*\*\s*`
    - Minutes may exceed 59, and `H:MM:SS` is allowed.
    - A stoplist rejects `Meeting Date`, `Date`, `Duration`, `Source`, `Language`, `Model`, `Diarization`, `Title` and `Summary`.
  - **Everything else:** blank-line paragraphs.
- **Packing:**
  - Units are packed greedily up to `TARGET=400` tokens. No chunk exceeds `HARD_CAP=512`.
  - A unit over 512 tokens is split at sentence boundaries `(?<=[.!?])\s+`. Whisper output is often unpunctuated; in that case it is split into word windows of 400 tokens or fewer. Each piece carries the turn's label.
- **Overlap:**
  - The next chunk starts about 60 tokens back, on a word boundary, never before the previous chunk's start + 1.
  - The overlap is real text, so `char_start(k+1) < char_end(k)`.
- **Short notes:** a note of 512 tokens or fewer becomes exactly one chunk. That is 517 of the 868 notes.
- **`search_text`:** each turn segment is rendered as `Label: words…`. Bold markup and timestamps are removed. This is the FTS source and the exact embedding input; title and date are not prefixed. That is deliberate: frontmatter-only passes (title-slug, classification, stamp) then never change a chunk hash, so they never trigger re-embedding. Title and summary context goes into the doc vector instead.
- **`start_s` / `end_s`:** taken from turn timestamps. They are NULL for undiarized bodies.
- **Expected volume:** about 2.40M tokens / 340 effective tokens per chunk + short notes ≈ **7.4k chunks** today.

### C.2 Embeddings

- **Model:** `qwen3-embedding:8b`, pinned by its full digest in `tx.embedding_model`. It runs on local Ollama through `POST {WAX_EMBED_URL}/api/embed`.
- **Request body:** `{"model", "input": [≤8], "dimensions": 2048, "truncate": true, "keep_alive": "30m", "options": {"num_ctx": 2048}}`.
- **Why local and not through the gateway:**
  - At `num_ctx=2048` the model takes 5.7 GB of VRAM with all 37 layers on the GPU, and serves a warm query in about 85 ms. The default 32k context spills layers to the CPU and takes 0.9–2.7 s per query.
  - The AutomaticAI gateway rule in HeyMa `AGENTS.md` governs chat-pass model chains. The `heyma-wax-inference` scope (`deploy/provision-inference`) has no embeddings route.
  - A self-hosted encoder costs $0, keeps personal transcripts on the box, and contains no banned model string.
- **Dimensions:** `halfvec(2048)`. This is Matryoshka truncation, which the infra report measured as exact against the renormalised first N values of the full vector. It is under halfvec's 4,000-dimension HNSW limit, at about 4.1 KB per vector (about 30 MB at 7.4k, about 400 MB at 100k).
- **Distance:** vectors are L2-normalised, so `<#>` (negative inner product) with `halfvec_ip_ops` ranks the same as cosine and is cheaper. The client validates `abs(norm-1) < 1e-3` and renormalises otherwise. The table CHECK allows 0.005 for halfvec rounding.
- **Instructions:** queries are prefixed `Instruct: {query_instruction}\nQuery: {q}`. Documents get no prefix.
- **Cache key:** `(model_id, sha256(exact input string))`. `model_id` pins name, digest, dims and instruction version, and queries cannot collide with chunks because their input includes the prefix.
- **What each pass costs:**
  - Frontmatter-only versions: zero chunk embeddings.
  - Title-slug: at most one doc embedding.
  - A body change: only chunks whose `search_sha256` is not already cached.
- **Model drift:** each embed sweep checks `/api/tags` and refuses to embed if the served digest is not the registry digest; doctor reports FAIL. A model change is a migration that adds a model row, re-embeds into a shadow table and swaps. It is not supported online in v1.

### C.3 Where the indexes live, and why

- **HNSW** sits on `chunk_vector_current` and `doc_vector`, both of which hold head content only.
  - Settings: `m=16`, `ef_construction=128` for chunks and 64 for docs, `hnsw.ef_search=200` (≥ the 200 candidates fetched), `hnsw.iterative_scan=relaxed_order` for filtered scans.
  - The result is re-sorted in a materialized CTE, as pgvector's docs recommend for relaxed order.
- **History** (`transcript_version`, `embedding_cache`) has no ANN index, so search never touches stale versions.
- **GIN `tsv`** on chunks. **GIN `doc_tsv`** (weighted title A, summary B, tags C, filename D) and **trigram GIN** on `display_title` in `transcript_current`. Trigram GIN on `transcript.note_relpath` resolves `show <fuzzy name>`.
- **Filters** resolve on the small head tables first:
  - btree on `effective_at`, `(origin, effective_at)` and `audio_duration_s`
  - btree on `classification`
  - GIN on `tags` and `project_ids`

  The result is a list of eligible `transcript_id`s. At about 900 transcripts this is a sub-millisecond scan.
- **Exact versus HNSW:** if the eligible set has `p_exact_below` (4,000) embedded chunks or fewer, the function runs an exact scan over `chunk_vector_current_transcript`, fenced with `OFFSET 0` so the planner cannot pick HNSW. Otherwise it runs HNSW with an `= ANY(ids)` post-filter and iterative scan. Exact results on small filtered sets beat approximate ones, and 4,000 × 2048 dimensions is about 10 ms.
- **`STORAGE PLAIN`** on the vector columns keeps the 4.1 KB value inline, so the exact path does not take a TOAST hop.

### C.4 `src/wax/transcriptions/migrations/0002_search.sql`

```sql
SET LOCAL search_path = tx, public;

CREATE FUNCTION tx.search(
    p_query           text,
    p_query_vec       halfvec     DEFAULT NULL,      -- NULL => lexical-only
    p_limit           integer     DEFAULT 10,
    p_since           timestamptz DEFAULT NULL,
    p_until           timestamptz DEFAULT NULL,
    p_classifications text[]      DEFAULT NULL,
    p_tags_any        text[]      DEFAULT NULL,
    p_project_ids_any text[]      DEFAULT NULL,
    p_origins         text[]      DEFAULT NULL,
    p_min_duration_s  numeric     DEFAULT NULL,
    p_max_duration_s  numeric     DEFAULT NULL,
    p_min_words       integer     DEFAULT NULL,
    p_k_chunks        integer     DEFAULT 200,
    p_k_docs          integer     DEFAULT 50,
    p_rrf_k           integer     DEFAULT 60,
    p_weights         float8[]    DEFAULT '{1.0,1.0,0.6,0.6}',  -- chunk_vec, chunk_fts, doc_vec, doc_lex
    p_exact_below     integer     DEFAULT 4000
) RETURNS TABLE (
    transcript_id      bigint,
    wax_item_id        text,
    version_id         bigint,
    version_no         integer,
    produced_by        text,
    display_title      text,
    summary            text,
    classification     text,
    tags               text[],
    origin             text,
    effective_at       timestamptz,
    audio_duration_s   numeric,
    note_path          text,
    audio_s3_uri       text,
    audio_https_url    text,
    score              float8,
    rank_chunk_vec     integer,
    rank_chunk_fts     integer,
    rank_doc_vec       integer,
    rank_doc_lex       integer,
    best_chunk_id      bigint,
    best_chunk_ordinal integer,
    best_chunk_start_s numeric,
    snippet            text
)
LANGUAGE plpgsql STABLE
SET search_path = tx, public, pg_catalog
SET plan_cache_mode = force_custom_plan          -- variables plan as constants: dead legs fold away
SET hnsw.ef_search = 200
SET hnsw.iterative_scan = relaxed_order
AS $$
DECLARE
    v_ids      bigint[];
    v_chunks   bigint := 0;
    v_exact    boolean := false;
    v_tsq      tsquery;
    v_filtered boolean := (p_since IS NOT NULL OR p_until IS NOT NULL OR p_classifications IS NOT NULL
                           OR p_tags_any IS NOT NULL OR p_project_ids_any IS NOT NULL OR p_origins IS NOT NULL
                           OR p_min_duration_s IS NOT NULL OR p_max_duration_s IS NOT NULL
                           OR p_min_words IS NOT NULL);
BEGIN
    IF v_filtered THEN
        SELECT array_agg(c.transcript_id) INTO v_ids
          FROM tx.transcript_current c
          JOIN tx.transcript t USING (transcript_id)
         WHERE t.searchable
           AND (p_since           IS NULL OR t.effective_at >= p_since)
           AND (p_until           IS NULL OR t.effective_at <  p_until)
           AND (p_classifications IS NULL OR c.classification = ANY (p_classifications))
           AND (p_tags_any        IS NULL OR c.tags && p_tags_any)
           AND (p_project_ids_any IS NULL OR c.project_ids && p_project_ids_any)
           AND (p_origins         IS NULL OR t.origin = ANY (p_origins))
           AND (p_min_duration_s  IS NULL OR coalesce(t.audio_duration_s, t.asr_duration_s) >= p_min_duration_s)
           AND (p_max_duration_s  IS NULL OR coalesce(t.audio_duration_s, t.asr_duration_s) <= p_max_duration_s)
           AND (p_min_words       IS NULL OR c.word_count >= p_min_words);
        IF v_ids IS NULL THEN
            RETURN;
        END IF;
        SELECT count(*) INTO v_chunks
          FROM tx.chunk_vector_current WHERE chunk_vector_current.transcript_id = ANY (v_ids);
        v_exact := v_chunks <= p_exact_below;
    END IF;

    -- OR-semantics keyword query (NL questions AND-ed over every lexeme match nothing);
    -- quoted phrases / OR / -term switch to websearch syntax.
    IF position('"' IN p_query) > 0 OR p_query ~ '(^|\s)(OR|-\w)' THEN
        v_tsq := websearch_to_tsquery('english', p_query);
    ELSE
        v_tsq := nullif(replace(plainto_tsquery('english', p_query)::text, '&', '|'), '')::tsquery;
    END IF;

    RETURN QUERY
    WITH
    cv_ann AS MATERIALIZED (
        SELECT v.chunk_id, v.transcript_id, (v.embedding <#> p_query_vec)::float8 AS dist
          FROM tx.chunk_vector_current v
         WHERE p_query_vec IS NOT NULL AND NOT v_exact
           AND (v_ids IS NULL OR v.transcript_id = ANY (v_ids))
         ORDER BY v.embedding <#> p_query_vec
         LIMIT p_k_chunks
    ),
    cv_exact AS MATERIALIZED (
        SELECT s.chunk_id, s.transcript_id, s.dist
          FROM (SELECT v.chunk_id, v.transcript_id, (v.embedding <#> p_query_vec)::float8 AS dist
                  FROM tx.chunk_vector_current v
                 WHERE p_query_vec IS NOT NULL AND v_exact AND v.transcript_id = ANY (v_ids)
                OFFSET 0) s                                   -- fence: never the HNSW index
         ORDER BY s.dist
         LIMIT p_k_chunks
    ),
    cv AS (
        SELECT x.chunk_id, x.transcript_id, row_number() OVER (ORDER BY x.dist, x.chunk_id) AS r
          FROM (SELECT * FROM cv_ann UNION ALL SELECT * FROM cv_exact) x
    ),
    cf AS MATERIALIZED (
        SELECT s.chunk_id, s.transcript_id, row_number() OVER (ORDER BY s.rk DESC, s.chunk_id) AS r
          FROM (SELECT c.chunk_id, c.transcript_id, ts_rank_cd(c.tsv, v_tsq, 1) AS rk
                  FROM tx.chunk_current c
                 WHERE v_tsq IS NOT NULL AND c.tsv @@ v_tsq
                   AND (v_ids IS NULL OR c.transcript_id = ANY (v_ids))
                 ORDER BY rk DESC, c.chunk_id
                 LIMIT p_k_chunks) s
    ),
    dv AS MATERIALIZED (
        SELECT s.transcript_id, row_number() OVER (ORDER BY s.dist, s.transcript_id) AS r
          FROM (SELECT d.transcript_id, (d.embedding <#> p_query_vec)::float8 AS dist
                  FROM tx.doc_vector d
                 WHERE p_query_vec IS NOT NULL
                   AND (v_ids IS NULL OR d.transcript_id = ANY (v_ids))
                 ORDER BY d.embedding <#> p_query_vec
                 LIMIT p_k_docs) s
    ),
    dl AS MATERIALIZED (
        SELECT s.transcript_id, row_number() OVER (ORDER BY s.rk DESC, s.transcript_id) AS r
          FROM (SELECT c.transcript_id,
                       coalesce(ts_rank_cd(c.doc_tsv, v_tsq, 1), 0)
                       + word_similarity(p_query, c.display_title) AS rk
                  FROM tx.transcript_current c
                 WHERE ((v_tsq IS NOT NULL AND c.doc_tsv @@ v_tsq) OR p_query <% c.display_title)
                   AND (v_ids IS NULL OR c.transcript_id = ANY (v_ids))
                 ORDER BY rk DESC, c.transcript_id
                 LIMIT p_k_docs) s
    ),
    legs AS (
        SELECT cv.transcript_id, min(cv.r) AS r, 1 AS leg FROM cv GROUP BY cv.transcript_id
        UNION ALL SELECT cf.transcript_id, min(cf.r), 2 FROM cf GROUP BY cf.transcript_id
        UNION ALL SELECT dv.transcript_id, dv.r, 3 FROM dv
        UNION ALL SELECT dl.transcript_id, dl.r, 4 FROM dl
    ),
    fused AS (
        SELECT l.transcript_id,
               sum(p_weights[l.leg] / (p_rrf_k + l.r)::float8)  AS score,
               min(l.r) FILTER (WHERE l.leg = 1)::int           AS r_cv,
               min(l.r) FILTER (WHERE l.leg = 2)::int           AS r_cf,
               min(l.r) FILTER (WHERE l.leg = 3)::int           AS r_dv,
               min(l.r) FILTER (WHERE l.leg = 4)::int           AS r_dl
          FROM legs l
         GROUP BY l.transcript_id
         ORDER BY score DESC, l.transcript_id
         LIMIT p_limit
    ),
    chunk_scores AS (
        SELECT u.chunk_id, u.transcript_id, sum(u.w) AS s
          FROM (SELECT cv.chunk_id, cv.transcript_id, p_weights[1] / (p_rrf_k + cv.r)::float8 AS w FROM cv
                UNION ALL
                SELECT cf.chunk_id, cf.transcript_id, p_weights[2] / (p_rrf_k + cf.r)::float8 FROM cf) u
         WHERE u.transcript_id IN (SELECT f.transcript_id FROM fused f)
         GROUP BY u.chunk_id, u.transcript_id
    ),
    best AS (
        SELECT DISTINCT ON (cs.transcript_id) cs.transcript_id, cs.chunk_id
          FROM chunk_scores cs
         ORDER BY cs.transcript_id, cs.s DESC, cs.chunk_id
    )
    SELECT f.transcript_id, t.wax_item_id, c.version_id, c.version_no, c.produced_by,
           c.display_title, c.summary, c.classification, c.tags, t.origin, t.effective_at,
           coalesce(t.audio_duration_s, t.asr_duration_s), t.note_path, t.audio_s3_uri, t.audio_https_url,
           f.score, f.r_cv, f.r_cf, f.r_dv, f.r_dl,
           ch.chunk_id, ch.ordinal, ch.start_s,
           CASE
               WHEN ch.chunk_id IS NULL THEN left(coalesce(c.summary, ''), 280)
               WHEN v_tsq IS NOT NULL AND ch.tsv @@ v_tsq
                   THEN ts_headline('english', ch.search_text, v_tsq,
                                    'MaxFragments=2,MinWords=8,MaxWords=30,StartSel=«,StopSel=»')
               ELSE left(ch.search_text, 280)
           END
      FROM fused f
      JOIN tx.transcript t         ON t.transcript_id = f.transcript_id
      JOIN tx.transcript_current c ON c.transcript_id = f.transcript_id
      LEFT JOIN best b             ON b.transcript_id = f.transcript_id
      LEFT JOIN tx.chunk_current ch ON ch.chunk_id = b.chunk_id
     ORDER BY f.score DESC, f.transcript_id;
END $$;

GRANT EXECUTE ON FUNCTION tx.search(text, halfvec, integer, timestamptz, timestamptz, text[], text[],
                                    text[], text[], numeric, numeric, integer, integer, integer,
                                    integer, float8[], integer)
      TO transcriptions_app, transcriptions_ro;
```

Every search result is a head version. `chunk_current` contains only head-body chunks, which `check_invariants` verifies. The returned `version_id`/`version_no` come from `transcript_current`, the materialized head.

### C.5 Expected plans and latency (about 7.4k chunks, about 860 transcripts)

**Unfiltered vector leg:**
```
Limit (rows=200) -> Index Scan using chunk_vector_current_hnsw on chunk_vector_current v
                     Order By: (embedding <#> '[…]'::halfvec)
```
Expected 2–5 ms. Hindsight's baseline is 23 ms over 64k rows.

**Large filtered set:** the same HNSW scan with `Filter: (transcript_id = ANY ('{…}'))`. Iterative scan continues until 200 rows or `max_scan_tuples` (20,000, which is more than the corpus).

**Small filtered set:**
```
Limit -> Sort (top-N heapsort) Sort Key: s.dist -> Subquery Scan s
      -> Bitmap Heap Scan on chunk_vector_current (Recheck: transcript_id = ANY …)
         -> Bitmap Index Scan on chunk_vector_current_transcript
```
Under 10 ms at 4,000 chunks or fewer.

**Keyword leg:**
```
Limit -> Sort (top-N) Key: ts_rank_cd DESC
      -> Bitmap Heap Scan on chunk_current
         (Recheck: tsv @@ q) -> Bitmap Index Scan on chunk_current_tsv
```
2–8 ms, depending on how common the terms are.

The doc legs take under 2 ms. Fusion and `ts_headline` for 10 rows take 1–3 ms. **The SQL function totals about 10–20 ms.**

**End to end:**

| Path | Time |
|---|---|
| Query embedding, cache hit | under 1 ms |
| Query embedding, warm Ollama | ~80–100 ms |
| Query embedding, cold load | ~5.6 s (`keep_alive=30m` keeps it rare) |
| CLI process cost on top | Python start plus one `op read` for the password, ~0.5–1 s. In-process callers resolve it once. |
| **Warm in-process hybrid search** | **~120 ms** |

**Headroom:** HNSW stays at single-digit milliseconds up to about 10^6 chunks. Past that, add a `binary_quantize` expression index with exact re-ranking, and denormalise `effective_at` onto `chunk_vector_current`.

---

## D. Wax integration

### D.1 Principle

The pass path does exactly two things, and neither can fail it:

1. **Synchronous local capture.** It writes a full snapshot of the note into SQLite (`wax.db`). This is the durable record of the version; it is needed because the vault has no git and the next pass overwrites the file.
2. **Best-effort synchronous projection** into Postgres. This uses `connect_timeout=2` and `statement_timeout=5000`, behind a breaker stored in ledger `meta` (`transcriptions.sync_breaker_until`, 300 s after any failure).

A `wax-transcriptions` daemon thread then handles the rest: it drains anything not yet projected, reconciles the vault, and embeds. A Postgres or Ollama outage shows up as lag in `wax status` and `wax doctor`, never as a pass or worker failure. The same split is used by the outbox (`events.py:1-12`, "delivered means acked").

### D.2 SQLite capture queue (`outbox.py`, created by `_ensure()` in the style of `events.py:68-83`)

```sql
CREATE TABLE IF NOT EXISTS transcript_snapshots (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         TEXT NOT NULL,
    kind            TEXT NOT NULL,          -- 'version' | 'pass_run'
    idempotency_key TEXT NOT NULL UNIQUE,
    md_path         TEXT,
    note_text       TEXT,                   -- full note at capture; NULL for pass_run
    payload         TEXT NOT NULL,          -- JSON: pass facts + ledger facts + asr summary
    created_at      TEXT NOT NULL,
    projected_at    TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT
);
CREATE INDEX IF NOT EXISTS transcript_snapshots_pending ON transcript_snapshots(projected_at, item_id, seq);
```

The payload is built field by field, each piece wrapped in `try`, so a MagicMock ledger in tests degrades to `None` instead of raising. It contains:
- **Pass facts:** slug, `pass_version`, attempt, `command_id`, state, `reason_code`, detail (first 300 characters), `definition_id`, `changed_fields`, `duration_s`, the pre-rename path, and `evidence` with `body_replace` stripped.
- **Ledger facts:** the `items` row, the `transcripts` row, `archive.references(item_id)` (`archive.py:329-337`), and `processing_plans` (`plan_id` plus provenance via `passes._plan_provenance`, `passes.py:1296-1309`).
- **For transcription snapshots only:** an `asr.json` summary (language_probability, segment count, duration, model, backend).

### D.3 Exact hooks

**`src/wax/transcriptions/__init__.py`** (public API; never raises):

```python
class StoreUnavailable(RuntimeError): ...

def record_transcription(*, item_id: str, md_path: Path, meta: dict, verdict: dict,
                         asr_path: Path, attempt: int, processing_seconds: float) -> None: ...

def record_pass(*, item_id: str, slug: str, kind: str, state: str, md_path: Optional[Path],
                pass_version: int, attempt: int, command_id: Optional[str],
                reason_code: Optional[str], detail: str, definition: Optional[dict],
                pass_result: Optional[dict], changed_fields: list[str],
                duration_s: Optional[float], pre_rename_md: Optional[Path] = None) -> None: ...
```

Both functions follow the same five steps:

1. Return if `WAX_TRANSCRIPTS=0`.
2. Enqueue the snapshot (`INSERT OR IGNORE` on `idempotency_key`).
3. If a DSN is configured and the breaker is closed, call `store.project_item(item_id)`. That projects all of this item's pending snapshots, in `seq` order.
4. On any failure, set the breaker and log a warning.
5. Swallow every `Exception`.

**`src/wax/passes.py`.** Add this helper after `_write_pass_note` (`:966-984`):

```python
def _capture(item_id: str, slug: str, **kw: Any) -> None:
    """Version the note in the `transcriptions` store. Fail-open: never raises, never fails a pass."""
    if is_document(item_id):
        return
    try:
        from . import transcriptions
        transcriptions.record_pass(item_id=item_id, slug=slug, **kw)
    except Exception as exc:  # noqa: BLE001
        log.warning("transcriptions capture failed for %s/%s: %s: %s", item_id, slug, type(exc).__name__, exc)
```

It is called from three sites, all under the item flock (`@serialized`, `:156-161`):

| Site | Placement | Call |
|---|---|---|
| `dependency_failed` | after the note write at `:1059-1067`, before the log at `:1068` | `_capture(item_id, slug, kind="pass_run", state="failed", md_path=md, pass_version=version, attempt=attempt, command_id=None, reason_code="dependency_failed", detail=detail, definition=ep, pass_result=None, changed_fields=[], duration_s=None)` |
| when / skip_when skip | after `frontmatter.merge` at `:1119-1122`, before the return at `:1123` | `_capture(item_id, slug, kind="version", state="skipped", md_path=md, pass_version=int(ep["version"]), attempt=attempt, command_id=None, reason_code=skip_reason, detail=skip_detail, definition=ep, pass_result=None, changed_fields=[], duration_s=None)` |
| main path | after `_write_pass_note(current_md, …)` at `:1248-1251`, before the log at `:1253` | `_capture(item_id, slug, kind="version" if state in ("completed", "skipped") else "pass_run", state=state, md_path=current_md, pass_version=version, attempt=attempt, command_id=cid, reason_code=failure, detail=err if not ok else "", definition=ep, pass_result=pass_result, changed_fields=changed_fields, duration_s=took, pre_rename_md=md)` |

`inapplicable_capability` (`:1080-1095`) is document-only, so it is not hooked. A `PassError` before the child starts (`run_auto` `:1350`, `run_all` `:1285`) is a configuration error that does not touch the note, so it is not hooked either. Every caller of `passes.run` (worker, sweep, tray retry, `wax ep run`) is covered by these three sites.

**`src/wax/transcribe_adapter.py`.** Hoist the processing time at `:366` into a local `processing_s`. Then, after `ledger.set_item_state(... "transcribed" ...)` at `:369-370` and still inside `if item_id:`:

```python
        try:
            from . import transcriptions
            transcriptions.record_transcription(item_id=item_id, md_path=final, meta=meta, verdict=verdict,
                                                asr_path=asr_path, attempt=attempt,
                                                processing_seconds=processing_s)
        except Exception as exc:  # noqa: BLE001 - the store must never cost a transcript
            log.warning("transcriptions capture failed for %s: %s", item_id, exc)
```

This also covers `wax transcribe <file>` (`bin/wax:1718`).

**`src/wax/operations.py`.** Add a non-blocking variant next to `item_lock` (`:12-30`):

```python
@contextmanager
def try_item_lock(item_id):
    """Yield True holding the item flock, or False at once if someone else holds it."""
    # same path derivation as item_lock; fcntl.LOCK_EX | fcntl.LOCK_NB; honours _local.held re-entrancy
```

### D.4 Projector (`store.py`): one Postgres transaction per snapshot

```python
def project_item(item_id: str, *, limit: int = 50) -> int:   # pending snapshots for one item, seq order
def project_pending(*, limit: int = 200) -> dict:            # drain: per item, try_item_lock(); skip if held
def apply_snapshot(conn, snap: Snapshot) -> Outcome:          # the steps below
```

1. Run `ext = extract.from_note(note_text, relpath, ledger=…, asr=…, sidecar=…)`. Pure. It produces identity columns, version columns, child rows, hashes and body stats. Verify `frontmatter_block + body == note_text`.
2. Resolve the identity with `SELECT … FOR UPDATE`, matching in this order: `wax_item_id`, then exact `note_relpath`. Insert it if absent. Coalesce identity columns: new non-null values win for ledger-observed fields (`ledger_state`, `audio_local_path`, `backup_*`, `note_*`), and existing non-null values are kept for transcribe-time facts.
3. Handle a rename. If `identity.note_relpath ≠ snapshot relpath`, update it and upsert `transcript_path(role='canonical')`.
4. Run `INSERT INTO transcript_body … ON CONFLICT DO NOTHING RETURNING`. Only if a row was inserted, insert the `body_speaker` rows.
5. Decide using `head = SELECT … FROM transcript_current WHERE transcript_id=$1` and `versioning.decide(head, kind, produced_by, pass_version, content_sha256)`:
   - no head → `mint`
   - same `produced_by` + `pass_version` + `content_sha256` → `no_change`
   - reconcile with the same content → `skip`
   - backfill with any existing head → `skip`
   - anything else → `mint`
6. If minting:
   - Run `INSERT transcript_version … ON CONFLICT (transcript_id, idempotency_key) DO NOTHING RETURNING version_id`. The trigger assigns `version_no` and advances the pointer.
   - Insert `version_tag`, `version_project`, `version_ticket` and `version_pass_state`.
   - Upsert `transcript_current`.
   - If the head body changed: run `DELETE FROM chunk_current WHERE transcript_id=$1` (this cascades to the vectors). If the transcript is searchable, insert the new chunks, set `chunk_count`, then fill vectors from cache:
     ```sql
     INSERT INTO tx.chunk_vector_current (chunk_id, transcript_id, model_id, embedding)
     SELECT c.chunk_id, c.transcript_id, e.model_id, e.embedding
       FROM tx.chunk_current c
       JOIN tx.embedding_cache e ON e.model_id = $active AND e.input_sha256 = c.search_sha256
      WHERE c.transcript_id = $1;
     ```
   - If `doc_input_sha256` changed: fill `doc_vector` from the cache, or delete the stale row so the sweep refills it.
7. For pass snapshots, insert `pass_run` (`source='hook'`, `outcome` ∈ {`version`, `no_change`, `not_applied`}) with `ON CONFLICT (idempotency_key) DO NOTHING`.
8. Commit. The deferred FKs prove that the head and the pointer agree. Then `UPDATE transcript_snapshots SET projected_at=? WHERE seq=?`. If the process dies between the commit and the mark, re-projection is a no-op because of the idempotency keys.

**Ordering safety:**
- The sync path runs under the item flock.
- The drain skips items whose flock is held.
- Both paths process an item's snapshots in `seq` order, and `FOR UPDATE` serializes version inserts per transcript.
- Reconcile skips any item that has pending snapshots.

### D.5 Daemon thread (`bin/waxd`, next to `wax-outbox`, `:123-129`)

This deliberately does not go in `Worker._tick_once`:
- The Worker only ticks while `state.pipeline_enabled()` (`worker.py:878-880`).
- Embedding must never delay the inbox drain.
- The tests around `worker.py` are fragile.

```python
        threading.Thread(target=self._transcriptions_loop, daemon=True, name="wax-transcriptions").start()

    def _transcriptions_loop(self):
        while not self.stopped.is_set():
            try:
                transcriptions.tick(gpu_busy=lambda: bool((self.snapshot.get("inbox") or {}).get("active_item")))
                self._condition("transcriptions_error", None)
            except Exception as e:  # noqa: BLE001 - the store must never take waxd down
                self._condition("transcriptions_error", f"{type(e).__name__}: {e}")
            self.stopped.wait(transcriptions.config.tick_s())          # default 15 s
```

`transcriptions.tick()` does the following:
1. With no DSN, write `meta['transcriptions.last_tick']` = `{"state": "disabled"}` and return.
2. `store.project_pending(limit=200)`.
3. If `meta['transcriptions.last_reconcile']` is older than `WAX_TRANSCRIPTS_RECONCILE_INTERVAL_S` (600), run `reconcile.sweep()`.
4. If `gpu_busy()` is false, run `embed.sweep(max_batches=8)` (8 inputs per batch).
   - The sweep only embeds transcripts whose body has settled: no body-mutating pass in the frozen plan (`body_mutation` set in `processing_plans.definitions`) is still non-terminal, where terminal means completed, skipped, or failed with `exhausted_at` set or `attempt ≥ passes.DEFAULT_MAX_ATTEMPTS`.
   - A chunk older than 6 h is embedded regardless.
   - The order is chunks of the newest `effective_at` first, then doc vectors.
5. Write a stats JSON into `meta['transcriptions.last_tick']`.

`active_item` covers transcription and diarization alike, because the claim stage includes `enrich`. Gating on it stops the 5.7 GB embedder from competing with whisper and pyannote for the GPU.

### D.6 Reconcile and backfill (`reconcile.py`)

- **Files:** list `paths.VAULT.glob("*.md")`, non-recursive like `reconcile.scan_vault` (`reconcile.py:113-144`). Exclude `*.suspect.md`, `.wax-ep-*` and `*.tmp`.
- **Change detection:** load the store's map (`relpath → mtime_ns, size, head content`) in one query. Skip files whose stat is unchanged.
- **Identity matching:**
  1. `wax-item-id`. If this file is not the ledger's canonical `transcripts.md_path` (447 of 455 match), record it as `transcript_path(role='alias')` and do not version it.
  2. Otherwise `note_relpath`.
  3. Otherwise an identity whose note vanished in this sweep and whose head `body_sha256` equals this note's body. This catches hand renames of Hermes or legacy notes.
  4. Otherwise create a new identity.
- **Versions:**
  - A new identity gets one `backfill` version.
  - A changed `content_sha256` gets a `reconcile` version. This captures Obsidian edits and any hook that was missed.
  - Same content means stat update only.
  - Wax items are skipped if `try_item_lock` fails or snapshots are pending.
- **Ledger-only identities:** a `transcripts` row whose note is missing (11 today) gets an identity with `note_missing_at` set and no version, so the gap stays visible.
- **Vanished notes:** an identity whose note is gone gets `note_missing_at`. It is never deleted.
- **Pass history import** is idempotent and continues from `meta['transcriptions.outbox_watermark']`:
  - Outbox subjects `%.audio.task.completed`, `.failed` and `.skipped` (both the `evt.` and legacy `evt.v1.` families) become `pass_run` rows with `source='outbox'` and key `pass:{command_id}`. That key dedupes them against hook rows.
  - Ledger `passes` rows then add `evidence`, `definition_id` and `reason_code` (`source='ledger'`, same key when `command_id` is present).
- **`--check`:**
  - Returns parity across disk, ledger and store: notes on disk, canonical notes, aliases, identities by origin, identities with `wax_item_id`, ledger transcripts without an identity, heads, chunks, embedded chunks, pending doc vectors, and `tx.check_invariants()` rows.
  - Sets `drift=true` on any mismatch, which gives CLI exit 2.

### D.7 CLI (`bin/wax`)

The parser goes after the `documents` block (`:1526-1555`), in the same style:

```python
    trx = sub.add_parser("transcripts", help="versioned transcript store (Postgres `transcriptions`)",
                         parents=[common]).add_subparsers(dest="sub", required=True)
    tsy = trx.add_parser("sync", parents=[common],
                         help="project queued snapshots, then reconcile vault + ledger (backfills unknown notes)")
    tsy.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    tsy.add_argument("--check", action="store_true", help="parity report only; exit 2 on drift")
    tsy.add_argument("--embed", action="store_true", help="also run the embed sweep until nothing is pending")
    tsy.add_argument("--limit", type=int, default=0, help="reconcile at most N notes (0 = all)")
    tse = trx.add_parser("search", parents=[common], help="hybrid semantic + keyword search over latest versions")
    tse.add_argument("query", nargs="+")
    tse.add_argument("-n", "--limit", type=int, default=10)
    tse.add_argument("--since", help="ISO date/datetime (effective_at >=)")
    tse.add_argument("--until", help="ISO date/datetime (effective_at <)")
    tse.add_argument("--classification", action="append", choices=["monolog", "meeting", "other"])
    tse.add_argument("--tag", action="append")
    tse.add_argument("--project", action="append")
    tse.add_argument("--origin", action="append", choices=["wax", "hermes-stt", "legacy"])
    tse.add_argument("--min-duration", type=float, help="seconds")
    tse.add_argument("--max-duration", type=float, help="seconds")
    tse.add_argument("--min-words", type=int)
    tse.add_argument("--lexical", action="store_true", help="no query embedding (keyword + title only)")
    tsh = trx.add_parser("show", parents=[common], help="one transcript: identity + latest (or --version N)")
    tsh.add_argument("ref", help="wax item id, #transcript_id, or note filename/path (fuzzy)")
    tsh.add_argument("--version", type=int, help="version_no (default: latest)")
    tsh.add_argument("--raw", action="store_true", help="print that version's exact note bytes")
    thi = trx.add_parser("history", parents=[common], help="every version and pass run of one transcript")
    thi.add_argument("ref")
    tmi = trx.add_parser("migrate", parents=[common], help="apply pending schema migrations (owner role)")
    tmi.add_argument("--dry-run", action="store_true")
    tem = trx.add_parser("embed", parents=[common], help="embed pending chunk/doc vectors now")
    tem.add_argument("--limit", type=int, default=0)
```

Dispatch goes before `doctor` (`:1984`). It follows the `documents` pattern at `:1889-1922` and uses `_emit(out, J)`:

| Subcommand | Behaviour |
|---|---|
| `sync` | `transcriptions.sync(dry_run=…, check=…, embed=…, limit=…)`; exit 2 on `drift` or `errors` |
| `search` | `transcriptions.search.run(" ".join(args.query), …)`. Human output is one line per hit: `score  YYYY-MM-DD  class  mm:ss  title  vN(produced_by)`, then an indented snippet and the `note_path`. JSON adds `mode: hybrid\|lexical` and the four leg ranks. |
| `show` | latest by default; `--raw` prints `frontmatter_block \|\| body` |
| `history` | `transcript_history` plus `pass_run` |
| `migrate` | `migrate.apply(dry_run=…)` |
| `embed` | `embed.sweep(until_empty=True, limit=…)` |

`StoreUnavailable` subclasses `RuntimeError`, so the existing handler at `:1993-1999` prints `error: …` and exits 2. Add `transcriptions` to the import at `:32-34`; the package imports psycopg lazily.

### D.8 Status and doctor

- **`ledger.enrich`:** after the outbox block (`:834-838`), set `snap["transcriptions"] = transcriptions.health.snapshot()`. It reads SQLite only (queue count, oldest age, `last_tick` meta) and is wrapped in `try`. States:
  - `ok`
  - `disabled` (no DSN)
  - `lagging` (oldest queued > 600 s)
  - `error` (oldest queued > `WAX_TRANSCRIPTS_LAG_FAIL_S` (3600), embed backlog older than 6 h, or a model digest mismatch)
- **`status_unhealthy`** (`bin/wax:1353-1368`): also returns `True` on `transcriptions.state == "error"`. Green is not evidence.
- **`render_status`:** add `transcripts  <state> (queued N, embed pending M)`.
- **Doctor probes** (in `probe_all`, `bin/wax:642`, beside `probe_outbox` `:1151`):
  1. Store reachable, and the schema is at the latest migration (FAIL otherwise).
  2. Projection drained (FAIL if the oldest item is older than the lag limit).
  3. Parity: identities with `wax_item_id` versus canonical ledger notes (WARN).
  4. `tx.check_invariants()` returns zero rows (FAIL).
  5. Embeddings current (WARN if older than 1 h).
  6. Ollama serves the pinned digest (FAIL on mismatch).
  7. A vector index is in use: `EXPLAIN (FORMAT JSON)` of the canonical ANN query must name `chunk_vector_current_hnsw` (FAIL otherwise).

### D.9 Configuration and secrets

**`ENV_SPECS` additions** (`bin/wax:256`). Every new knob must be listed:

| Variable | Default | Notes |
|---|---|---|
| `WAX_TRANSCRIPTS` | `1` | `0` disables capture and projection |
| `WAX_TRANSCRIPTS_DSN` | `<unset = capture only>` | Password-less libpq URI. Its name does not match `SECRET_NAME_RE` (`:92`), so it must never carry a password. |
| `WAX_TRANSCRIPTS_PASSWORD_OP` | — | `op://DeLoSecrets/<app-item-uuid>/password` |
| `WAX_TRANSCRIPTS_PASSWORD` | `<unset>` | direct override; redacted |
| `WAX_TRANSCRIPTS_OWNER_DSN` | — | `postgresql://transcriptions_owner@localhost:5432/transcriptions` |
| `WAX_TRANSCRIPTS_OWNER_PASSWORD_OP` | — | owner item; used by `migrate` |
| `WAX_TRANSCRIPTS_TICK_S` | `15` | |
| `WAX_TRANSCRIPTS_RECONCILE_INTERVAL_S` | `600` | |
| `WAX_TRANSCRIPTS_SYNC_BREAKER_S` | `300` | |
| `WAX_TRANSCRIPTS_LAG_FAIL_S` | `3600` | |
| `WAX_TRANSCRIPTS_LOCAL_TZ` | `America/New_York` | |
| `WAX_EMBED_URL` | `http://localhost:11434` | |
| `WAX_EMBED_BATCH` | `8` | |
| `WAX_EMBED_KEEP_ALIVE` | `30m` | |
| `WAX_EMBED_TIMEOUT_S` | `60` | |
| `WAX_S3_ENDPOINT` | `<mc alias delo>` | already read by `finalize.py:16`, but missing from `ENV_SPECS` today |

The model name, digest and dimensions are code constants in `embed.py`, checked against the active `tx.embedding_model` row. They are not environment knobs, because changing them without a migration would corrupt the index.

**Resolution:**
- `config.py` reads `os.environ` first, then falls back to the `Environment=` lines of the installed `~/.config/systemd/user/waxd.service` (parsed with `shlex`). That way the CLI and the daemon share one source of truth, and no credential file is ever written.
- `db.py` resolves the password with `provider.resolve_key(env_var="WAX_TRANSCRIPTS_PASSWORD",op_ref=…)` (`provider.py:128-156`, `op read` with a 5 s timeout, using the `OP_SERVICE_ACCOUNT_TOKEN` already present in waxd's environ) and caches it per process.
- It then calls `psycopg.connect(dsn, password=pw, connect_timeout=2, options="-c statement_timeout=5000 -c search_path=tx,public")`. Search connections use a 10 s `statement_timeout`.

**Unit template additions** (`deploy/systemd/user/waxd.service`, after `:27`):
```
Environment=WAX_TRANSCRIPTS_DSN=postgresql://transcriptions_app@localhost:5432/transcriptions?application_name=waxd
Environment=WAX_TRANSCRIPTS_PASSWORD_OP=op://DeLoSecrets/<app-item-uuid>/password
Environment=WAX_TRANSCRIPTS_OWNER_DSN=postgresql://transcriptions_owner@localhost:5432/transcriptions?application_name=wax-migrate
Environment=WAX_TRANSCRIPTS_OWNER_PASSWORD_OP=op://DeLoSecrets/<owner-item-uuid>/password
Environment=WAX_S3_ENDPOINT=https://s3.delo.sh
```
These are committed `op://` references, referenced by item UUID as the global rule requires. Nothing is added to the repo `.env.op`, because the mise hook would materialize a plaintext password into `.env`.

**1Password:** `deploy/provision-transcriptions` creates three DATABASE items in DeLoSecrets, shaped like "Candybar Postgres Role" (server, port, database, username, password, dsn):
- "Transcriptions Postgres Owner"
- "Transcriptions Postgres App"
- "Transcriptions Postgres RO"

Each item is created by piping a JSON template to `op item create --vault DeLoSecrets -` on stdin, never through argv or a file. The script prints the UUIDs to paste into the unit template. The RO item is for outside readers (Holocene, agents); Wax itself uses the app role.

**Dependencies:** in `pyproject.toml` (`:10`), set `dependencies = ["PyYAML>=6", "jsonschema>=4.19", "psycopg[binary]>=3.2"]`. This is documentation only: the runtime is apt's `python3-psycopg` 3.2.6 on `/usr/bin/python3`. No `pgvector` adapter is needed, because vectors travel as `'[…]'::halfvec` text literals formatted with `.6g`.

### D.10 Module layout

```
src/wax/transcriptions/
  __init__.py      record_transcription, record_pass, sync, tick, StoreUnavailable   (no psycopg import)
  config.py        env + installed-unit fallback, all knobs above
  db.py            lazy psycopg connect (app/owner/ro), password via provider.resolve_key, timeouts
  migrate.py       runner: tx.schema_migrations(version PK, sha256, applied_at); pg_advisory_lock;
                   one transaction per file; refuses if an applied file's sha256 changed
  migrations/0001_core.sql, 0002_search.sql
  outbox.py        SQLite transcript_snapshots: _ensure, enqueue, pending(item|all), mark, backlog
  extract.py       PURE: split_note, to_jsonable, parse_ts, parse_recorded_at, legacy bullets,
                   origin, coalesce identity/version columns, child rows, hashes, body stats, doc_input
  chunking.py      PURE: chunk(body) -> list[Chunk]; CHUNKER_VERSION, TARGET/HARD_CAP/OVERLAP
  versioning.py    PURE: idempotency_key(...), decide(head, ...)
  gates.py         body_settled(item_id) from the ledger plan + passes
  store.py         project_item, project_pending, apply_snapshot (section D.4)
  embed.py         OllamaEmbedder (batch<=8, digest check, norm check), sweep(), query_vector()
  search.py        run(query, **filters) -> {mode, results}; show(ref, version, raw); history(ref)
  reconcile.py     sweep(dry_run, limit, check), history import, parity
  health.py        snapshot() for status (SQLite only); probes() for doctor
```

---

## E. Test plan

### E.1 Pure tests

These run under `mise run wax:test` on mise Python 3.14. They never import psycopg and need no Docker.

**`tests/transcriptions_extract_test.py`** checks that:
- `frontmatter_block + body == text` holds for: a full Wax note, a migrated note with a `wax` block but no passes, a Hermes note, a legacy note with bullets, an Otter note, a note with no frontmatter, and a 17-byte note.
- `body_sha256` equals `sha256(frontmatter.split(text)[1].encode())`, the same split `passes.py:913` uses.
- A naive `transcribed-at` becomes UTC, an aware value keeps its offset, and a bare-date `captured` gets precision `date`.
- `tags: ''` becomes `[]`; a string tag becomes a one-item list; `labels` stays separate.
- Empty vault-router strings become NULL; unknown keys land in `vault_extra`.
- A completed pass carrying a stale `reason_code` becomes NULL in `version_pass_state`.
- `fell_through` parses model names that contain `/`.
- An invalid classification fails extraction for the version; the CHECK constraint mirrors `passes.py:864`.
- Origin comes out right for: `a-first.ogg.md` → `test`, a Hermes `/tmp/hermes-local-stt-…` note → `hermes-stt`, a clip with no source → `legacy`, and an item with an id → `wax`.
- All six `recorded_at` patterns parse, including `2025_05_31_17_53_58.mp3`, `clip_0055__20260720-105052` and `telnyx-2026-10-01` (precision `date`).
- Legacy `172m 44s` becomes 10364; `large-v3 (faster-whisper)` splits into model and backend.
- The coalescing order holds, e.g. `wax.audio_duration_s` beats `duration-seconds`, which beats the ledger value.
- Changing only `wax.passes.*.at` leaves `content_sha256` unchanged but changes `note_sha256`.

**`tests/transcriptions_chunking_test.py`** checks that:
- A note of 512 estimated tokens or fewer becomes exactly one chunk.
- Diarized bodies break only at turn boundaries, unless a single turn exceeds 512 tokens.
- No chunk exceeds 512 estimated tokens.
- Overlap is between 1 and 60 tokens and is real text (`char_start(k+1) < char_end(k)`).
- An unpunctuated 3,000-token monologue turn splits into word windows that carry the label.
- The H1 and legacy bullets are stripped; a `## Summary` section becomes a `kind='summary'` chunk.
- `MM:SS` with minutes above 59 and `H:MM:SS` both parse to `start_s`.
- `**Gershon:**` counts as a speaker; `**Meeting Date:**` does not.
- Output is deterministic: same input, same `search_sha256` list.
- `body[char_start:char_end] == text` for every chunk.

**`tests/transcriptions_versioning_test.py`** covers:
- Idempotency keys for every producer kind.
- `decide()`:
  - no head → mint
  - same slug + version + content → `no_change`
  - different slug, same content → mint (e.g. clipboard-copy after a stamp)
  - reconcile with identical content → skip
  - reconcile after an edit → mint
  - backfill with an existing head → skip

**`tests/transcriptions_capture_test.py`** uses a temp ledger (`patch.object(paths, "DB"/"VAR"/"VAULT")`, closing `ledger._local.conn` as in `passes_test.py:10-14`) and a real shell pass in a temp `passes.d`. It checks that:
- A completed pass enqueues exactly one `version` snapshot whose `note_text` equals the file after `_write_pass_note`, including the `wax.passes.<slug>` entry and any rename.
- A failed pass enqueues `pass_run` only.
- A `when` skip enqueues a `version` keyed `skip:…`.
- `dependency_failed` enqueues `pass_run` keyed `depfail:…`.
- A `doc-<sha16>@binding` key enqueues nothing.
- If `transcriptions.record_pass` raises, the pass result and its ledger row are unchanged.
- With the DSN unset, `store.project_item` is never called.
- With `store.project_item` patched to raise, the breaker is set and the next pass makes no projection attempt until it expires.

**`tests/transcriptions_embed_test.py`** runs a fake Ollama (`http.server` in a thread on an ephemeral port). It checks:
- The request carries `dimensions=2048`, `truncate=true`, `options.num_ctx=2048`, `keep_alive`, and at most 8 inputs.
- The query gets the `Instruct:` prefix; chunks get none.
- A non-unit vector is renormalised; a wrong length raises.
- A digest mismatch from `/api/tags` refuses the sweep.
- The halfvec literal is formatted correctly.

**`tests/transcriptions_cli_test.py`** imports `bin/wax` the way the existing tests load scripts. It checks that:
- `transcripts search a b --tag x --tag y --since 2026-10-01 --classification meeting --json` dispatches `search.run("a b", tags=["x","y"], …)`.
- `show 4c9f2d894fde1cc5 --version 2 --raw` dispatches correctly.
- `StoreUnavailable` gives exit 2.

### E.2 Real-Postgres tests: `tests/transcriptions_pg_test.py`

These run under `mise run wax:test:pg`, which is `/usr/bin/python3 -m pytest tests/transcriptions_pg_test.py`. System Python has psycopg 3.2.6 and pytest 7.4.4.

**Skip rules.** The module uses `unittest.skipUnless` when any of these hold:
- psycopg cannot be imported (so under mise Python it skips cleanly), or
- `docker info` fails, or
- `docker run` of `pgvector/pgvector:0.8.0-pg17` fails within 60 s. That image matches the host's pgvector 0.8.0 and Postgres 17. The container is started with `--rm -p 127.0.0.1::5432` and a password generated by the test. No real credential is used.

**Setup.** As the container superuser, create the three roles and extensions with the bootstrap SQL, run `migrate.apply` as owner, then run everything else as app. The fake embedder returns deterministic unit vectors seeded from `sha256(input)`.

**Cases:**
1. Migrations: the first run applies 2 files, a second run applies 0, and editing an applied file is refused.
2. As app, `UPDATE`/`DELETE` on `transcript_version` raises `InsufficientPrivilege`. As owner, the trigger raises.
3. A transcription snapshot gives v1:
   - `transcript_current.version_id` equals `transcript.current_version_id`.
   - Chunks exist and FTS finds them immediately.
   - `chunk_vector_current` is empty until `embed.sweep()`, which then makes N calls.
4. A frontmatter-stamp snapshot gives v2:
   - `chunk_id`s are unchanged.
   - `embedding_cache` and the embed call count are unchanged.
   - `content_sha256` has changed.
5. A title-slug snapshot with a rename gives v3:
   - A `transcript_path` canonical row exists for the old path and `note_relpath` has moved.
   - Exactly one doc embedding is made.
6. A diarization snapshot with a new body gives v4:
   - Chunks are swapped.
   - Chunks with cached `search_sha256` reuse their vectors with no embed call; only new chunk texts are embedded.
7. Re-delivering the v4 snapshot is a no-op.
8. A new attempt with the same pass version and unchanged content mints no version, and records a `pass_run` with `outcome='no_change'`.
9. Inserting a version without upserting `transcript_current` fails at COMMIT (the deferred FK).
10. `tx.search`:
    - Returns the head `version_id`.
    - Classification, tag, date and origin filters work.
    - A filtered small set (exact path) and the same set above `p_exact_below=0` (HNSW path) return the same top-3.
    - `EXPLAIN` of the canonical ANN statement names `chunk_vector_current_hnsw`.
    - Lexical mode (`p_query_vec` NULL) still returns title matches.
11. Reconcile over a temp vault of 8 fixture notes:
    - Every note gets one `backfill` version and a second run changes nothing.
    - Editing a body gives a `reconcile` version.
    - A duplicate `"… 1.md"` copy becomes an alias with no version.
    - A deleted note gets `note_missing_at` and its rows stay.
    - Outbox `task.completed` rows are imported once.
12. Outage: stop the container. A snapshot stays pending and the breaker opens. Restart the container, and `project_pending` projects in `seq` order with the right `version_no` sequence.
13. `tx.check_invariants()` returns zero rows after all of the above.

---

## F. Implementation plan and backfill runbook

Work on HeyMa `main` and commit and push after each step. Step 0 first: claim a HEYMA ticket with `px`, since `.project.json` binds the Plane board HEYMA with `heyma-pm`.

| # | File(s) | Work | Acceptance check |
|---|---|---|---|
| 1 | `deploy/provision-transcriptions` (new, `#!/usr/bin/python3`, dry run by default, modelled on `deploy/provision-inference`) | Find or create the three 1Password items. Pipe the B.1 SQL to `sudo -n -u postgres psql`. Print the item UUIDs. Idempotent: an existing role gets `ALTER ROLE … PASSWORD` from the vault. | After `--apply`, `psql "postgresql://transcriptions_app@localhost/transcriptions"` connects with the vault password; `\dx` shows vector 0.8.0 and pg_trgm 1.6; a second `--apply` changes nothing. |
| 2 | `src/wax/transcriptions/migrations/0001_core.sql`, `0002_search.sql`, `migrate.py`, `config.py`, `db.py`, `__init__.py` (stubs) | SQL exactly as in B.2 and C.4; runner as in D.10 | `wax transcripts migrate` applies 2 files, a re-run applies 0, and `SELECT * FROM tx.check_invariants()` returns 0 rows. |
| 3 | `extract.py` + `tests/transcriptions_extract_test.py` | Section B.3 rules | Tests pass. A throwaway run over the real vault (`/usr/bin/python3 -c …`, read-only) gives origins of about wax 449, hermes-stt about 334, legacy about 78, test 3, with no exceptions. |
| 4 | `chunking.py` + test | Section C.1 | Tests pass. Over the real vault: about 7.4k chunks, max token estimate ≤ 512, and 517 single-chunk notes (±5). |
| 5 | `versioning.py` + test | Keys and `decide()` | Tests pass |
| 6 | `src/wax/operations.py` | `try_item_lock` | Unit test: a second holder gets `False` immediately |
| 7 | `outbox.py`, `gates.py`, `__init__.py` (real hooks) | Sections D.2–D.3 | Capture test passes |
| 8 | `store.py` | Section D.4 | Cases E.2 3–9 and 12 pass |
| 9 | `embed.py` | Section C.2 | Embed test plus E.2 3–6 pass |
| 10 | `search.py` | Python wrapper with query-vector cache read-through (app role inserts cache rows; the RO role skips caching) | E.2 10 passes |
| 11 | `reconcile.py` | Section D.6 | E.2 11 passes |
| 12 | `health.py` | Section D.8 data | Unit test on a temp ledger |
| 13 | `src/wax/passes.py` | `_capture` helper plus the three call sites | Full suite: `mise x -- python3 -m pytest components/wax/tests -q` still gives 501 passed plus the new ones. `worker_enrichment_test` and `portability_test` are unchanged and green. |
| 14 | `src/wax/transcribe_adapter.py` | Hoist `processing_s`; hook after `:370` | `portability_test` green. Manual `wax transcribe <sample>` gives a `transcription` version in the store. |
| 15 | `bin/waxd` | `wax-transcriptions` thread (D.5) | After a restart the journal shows no `transcriptions_error`; `meta transcriptions.last_tick` advances every 15 s or so. |
| 16 | `src/wax/ledger.py` (`enrich`), `bin/wax` (ENV_SPECS, CLI, dispatch, `status_unhealthy`, `render_status`, doctor probes, import) | Sections D.7–D.9 | `wax transcripts --help` lists the subcommands. `wax doctor --json` has the seven probes. `wax status` shows a `transcripts` line. |
| 17 | `deploy/systemd/user/waxd.service` | The D.9 `Environment=` lines with the real UUIDs | `deploy/install-systemd-user --dry-run` renders them. After install, `systemctl --user daemon-reload` and a waxd restart (it does not stop the encoder scope), `/proc/<waxd>/environ` has `WAX_TRANSCRIPTS_DSN`. |
| 18 | `pyproject.toml` | Dependencies as in D.9 | — |
| 19 | `/home/delorenj/HeyMa/mise.toml` | Tasks `wax:transcripts:sync`, `wax:transcripts:search`, `wax:transcripts:migrate` (each `'{{config_root}}/bin/wax' transcripts <sub>`), and `wax:test:pg` (`dir = components/wax`, `run = "/usr/bin/python3 -m pytest tests/transcriptions_pg_test.py -q"`) | `mise tasks` lists them; `mise run wax:test:pg` passes, or skips cleanly with no Docker. |
| 20 | `tests/transcriptions_pg_test.py` | Section E.2 | Green |
| 21 | Docs | <br>• `docs/WAX-DESIGN.md`: new section "Transcriptions store" (A–D condensed) <br>• `docs/ENRICHMENT-PASSES.md`: every applied pass is versioned; frontmatter-only passes cost zero embeddings; `body_mutation` passes re-chunk <br>• HeyMa `AGENTS.md`: layout row for `transcriptions` and the `wax transcripts` commands <br>• the canonical skill at `~/code/skillex/all-skills/wax-pipeline`, never `.agents/skills` | Docs match the CLI `--help` output |

### F.1 Backfill runbook for the existing 868 notes

1. **Prerequisites.** Steps 1–2 and 13–17 are deployed. `wax doctor` shows the store reachable and the schema current.
2. **Dry run.** Run `wax transcripts sync --dry-run --json`. Expected today:
   - identities created ≈ 862 (868 notes − 6 aliases), plus about 11 ledger-only identities with `note_missing_at` set
   - `backfill` versions = canonical notes, about 862
   - aliases ≈ 6
   - pass_run imports ≈ 1,049 from the outbox, deduped by `command_id`
3. **Projection, no embedding.** Run `wax transcripts sync`. It is read-only on the vault and the ledger and takes about 1–2 minutes (868 reads, about 7.4k chunk inserts).
4. **Embedding.** Either let waxd's thread embed (8 batches × 8 chunks per 15 s tick, so about 30 minutes, deferring while anything is being transcribed or diarized), or run `wax transcripts embed` in the foreground (about 11–13 minutes at about 3,500 tokens/s). The CLI itself is light; the GPU work happens in Ollama. Then run `ANALYZE` as owner (`psql … -c 'ANALYZE'`) so the planner sees real row counts.
5. **Acceptance:**
   - `wax transcripts sync --check` exits 0 with:
     - `identities_with_wax_id` = distinct `wax-item-id` over canonical notes (449 today) + ledger-only identities
     - `heads` = identities that have a note
     - `chunks_embedded` = `chunks`
     - `doc_pending` = 0
     - `invariant_violations` = 0
   - SQL check: `SELECT count(*) FROM tx.transcript_version GROUP BY producer_kind` → only `backfill` before new activity.
   - SQL check: `SELECT count(*) FROM tx.chunk_current c JOIN tx.transcript_current h USING (transcript_id) WHERE c.body_sha256 <> h.body_sha256` → 0.
   - `wax transcripts search "phone quality time with the kids" -n 3` → `20260210-153625-spouses-disagreeing-over-quality-time-and-phone-usage.md` at rank 1, with v1 (`backfill`) and a `Speaker 2` snippet.
   - Live pass test: `wax ep run frontmatter-stamp <that item>` → `wax transcripts history <item>` shows v2 (`frontmatter-stamp`). `SELECT count(*) FROM tx.embedding_cache` is unchanged before and after, so zero new embeddings. `wax transcripts search` returns `version_no=2` for it.
   - Next recording: record → `transcription` v1 → stamp, clipboard, classification and title-slug versions (title-slug adds ≤ 1 doc embedding) → diarization version (re-chunk; only new chunk texts embedded after the body settles) → search returns the diarization version.
   - Outage drill: `sudo systemctl stop postgresql@17-main` is not allowed on the shared cluster. Run E.2 case 12 against the container instead, and on the host confirm that `WAX_TRANSCRIPTS_DSN` pointing at a wrong port leaves passes completing while `wax status` shows `transcripts lagging/error`.
   - Backups: the day after provisioning, `~/backups/postgres/dbs/transcriptions_YYYYMMDD.dump` exists. Restore-test it with `pg_restore --list`.
6. **Rollback.** Set `WAX_TRANSCRIPTS=0` in the unit and restart waxd. Capture and projection stop and nothing else changes. The vault and ledger were never written, so there is nothing to undo.

### F.2 Decisions an implementer must not reopen

- One store row per vault note, documents excluded. Hooks live only in `passes.py` and `transcribe_adapter.py`. The drain lives in a waxd thread, not `worker.py`.
- Embedding input is `search_text` only. Title and summary go into the doc vector, so frontmatter-only passes stay free.
- The head pointer is advanced by a trigger, enforced by deferred FKs, and versions are immutable by both grants and triggers.
- Search reads only the `*_current` tables. History has no ANN index.
---

## G. Review amendments (2026-10-28, orchestrator review before implementation)

1. **`searchable` applies to every search leg.** `tx.search` must exclude `transcript.searchable = false`
   rows in the doc-vector and doc-lexical legs too, including the unfiltered path (v_ids NULL). Test notes
   never surface in results.
2. **Deviations are recorded here.** If the implementation finds a fact in this document wrong (a line
   number, a measured number, a pgvector/psycopg behaviour), fix the implementation to match reality and
   add a dated bullet to this section. Do not silently diverge.
3. **The research reports** that fed this design (lifecycle, corpus survey, infra probes) are summarized in
   sections 0 and A–C; the raw survey script lived in /tmp and is not part of the design of record.
4. **Implementation findings (2026-10-28, stage F 2–12 + 20; verified against the real vault and a copy of
   the ledger projected into a disposable `pgvector/pgvector:0.8.0-pg17`).**
   - **Corpus numbers.** Origins over all 868 notes with ledger facts: wax 455 (449 canonical + 6 aliases),
     hermes-stt 335, legacy 75, test 3. Chunks: 7,428 over every note, 7,309 over canonical searchable
     notes; max token estimate 512. Single-chunk notes are 346 **plus 174 empty bodies** (H1 and legacy
     header only), which get **zero** chunks rather than one empty chunk: 520 notes at ≤ 512 tokens
     (C.1 said 517). `recorded_at` parses for 541 of 868 names (A said 490).
   - **Backfill rehearsal (F.1 step 2/3 against the container).** 862 identities + 862 `backfill`
     versions, 6 aliases, 11 ledger-only identities, 0 errors; all 862 versions restore byte-for-byte;
     `check_invariants()` 0 rows. History: 1,185 outbox `task.completed|failed|skipped` events seen
     (both families), 1,161 imported (24 belong to documents), plus 18 ledger-only `passes` rows;
     F.1's "≈1,049" counted `.completed` only. Dry run 7.5 s, real run 9.3 s (not 1–2 min), re-run
     0.0 s (862 unchanged). Real Ollama: 16 chunks in 2 calls (4.1 s incl. cold load), norms within
     4e-5 of 1, hybrid search ranked the spouses note #1 in 0.09 s; the doctor ANN probe named
     `chunk_vector_current_hnsw` on the real-size table.
   - **Reconcile key.** `reconcile:v{head_version_no}:{content_sha}`, not `reconcile:{content_sha}`:
     an edit A → B → A would otherwise collide with the first reconcile key and leave the head at B.
   - **`recorded_at`.** Pattern 2 also accepts a space (`20260728 125905.m4a`, 6 notes); that is the
     sixth pattern E.1 counts.
   - **hermes-stt.** Hermes legacy notes carry their path in the `**Source**` bullet, not
     `source-audio`, so the origin rule tests the coalesced `source_audio_original_path`.
   - **Poison snapshots.** An `ExtractError` (invalid classification, NUL byte) marks the SQLite row
     `rejected: …` (and records a `not_applied` `pass_run`) instead of wedging that item's queue;
     `outbox.backlog()`/`health.snapshot()` count `rejected`. Any other projection error leaves the row
     pending with `attempts`/`last_error`.
   - **History merge.** The app role has no UPDATE on `pass_run`, so ledger `passes` evidence,
     `definition_id` and `reason_code` are merged into the outbox row at insert time (same
     `pass:{command_id}` key); ledger rows without an outbox event insert with `source='ledger'`.
   - **Embed age.** `chunk_current` has no timestamp, so the 6 h override uses the first version that
     carried the head body (`min(transcript_version.created_at)` for that `body_sha256`). No schema change.
   - **`audio_https_url`.** Same formula as `finalize.audio_url`, but only from `WAX_S3_ENDPOINT` (env,
     then the installed unit); the projector never falls back to `mc alias list`. NULL when unset.
   - **Overlap.** Dropped when overlap + the next unit would pass `HARD_CAP` (an unsplit 452–512-token
     turn): 30 of ~6.5k chunk boundaries in the vault. Elsewhere 6–60 tokens, mean 59.
   - **New knob.** `WAX_TRANSCRIPTS_OWNER_PASSWORD` (direct override, mirrors `WAX_TRANSCRIPTS_PASSWORD`)
     must join `ENV_SPECS` as redacted in step 16.
   - **Tests.** E.2 case 12 uses `docker pause`/`unpause` rather than a stop: with `--rm` a stop deletes
     the container and its random host port. A paused server fails `connect_timeout=2` exactly like a
     down one. Case 10's EXPLAIN sets `enable_seqscan = off` because the planner prefers a seq scan at
     ~30 rows. pathlib's `glob("*.md")` matches dotfiles, so reconcile excludes `.`-prefixed names
     (`.wax-ep-*.md`) explicitly.
5. **Wiring findings (2026-10-28, stage F 13–19 + 21 + step 1 written, not applied).**
   - **Hooks.** `_capture()` wraps the `is_document` check in its own `try` as well, so even a broken
     documents table cannot reach a pass. The main-path call passes `pre_rename_md=md`; the note text is
     read from `current_md` after `_write_pass_note`. `transcribe_adapter` hoists `processing_s`, which
     also removes a bogus `__import__("time")` "model literal" from `chat_pass_routing_test`'s scan: the
     suite reports 409 subtests where the baseline had 410, with no assertion lost.
   - **Test isolation (new knob).** Every `passes.run` now captures, and config falls back to the
     INSTALLED unit, which will carry the production DSN. Under `/usr/bin/python3` (it has psycopg) a
     fixture could then be projected into the real append-only store. `WAX_TRANSCRIPTS_UNIT_FILE`
     overrides the unit path, and `tests/conftest.py` sets it (inherited by `bin/wax` subprocesses) to a
     nonexistent path, patches `config.UNIT_PATH` the same way, and drops inherited DSN/password vars.
     Listed in `ENV_SPECS`.
   - **Unit placeholders.** The committed waxd unit carries the D.9 lines with
     `op://DeLoSecrets/REPLACE_WITH_{APP,OWNER}_ITEM_UUID/password` until the 1Password items exist
     (none did on 2026-10-28). `db._password` refuses a placeholder reference with a provisioning hint
     instead of a failing `op read` on every tick. `deploy/provision-transcriptions --apply` fills both
     placeholders in the template; the unit still has to be committed and installed.
   - **Provisioning.** Passwords reach psql as `\set` variables inside the stdin script (not `-v` on argv,
     which `ps` shows), and the session sets `log_statement = 'none'` and `log_min_error_statement =
     'panic'` first so no `CREATE/ALTER ROLE … PASSWORD` can reach the server log. "A second --apply
     changes nothing" is implemented by testing each role's vault password over TCP first; only a role
     that does not authenticate gets `ALTER ROLE`. Item passwords are `secrets.token_urlsafe(32)`, so the
     items' concealed `dsn` field (which, unlike the unit, carries the password) needs no escaping. The
     dry run exercises `op item create --dry-run` with the real template. Two items with one title are
     refused, never guessed.
   - **Status.** `tick()` writes `{"state": "unreachable", "error": …}` to `transcriptions.last_tick`
     when it cannot connect, and `health.snapshot()` then reads `lagging` even with an empty queue, so a
     wrong DSN is not green between passes. With a DSN configured it also reads `lagging` when no tick
     was ever recorded, when the last tick is older than max(600 s, 10 × tick), or when waxd's last tick
     said `disabled` (the unit gained a DSN after waxd started: restart it). It stays `lagging`, not
     `error` (status exit 2), until the queue passes `WAX_TRANSCRIPTS_LAG_FAIL_S`; doctor's
     reachability probe FAILs immediately, with a provisioning fix when the unit still has placeholders.
   - **CLI.** An unknown `show`/`history` ref (`LookupError`) and a bad `--since` (`ValueError`) print
     `error: …` and exit 2 like `StoreUnavailable`; `show --raw` writes the exact bytes with no newline.
     `WAX_S3_ENDPOINT=https://s3.delo.sh` (equal to `mc alias delo`) in the unit also stops
     `finalize.audio_url` shelling out to `mc`.
6. **Live deploy (2026-10-28, F step 1 applied, F.1 runbook run on the host; HeyMa `48e3571`..).**
   - **Provisioned.** `provision-transcriptions --apply` created the DeLoSecrets DATABASE items
     Owner `xgnmsebepsybgd627rtudh2iuq`, App `dncjuqof2przgk6me6ct6f2xma`, RO
     `jdt6hd7awwzxd376agaszc7z3u`, the three LOGIN roles (no superuser/createdb), the database (owner
     `transcriptions_owner`, CONNECT for app/ro only) and vector 0.8.0 + pg_trgm 1.6. All three roles
     log in over TCP with their vault passwords; the server log holds no `CREATE/ALTER ROLE` text. A
     second `--apply` reused all three items, found every role authenticating and re-issued only the
     idempotent grants: no change. The unit carries the App and Owner UUIDs.
   - **Before the unit is installed** the CLI can already reach the store with
     `WAX_TRANSCRIPTS_UNIT_FILE=<repo>/components/wax/deploy/systemd/user/waxd.service`: config reads
     only `Environment=` lines, and the template's `@…@` lines have no `=` and are ignored. That is
     how `migrate` (2 files, re-run 0), the backfill and the embed ran before waxd was restarted.
   - **Backfill numbers equal the G.4 rehearsal.** Dry run 7.8 s, real run 9.9 s: 862 identities and
     862 `backfill` versions, 6 aliases, 11 ledger-only identities, 1,161 outbox + 18 ledger pass runs
     (outbox completed 1,029 / failed 119 / skipped 13; ledger completed 2 / skipped 16), 0 errors;
     all 862 versions restore byte-for-byte. `identities_with_wax_id` is **462**, not F.1's 449 + 11:
     the `test`-origin fixtures `a-first.ogg.md` and `b-second.ogg.md` carry a `wax-item-id` with no
     ledger row. Not drift.
   - **Embedding.** Foreground `wax transcripts embed`: 935 Ollama calls, 7,463 inputs (7,156 distinct
     chunk texts for 7,309 chunks, 307 distinct doc inputs for 315 doc vectors), 0 deferred, 731 s
     (≈10 inputs/s; F.1 said 11–13 min). Nothing was transcribed meanwhile. `ANALYZE` as owner works
     but prints harmless "permission denied to analyze pg_authid…" warnings for shared catalogs.
     Size afterwards: 253 MB (`chunk_vector_current` 113 MB incl. its 56 MB HNSW, `embedding_cache`
     41 MB). A `pg_dump -Fc` of it is 141 MB and takes 13 s.
   - **C.5 is wrong at this size: the unfiltered vector leg is an exact scan, not HNSW.** pgvector
     prices an HNSW scan's startup in proportion to `hnsw.ef_search`. At 7,309 chunks the canonical
     ANN `EXPLAIN` at the default ef_search (40) names `chunk_vector_current_hnsw`, but under
     `tx.search`'s own `ef_search = 200` an exact seq scan + top-N heapsort (cost 7,716) beats the
     index (7,226 startup + per-row) for `LIMIT 200`; auto_explain of a live `tx.search` shows that
     Seq Scan at 10.9 ms. It is exact and fast, and flips to HNSW by itself as the table grows. The
     doctor probe still FAILs only on an unusable index; its detail now says which path search takes.
   - **The keyword leg dominates common-word queries.** OR-semantics over "phone quality time with
     the kids" matches 3,127 of 7,309 chunks, so the planner seq-scans `chunk_current` and ranks
     every match (≈22 ms). The SQL function measures 9–35 ms in-process, not C.5's 10–20 ms.
   - **Measured latency (warm Ollama).** In-process, one connection: a new query (embedding miss)
     83–94 ms, a cached query 17.7–35.5 ms median, `--lexical` 8.6–26 ms. CLI end to end
     (`wax transcripts search`, process start + `op read` + connect): 203–292 ms, not 0.5–1 s; the
     service-account `op read` takes ≈60–70 ms here.
   - **Live pass.** `wax ep run frontmatter-stamp 4c9f2d894fde1cc5` minted v2 (`frontmatter-stamp`,
     changed duration-seconds/engine-model/word-count) through the CLI hook in the same second, with
     `pass_run.outcome = version`; chunk ids unchanged, `embedding_cache` 7,464 → 7,464, and search
     ranks the note #1 in all four legs at `version_no = 2`.
   - **waxd.** Restarted at 07:09:58 EDT while stream, inbox and queue were idle; `/proc/<pid>/environ`
     carries the five store variables and `OP_SERVICE_ACCOUNT_TOKEN`; `transcriptions.last_tick`
     advances every 15 s with `state: ok`; no `transcriptions_error`. `wax doctor`: 43/43 pass.
   - **A unit test dialed the real cluster.** `HealthSnapshotTest` used
     `postgresql://x@localhost/transcriptions` and recorded a pass, so under `/usr/bin/python3` it
     really connected (the server log had `password authentication failed for user "x"` at every pg
     test run). It now uses `127.0.0.1:1`.
   - **Backups.** `postgres-nightly-dump.sh` (`/etc/cron.daily/db-backup`) dumps every
     `NOT datistemplate AND datallowconn` database as the postgres superuser over the socket, and that
     list now includes `transcriptions` (the database-level REVOKE does not bind a superuser), so the
     first `transcriptions_YYYYMMDD.dump` lands with the next cron.daily run. Since DeLoContainers
     `b9edbee` (2026-10-28) it is in `REQUIRED_DBS`: a night where it is missing or dumps zero objects
     fails and alerts ntfy `infra`.
7. **Adversarial review (2026-10-28, after the live deploy; diff `d6e53d3..`, live store and GPU).**
   - **The resident embedder starved whisper (fixed).** Ollama keeps `qwen3-embedding:8b` resident for
     `keep_alive` after its last call, and D.9's 30m did that after every sweep and every search. Measured at
     07:28 EDT on the RTX 3090: 20,712 of 24,576 MiB used, the Ollama runner holding 5,296 MiB, **3,864 MiB
     free**. `scripts/transcribe.py` needs `TRANSCRIBE_MIN_FREE_VRAM_MB` = 5,000 for large-v3, waits 30 s, then
     transcribes on CPU int8; Sortformer diarization requires CUDA. The `inbox.active_item` gate (D.5) only
     stopped NEW embeds. `POST /api/generate {"model": "qwen3-embedding:8b", "keep_alive": 0}` answers
     `done_reason: unload` (2 ms; a 0.5 ms no-op when nothing is loaded, it never loads the model) and took
     free VRAM to 8,712 MiB. Now: waxd's thread polls the gate every second between ticks and unloads once
     per busy period (`transcriptions.yield_gpu()`); `tick()` unloads when it finds the gate busy; the sweep
     re-asks the gate before every batch (`stopped: gpu_busy`); `WAX_EMBED_KEEP_ALIVE` defaults to **2m**
     (warm across 15 s ticks and a run of searches; a cold load costs 2.4 s). `embedding_model.request_options`
     in `0001_core` still says 30m: informational only, and an applied migration is immutable.
   - **Query embeddings skipped the digest pin (fixed).** `embed.query_vector` embedded a cache miss without
     `check_digest()`, so a re-tagged model would put the query in another space than every stored vector
     and cache that vector under the active `model_id` for good. It now checks first (one ~1 ms local GET on a
     miss only); a mismatch is `EmbedError(digest_mismatch)`, search answers `mode: lexical` with `degraded`,
     and nothing is cached.
   - **Reconcile decided outside the item flock (fixed).** `sweep()` read the note, and took the pending set
     at sweep start, BEFORE `try_item_lock`. A pass finishing in between left either its own snapshot queued
     (reconcile then minted the same content as `reconcile` ahead of it) or projected (reconcile then minted
     the OLDER text it had read: the head rolled back until the next sweep, and append-only history kept two
     spurious versions). Under the lock it now re-checks `outbox.has_pending(item)` and re-stats, re-reading
     the note if it changed (and skipping if its `wax-item-id` changed).
   - **60 s statements under the item flock (fixed).** The drain, reconcile and `wax transcripts sync` share
     a 60 s `statement_timeout` connection while holding an item's flock, which a pass blocks on. Every
     snapshot transaction now starts with `store.bound_statements()` (`set_config('statement_timeout', '5000',
     true)`, transaction-local), so a pass never waits behind a statement longer than D.9's 5 s.
   - **Verified, unchanged.** No psycopg import at module load: importing `wax.transcriptions`, `passes`,
     `transcribe_adapter` and `ledger` under mise Python 3.14 leaves `psycopg` out of `sys.modules`. Every
     `_capture` call-site argument is already bound on its path. Latest-only holds by construction (trigger
     pointer under `FOR UPDATE`, deferred FKs, chunk swap and vector fill in the version's transaction,
     per-item flock + seq order, `UNIQUE (transcript_id, idempotency_key)` re-delivery). Live, the
     frontmatter-stamp v2 of `4c9f2d894fde1cc5` kept chunk ids 7442-7453 with their 07:09:20 vectors and no
     `embedding_cache` row is newer than 07:09:32 except 5 queries. G.1 live: 0 chunks and 0 doc vectors
     belong to `test` identities. Metadata: over all 868 notes every top-level frontmatter key maps to a
     column, a child table or `vault_extra` (0 lossy coercions; 11 `wax.passes.<slug>` keys all exploded);
     10 diverse notes (wax with passes, hermes-stt, legacy clips with labels/obsidian-note-status/
     last_triage_pass/triage-pass/hitl-reason, classification, `modified`, no frontmatter) match their head
     row column for column, `frontmatter` jsonb, pass states and tags: 0 diffs. Secrets: the diff, unit,
     waxd environ, journal and server log carry only `op://` references (0 `CREATE/ALTER ROLE … PASSWORD`
     lines); test containers use a random `secrets.token_hex` superuser password.
   - **Plans (auto_explain, live, 7,309 chunks).** Unfiltered hybrid 36-41 ms: vector leg exact seq scan +
     top-N (10 ms, G.6), chunk FTS seq scan for the common-word query (3,127 matches, 20 ms), doc legs seq
     scans of 315/870 rows. Filtered small (`--since 2026-10-01`, 1,289 chunks): exact path via
     `chunk_vector_current_transcript` + `BitmapAnd` of `chunk_current_tsv` GIN, 13 ms. Filtered large
     (`--origin wax`, 6,067 chunks > `p_exact_below`): ANN path, still a seq scan at this size, 36 ms.
     Lexical rare word: `chunk_current_tsv`, `transcript_current_doc_tsv` and `transcript_current_title_trgm`
     all used, 2 ms. HNSW is unused today and the plans are cheaper without it.
   - **Residual, not fixed.** A server that hangs AFTER accepting the connection (not a lock wait) would hold
     a pass: `statement_timeout` is server-side and psycopg has no client read timeout. `transcript_snapshots`
     keeps every captured `note_text` after projection (~25 KB a snapshot; the 1 Hz `backlog()` stays ~1 ms at
     3,000 rows because a NULL `last_error` is read from the record header): prune projected rows' text if
     `wax.db` growth matters. The embed fill can hit a transient FK error if a chunk swap commits mid-batch;
     the next tick retries. Doctor reports `rejected` snapshots in its detail but still PASSes.
   - **Live after the fix (HeyMa `ee504b3`).** waxd restarted at 07:44:03 EDT with stream, inbox and queue
     idle; `transcriptions.last_tick` advances every 15 s (`ok`), 0 `transcriptions_error`; `wax status`
     `transcripts ok (queued 0, embed pending 0)`; `yield_gpu()` → True with `/api/ps` empty. A cached CLI
     search answers in 0.20 s, a new one in 1.49 s (cold load, digest checked) and Ollama then reports the
     model expiring 2 min later, not 30.
8. **Skipped passes are pass history only (2026-10-28, operator decision; supersedes B.0 "What a version is").**
   A version is minted only by transcription, a *completed* pass, backfill or reconcile. A `when`/`skip_when`
   skip and a pass-reported `state: skipped` are captured as `kind='pass_run'` (`passes.py` both sites) and
   project to `tx.pass_run` with `outcome='not_applied'`, `state='skipped'`, `version_id` NULL.
   `store.apply_snapshot` also routes any queued `version` snapshot whose pass state is `skipped` to pass
   history, so pre-change SQLite queue rows cannot mint one. If a pass-reported skip did edit the note, the
   next completed pass or the reconcile sweep (`reconcile:` key) versions that content. Migration
   `0003_skips_are_history.sql` removed the versions skipped passes had already minted live (one: a
   diarization skip on a monolog, item `3c697cbb47e14690`), repointed heads, renumbered each affected
   transcript's `version_no` contiguously and re-enabled the immutability trigger after firing the deferred
   head FKs. PG tests 16–17 pin both behaviours.
