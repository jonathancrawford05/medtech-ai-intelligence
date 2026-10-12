"""Device-level intended use from the 510(k) Summary (Issue 4, PR 4C; ADR 0021).

Until now the only device-specific text the leads could run the stage-1 mortality
keyword pass over was the device name (ADR 0016 Decision 2's known limit). The
Summary PDF states the device's own intended use, and PR 4A/4B already hold
everything needed to read it: the per-page text in ``bronze_summary_documents``
and the page roles in ``silver_document_extraction``.

**This pass does not re-segment.** It rebuilds each document's page roles from
the ``ifu_form_pages`` / ``summary_pages`` 4B stored, reads the text from the
bronze fetch with the *same* ``content_sha256``, and looks for the statement in
this order (first hit wins):

``form_3881``        the FDA 3881 Indications-for-Use form: the text after
                     "Indications for Use (Describe)" (or "Indications for Use:"
                     on the pre-3881 page) up to "Type of Use" / "Prescription
                     Use" / the form's footer. Every form page that yields a
                     statement contributes (K190013 files an OTC and an Rx form).
``form_estar``       the eSTAR form, which 4B classes as Summary text: the text
                     after "Please provide your Indications for Use below."
``summary_section``  the Summary's own "Indications for Use" / "Intended Use"
                     section (adjacent such headings read as one), capped.

A candidate needs at least `_MIN_LETTERS` letters: "Intended Use /" (a heading
pair) and "See Indications for Use below." are not statements. Text is a span of
the normalised page (`document_extraction.normalise`), so it is verbatim of what
was matched and carries its page(s). Evidence, never a judgement (ADR 0007).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from registry.config.settings import Settings, get_settings
from registry.schemas import IntendedUseRecord
from registry.transform import document_extraction as dx

logger = logging.getLogger(__name__)

INTENDED_USE_TABLE = "silver_intended_use"

# Bump on any rule change, like 4B's EXTRACTOR_VERSION.
INTENDED_USE_VERSION = "4c.1"

# A statement has words: below this many letters it is a heading fragment.
_MIN_LETTERS = 40
# A Summary section with no closing heading must not sweep the rest of the
# document; real statements on the slice run to ~1,600 characters (K250177).
_SECTION_WINDOW = 2500

_IFU_LABEL = re.compile(
    dx.phrase("Indications for Use") + r"\s*(?:\(\s*Describe\s*\)|:)", re.IGNORECASE
)
# Where the 3881 statement stops: the "Type of Use" checkboxes, or the older
# form's signature block. The first of these after the label closes it.
# "Prescription Use" counts only as the checkbox (followed by its CFR cite): a
# statement may itself say "for over-the-counter use" (K250507).
_USE_CHECKBOX = r"(?:Prescription|Over[\s-]*The[\s-]*Counter)\s+Use\b.{0,40}?21\s*CFR"
_FORM_END = re.compile(
    rf"Type\s+of\s+Use|{_USE_CHECKBOX}"
    r"|\(\s*PLEASE\s+DO\s+NOT\s+WRITE|CONTINUE\s+ON\s+A(?:NOTHER|\s+SEPARATE)\s+PAGE"
    r"|Concurrence\s+of\s+CDRH|FOR\s+FDA\s+USE\s+ONLY",
    re.IGNORECASE,
)
_ESTAR_LABEL = re.compile(
    dx.phrase("Please provide your Indications for Use below") + r"\.?", re.IGNORECASE
)
_ESTAR_END = re.compile(
    rf"Please\s+select\s+the\s+types?\s+of\s+uses?|{_USE_CHECKBOX}", re.IGNORECASE
)
_IFU_SECTIONS = ("indications_for_use", "intended_use")
# Labels that close an intended-use section but are not 4B headings (adding them
# to 4B's vocabulary would move its predicate windows).
_SECTION_END = dx._compile(
    "product description",
    "contraindications",
    "comparison",
    "summary of",
    "principles of operation",
    "device characteristics",
)
# Field labels that close a section only with their colon ("Technology: The
# Voluson ... employs the same technology as its predicate", K233692).
_LABEL_END = re.compile(
    r"(?:Technology|Determination of Substantial Equivalence|Contraindications?|"
    r"Device Description|Comparison)\s*:"
)
# Punctuation and checkbox debris either side of a statement.
_EDGE = " \t\n:;|/-•*?"


@dataclass(frozen=True)
class IntendedUse:
    text: str | None
    basis: str  # see schemas.IntendedUseBasis
    pages: list[int] = field(default_factory=list)
    heading: str | None = None


def _letters(text: str) -> int:
    return sum(c.isalpha() for c in text)


def _clean(doc: dx.Document, start: int, end: int) -> tuple[int, int]:
    """Trim edge debris, and trailing one-character checkbox glyphs ("I Z")."""
    text = doc.text
    while True:
        while start < end and text[start] in _EDGE:
            start += 1
        # A lone checkbox glyph before the statement ("I The Hypertension ...").
        head = text[start:end].split(" ", 1)
        if len(head) == 2 and len(head[0]) == 1 and head[0] not in "A" and head[1][:1].isupper():
            start += 2
            continue
        break
    while True:
        while end > start and text[end - 1] in _EDGE:
            end -= 1
        tail = text[start:end].rsplit(" ", 1)
        if len(tail) == 2 and len(tail[1]) == 1 and not tail[1].isdigit():
            end -= 2
            continue
        return start, end


def _statement(doc: dx.Document, start: int, end: int) -> tuple[int, int] | None:
    start, end = _clean(doc, start, end)
    return (start, end) if _letters(doc.text[start:end]) >= _MIN_LETTERS else None


def _page_span(doc: dx.Document, page: int) -> tuple[int, int]:
    lo = doc.starts[page - 1]
    return lo, lo + len(doc.pages[page - 1])


def from_forms(doc: dx.Document, seg: dx.Segmentation) -> IntendedUse | None:
    """The statement on every 3881 form page that has one."""
    parts: list[str] = []
    pages: list[int] = []
    heading: str | None = None
    for page in seg.pages_of("ifu_form"):
        lo, hi = _page_span(doc, page)
        label = _IFU_LABEL.search(doc.text, lo, hi)
        if label is None:
            continue
        stop = _FORM_END.search(doc.text, label.end(), hi)
        span = _statement(doc, label.end(), stop.start() if stop else hi)
        if span is None:
            continue
        parts.append(doc.text[span[0] : span[1]])
        pages.append(page)
        heading = heading or label.group(0)
    if not parts:
        return None
    return IntendedUse(dx.PAGE_BREAK.join(parts), "form_3881", pages, heading)


def from_estar(doc: dx.Document, seg: dx.Segmentation) -> IntendedUse | None:
    """The eSTAR form's statement (4B classes the page as Summary text)."""
    for lo, hi in seg.summary_spans:
        label = _ESTAR_LABEL.search(doc.text, lo, hi)
        if label is None:
            continue
        stop = _ESTAR_END.search(doc.text, label.end(), hi)
        span = _statement(doc, label.end(), stop.start() if stop else hi)
        if span is not None:
            text = doc.text[span[0] : span[1]]
            return IntendedUse(text, "form_estar", [doc.page_of(span[0])], label.group(0))
    return None


