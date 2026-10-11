"""Deterministic extraction from 510(k) Summary text (Issue 4, PR 4B; ADR 0019).

Reads the per-page text PR 4A stored in ``bronze_summary_documents`` and writes
one row per document to ``silver_document_extraction``: predicate lineage, a
cybersecurity presence flag and ``has_pccp``, each with the page and method it came
from. Offline and deterministic -- no request leaves this module, so it runs in any
session against the stored text and re-runs without re-fetching.

The pipeline, in the order a document goes through it:

1. **Normalise** each page (`normalise`). The text layer splits glyphs ("Noti fi
   cation", "K0921 16") and misreads digits ("Kl41248"); finding 0011 measured it.
   Submission numbers are rebuilt in place, ligatures are expanded, whitespace is
   collapsed. Keyword matching additionally tolerates a split inside a word
   (`phrase`). Every rule below runs on normalised text only.
2. **Segment** (`segment`). Each page is the FDA clearance letter, the FDA 3881
   Indications-for-Use form, or Summary text; within the Summary, label-style
   headings ("Predicate Device(s)", "Indications for Use", "Intended Use",
   "Substantial Equivalence", ...) open sections. A heading is told from prose by
   the word before it: "the predicate device" is prose, "Class II Predicate
   Device:" is a label.
3. **Extract**: predicates (`extract_predicates`), cybersecurity
   (`extract_cybersecurity`) and PCCP (`extract_pccp`). The rules, and why each
   reads ``None`` rather than ``False`` when the text cannot answer, are ADR 0019.

Extraction produces evidence (ADR 0007): every value carries its page, method and
a verbatim quote of the normalised text, and nothing here touches a mortality flag.
"""

from __future__ import annotations

import bisect
import datetime as dt
import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from registry.config.settings import Settings, get_settings
from registry.schemas import (
    DocumentExtractionRecord,
    PredicateMethod,
    PrimaryPredicateBasis,
)

logger = logging.getLogger(__name__)

BRONZE_TABLE = "bronze_summary_documents"
EXTRACTION_TABLE = "silver_document_extraction"

# Bump on any rule change: rows record the version that produced them, so a
# full-scale yield is always attributable to one rule set.
EXTRACTOR_VERSION = "4b.1"

# How much text a quote carries either side of the matched span.
_QUOTE_CONTEXT = 120

# ---------------------------------------------------------------------------
# 1. Normalisation
# ---------------------------------------------------------------------------

# Characters NFKC leaves alone but the text layer uses for plain ones.
_TRANSLATE = str.maketrans(
    {
        0x00AD: None,  # soft hyphen
        0x200B: None,  # zero-width space
        0xFEFF: None,  # byte-order mark
        **dict.fromkeys((0x2010, 0x2011, 0x2012, 0x2013, 0x2014), "-"),  # hyphens, dashes
        **dict.fromkeys((0x2018, 0x2019), "'"),  # curly single quotes
        **dict.fromkeys((0x201C, 0x201D), '"'),  # curly double quotes
    }
)

# A submission number as the text layer actually renders it: the prefix, then six
# digit-like characters with at most one space between any two ("K0921 16",
# "K 2 5 3 6 2 8"), where an OCR'd "l"/"I" stands for 1 and "O" for 0 ("Kl41248").
# Not followed by a further digit, so "K1234567" is not truncated into a match.
_DIGITISH = r"[0-9lIO]"
_SUBMISSION_RAW = re.compile(
    rf"(?<![A-Za-z0-9])(DEN|[KkP])\s?({_DIGITISH}(?:\s?{_DIGITISH}){{5}})(?![0-9])"
)
_DIGIT_FIX = str.maketrans({"l": "1", "I": "1", "O": "0"})

# A canonical number in normalised text.
SUBMISSION_NUMBER = re.compile(r"(?<![A-Za-z0-9])(?:K|DEN|P)\d{6}(?![0-9])")


def _canonical_submission(match: re.Match[str]) -> str:
    prefix, body = match.group(1), match.group(2)
    digits = body.replace(" ", "")
    # At least four true digits: "KIOlIO..." is a word, not a misread number.
    if sum(c.isdigit() for c in digits) < 4:
        return match.group(0)
    return prefix.upper() + digits.translate(_DIGIT_FIX)


