"""Ingest the FDA's curated AI-enabled medical device list into bronze.

Acquisition strategy
--------------------
The FDA page offers CSV/Excel/XML exports alongside the rendered HTML table, so
we try the **CSV export first** and fall back to scraping the HTML table only if
the export 404s. Parsing a published export is far less brittle than scraping
markup, and the fallback means a page redesign degrades rather than breaks us.

The page has moved and been reorganised before, so the parser is tolerant by
design: columns are matched through an alias table rather than by exact header
text, and an unrecognised header set raises a loud, specific error instead of
silently producing empty rows.

Bronze is append-only. Every pull is stamped with an ``ingested_at`` timestamp
and a ``source_snapshot_id`` (a content hash), so we retain a full history of
what the FDA's list looked like at each pull.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import logging
import time
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urljoin

import httpx

from registry.config.settings import Settings, get_settings
from registry.schemas import BronzeFdaAiListRecord, spark_schema_for

logger = logging.getLogger(__name__)

BRONZE_TABLE = "bronze_fda_ai_list"

ContentKind = Literal["csv", "html"]


class IngestError(RuntimeError):
    """Base class for ingestion failures."""


class SourceUnavailableError(IngestError):
    """The FDA endpoint could not be reached after retries."""


class SourceFormatError(IngestError):
    """The payload arrived but did not look like the list we expect."""


# ---------------------------------------------------------------------------
# Column mapping
# ---------------------------------------------------------------------------

# Normalised header text -> bronze field name. Normalisation lowercases, strips,
# and collapses non-alphanumerics, so "Panel (lead)" and "panel_lead" both match.
_HEADER_ALIASES: dict[str, str] = {
    "submissionnumber": "submission_number",
    "submissionno": "submission_number",
    "510kdenpmanumber": "submission_number",
    "510knumber": "submission_number",
    "knumber": "submission_number",
    "device": "device_name",
    "devicename": "device_name",
    "tradename": "device_name",
    "company": "applicant_raw",
    "applicant": "applicant_raw",
    "companyapplicant": "applicant_raw",
    "dateoffinaldecision": "decision_date_raw",
    "decisiondate": "decision_date_raw",
    "datedecision": "decision_date_raw",
    "panellead": "panel_raw",
    "panel": "panel_raw",
    "leadpanel": "panel_raw",
    "advisorycommittee": "panel_raw",
    "primaryproductcode": "product_code",
    "productcode": "product_code",
}

_ACCESSDATA = "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs"


def _normalise_header(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _map_headers(headers: list[str]) -> dict[int, str]:
    """Map column index -> bronze field name, ignoring columns we do not use."""
    mapping: dict[int, str] = {}
    for idx, raw in enumerate(headers):
        field_name = _HEADER_ALIASES.get(_normalise_header(raw or ""))
        if field_name and field_name not in mapping.values():
            mapping[idx] = field_name
    if "submission_number" not in mapping.values():
        raise SourceFormatError(
            "Could not find a submission number column in the FDA list. "
            f"Headers were: {headers!r}. If the FDA renamed this column, add the "
            "new name to _HEADER_ALIASES."
        )
    return mapping


def source_url_for(submission_number: str) -> str:
    """Best-effort deep link back to the FDA record, for auditability."""
    num = submission_number.upper()
    if num.startswith("DEN"):
        return f"{_ACCESSDATA}/cfpmn/denovo.cfm?ID={num}"
    if num.startswith("P"):
        return f"{_ACCESSDATA}/cfpma/pma.cfm?id={num}"
    return f"{_ACCESSDATA}/cfpmn/pmn.cfm?ID={num}"


def snapshot_id(payload: bytes) -> str:
    """Content hash identifying a particular pull of the source."""
    return hashlib.sha256(payload).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _row_to_record(
    values: dict[str, str], snapshot_id: str, ingested_at: dt.datetime
) -> BronzeFdaAiListRecord | None:
    """Build a bronze record, or None if the row has no usable key."""
    submission = (values.get("submission_number") or "").strip().upper()
    if not submission:
        return None

    def clean(key: str) -> str | None:
        val = (values.get(key) or "").strip()
        return val or None

    product_code = clean("product_code")
    return BronzeFdaAiListRecord(
        submission_number=submission,
        device_name=clean("device_name"),
        applicant_raw=clean("applicant_raw"),
        decision_date_raw=clean("decision_date_raw"),
        panel_raw=clean("panel_raw"),
        product_code=product_code.upper() if product_code else None,
        source_url=source_url_for(submission),
        ingested_at=ingested_at,
        source_snapshot_id=snapshot_id,
    )


def _records_from_rows(
    header: list[str],
    rows: list[list[str]],
    snapshot_id: str,
    ingested_at: dt.datetime,
) -> list[BronzeFdaAiListRecord]:
    mapping = _map_headers(header)
    records = []
    for row in rows:
        if not any(cell.strip() for cell in row):
            continue  # blank separator row
        values = {name: row[idx] for idx, name in mapping.items() if idx < len(row)}
        record = _row_to_record(values, snapshot_id, ingested_at)
        if record is None:
            logger.debug("Skipping FDA list row without a submission number: %r", row)
            continue
        records.append(record)
    return records


def parse_csv(
    payload: bytes, *, snapshot_id: str, ingested_at: dt.datetime
) -> list[BronzeFdaAiListRecord]:
    """Parse the FDA CSV export into bronze records."""
    text = payload.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise SourceFormatError("FDA list CSV export was empty.") from None
    return _records_from_rows(header, list(reader), snapshot_id, ingested_at)


def parse_html(
    payload: bytes, *, snapshot_id: str, ingested_at: dt.datetime
) -> list[BronzeFdaAiListRecord]:
    """Parse the rendered HTML table -- the fallback when the export is gone."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(payload, "lxml")
    table = soup.find("table")
    if table is None:
        raise SourceFormatError(
            "No <table> found on the FDA AI-enabled device list page. "
            "The page layout may have changed."
        )

    header_cells = table.find_all("th")
    if not header_cells:
        first_row = table.find("tr")
        header_cells = first_row.find_all("td") if first_row else []
    header = [c.get_text(strip=True) for c in header_cells]

    rows = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        cells = tr.find_all("td")
        if cells:
            rows.append([c.get_text(strip=True) for c in cells])

    return _records_from_rows(header, rows, snapshot_id, ingested_at)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RawSnapshot:
    """An immutable capture of one pull, before any interpretation."""

    payload: bytes
    content_kind: ContentKind
    url: str
    fetched_at: dt.datetime = field(
        default_factory=lambda: dt.datetime.now(dt.UTC).replace(tzinfo=None)
    )

    @property
    def snapshot_id(self) -> str:
        return snapshot_id(self.payload)

    def parse(self) -> list[BronzeFdaAiListRecord]:
        parser = parse_csv if self.content_kind == "csv" else parse_html
        return parser(self.payload, snapshot_id=self.snapshot_id, ingested_at=self.fetched_at)


