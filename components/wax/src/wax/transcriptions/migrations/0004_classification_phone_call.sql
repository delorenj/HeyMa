-- Four-way classification (docs/HOLOCENE-TRANSCRIPTS-TAB.md A.1, A.4 row 1): `phone_call` joins
-- monolog | meeting | other. Must land BEFORE any pass may write `phone_call`: until then
-- extract.py and passes.py reject it, and after it they share wax.frontmatter.CLASSIFICATIONS.
-- tx.transcript_current.classification has no CHECK and tx.search takes a free text[], so neither
-- changes. ADD CONSTRAINT validates existing rows by reading them; the append-only trigger only
-- fires on UPDATE/DELETE, so it is untouched.
SET LOCAL search_path = tx, public;

ALTER TABLE tx.transcript_version DROP CONSTRAINT transcript_version_classification_check;
ALTER TABLE tx.transcript_version ADD CONSTRAINT transcript_version_classification_check
    CHECK (classification IN ('monolog', 'meeting', 'phone_call', 'other'));