def normalise(text: str) -> str:
    """The text every rule matches on.

    NFKC expands ligatures ("ﬁ" -> "fi") and compatibility forms; dashes and quotes
    are folded to ASCII; whitespace collapses to single spaces; and submission
    numbers split or misread by the text layer are rebuilt (finding 0011).
    """
    text = unicodedata.normalize("NFKC", text or "").translate(_TRANSLATE)
    text = re.sub(r"\s+", " ", text).strip()
    return _SUBMISSION_RAW.sub(_canonical_submission, text)


def phrase(words: str) -> str:
    """A regex for ``words`` that survives glyph splitting.

    Any letter may be followed by a single space ("Noti fi cation") or a line-end
    hyphen ("cyber- security"); words may be separated by any whitespace or a
    hyphen. Case-insensitivity is the caller's flag.
    """
    parts = []
    for word in words.split():
        parts.append(r"(?:\s|-\s?)?".join(re.escape(ch) for ch in word))
    return r"[\s-]*".join(parts)


def _compile(*words: str) -> re.Pattern[str]:
    """Case-insensitive alternation of `phrase`s, each ending on a word boundary."""
    alternatives = "|".join(f"(?:{phrase(w)})" for w in words)
    return re.compile(f"(?:{alternatives})(?![A-Za-z])", re.IGNORECASE)


# ---------------------------------------------------------------------------
# The document: normalised pages joined into one string with page offsets
# ---------------------------------------------------------------------------

PAGE_BREAK = "\n\n"


@dataclass
class Document:
    """A Summary's normalised text, addressable by character offset and by page."""

    submission_number: str
    pages: list[str]  # normalised; pages[i] is page i + 1
    text: str = field(init=False)
    starts: list[int] = field(init=False)

    def __post_init__(self) -> None:
        self.starts, offset = [], 0
        for page in self.pages:
            self.starts.append(offset)
            offset += len(page) + len(PAGE_BREAK)
        self.text = PAGE_BREAK.join(self.pages)

    @classmethod
    def from_raw(cls, submission_number: str, page_texts: Iterable[str]) -> Document:
        return cls(submission_number.strip().upper(), [normalise(p) for p in page_texts])

    def page_of(self, offset: int) -> int:
        """1-based page number holding ``offset``."""
        return max(bisect.bisect_right(self.starts, offset), 1)

    def quote(self, start: int, end: int) -> str:
        """The matched span with context, clipped to its page."""
        page = self.page_of(start)
        lo = self.starts[page - 1]
        hi = lo + len(self.pages[page - 1])
        return self.text[max(lo, start - _QUOTE_CONTEXT) : min(hi, end + _QUOTE_CONTEXT)].strip()


# ---------------------------------------------------------------------------
# 2. Segmentation
# ---------------------------------------------------------------------------

# The FDA letter: its first page carries a salutation plus the "Re: K…" line or the
# letterhead; later pages open with the "Page 2 - Name" / "K… - Name Page 2" header.
_LETTER_OPENING = re.compile(
    r"(?:\bRe:\s*(?:K|DEN)\d{6}|Food\s*(?:and|&)\s*Drug\s+Administration)", re.IGNORECASE
)
_SALUTATION = re.compile(r"\bDear\s+\S", re.IGNORECASE)
_LETTER_CONTINUATION = re.compile(
    r"^.{0,40}?(?:(?:K|DEN)\d{6}\s*-\s*.{0,80}?Page\s*\d|Page\s*\d+\s*-\s*\S)", re.IGNORECASE
)
# Form FDA 3881 (and the pre-3881 Indications for Use page that asked the same).
_IFU_FORM = re.compile(
    rf"FORM\s+FDA\s+3881|{phrase('Indications for Use')}.{{0,400}}"
    rf"(?:OMB\s+No|{phrase('510(k) Number')}\s*\(if\s+known\))",
    re.IGNORECASE,
)

# Summary headings, in the vocabulary the handoff names plus the neighbours that
# close a predicate section. Order matters only for readability.
_HEADINGS: dict[str, re.Pattern[str]] = {
    "predicate_device": _compile("predicate devices", "predicate device"),
    "reference_device": _compile("reference devices", "reference device"),
    "indications_for_use": _compile("indications for use"),
    "intended_use": _compile("intended use"),
    "substantial_equivalence": _compile("substantial equivalence", "substantial equivalency"),
    "technological_characteristics": _compile("technological characteristics"),
    "device_description": _compile("device description"),
    "performance": _compile(
        "performance data", "performance testing", "non-clinical testing", "clinical testing"
    ),
    "conclusion": _compile("conclusions", "conclusion"),
}

