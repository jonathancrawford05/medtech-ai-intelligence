"""Score a run against a labelled gold set, per field and per check status (ADR 0022 Decision 7).

A gold row is one value a model once proposed, with a judgement against the full
Summary text: ``y`` (right), ``n`` (wrong, with ``correct_value``), ``partly`` or
``unsure``. Scoring a run means finding, for each gold row, the run's value for the
same thing and asking two questions:

- **Is the value right?** The gold judgement carries over when the run proposed the
  same value (and, for a metric, the same measure name). When the run says
  something else, the row's ``correct_value`` decides if it names a value
  (``mixed``, ``monitoring``, ``other: …``); otherwise the row is ``unjudged`` and
  needs a fresh label.
- **What did the checks do with it?** Accepted, flagged or rejected.

So every gold row lands in one cell of {accepted, rejected, missing} x {right,
wrong, other}, tallied per field, per check status and per sampling stratum. The
number that decides whether a field ships is the precision of what the checks
**accept**; a rejected wrong value is the checks working, a rejected right value is
recall lost. The bar itself is the maintainer's call on a fresh draw (handoff §3
rule 6); `Tally.clears` only reports against one.
"""

from __future__ import annotations

import csv
import typing as t
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from registry.llm_extraction import schema as sc
from registry.schemas import LlmEvidenceRecord

Verdict = t.Literal["right", "wrong", "other", "unjudged"]
_MEASURES = frozenset(t.get_args(sc.Measure))
_ENUM_VALUES = (
    frozenset(t.get_args(sc.StudyDesign))
    | frozenset(t.get_args(sc.ClinicalEndpoint))
    | {"true", "false"}
)


@dataclass(frozen=True)
class GoldRow:
    row: int
    submission_number: str
    field: str  # metric | performance_reported | ... (FIELDS)
    value: str
    metric: str | None
    setting: str | None
    label: str | None
    check_status: str  # the spike's status when the row was drawn
    why_sampled: str
    judgement: str  # y | n | partly | unsure
    correct_value: str

    @property
    def value_number(self) -> float | None:
        try:
            return float(self.value)
        except ValueError:
            return None


def load_gold(path: Path) -> list[GoldRow]:
    """The gold CSV (``tests/fixtures/llm_extraction/gold_set_v1.csv``'s columns)."""
    rows = []
    with path.open(newline="") as fh:
        for r in csv.DictReader(fh):
            metric = setting = None
            name = r["field"]
            if name.startswith("metric:"):
                metric, setting = name.removeprefix("metric:").split("/", 1)
                name = "metric"
            rows.append(
                GoldRow(
                    row=int(r["row"]),
                    submission_number=r["submission_number"].strip().upper(),
                    field=name,
                    value=r["value"],
                    metric=metric,
                    setting=setting,
                    label=r["label"] or None,
                    check_status=r["check_status"],
                    why_sampled=r["why_sampled"],
                    judgement=r["judgement"].strip().lower(),
                    correct_value=r["correct_value"],
                )
            )
    return rows


# ---------------------------------------------------------------------------
# Matching a gold row to a run's value
# ---------------------------------------------------------------------------


