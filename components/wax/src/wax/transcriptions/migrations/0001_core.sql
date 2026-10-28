-- Wax transcriptions store: core schema (design B.2). Applied as transcriptions_owner by
-- wax.transcriptions.migrate, one transaction per file. Never edit an applied migration:
-- the runner refuses a changed sha256; add a new numbered file instead.
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