# The word before a heading phrase that marks it as prose, not a label.
_PROSE_WORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "its",
        "their",
        "his",
        "her",
        "our",
        "your",
        "this",
        "that",
        "these",
        "those",
        "both",
        "each",
        "either",
        "any",
        "to",
        "of",
        "with",
        "as",
        "and",
        "or",
        "for",
        "in",
        "on",
        "by",
        "from",
        "same",
        "similar",
        "primary",
        "legally",
        "identified",
        "cleared",
        "marketed",
        "new",
        "existing",
        "subject",
        "proposed",
        "no",
        "not",
    ]
)


def _is_label(text: str, start: int) -> bool:
    """True when the phrase at ``start`` reads as a heading or field label.

    Flattened PDF text has no layout left, so the only evidence is the token before
    the phrase: a lowercase word or a determiner ("to the predicate device") is
    prose; a number, punctuation or another capitalised label ("Class II Predicate
    Device:") is a label. A heading also starts with a capital, and an all-caps word
    before it belongs to the same heading ("COMPARISON OF TECHNOLOGICAL ...").
    """
    if not text[start].isupper():
        return False
    before = text[max(0, start - 40) : start].rstrip()
    if not before:
        return True
    token = before.split()[-1] if before.split() else ""
    word = token.strip("\"'(),")
    if not word or not word[-1].isalpha():
        return True  # "4.", ")", "892.2050", "|", a page break
    if len(word) > 1 and word.isupper():
        return True
    if word.lower() in _PROSE_WORDS:
        return False
    return not word[0].islower()


@dataclass(frozen=True)
class Section:
    name: str
    page: int
    start: int  # offset of the heading
    end: int  # offset where the next heading (or the page range) begins


@dataclass
class Segmentation:
    """Which pages are which, and the labelled sections inside the Summary pages."""

    page_kinds: list[str]  # per page: fda_letter | ifu_form | summary | unreadable
    sections: list[Section]
    # (start, end) offsets of every summary page, for "is this offset Summary text?"
    summary_spans: list[tuple[int, int]]

    def pages_of(self, kind: str) -> list[int]:
        return [i for i, k in enumerate(self.page_kinds, start=1) if k == kind]

    def in_summary(self, offset: int) -> bool:
        return any(lo <= offset < hi for lo, hi in self.summary_spans)

    def section_at(self, offset: int) -> str:
        """The innermost section heading at or before ``offset``; ``summary`` if none."""
        name = "summary"
        for section in self.sections:
            if section.start <= offset < section.end:
                name = section.name
        return name


# A page with fewer non-whitespace characters than this has no usable text layer
# (the same floor ADR 0018 uses for text_class).
_READABLE_FLOOR = 100
# Real text has words: a page whose alphabetic share is this low is a font-encoding
# failure (K141480's Summary reads as `!"!  "#""`), not a text layer.
_ALPHA_FLOOR = 0.5


def _readable(page: str) -> bool:
    chars = re.sub(r"\s", "", page)
    if len(chars) < _READABLE_FLOOR:
        return False
    return sum(c.isalpha() for c in chars) / len(chars) >= _ALPHA_FLOOR


def classify_page(page: str, previous: str | None = None) -> str:
    """fda_letter | ifu_form | summary | unreadable, for one normalised page."""
    if not _readable(page):
        return "unreadable"
    if _SALUTATION.search(page) and _LETTER_OPENING.search(page):
        return "fda_letter"
    if previous == "fda_letter" and _LETTER_CONTINUATION.search(page):
        return "fda_letter"
    if _IFU_FORM.search(page):
        return "ifu_form"
    return "summary"


def segment(doc: Document) -> Segmentation:
    kinds: list[str] = []
    for page in doc.pages:
        kinds.append(classify_page(page, kinds[-1] if kinds else None))

    spans = [
        (doc.starts[i], doc.starts[i] + len(doc.pages[i]))
        for i, kind in enumerate(kinds)
        if kind == "summary"
    ]

    headings: list[tuple[int, int, str]] = []
    for name, pattern in _HEADINGS.items():
        for match in pattern.finditer(doc.text):
            if any(lo <= match.start() < hi for lo, hi in spans) and _is_label(
                doc.text, match.start()
            ):
                headings.append((match.start(), match.end(), name))
    headings.sort()

    sections = []
    for i, (start, _end, name) in enumerate(headings):
        nxt = headings[i + 1][0] if i + 1 < len(headings) else len(doc.text)
        sections.append(Section(name=name, page=doc.page_of(start), start=start, end=nxt))
    return Segmentation(page_kinds=kinds, sections=sections, summary_spans=spans)


