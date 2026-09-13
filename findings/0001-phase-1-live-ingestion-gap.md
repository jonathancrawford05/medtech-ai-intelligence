# 0001 — Phase 1 acceptance is unmet: the pipeline has never run against the live FDA source

**Date:** 2026-09-07 · **Status:** **Closed 2026-09-13** · **Component:** `ingest/fda_ai_list.py`

**Closed by [finding 0006](0006-phase-1-acceptance-met.md):** a full-sized bronze
write ran both locally and on CI — 1,614 rows fetched, parsed and written, 100%
against the plan's 95% criterion. The body below is left intact as the record of
what was open and why.

**Updated 2026-09-13:** acquisition is now verified by *execution* — a live
dry run fetched and parsed all 1,614 rows from a developer machine. See
[finding 0005](0005-first-live-ingest-attempt.md). Only the bronze write
remains unexecuted; it was blocked by a missing JVM, now guarded.

## The finding

The FDA acquisition path was **verified by inspection, not by execution.**

[ADR 0009](../docs/adr/0009-fda-ai-list-acquisition-verified.md) and
`CONTINUATION.md` §2 both mark the FDA source "verified live ✅", and that is
accurate for what it claims: someone opened the page in a browser on an
unrestricted network and confirmed the export URL, the column headers and the row
count. That was a real and valuable check — it caught that the guessed
`?export=csv` URLs 404 and that every run had been silently falling back to HTML
scraping.

But no one has **run the code** against the live source. `fetch_raw` →
`parse_csv` → `ingest_snapshot` → `bronze_fda_ai_list` has only ever executed
against fixtures.

This matters because the ✅ in `CONTINUATION.md` §2 is easy to read as "Phase 1
acceptance met". It is not. The development plan's Phase 1 acceptance criterion is:

> running it live (manually, not in CI) against the real FDA list succeeds without
> errors for **at least 95% of rows**

That number has never been measured. No bronze table has ever held 1,615 rows.

## Verified vs. assumed

| Claim | Status |
|-------|--------|
| CSV export exists at `https://www.fda.gov/media/178541/download?attachment` | **Verified** — browser, 2026-09-06 (ADR 0009) |
| Headers match `_HEADER_ALIASES` with no changes needed | **Verified** — browser, 2026-09-06 |
| Export contains the full list, 1,615 data rows | **Verified** — browser, 2026-09-06 |
| `parse_csv` handles all 1,614 real rows | **Verified** — live dry run, 2026-09-13 (finding 0005) |
| The known CSV URL serves it without falling back | **Verified** — live dry run, 2026-09-13 |
| ≥95% of rows parse without error | **Verified** — 1,614 fetched, 1,614 parsed, no errors |
| A bronze write of the full list succeeds | **Still never executed** — needs a JVM |
| `_discover_csv_url` finds the real link on the real page | **Assumed** — tested against a fixture only |

The 14-row fixture is a genuine slice of the real export, which is much better than
synthetic data. But 14 rows out of 1,615 will not contain every malformed date,
every unusual applicant string, or every supplement-suffixed PMA number.

## Why it is still open

`fda.gov` is blocked at the network egress policy of both the agent build
environment and the dev VM (`CONTINUATION.md` §2). Confirmed here on 2026-09-06:
`CONNECT` to `www.fda.gov:443` and `api.fda.gov:443` both return `403` from the
proxy. So this cannot be closed from an agent session as currently configured.

## How to close it

On a host that can reach `fda.gov`:

```bash
uv run registry ingest-fda-list --dry-run --verbose   # fetch + parse, write nothing
uv run registry ingest-fda-list --verbose             # then write bronze
```

Record in a follow-up finding:

1. Which acquisition tier actually served the request — the known CSV URL, the
   discovered link, or the HTML fallback. If tier 1 failed, the media id has
   already drifted and `settings.fda_ai_list_csv_url` needs updating.
2. Rows fetched vs. rows written. Anything below 95% fails the Phase 1 criterion;
   the skipped rows and why are the interesting part.
3. Whether the row count is still ~1,615.
4. A spot-check of the development plan's named devices — CaRi-Heart, AVIEW CAC,
   AI-CVD — which is also the Phase 2 acceptance check.

Then enlarge the fixture with any row shape that broke, and update
`CONTINUATION.md` §1.

## Related

- The weekly `live-network` CI job (`.github/workflows/ci.yml`) runs
  `pytest -m live_network` on a schedule and will catch *acquisition* drift.
  It does not exercise the bronze write, so it does not close this finding.
