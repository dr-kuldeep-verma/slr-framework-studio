"""The `project.slr` SQLite schema.

One database per review. Version 1 covered the protocol, import, record and
deduplication tables; version 2 adds screening. The AI accountability tables
have existed since version 1 so that the integrity rules (every AI decision
logged, every override logged, every claim traceable) are enforceable by the
storage layer rather than bolted on later.

Conventions
-----------
* Timestamps are ISO-8601 UTC strings with a trailing ``Z``.
* JSON-valued columns hold canonical JSON (see :mod:`slr_studio.core.hashing`).
* Nothing is ever hard-deleted. Records are retired with ``active = 0`` so the
  PRISMA counts and the audit trail stay reconstructable.

Adding a version
----------------
Put the new DDL in its own string, append it to ``SCHEMA_SQL`` so fresh
projects get it, and register the same string in ``MIGRATIONS`` so existing
projects are brought forward. Migrations only ever add; a column an older
build wrote is never dropped, because the audit trail refers to it.
"""

from __future__ import annotations

from typing import Any, Callable

#: The `project` table on its own, because a migration has to rebuild it:
#: SQLite can only drop a CHECK constraint by creating the table afresh
#: and copying the rows, and a second hand-maintained copy of these
#: columns would drift from this one the first time a column was added.
PROJECT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS project (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    title           TEXT    NOT NULL,
    objectives      TEXT    NOT NULL DEFAULT '',
    research_questions TEXT NOT NULL DEFAULT '',   -- JSON array
    -- A built-in question framework (PICO, PCC, SPIDER, CIMO and the rest
    -- of core/frameworks.py) or one the reviewer defined in
    -- `framework_definition`. There is no CHECK listing the names: it held
    -- four, and widening it meant rebuilding the table every time another
    -- framework was added. What the constraint was protecting -- that the
    -- name is one the Gap Finder has stages for -- is checked in
    -- Project.update_info against the catalogue and this table together,
    -- which a CHECK cannot do.
    framework_type  TEXT    NOT NULL DEFAULT 'PCC',

    -- How the framework is built, which is a different question from what
    -- the review is about. 'bottom_up' derives the constructs from what the
    -- included studies turned out to measure; 'top_down' starts from
    -- constructs the reviewer commits to in advance and codes the studies
    -- into them. The default is bottom-up because a reviewer who has not
    -- declared any constructs has not committed to any.
    framework_approach TEXT NOT NULL DEFAULT 'bottom_up'
                    CHECK (framework_approach IN ('bottom_up','top_down')),
    -- The constructs a top-down review commits to, one per line, optionally
    -- "Name — the reviewer's own definition". They are written into the
    -- protocol document, so the commitment is hashed and dated: that is
    -- what makes them a priori rather than a story told afterwards.
    a_priori_constructs TEXT NOT NULL DEFAULT '',
    -- Where those constructs came from. A reader cannot judge a deductive
    -- framework without knowing which theory it is deducing from, so the
    -- Methods draft says "constructs drawn from Social Exchange Theory
    -- (Ap, 1992)" rather than reporting a count of constructs from nowhere.
    a_priori_theory TEXT   NOT NULL DEFAULT '',
    a_priori_citation TEXT NOT NULL DEFAULT '',
    inclusion_criteria TEXT NOT NULL DEFAULT '',
    date_from       INTEGER,
    date_to         INTEGER,
    languages       TEXT    NOT NULL DEFAULT '[]', -- JSON array of ISO codes
    contact_email   TEXT,                          -- used for the Unpaywall API
    -- The date the searches were run. Every claim of absence the Gap Finder
    -- makes is qualified against it, so it is the reviewer's to state and is
    -- never inferred from when the software happened to run.
    searched_to     TEXT    NOT NULL DEFAULT '',
    -- Theories the reviewer is working from, and the stimulus attributes they
    -- care about. Section 11 reports which of each was never measured or
    -- varied, which it cannot do from an empty list.
    theories        TEXT    NOT NULL DEFAULT '',
    stimulus_attributes TEXT NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);
"""


BASE_SQL = """
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- meta -----

CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

-- The append-only audit trail. `prev_hash`/`entry_hash` chain the rows so a
-- deletion or edit anywhere in the history is detectable (see audit.verify).
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    stage       TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    actor       TEXT    NOT NULL,              -- 'human:<name>' | 'ai:<model>' | 'system'
    entity      TEXT,                          -- table name
    entity_id   TEXT,
    detail      TEXT,                          -- canonical JSON
    prev_hash   TEXT,
    entry_hash  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_stage ON audit_log(stage, ts);
CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit_log(entity, entity_id);

-- ------------------------------------------------------------ protocol -----

""" + PROJECT_TABLE_SQL + """

