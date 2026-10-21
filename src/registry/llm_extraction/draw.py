"""Draw a fresh gold set from one run (ADR 0022 Decision 7; finding 0024).

Gold v1 steered three prompt versions, so it is development data now; a field's bar is
judged on a draw from documents nobody tuned on. `draw` samples one run's values by
stratum, reproducibly (a seed), from documents not in any excluded gold set, and writes
three files beside each other:

``<name>.csv``          the key: gold_set_v1's columns, judgement columns empty. This is
                        what `load_gold` / `score-gold` read once it is merged.
``<name>.blind.csv``    the labelling sheet: the same rows **without** ``check_status``
                        and ``why_sampled`` (v1 was labelled blind to both), in shuffled
                        order, so nothing in it says which rows the checks doubted.
``<name>.strata.csv``   each stratum's population and how many were drawn, so a
                        population estimate can weight the strata (finding 0023 did).

`merge` puts the labelled blind sheet back onto the key by ``row``.

Unlike v1, which drew only accepted values, the draw also samples **rejected** ones: a
rejected right value is recall the checks cost, and v1 could not measure it.
"""

from __future__ import annotations

import csv
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from registry.llm_extraction import checks
from registry.llm_extraction import schema as sc
from registry.schemas import LlmEvidenceRecord

GOLD_COLUMNS = (
    "row",
    "submission_number",
    "field",
    "value",
    "label",
    "ci",
    "quote",
    "page_cited",
    "check_status",
    "why_sampled",
    "judgement",
    "correct_value",
    "evidence_page",
    "evidence_quote",
    "reason",
)
_HIDDEN = ("check_status", "why_sampled")
LABEL_COLUMNS = ("judgement", "correct_value", "evidence_page", "evidence_quote", "reason")

# Stratum -> rows to draw. Names follow gold v1's `why_sampled` where the stratum is the
# same, so `score-gold` reports v1 and v2 side by side.
DEFAULT_QUOTAS: dict[str, int] = {
    "metric: plain accept": 10,
    "metric: gapped quote": 5,
    "metric: table numbers": 5,
    "metric: rescaled": 3,
    "metric: rejected": 6,
    "sample_size": 5,
    "study_design": 5,
    "demographics_disclosed": 5,
    "multi_site": 5,
    "performance_reported": 4,
    "scalar: rejected": 6,
    "endpoint: risk": 6,
    "endpoint: not risk": 8,
}


def stratum(e: LlmEvidenceRecord) -> str | None:
    """Which stratum a value belongs to; None for a dropped duplicate."""
    if e.check_status in checks.DROP:
        return None
    if e.field == "metric":
        if not e.accepted:
            return "metric: rejected"
        return {
            "ok_gapped": "metric: gapped quote",
            "ok_numbers": "metric: table numbers",
            "ok_rescaled": "metric: rescaled",
        }.get(e.check_status, "metric: plain accept")
    if not e.accepted:
        return "scalar: rejected"
    if e.field == "clinical_endpoint":
        return "endpoint: risk" if e.value_text in sc.RISK_ENDPOINTS else "endpoint: not risk"
    return e.field


def _ci(e: LlmEvidenceRecord) -> str:
    if e.ci_low is None and e.ci_high is None:
        return ""
    return f"{e.ci_low}-{e.ci_high}"


def _key_row(n: int, e: LlmEvidenceRecord, why: str) -> dict[str, str]:
    field = f"metric:{e.metric}/{e.setting}" if e.field == "metric" else e.field
    return {
        "row": str(n),
        "submission_number": e.submission_number,
        "field": field,
        "value": e.value_text or "",
        "label": e.label or "",
        "ci": _ci(e),
        "quote": e.quote or "",
        "page_cited": ";".join(map(str, e.quote_pages)),
        "check_status": e.check_status,
        "why_sampled": why,
        **dict.fromkeys(LABEL_COLUMNS, ""),
    }


def submissions_in(gold_paths: Iterable[Path]) -> set[str]:
    out: set[str] = set()
    for path in gold_paths:
        with path.open(newline="") as fh:
            out |= {r["submission_number"].strip().upper() for r in csv.DictReader(fh)}
    return out


