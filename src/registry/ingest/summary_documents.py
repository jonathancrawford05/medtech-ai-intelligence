"""Fetch 510(k) Summary PDFs into ``bronze_summary_documents`` as per-page text.

Issue 4, PR 4A (ADR 0018). This is the **acquisition** half of the document pass
and the only part that needs ``accessdata.fda.gov``; extraction (predicates,
intended use, cybersecurity) reads the stored text offline and lives in silver.

What finding 0011 measured, and this module encodes:

- **URL.** ``cdrh_docs/pdf{int(yy)}/<K>.pdf``, where ``yy`` is the two-digit year in
  the submission number and has *no leading zero* (``K093456`` → ``pdf9/``); the
  oldest filings sit under bare ``cdrh_docs/pdf/``. Every sampled document resolved
  on the first or second candidate. Only ``K…`` numbers are fetched: De Novo and
  PMA documents live elsewhere and are not verified, so they are **deferred** and
  reported, not guessed at (ADR 0018).
- **Text vs image.** A page with fewer than 100 non-whitespace characters is an
  image page; a document is ``text`` (all pages over), ``image`` (none) or
  ``mixed``. Stored per page so the rule can be re-applied later.
- **Citizenship.** ~1 request/second across every request, and a 429 or 403
  **stops the pass** (after flushing what was fetched) rather than retrying into
  a block. 5xx and network errors are retried with the shared backoff.

Bronze stores what the source served, nothing more: no normalisation of the text
layer happens here. The glyph-splitting it is known for ("Noti fi cation") is
silver's problem (4B), and keeping the raw layer is what lets 4B fix it offline.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import logging
import os
import re
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx
import pypdf

from registry.config.settings import Settings, get_settings
from registry.schemas import BronzeSummaryDocumentRecord, TextClass

logger = logging.getLogger(__name__)

BRONZE_TABLE = "bronze_summary_documents"
ENRICHMENT_TABLE = "silver_device_enrichment"

# Finding 0011's rule: under this many non-whitespace characters, a page has no
# usable text layer (a scan, a blank, a signature page).
IMAGE_PAGE_MAX_CHARS = 100

# Which library produced `page_texts`. Stored on every row: the spike measured with
# pdf.js, and a later re-extraction with another library must be distinguishable.
EXTRACTOR = f"pypdf=={pypdf.__version__}"

# A 510(k) number: K + six digits, the first two being the submission year.
_K_NUMBER = re.compile(r"^K(\d{2})\d{4}$")

# Statuses that mean "stop asking", not "try the next URL" (handoff §1).
_HALT_STATUSES = frozenset({403, 429})


class SummaryFetchError(RuntimeError):
    """Base class for summary-acquisition failures."""


class FetchHaltedError(SummaryFetchError):
    """The server asked us to stop (429) or refused us (403). Do not retry."""

    def __init__(self, status: int, url: str):
        super().__init__(f"accessdata.fda.gov returned {status} for {url}; halting the pass")
        self.status = status
        self.url = url


class SummarySourceUnavailableError(SummaryFetchError):
    """accessdata.fda.gov could not be reached (network / 5xx) after retries."""


class PdfExtractionError(SummaryFetchError):
    """The bytes could not be read as a PDF."""


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


def is_fetchable(submission_number: str) -> bool:
    """True for a ``K…`` 510(k) number -- the only kind whose URL is verified."""
    return bool(_K_NUMBER.match(submission_number.strip().upper()))


def candidate_urls(submission_number: str, base_url: str) -> list[str]:
    """The URLs to try for one Summary, in order: ``pdf{int(yy)}/`` then bare ``pdf/``."""
    number = submission_number.strip().upper()
    match = _K_NUMBER.match(number)
    if match is None:
        raise ValueError(
            f"{submission_number!r} is not a 510(k) K-number; De Novo and PMA documents "
            "are deferred (ADR 0018)"
        )
    base = base_url.rstrip("/")
    return [f"{base}/pdf{int(match.group(1))}/{number}.pdf", f"{base}/pdf/{number}.pdf"]


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def char_count(text: str) -> int:
    """Non-whitespace characters: the spike's measure of a page's text layer."""
    return sum(1 for ch in text if not ch.isspace())


def classify_pages(char_counts: Iterable[int]) -> TextClass | None:
    """``text`` / ``mixed`` / ``image`` from per-page counts; ``None`` with no pages."""
    counts = list(char_counts)
    if not counts:
        return None
    text_pages = sum(1 for c in counts if c >= IMAGE_PAGE_MAX_CHARS)
    if text_pages == len(counts):
        return "text"
    return "image" if text_pages == 0 else "mixed"


@dataclass(slots=True)
class PageExtraction:
    texts: list[str]
    unreadable_pages: list[int]  # 1-based; their entry in `texts` is ""
    first_error: str | None = None


def read_pages(pdf_bytes: bytes) -> PageExtraction:
    """Per-page text, tolerating a page the extractor cannot read.

    A document that will not open at all raises ``PdfExtractionError``. A single
    page that fails (a malformed content stream or font) becomes ``""`` and is
    named in ``unreadable_pages``, so one bad page never costs the whole Summary.
    """
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        pages = list(reader.pages)
    except Exception as exc:  # pypdf raises a wide family; all mean "not readable"
        raise PdfExtractionError(f"{type(exc).__name__}: {exc}") from exc

    result = PageExtraction(texts=[], unreadable_pages=[])
    for number, page in enumerate(pages, start=1):
        try:
            result.texts.append(page.extract_text() or "")
        except Exception as exc:
            result.texts.append("")
            result.unreadable_pages.append(number)
            result.first_error = result.first_error or f"{type(exc).__name__}: {exc}"
    return result


def extract_pages(pdf_bytes: bytes) -> list[str]:
    """One string per page, verbatim from the text layer (empty for a scanned or
    unreadable page)."""
    return read_pages(pdf_bytes).texts


def _looks_like_pdf(response: httpx.Response) -> bool:
    """Content decides, not the URL: a 200 HTML page at a ``.pdf`` path is not a Summary."""
    if "pdf" in response.headers.get("content-type", "").lower():
        return True
    return b"%PDF-" in response.content[:1024]


# ---------------------------------------------------------------------------
# One fetched document
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SummaryDocument:
    """What one fetch produced, before it is stamped for bronze."""

    submission_number: str
    url: str
    urls_tried: list[str]
    http_status: int
    fetched_at: dt.datetime
    content_type: str | None = None
    content_sha256: str | None = None
    byte_count: int | None = None
    page_texts: list[str] = field(default_factory=list)
    extractor: str | None = None
    extraction_error: str | None = None

    @property
    def found(self) -> bool:
        return self.content_sha256 is not None

    @property
    def page_count(self) -> int:
        return len(self.page_texts)

    @property
    def page_char_counts(self) -> list[int]:
        return [char_count(text) for text in self.page_texts]

    @property
    def text_class(self) -> TextClass | None:
        return classify_pages(self.page_char_counts)


def to_record(doc: SummaryDocument, *, ingested_at: dt.datetime) -> BronzeSummaryDocumentRecord:
    """Stamp a fetched document as a bronze row."""
    return BronzeSummaryDocumentRecord(
        submission_number=doc.submission_number,
        url=doc.url,
        urls_tried=doc.urls_tried,
        http_status=doc.http_status,
        content_type=doc.content_type,
        content_sha256=doc.content_sha256,
        byte_count=doc.byte_count,
        page_count=doc.page_count,
        page_texts=doc.page_texts,
        page_char_counts=doc.page_char_counts,
        text_class=doc.text_class,
        extractor=doc.extractor,
        extraction_error=doc.extraction_error,
        fetched_at=doc.fetched_at,
        ingested_at=ingested_at,
        source_snapshot_id=doc.content_sha256[:16] if doc.content_sha256 else None,
    )


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------


class SummaryFetcher:
    """Throttled fetch of one Summary at a time, with the ``pdf/`` fallback.

    ``sleep`` and ``clock`` are injectable so tests can assert the throttle without
    spending wall-clock time.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._settings = settings or get_settings()
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=self._settings.http_timeout_seconds,
            headers={"User-Agent": self._settings.http_user_agent},
            follow_redirects=True,
        )
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None
        archive = self._settings.summary_pdf_archive_dir
        self._archive_dir = Path(archive) if archive else None

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> SummaryFetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- public API --------------------------------------------------------
    def fetch(self, submission_number: str) -> SummaryDocument:
        """Fetch one Summary. A miss is a returned document, not an exception.

        Raises ``FetchHaltedError`` on 429/403 and ``SummarySourceUnavailableError``
        when retries are exhausted -- both mean "stop the pass", not "skip this one".
        """
        number = submission_number.strip().upper()
        urls = candidate_urls(number, self._settings.summary_documents_base_url)
        tried: list[str] = []
        status = 0
        for url in urls:
            tried.append(url)
            response = self._get(url)
            status = response.status_code
            if status == 200 and response.content and _looks_like_pdf(response):
                return self._document(number, url, tried, response)
            logger.debug("%s: %s returned %s; trying the next candidate", number, url, status)

        logger.info("%s: no Summary PDF at %s (last status %s)", number, tried, status)
        return SummaryDocument(
            submission_number=number,
            url=tried[-1],
            urls_tried=tried,
            http_status=status,
            fetched_at=_utcnow(),
        )

    # -- internals ---------------------------------------------------------
    def _document(
        self, number: str, url: str, tried: list[str], response: httpx.Response
    ) -> SummaryDocument:
        body = response.content
        sha = hashlib.sha256(body).hexdigest()
        self._archive(sha, body)
        doc = SummaryDocument(
            submission_number=number,
            url=url,
            urls_tried=tried,
            http_status=response.status_code,
            fetched_at=_utcnow(),
            content_type=response.headers.get("content-type") or None,
            content_sha256=sha,
            byte_count=len(body),
            extractor=EXTRACTOR,
        )
        try:
            pages = read_pages(body)
        except PdfExtractionError as exc:
            # The bytes arrived and are hashed; say why no text came out of them.
            logger.warning("%s: could not read %s as a PDF: %s", number, url, exc)
            doc.extraction_error = str(exc)
            return doc
        doc.page_texts = pages.texts
        if pages.unreadable_pages:
            listed = ", ".join(str(n) for n in pages.unreadable_pages)
            doc.extraction_error = f"unreadable page {listed} ({pages.first_error})"
            logger.warning("%s: %s", number, doc.extraction_error)
        return doc

    def _throttle(self) -> None:
        interval = self._settings.summary_request_interval_seconds
        now = self._clock()
        if self._last_request is not None:
            wait = interval - (now - self._last_request)
            if wait > 0:
                self._sleep(wait)
                now += wait
        self._last_request = now

    def _get(self, url: str) -> httpx.Response:
        settings = self._settings
        for attempt in range(settings.http_max_retries):
            self._throttle()
            try:
                response = self._client.get(url)
            except httpx.HTTPError as exc:
                logger.warning("Network error fetching %s: %s", url, exc)
            else:
                if response.status_code in _HALT_STATUSES:
                    raise FetchHaltedError(response.status_code, url)
                if response.status_code < 500:
                    return response
                logger.warning("%s returned %s", url, response.status_code)

            if attempt < settings.http_max_retries - 1:
                backoff = settings.http_backoff_seconds * (2**attempt)
                if backoff:
                    self._sleep(backoff)
        raise SummarySourceUnavailableError(
            f"{url} did not succeed after {settings.http_max_retries} attempts"
        )

    def _archive(self, sha: str, body: bytes) -> None:
        """Keep the PDF bytes by hash, if configured. Never fails the fetch."""
        if self._archive_dir is None:
            return
        target = self._archive_dir / f"{sha}.pdf"
        if target.exists():
            return
        tmp_path: Path | None = None
        try:
            self._archive_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "wb", dir=self._archive_dir, prefix=f".{sha}.", suffix=".tmp", delete=False
            ) as handle:
                handle.write(body)
                tmp_path = Path(handle.name)
            os.replace(tmp_path, target)
        except OSError as exc:
            logger.warning("Could not archive PDF %s: %s", target, exc)
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    tmp_path.unlink()