# ---------------------------------------------------------------------------
# 3a. Predicates
# ---------------------------------------------------------------------------

# A predicate section runs from its label to the next heading, capped: a label with
# no following heading must not sweep the rest of the document.
_PREDICATE_WINDOW = 1500
_REFERENCE_WINDOW = 600
# This many distinct numbers under one predicate label is a table of predicates
# (K181892 lists eight), recorded as method `table` rather than `section`.
_TABLE_MIN = 3

_PREDICATE_WORD = re.compile(phrase("predicate"), re.IGNORECASE)
_SE_PHRASE = _compile("substantially equivalent to", "substantial equivalence to")
_PRIMARY_PREDICATE = _compile("primary predicate")
_REFERENCE_PHRASE = _compile("reference device")
# Sentence ends: a full stop / semicolon before a capital or an opening bracket.
_SENTENCE_END = re.compile(r"(?<=[.;])\s+(?=[A-Z(\"'])")


@dataclass(frozen=True)
class PredicateHit:
    number: str
    page: int
    method: PredicateMethod
    section: str
    start: int
    quote: str


@dataclass
class PredicateResult:
    predicates: list[PredicateHit]  # one per distinct number, in document order
    primary: PredicateHit | None
    primary_basis: PrimaryPredicateBasis | None
    reference_devices: list[str]


def _numbers_in(text: str, lo: int, hi: int) -> list[tuple[str, int]]:
    return [(m.group(0), m.start()) for m in SUBMISSION_NUMBER.finditer(text, lo, hi)]


def _sentences(text: str, lo: int, hi: int) -> list[tuple[int, int]]:
    """(start, end) of each sentence in text[lo:hi]."""
    bounds, start = [], lo
    for match in _SENTENCE_END.finditer(text, lo, hi):
        bounds.append((start, match.start()))
        start = match.end()
    bounds.append((start, hi))
    return bounds


def _window_end(seg: Segmentation, section: Section, cap: int) -> int:
    end = min(section.end, section.start + cap)
    # Never run past the summary page range the label sits in.
    for lo, hi in seg.summary_spans:
        if lo <= section.start < hi:
            # Allow a section to continue onto the next summary page (K181892's
            # table starts a page after its label), but not into a letter or form.
            following = [s for s in seg.summary_spans if s[0] >= lo]
            contiguous_hi = hi
            for nlo, nhi in following[1:]:
                if nlo - contiguous_hi <= len(PAGE_BREAK):
                    contiguous_hi = nhi
                else:
                    break
            end = min(end, contiguous_hi)
    return end