def _heading_end(doc: dx.Document, section: dx.Section) -> int:
    match = dx._HEADINGS[section.name].match(doc.text, section.start)
    return match.end() if match else section.start


def from_summary(doc: dx.Document, seg: dx.Segmentation) -> IntendedUse | None:
    """The first Summary intended-use section that holds a statement.

    Adjacent intended-use headings read as one ("Intended Use / Indications for
    Use: ...", K232699), so the statement after a heading pair is not lost.
    """
    sections = seg.sections
    i = 0
    while i < len(sections):
        first = sections[i]
        if first.name not in _IFU_SECTIONS:
            i += 1
            continue
        last = first
        while i + 1 < len(sections) and sections[i + 1].name in _IFU_SECTIONS:
            i += 1
            last = sections[i]
        i += 1
        start = _heading_end(doc, first)
        # To the last adjacent heading's end, capped, and never past the contiguous
        # Summary pages (4B's predicate-window rule).
        body = dx.Section(first.name, first.page, start, last.end)
        end = max(dx._window_end(seg, body, _SECTION_WINDOW), start)
        # A statement that ends its page does not run into the next one; one that
        # breaks mid-sentence does (K231010).
        page_hi = _page_span(doc, doc.page_of(start))[1]
        if end > page_hi and doc.text[start:page_hi].rstrip().endswith((".", ")")):
            end = page_hi
        stops = [
            m.start()
            for m in _SECTION_END.finditer(doc.text, start, end)
            if dx._is_label(doc.text, m.start())
        ]
        stops += [m.start() for m in _LABEL_END.finditer(doc.text, start, end)]
        end = min([end, *stops])
        span = _statement(doc, start, end)
        if span is None:
            continue
        text = doc.text[span[0] : span[1]]
        pages = sorted({doc.page_of(span[0]), doc.page_of(span[1] - 1)})
        heading = doc.text[first.start : _heading_end(doc, first)]
        return IntendedUse(text, "summary_section", pages, heading)
    return None