# ---------------------------------------------------------------------------
# Which documents to fetch
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TargetPlan:
    targets: list[str]
    deferred: list[str]  # Summary filings we do not fetch: De Novo / PMA
    already_fetched: int = 0


def _normalise(number: str) -> str:
    return number.strip().upper()


def select_targets(
    enrichment_rows: Iterable[Mapping[str, object]],
    *,
    already_fetched: set[str],
    only: Iterable[str] | None = None,
    limit: int | None = None,
) -> TargetPlan:
    """Choose what to fetch.

    By default: every submission openFDA says filed a ``Summary``, that is a
    ``K…`` number, and has no PDF in bronze yet -- so a re-run resumes. ``only``
    names exact documents instead and fetches them even if already in bronze (the
    write then skips any whose bytes have not changed).
    """
    if only is not None:
        wanted = list(dict.fromkeys(_normalise(n) for n in only if n.strip()))
        plan = TargetPlan(
            targets=[n for n in wanted if is_fetchable(n)],
            deferred=[n for n in wanted if not is_fetchable(n)],
        )
    else:
        summaries = sorted(
            {
                _normalise(str(row["submission_number"]))
                for row in enrichment_rows
                if str(row.get("statement_or_summary") or "").strip().casefold() == "summary"
            }
        )
        fetchable = [n for n in summaries if is_fetchable(n)]
        plan = TargetPlan(
            targets=[n for n in fetchable if n not in already_fetched],
            deferred=[n for n in summaries if not is_fetchable(n)],
            already_fetched=sum(1 for n in fetchable if n in already_fetched),
        )
    if limit is not None:
        plan.targets = plan.targets[:limit]
    return plan


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


