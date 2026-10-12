"""The next curation batch, with its evidence attached (Issue 4, PR 4C; ADR 0021).

The mortality seed (`config/mortality_seed.yaml`) was curated by hand over the
cardiovascular devices, from intended-use text a Cowork session copied out of each
PDF; 21 were omitted for want of clean text (finding 0012). PR 4C reads every
Summary's intended use verbatim, so the stage-1 keyword pass can now run over the
device's own statement for *every* device. This report lists the ones in scope
whose statement trips it but which the seed has not reviewed: the next batch, each
with the matched terms, the statement, its page and its PDF.

In scope (`scope` on each candidate): a cardiometabolic specialty (the taxonomy's
`mortality_relevant_categories`), **or** any other specialty whose statement speaks
of the heart (`_CARDIOVASCULAR_LANGUAGE`). Finding 0022 found 18 coronary-plaque
tools cleared through Radiology that a specialty-only scope never offered -- the
"cardiovascular-risk tool cleared through Radiology" ADR 0016 warned about.

It also measures stage 1 against the curated judgements (`keyword_agreement`):
with the device's full statement in place of the curator's truncated excerpt, how
many confirmed devices does stage 1 still miss (`keyword_disagrees`, finding 0014:
0 of 11 on the curated text), and how many curated negatives does it now flag?

Evidence for a curator, never a judgement: nothing here writes the seed or a
mortality flag (ADR 0007/0014). A candidate is "read this", not "this is one".
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from registry.config.settings import Settings, get_settings
from registry.schemas import EvidenceRecord
from registry.transform import intended_use, mortality_seed, taxonomy

# The heart, named: what brings a device from another specialty into scope. Anatomy,
# not risk -- the stage-1 hit is still required on top of it.
_CARDIOVASCULAR_LANGUAGE = re.compile(
    r"\b(?:coronar\w*|cardiac|cardio\w*|heart|myocard\w*|atheroscler\w*)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class Candidate:
    submission_number: str
    device_name: str
    applicant: str | None
    decision_date: dt.date | None
    specialty_category: str
    product_code: str | None
    keyword_hits: list[str]
    intended_use_basis: str
    intended_use_pages: list[int]
    intended_use_text: str
    source_url: str | None
    scope: str  # "specialty" (cardiometabolic) | "statement" (cardiovascular language)


def candidates(
    devices: Iterable[Mapping[str, Any]],
    statements: Mapping[str, Mapping[str, Any]],
    *,
    reviewed: set[str],
    categories: frozenset[str],
) -> list[Candidate]:
    """In-scope devices whose own statement trips stage 1, not yet reviewed.

    In scope: a cardiometabolic specialty, or a statement that speaks of the heart
    (module docstring). Newest decision first, so the batch starts with what is most
    current.
    """
    out: list[Candidate] = []
    for device in devices:
        key = device["submission_number"]
        if key in reviewed:
            continue
        row = statements.get(key)
        text = row["intended_use_text"] if row else None
        hits = mortality_seed.keyword_hits(text or "")
        if not hits:
            continue
        if device["specialty_category"] in categories:
            scope = "specialty"
        elif _CARDIOVASCULAR_LANGUAGE.search(text):
            scope = "statement"
        else:
            continue
        out.append(
            Candidate(
                submission_number=key,
                device_name=device["device_name"],
                applicant=device.get("applicant_resolved"),
                decision_date=device.get("decision_date"),
                specialty_category=device["specialty_category"],
                product_code=device.get("product_code"),
                keyword_hits=hits,
                intended_use_basis=row["intended_use_basis"],
                intended_use_pages=list(row["intended_use_pages"] or []),
                intended_use_text=text,
                source_url=row.get("source_url"),
                scope=scope,
            )
        )
    out.sort(key=lambda c: (c.decision_date or dt.date.min, c.submission_number), reverse=True)
    return out


@dataclass
class KeywordAgreement:
    """Stage 1 on the curated excerpt vs the device's own statement, per judgement."""

    curated: int = 0
    with_statement: int = 0
    confirmed: int = 0
    negatives: int = 0
    # keyword_disagrees: confirmed, and stage 1 misses the text.
    disagrees_on_curated: list[str] = field(default_factory=list)
    disagrees_on_statement: list[str] = field(default_factory=list)
    confirmed_without_statement: list[str] = field(default_factory=list)
    # Curated negatives stage 1 still flags (finding 0014: 1, K192732).
    negatives_flagged_on_curated: list[str] = field(default_factory=list)
    negatives_flagged_on_statement: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        def ids(keys: list[str]) -> str:
            return f"  {', '.join(keys)}" if keys else ""

        on_statement = self.confirmed - len(self.confirmed_without_statement)
        return [
            f"curated devices                : {self.curated:,} "
            f"({self.confirmed:,} confirmed, {self.negatives:,} negatives)",
            f"  with a Summary statement     : {self.with_statement:,}",
            "keyword_disagrees (confirmed, stage 1 misses):",
            f"  on the curated excerpt       : {len(self.disagrees_on_curated)} / {self.confirmed}"
            f"   [finding 0014: 0 / 11]{ids(self.disagrees_on_curated)}",
            f"  on the device's statement    : {len(self.disagrees_on_statement)} / {on_statement}"
            f"{ids(self.disagrees_on_statement)}",
            f"  confirmed, no statement read : {len(self.confirmed_without_statement)}"
            f"{ids(self.confirmed_without_statement)}",
            "negatives stage 1 flags:",
            f"  on the curated excerpt       : {len(self.negatives_flagged_on_curated)}"
            f" / {self.negatives}   [finding 0014: 1]{ids(self.negatives_flagged_on_curated)}",
            f"  on the device's statement    : {len(self.negatives_flagged_on_statement)}"
            f"{ids(self.negatives_flagged_on_statement)}",
        ]