def extract_predicates(doc: Document, seg: Segmentation) -> PredicateResult:
    """All predicate numbers, how each was found, the primary, and reference devices.

    Methods, strongest first: ``table`` (three or more numbers under one predicate
    label), ``section`` (a number under a predicate label), ``sentence`` (a number in
    a Summary sentence naming a predicate or substantial equivalence). A number is
    reported once, under its strongest method. The subject's own number and any
    number under a reference-device label are excluded.
    """
    subject = doc.submission_number
    text = doc.text

    reference_spans: list[tuple[int, int]] = []
    reference_numbers: list[str] = []
    for section in seg.sections:
        if section.name == "reference_device":
            end = _window_end(seg, section, _REFERENCE_WINDOW)
            reference_spans.append((section.start, end))
            reference_numbers += [n for n, _ in _numbers_in(text, section.start, end)]

    def excluded(number: str, offset: int) -> bool:
        if number == subject:
            return True
        return any(lo <= offset < hi for lo, hi in reference_spans)

    hits: dict[str, PredicateHit] = {}
    strength = {"table": 3, "section": 2, "sentence": 1}

    def keep(number: str, offset: int, method: PredicateMethod) -> None:
        current = hits.get(number)
        if current is not None and strength[current.method] >= strength[method]:
            return
        hits[number] = PredicateHit(
            number=number,
            page=doc.page_of(offset),
            method=method,
            section=seg.section_at(offset),
            start=offset if current is None else min(current.start, offset),
            quote=doc.quote(offset, offset + len(number)),
        )

    section_order: list[str] = []
    for section in seg.sections:
        if section.name != "predicate_device":
            continue
        end = _window_end(seg, section, _PREDICATE_WINDOW)
        found = [(n, o) for n, o in _numbers_in(text, section.start, end) if not excluded(n, o)]
        method: PredicateMethod = "table" if len({n for n, _ in found}) >= _TABLE_MIN else "section"
        for number, offset in found:
            keep(number, offset, method)
            if number not in section_order:
                section_order.append(number)

    primary_stated: PredicateHit | None = None
    for lo, hi in seg.summary_spans:
        for s_lo, s_hi in _sentences(text, lo, hi):
            sentence = text[s_lo:s_hi]
            if not (_PREDICATE_WORD.search(sentence) or _SE_PHRASE.search(sentence)):
                continue
            stated = _PRIMARY_PREDICATE.search(sentence)
            for number, offset in _numbers_in(text, s_lo, s_hi):
                if excluded(number, offset):
                    continue
                # "Reference Device: K150910" inside a run-on table sentence.
                lead = text[max(s_lo, offset - 80) : offset]
                if _REFERENCE_PHRASE.search(lead) and not _PREDICATE_WORD.search(lead):
                    continue
                keep(number, offset, "sentence")
                if stated and primary_stated is None:
                    primary_stated = hits[number]

    predicates = sorted(hits.values(), key=lambda h: h.start)

    primary: PredicateHit | None
    basis: PrimaryPredicateBasis | None
    if primary_stated is not None:
        primary, basis = primary_stated, "stated_primary"
    elif section_order:
        primary, basis = hits[section_order[0]], "first_in_section"
    elif predicates:
        primary, basis = predicates[0], "first_in_sentence"
    else:
        primary, basis = None, None

    predicate_numbers = {h.number for h in predicates}
    references = []
    for number in reference_numbers:
        if number != subject and number not in predicate_numbers and number not in references:
            references.append(number)
    return PredicateResult(predicates, primary, basis, references)


# ---------------------------------------------------------------------------
# 3b. Cybersecurity presence
# ---------------------------------------------------------------------------

_CYBER = re.compile(phrase("cyber") + r"[\s-]*" + phrase("security"), re.IGNORECASE)
# "Cybersecurity: N/A", "Cybersecurity is not applicable", "... not required".
_CYBER_NEGATION_AFTER = re.compile(
    r"^\W{0,3}(?:(?:is|was|are|were)\s+)?"
    r"(?:n/?a\b|not\s+applicable|none\b|not\s+required|not\s+relevant)",
    re.IGNORECASE,
)
# "No cybersecurity ...", "without cybersecurity ...": the mention itself is negated.
_CYBER_NEGATION_BEFORE = re.compile(r"\b(?:no|without)\s+(?:\S+\s+)?$", re.IGNORECASE)


@dataclass
class CyberResult:
    present: bool | None
    basis: str  # mention | negated | no_mention | no_text
    page: int | None = None
    quote: str | None = None


def extract_cybersecurity(doc: Document, seg: Segmentation) -> CyberResult:
    """Is a cybersecurity statement present in the Summary text?

    Present = a mention of cybersecurity in Summary text (not the FDA letter or the
    3881 form) that is not itself a negation ("Cybersecurity: N/A"). Only negated
    mentions -> ``False`` (``negated``); no mention -> ``False`` (``no_mention``).
    ``None`` when no Summary page has a readable text layer: absence there is
    unknown, not "no" (ADR 0012).
    """
    if not seg.summary_spans:
        return CyberResult(present=None, basis="no_text")
    negated: re.Match[str] | None = None
    for match in _CYBER.finditer(doc.text):
        if not seg.in_summary(match.start()):
            continue
        after = doc.text[match.end() : match.end() + 40]
        before = doc.text[max(0, match.start() - 30) : match.start()]
        if _CYBER_NEGATION_AFTER.search(after) or _CYBER_NEGATION_BEFORE.search(before):
            negated = negated or match
            continue
        return CyberResult(
            present=True,
            basis="mention",
            page=doc.page_of(match.start()),
            quote=doc.quote(match.start(), match.end()),
        )
    if negated is not None:
        return CyberResult(
            present=False,
            basis="negated",
            page=doc.page_of(negated.start()),
            quote=doc.quote(negated.start(), negated.end()),
        )
    return CyberResult(present=False, basis="no_mention")


# ---------------------------------------------------------------------------
# 3c. PCCP (finding 0019)
# ---------------------------------------------------------------------------