@dataclass(frozen=True)
class Draw:
    rows: list[dict[str, str]]  # the key, in row order
    population: dict[str, int]  # stratum -> eligible values
    drawn: dict[str, int]
    documents: int  # eligible documents after exclusions


def draw(
    evidence: Sequence[LlmEvidenceRecord],
    *,
    seed: int,
    exclude: Iterable[str] = (),
    quotas: Mapping[str, int] | None = None,
) -> Draw:
    """Sample one run's values by stratum, reproducibly, from documents not excluded."""
    combos = {(e.model_digest, e.prompt_version) for e in evidence}
    if len(combos) > 1:
        raise ValueError(
            f"evidence mixes {len(combos)} model/prompt combinations; draw from one run"
        )
    quotas = dict(DEFAULT_QUOTAS if quotas is None else quotas)
    excluded = {k.upper() for k in exclude}
    by_stratum: dict[str, list[LlmEvidenceRecord]] = {s: [] for s in quotas}
    eligible_docs: set[str] = set()
    for e in evidence:
        if e.submission_number in excluded:
            continue
        eligible_docs.add(e.submission_number)
        s = stratum(e)
        if s in by_stratum:
            by_stratum[s].append(e)
    rng = random.Random(seed)
    picked: list[tuple[LlmEvidenceRecord, str]] = []
    for name, pool in by_stratum.items():
        pool.sort(
            key=lambda e: (e.submission_number, e.field, e.metric_index or 0, e.value_text or "")
        )
        for e in rng.sample(pool, min(quotas[name], len(pool))):
            picked.append((e, name))
    rng.shuffle(picked)  # row order says nothing about the stratum
    rows = [_key_row(i, e, why) for i, (e, why) in enumerate(picked, start=1)]
    drawn: dict[str, int] = {}
    for _, why in picked:
        drawn[why] = drawn.get(why, 0) + 1
    return Draw(
        rows=rows,
        population={s: len(p) for s, p in by_stratum.items()},
        drawn={s: drawn.get(s, 0) for s in quotas},
        documents=len(eligible_docs),
    )


def _write(path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, str]]) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def paths_for(key: Path) -> tuple[Path, Path, Path]:
    stem = key.with_suffix("")
    return key, stem.with_suffix(".blind.csv"), stem.with_suffix(".strata.csv")


def write(result: Draw, key: Path) -> tuple[Path, Path, Path]:
    """The key, the blind labelling sheet and the strata table (see the module doc)."""
    key, blind, strata = paths_for(key)
    _write(key, GOLD_COLUMNS, result.rows)
    _write(blind, [c for c in GOLD_COLUMNS if c not in _HIDDEN], result.rows)
    _write(
        strata,
        ("stratum", "population", "drawn"),
        (
            {"stratum": s, "population": str(result.population[s]), "drawn": str(result.drawn[s])}
            for s in result.population
        ),
    )
    return key, blind, strata


def merge(key: Path, labelled: Path, out: Path) -> int:
    """The labelled blind sheet's judgement columns onto the key, by ``row``.

    Refuses a sheet whose rows do not match the key's (a lost or added row, or a
    value edited by hand), so labels never land on the wrong value.
    """
    with key.open(newline="") as fh:
        key_rows = list(csv.DictReader(fh))
    with labelled.open(newline="") as fh:
        labels = {r["row"]: r for r in csv.DictReader(fh)}
    if set(labels) != {r["row"] for r in key_rows}:
        raise ValueError(f"{labelled.name}'s rows do not match {key.name}'s")
    for r in key_rows:
        lab = labels[r["row"]]
        for col in ("submission_number", "field", "value"):
            if lab.get(col, r[col]) != r[col]:
                raise ValueError(f"row {r['row']}: {col} differs between the key and the sheet")
        for col in LABEL_COLUMNS:
            r[col] = lab.get(col, "")
    _write(out, GOLD_COLUMNS, key_rows)
    return sum(1 for r in key_rows if r["judgement"].strip())
