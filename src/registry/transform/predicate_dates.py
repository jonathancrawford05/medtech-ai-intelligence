"""Predicate decision dates from openFDA, for ``predicate_age_days`` (ADR 0019).

The Summary names a predicate by number but not its decision date, and most
predicates are not AI devices, so they are not in silver. Their dates come from
the existing openFDA client (ADR 0010) -- cached, throttled, and missing = None --
into ``silver_predicate_dates``. An enrichment step, not a PDF step: like
``registry enrich-openfda`` it needs ``api.fda.gov`` and runs on a network-
permitted host; ``build-silver`` only ever reads the table it produced.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from typing import Any, Protocol

from registry.config.settings import Settings, get_settings
from registry.schemas import PredicateDateRecord

logger = logging.getLogger(__name__)

DATES_TABLE = "silver_predicate_dates"
EXTRACTION_TABLE = "silver_document_extraction"


class SupportsFetchSubmission(Protocol):
    def fetch_submission(self, submission_number: str) -> Any: ...


def _parse(raw: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat((raw or "").strip())
    except ValueError:
        return None


def build_records(
    numbers: Iterable[str], client: SupportsFetchSubmission, *, now: dt.datetime | None = None
) -> list[PredicateDateRecord]:
    """One record per distinct predicate number; a miss is a record too."""
    fetched_at = now or dt.datetime.now(dt.UTC).replace(tzinfo=None)
    records = []
    for number in sorted({n.strip().upper() for n in numbers if n and n.strip()}):
        device = client.fetch_submission(number)
        records.append(
            PredicateDateRecord(
                submission_number=number,
                fetched_at=fetched_at,
                found=device is not None,
                endpoint=getattr(device, "endpoint", None),
                decision_date=_parse(getattr(device, "decision_date", None)),
            )
        )
    found = sum(r.decision_date is not None for r in records)
    logger.info("Predicate decision dates: %d / %d resolved", found, len(records))
    return records


def predicate_age_days(
    decision_date: dt.date | None, predicate_decision_date: dt.date | None
) -> int | None:
    """Days from the predicate's clearance to the device's; None if unknown or negative.

    A negative span would mean the predicate was cleared after the device that
    cites it -- a misread number or bad source data, not a fact to report.
    """
    if decision_date is None or predicate_decision_date is None:
        return None
    days = (decision_date - predicate_decision_date).days
    return days if days >= 0 else None


def run(spark, settings: Settings | None = None) -> int:
    """Fetch the decision date of every primary predicate. Entry point for the CLI."""
    from registry import tables
    from registry.ingest.openfda_client import OpenFdaClient
    from registry.schemas import spark_schema_for

    settings = settings or get_settings()
    if not tables.table_exists(spark, settings, EXTRACTION_TABLE):
        raise ValueError(f"No {EXTRACTION_TABLE} table. Run `registry extract-documents` first.")
    numbers = [
        r["primary_predicate"]
        for r in tables.read_table(spark, settings, EXTRACTION_TABLE)
        .where("primary_predicate IS NOT NULL")
        .select("primary_predicate")
        .distinct()
        .collect()
    ]
    logger.info("Fetching decision dates for %d distinct primary predicates", len(numbers))
    with OpenFdaClient(settings) as client:
        records = build_records(numbers, client)
    df = spark.createDataFrame(
        [r.model_dump() for r in records], schema=spark_schema_for(PredicateDateRecord)
    )
    tables.write_table(df, settings, DATES_TABLE, mode="overwrite")
    return len(records)


def dates_by_submission(spark, settings: Settings | None = None) -> dict[str, dt.date]:
    """Predicate number -> decision date, for the silver join; ``{}`` if absent."""
    from registry import tables

    settings = settings or get_settings()
    if not tables.table_exists(spark, settings, DATES_TABLE):
        return {}
    rows = (
        tables.read_table(spark, settings, DATES_TABLE)
        .where("decision_date IS NOT NULL")
        .select("submission_number", "decision_date")
        .collect()
    )
    return {r["submission_number"]: r["decision_date"] for r in rows}