_PCCP = re.compile(rf"(?:{phrase('predetermined change control plan')}|\bPCCPs?\b)", re.IGNORECASE)
# The FDA clearance letter's formulaic sentence (finding 0019, 68 documents):
# "FDA's substantial equivalence determination also included the review and
# clearance of your Predetermined Change Control Plan (PCCP)."
_LETTER_SENTENCE = re.compile(
    phrase("review and clearance of") + r"\s+(?:your|the)\s+(?:" + _PCCP.pattern + ")",
    re.IGNORECASE,
)
# FDA correction letters: the original SE letter "inadvertently indicated" a PCCP.
_CORRECTION = re.compile(phrase("inadvertently"), re.IGNORECASE)
_CORRECTION_REACH = 600
# Negation immediately before a mention: "No Predetermined Change Control Plan is
# included", "does not include a PCCP", "without a PCCP".
_PCCP_NEGATION_BEFORE = re.compile(
    r"\b(?:no|without|not\s+(?:include|includes|included|contain|contains|submit|"
    r"submitted|propose|proposed|request|requested)|"
    r"(?:does|did|do)\s+not\s+(?:include|contain|have|propose|request))"
    r"\s+(?:an?\s+|the\s+|any\s+)?$",
    re.IGNORECASE,
)
# Negation immediately after: "PCCP: N/A", "PCCP is not included / not applicable".
_PCCP_NEGATION_AFTER = re.compile(
    r"^\s*(?:\(PCCPs?\)\s*)?\W{0,3}(?:(?:is|was|are|were|has|have)\s+(?:been\s+)?)?"
    r"(?:n/?a\b|not\s+applicable|none\b|not\s+(?:included|provided|submitted|proposed|"
    r"requested|part\b))",
    re.IGNORECASE,
)
# "PCCP (cleared as part of K234009)": the plan belongs to an earlier clearance.
_PCCP_INHERITED = re.compile(
    r"^.{0,60}?\bcleared\s+(?:as\s+part\s+of|in|under|with|through|via)\s+"
    r"((?:K|DEN|P)\d{6})",
    re.IGNORECASE,
)


@dataclass
class PccpResult:
    has_pccp: bool | None
    method: str  # see schemas.PccpMethod
    page: int | None = None
    quote: str | None = None
    reference_submission: str | None = None


def _classify_mention(doc: Document, match: re.Match[str]) -> tuple[str, str | None]:
    """negation | inherited | positive for one Summary-body PCCP mention."""
    before = doc.text[max(0, match.start() - 60) : match.start()]
    after = doc.text[match.end() : match.end() + 120]
    if _PCCP_NEGATION_BEFORE.search(before) or _PCCP_NEGATION_AFTER.search(after):
        return "negation", None
    inherited = _PCCP_INHERITED.search(after)
    if inherited and inherited.group(1).upper() != doc.submission_number:
        return "inherited", inherited.group(1).upper()
    return "positive", None


def extract_pccp(doc: Document, seg: Segmentation) -> PccpResult:
    """``has_pccp`` from the clearance letter, else a classified Summary statement.

    Precedence (ADR 0019): a correction letter -> ``False``; the clearance-letter
    sentence -> ``True``; otherwise the Summary-body mentions are classified --
    any positive -> ``True``; only inherited (a PCCP cleared under an earlier
    number) -> ``False`` with that number kept; only negations -> ``False``. No
    mention -> ``False`` when the document has readable text, ``None`` when it has
    none.
    """
    mentions = list(_PCCP.finditer(doc.text))

    def result(has: bool | None, method: str, start: int, end: int, ref: str | None = None):
        return PccpResult(
            has_pccp=has,
            method=method,
            page=doc.page_of(start),
            quote=doc.quote(start, end),
            reference_submission=ref,
        )

    for correction in _CORRECTION.finditer(doc.text):
        near = [m for m in mentions if abs(m.start() - correction.start()) <= _CORRECTION_REACH]
        if near:
            return result(False, "correction_letter", correction.start(), correction.end())

    letter = _LETTER_SENTENCE.search(doc.text)
    if letter:
        return result(True, "clearance_letter", letter.start(), letter.end())

    classified: dict[str, tuple[re.Match[str], str | None]] = {}
    for match in mentions:
        label, ref = _classify_mention(doc, match)
        classified.setdefault(label, (match, ref))
    if "positive" in classified:
        match, _ = classified["positive"]
        return result(True, "summary_statement", match.start(), match.end())
    if "inherited" in classified:
        match, ref = classified["inherited"]
        return result(False, "summary_inherited", match.start(), match.end(), ref)
    if "negation" in classified:
        match, _ = classified["negation"]
        return result(False, "summary_negation", match.start(), match.end())

    if any(kind != "unreadable" for kind in seg.page_kinds):
        return PccpResult(has_pccp=False, method="no_mention")
    return PccpResult(has_pccp=None, method="no_text")


