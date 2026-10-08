-- Idempotent schema: applied at the start of every run.

CREATE TABLE IF NOT EXISTS jobs (
    id                text PRIMARY KEY,          -- {source}:{source_job_id}
    source            text NOT NULL,
    source_job_id     text NOT NULL,
    url               text NOT NULL,
    title             text NOT NULL,
    company           text,
    company_logo_url  text,
    location          text,
    work_type         text,
    salary_min        numeric,
    salary_max        numeric,
    salary_period     text,
    posted_at         timestamptz,
    description       text,
    canonical_job_id  text NOT NULL,
    content_hash      text NOT NULL,
    first_seen_at     timestamptz NOT NULL,
    last_seen_at      timestamptz NOT NULL,
    removed_at        timestamptz,
    -- bookkeeping (not part of the graded contract)
    removal_evidence  text,
    card_hash         text,                      -- fingerprint of the search card last seen
    last_detail_at    timestamptz,               -- last successful detail fetch
    next_refresh_at   timestamptz,               -- when the detail page is due again
    updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_source_idx    ON jobs (source);
CREATE INDEX IF NOT EXISTS jobs_canonical_idx ON jobs (canonical_job_id);
CREATE INDEX IF NOT EXISTS jobs_refresh_idx   ON jobs (source, next_refresh_at) WHERE removed_at IS NULL;

-- Search-result cards. A card is a cheap signal that a listing is live; the jobs
-- table only receives a row once the full detail page has been parsed.
CREATE TABLE IF NOT EXISTS listing_cards (
    id             text PRIMARY KEY,
    source         text NOT NULL,
    source_job_id  text NOT NULL,
    url            text NOT NULL,
    card           jsonb NOT NULL,
    card_hash      text NOT NULL,
    first_seen_at  timestamptz NOT NULL,
    last_seen_at   timestamptz NOT NULL,
    last_run_id    bigint,
    needs_detail   boolean NOT NULL DEFAULT true,
    detail_failures int NOT NULL DEFAULT 0,
    invalid_reason text                          -- e.g. not_uk; skipped for storage
);
CREATE INDEX IF NOT EXISTS cards_pending_idx ON listing_cards (source) WHERE needs_detail;

CREATE TABLE IF NOT EXISTS crawl_runs (
    id           bigserial PRIMARY KEY,
    started_at   timestamptz NOT NULL,
    finished_at  timestamptz,
    status       text NOT NULL,                 -- running | finished | interrupted
    resumed_from bigint,
    report       jsonb
);

-- Search pagination checkpoints, so an interrupted crawl resumes instead of restarting.
CREATE TABLE IF NOT EXISTS crawl_progress (
    run_id     bigint NOT NULL,
    source     text NOT NULL,
    shard      text NOT NULL,
    last_page  int NOT NULL,
    done       boolean NOT NULL DEFAULT false,
    complete   boolean NOT NULL DEFAULT false,  -- ended naturally (no blocks/failures)
    PRIMARY KEY (run_id, source, shard)
);

CREATE TABLE IF NOT EXISTS request_log (
    id           bigserial PRIMARY KEY,
    run_id       bigint,
    ts           timestamptz NOT NULL,
    source       text,
    kind         text,                          -- search | detail | confirm
    url          text NOT NULL,
    attempt      int NOT NULL,
    status       int,                           -- HTTP status, null on network error
    blocked      boolean NOT NULL,
    reason       text,                          -- block / error reason
    bytes        bigint NOT NULL DEFAULT 0,
    elapsed_ms   int
);
CREATE INDEX IF NOT EXISTS request_log_run_idx ON request_log (run_id);