def _fetch_with_retries(
    client: httpx.Client, url: str, settings: Settings
) -> httpx.Response | None:
    """GET ``url``, retrying transient (5xx / network) failures with backoff.

    Returns the 200 response, or ``None`` if the URL is unavailable: a 4xx means
    "this shape is gone, try the next source" and is not retried; 5xx and network
    errors are retried up to ``http_max_retries`` times before giving up on it.
    """
    for attempt in range(settings.http_max_retries):
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            logger.warning("Network error fetching %s: %s", url, exc)
        else:
            if response.status_code == 200 and response.content:
                return response
            if response.status_code < 500:
                logger.info("%s returned %s; moving on", url, response.status_code)
                return None
            logger.warning("%s returned %s", url, response.status_code)

        if attempt < settings.http_max_retries - 1:
            backoff = settings.http_backoff_seconds * (2**attempt)
            logger.warning("Retrying %s in %.1fs", url, backoff)
            if backoff:
                time.sleep(backoff)
    return None


def _discover_csv_url(page_html: bytes, page_url: str) -> str | None:
    """Find the "Download a CSV File" link on the rendered page.

    The FDA serves the list as a downloadable file behind a ``/media/NNNNN/download``
    link whose id can change when the page is republished. Rather than hard-coding
    that id forever, we scrape the link off the page as a fallback, so a re-upload
    degrades to discovery instead of breaking ingestion.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(page_html, "lxml")
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True).lower()
        href = anchor["href"]
        looks_like_csv = "csv" in text or "csv" in href.lower()
        looks_like_download = "download" in text or "/media/" in href.lower()
        if looks_like_csv and looks_like_download:
            resolved = urljoin(page_url, href)
            logger.info("Discovered CSV export link on the page: %s", resolved)
            return resolved
    return None


def fetch_raw(
    settings: Settings | None = None, *, client: httpx.Client | None = None
) -> RawSnapshot:
    """Fetch the list as CSV if at all possible, falling back to the HTML table.

    Acquisition order (stop at the first that works):

    1. the known CSV export URL (``settings.fda_ai_list_csv_url``) -- the fast path;
    2. the CSV link *discovered* on the rendered page, if the known URL is gone;
    3. the rendered page itself, parsed as an HTML table (last-resort fallback).

    The response ``Content-Type`` -- not the URL we asked for -- decides how the
    payload is parsed. Transient (5xx / network) failures are retried with
    exponential backoff; a 4xx moves straight on to the next source.
    """
    settings = settings or get_settings()
    owns_client = client is None
    client = client or httpx.Client(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": settings.http_user_agent},
        follow_redirects=True,
    )

    try:
        # 1. Known CSV export URL -- the fast path.
        if settings.fda_ai_list_csv_url:
            response = _fetch_with_retries(client, settings.fda_ai_list_csv_url, settings)
            if response is not None:
                return _snapshot_from(response, "csv", settings.fda_ai_list_csv_url)

        # 2 & 3. Fetch the page; discover a CSV link on it, else parse it as HTML.
        page = _fetch_with_retries(client, settings.fda_ai_list_url, settings)
        if page is None:
            raise SourceUnavailableError(
                "Could not fetch the FDA AI-enabled device list: neither the CSV "
                f"export ({settings.fda_ai_list_csv_url}) nor the page "
                f"({settings.fda_ai_list_url}) responded."
            )

        discovered = _discover_csv_url(page.content, str(page.url))
        if discovered and discovered != settings.fda_ai_list_csv_url:
            response = _fetch_with_retries(client, discovered, settings)
            if response is not None:
                return _snapshot_from(response, "csv", discovered)

        # Last resort: the rendered table we already have in hand.
        return _snapshot_from(page, "html", str(page.url))
    finally:
        if owns_client:
            client.close()


def _snapshot_from(response: httpx.Response, requested: ContentKind, url: str) -> RawSnapshot:
    """Build a snapshot, trusting the response content-type over the requested kind."""
    resolved = _resolve_kind(response, requested)
    logger.info("Fetched FDA list from %s as %s", url, resolved)
    return RawSnapshot(payload=response.content, content_kind=resolved, url=url)


def _resolve_kind(response: httpx.Response, requested: ContentKind) -> ContentKind:
    """Trust the response's content type over the URL we happened to ask for."""
    content_type = response.headers.get("content-type", "").lower()
    if "csv" in content_type:
        return "csv"
    if "html" in content_type:
        return "html"
    return requested


# ---------------------------------------------------------------------------
# Bronze write
# ---------------------------------------------------------------------------


def ingest_snapshot(spark, snapshot: RawSnapshot, settings: Settings | None = None) -> int:
    """Parse a snapshot and append it to the bronze table. Returns rows written."""
    from registry import tables

    settings = settings or get_settings()
    records = snapshot.parse()
    if not records:
        raise SourceFormatError(
            f"Parsed zero rows from {snapshot.url}; refusing to write an empty bronze pull."
        )

    df = spark.createDataFrame(
        [r.model_dump() for r in records],
        schema=spark_schema_for(BronzeFdaAiListRecord),
    )
    # Append-only: never lose the history of what the source said.
    tables.write_table(df, settings, BRONZE_TABLE, mode="append", merge_schema=True)
    logger.info(
        "Appended %d rows to %s (snapshot %s)", len(records), BRONZE_TABLE, snapshot.snapshot_id
    )
    return len(records)


def run(spark=None, settings: Settings | None = None) -> int:
    """Fetch the live list and append it to bronze. Entry point for the CLI."""
    from registry.spark_session import get_spark

    settings = settings or get_settings()
    spark = spark or get_spark(settings)
    return ingest_snapshot(spark, fetch_raw(settings), settings)