def extract(doc: dx.Document, seg: dx.Segmentation) -> IntendedUse:
    """The device's intended use, by the precedence in the module docstring."""
    for reader in (from_forms, from_estar, from_summary):
        found = reader(doc, seg)
        if found is not None:
            return found
    if seg.pages_of("ifu_form") or seg.summary_spans:
        return IntendedUse(None, "not_found")
    return IntendedUse(None, "no_text")


def extract_document(
    bronze_row: Mapping[str, Any],
    roles: Mapping[str, Any],
    *,
    now: dt.datetime | None = None,
) -> IntendedUseRecord:
    """One bronze fetch plus its 4B extraction row -> one intended-use record."""
    if bronze_row["content_sha256"] != roles["source_content_sha256"]:
        raise ValueError(
            f"{bronze_row['submission_number']}: the page roles describe another fetch "
            f"({roles['source_content_sha256']}); re-run `registry extract-documents`"
        )
    doc = dx.Document.from_raw(bronze_row["submission_number"], bronze_row.get("page_texts") or [])
    kinds = dx.page_kinds_from_roles(
        len(doc.pages),
        letter_pages=roles.get("letter_pages") or (),
        ifu_form_pages=roles.get("ifu_form_pages") or (),
        summary_pages=roles.get("summary_pages") or (),
    )
    found = extract(doc, dx.sections_for(doc, kinds))
    return IntendedUseRecord(
        submission_number=doc.submission_number,
        extractor_version=INTENDED_USE_VERSION,
        extracted_at=now or dt.datetime.now(dt.UTC).replace(tzinfo=None),
        source_content_sha256=bronze_row["content_sha256"],
        source_url=bronze_row.get("url"),
        segmentation_version=roles["extractor_version"],
        intended_use_text=found.text,
        intended_use_basis=found.basis,
        intended_use_pages=found.pages,
        intended_use_heading=found.heading,
    )


# ---------------------------------------------------------------------------
# Yields: coverage against finding 0019's "readable somewhere" ceiling
# ---------------------------------------------------------------------------

# Finding 0019: the Indications-for-Use wording is readable in 1,537 / 1,541.
FINDING_0019_READABLE = (1537, 1541)


@dataclass
class Yields:
    documents: int = 0
    basis: Counter[str] = field(default_factory=Counter)
    # Extraction rows with no bronze fetch of the same sha (should be 0).
    unmatched: int = 0

    @property
    def found(self) -> int:
        return sum(self.basis[b] for b in ("form_3881", "form_estar", "summary_section"))

    def lines(self) -> list[str]:
        n = self.documents
        pct = f"{self.found:,} / {n:,} ({100 * self.found / n:.1f}%)" if n else "0 / 0"
        k, total = FINDING_0019_READABLE
        out = [
            f"documents read                 : {n:,}",
            f"intended use found             : {pct}"
            f"   [finding 0019 readable: {k:,} / {total:,} ({100 * k / total:.1f}%)]",
            "  by basis                      : "
            + ", ".join(f"{b} {c:,}" for b, c in sorted(self.basis.items())),
        ]
        if self.unmatched:
            out.append(
                f"  extraction rows with no matching fetch: {self.unmatched:,} "
                "(re-run `registry extract-documents`)"
            )
        return out


def summarise(records: Iterable[IntendedUseRecord]) -> Yields:
    y = Yields()
    for rec in records:
        y.documents += 1
        y.basis[rec.intended_use_basis] += 1
    return y


