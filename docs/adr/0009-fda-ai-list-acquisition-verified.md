# 0009 — FDA AI list acquisition: known CSV URL + link discovery

**Status:** Accepted · **Date:** 2026-09-06
**Supersedes:** [0005](0005-fda-ai-list-acquisition.md)
**Relates to:** CONTINUATION.md §2 (the top open item, now closed)

## Context

ADR 0005 chose "CSV export first, HTML table as fallback" but could not verify it:
the build environment blocked `fda.gov`, so the export URL and column headers were
*inferred*. The live site was inspected on 2026-09-06 (via a browser on an
unrestricted network) and the inferred parts turned out to be partly wrong.

What the live page actually shows:

- It **does** offer downloadable exports — CSV, Excel and XML — confirming the
  core premise of ADR 0005.
- The CSV is served from **`https://www.fda.gov/media/178541/download?attachment`**,
  reached via a "Download a CSV File" link. It is **not** a query-string variant
  of the page URL. The two URLs ADR 0005 guessed —
  `…/artificial-intelligence-enabled-medical-devices?export=csv` and
  `…/export?format=csv` — both 404, so the old code silently fell through to HTML
  scraping on every run.
- The CSV headers are exactly
  `Date of Final Decision, Submission Number, Device, Company, Panel (Lead),
  Primary Product Code`, which the existing `_HEADER_ALIASES` table already maps.
  No alias changes were needed.
- The export holds the **full list** — 1,615 data rows (~1,614 table entries) —
  not a truncated page. The rendered table is a client-side DataTable: it paginates
  the *live DOM* to 50 rows, but the raw server HTML embeds every row, so even the
  HTML fallback is not truncated.

## Decision

Acquire the list in this order, stopping at the first that works:

1. **The known CSV export URL** (`settings.fda_ai_list_csv_url`, default the media
   URL above) — the fast path, one request.
2. **A CSV link discovered on the page.** If the known URL fails, fetch the page
   and scrape the "Download a CSV File" link (`_discover_csv_url`), then fetch it.
   FDA media ids can change when the list is republished; discovery means a
   re-upload degrades to a second lookup instead of breaking ingestion.
3. **The rendered HTML table** (`parse_html`) — the last-resort fallback, using the
   page we already fetched in step 2.

The response `Content-Type` still decides how a payload is parsed, not the URL we
asked for. Transient (5xx / network) failures retry with exponential backoff; a
4xx moves straight to the next source.

Fixtures are now a real slice of the export captured on the verification date
(`tests/fixtures/fda_ai_list_sample.csv`, 14 rows spanning 510(k)/De Novo/PMA,
quoted fields, a PMA supplement number `P130020/S005`, and the oldest 1995 entry).
The hand-written defensive fixture (`fda_ai_list_messy.csv`) is kept as-is — it
encodes edge cases the real slice does not. A `@pytest.mark.live_network` test
exercises the real endpoint and is never run in CI.

## Consequences

- The CSV export is now actually used, instead of always falling back to HTML.
- A media-id change costs one extra request (discovery), not an outage.
- A full page redesign still degrades to HTML parsing, as before.
- `settings.fda_ai_list_csv_url` is overridable, so a future id change can be fixed
  by env var even before the page is re-scraped.

## Alternatives considered

**Hard-code the media URL as the only source.** Rejected: the id is stable *enough*
to be a good default but not forever; with no discovery, a re-upload would silently
fall back to brittle HTML scraping.

**Discovery only, no known URL.** Rejected: it forces two requests on every run for
a URL that is stable in practice. The known URL is the fast path; discovery is the
safety net.
