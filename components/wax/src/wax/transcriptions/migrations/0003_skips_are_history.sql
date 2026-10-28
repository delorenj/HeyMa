-- Skipped passes are pass history (tx.pass_run) only, never a version (design G.8).
-- Removes versions minted by skipped passes before that rule, renumbers each
-- affected transcript's versions contiguously and repoints the head. A skip
-- never changes content (wax.passes is excluded from content_sha256), so the
-- previous version is an exact stand-in for the head's derived columns.
SET LOCAL search_path = tx, public;

CREATE TEMP TABLE skip_version ON COMMIT DROP AS
SELECT v.version_id, v.transcript_id,
       (SELECT p.version_id FROM tx.transcript_version p
         WHERE p.transcript_id = v.transcript_id AND p.version_no < v.version_no
           AND p.pass_state IS DISTINCT FROM 'skipped'
         ORDER BY p.version_no DESC LIMIT 1) AS stand_in
  FROM tx.transcript_version v
 WHERE v.pass_state = 'skipped';

ALTER TABLE tx.transcript_version DISABLE TRIGGER transcript_version_immutable;

UPDATE tx.pass_run r SET version_id = NULL, outcome = 'not_applied'
  FROM skip_version s WHERE r.version_id = s.version_id;
UPDATE tx.transcript_path p SET first_version_id = s.stand_in
  FROM skip_version s WHERE p.first_version_id = s.version_id;
DELETE FROM tx.version_tag       WHERE version_id IN (SELECT version_id FROM skip_version);
DELETE FROM tx.version_project   WHERE version_id IN (SELECT version_id FROM skip_version);
DELETE FROM tx.version_ticket    WHERE version_id IN (SELECT version_id FROM skip_version);
DELETE FROM tx.version_pass_state WHERE version_id IN (SELECT version_id FROM skip_version);

-- Repoint heads that were a skip (deferred FKs are checked at COMMIT).
UPDATE tx.transcript t SET current_version_id = s.stand_in, updated_at = now()
  FROM skip_version s WHERE t.current_version_id = s.version_id;

DELETE FROM tx.transcript_version WHERE version_id IN (SELECT version_id FROM skip_version);

-- Renumber contiguously; negate first so UNIQUE (transcript_id, version_no) never collides.
UPDATE tx.transcript_version SET version_no = -version_no
 WHERE transcript_id IN (SELECT transcript_id FROM skip_version);
UPDATE tx.transcript_version v SET version_no = n.rn
  FROM (SELECT version_id, row_number() OVER (PARTITION BY transcript_id ORDER BY -version_no) AS rn
          FROM tx.transcript_version
         WHERE transcript_id IN (SELECT transcript_id FROM skip_version)) n
 WHERE v.version_id = n.version_id;

UPDATE tx.transcript_current c
   SET version_id = t.current_version_id, version_no = v.version_no, produced_by = v.produced_by,
       updated_at = now()
  FROM tx.transcript t JOIN tx.transcript_version v ON v.version_id = t.current_version_id
 WHERE c.transcript_id = t.transcript_id
   AND t.transcript_id IN (SELECT transcript_id FROM skip_version);

-- Fire the deferred head FKs now: ALTER TABLE refuses to run with trigger events pending.
SET CONSTRAINTS ALL IMMEDIATE;
ALTER TABLE tx.transcript_version ENABLE TRIGGER transcript_version_immutable;