# ---------------------------------------------------------------------------
# Spark: bronze + silver_document_extraction -> silver_intended_use
# ---------------------------------------------------------------------------


def _matched_rows(spark, settings: Settings) -> tuple[list[dict], list[dict], int]:
    """(bronze rows, their extraction rows, extraction rows with no match).

    Joined on (submission, content_sha256): the page roles must describe exactly
    the text being read. Any fetch with that sha has the same bytes, so one is kept.
    """
    from pyspark.sql import functions as F

    from registry import tables

    roles = tables.read_table(spark, settings, dx.EXTRACTION_TABLE).select(
        "submission_number",
        F.col("source_content_sha256").alias("content_sha256"),
        "extractor_version",
        "letter_pages",
        "ifu_form_pages",
        "summary_pages",
    )
    bronze = (
        tables.read_table(spark, settings, dx.BRONZE_TABLE)
        .where("content_sha256 IS NOT NULL")
        .select("submission_number", "content_sha256", "url", "page_texts")
        .dropDuplicates(["submission_number", "content_sha256"])
    )
    joined = roles.join(bronze, ["submission_number", "content_sha256"], "left").collect()
    pairs = [r.asDict() for r in joined if r["page_texts"] is not None]
    unmatched = len(joined) - len(pairs)
    bronze_rows = [
        {k: p[k] for k in ("submission_number", "content_sha256", "url", "page_texts")}
        for p in pairs
    ]
    role_rows = [
        {
            "source_content_sha256": p["content_sha256"],
            "extractor_version": p["extractor_version"],
            "letter_pages": p["letter_pages"],
            "ifu_form_pages": p["ifu_form_pages"],
            "summary_pages": p["summary_pages"],
        }
        for p in pairs
    ]
    return bronze_rows, role_rows, unmatched


@dataclass
class IntendedUseRun:
    written: int
    yields: Yields


def run(spark, settings: Settings | None = None) -> IntendedUseRun:
    """Rebuild ``silver_intended_use``. Entry point for the CLI.

    Overwrite, like every silver table: derived from bronze plus 4B's page roles,
    both of which keep their own history.
    """
    from registry import tables
    from registry.schemas import spark_schema_for

    settings = settings or get_settings()
    if not tables.table_exists(spark, settings, dx.EXTRACTION_TABLE):
        raise ValueError(
            f"No {dx.EXTRACTION_TABLE} table. Run `registry extract-documents` first: "
            "this pass reads its page roles rather than re-segmenting (ADR 0021)."
        )
    bronze_rows, role_rows, unmatched = _matched_rows(spark, settings)
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    records = [extract_document(b, r, now=now) for b, r in zip(bronze_rows, role_rows, strict=True)]
    if not records:
        raise ValueError(f"No {dx.EXTRACTION_TABLE} row has its bronze fetch; nothing to read")
    df = spark.createDataFrame(
        [r.model_dump() for r in records], schema=spark_schema_for(IntendedUseRecord)
    )
    tables.write_table(df, settings, INTENDED_USE_TABLE, mode="overwrite", merge_schema=True)
    logger.info("Wrote %d rows to %s", len(records), INTENDED_USE_TABLE)
    yields = summarise(records)
    yields.unmatched = unmatched
    return IntendedUseRun(written=len(records), yields=yields)


def rows_by_submission(spark, settings: Settings | None = None) -> dict[str, dict[str, Any]]:
    """submission -> its `silver_intended_use` row, statement read or not.

    ``{}`` when the pass has not run: the leads still build (ADR 0013's rule).
    """
    from registry import tables

    settings = settings or get_settings()
    if not tables.table_exists(spark, settings, INTENDED_USE_TABLE):
        return {}
    rows = tables.read_table(spark, settings, INTENDED_USE_TABLE).collect()
    return {r["submission_number"]: r.asDict() for r in rows}


def texts_by_submission(spark, settings: Settings | None = None) -> dict[str, str]:
    """submission -> intended-use text, for the leads (statements read only)."""
    return {
        key: row["intended_use_text"]
        for key, row in rows_by_submission(spark, settings).items()
        if row["intended_use_text"]
    }
