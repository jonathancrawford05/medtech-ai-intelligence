# 0005 — Prefer the FDA CSV export; scrape HTML only as fallback

**Status:** Accepted, **unverified against the live site** · **Date:** 2026-09-06
**Relates to:** open question 1 in the development plan (§7)

## Context

The FDA's AI-enabled device list is the primary source. The development plan
assumed it was a maintained HTML table and suggested scraping it with
`pandas.read_html` or BeautifulSoup. The plan also flagged that the page has
moved and been reorganised before (the July 2025 note), so resilience matters
more than elegance here.

Research during this build indicates the FDA page offers **CSV, Excel and XML
downloads** alongside the rendered table, which is a materially better
acquisition path than scraping markup.

**Important caveat:** the build environment's network policy blocks `fda.gov`
and `api.fda.gov`, so this could not be confirmed against the live site. The
exact export URL and column headers are, as of this ADR, *inferred*.

## Decision

Try acquisition strategies in order and stop at the first that works:

1. `…/artificial-intelligence-enabled-medical-devices?export=csv`
2. `…/artificial-intelligence-enabled-medical-devices/export?format=csv`
3. the page itself, parsed as an HTML table (fallback)

The response's `Content-Type` decides how the payload is parsed, not the URL we
happened to ask for. A 404 on an export candidate is treated as "that shape is
gone, try the next" rather than a transient failure; 5xx and network errors are
retried with exponential backoff.

Columns are matched through an **alias table** (`_HEADER_ALIASES`) on normalised
header text, so `"Panel (lead)"`, `"Lead Panel"` and `"panel_lead"` all resolve.
If no submission-number column can be found, ingestion raises
`SourceFormatError` naming the headers it saw — a loud failure rather than a
table full of empty rows.

## Consequences

- A page redesign degrades to the HTML fallback instead of breaking ingestion.
- A column rename is a one-line addition to the alias table.
- Parsing is fully testable from fixtures, with no live network in CI.
- The candidate URLs and the exact headers **must be verified on the first live
  run**. See `CONTINUATION.md`; this is the top open item.

## Alternatives considered

**Scrape the HTML table only,** as the plan suggested. Rejected as the primary
path: parsing a published export is far less brittle than parsing markup that
exists to be looked at. Kept as the fallback, which is where it earns its place.

**`pandas.read_html`.** Convenient, but it drags pandas into the ingestion path
purely as a parser and gives less control over messy rows than the explicit
BeautifulSoup walk plus alias mapping.