class SupportsFetch(Protocol):
    """The slice of ``SummaryFetcher`` the pass uses (so tests can fake it)."""

    def fetch(self, submission_number: str) -> SummaryDocument: ...


@dataclass(slots=True)
class FetchResult:
    attempted: int = 0
    written: int = 0
    found: int = 0
    not_found: int = 0
    unchanged: int = 0  # refetched, same bytes as a row already in bronze
    text_classes: dict[str, int] = field(default_factory=dict)
    deferred: list[str] = field(default_factory=list)
    already_fetched: int = 0
    halted: str | None = None  # why the pass stopped early, if it did


def fetch_documents(
    targets: Iterable[str],
    fetcher: SupportsFetch,
    *,
    write: Callable[[list[BronzeSummaryDocumentRecord]], None],
    batch_size: int,
    existing_hashes: set[tuple[str, str]],
    now: Callable[[], dt.datetime] = _utcnow,
) -> FetchResult:
    """Fetch each target and hand bronze rows to ``write`` in batches.

    A halt (429/403, or the source unreachable) flushes the pending batch and
    stops: what was fetched is kept, nothing more is requested.
    """
    result = FetchResult()
    classes: Counter[str] = Counter()
    pending: list[BronzeSummaryDocumentRecord] = []

    def flush() -> None:
        if pending:
            write(list(pending))
            result.written += len(pending)
            pending.clear()

    for number in targets:
        result.attempted += 1
        try:
            doc = fetcher.fetch(number)
        except (FetchHaltedError, SummarySourceUnavailableError) as exc:
            result.halted = str(exc)
            logger.error("Halting the summary pass at %s: %s", number, exc)
            break

        if doc.found:
            result.found += 1
            if (doc.submission_number, doc.content_sha256) in existing_hashes:
                result.unchanged += 1
                continue
            classes[doc.text_class or "unreadable"] += 1
        else:
            result.not_found += 1

        pending.append(to_record(doc, ingested_at=now()))
        if len(pending) >= batch_size:
            flush()
        if result.attempted % 100 == 0:
            logger.info("Summary pass: %d fetched so far", result.attempted)

    flush()
    result.text_classes = dict(classes)
    return result


