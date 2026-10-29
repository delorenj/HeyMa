-- Harvest ticket links (docs/HOLOCENE-TRANSCRIPTS-TAB.md B.3-B.5): `px harvest` artifacts
-- (kind px.harvest.actionitems, version 2) ingested by `wax transcripts link-harvest` and the
-- sync/tick backstop over WAX_HARVEST_DIR, as transcriptions_app. One run per artifact
-- harvest_id, its items, and each item's per-board publish block. Dry-run artifacts and
-- `--here` test publishes are kept for diagnostics and excluded by tx.transcript_tickets.
-- Named harvest_* because tx.version_ticket is the unrelated `ticketable` intake path.
SET LOCAL search_path = tx, public;

CREATE TABLE tx.harvest_run (
    harvest_id        text PRIMARY KEY,                 -- artifact.harvest_id
    transcript_id     bigint REFERENCES tx.transcript,  -- NULL = unmatched or ambiguous (kept, hidden from the UI)
    match_method      text CHECK (match_method IN ('wax_item_id', 'body_sha256')),
    status            text NOT NULL CHECK (status IN ('complete', 'dry-run')),
    document_path     text,
    body_sha256       text,
    wax_item_id       text,
    s3_uri            text,
    artifact_path     text NOT NULL,
    artifact_sha256   text NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),  -- unchanged file = no-op
    artifact_bytes    bigint,                           -- with artifact_mtime_ns: the scan stats, never re-hashes
    artifact_mtime_ns bigint,
    item_count        integer NOT NULL,
    clear_count       integer NOT NULL,
    held_count        integer NOT NULL,
    created_at        timestamptz NOT NULL,             -- artifact.created_at
    updated_at        timestamptz NOT NULL,             -- artifact.updated_at
    ingested_at       timestamptz NOT NULL DEFAULT now(),
    CHECK ((transcript_id IS NULL) = (match_method IS NULL))
);
CREATE INDEX harvest_run_transcript ON tx.harvest_run (transcript_id);
CREATE INDEX harvest_run_body       ON tx.harvest_run (body_sha256);
CREATE INDEX harvest_run_artifact   ON tx.harvest_run (artifact_path);

CREATE TABLE tx.harvest_item (
    harvest_id  text     NOT NULL REFERENCES tx.harvest_run ON DELETE CASCADE,
    item_id     text     NOT NULL,                      -- 'h-<12 hex>'
    ordinal     smallint NOT NULL,
    title       text     NOT NULL,
    category    text,
    status      text CHECK (status IN ('firm', 'tentative')),
    gate_state  text     NOT NULL CHECK (gate_state IN ('clear', 'held')),
    project_id  text,                                   -- override.project, else classification.project
    group_kind  text,                                   -- the schema group rule: board | project | general
    quote       text,                                   -- first evidence span, truncated to 400 chars
    PRIMARY KEY (harvest_id, item_id)
);

CREATE TABLE tx.harvest_ticket (
    harvest_id     text NOT NULL,
    item_id        text NOT NULL,
    provider       text NOT NULL,                       -- plane | trello
    board_id       text NOT NULL,
    publish_status text NOT NULL CHECK (publish_status IN
                       ('unpublished', 'sent', 'published', 'adopted', 'blocked', 'failed')),
    ticket_ref     text,                                -- 'PX-123'
    ticket_url     text,
    project_id     text,                                -- publish.project
    lane           text,
    published_at   timestamptz,                         -- publish.at of a `published` block
    is_test        boolean NOT NULL DEFAULT false,      -- publish.here
    PRIMARY KEY (harvest_id, item_id, provider, board_id),
    FOREIGN KEY (harvest_id, item_id) REFERENCES tx.harvest_item ON DELETE CASCADE
);
CREATE INDEX harvest_ticket_ref ON tx.harvest_ticket (ticket_ref);

-- One row per item and real (non-test) ticket; an item with no real ticket keeps one row with NULL
-- ticket columns (the test exclusion sits in the join, so a `--here`-only item reads "not filed"
-- instead of vanishing).
CREATE VIEW tx.transcript_tickets AS
SELECT r.transcript_id, r.harvest_id, r.created_at AS harvested_at,
       i.item_id, i.ordinal, i.title, i.category, i.status AS item_status, i.gate_state,
       i.quote, t.provider, t.board_id, t.publish_status, t.ticket_ref, t.ticket_url,
       t.project_id, t.lane, t.published_at
  FROM tx.harvest_run r
  JOIN tx.harvest_item i ON i.harvest_id = r.harvest_id
  LEFT JOIN tx.harvest_ticket t ON t.harvest_id = i.harvest_id AND t.item_id = i.item_id AND NOT t.is_test
 WHERE r.transcript_id IS NOT NULL
   AND r.status = 'complete';

-- The default privileges from 0001 grant SELECT to the RO role on new tables; repeated here for the
-- view and because the app role (the writer) has no default grant.
GRANT SELECT ON tx.harvest_run, tx.harvest_item, tx.harvest_ticket, tx.transcript_tickets
      TO transcriptions_app, transcriptions_ro;
GRANT INSERT, UPDATE, DELETE ON tx.harvest_run, tx.harvest_item, tx.harvest_ticket TO transcriptions_app;
