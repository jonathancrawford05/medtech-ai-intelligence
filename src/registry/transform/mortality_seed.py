"""Load the hand-curated mortality judgements into ``EvidenceRecord``s.

`mortality_or_mace_risk_indicated` is the one field in this registry that is an
*interpretation* rather than something the FDA published, so ADR 0007 stores it
as two columns plus provenance. This module is the door that judgement comes
through, and its job is mostly refusal: a judgement whose provenance is missing,
whose review method is unrecognised, or whose supporting text is absent is
**invalid data, not a warning**. It raises rather than skipping, because a
silently dropped judgement looks identical to one nobody made.

The file (`config/mortality_seed.yaml`) is written by hand -- see
`docs/handoffs/cowork-spike-and-curation.md` for the curation brief.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import yaml

from registry.config.settings import Settings, get_settings
from registry.schemas import EvidenceRecord

logger = logging.getLogger(__name__)

EVIDENCE_TABLE = "silver_evidence"

_REVIEW_METHODS = {"human", "llm_assisted"}

# Stage 1 of ADR 0007: a deterministic, reproducible pass over the intended-use
# text. It is never the answer -- only `mortality_confirmed_flag` may gate the
# mart -- but running it beside the curated judgement makes disagreements
# visible, and a `confirmed=True` with no keyword hit is exactly the row worth
# looking at twice.
_MORTALITY_KEYWORDS = re.compile(
    r"\b("
    r"mortalit\w*|death\w*|fatal\w*|surviv\w*|"
    r"mace|major adverse cardiac event\w*|major adverse cardiovascular event\w*|"
    r"cardiac arrest|sudden cardiac|"
    r"risk (?:of|for) (?:death|dying)|"
    r"life[- ]threatening|prognos\w*|"
    # Widened from curation evidence (findings/0012 / 0014): 8 of the 11 confirmed
    # devices used mortality-relevant language stage 1 did not recognise. Stage 1
    # stays a cheap, recall-oriented lead flag -- never the mart filter (ADR 0014),
    # so widening it only shrinks the keyword_disagrees gap.
    r"hemodynamic instabilit\w*|hypotens\w*|hypoperfus\w*|"
    r"loss of pulse|h(?:ae|e)morrhag\w*|deteriorat\w*|plaque\w*"
    r")\b",
    re.IGNORECASE,
)


def keyword_flag(intended_use_text: str) -> bool:
    """Stage 1. Deterministic, cheap, and safe to re-run at any time."""
    return bool(_MORTALITY_KEYWORDS.search(intended_use_text or ""))


def _require(entry: dict[str, Any], field: str, submission: str) -> str:
    value = entry.get(field)
    text = str(value).strip() if value is not None else ""
    if not text:
        raise ValueError(
            f"mortality seed entry {submission!r} has no {field}. "
            "A judgement nobody can audit is invalid data (ADR 0007)."
        )
    return text


def _build(entry: dict[str, Any]) -> EvidenceRecord:
    submission = str(entry.get("submission_number") or "").strip().upper()
    if not submission:
        raise ValueError("mortality seed entry has no submission_number")

    confirmed = entry.get("mortality_confirmed")
    if confirmed is not None and not isinstance(confirmed, bool):
        raise ValueError(
            f"mortality seed entry {submission!r}: mortality_confirmed must be a "
            f"boolean, got {confirmed!r}"
        )

    method = str(entry.get("review_method") or "").strip().lower() or None
    if confirmed is not None and method not in _REVIEW_METHODS:
        raise ValueError(
            f"mortality seed entry {submission!r}: review_method must be one of "
            f"{sorted(_REVIEW_METHODS)}, got {entry.get('review_method')!r}. "
            "ADR 0007: a confirmation with no provenance is invalid."
        )

    intended_use = _require(entry, "intended_use_text", submission)
    source = _require(entry, "intended_use_source", submission)

    return EvidenceRecord(
        submission_number=submission,
        intended_use_text=intended_use,
        intended_use_source=source,
        mortality_keyword_flag=keyword_flag(intended_use),
        mortality_confirmed_flag=confirmed,
        mortality_review_method=method,
        mortality_review_notes=(str(entry["notes"]).strip() if entry.get("notes") else None),
        # reports_sensitivity_specificity / discloses_demographics stay None: they
        # come from the 510(k) summary PDF, which nothing fetches yet (Issue 4).
    )


def load(settings: Settings | None = None) -> list[EvidenceRecord]:
    """Read and validate the seed. A missing file is empty, not an error."""
    settings = settings or get_settings()
    path = settings.mortality_seed_path
    if not path.exists():
        logger.info(
            "No mortality seed at %s; the mart will be empty. See "
            "docs/handoffs/cowork-spike-and-curation.md",
            path,
        )
        return []

    raw = yaml.safe_load(path.read_text()) or {}
    records: list[EvidenceRecord] = []
    seen: set[str] = set()
    for entry in raw.get("reviewed") or []:
        record = _build(entry)
        if record.submission_number in seen:
            raise ValueError(
                f"{path}: {record.submission_number!r} is reviewed twice; one "
                "judgement would silently win. Keep a single entry per device."
            )
        seen.add(record.submission_number)
        records.append(record)

    logger.info("Loaded %d curated mortality judgement(s) from %s", len(records), path)
    return records


def run(spark, settings: Settings | None = None) -> int:
    """Write the seed to ``silver_evidence``. Derived state, so it overwrites."""
    from registry import tables
    from registry.schemas import spark_schema_for

    settings = settings or get_settings()
    records = load(settings)
    df = spark.createDataFrame(
        [r.model_dump() for r in records], schema=spark_schema_for(EvidenceRecord)
    )
    tables.write_table(df, settings, EVIDENCE_TABLE, mode="overwrite", merge_schema=True)
    return len(records)
