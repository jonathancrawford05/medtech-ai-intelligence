"""The deterministic checks: every model value is accepted, flagged or rejected here.

Ported from the spike scorer (finding 0023, "The deterministic checks"; the scorer
itself never left the maintainer's Mac) and extended with the rules the gold set
added. The model is never asked whether it is right; this code decides, from the
document text alone. ADR 0022 Decision 4.

**The quote ladder** (`quote_status`), first match wins:

==================  =====================================================  ==========
``ok``              the quote is a substring of one page (NFKC, quotes and  accept
                    dashes unified, whitespace collapsed, case-folded,
                    trailing punctuation dropped)
``ok_loose``        the same, keeping only letters, digits, ``%`` and ``.``  accept
                    (``75 .9%``, ``per -protocol``)
``ok_gapped``       the quote's words appear in order as runs of >= 2       flagged
                    words with gaps <= 150 characters (a footer or page
                    break inside a sentence)
``ok_numbers``      quote not found, but the metric's value and its CI      flagged
                    bound(s) appear in order on one page with at most 3
                    numbers between them (a table row)
``quote_not_found`` none of the above                                       reject
==================  =====================================================  ==========

A found quote must then contain every number it supports -- the metric value and
each CI bound, or the sample size -- else ``value_not_in_quote`` (reject), unless
they appear on the other scale (0.95 vs 95%): ``ok_rescaled`` (flagged).

**Rules on top** (`check`), in this order:

- ``duplicate`` -- same metric, setting, value and CI as an earlier row: dropped.
- ``not_a_result`` -- the model itself marked a threshold or criterion (reject).
- ``ci_inconsistent`` -- ``ci_sided`` disagrees with the bounds given (reject).
- ``zero_triple`` -- ``0 (0-0)``, a figure built from a ``-`` cell (reject; gold set).
- ``measure_not_named`` -- neither the quote nor the label names the measure the
  metric claims (reject; 0 / 6 such rows were right in the gold set).
- ``no_quote`` -- a non-null scalar with no quote (reject); ``absent`` -- the one
  allowed exception, ``performance_reported=false`` with no metrics (accept).

The page is derived: wherever the quote (or the numbers) were found.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from registry.llm_extraction.pages import Page
from registry.llm_extraction.schema import ExtractionOutput, parse_output

# Bump on any rule change: silver rows record the version that judged them.
CHECKS_VERSION = "4d.1"

# The spike's prompt version: its responses have no applies_to / is_result / ci_sided.
SPIKE_PROMPT_VERSION = "4d-spike-4"

ACCEPT = frozenset({"ok", "ok_loose", "ok_gapped", "ok_numbers", "ok_rescaled", "absent"})
REVIEW = frozenset({"ok_gapped", "ok_numbers", "ok_rescaled"})
REJECT = frozenset(
    {
        "quote_not_found",
        "value_not_in_quote",
        "no_quote",
        "not_a_result",
        "ci_inconsistent",
        "zero_triple",
        "measure_not_named",
    }
)
DROP = frozenset({"duplicate"})
STATUSES = ACCEPT | REJECT | DROP

SCALAR_FIELDS = (
    "performance_reported",
    "study_design",
    "sample_size",
    "multi_site",
    "demographics_disclosed",
    "clinical_endpoint",
)
FIELDS = ("metric", *SCALAR_FIELDS)

_GAP_MAX = 150
_NUMBERS_BETWEEN_MAX = 3

# What a quote or label must say for a metric to carry that name. Lower-case
# fragments matched against the strict-normalised text; `other` claims no name.
MEASURE_NAMES: dict[str, tuple[str, ...]] = {
    "sensitivity": ("sensitiv", "true positive rate", "tpr"),
    "specificity": ("specificit", "true negative rate", "tnr"),
    "auc": ("auc", "area under", "roc", "c-statistic", "c statistic"),
    "ppv": ("ppv", "positive predictive"),
    "npv": ("npv", "negative predictive"),
    "accuracy": ("accura",),
    "precision": ("precision",),
    "recall": ("recall",),
    "f1": ("f1", "f-score", "f score", "f-measure"),
    "dice": ("dice",),
    "kappa": ("kappa",),
    "icc": ("icc", "intraclass", "intra-class"),
    "correlation": ("correlat", "pearson", "spearman", "r2", "r²", "r ="),
    "agreement": ("agree", "concordan"),
    "mae": ("mae", "mean absolute error", "absolute mean error"),
    "rmse": ("rmse", "root mean square", "root-mean-square", "rms error"),
    "arms": ("arms", "average root mean square", "a rms"),
    "mean_difference": ("mean difference", "mean diff", "bias", "mean error"),
}

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_TRANSLATE = str.maketrans(
    {
        **dict.fromkeys((0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2212), "-"),
        **dict.fromkeys((0x2018, 0x2019, 0x201B, 0x2032), "'"),
        **dict.fromkeys((0x201C, 0x201D, 0x201F, 0x2033), '"'),
        0x00AD: None,
        0x200B: None,
        0xFEFF: None,
    }
)
_TRAILING = " .,;:!?\"')]"
_LOOSE_DROP = re.compile(r"[^a-z0-9%.]")


def strict(text: str) -> str:
    """NFKC, quotes and dashes unified, whitespace collapsed, case-folded."""
    text = unicodedata.normalize("NFKC", text or "").translate(_TRANSLATE)
    # Control characters stand in for spaces in some text layers.
    text = "".join(" " if unicodedata.category(c) == "Cc" else c for c in text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def loose(text: str) -> str:
    """Only letters, digits, ``%`` and ``.`` survive (PDF spacing artefacts)."""
    return _LOOSE_DROP.sub("", strict(text))


_TOKEN_SPLIT = re.compile(r"[^a-z0-9%.]+")


def _tokens(text: str) -> list[str]:
    """Words for the gapped search: split on anything but letters, digits, ``%``
    and ``.`` (so ``92.26(88.05,`` is two numbers), sentence points dropped."""
    return [tok for tok in (w.strip(".") for w in _TOKEN_SPLIT.split(strict(text))) if tok]


def _quote_core(quote: str) -> str:
    return strict(quote).strip(_TRAILING)


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------

# 2,460 / 98.1 / .95 ; an artefact space before or after a decimal point is closed
# first ("75 .9%", "71. 8%"), on a copy, so a number is never split.
_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+")
_SPACED_POINT = re.compile(r"(\d) ?\. ?(?=\d)")


def numbers_in(text: str) -> list[float]:
    """Every unsigned number in ``text``, in order, read both raw and point-closed."""
    s = strict(text)
    found = [float(m.group(0).replace(",", "")) for m in _NUMBER.finditer(s)]
    closed = _SPACED_POINT.sub(r"\1.", s)
    if closed != s:
        found += [float(m.group(0).replace(",", "")) for m in _NUMBER.finditer(closed)]
    return found


def _same(a: float, b: float) -> bool:
    return abs(abs(a) - b) <= 1e-9 * max(1.0, abs(b))


def _number_status(wanted: Sequence[float], present: Sequence[float]) -> str:
    """``ok`` if every wanted number is present, ``ok_rescaled`` if only on the
    other scale (x100 or /100), else ``value_not_in_quote``."""
    rescaled = False
    for v in wanted:
        if any(_same(v, p) for p in present):
            continue
        if any(_same(v * 100, p) or _same(v / 100, p) for p in present):
            rescaled = True
            continue
        return "value_not_in_quote"
    return "ok_rescaled" if rescaled else "ok"


# ---------------------------------------------------------------------------
# The model's values, as candidates for checking
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One value the model proposed. ``index`` is its position among the metrics."""

    field: str
    quote: str | None
    value_text: str | None = None
    value_number: float | None = None
    value_bool: bool | None = None
    sample_unit: str | None = None
    # Metric only.
    index: int | None = None
    metric: str | None = None
    label: str | None = None
    context: str | None = None
    applies_to: str | None = None  # None: not asked (the spike prompt)
    setting: str | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    ci_sided: str | None = None
    is_result: bool | None = None  # None: not asked (the spike prompt)

    def numbers(self) -> list[float]:
        """The numbers the quote must carry."""
        if self.field == "metric":
            return [x for x in (self.value_number, self.ci_low, self.ci_high) if x is not None]
        if self.field == "sample_size" and self.value_number is not None:
            return [self.value_number]
        return []


def _infer_sided(low: float | None, high: float | None) -> str | None:
    if low is not None and high is not None:
        return "two"
    if low is not None:
        return "lower"
    return "upper" if high is not None else None


def _scalar(name: str, fact: Any, *, unit: str | None = None) -> Candidate | None:
    value = fact.get("value") if isinstance(fact, dict) else None
    quote = fact.get("quote") if isinstance(fact, dict) else None
    if value is None or value == "not_stated":
        return None
    if isinstance(value, bool):
        return Candidate(name, quote, value_text=str(value).lower(), value_bool=value)
    if name == "sample_size":
        return Candidate(
            name, quote, value_text=str(value), value_number=float(value), sample_unit=unit
        )
    return Candidate(name, quote, value_text=str(value))


def candidates(content: str | dict[str, Any], prompt_version: str) -> list[Candidate]:
    """The values in one response, metrics first, in the model's order.

    ``4d-spike-4`` responses (the recorded fixtures) predate ``applies_to``,
    ``is_result`` and ``ci_sided``: the first two are ``None`` (not asked), and the
    side is read from which bounds are present. Any other version must validate
    against `ExtractionOutput`.
    """
    if prompt_version == SPIKE_PROMPT_VERSION:
        data = json.loads(content) if isinstance(content, str) else content
        unit = data.get("sample_unit")
    else:
        parsed = (
            parse_output(content)
            if isinstance(content, str)
            else ExtractionOutput.model_validate(content)
        )
        data = parsed.model_dump()
        unit = data["sample_size"]["unit"]
    out: list[Candidate] = []
    for i, m in enumerate(data.get("metrics") or []):
        low, high = m.get("ci_low"), m.get("ci_high")
        out.append(
            Candidate(
                "metric",
                m.get("quote"),
                value_text=None if m.get("value") is None else str(m["value"]),
                value_number=None if m.get("value") is None else float(m["value"]),
                index=i,
                metric=m.get("metric"),
                label=m.get("label"),
                context=m.get("context"),
                applies_to=m.get("applies_to"),
                setting=m.get("setting"),
                ci_low=low,
                ci_high=high,
                ci_sided=m["ci_sided"] if "ci_sided" in m else _infer_sided(low, high),
                is_result=m.get("is_result"),
            )
        )
    for name in SCALAR_FIELDS:
        cand = _scalar(name, data.get(name), unit=unit if name == "sample_size" else None)
        if cand is not None:
            out.append(cand)
    return out


# ---------------------------------------------------------------------------
# Locating a quote
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Located:
    status: str  # ok | ok_loose | ok_gapped | ok_numbers | quote_not_found
    pages: list[int] = field(default_factory=list)


class DocumentText:
    """A document's pages, pre-normalised once for every check against them."""

    def __init__(self, pages: Iterable[Page]):
        self.pages = list(pages)
        self._strict = [strict(p.text) for p in self.pages]
        self._loose = [loose(p.text) for p in self.pages]
        self._numbers = [numbers_in(p.text) for p in self.pages]
        # One token stream for the gapped search, which may cross a page break.
        self._tok_text = ""
        self._tok_page: list[tuple[int, int]] = []  # (offset, page)
        for p in self.pages:
            self._tok_page.append((len(self._tok_text), p.page))
            self._tok_text += " ".join(_tokens(p.text)) + " "

    def _page_at(self, offset: int) -> int:
        page = self._tok_page[0][1]
        for start, n in self._tok_page:
            if start <= offset:
                page = n
        return page

    def find(self, quote: str) -> Located:
        core = _quote_core(quote)
        if not core:
            return Located("quote_not_found")
        for p, text in zip(self.pages, self._strict, strict=True):
            if core in text:
                return Located("ok", [p.page])
        lq = loose(core)
        if lq:
            for p, text in zip(self.pages, self._loose, strict=True):
                if lq in text:
                    return Located("ok_loose", [p.page])
        pages = self._gapped(_tokens(core))
        if pages:
            return Located("ok_gapped", pages)
        return Located("quote_not_found")

    def _gapped(self, words: list[str]) -> list[int]:
        """Pages spanned when ``words`` appear in order as runs of >= 2 words,
        each within ``_GAP_MAX`` characters of the last; else ``[]``.

        Every occurrence of a run is tried (a phrase such as "relative sensitivity
        of" can recur on the page before the one the quote is from, K251221).
        """
        if len(words) < 2 or not self._tok_text:
            return []
        text = self._tok_text
        budget = [10_000]  # occurrence tries; quotes are short, this never binds on real text

        def runs_from(i: int, prev_end: int | None) -> int | None:
            """End offset of a match of words[i:], or None."""
            if i == len(words):
                return prev_end
            for j in range(len(words), i + 1, -1):
                needle = " ".join(words[i:j])
                k = text.find(needle, 0 if prev_end is None else prev_end)
                while k != -1:
                    budget[0] -= 1
                    if budget[0] < 0 or (prev_end is not None and k - prev_end > _GAP_MAX):
                        break
                    if _at_word(text, k, len(needle)):
                        end = runs_from(j, k + len(needle))
                        if end is not None:
                            starts.append(k)
                            return end
                    k = text.find(needle, k + 1)
            return None

        starts: list[int] = []
        end = runs_from(0, None)
        if end is None:
            return []
        return sorted({self._page_at(min(starts)), self._page_at(end - 1)})

    def find_numbers(self, wanted: Sequence[float]) -> list[int]:
        """The first page where ``wanted`` appear in order with at most
        ``_NUMBERS_BETWEEN_MAX`` numbers between neighbours; else ``[]``."""
        for p, nums in zip(self.pages, self._numbers, strict=True):
            if _in_order(wanted, nums):
                return [p.page]
        return []


def _at_word(text: str, k: int, n: int) -> bool:
    before = k == 0 or text[k - 1] == " "
    after = k + n >= len(text) or text[k + n] == " "
    return before and after


def _in_order(wanted: Sequence[float], nums: Sequence[float]) -> bool:
    for start, x in enumerate(nums):
        if not _same(wanted[0], x):
            continue
        pos, ok = start, True
        for w in wanted[1:]:
            nxt = next(
                (
                    q
                    for q in range(pos + 1, min(len(nums), pos + 2 + _NUMBERS_BETWEEN_MAX))
                    if _same(w, nums[q])
                ),
                None,
            )
            if nxt is None:
                ok = False
                break
            pos = nxt
        if ok:
            return True
    return False


# ---------------------------------------------------------------------------
# Judging one value
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Checked:
    candidate: Candidate
    quote_status: str | None  # the ladder alone (None: no quote to look for)
    status: str  # after every rule; see STATUSES
    pages: list[int] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.status in ACCEPT

    @property
    def needs_review(self) -> bool:
        return self.status in REVIEW


def quote_status(cand: Candidate, doc: DocumentText) -> Located:
    """The spike's ladder: where the quote is, and whether it carries its numbers."""
    wanted = cand.numbers()
    located = doc.find(cand.quote or "")
    if located.status == "quote_not_found":
        # A table row: the figures without the stitched quote (metrics with a CI only;
        # a lone number is found on almost any page).
        if cand.field == "metric" and len(wanted) >= 2:
            pages = doc.find_numbers(wanted)
            if pages:
                return Located("ok_numbers", pages)
        return located
    if wanted:
        numbers = _number_status(wanted, numbers_in(cand.quote or ""))
        if numbers != "ok":
            return Located(numbers, located.pages)
    return located


def names_measure(cand: Candidate) -> bool:
    """Does the quote or the label name the measure the metric claims?"""
    names = MEASURE_NAMES.get(cand.metric or "other")
    if not names:
        return True
    said = f" {strict(cand.quote or '')} | {strict(cand.label or '')} "
    return any(_names(said, n) for n in names)


def _names(text: str, name: str) -> bool:
    # Short acronyms must stand alone ("auc" not in "caucasian", "mae" not in "maestro").
    if len(name) <= 4 and name.isalnum():
        return re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", text) is not None
    return name in text


def _ci_consistent(cand: Candidate) -> bool:
    low, high = cand.ci_low is not None, cand.ci_high is not None
    expected = {
        "two": (True, True),
        "lower": (True, False),
        "upper": (False, True),
        None: (False, False),
    }
    return expected.get(cand.ci_sided) == (low, high)


def _metric_key(cand: Candidate) -> tuple[Any, ...]:
    return (cand.metric, cand.setting, cand.value_number, cand.ci_low, cand.ci_high)


def check(cands: Sequence[Candidate], doc: DocumentText) -> list[Checked]:
    """Judge every candidate of one response against its document's pages."""
    seen: set[tuple[Any, ...]] = set()
    has_metrics = any(c.field == "metric" for c in cands)
    out: list[Checked] = []
    for cand in cands:
        if cand.field == "metric":
            key = _metric_key(cand)
            if key in seen:
                out.append(Checked(cand, None, "duplicate"))
                continue
            seen.add(key)
        if not cand.quote:
            if (
                cand.field == "performance_reported"
                and cand.value_bool is False
                and not has_metrics
            ):
                out.append(Checked(cand, None, "absent"))
            else:
                out.append(Checked(cand, None, "no_quote"))
            continue
        located = quote_status(cand, doc)
        status = located.status
        if cand.field == "metric" and status in ACCEPT:
            status = _metric_rule(cand) or status
        out.append(Checked(cand, located.status, status, located.pages))
    return out


def _metric_rule(cand: Candidate) -> str | None:
    if cand.is_result is False:
        return "not_a_result"
    if not _ci_consistent(cand):
        return "ci_inconsistent"
    if cand.value_number == 0 and cand.ci_low == 0 and cand.ci_high == 0:
        return "zero_triple"
    if not names_measure(cand):
        return "measure_not_named"
    return None


def check_response(
    content: str | dict[str, Any], prompt_version: str, pages: Iterable[Page]
) -> list[Checked]:
    """Parse one response and judge it against ``pages`` (the pages it was sent)."""
    return check(candidates(content, prompt_version), DocumentText(pages))