def keyword_agreement(
    evidence: Iterable[EvidenceRecord], statements: Mapping[str, str]
) -> KeywordAgreement:
    """Measure stage 1 over both texts for every curated judgement."""
    out = KeywordAgreement()
    for record in evidence:
        if record.mortality_confirmed_flag is None:
            continue  # recorded but not judged: nothing to agree with
        key = record.submission_number
        statement = statements.get(key)
        out.curated += 1
        out.with_statement += statement is not None
        on_curated = mortality_seed.keyword_flag(record.intended_use_text)
        on_statement = mortality_seed.keyword_flag(statement) if statement else None
        if record.mortality_confirmed_flag:
            out.confirmed += 1
            if not on_curated:
                out.disagrees_on_curated.append(key)
            if on_statement is None:
                out.confirmed_without_statement.append(key)
            elif not on_statement:
                out.disagrees_on_statement.append(key)
        else:
            out.negatives += 1
            if on_curated:
                out.negatives_flagged_on_curated.append(key)
            if on_statement:
                out.negatives_flagged_on_statement.append(key)
    return out


@dataclass
class CurationReport:
    candidates: list[Candidate]
    agreement: KeywordAgreement
    devices_in_scope: int
    in_scope_with_statement: int


def build(spark, settings: Settings | None = None) -> CurationReport:
    """Read silver, the intended-use pass and the seed. Entry point for the CLI."""
    from registry import tables

    settings = settings or get_settings()
    for table, command in (
        ("silver_devices", "build-silver"),
        (intended_use.INTENDED_USE_TABLE, "extract-intended-use"),
    ):
        if not tables.table_exists(spark, settings, table):
            raise ValueError(f"No {table} table. Run: uv run registry {command}")
    categories = taxonomy.load(settings).mortality_relevant_categories
    devices = [
        r.asDict()
        for r in tables.read_table(spark, settings, "silver_devices")
        .select(
            "submission_number",
            "device_name",
            "applicant_resolved",
            "decision_date",
            "specialty_category",
            "product_code",
        )
        .collect()
    ]
    statements = intended_use.rows_by_submission(spark, settings)
    texts = {k: r["intended_use_text"] for k, r in statements.items() if r["intended_use_text"]}
    seed = mortality_seed.load(settings)
    in_scope = [d for d in devices if d["specialty_category"] in categories]
    return CurationReport(
        candidates=candidates(
            devices,
            statements,
            reviewed={r.submission_number for r in seed},
            categories=categories,
        ),
        agreement=keyword_agreement(seed, texts),
        devices_in_scope=len(in_scope),
        in_scope_with_statement=sum(d["submission_number"] in texts for d in in_scope),
    )


CSV_COLUMNS = list(Candidate.__dataclass_fields__)


def write_csv(rows: list[Candidate], path: Path) -> None:
    """The batch as a sheet a curator can work through; lists joined with `;`."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            record = asdict(row)
            record["keyword_hits"] = "; ".join(row.keyword_hits)
            record["intended_use_pages"] = "; ".join(map(str, row.intended_use_pages))
            writer.writerow(record)