# ---------------------------------------------------------------------------
# One bronze row -> one extraction record
# ---------------------------------------------------------------------------


def extract_document(
    row: Mapping[str, Any], *, now: dt.datetime | None = None
) -> DocumentExtractionRecord:
    """Run every extractor over one bronze row (a found PDF)."""
    doc = Document.from_raw(row["submission_number"], row.get("page_texts") or [])
    seg = segment(doc)
    preds = extract_predicates(doc, seg)
    cyber = extract_cybersecurity(doc, seg)
    pccp = extract_pccp(doc, seg)
    primary = preds.primary
    return DocumentExtractionRecord(
        submission_number=doc.submission_number,
        extractor_version=EXTRACTOR_VERSION,
        extracted_at=now or dt.datetime.now(dt.UTC).replace(tzinfo=None),
        source_content_sha256=row["content_sha256"],
        source_snapshot_id=row.get("source_snapshot_id"),
        source_fetched_at=row["fetched_at"],
        text_class=row.get("text_class"),
        page_count=len(doc.pages),
        letter_pages=seg.pages_of("fda_letter"),
        ifu_form_pages=seg.pages_of("ifu_form"),
        summary_pages=seg.pages_of("summary"),
        predicate_numbers=[h.number for h in preds.predicates],
        predicate_pages=[h.page for h in preds.predicates],
        predicate_methods=[h.method for h in preds.predicates],
        predicate_sections=[h.section for h in preds.predicates],
        primary_predicate=primary.number if primary else None,
        primary_predicate_page=primary.page if primary else None,
        primary_predicate_method=primary.method if primary else None,
        primary_predicate_basis=preds.primary_basis,
        primary_predicate_quote=primary.quote if primary else None,
        reference_device_numbers=preds.reference_devices,
        cybersecurity_statement_present=cyber.present,
        cybersecurity_basis=cyber.basis,
        cybersecurity_page=cyber.page,
        cybersecurity_quote=cyber.quote,
        has_pccp=pccp.has_pccp,
        pccp_method=pccp.method,
        pccp_page=pccp.page,
        pccp_quote=pccp.quote,
        pccp_reference_submission=pccp.reference_submission,
    )


# ---------------------------------------------------------------------------
# Yields: the numbers a finding reports against finding 0019
# ---------------------------------------------------------------------------


@dataclass
class Yields:
    documents: int = 0
    with_predicate: int = 0
    primary_basis: Counter[str] = field(default_factory=Counter)
    predicate_methods: Counter[str] = field(default_factory=Counter)
    cybersecurity: Counter[str] = field(default_factory=Counter)  # by basis
    # `no_mention` read from a `mixed` document: its scanned pages could hide a
    # statement (ADR 0019 Decision 5), so the size of that gap is reported.
    cybersecurity_no_mention_mixed: int = 0
    pccp: Counter[str] = field(default_factory=Counter)  # by method
    has_pccp: Counter[str] = field(default_factory=Counter)  # "True" / "False" / "None"

    def lines(self) -> list[str]:
        n = self.documents

        def pct(k: int) -> str:
            return f"{k:,} / {n:,} ({100 * k / n:.1f}%)" if n else "0 / 0"

        out = [
            f"documents extracted            : {n:,}",
            f"predicate found                : {pct(self.with_predicate)}"
            "   [finding 0019 proxy ceiling: 1,508 / 1,541 (97.9%)]",
            "  primary chosen by             : "
            + ", ".join(f"{k} {v:,}" for k, v in sorted(self.primary_basis.items())),
            "  predicate numbers by method   : "
            + ", ".join(f"{k} {v:,}" for k, v in sorted(self.predicate_methods.items())),
            f"cybersecurity present          : {pct(self.cybersecurity['mention'])}"
            "   [finding 0019 mentioned: 471 / 1,541 (30.6%)]",
            "  by basis                      : "
            + ", ".join(f"{k} {v:,}" for k, v in sorted(self.cybersecurity.items())),
            f"  no_mention in a mixed document: {self.cybersecurity_no_mention_mixed:,}",
            f"has_pccp True                  : {pct(self.has_pccp['True'])}"
            "   [finding 0019: 68 letter + 4 Summary positives]",
            "  by method                     : "
            + ", ".join(f"{k} {v:,}" for k, v in sorted(self.pccp.items())),
        ]
        return out


