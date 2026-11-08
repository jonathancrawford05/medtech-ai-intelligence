"""Each device's headline sensitivity and specificity, from one LLM run (ADR 0023).

Metrics cleared the bar on gold set v2 (finding 0024: 14 / 15 accepted right), so
they ship; nothing else from the LLM pass does. This module turns one run's checked
evidence (`silver_llm_evidence`) into one row per answered document in
``silver_device_performance``, and is the only way an LLM figure reaches a device.

**What counts.** A metric row that the checks accepted, that the model attributed to
the subject device (``applies_to``) and marked as a result (``is_result``), in the
standalone setting, named ``sensitivity`` or ``specificity``. A run whose prompt
never asked for attribution (``4d-spike-4``) therefore ships nothing.

**One figure per measure**, by `SELECTION_VERSION`'s rule:

``single``            the only distinct figure.
``overall``           of several, exactly one is not tied to a subgroup (no context,
                      or a context made only of words such as "overall", "all",
                      "total", "pooled": "Overall (all sites)", not "all female").
``first_of_several``  otherwise the first in document order, among the overall
                      figures if there are any, else among all; flagged for review.

**Scale.** Figures are kept as written and stored as a proportion beside them: over
1, or written with ``%`` in the quote, means a percentage. A result above 1 is not a
proportion and is dropped.

``reports_sensitivity_specificity`` is True when a figure was chosen and None
otherwise, never False: the gold set measured precision, not absence.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from registry.config.settings import Settings, get_settings
from registry.llm_extraction import checks
from registry.llm_extraction import schema as sc
from registry.schemas import DevicePerformanceRecord, LlmEvidenceRecord

logger = logging.getLogger(__name__)

PERFORMANCE_TABLE = "silver_device_performance"

# Bump on any rule change: rows record the rule that chose them.
SELECTION_VERSION = "perf.2"

MEASURES = ("sensitivity", "specificity")

# A context names the whole test set only when it says nothing else: every word is
# one of these. "Overall (all sites)" is overall; "all female" and "primary care"
# are subgroups (PR #26 review). Filler words describe the set, not a part of it.
_OVERALL_WORDS = frozenset(
    ["overall", "all", "total", "pooled", "combined", "entire", "primary", "whole"]
)
_FILLER_WORDS = frozenset(
    re.findall(
        r"[a-z]+",
        "the of in and set dataset data population cohort sample test testing study "
        "sites site patients subjects analysis endpoint results",
    )
)
_WORD = re.compile(r"[a-z]+")


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def eligible(e: LlmEvidenceRecord) -> bool:
    """Can this evidence row supply a shipped figure?"""
    return (
        e.field == "metric"
        and e.accepted
        and e.metric in MEASURES
        and e.setting == "standalone"
        and e.applies_to == "subject_device"
        and e.is_result is True
        and e.value_number is not None
        and e.quote is not None
        and bool(e.quote_pages)
    )


def as_proportion(value: float, quote: str | None) -> float | None:
    """The figure as a 0-1 proportion; None when it cannot be one."""
    percent = value > 1 or any(checks._same(value, p) for p in checks.percents_in(quote or ""))
    out = value / 100 if percent else value
    return out if 0 <= out <= 1 else None


def is_overall(context: str | None) -> bool:
    """No context, or one made only of overall and filler words."""
    words = _WORD.findall((context or "").lower())
    if not words:
        return True
    return any(w in _OVERALL_WORDS for w in words) and all(
        w in _OVERALL_WORDS or w in _FILLER_WORDS for w in words
    )


@dataclass(frozen=True)
class _Choice:
    row: LlmEvidenceRecord
    proportion: float
    basis: str
    candidates: int


def choose(rows: Sequence[LlmEvidenceRecord]) -> _Choice | None:
    """One measure's figure for one device, from its eligible rows."""
    distinct: dict[float, tuple[LlmEvidenceRecord, float]] = {}
    for r in sorted(rows, key=lambda r: r.metric_index or 0):
        p = as_proportion(r.value_number, r.quote)
        if p is None:
            continue
        key = round(p, 9)
        if key not in distinct:  # the first occurrence, in document order
            distinct[key] = (r, p)
        elif is_overall(r.context) and not is_overall(distinct[key][0].context):
            # The same figure stated again for the whole set: it is an overall figure.
            distinct[key] = (r, p)
    if not distinct:
        return None
    figures = list(distinct.values())
    n = len(figures)
    if n == 1:
        r, p = figures[0]
        return _Choice(r, p, "single", 1)
    overall = [f for f in figures if is_overall(f[0].context)]
    if len(overall) == 1:
        r, p = overall[0]
        return _Choice(r, p, "overall", n)
    r, p = (overall or figures)[0]
    return _Choice(r, p, "first_of_several", n)


def _fields(name: str, c: _Choice | None) -> dict[str, Any]:
    if c is None:
        return {}
    return {
        name: c.proportion,
        f"{name}_as_written": c.row.value_number,
        f"{name}_label": c.row.label,
        f"{name}_context": c.row.context,
        f"{name}_quote": c.row.quote,
        f"{name}_pages": list(c.row.quote_pages),
        f"{name}_basis": c.basis,
        f"{name}_candidates": c.candidates,
    }


