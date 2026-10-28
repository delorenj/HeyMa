-- Wax transcriptions store: hybrid search over head versions (design C.4, amended by G.1).
-- Applied as transcriptions_owner by wax.transcriptions.migrate, one transaction per file.
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
                   AND EXISTS (SELECT 1 FROM tx.transcript st         -- G.1: test notes never surface
                                WHERE st.transcript_id = d.transcript_id AND st.searchable)
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
                   AND EXISTS (SELECT 1 FROM tx.transcript st         -- G.1: test notes never surface
                                WHERE st.transcript_id = c.transcript_id AND st.searchable)
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