def _same_number(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= 1e-9 * max(1.0, abs(b))


def match(gold: GoldRow, evidence: Sequence[LlmEvidenceRecord]) -> LlmEvidenceRecord | None:
    """The run's value for what ``gold`` judged; duplicates are never the match.

    Metrics are found by their figure: same value, then preferring the same measure,
    setting and label in that order (a run may rename what it found). A scalar is the
    run's value for that field, whatever it is.
    """
    mine = [
        e
        for e in evidence
        if e.submission_number == gold.submission_number
        and e.field == gold.field
        and e.check_status != "duplicate"
    ]
    if gold.field != "metric":
        return mine[0] if mine else None
    same = [e for e in mine if _same_number(e.value_number, gold.value_number)]
    if not same:
        return None
    same.sort(
        key=lambda e: (e.metric != gold.metric, e.setting != gold.setting, e.label != gold.label)
    )
    return same[0]


def _leading_value(text: str) -> str | None:
    """``correct_value``'s value when it names exactly one: ``mixed``, ``other: …``,
    ``True``; never an alternative ("deterioration_risk or monitoring")."""
    claim = text.split("(", 1)[0].split(":", 1)[0].lower()
    if " or " in f" {claim} ":
        return None
    head = text.strip().split(" ", 1)[0].split(":", 1)[0].strip().lower()
    if head in _MEASURES or head in _ENUM_VALUES:
        return head
    if text.strip().lower().startswith(("not stated", "not reported", "not a ")):
        return "null"
    return None


def verdict(gold: GoldRow, run_value: LlmEvidenceRecord | None) -> Verdict:
    """Is the run's value right, by this gold row?"""
    if gold.judgement not in ("y", "n"):
        return "other"
    if run_value is None:
        # The run proposed nothing: right exactly when the truth is "not stated".
        return (
            "right"
            if gold.judgement == "n" and _leading_value(gold.correct_value) == "null"
            else "unjudged"
        )
    if gold.field == "metric":
        if run_value.metric == gold.metric:
            return "right" if gold.judgement == "y" else "wrong"
        truth = gold.metric if gold.judgement == "y" else _leading_value(gold.correct_value)
        if truth in _MEASURES:
            return "right" if run_value.metric == truth else "wrong"
        return "unjudged"
    proposed = (run_value.value_text or "").lower()
    if proposed == gold.value.lower():
        return "right" if gold.judgement == "y" else "wrong"
    truth = gold.value.lower() if gold.judgement == "y" else _leading_value(gold.correct_value)
    if truth is None:
        return "unjudged"
    return "right" if proposed == truth else "wrong"


# ---------------------------------------------------------------------------
# Tallies
# ---------------------------------------------------------------------------


@dataclass
class Tally:
    """Gold rows in one group, by what the checks did and whether the value is right."""

    accepted_right: int = 0
    accepted_wrong: int = 0
    accepted_other: int = 0  # partly / unsure / unjudged
    flagged: int = 0  # of the accepted, how many are flagged for review
    rejected_right: int = 0  # recall lost
    rejected_wrong: int = 0  # the checks working
    rejected_other: int = 0
    missing: int = 0  # the run proposed nothing for the row

    def add(self, accepted: bool | None, flagged: bool, v: Verdict) -> None:
        if accepted is None:
            self.missing += 1
            return
        side = "accepted" if accepted else "rejected"
        cell = {"right": "right", "wrong": "wrong"}.get(v, "other")
        setattr(self, f"{side}_{cell}", getattr(self, f"{side}_{cell}") + 1)
        if accepted and flagged:
            self.flagged += 1

    @property
    def rows(self) -> int:
        return (
            self.accepted_right
            + self.accepted_wrong
            + self.accepted_other
            + self.rejected_right
            + self.rejected_wrong
            + self.rejected_other
            + self.missing
        )

    @property
    def accepted(self) -> int:
        return self.accepted_right + self.accepted_wrong + self.accepted_other

    @property
    def precision(self) -> float | None:
        """Right share of the accepted values that have a y/n verdict."""
        judged = self.accepted_right + self.accepted_wrong
        return self.accepted_right / judged if judged else None

    def clears(self, bar: float, min_judged: int) -> bool:
        judged = self.accepted_right + self.accepted_wrong
        return judged >= min_judged and (self.precision or 0.0) >= bar

    def line(self, name: str) -> str:
        p = "  n/a" if self.precision is None else f"{100 * self.precision:5.1f}%"
        return (
            f"{name:<34} rows {self.rows:>3} | accepted {self.accepted:>3} "
            f"(right {self.accepted_right}, wrong {self.accepted_wrong}, "
            f"other {self.accepted_other}; flagged {self.flagged}) precision {p} | "
            f"rejected right {self.rejected_right}, wrong {self.rejected_wrong}, "
            f"other {self.rejected_other} | missing {self.missing}"
        )


@dataclass(frozen=True)
class Scored:
    gold: GoldRow
    evidence: LlmEvidenceRecord | None
    verdict: Verdict


@dataclass
class GoldScore:
    scored: list[Scored]
    by_field: dict[str, Tally] = field(default_factory=dict)
    by_status: dict[str, Tally] = field(default_factory=dict)
    by_stratum: dict[str, Tally] = field(default_factory=dict)

    def lines(self, *, bar: float | None = None, min_judged: int = 1) -> list[str]:
        out = ["BY FIELD"]
        for name, tally in self.by_field.items():
            line = tally.line(name)
            if bar is not None:
                line += (
                    f" | bar {bar:.0%}: {'clears' if tally.clears(bar, min_judged) else 'not yet'}"
                )
            out.append("  " + line)
        out.append("BY CHECK STATUS (this run's)")
        out += ["  " + t.line(s) for s, t in self.by_status.items()]
        out.append("BY SAMPLING STRATUM")
        out += ["  " + t.line(s) for s, t in self.by_stratum.items()]
        return out


def score(gold: Iterable[GoldRow], evidence: Sequence[LlmEvidenceRecord]) -> GoldScore:
    """Every gold row against one run's evidence rows (one model digest and prompt)."""
    combos = {(e.model_digest, e.prompt_version) for e in evidence}
    if len(combos) > 1:
        raise ValueError(
            f"evidence mixes {len(combos)} model/prompt combinations; score one at a time: "
            + ", ".join(f"{d[:12]}/{p}" for d, p in sorted(combos))
        )
    result = GoldScore(scored=[])
    for g in gold:
        e = match(g, evidence)
        v = verdict(g, e)
        result.scored.append(Scored(g, e, v))
        accepted = None if e is None else e.accepted
        flagged = bool(e and e.needs_review)
        status = "(missing)" if e is None else e.check_status
        for groups, key in (
            (result.by_field, g.field),
            (result.by_status, status),
            (result.by_stratum, g.why_sampled),
        ):
            groups.setdefault(key, Tally()).add(accepted, flagged, v)
    return result