-- Exclusion codes are ordered cheapest/most objective first; screening applies
-- them in `position` order and reports the FIRST failing code.
CREATE TABLE IF NOT EXISTS exclusion_code (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL UNIQUE,
    label       TEXT    NOT NULL,
    definition  TEXT    NOT NULL DEFAULT '',
    position    INTEGER NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_exclusion_position ON exclusion_code(position);

-- A locked snapshot of the protocol. Version 1 is the pre-registration.
CREATE TABLE IF NOT EXISTS protocol_version (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version     INTEGER NOT NULL UNIQUE,
    content     TEXT    NOT NULL,     -- the drafted protocol document (markdown)
    content_hash TEXT   NOT NULL,
    locked_at   TEXT    NOT NULL,
    locked_by   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS amendment (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    from_version    INTEGER REFERENCES protocol_version(id),
    to_version      INTEGER REFERENCES protocol_version(id),
    changed_at      TEXT    NOT NULL,
    reason          TEXT    NOT NULL,
    after_data_seen INTEGER NOT NULL,   -- 0/1, decided from the import timeline
    diff            TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS seed_study (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doi         TEXT    NOT NULL UNIQUE,   -- stored normalised
    note        TEXT    NOT NULL DEFAULT '',
    found_record_id INTEGER REFERENCES record(id),
    checked_at  TEXT
);

-- -------------------------------------------------------------- import -----

CREATE TABLE IF NOT EXISTS import_file (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT    NOT NULL,
    original_path   TEXT    NOT NULL,
    file_sha256     TEXT    NOT NULL,
    format          TEXT    NOT NULL,       -- ris | nbib | endnote_xml | csv
    dialect         TEXT    NOT NULL,       -- scopus | wos | ebsco | proquest | pubmed | generic ...
    source_database TEXT    NOT NULL,       -- display name, user-correctable
    n_records       INTEGER NOT NULL DEFAULT 0,
    n_parse_errors  INTEGER NOT NULL DEFAULT 0,
    imported_at     TEXT    NOT NULL,
    notes           TEXT    NOT NULL DEFAULT ''
);
-- Not unique: ImportService refuses a repeat import by default and names the
-- earlier one, but a reviewer who means it can override that. The duplicate
-- records are caught at the deduplication stage either way.
CREATE INDEX IF NOT EXISTS ix_import_sha ON import_file(file_sha256);

-- One row per reference as it arrived. `raw` is never modified: normalisation
-- only ever writes to the derived columns beside it.
CREATE TABLE IF NOT EXISTS record (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    import_file_id  INTEGER NOT NULL REFERENCES import_file(id),
    position_in_file INTEGER NOT NULL,

    title           TEXT    NOT NULL DEFAULT '',
    abstract        TEXT    NOT NULL DEFAULT '',
    authors         TEXT    NOT NULL DEFAULT '[]',  -- JSON array, author order kept
    year            INTEGER,
    journal         TEXT    NOT NULL DEFAULT '',
    volume          TEXT    NOT NULL DEFAULT '',
    issue           TEXT    NOT NULL DEFAULT '',
    pages           TEXT    NOT NULL DEFAULT '',
    doi             TEXT,                            -- normalised, lower-case
    issn            TEXT    NOT NULL DEFAULT '',
    url             TEXT    NOT NULL DEFAULT '',
    keywords        TEXT    NOT NULL DEFAULT '[]',  -- JSON array
    language        TEXT    NOT NULL DEFAULT '',
    doc_type        TEXT    NOT NULL DEFAULT '',
    publisher       TEXT    NOT NULL DEFAULT '',
    accession       TEXT    NOT NULL DEFAULT '',    -- UT / AN / PMID
    source_database TEXT    NOT NULL DEFAULT '',

    title_norm      TEXT    NOT NULL DEFAULT '',    -- punctuation/case stripped
    title_key       TEXT    NOT NULL DEFAULT '',    -- blocking key for dedup
    first_author_norm TEXT  NOT NULL DEFAULT '',

    raw             TEXT    NOT NULL,               -- verbatim source block
    raw_format      TEXT    NOT NULL,
    record_hash     TEXT    NOT NULL,

    has_abstract    INTEGER NOT NULL DEFAULT 0,
    flags           TEXT    NOT NULL DEFAULT '[]',  -- JSON array of strings

    cluster_id      INTEGER,                        -- set by deduplication
    duplicate_of    INTEGER REFERENCES record(id),  -- NULL on the kept record
    active          INTEGER NOT NULL DEFAULT 1,     -- 0 once merged away

    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_record_doi ON record(doi) WHERE doi IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_record_titlekey ON record(title_key);
CREATE INDEX IF NOT EXISTS ix_record_year ON record(year);
CREATE INDEX IF NOT EXISTS ix_record_cluster ON record(cluster_id);
CREATE INDEX IF NOT EXISTS ix_record_active ON record(active);
CREATE INDEX IF NOT EXISTS ix_record_file ON record(import_file_id);

-- Every database a surviving record was seen in, including the ones whose
-- duplicates were merged away. PRISMA asks for per-database counts.
CREATE TABLE IF NOT EXISTS record_source (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id       INTEGER NOT NULL REFERENCES record(id),
    source_database TEXT    NOT NULL,
    import_file_id  INTEGER NOT NULL REFERENCES import_file(id),
    origin_record_id INTEGER NOT NULL REFERENCES record(id),
    accession       TEXT    NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_record_source
    ON record_source(record_id, origin_record_id);
CREATE INDEX IF NOT EXISTS ix_record_source_db ON record_source(source_database);

-- --------------------------------------------------------- deduplication ---

CREATE TABLE IF NOT EXISTS dedup_run (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    fuzzy_threshold REAL    NOT NULL,
    auto_threshold  REAL    NOT NULL,
    n_records       INTEGER NOT NULL DEFAULT 0,
    n_pairs         INTEGER NOT NULL DEFAULT 0,
    n_auto          INTEGER NOT NULL DEFAULT 0,
    n_uncertain     INTEGER NOT NULL DEFAULT 0,
    n_merged        INTEGER NOT NULL DEFAULT 0,
    settings        TEXT    NOT NULL DEFAULT '{}'
);

-- A candidate duplicate pair. `record_a_id < record_b_id` always, so a pair is
-- stored once. Human decisions survive re-runs; automatic ones are recomputed.
CREATE TABLE IF NOT EXISTS dedup_pair (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER REFERENCES dedup_run(id),
    record_a_id     INTEGER NOT NULL REFERENCES record(id),
    record_b_id     INTEGER NOT NULL REFERENCES record(id),
    rule            TEXT    NOT NULL,   -- doi_exact | title_year | fuzzy_title
    score           REAL    NOT NULL,
    status          TEXT    NOT NULL    -- auto_match | uncertain | auto_distinct
                                        -- | human_match | human_distinct
                    CHECK (status IN ('auto_match','uncertain','auto_distinct',
                                      'human_match','human_distinct')),
    evidence        TEXT    NOT NULL DEFAULT '{}',  -- JSON: what the rule saw
    decided_at      TEXT,
    decided_by      TEXT,
    CHECK (record_a_id < record_b_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_dedup_pair
    ON dedup_pair(record_a_id, record_b_id);
CREATE INDEX IF NOT EXISTS ix_dedup_status ON dedup_pair(status);

CREATE TABLE IF NOT EXISTS merge_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER REFERENCES dedup_run(id),
    kept_record_id  INTEGER NOT NULL REFERENCES record(id),
    merged_record_id INTEGER NOT NULL REFERENCES record(id),
    rule            TEXT    NOT NULL,
    score           REAL    NOT NULL,
    decided_by      TEXT    NOT NULL,
    merged_at       TEXT    NOT NULL,
    reverted_at     TEXT,
    detail          TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_merge_kept ON merge_log(kept_record_id);

-- ------------------------------------------------- AI accountability -------
-- Populated from stage 5 onward. Present in schema v1 because integrity rules
-- 2-4 are non-negotiable: no AI call may exist without a row here.

CREATE TABLE IF NOT EXISTS ai_call (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stage           TEXT    NOT NULL,
    task            TEXT    NOT NULL,
    provider        TEXT    NOT NULL,
    model           TEXT    NOT NULL,
    model_version   TEXT    NOT NULL DEFAULT '',
    temperature     REAL    NOT NULL DEFAULT 0.0,
    prompt_hash     TEXT    NOT NULL,
    input_hash      TEXT    NOT NULL,
    output          TEXT    NOT NULL,
    output_hash     TEXT    NOT NULL,
    prompt_tokens   INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL    NOT NULL DEFAULT 0.0,
    cached          INTEGER NOT NULL DEFAULT 0,
    entity          TEXT,
    entity_id       TEXT,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ai_call_stage ON ai_call(stage, task);
CREATE INDEX IF NOT EXISTS ix_ai_call_entity ON ai_call(entity, entity_id);

CREATE TABLE IF NOT EXISTS ai_cache (
    cache_key       TEXT PRIMARY KEY,   -- sha256(provider|model|prompt|input)
    output          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    hits            INTEGER NOT NULL DEFAULT 0
);

-- Every time a human changes what the AI said, here and in audit_log.
CREATE TABLE IF NOT EXISTS human_override (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stage           TEXT    NOT NULL,
    entity          TEXT    NOT NULL,
    entity_id       TEXT    NOT NULL,
    field           TEXT    NOT NULL,
    ai_value        TEXT,
    human_value     TEXT,
    reason          TEXT    NOT NULL DEFAULT '',
    actor           TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_override_entity ON human_override(entity, entity_id);

-- PRISMA counts are derived, never typed in. This view is the single source
-- for the identification block of the flow diagram.
CREATE VIEW IF NOT EXISTS v_prisma_identification AS
SELECT
    (SELECT COUNT(*) FROM record)                                AS n_identified,
    (SELECT COUNT(*) FROM record WHERE active = 0
                                   AND duplicate_of IS NOT NULL) AS n_duplicates_removed,
    (SELECT COUNT(*) FROM record WHERE active = 1)               AS n_screened;

CREATE VIEW IF NOT EXISTS v_records_per_database AS
SELECT source_database,
       COUNT(*) AS n_imported,
       SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END) AS n_after_dedup
FROM record
GROUP BY source_database;
"""

# --------------------------------------------------------------- v2 --------
# Title and abstract screening. Two AI screeners judge each record without
# seeing each other's answer, a third adjudicates disagreements, a
# quality-control pass re-reads a sample of joint exclusions, and a human
# settles whatever is left. Every one of those is a row in the same table,
# distinguished by `reviewer_role`, so the Excel log is one query and the
# agreement statistics never have to join across shapes.

SCREENING_SQL = """
CREATE TABLE IF NOT EXISTS screening_run (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT    NOT NULL
                    CHECK (kind IN ('pilot','full','adjudication','qc')),
    status          TEXT    NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running','paused','finished','failed')),
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    provider        TEXT    NOT NULL DEFAULT '',
    model           TEXT    NOT NULL DEFAULT '',
    model_version   TEXT    NOT NULL DEFAULT '',
    temperature     REAL    NOT NULL DEFAULT 0.0,
    prompt_version  TEXT    NOT NULL DEFAULT '',
    protocol_version INTEGER,          -- which locked protocol was in force
    settings        TEXT    NOT NULL DEFAULT '{}',
    n_records       INTEGER NOT NULL DEFAULT 0,
    n_done          INTEGER NOT NULL DEFAULT 0,
    n_calls         INTEGER NOT NULL DEFAULT 0,
    n_cached        INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL    NOT NULL DEFAULT 0.0,
    note            TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_screening_run_kind ON screening_run(kind, started_at);

-- One row per reviewer per record. `reviewer_role` says which hat; `reviewer`
-- says who or what wore it (a model string, or human:<name>).
CREATE TABLE IF NOT EXISTS screening_decision (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER REFERENCES screening_run(id),
    record_id       INTEGER NOT NULL REFERENCES record(id),
    reviewer_role   TEXT    NOT NULL
                    CHECK (reviewer_role IN ('screener_a','screener_b',
                                             'adjudicator','qc','human','pilot_human')),
    reviewer        TEXT    NOT NULL,
    decision        TEXT    NOT NULL
                    CHECK (decision IN ('include','exclude','unclear')),
    exclusion_code  TEXT    NOT NULL DEFAULT '',
    reason          TEXT    NOT NULL DEFAULT '',
    quote           TEXT    NOT NULL DEFAULT '',
    quote_verified  INTEGER NOT NULL DEFAULT 0,
    ai_call_id      INTEGER REFERENCES ai_call(id),
    created_at      TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_screening_decision
    ON screening_decision(record_id, reviewer_role, IFNULL(run_id, 0));
CREATE INDEX IF NOT EXISTS ix_screening_decision_record
    ON screening_decision(record_id);
CREATE INDEX IF NOT EXISTS ix_screening_decision_run
    ON screening_decision(run_id, reviewer_role);

-- The settled answer for each record: what the PRISMA counts and the RIS
-- export read. Derived by the engine from the decisions above, never typed in.
CREATE TABLE IF NOT EXISTS screening_status (
    record_id       INTEGER PRIMARY KEY REFERENCES record(id),
    decision        TEXT    NOT NULL
                    CHECK (decision IN ('include','exclude','unclear')),
    exclusion_code  TEXT    NOT NULL DEFAULT '',
    -- How the decision was reached. 'unresolved' means the screeners
    -- disagreed and no AI call settled it, so the record is carrying a
    -- provisional status while it waits for a human.
    source          TEXT    NOT NULL
                    CHECK (source IN ('agreement','adjudication','qc','human',
                                      'unresolved')),
    decided_by      TEXT    NOT NULL,
    decided_at      TEXT    NOT NULL,
    needs_review    INTEGER NOT NULL DEFAULT 0,
    review_reason   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_screening_status_decision
    ON screening_status(decision);
CREATE INDEX IF NOT EXISTS ix_screening_status_review
    ON screening_status(needs_review);

-- The review queue itself is defined once, in REVIEW_QUEUE_SQL below, and
-- spliced in here. Every stage adds reasons an item can be queued for, so a
-- second copy of the definition would drift out of step with the rebuild the
-- migrations run.

-- Percentage agreement, Cohen's kappa and PABAK, stored rather than
-- recomputed so the Methods section reports the figure that was on screen
-- when the reviewer decided to proceed.
CREATE TABLE IF NOT EXISTS agreement_report (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER REFERENCES screening_run(id),
    scope           TEXT    NOT NULL,   -- e.g. 'pilot:human_vs_ai', 'full:a_vs_b'
    n               INTEGER NOT NULL,
    n_agree         INTEGER NOT NULL,
    percent_agreement REAL  NOT NULL,
    kappa           REAL,
    pabak           REAL,
    p_observed      REAL,
    p_expected      REAL,
    detail          TEXT    NOT NULL DEFAULT '{}',
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_agreement_run ON agreement_report(run_id);

-- The pilot can be waived, but never silently.
CREATE TABLE IF NOT EXISTS screening_waiver (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scope           TEXT    NOT NULL,
    reason          TEXT    NOT NULL,
    actor           TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);

"""

#: Kept separate so the v3 migration can rebuild them after the queue gains
#: its stage column.
SCREENING_VIEWS_SQL = """
CREATE VIEW IF NOT EXISTS v_prisma_screening AS
SELECT
    (SELECT COUNT(*) FROM record WHERE active = 1) AS n_to_screen,
    (SELECT COUNT(*) FROM screening_status s JOIN record r ON r.id = s.record_id
       WHERE r.active = 1 AND s.decision = 'include')  AS n_included,
    (SELECT COUNT(*) FROM screening_status s JOIN record r ON r.id = s.record_id
       WHERE r.active = 1 AND s.decision = 'exclude')  AS n_excluded,
    (SELECT COUNT(*) FROM screening_status s JOIN record r ON r.id = s.record_id
       WHERE r.active = 1 AND s.decision = 'unclear')  AS n_unclear,
    (SELECT COUNT(*) FROM record r WHERE r.active = 1
       AND NOT EXISTS (SELECT 1 FROM screening_status s WHERE s.record_id = r.id))
                                                       AS n_not_yet_screened,
    (SELECT COUNT(*) FROM review_queue
       WHERE resolved_at IS NULL AND stage = 'screen') AS n_awaiting_review;

-- Exclusions grouped by the first code each record failed, which is the
-- "records excluded, with reasons" box of the PRISMA diagram.
CREATE VIEW IF NOT EXISTS v_exclusion_reasons AS
SELECT s.exclusion_code,
       COALESCE(c.label, s.exclusion_code) AS label,
       COUNT(*) AS n
FROM screening_status s
JOIN record r ON r.id = s.record_id AND r.active = 1
LEFT JOIN exclusion_code c ON c.code = s.exclusion_code
WHERE s.decision = 'exclude'
GROUP BY s.exclusion_code, label;
"""

# The views and the review queue are spliced on further down, once the queue's
# single definition has been declared.

# --------------------------------------------------------------- v3 --------
# Full-text retrieval and eligibility.
#
# The extracted text of every page is stored. That is what makes a quotation
# checkable years later: the assessment says "page 7", and the words on page 7
# as the app read them are still here to compare against, whether or not the
# PDF is.

FULLTEXT_SQL = """
-- Where an open-access copy was found. Links only: the app never downloads
-- from a paywalled host, uses institutional credentials, solves a CAPTCHA or
-- scrapes. The reviewer follows the link and saves the file themselves.
CREATE TABLE IF NOT EXISTS oa_location (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id       INTEGER NOT NULL REFERENCES record(id),
    source          TEXT    NOT NULL DEFAULT 'unpaywall',
    url             TEXT    NOT NULL,
    host_type       TEXT    NOT NULL DEFAULT '',   -- publisher | repository
    version         TEXT    NOT NULL DEFAULT '',   -- publishedVersion, etc.
    license         TEXT    NOT NULL DEFAULT '',
    is_best         INTEGER NOT NULL DEFAULT 0,
    found_at        TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_oa_location ON oa_location(record_id, url);

-- One row per DOI looked up, whether or not anything was found, so a second
-- pass does not re-ask for answers already given.
CREATE TABLE IF NOT EXISTS oa_lookup (
    record_id       INTEGER PRIMARY KEY REFERENCES record(id),
    doi             TEXT    NOT NULL DEFAULT '',
    queried_at      TEXT    NOT NULL,
    is_oa           INTEGER NOT NULL DEFAULT 0,
    oa_status       TEXT    NOT NULL DEFAULT '',
    n_locations     INTEGER NOT NULL DEFAULT 0,
    error           TEXT    NOT NULL DEFAULT ''
);

-- The report itself: what was retrieved for a record, or why it was not.
CREATE TABLE IF NOT EXISTS report (
    record_id       INTEGER PRIMARY KEY REFERENCES record(id),
    status          TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','retrieved','not_retrieved',
                                      'abstract_only')),
    reason          TEXT    NOT NULL DEFAULT '',
    filename        TEXT    NOT NULL DEFAULT '',
    stored_path     TEXT    NOT NULL DEFAULT '',
    file_sha256     TEXT    NOT NULL DEFAULT '',
    n_pages         INTEGER NOT NULL DEFAULT 0,
    n_pages_ocr     INTEGER NOT NULL DEFAULT 0,
    has_text_layer  INTEGER NOT NULL DEFAULT 0,
    -- How the file was tied to this record, and how sure that was.
    matched_by      TEXT    NOT NULL DEFAULT 'none'
                    CHECK (matched_by IN ('none','doi','title','manual')),
    match_score     REAL    NOT NULL DEFAULT 0.0,
    added_at        TEXT,
    extracted_at    TEXT,
    note            TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_report_status ON report(status);

-- The text of each page, as the app read it.
CREATE TABLE IF NOT EXISTS report_page (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id       INTEGER NOT NULL REFERENCES record(id),
    page_no         INTEGER NOT NULL,              -- 1-based, as printed
    text            TEXT    NOT NULL DEFAULT '',
    n_chars         INTEGER NOT NULL DEFAULT 0,
    ocr             INTEGER NOT NULL DEFAULT 0     -- read by OCR, not a text layer
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_report_page ON report_page(record_id, page_no);

-- PDFs dropped in that matched no record, waiting to be matched by hand.
CREATE TABLE IF NOT EXISTS unmatched_pdf (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT    NOT NULL,
    stored_path     TEXT    NOT NULL,
    file_sha256     TEXT    NOT NULL,
    detected_doi    TEXT    NOT NULL DEFAULT '',
    detected_title  TEXT    NOT NULL DEFAULT '',
    best_guess_record_id INTEGER REFERENCES record(id),
    best_guess_score REAL   NOT NULL DEFAULT 0.0,
    added_at        TEXT    NOT NULL,
    resolved_at     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_unmatched_pdf ON unmatched_pdf(file_sha256);

CREATE TABLE IF NOT EXISTS fulltext_run (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT    NOT NULL DEFAULT 'fulltext'
                    CHECK (kind IN ('fulltext','fulltext_qc')),
    status          TEXT    NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running','paused','finished','failed')),
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    provider        TEXT    NOT NULL DEFAULT '',
    model           TEXT    NOT NULL DEFAULT '',
    temperature     REAL    NOT NULL DEFAULT 0.0,
    prompt_version  TEXT    NOT NULL DEFAULT '',
    protocol_version INTEGER,
    settings        TEXT    NOT NULL DEFAULT '{}',
    n_records       INTEGER NOT NULL DEFAULT 0,
    n_done          INTEGER NOT NULL DEFAULT 0,
    n_calls         INTEGER NOT NULL DEFAULT 0,
    n_cached        INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL    NOT NULL DEFAULT 0.0,
    note            TEXT    NOT NULL DEFAULT ''
);

-- An eligibility assessment. `quote_page` is the page the assessment cites,
-- and `quote_verified` says whether those words are actually on it.
CREATE TABLE IF NOT EXISTS fulltext_decision (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER REFERENCES fulltext_run(id),
    record_id       INTEGER NOT NULL REFERENCES record(id),
    reviewer_role   TEXT    NOT NULL
                    CHECK (reviewer_role IN ('assessor','human')),
    reviewer        TEXT    NOT NULL,
    decision        TEXT    NOT NULL
                    CHECK (decision IN ('include','exclude','unclear')),
    exclusion_code  TEXT    NOT NULL DEFAULT '',
    reason          TEXT    NOT NULL DEFAULT '',
    quote           TEXT    NOT NULL DEFAULT '',
    quote_page      INTEGER,
    quote_verified  INTEGER NOT NULL DEFAULT 0,
    quote_found_on  INTEGER,                       -- the page it is really on
    verify_detail   TEXT    NOT NULL DEFAULT '',
    ai_call_id      INTEGER REFERENCES ai_call(id),
    created_at      TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_fulltext_decision
    ON fulltext_decision(record_id, reviewer_role, IFNULL(run_id, 0));
CREATE INDEX IF NOT EXISTS ix_fulltext_decision_record
    ON fulltext_decision(record_id);

CREATE TABLE IF NOT EXISTS fulltext_status (
    record_id       INTEGER PRIMARY KEY REFERENCES record(id),
    decision        TEXT    NOT NULL
                    CHECK (decision IN ('include','exclude','unclear')),
    exclusion_code  TEXT    NOT NULL DEFAULT '',
    source          TEXT    NOT NULL
                    CHECK (source IN ('ai','human','unresolved')),
    decided_by      TEXT    NOT NULL,
    decided_at      TEXT    NOT NULL,
    needs_review    INTEGER NOT NULL DEFAULT 0,
    review_reason   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_fulltext_status_decision ON fulltext_status(decision);

-- The retrieval and eligibility blocks of the PRISMA flow diagram.
CREATE VIEW IF NOT EXISTS v_prisma_fulltext AS
SELECT
    (SELECT COUNT(*) FROM screening_status s JOIN record r ON r.id = s.record_id
       WHERE r.active = 1 AND s.decision IN ('include','unclear'))  AS n_sought,
    (SELECT COUNT(*) FROM report WHERE status = 'retrieved')        AS n_retrieved,
    (SELECT COUNT(*) FROM report WHERE status = 'not_retrieved')    AS n_not_retrieved,
    (SELECT COUNT(*) FROM report WHERE status = 'abstract_only')    AS n_abstract_only,
    (SELECT COUNT(*) FROM fulltext_status)                          AS n_assessed,
    (SELECT COUNT(*) FROM fulltext_status WHERE decision = 'include') AS n_included,
    (SELECT COUNT(*) FROM fulltext_status WHERE decision = 'exclude') AS n_excluded,
    (SELECT COUNT(*) FROM fulltext_status WHERE decision = 'unclear') AS n_unclear,
    (SELECT COUNT(*) FROM review_queue
       WHERE resolved_at IS NULL AND stage = 'fulltext')            AS n_awaiting_review,
    (SELECT COUNT(*) FROM unmatched_pdf WHERE resolved_at IS NULL)  AS n_unmatched_pdfs;

CREATE VIEW IF NOT EXISTS v_fulltext_exclusion_reasons AS
SELECT f.exclusion_code,
       COALESCE(c.label, f.exclusion_code) AS label,
       COUNT(*) AS n
FROM fulltext_status f
LEFT JOIN exclusion_code c ON c.code = f.exclusion_code
WHERE f.decision = 'exclude'
GROUP BY f.exclusion_code, label;
"""

#: The v3 shape of the queue, used both by a fresh project and by the rebuild
#: below. Kept as one definition so the two cannot drift apart.
REVIEW_QUEUE_SQL = """
CREATE TABLE {name} ( -- review_queue shape v5
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id       INTEGER NOT NULL REFERENCES record(id),
    reason          TEXT    NOT NULL
                    CHECK (reason IN ('conflict','unclear','qc_rescue',
                                      'random_audit','quote_failed',
                                      'confirm_inclusion','direction_mismatch',
                                      'design_rule','coder_disagreement',
                                      'nothing_extracted','contestable_mapping',
                                      'unmapped_label')),
    detail          TEXT    NOT NULL DEFAULT '',
    run_id          INTEGER REFERENCES screening_run(id),
    stage           TEXT    NOT NULL DEFAULT 'screen',
    created_at      TEXT    NOT NULL,
    resolved_at     TEXT,
    resolution      TEXT    NOT NULL DEFAULT ''
);
"""

#: Bumped whenever REVIEW_QUEUE_SQL changes. The marker is written into the
#: stored CREATE TABLE text, so a migration can tell at a glance whether a
#: project's queue is already the current shape.
REVIEW_QUEUE_SHAPE = "review_queue shape v5"

#: The table plus its indexes, as a fresh project creates them.
REVIEW_QUEUE_FULL_SQL = (
    REVIEW_QUEUE_SQL.replace("CREATE TABLE {name}", "CREATE TABLE IF NOT EXISTS {name}")
    .format(name="review_queue")
    + """
-- One open item per record, reason *and stage*. Without the stage, a record
-- queued as a conflict at screening would block the same record being queued
-- as a conflict at full text, which is a different question entirely.
CREATE UNIQUE INDEX IF NOT EXISTS ux_review_queue_open
    ON review_queue(record_id, reason, stage) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_review_queue_open ON review_queue(resolved_at);
"""
)

# Spliced into the screening script in dependency order: the tables the queue
# refers to, then the queue, then the views that read it.
SCREENING_SQL = SCREENING_SQL + REVIEW_QUEUE_FULL_SQL + SCREENING_VIEWS_SQL


def rebuild_review_queue(connection) -> bool:
    """Bring the queue table to its current shape, keeping every row.

    SQLite cannot widen a CHECK constraint in place, and each stage adds
    reasons an item can be queued for. So the table is rebuilt: new table,
    rows copied, old one dropped, indexes recreated. Returns whether any work
    was needed.

    Nothing a reviewer decided is lost — only the shape around the rows
    changes.
    """
    current = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'review_queue'"
    ).fetchone()
    if current and REVIEW_QUEUE_SHAPE in (current[0] or ""):
        return False

    columns = {row[1] for row in connection.execute("PRAGMA table_info(review_queue)")}
    stage_source = "stage" if "stage" in columns else "'screen'"
    # The screening PRISMA view reads this table, so it has to go first and be
    # recreated afterwards by the caller.
    connection.execute("DROP VIEW IF EXISTS v_prisma_screening")
    connection.execute("DROP VIEW IF EXISTS v_prisma_fulltext")
    # SQLite's documented procedure for changing a table's shape: foreign keys
    # off for the swap, because the old table is dropped while the new one is
    # being put in its place.
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.executescript(REVIEW_QUEUE_SQL.format(name="review_queue_new"))
        connection.execute(
            "INSERT INTO review_queue_new"
            "(id, record_id, reason, detail, run_id, stage, created_at, "
            " resolved_at, resolution) "
            "SELECT id, record_id, reason, detail, run_id, "
            f"       {stage_source}, "
            "       created_at, resolved_at, resolution FROM review_queue"
        )
        connection.execute("DROP TABLE review_queue")
        connection.execute("ALTER TABLE review_queue_new RENAME TO review_queue")
        connection.execute(
            "CREATE UNIQUE INDEX ux_review_queue_open "
            "ON review_queue(record_id, reason, stage) WHERE resolved_at IS NULL"
        )
        connection.execute(
            "CREATE INDEX ix_review_queue_open ON review_queue(resolved_at)"
        )
    finally:
        connection.execute("PRAGMA foreign_keys = ON")
    return True


# --------------------------------------------------------------- v4 --------
# Data extraction.
#
# Two shapes, because a review needs both: one row per *report* describing the
# study, and one row per *relationship* the report states between two things
# it measured. The relationship rows are what the synthesis counts, so each
# one carries the page and the words it came from, and a second AI pass that
# re-derives the direction from that quotation alone.
#
# Both tables are keyed by coder as well as record, so the same report can be
# coded independently by the AI and by a second person and the two compared.

EXTRACTION_SQL = """
CREATE TABLE IF NOT EXISTS extraction_run (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT    NOT NULL DEFAULT 'extract'
                    CHECK (kind IN ('extract','verify')),
    status          TEXT    NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running','paused','finished','failed')),
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    provider        TEXT    NOT NULL DEFAULT '',
    model           TEXT    NOT NULL DEFAULT '',
    temperature     REAL    NOT NULL DEFAULT 0.0,
    prompt_version  TEXT    NOT NULL DEFAULT '',
    protocol_version INTEGER,
    settings        TEXT    NOT NULL DEFAULT '{}',
    n_records       INTEGER NOT NULL DEFAULT 0,
    n_done          INTEGER NOT NULL DEFAULT 0,
    n_relationships INTEGER NOT NULL DEFAULT 0,
    n_calls         INTEGER NOT NULL DEFAULT 0,
    n_cached        INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL    NOT NULL DEFAULT 0.0,
    note            TEXT    NOT NULL DEFAULT ''
);

-- One row per report per coder.
CREATE TABLE IF NOT EXISTS report_extraction (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER REFERENCES extraction_run(id),
    record_id       INTEGER NOT NULL REFERENCES record(id),
    coder_role      TEXT    NOT NULL
                    CHECK (coder_role IN ('ai','human','second_coder')),
    coder           TEXT    NOT NULL,

    country         TEXT    NOT NULL DEFAULT '',
    setting         TEXT    NOT NULL DEFAULT '',
    population      TEXT    NOT NULL DEFAULT '',

    -- `design` is what the authors call it; `design_family` is the comparable
    -- form the synthesis groups by.
    design          TEXT    NOT NULL DEFAULT '',
    design_family   TEXT    NOT NULL DEFAULT 'unclear',
    -- The experiment rule: a study is only an experiment if the report shows
    -- a manipulated treatment compared against a control.
    manipulated_treatment INTEGER NOT NULL DEFAULT 0,
    control_group   INTEGER NOT NULL DEFAULT 0,
    design_downgraded INTEGER NOT NULL DEFAULT 0,
    design_note     TEXT    NOT NULL DEFAULT '',

    sampling        TEXT    NOT NULL DEFAULT '',
    sample_size     INTEGER,
    sample_size_text TEXT   NOT NULL DEFAULT '',
    time_horizon    TEXT    NOT NULL DEFAULT '',

    theories        TEXT    NOT NULL DEFAULT '[]',   -- [{name, role}]
    framework_stages TEXT   NOT NULL DEFAULT '[]',
    key_findings    TEXT    NOT NULL DEFAULT '',
    limitations     TEXT    NOT NULL DEFAULT '',

    appraisal_instrument TEXT NOT NULL DEFAULT 'none',
    appraisal_note  TEXT    NOT NULL DEFAULT '',
    appraisal_concern TEXT  NOT NULL DEFAULT 'unclear'
                    CHECK (appraisal_concern IN ('low','moderate','high','unclear')),
    instrument_matches_design INTEGER NOT NULL DEFAULT 1,

    quote           TEXT    NOT NULL DEFAULT '',
    quote_page      INTEGER,
    quote_verified  INTEGER NOT NULL DEFAULT 0,
    quote_found_on  INTEGER,

    needs_review    INTEGER NOT NULL DEFAULT 0,
    review_reason   TEXT    NOT NULL DEFAULT '',
    ai_call_id      INTEGER REFERENCES ai_call(id),
    created_at      TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_report_extraction
    ON report_extraction(record_id, coder_role);
CREATE INDEX IF NOT EXISTS ix_report_extraction_family
    ON report_extraction(design_family);

-- One row per relationship a report states, per coder.
CREATE TABLE IF NOT EXISTS relationship (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER REFERENCES extraction_run(id),
    record_id       INTEGER NOT NULL REFERENCES record(id),
    coder_role      TEXT    NOT NULL
                    CHECK (coder_role IN ('ai','human','second_coder')),
    coder           TEXT    NOT NULL,
    position        INTEGER NOT NULL DEFAULT 0,  -- order within the report

    -- Labels are kept exactly as the authors wrote them. Mapping them onto
    -- shared constructs is stage 5's job, and doing it here would lose the
    -- wording the mapping has to be justified against.
    predictor_label TEXT    NOT NULL,
    outcome_label   TEXT    NOT NULL,
    mediator_label  TEXT    NOT NULL DEFAULT '',
    moderator_label TEXT    NOT NULL DEFAULT '',

    relationship_type TEXT  NOT NULL DEFAULT 'association'
                    CHECK (relationship_type IN ('association','prediction',
                                                 'causal_effect','mediation',
                                                 'moderation','qualitative_link')),
    direction       TEXT    NOT NULL DEFAULT 'unclear'
                    CHECK (direction IN ('positive','negative','none','mixed','unclear')),
    strength        TEXT    NOT NULL DEFAULT '',
    statistic       TEXT    NOT NULL DEFAULT '',
    significance    TEXT    NOT NULL DEFAULT 'not_reported'
                    CHECK (significance IN ('significant','not_significant',
                                            'not_reported','not_applicable')),
    evidence_basis  TEXT    NOT NULL DEFAULT 'asserted'
                    CHECK (evidence_basis IN ('statistical','thematic','asserted')),

    quote           TEXT    NOT NULL DEFAULT '',
    quote_page      INTEGER,
    quote_verified  INTEGER NOT NULL DEFAULT 0,
    quote_found_on  INTEGER,
    verify_detail   TEXT    NOT NULL DEFAULT '',

    -- The separate verification pass: the direction re-derived from the
    -- quotation alone, without seeing what the extraction said.
    verified_direction TEXT,
    verify_agrees   INTEGER,
    verify_note     TEXT    NOT NULL DEFAULT '',
    verify_call_id  INTEGER REFERENCES ai_call(id),

    needs_review    INTEGER NOT NULL DEFAULT 0,
    review_reason   TEXT    NOT NULL DEFAULT '',
    ai_call_id      INTEGER REFERENCES ai_call(id),
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_relationship_record
    ON relationship(record_id, coder_role);
CREATE INDEX IF NOT EXISTS ix_relationship_direction ON relationship(direction);
CREATE INDEX IF NOT EXISTS ix_relationship_review ON relationship(needs_review);

-- How closely a second coder agreed with the first, field by field.
CREATE TABLE IF NOT EXISTS coder_agreement (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scope           TEXT    NOT NULL,        -- e.g. 'report:design_family'
    n               INTEGER NOT NULL,
    n_agree         INTEGER NOT NULL,
    percent_agreement REAL  NOT NULL,
    kappa           REAL,
    pabak           REAL,
    detail          TEXT    NOT NULL DEFAULT '{}',
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_coder_agreement_scope ON coder_agreement(scope);

-- Which reports the second coder was asked to code, chosen reproducibly.
CREATE TABLE IF NOT EXISTS second_coding_sample (
    record_id       INTEGER PRIMARY KEY REFERENCES record(id),
    chosen_at       TEXT    NOT NULL,
    coded_at        TEXT
);

CREATE VIEW IF NOT EXISTS v_extraction_progress AS
SELECT
    (SELECT COUNT(*) FROM fulltext_status WHERE decision = 'include')  AS n_included,
    (SELECT COUNT(*) FROM report_extraction WHERE coder_role = 'ai')   AS n_extracted,
    (SELECT COUNT(*) FROM relationship WHERE coder_role = 'ai')        AS n_relationships,
    (SELECT COUNT(*) FROM relationship
       WHERE coder_role = 'ai' AND verify_agrees = 0)                  AS n_direction_mismatches,
    (SELECT COUNT(*) FROM relationship
       WHERE coder_role = 'ai' AND quote <> '' AND quote_found_on IS NULL)
                                                                       AS n_quotes_not_found,
    (SELECT COUNT(*) FROM review_queue
       WHERE resolved_at IS NULL AND stage = 'extract')                AS n_awaiting_review,
    (SELECT COUNT(*) FROM second_coding_sample)                        AS n_second_coded_sample,
    (SELECT COUNT(*) FROM second_coding_sample WHERE coded_at IS NOT NULL)
                                                                       AS n_second_coded;

-- What the synthesis will read: every relationship from an included report,
-- with the study description beside it.
CREATE VIEW IF NOT EXISTS v_relationship_inventory AS
SELECT rel.id              AS relationship_id,
       rel.record_id,
       r.title,
       r.year,
       r.journal,
       r.doi,
       ex.country,
       ex.design_family,
       ex.sample_size,
       ex.appraisal_concern,
       rel.predictor_label,
       rel.outcome_label,
       rel.mediator_label,
       rel.moderator_label,
       rel.relationship_type,
       rel.direction,
       rel.strength,
       rel.statistic,
       rel.significance,
       rel.evidence_basis,
       rel.quote,
       rel.quote_page,
       rel.quote_verified,
       rel.verified_direction,
       rel.verify_agrees,
       rel.needs_review
FROM relationship rel
JOIN record r ON r.id = rel.record_id
LEFT JOIN report_extraction ex
       ON ex.record_id = rel.record_id AND ex.coder_role = rel.coder_role
WHERE rel.coder_role = 'ai'
ORDER BY rel.record_id, rel.position;
"""


# --------------------------------------------------------------- v5 --------
# Groundwork for the Gap Finder (spec section 11), plus the two things it
# needs that earlier stages do not yet record.
#
# Nothing here is filled in by the stages built so far. It is added now
# because all three change the shape of data those stages produce, and
# retrofitting them after a review has been coded would mean asking a
# reviewer to code it again.

GAP_SQL = """
-- Keywords, one row each, so topic clustering and keyword co-occurrence can
-- be queried rather than parsed out of JSON every time.
--
-- `from_record_id` is which record the keyword arrived on. After
-- deduplication the surviving record carries the keywords of the duplicates
-- merged into it as well as its own: databases index the same paper with
-- different vocabularies, and throwing away the loser's keywords would make
-- the corpus look narrower than it is.
CREATE TABLE IF NOT EXISTS record_keyword (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id       INTEGER NOT NULL REFERENCES record(id),
    keyword         TEXT    NOT NULL,          -- as the database wrote it
    keyword_norm    TEXT    NOT NULL,          -- case and punctuation folded
    from_record_id  INTEGER NOT NULL REFERENCES record(id),
    source_database TEXT    NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_record_keyword
    ON record_keyword(record_id, keyword_norm, from_record_id);
CREATE INDEX IF NOT EXISTS ix_record_keyword_norm ON record_keyword(keyword_norm);
CREATE INDEX IF NOT EXISTS ix_record_keyword_record ON record_keyword(record_id);

-- What the quick scan reads: one row per record still in play, with the text
-- to cluster and the year to trend. A record with no year is still listed,
-- with a null, so "how many have no year" is answerable rather than invisible.
CREATE VIEW IF NOT EXISTS v_quick_scan_input AS
SELECT r.id            AS record_id,
       r.title,
       r.abstract,
       r.year,
       r.has_abstract,
       r.source_database,
       r.doc_type,
       r.language,
       -- One surface form per normalised keyword. Two databases spell the
       -- same term differently ("coastal tourism", "COASTAL TOURISM") and
       -- listing both would double-count it in a co-occurrence matrix.
       (SELECT GROUP_CONCAT(k.keyword, '; ')
          FROM (SELECT keyword_norm, MIN(keyword) AS keyword
                  FROM record_keyword WHERE record_id = r.id
                 GROUP BY keyword_norm ORDER BY keyword_norm) k)  AS keywords,
       (SELECT COUNT(DISTINCT keyword_norm)
          FROM record_keyword WHERE record_id = r.id)            AS n_keywords
FROM record r
WHERE r.active = 1;

-- "Future research" and "limitations" sentences, which the Gap Finder
-- clusters into author-stated gaps. One row per statement, each keeping the
-- page it came from, because a gap built on them has to cite them.
CREATE TABLE IF NOT EXISTS future_research (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id       INTEGER NOT NULL REFERENCES record(id),
    coder_role      TEXT    NOT NULL DEFAULT 'ai'
                    CHECK (coder_role IN ('ai','human','second_coder')),
    coder           TEXT    NOT NULL DEFAULT '',
    kind            TEXT    NOT NULL DEFAULT 'future_research'
                    CHECK (kind IN ('future_research','limitation')),
    statement       TEXT    NOT NULL,
    quote           TEXT    NOT NULL DEFAULT '',
    quote_page      INTEGER,
    quote_verified  INTEGER NOT NULL DEFAULT 0,
    quote_found_on  INTEGER,
    theme           TEXT    NOT NULL DEFAULT '',   -- set when they are clustered
    needs_review    INTEGER NOT NULL DEFAULT 0,
    review_reason   TEXT    NOT NULL DEFAULT '',
    run_id          INTEGER REFERENCES extraction_run(id),
    ai_call_id      INTEGER REFERENCES ai_call(id),
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_future_research_record
    ON future_research(record_id, coder_role);
CREATE INDEX IF NOT EXISTS ix_future_research_theme ON future_research(theme);

-- One row per gap. `statement` is the sentence a reviewer reads; everything
-- beside it is what makes that sentence checkable.
CREATE TABLE IF NOT EXISTS gap_register (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    gap_type        TEXT    NOT NULL
                    CHECK (gap_type IN ('evidence','theoretical','contradiction',
                                        'methodological','population_context',
                                        'information','author_stated')),
    statement       TEXT    NOT NULL,
    -- Every claim of absence is qualified by the corpus it is absent from.
    corpus_searched_to TEXT NOT NULL DEFAULT '',
    scope_note      TEXT    NOT NULL DEFAULT '',
    -- The counts the statement rests on, e.g. {"n_datasets": 0, "n_total": 89}.
    -- A gap with no counts is not a gap; the engine refuses to write one.
    supporting_counts TEXT  NOT NULL DEFAULT '{}',
    cell_key        TEXT    NOT NULL DEFAULT '',   -- e.g. 'efficacy x Africa'

    freshness_status TEXT   NOT NULL DEFAULT 'not_checked'
                    CHECK (freshness_status IN ('not_checked','none_found',
                                                'possibly_addressed','check_failed')),
    freshness_detail TEXT   NOT NULL DEFAULT '{}',
    freshness_checked_at TEXT,

    score_centrality REAL,
    score_size      REAL,
    score_relevance REAL,
    score_feasibility REAL,
    priority        REAL,
    -- Scores the reviewer overrode, as {criterion: value}, with why.
    score_overrides TEXT    NOT NULL DEFAULT '{}',
    override_reason TEXT    NOT NULL DEFAULT '',
    overridden_by   TEXT    NOT NULL DEFAULT '',
    overridden_at   TEXT,

    suggested_question   TEXT NOT NULL DEFAULT '',
    suggested_hypothesis TEXT NOT NULL DEFAULT '',
    suggested_design     TEXT NOT NULL DEFAULT '',
    suggested_measures   TEXT NOT NULL DEFAULT '',
    suggested_population TEXT NOT NULL DEFAULT '',
    suggested_setting    TEXT NOT NULL DEFAULT '',

    status          TEXT    NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed','accepted','rejected','merged')),
    status_reason   TEXT    NOT NULL DEFAULT '',
    run_id          INTEGER,
    ai_call_id      INTEGER REFERENCES ai_call(id),
    created_at      TEXT    NOT NULL,
    created_by      TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_gap_register_type ON gap_register(gap_type);
CREATE INDEX IF NOT EXISTS ix_gap_register_priority ON gap_register(priority);
CREATE INDEX IF NOT EXISTS ix_gap_register_status ON gap_register(status);

-- What each gap points at: the records, relationships and author statements
-- its counts were taken from. Integrity rule 1 in table form — a gap
-- statement with no rows here cannot be exported.
CREATE TABLE IF NOT EXISTS gap_evidence (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    gap_id          INTEGER NOT NULL REFERENCES gap_register(id),
    kind            TEXT    NOT NULL
                    CHECK (kind IN ('record','relationship','future_research',
                                    'construct','link','cell')),
    record_id       INTEGER REFERENCES record(id),
    relationship_id INTEGER REFERENCES relationship(id),
    future_research_id INTEGER REFERENCES future_research(id),
    -- For evidence that is not a row: a construct name, a link id, a cell key.
    label           TEXT    NOT NULL DEFAULT '',
    role            TEXT    NOT NULL DEFAULT 'supports'
                    CHECK (role IN ('supports','counts_toward','absent_from',
                                    'cited_by')),
    note            TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_gap_evidence_gap ON gap_evidence(gap_id);
CREATE INDEX IF NOT EXISTS ix_gap_evidence_record ON gap_evidence(record_id);

CREATE VIEW IF NOT EXISTS v_gap_register AS
SELECT g.*,
       (SELECT COUNT(*) FROM gap_evidence e WHERE e.gap_id = g.id) AS n_evidence,
       (SELECT COUNT(DISTINCT e.record_id) FROM gap_evidence e
         WHERE e.gap_id = g.id AND e.record_id IS NOT NULL)        AS n_records
FROM gap_register g;
"""


def migrate_to_v5(connection) -> None:
    """Bring a v4 project forward: Gap Finder groundwork.

    Additive only — new tables and views, nothing dropped or rewritten — so a
    project written by the previous build opens unchanged. The keyword index
    is backfilled from the JSON already stored on each record, including the
    keywords of records that deduplication merged away.
    """
    connection.executescript(GAP_SQL)
    backfill_record_keywords(connection)


def backfill_record_keywords(connection) -> None:
    """Fill `record_keyword` from the JSON on each record.

    Safe to run more than once: the unique index means a second pass inserts
    nothing. A record that was merged into another contributes its keywords to
    the survivor as well as to itself.
    """
    import json

    from ..importers.normalise import normalise_title

    existing = connection.execute(
        "SELECT COUNT(*) FROM record_keyword"
    ).fetchone()[0]
    if existing:
        return

    rows = connection.execute(
        "SELECT id, keywords, source_database, duplicate_of FROM record"
    ).fetchall()
    payload: list[tuple] = []
    for record_id, raw, source_database, duplicate_of in rows:
        try:
            keywords = json.loads(raw or "[]")
        except (TypeError, ValueError):
            keywords = []
        for keyword in keywords:
            text = str(keyword).strip()
            if not text:
                continue
            normalised = normalise_title(text)
            if not normalised:
                continue
            payload.append((record_id, text, normalised, record_id, source_database or ""))
            if duplicate_of:
                payload.append(
                    (duplicate_of, text, normalised, record_id, source_database or "")
                )
    if payload:
        connection.executemany(
            "INSERT OR IGNORE INTO record_keyword"
            "(record_id, keyword, keyword_norm, from_record_id, source_database) "
            "VALUES(?, ?, ?, ?, ?)",
            payload,
        )


def migrate_to_v3(connection) -> None:
    """Bring a v2 project forward: full-text retrieval and eligibility.

    The queue gains a `stage` column so screening and full-text items can
    share one table, plus the reasons full text needs.
    """
    rebuild_review_queue(connection)
    connection.execute("DROP VIEW IF EXISTS v_prisma_screening")
    connection.executescript(SCREENING_VIEWS_SQL + FULLTEXT_SQL)


def migrate_to_v4(connection) -> None:
    """Bring a v3 project forward: data extraction.

    The queue is rebuilt again for the reasons extraction adds — a direction
    the verification pass disagreed with, a design claim that fails the
    experiment rule, two coders who differ.
    """
    if rebuild_review_queue(connection):
        # The rebuild drops the two views that read the queue; both scripts
        # are idempotent, so running them again puts them back.
        connection.executescript(SCREENING_VIEWS_SQL + FULLTEXT_SQL)
    connection.executescript(EXTRACTION_SQL)


# --------------------------------------------------------------- v6 --------
# The synthesis engine (spec section 9).
#
# Extraction left the authors' own words alone, which is right for a record of
# what each report said and useless for counting across reports: "resident
# empowerment", "community empowerment" and "local agency" may be the same
# thing measured three ways, or three different things. This stage builds the
# bridge, in four moves that must happen in order.
#
#   constructs      a codebook the human approves before anything is mapped
#   label_mapping   each author label placed against a construct, with reasons
#   link_vote       one harmonised vote per dataset per construct pair
#   graded_link     the counted, graded, interval-bearing inventory
#
# The order matters and the schema enforces what it can: a mapping points at a
# construct, a vote points at a mapping's constructs, a graded link is built
# from votes. Nothing downstream can quietly invent a construct that the human
# never saw.

SYNTHESIS_SQL = """
-- The codebook. One row per construct the review will count in.
CREATE TABLE IF NOT EXISTS construct (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    name_norm       TEXT    NOT NULL,
    definition      TEXT    NOT NULL DEFAULT '',
    includes        TEXT    NOT NULL DEFAULT '[]',   -- ["what counts", ...]
    excludes        TEXT    NOT NULL DEFAULT '[]',   -- ["what does not", ...]

    -- The reference orientation: which pole of the construct counts as "more".
    -- Harmonisation re-expresses every relationship against this, so that
    -- "higher trust" and "lower distrust" are not counted as opposite
    -- findings. Set by the human, never by the model.
    orientation     TEXT    NOT NULL DEFAULT '',
    orientation_set_by TEXT NOT NULL DEFAULT '',
    orientation_set_at TEXT,

    -- 'other' is the residual code the spec allows: a label that genuinely
    -- fits nothing goes here rather than being forced into a construct.
    is_residual     INTEGER NOT NULL DEFAULT 0,

    status          TEXT    NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed','approved','rejected','merged')),
    merged_into     INTEGER REFERENCES construct(id),
    source          TEXT    NOT NULL DEFAULT 'ai'
                    CHECK (source IN ('ai','human')),
    note            TEXT    NOT NULL DEFAULT '',
    position        INTEGER NOT NULL DEFAULT 0,
    run_id          INTEGER,
    ai_call_id      INTEGER REFERENCES ai_call(id),
    created_at      TEXT    NOT NULL,
    approved_by     TEXT    NOT NULL DEFAULT '',
    approved_at     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_construct_name ON construct(name_norm);
CREATE INDEX IF NOT EXISTS ix_construct_status ON construct(status);

-- Whether the codebook as a whole has been signed off. Mapping refuses to run
-- until there is an approval row, because a codebook the human has not read
-- is the model's vocabulary, not the review's.
CREATE TABLE IF NOT EXISTS codebook_approval (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    n_constructs    INTEGER NOT NULL,
    construct_hash  TEXT    NOT NULL,    -- what exactly was approved
    approved_by     TEXT    NOT NULL,
    approved_at     TEXT    NOT NULL,
    note            TEXT    NOT NULL DEFAULT '',
    superseded_at   TEXT
);

-- One row per distinct author label, per role it plays.
CREATE TABLE IF NOT EXISTS label_mapping (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT    NOT NULL,        -- as the authors wrote it
    label_norm      TEXT    NOT NULL,
    role            TEXT    NOT NULL DEFAULT 'variable'
                    CHECK (role IN ('variable','predictor','outcome',
                                    'mediator','moderator')),
    construct_id    INTEGER REFERENCES construct(id),

    -- Mapping is by what was measured, not by vocabulary, so the model is
    -- asked for the measurement it inferred and that is what the human reads
    -- when deciding whether the mapping holds.
    measured_as     TEXT    NOT NULL DEFAULT '',
    rationale       TEXT    NOT NULL DEFAULT '',
    confidence      REAL,

    -- A mapping the model itself is unsure of, or that the rules below catch.
    contestable     INTEGER NOT NULL DEFAULT 0,
    contest_reason  TEXT    NOT NULL DEFAULT '',

    status          TEXT    NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed','accepted','overridden')),
    overridden_from INTEGER REFERENCES construct(id),
    override_reason TEXT    NOT NULL DEFAULT '',
    decided_by      TEXT    NOT NULL DEFAULT '',
    decided_at      TEXT,

    n_uses          INTEGER NOT NULL DEFAULT 0,   -- how many relationships
    run_id          INTEGER,
    ai_call_id      INTEGER REFERENCES ai_call(id),
    created_at      TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_label_mapping ON label_mapping(label_norm, role);
CREATE INDEX IF NOT EXISTS ix_label_mapping_construct ON label_mapping(construct_id);
CREATE INDEX IF NOT EXISTS ix_label_mapping_contestable ON label_mapping(contestable);

-- A dataset: one body of fieldwork, which may be reported in several papers.
-- The unit the synthesis counts, so that a team publishing the same survey
-- four times does not out-vote four independent studies.
CREATE TABLE IF NOT EXISTS dataset (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT    NOT NULL DEFAULT '',
    country         TEXT    NOT NULL DEFAULT '',
    note            TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL
);

-- Which reports belong to which dataset. A report with no confirmed link is
-- its own dataset of one, which is the common case.
CREATE TABLE IF NOT EXISTS dataset_member (
    dataset_id      INTEGER NOT NULL REFERENCES dataset(id),
    record_id       INTEGER NOT NULL REFERENCES record(id),
    is_primary      INTEGER NOT NULL DEFAULT 0,
    added_at        TEXT    NOT NULL,
    PRIMARY KEY (dataset_id, record_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_dataset_member_record
    ON dataset_member(record_id);

-- A suspected shared sample, awaiting the human's confirmation. Detection is
-- a signal, never a merge: two surveys of the same town in the same year by
-- the same team may still be two datasets, and only a person reading both can
-- say.
CREATE TABLE IF NOT EXISTS dataset_link (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    record_a        INTEGER NOT NULL REFERENCES record(id),
    record_b        INTEGER NOT NULL REFERENCES record(id),
    score           REAL    NOT NULL DEFAULT 0.0,
    signals         TEXT    NOT NULL DEFAULT '{}',  -- authors, site, dates, n
    reason          TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed','confirmed','rejected')),
    decided_by      TEXT    NOT NULL DEFAULT '',
    decided_at      TEXT,
    decision_reason TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_dataset_link ON dataset_link(record_a, record_b);
CREATE INDEX IF NOT EXISTS ix_dataset_link_status ON dataset_link(status);

-- One relationship, mapped onto constructs and re-expressed against their
-- reference orientations. Every sign flip is recorded here with the reason,
-- so a reviewer can always see the original alongside the harmonised form.
CREATE TABLE IF NOT EXISTS harmonised_relationship (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    relationship_id INTEGER NOT NULL REFERENCES relationship(id),
    record_id       INTEGER NOT NULL REFERENCES record(id),
    dataset_id      INTEGER REFERENCES dataset(id),

    predictor_construct INTEGER REFERENCES construct(id),
    outcome_construct   INTEGER REFERENCES construct(id),
    mediator_construct  INTEGER REFERENCES construct(id),
    moderator_construct INTEGER REFERENCES construct(id),

    original_direction  TEXT NOT NULL,
    direction       TEXT    NOT NULL
                    CHECK (direction IN ('positive','negative','none','mixed','unclear')),
    flipped         INTEGER NOT NULL DEFAULT 0,
    flip_reason     TEXT    NOT NULL DEFAULT '',

    -- Why this relationship does or does not get a vote.
    counts          INTEGER NOT NULL DEFAULT 1,
    excluded_reason TEXT    NOT NULL DEFAULT '',

    run_id          INTEGER,
    created_at      TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_harmonised_relationship
    ON harmonised_relationship(relationship_id);
CREATE INDEX IF NOT EXISTS ix_harmonised_pair
    ON harmonised_relationship(predictor_construct, outcome_construct);
CREATE INDEX IF NOT EXISTS ix_harmonised_flipped ON harmonised_relationship(flipped);

-- One vote per dataset per construct pair: what that body of fieldwork found,
-- after reconciling the reports that share it.
CREATE TABLE IF NOT EXISTS link_vote (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER,
    predictor_construct INTEGER NOT NULL REFERENCES construct(id),
    outcome_construct   INTEGER NOT NULL REFERENCES construct(id),
    dataset_id      INTEGER NOT NULL REFERENCES dataset(id),
    direction       TEXT    NOT NULL
                    CHECK (direction IN ('positive','negative','none','mixed','unclear')),
    n_relationships INTEGER NOT NULL DEFAULT 1,
    within_dataset_conflict INTEGER NOT NULL DEFAULT 0,
    country         TEXT    NOT NULL DEFAULT '',
    design_family   TEXT    NOT NULL DEFAULT '',
    evidence_basis  TEXT    NOT NULL DEFAULT '',
    appraisal_concern TEXT  NOT NULL DEFAULT 'unclear',
    created_at      TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_link_vote
    ON link_vote(run_id, predictor_construct, outcome_construct, dataset_id);

-- The graded inventory. The only thing the framework builder may cite.
CREATE TABLE IF NOT EXISTS graded_link (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER,
    link_key        TEXT    NOT NULL,          -- stable, human-readable id
    predictor_construct INTEGER NOT NULL REFERENCES construct(id),
    outcome_construct   INTEGER NOT NULL REFERENCES construct(id),

    direction       TEXT    NOT NULL DEFAULT 'unclear',
    grade           TEXT    NOT NULL
                    CHECK (grade IN ('asserted','contested','strong','moderate',
                                     'emerging','tentative')),
    grade_reason    TEXT    NOT NULL DEFAULT '',
    rule_applied    TEXT    NOT NULL DEFAULT '',

    n_datasets      INTEGER NOT NULL DEFAULT 0,
    n_reports       INTEGER NOT NULL DEFAULT 0,
    n_agree         INTEGER NOT NULL DEFAULT 0,
    n_oppose        INTEGER NOT NULL DEFAULT 0,
    n_null          INTEGER NOT NULL DEFAULT 0,
    n_unclear       INTEGER NOT NULL DEFAULT 0,
    n_countries     INTEGER NOT NULL DEFAULT 0,
    countries       TEXT    NOT NULL DEFAULT '[]',

    proportion      REAL,
    ci_low          REAL,
    ci_high         REAL,
    ci_level        REAL    NOT NULL DEFAULT 0.95,
    sign_p          REAL,
    sign_significant INTEGER NOT NULL DEFAULT 0,

    evidence_bases  TEXT    NOT NULL DEFAULT '[]',
    design_families TEXT    NOT NULL DEFAULT '[]',

    -- A human may overrule the grade; the original is kept beside it.
    grade_overridden INTEGER NOT NULL DEFAULT 0,
    original_grade  TEXT    NOT NULL DEFAULT '',
    override_reason TEXT    NOT NULL DEFAULT '',
    overridden_by   TEXT    NOT NULL DEFAULT '',
    overridden_at   TEXT,

    created_at      TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_graded_link ON graded_link(run_id, link_key);
CREATE INDEX IF NOT EXISTS ix_graded_link_grade ON graded_link(grade);

CREATE TABLE IF NOT EXISTS synthesis_run (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT    NOT NULL
                    CHECK (kind IN ('codebook','mapping','harmonise','linkage',
                                    'grade','sensitivity','contradiction')),
    status          TEXT    NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running','paused','finished','failed')),
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    provider        TEXT    NOT NULL DEFAULT '',
    model           TEXT    NOT NULL DEFAULT '',
    temperature     REAL    NOT NULL DEFAULT 0.0,
    prompt_version  TEXT    NOT NULL DEFAULT '',
    protocol_version INTEGER,
    settings        TEXT    NOT NULL DEFAULT '{}',
    n_calls         INTEGER NOT NULL DEFAULT 0,
    n_cached        INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL    NOT NULL DEFAULT 0.0,
    summary         TEXT    NOT NULL DEFAULT '{}',
    note            TEXT    NOT NULL DEFAULT ''
);

-- What changed when the grading was re-run under a different rule.
CREATE TABLE IF NOT EXISTS sensitivity_result (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER,
    analysis        TEXT    NOT NULL,          -- which variant
    description     TEXT    NOT NULL DEFAULT '',
    link_key        TEXT    NOT NULL,
    base_grade      TEXT    NOT NULL DEFAULT '',
    new_grade       TEXT    NOT NULL DEFAULT '',
    base_direction  TEXT    NOT NULL DEFAULT '',
    new_direction   TEXT    NOT NULL DEFAULT '',
    changed_grade   INTEGER NOT NULL DEFAULT 0,
    changed_sign    INTEGER NOT NULL DEFAULT 0,
    n_datasets      INTEGER NOT NULL DEFAULT 0,
    base_n_datasets INTEGER NOT NULL DEFAULT 0,
    detail          TEXT    NOT NULL DEFAULT '{}',
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sensitivity_analysis ON sensitivity_result(analysis);
CREATE INDEX IF NOT EXISTS ix_sensitivity_link ON sensitivity_result(link_key);

-- Why a contested link is contested, and what to do about it.
CREATE TABLE IF NOT EXISTS contradiction (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER,
    link_key        TEXT    NOT NULL,
    graded_link_id  INTEGER REFERENCES graded_link(id),
    explanation_type TEXT   NOT NULL DEFAULT 'unclear'
                    CHECK (explanation_type IN ('context','measurement','method',
                                                'sample','unclear')),
    explanation     TEXT    NOT NULL DEFAULT '',
    suggestion_type TEXT    NOT NULL DEFAULT 'none'
                    CHECK (suggestion_type IN ('moderator','construct_split','none')),
    suggested_moderator TEXT NOT NULL DEFAULT '',
    suggested_split TEXT    NOT NULL DEFAULT '{}',
    supporting      TEXT    NOT NULL DEFAULT '{}',   -- the datasets each way
    status          TEXT    NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed','accepted','rejected')),
    decided_by      TEXT    NOT NULL DEFAULT '',
    decided_at      TEXT,
    decision_reason TEXT    NOT NULL DEFAULT '',
    ai_call_id      INTEGER REFERENCES ai_call(id),
    created_at      TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_contradiction ON contradiction(run_id, link_key);

-- Every label extraction produced, with how often it was used and whether it
-- has been mapped yet. The mapping stage's worklist.
CREATE VIEW IF NOT EXISTS v_label_inventory AS
SELECT label, role, COUNT(*) AS n_uses, MIN(record_id) AS first_record_id
FROM (
    SELECT predictor_label AS label, 'predictor' AS role, record_id
      FROM relationship WHERE coder_role = 'ai' AND predictor_label <> ''
    UNION ALL
    SELECT outcome_label, 'outcome', record_id
      FROM relationship WHERE coder_role = 'ai' AND outcome_label <> ''
    UNION ALL
    SELECT mediator_label, 'mediator', record_id
      FROM relationship WHERE coder_role = 'ai' AND mediator_label <> ''
    UNION ALL
    SELECT moderator_label, 'moderator', record_id
      FROM relationship WHERE coder_role = 'ai' AND moderator_label <> ''
)
GROUP BY label, role;

CREATE VIEW IF NOT EXISTS v_synthesis_progress AS
SELECT
    (SELECT COUNT(*) FROM construct WHERE status = 'approved')      AS n_constructs,
    (SELECT COUNT(*) FROM construct WHERE status = 'proposed')      AS n_constructs_proposed,
    (SELECT COUNT(*) FROM codebook_approval WHERE superseded_at IS NULL)
                                                                    AS n_approvals,
    (SELECT COUNT(*) FROM v_label_inventory)                        AS n_labels,
    (SELECT COUNT(*) FROM label_mapping)                            AS n_mapped,
    (SELECT COUNT(*) FROM label_mapping WHERE contestable = 1
       AND status = 'proposed')                                     AS n_contestable,
    (SELECT COUNT(*) FROM dataset)                                  AS n_datasets,
    (SELECT COUNT(*) FROM dataset_link WHERE status = 'proposed')   AS n_links_proposed,
    (SELECT COUNT(*) FROM dataset_link WHERE status = 'confirmed')  AS n_links_confirmed,
    (SELECT COUNT(*) FROM harmonised_relationship)                  AS n_harmonised,
    (SELECT COUNT(*) FROM harmonised_relationship WHERE flipped = 1) AS n_flipped,
    (SELECT COUNT(*) FROM graded_link)                              AS n_graded_links,
    (SELECT COUNT(*) FROM graded_link WHERE grade = 'strong')       AS n_strong,
    (SELECT COUNT(*) FROM graded_link WHERE grade = 'contested')    AS n_contested,
    (SELECT COUNT(*) FROM contradiction WHERE status = 'proposed')  AS n_contradictions,
    (SELECT COUNT(*) FROM sensitivity_result WHERE changed_grade = 1
        OR changed_sign = 1)                                        AS n_sensitive;

-- The graded inventory in readable form: construct names rather than ids.
-- This is what the framework builder reads and what the export writes.
CREATE VIEW IF NOT EXISTS v_graded_inventory AS
SELECT g.id,
       g.run_id,
       g.link_key,
       p.name   AS predictor,
       o.name   AS outcome,
       g.direction,
       g.grade,
       g.grade_reason,
       g.n_datasets,
       g.n_reports,
       g.n_agree,
       g.n_oppose,
       g.n_null,
       g.n_countries,
       g.countries,
       g.proportion,
       g.ci_low,
       g.ci_high,
       g.sign_p,
       g.sign_significant,
       g.grade_overridden,
       g.original_grade
FROM graded_link g
JOIN construct p ON p.id = g.predictor_construct
JOIN construct o ON o.id = g.outcome_construct
ORDER BY g.grade, p.name, o.name;
"""


FRAMEWORK_SQL = """
-- One figure per theme, so the themes are stored rather than implied.
CREATE TABLE IF NOT EXISTS theme (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    description     TEXT    NOT NULL DEFAULT '',
    position        INTEGER NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed','approved','rejected')),
    status_reason   TEXT    NOT NULL DEFAULT '',
    decided_by      TEXT    NOT NULL DEFAULT '',
    decided_at      TEXT,
    run_id          INTEGER,
    ai_call_id      INTEGER REFERENCES ai_call(id),
    created_at      TEXT    NOT NULL,
    created_by      TEXT    NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_theme_name ON theme(name);

CREATE TABLE IF NOT EXISTS theme_member (
    theme_id        INTEGER NOT NULL REFERENCES theme(id),
    construct_id    INTEGER NOT NULL REFERENCES construct(id),
    PRIMARY KEY (theme_id, construct_id)
);
CREATE INDEX IF NOT EXISTS ix_theme_member_construct
    ON theme_member(construct_id);

-- A proposition. Every field the spec names is its own column, because a
-- proposition missing its mechanism or its falsifying test is not a
-- proposition, and a single blob of prose would hide that.
CREATE TABLE IF NOT EXISTS proposition (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    number          INTEGER NOT NULL DEFAULT 0,
    theme_id        INTEGER REFERENCES theme(id),
    statement       TEXT    NOT NULL,
    mechanism       TEXT    NOT NULL DEFAULT '',
    -- Theories the user listed, as the model was told to draw on them.
    theories        TEXT    NOT NULL DEFAULT '[]',
    moderators      TEXT    NOT NULL DEFAULT '',
    boundary_conditions TEXT NOT NULL DEFAULT '',
    falsifying_test TEXT    NOT NULL DEFAULT '',
    element_kind    TEXT    NOT NULL DEFAULT 'evidenced_link'
                    CHECK (element_kind IN ('evidenced_link',
                                            'proposed_mechanism',
                                            'untested_pathway')),
    status          TEXT    NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed','accepted','rejected','edited')),
    status_reason   TEXT    NOT NULL DEFAULT '',
    edited_from     TEXT    NOT NULL DEFAULT '',
    decided_by      TEXT    NOT NULL DEFAULT '',
    decided_at      TEXT,
    run_id          INTEGER,
    ai_call_id      INTEGER REFERENCES ai_call(id),
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_proposition_theme ON proposition(theme_id);

-- The grounding. A row can only point at a link that is in the graded
-- inventory; the foreign key is half of the spec's "reject any output that
-- does not", and the engine refuses the rest before it ever gets here.
CREATE TABLE IF NOT EXISTS proposition_link (
    proposition_id  INTEGER NOT NULL REFERENCES proposition(id),
    graded_link_id  INTEGER NOT NULL REFERENCES graded_link(id),
    note            TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (proposition_id, graded_link_id)
);
CREATE INDEX IF NOT EXISTS ix_proposition_link_link
    ON proposition_link(graded_link_id);

-- What the integrated diagram draws. The three kinds are kept apart here so
-- that the renderer cannot blur them: an evidenced link must name the graded
-- link it stands for, and the other two must not pretend to.
CREATE TABLE IF NOT EXISTS framework_element (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT    NOT NULL
                    CHECK (kind IN ('evidenced_link','proposed_mechanism',
                                    'untested_pathway')),
    source_label    TEXT    NOT NULL,
    target_label    TEXT    NOT NULL,
    graded_link_id  INTEGER REFERENCES graded_link(id),
    proposition_id  INTEGER REFERENCES proposition(id),
    direction       TEXT    NOT NULL DEFAULT 'unclear',
    label           TEXT    NOT NULL DEFAULT '',
    note            TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL,
    CHECK (kind <> 'evidenced_link' OR graded_link_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_framework_element_kind ON framework_element(kind);

CREATE TABLE IF NOT EXISTS figure (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT    NOT NULL
                    CHECK (kind IN ('evidence_map','integrated')),
    theme_id        INTEGER REFERENCES theme(id),
    filename        TEXT    NOT NULL,
    format          TEXT    NOT NULL DEFAULT 'svg'
                    CHECK (format IN ('svg','png','pptx','pdf')),
    n_constructs    INTEGER NOT NULL DEFAULT 0,
    n_links         INTEGER NOT NULL DEFAULT 0,
    n_crossings     INTEGER NOT NULL DEFAULT 0,
    grades          TEXT    NOT NULL DEFAULT '{}',
    run_id          INTEGER,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_figure_theme ON figure(theme_id, kind);

CREATE VIEW IF NOT EXISTS v_propositions AS
SELECT p.*,
       t.name AS theme_name,
       (SELECT COUNT(*) FROM proposition_link l
         WHERE l.proposition_id = p.id)                       AS n_links,
       (SELECT GROUP_CONCAT('L' || l.graded_link_id, ', ')
          FROM proposition_link l WHERE l.proposition_id = p.id) AS cited_links,
       (SELECT COUNT(*) FROM proposition_link l
          JOIN graded_link g ON g.id = l.graded_link_id
         WHERE l.proposition_id = p.id
           AND g.grade IN ('strong','moderate'))              AS n_well_graded
FROM proposition p
LEFT JOIN theme t ON t.id = p.theme_id;

CREATE VIEW IF NOT EXISTS v_framework_progress AS
SELECT
    (SELECT COUNT(*) FROM theme)                              AS n_themes,
    (SELECT COUNT(*) FROM theme WHERE status = 'approved')     AS n_themes_approved,
    (SELECT COUNT(*) FROM proposition)                        AS n_propositions,
    (SELECT COUNT(*) FROM proposition WHERE status = 'accepted')
                                                              AS n_accepted,
    (SELECT COUNT(*) FROM proposition WHERE status = 'rejected')
                                                              AS n_rejected,
    (SELECT COUNT(*) FROM proposition WHERE n_links = 0 FROM_PLACEHOLDER)
                                                              AS n_ungrounded,
    (SELECT COUNT(*) FROM framework_element WHERE kind = 'evidenced_link')
                                                              AS n_evidenced,
    (SELECT COUNT(*) FROM framework_element WHERE kind = 'proposed_mechanism')
                                                              AS n_mechanisms,
    (SELECT COUNT(*) FROM framework_element WHERE kind = 'untested_pathway')
                                                              AS n_untested,
    (SELECT COUNT(*) FROM figure)                             AS n_figures;
"""

# The progress view counts ungrounded propositions through the view above it,
# which SQLite cannot express inline; written out rather than nested so the
# query stays readable.
FRAMEWORK_SQL = FRAMEWORK_SQL.replace(
    "(SELECT COUNT(*) FROM proposition WHERE n_links = 0 FROM_PLACEHOLDER)",
    "(SELECT COUNT(*) FROM v_propositions WHERE n_links = 0)",
)


GAP_RUN_SQL = """
CREATE TABLE IF NOT EXISTS gap_run (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mode            TEXT    NOT NULL CHECK (mode IN ('quick','full')),
    status          TEXT    NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running','paused','finished','failed')),
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    -- Copied onto every gap the run produces, so a gap exported months later
    -- still says what it was an absence *within*.
    corpus_searched_to TEXT NOT NULL DEFAULT '',
    provider        TEXT    NOT NULL DEFAULT '',
    model           TEXT    NOT NULL DEFAULT '',
    prompt_version  TEXT    NOT NULL DEFAULT '',
    settings        TEXT    NOT NULL DEFAULT '{}',
    n_records       INTEGER NOT NULL DEFAULT 0,
    n_topics        INTEGER NOT NULL DEFAULT 0,
    n_gaps          INTEGER NOT NULL DEFAULT 0,
    n_calls         INTEGER NOT NULL DEFAULT 0,
    n_cached        INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL    NOT NULL DEFAULT 0.0,
    note            TEXT    NOT NULL DEFAULT ''
);

-- The quick scan's topics. A scoping view rather than a finding, which is
-- why nothing downstream may cite them: they are kept so the screen can be
-- redrawn without re-clustering, not so they can be reported.
CREATE TABLE IF NOT EXISTS topic (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER REFERENCES gap_run(id),
    label           TEXT    NOT NULL,
    terms           TEXT    NOT NULL DEFAULT '[]',
    n_records       INTEGER NOT NULL DEFAULT 0,
    year_first      INTEGER,
    year_last       INTEGER,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS topic_member (
    topic_id        INTEGER NOT NULL REFERENCES topic(id),
    record_id       INTEGER NOT NULL REFERENCES record(id),
    similarity      REAL,
    PRIMARY KEY (topic_id, record_id)
);
CREATE INDEX IF NOT EXISTS ix_topic_member_record ON topic_member(record_id);

CREATE VIEW IF NOT EXISTS v_gap_summary AS
SELECT g.gap_type,
       COUNT(*)                                                  AS n_gaps,
       SUM(CASE WHEN g.freshness_status = 'possibly_addressed'
                THEN 1 ELSE 0 END)                               AS n_possibly_addressed,
       SUM(CASE WHEN g.status = 'accepted' THEN 1 ELSE 0 END)    AS n_accepted,
       SUM(CASE WHEN g.suggested_question <> '' THEN 1 ELSE 0 END) AS n_with_study
FROM gap_register g
GROUP BY g.gap_type;
"""


def migrate_to_v9(connection) -> None:
    """Allow a figure to be a PDF.

    SQLite cannot widen a CHECK constraint in place, so the table is rebuilt:
    new table, copy the rows, drop the old, rename, put the index back. The
    rows already in it are a reviewer's record of the figures they have drawn
    and must survive.
    """
    sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'figure'"
    ).fetchone()
    if sql and "'pdf'" in sql[0]:
        return
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.executescript(
        """
        CREATE TABLE figure_new (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            kind            TEXT    NOT NULL
                            CHECK (kind IN ('evidence_map','integrated')),
            theme_id        INTEGER REFERENCES theme(id),
            filename        TEXT    NOT NULL,
            format          TEXT    NOT NULL DEFAULT 'svg'
                            CHECK (format IN ('svg','png','pptx','pdf')),
            n_constructs    INTEGER NOT NULL DEFAULT 0,
            n_links         INTEGER NOT NULL DEFAULT 0,
            n_crossings     INTEGER NOT NULL DEFAULT 0,
            grades          TEXT    NOT NULL DEFAULT '{}',
            run_id          INTEGER,
            created_at      TEXT    NOT NULL
        );
        INSERT INTO figure_new
            SELECT id, kind, theme_id, filename, format, n_constructs,
                   n_links, n_crossings, grades, run_id, created_at
              FROM figure;
        DROP TABLE figure;
        ALTER TABLE figure_new RENAME TO figure;
        CREATE INDEX IF NOT EXISTS ix_figure_theme ON figure(theme_id, kind);
        """
    )
    connection.execute("PRAGMA foreign_keys = ON")


def migrate_to_v8(connection) -> None:
    """Bring a v7 project forward: the Gap Finder's own tables.

    Additive. The three new project columns default to empty rather than to
    anything derived: the search date in particular is the reviewer's to
    state, and guessing it from when the software ran would put a false
    qualifier on every claim of absence the review makes.
    """
    existing = {row[1] for row in connection.execute("PRAGMA table_info(project)")}
    for column in ("searched_to", "theories", "stimulus_attributes"):
        if column not in existing:
            connection.execute(
                f"ALTER TABLE project ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
            )
    connection.executescript(GAP_RUN_SQL)


def migrate_to_v7(connection) -> None:
    """Bring a v6 project forward: the framework builder.

    Additive only. Nothing the synthesis wrote is touched, because the
    framework reads the graded inventory and never changes it.
    """
    connection.executescript(FRAMEWORK_SQL)


def migrate_to_v6(connection) -> None:
    """Bring a v5 project forward: the synthesis engine.

    Additive apart from the review queue, which gains two reasons a mapping
    can be sent to a human and so has to be rebuilt; the rebuild keeps every
    row a reviewer has already decided.
    """
    if rebuild_review_queue(connection):
        connection.executescript(SCREENING_VIEWS_SQL + FULLTEXT_SQL)
    connection.executescript(SYNTHESIS_SQL)


FRAMEWORK_DEFINITION_SQL = """
CREATE TABLE IF NOT EXISTS framework_definition (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    name_norm   TEXT    NOT NULL UNIQUE,
    -- JSON array, in order. The order is the framework: the Gap Finder
    -- reports the transitions between consecutive stages.
    stages      TEXT    NOT NULL,
    note        TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);
"""


def migrate_to_v11(connection) -> None:
    """Reviewer-defined question frameworks, and the a priori theory.

    Three changes. The `framework_definition` table holds a framework a
    reviewer defined, with its stages. Two columns on `project` record
    which theory the a priori constructs were drawn from and its citation.
    And the CHECK constraint on `framework_type` goes, because it listed
    four names and the whole point is that the list is no longer closed.

    Dropping a CHECK means rebuilding the table, which SQLite will do only
    by copy-and-swap. The project row carries the reviewer's protocol
    fields, so losing it would lose their review: the copy names every
    column explicitly and the test for this migration asserts the settings
    survive.
    """
    connection.executescript(FRAMEWORK_DEFINITION_SQL)

    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(project)").fetchall()
    }
    for column in ("a_priori_theory", "a_priori_citation"):
        if column not in columns:
            connection.execute(
                f"ALTER TABLE project ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
            )

    existing = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'project'"
    ).fetchone()
    if not existing or "CHECK (framework_type IN" not in (existing[0] or ""):
        return

    # Carry over whatever the table has now, in its own order, so that a
    # project migrated from any earlier version copies cleanly.
    present = [
        row[1] for row in connection.execute("PRAGMA table_info(project)").fetchall()
    ]
    names = ", ".join(present)
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.executescript(
            PROJECT_TABLE_SQL.replace(
                "CREATE TABLE IF NOT EXISTS project",
                "CREATE TABLE project_new",
            )
        )
        connection.execute(
            f"INSERT INTO project_new ({names}) SELECT {names} FROM project"
        )
        connection.execute("DROP TABLE project")
        connection.execute("ALTER TABLE project_new RENAME TO project")
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def migrate_to_v10(connection) -> None:
    """Record how the reviewer wants the framework built.

    Two columns added to `project`. Both are additive, so a reviewer's
    half-screened corpus opens untouched, and the default says they were
    working bottom-up -- which they were, because until this version that
    was the only thing the application could do.
    """
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(project)").fetchall()
    }
    if "framework_approach" not in columns:
        connection.execute(
            "ALTER TABLE project ADD COLUMN framework_approach TEXT NOT NULL "
            "DEFAULT 'bottom_up' "
            "CHECK (framework_approach IN ('bottom_up','top_down'))"
        )
    if "a_priori_constructs" not in columns:
        connection.execute(
            "ALTER TABLE project ADD COLUMN a_priori_constructs TEXT NOT NULL "
            "DEFAULT ''"
        )


SCHEMA_SQL = (
    BASE_SQL + FRAMEWORK_DEFINITION_SQL + SCREENING_SQL + FULLTEXT_SQL + EXTRACTION_SQL + GAP_SQL
    + SYNTHESIS_SQL + FRAMEWORK_SQL + GAP_RUN_SQL
)

#: Applied in order to a project written by an older build. The key is the
#: version the migration brings the database *to*. A value is either a script
#: to run, or a function taking the connection when a step needs to look at
#: the database before deciding what to do.
MIGRATIONS: dict[int, "str | Callable[[Any], None]"] = {
    2: SCREENING_SQL,
    3: migrate_to_v3,
    4: migrate_to_v4,
    5: migrate_to_v5,
    6: migrate_to_v6,
    7: migrate_to_v7,
    8: migrate_to_v8,
    9: migrate_to_v9,
    10: migrate_to_v10,
    11: migrate_to_v11,
}