def _bronze_state(spark, settings: Settings) -> tuple[set[str], set[tuple[str, str]]]:
    """Submissions with a PDF in bronze, and every (submission, sha256) already stored."""
    from registry import tables

    if not tables.table_exists(spark, settings, BRONZE_TABLE):
        return set(), set()
    rows = (
        tables.read_table(spark, settings, BRONZE_TABLE)
        .where("content_sha256 IS NOT NULL")
        .select("submission_number", "content_sha256")
        .distinct()
        .collect()
    )
    hashes = {(r["submission_number"], r["content_sha256"]) for r in rows}
    return {number for number, _ in hashes}, hashes


def run(
    spark,
    settings: Settings | None = None,
    *,
    limit: int | None = None,
    only: Iterable[str] | None = None,
    fetcher: SupportsFetch | None = None,
) -> FetchResult:
    """Fetch the Summaries not yet in bronze and append them. Entry point for the CLI."""
    from registry import tables
    from registry.schemas import spark_schema_for

    settings = settings or get_settings()
    already, hashes = _bronze_state(spark, settings)

    enrichment_rows: list[dict] = []
    if only is None:
        if not tables.table_exists(spark, settings, ENRICHMENT_TABLE):
            raise SummaryFetchError(
                f"No {ENRICHMENT_TABLE} table: which devices filed a public Summary comes "
                "from openFDA. Run `registry enrich-openfda` first, or name documents "
                "with --only."
            )
        enrichment_rows = [
            r.asDict()
            for r in tables.read_table(spark, settings, ENRICHMENT_TABLE)
            .select("submission_number", "statement_or_summary")
            .collect()
        ]

    plan = select_targets(enrichment_rows, already_fetched=already, only=only, limit=limit)
    logger.info(
        "Summary pass: %d to fetch, %d already in bronze, %d deferred (De Novo/PMA)",
        len(plan.targets),
        plan.already_fetched,
        len(plan.deferred),
    )

    schema = spark_schema_for(BronzeSummaryDocumentRecord)

    def write(batch: list[BronzeSummaryDocumentRecord]) -> None:
        df = spark.createDataFrame([r.model_dump() for r in batch], schema=schema)
        # Append-only: a document's history of fetches is kept, never replaced.
        tables.write_table(df, settings, BRONZE_TABLE, mode="append")

    with contextlib.ExitStack() as stack:
        if fetcher is None:
            fetcher = stack.enter_context(SummaryFetcher(settings))
        result = fetch_documents(
            plan.targets,
            fetcher,
            write=write,
            batch_size=settings.summary_write_batch_size,
            existing_hashes=hashes,
        )
    result.deferred = plan.deferred
    result.already_fetched = plan.already_fetched
    return result
