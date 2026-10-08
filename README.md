# Rotto backend task — UK job crawler (Indeed UK, LinkedIn, Reed)

```bash
cp .env.example .env              # proxy credentials (or export PROXY_* / PROXY_URL)
docker compose up -d              # Postgres 16
make crawl                        # live crawl; run again for an incremental run
make test                         # lifecycle fixture (FIXTURE_PATH=/path/to/file.json make test)
```
Output: `output/jobs.jsonl` (every stored row, removed included) and `output/report.json`, rewritten each run.

## How it works
Three layers, kept apart: **fetch** (`crawler/fetch.py`: HTTP, proxy, retries, block detection, request log) →
**parse** (`crawler/sources/{reed,indeed,linkedin}.py`: pure functions HTML → fields) →
**persist** (`crawler/persist.py`: the only writer of `jobs`; also used by `make test`). `crawler/run.py` decides what to fetch.

1. **Search** — each board caps pagination (~1,000 results on Indeed/LinkedIn), so coverage comes from shards:
   UK-wide, 50 cities, and keyword × big-city splits (LinkedIn also splits by job type). Pages are checkpointed in
   `crawl_progress`; an interrupted run resumes from the last page instead of restarting.
2. **Cards** — every search card goes to `listing_cards` with a fingerprint of its visible fields. Seeing a card
   advances `last_seen_at` of an existing job.
3. **Details** — fetched only when the job is new, its card fingerprint changed, it reappeared, or its refresh is due.
   Each detail result is committed immediately, so a crash never loses finished work.
4. **Dedupe** after every run, then export + report.

**Refresh policy.** Card change = reliable signal → refetch now. Otherwise a detail refresh every `REFRESH_DAYS`
(default 3, + up to 1 day jitter so refreshes spread across runs). A refresh is how detail-only edits and expiry of
listings that fell out of search results get discovered.

**Removal.** `removed_at` is set only on evidence: an explicit "expired / no longer accepting applications" page,
or a 404/410 confirmed from a second IP. Blocked pages, failed pagination, absence from search and cut-short crawls
never remove anything. Absence removes only via `remove_absent_from_complete_scope`, which the live crawler never
calls because no search listing is authoritative. A removed job that is seen again is restored with its original
`first_seen_at`.

**Dedupe.** Block by (company, title) and (title, city); score pairs by description word-set containment (wording
differs across boards) plus same city / salary / date bonuses; greedy one-to-one matching per pair of boards so
"Tesco — Customer Assistant — London" ×30 does not collapse into one job. Groups keep the canonical id of their oldest
member, so ids are stable across runs.

## Getting past bot protection
- `curl_cffi` impersonating Chrome/Safari TLS + HTTP/2 fingerprints (plain `requests` is fingerprinted and blocked).
- Oxylabs sticky sessions (`-sessid-…` on the username): a worker keeps one UK residential IP and its cookies until
  blocked, then rotates IP and fingerprint. Per-board pacing; a breaker pauses a board when >80% of recent requests
  are blocked instead of burning proxy traffic.
- LinkedIn via its public guest endpoints (`jobs-guest/...`), which are logged-out and ~10× smaller than `/jobs/view`.
- Every attempt is logged (`request_log` table + `logs/requests-<run>.jsonl`) with status, block reason and bytes.

**What didn't work:** _TODO — fill in from your own runs (e.g. block rates you saw before/without impersonation)._

## Tradeoffs
- Correctness over volume: jobs without a full description, or not verifiably UK (remote roles must say they're
  open to UK workers), are skipped rather than stored. Volume is capped per run (`MAX_DETAILS_PER_SOURCE`, time budget).
- `posted_at` is excluded from `content_hash`: relative dates ("3 days ago") drift, which would fake changes.
- Monthly/weekly salaries are annualised (×12/×52) because the schema allows only year/day/hour.
- Work type with several labels ("Contract, full-time") resolves to the more specific one (contract/temporary).

## Cost per 1,000 pages
_TODO — from `output/report.json`: `bytes_total / requests_total × 1000 / 1e9 × $/GB`._ Estimated at
`PROXY_COST_PER_GB` (default $8/GB, residential list price).