def summarise(records: Iterable[DocumentExtractionRecord]) -> Yields:
    y = Yields()
    for rec in records:
        y.documents += 1
        y.with_predicate += bool(rec.predicate_numbers)
        if rec.primary_predicate_basis:
            y.primary_basis[rec.primary_predicate_basis] += 1
        y.predicate_methods.update(rec.predicate_methods)
        y.cybersecurity[rec.cybersecurity_basis] += 1
        if rec.cybersecurity_basis == "no_mention" and rec.text_class == "mixed":
            y.cybersecurity_no_mention_mixed += 1
        y.pccp[rec.pccp_method] += 1
        y.has_pccp[str(rec.has_pccp)] += 1
    return y


# ---------------------------------------------------------------------------
# Spark: bronze -> silver_document_extraction, and the build-silver read
# ---------------------------------------------------------------------------


def latest_documents(spark, settings: Settings | None = None) -> list[dict[str, Any]]:
    """Each submission's newest *found* fetch from bronze (append-only history)."""
    from pyspark.sql import Window
    from pyspark.sql import functions as F

    from registry import tables

    settings = settings or get_settings()
    found = tables.read_table(spark, settings, BRONZE_TABLE).where("content_sha256 IS NOT NULL")
    newest = Window.partitionBy("submission_number").orderBy(
        F.col("fetched_at").desc(), F.col("content_sha256").desc()
    )
    latest = (
        found.withColumn("_rank", F.row_number().over(newest)).filter("_rank = 1").drop("_rank")
    )
    return [r.asDict() for r in latest.collect()]


@dataclass
class ExtractionRun:
    written: int
    yields: Yields


def run(spark, settings: Settings | None = None) -> ExtractionRun:
    """Rebuild ``silver_document_extraction`` from bronze. Entry point for the CLI.

    Overwrite, like every silver table: it is derived state, re-derivable from
    bronze at any time; bronze keeps the history (ADR 0018).
    """
    from registry import tables
    from registry.schemas import spark_schema_for

    settings = settings or get_settings()
    if not tables.table_exists(spark, settings, BRONZE_TABLE):
        raise ValueError(
            f"No {BRONZE_TABLE} table. Run `registry fetch-summaries` on a host that can "
            "reach accessdata.fda.gov first (ADR 0018)."
        )
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    records = [extract_document(row, now=now) for row in latest_documents(spark, settings)]
    if not records:
        raise ValueError(f"{BRONZE_TABLE} holds no fetched document; nothing to extract")
    df = spark.createDataFrame(
        [r.model_dump() for r in records], schema=spark_schema_for(DocumentExtractionRecord)
    )
    tables.write_table(df, settings, EXTRACTION_TABLE, mode="overwrite", merge_schema=True)
    logger.info("Wrote %d rows to %s", len(records), EXTRACTION_TABLE)
    return ExtractionRun(written=len(records), yields=summarise(records))


@dataclass(frozen=True)
class DocumentFacts:
    """What ``build-silver`` copies onto ``DeviceRecord`` from one extraction row."""

    predicate_submission_number: str | None
    has_pccp: bool | None
    cybersecurity_statement_present: bool | None


def facts_by_submission(spark, settings: Settings | None = None) -> dict[str, DocumentFacts]:
    """Read ``silver_document_extraction`` for the silver join; ``{}`` if absent.

    Silver must still build before any document is extracted (ADR 0013's rule for
    enrichment): a missing table leaves these fields ``None``.
    """
    from registry import tables

    settings = settings or get_settings()
    if not tables.table_exists(spark, settings, EXTRACTION_TABLE):
        logger.info(
            "No %s table; predicate, PCCP and cybersecurity stay null. "
            "Run: registry extract-documents",
            EXTRACTION_TABLE,
        )
        return {}
    rows = (
        tables.read_table(spark, settings, EXTRACTION_TABLE)
        .select(
            "submission_number",
            "primary_predicate",
            "has_pccp",
            "cybersecurity_statement_present",
        )
        .collect()
    )
    return {
        r["submission_number"]: DocumentFacts(
            predicate_submission_number=r["primary_predicate"],
            has_pccp=r["has_pccp"],
            cybersecurity_statement_present=r["cybersecurity_statement_present"],
        )
        for r in rows
    }