def select(
    evidence: Iterable[LlmEvidenceRecord], *, built_at: dt.datetime | None = None
) -> list[DevicePerformanceRecord]:
    """One row per document in one run's evidence, figures chosen by the rule above."""
    evidence = list(evidence)
    combos = {(e.model_digest, e.prompt_version) for e in evidence}
    if len(combos) > 1:
        raise ValueError(
            f"evidence mixes {len(combos)} model/prompt combinations; select from one run"
        )
    built_at = built_at or _utcnow()
    by_doc: dict[str, list[LlmEvidenceRecord]] = {}
    for e in evidence:
        by_doc.setdefault(e.submission_number, []).append(e)
    out = []
    for number in sorted(by_doc):
        rows = by_doc[number]
        first = rows[0]
        runs = {r.run_id for r in rows}
        if len(runs) > 1:
            # build-evidence keeps one response per (document, digest, prompt), so a
            # document's rows share one run; anything else would mix two answers.
            raise ValueError(f"{number}: evidence from {len(runs)} runs ({sorted(runs)})")
        chosen = {m: choose([r for r in rows if eligible(r) and r.metric == m]) for m in MEASURES}
        picked = [c for c in chosen.values() if c is not None]
        out.append(
            DevicePerformanceRecord(
                submission_number=number,
                run_id=first.run_id,
                model=first.model,
                model_digest=first.model_digest,
                prompt_version=first.prompt_version,
                checks_version=first.checks_version,
                selection_version=SELECTION_VERSION,
                built_at=built_at,
                source_content_sha256=first.source_content_sha256,
                reports_sensitivity_specificity=True if picked else None,
                needs_review=any(
                    c.row.needs_review or c.basis == "first_of_several" for c in picked
                ),
                **_fields("sensitivity", chosen["sensitivity"]),
                **_fields("specificity", chosen["specificity"]),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


@dataclass
class PerformanceYields:
    documents: int = 0
    with_sensitivity: int = 0
    with_specificity: int = 0
    reporting: int = 0
    needs_review: int = 0
    basis: Counter[str] = field(default_factory=Counter)
    run: str | None = None

    def lines(self) -> list[str]:
        head = f"{self.run}: " if self.run else ""
        return [
            f"{head}documents: {self.documents:,}",
            f"  with a figure (reports_sensitivity_specificity): {self.reporting:,}",
            f"  sensitivity: {self.with_sensitivity:,}   specificity: {self.with_specificity:,}",
            "  chosen as: " + (", ".join(f"{b} {n}" for b, n in sorted(self.basis.items())) or "-"),
            f"  needs review: {self.needs_review:,}",
        ]


def summarise(records: Iterable[DevicePerformanceRecord]) -> PerformanceYields:
    y = PerformanceYields()
    for r in records:
        y.documents += 1
        y.reporting += bool(r.reports_sensitivity_specificity)
        y.with_sensitivity += r.sensitivity is not None
        y.with_specificity += r.specificity is not None
        y.needs_review += r.needs_review
        y.basis.update(b for b in (r.sensitivity_basis, r.specificity_basis) if b)
        y.run = f"{r.model} / {r.prompt_version}"
    return y


# ---------------------------------------------------------------------------
# Spark
# ---------------------------------------------------------------------------


def one_run(
    evidence: Iterable[LlmEvidenceRecord],
    *,
    prompt_version: str = sc.PROMPT_VERSION,
    model_digest: str | None = None,
) -> list[LlmEvidenceRecord]:
    """The evidence of one run: a prompt version and, when several models ran it, a digest."""
    rows = [
        e
        for e in evidence
        if e.prompt_version == prompt_version
        and (model_digest is None or e.model_digest.startswith(model_digest))
    ]
    digests = sorted({e.model_digest for e in rows})
    if len(digests) > 1:
        raise ValueError(
            f"prompt {prompt_version!r} ran on {len(digests)} models "
            f"({', '.join(d[:12] for d in digests)}); pick one with --model-digest"
        )
    return rows


@dataclass
class PerformanceBuild:
    written: int
    yields: PerformanceYields


def run(
    spark,
    settings: Settings | None = None,
    *,
    prompt_version: str = sc.PROMPT_VERSION,
    model_digest: str | None = None,
) -> PerformanceBuild:
    """Rebuild ``silver_device_performance`` from one run in ``silver_llm_evidence``."""
    from registry import tables
    from registry.llm_extraction import run as llm_run
    from registry.schemas import spark_schema_for

    settings = settings or get_settings()
    if not tables.table_exists(spark, settings, llm_run.EVIDENCE_TABLE):
        raise ValueError(f"No {llm_run.EVIDENCE_TABLE} table. Run `registry build-evidence` first.")
    evidence = one_run(
        llm_run.read_evidence(spark, settings),
        prompt_version=prompt_version,
        model_digest=model_digest,
    )
    if not evidence:
        raise ValueError(f"No evidence for prompt {prompt_version!r} in {llm_run.EVIDENCE_TABLE}")
    records = select(evidence)
    df = spark.createDataFrame(
        [r.model_dump() for r in records], schema=spark_schema_for(DevicePerformanceRecord)
    )
    tables.write_table(df, settings, PERFORMANCE_TABLE, mode="overwrite", merge_schema=True)
    logger.info("Wrote %d rows to %s", len(records), PERFORMANCE_TABLE)
    return PerformanceBuild(len(records), summarise(records))


def by_submission(spark, settings: Settings | None = None) -> dict[str, dict[str, Any]]:
    """submission -> its performance row; ``{}`` when the pass has not run."""
    from registry import tables

    settings = settings or get_settings()
    if not tables.table_exists(spark, settings, PERFORMANCE_TABLE):
        return {}
    return {
        r["submission_number"]: r.asDict()
        for r in tables.read_table(spark, settings, PERFORMANCE_TABLE).collect()
    }
