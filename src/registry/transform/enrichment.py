"""Enrich silver devices from openFDA (ADR 0013).

Two tiers that succeed or fail independently:

**Tier 1 — `classification`, keyed by product code.** 181 distinct product codes
cover all 1,614 devices in the current pull, so this is where ``device_class``
comes from: roughly a tenth of the calls a per-submission pass would need, for
the one field ADR 0012 declared part of the silver row.

**Tier 2 — `510k` / `pma`, keyed by submission number.** Per-device facts a
product code cannot carry: when the submission was received (so, how long review
took), whether a public 510(k) summary exists, and the FDA's own spelling of the
applicant.

Results land in ``silver_device_enrichment``, never inside ``silver_devices``.
Rebuilding silver is a cheap, offline, deterministic operation and stays that way
only if the remote fetch lives in its own table.

What this deliberately does **not** produce: predicate lineage, PCCP and the
cybersecurity statement. No openFDA endpoint carries them -- they are in the
510(k) summary PDF. ``statement_or_summary`` is recorded here precisely so the
size of that future pass is a query rather than a guess.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from registry.config.settings import Settings, get_settings
from registry.schemas import DeviceEnrichmentRecord

logger = logging.getLogger(__name__)

ENRICHMENT_TABLE = "silver_device_enrichment"
SILVER_TABLE = "silver_devices"

# openFDA returns the device class as a bare numeral. Anything outside this map --
# including the empty string it returns when it has no class -- is *unknown*, not
# "unclassified": ADR 0012 keeps those two facts distinct and this is where they
# would be collapsed.
_DEVICE_CLASS = {"1": "I", "2": "II", "3": "III"}

# The FDA's Y/N flag columns. An empty string means the flag was not set, which is
# not the same claim as "N", so it maps to None like every other unknown here.
_TRUE = {"y", "yes", "true"}
_FALSE = {"n", "no", "false"}

# `build_device_record` writes this when the FDA row carries no product code. It is
# our sentinel, not an FDA code, so looking it up is a guaranteed miss -- and would
# be one call per affected device if the source ever starts omitting the column.
_UNKNOWN_PRODUCT_CODE = "UNKNOWN"


class SupportsOpenFda(Protocol):
    """The slice of ``OpenFdaClient`` this module uses (so tests can fake it)."""

    def fetch_submission(self, submission_number: str) -> Any: ...
    def fetch_classification(self, product_code: str) -> Any: ...


def map_device_class(raw: str | None, *, unclassified_reason: str | None) -> str | None:
    """Map openFDA's device class onto ``DeviceClass``.

    ``None`` means *we do not know*; ``"unclassified"`` means the FDA itself says
    the device is unclassified. Collapsing those is the specific mistake ADR 0012
    exists to prevent, so an unrecognised value returns ``None`` rather than
    defaulting to either end.
    """
    value = (raw or "").strip()
    mapped = _DEVICE_CLASS.get(value)
    if mapped is not None:
        return mapped
    if (unclassified_reason or "").strip():
        return "unclassified"
    return None


def parse_flag(raw: str | None) -> bool | None:
    """Read an FDA ``Y``/``N`` flag. Absent or unrecognised is ``None``, not False."""
    value = (raw or "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return None


def _parse_date(raw: str | None) -> dt.date | None:
    """openFDA dates are ISO (``YYYY-MM-DD``); a malformed one is data, not a crash."""
    value = (raw or "").strip()
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        logger.debug("Unparseable openFDA date %r", raw)
        return None


def _raw(device: Any) -> Mapping[str, Any]:
    return getattr(device, "raw", None) or {}


def build_records(
    silver_rows: Iterable[Mapping[str, Any]],
    client: SupportsOpenFda,
    *,
    now: dt.datetime | None = None,
) -> list[DeviceEnrichmentRecord]:
    """Fetch both tiers for ``silver_rows`` and return one record per submission.

    ``silver_rows`` needs ``submission_number``, ``product_code`` and
    ``decision_date``. A record is returned even when both tiers miss: a miss is a
    fact worth storing, or every re-run refetches it.

    Eager, not a generator. The "one call per product code" guarantee is the whole
    point of the two-tier split (ADR 0013), and a lazy generator makes it hold only
    if the caller happens to consume the whole thing -- a silent tenfold cost
    increase for anyone who breaks early or forgets.
    """
    rows = list(silver_rows)
    enriched_at = now or dt.datetime.now(dt.UTC).replace(tzinfo=None)

    # Tier 1 first, once per distinct product code -- the entire point of the
    # two-tier split. Misses are cached as None so a bad code is not retried per
    # device that happens to use it.
    codes = {
        code
        for row in rows
        if (code := (row.get("product_code") or "").strip().upper())
        and code != _UNKNOWN_PRODUCT_CODE
    }
    classifications: dict[str, Any] = {}
    for code in sorted(codes):
        classifications[code] = client.fetch_classification(code)
    found = sum(1 for v in classifications.values() if v is not None)
    logger.info("Tier 1: %d/%d product codes resolved", found, len(codes))

    records: list[DeviceEnrichmentRecord] = []
    for index, row in enumerate(rows, start=1):
        submission = (row.get("submission_number") or "").strip().upper()
        if not submission:
            continue

        device = client.fetch_submission(submission)
        code = (row.get("product_code") or "").strip().upper()
        classification = classifications.get(code)

        if index % 200 == 0:
            logger.info("Tier 2: %d/%d submissions fetched", index, len(rows))

        records.append(
            _record(submission, enriched_at, device, classification, row.get("decision_date"))
        )
    return records


def _record(
    submission: str,
    enriched_at: dt.datetime,
    device: Any,
    classification: Any,
    decision_date: dt.date | None,
) -> DeviceEnrichmentRecord:
    raw = _raw(device)
    date_received = _parse_date(raw.get("date_received"))

    # Review duration is only meaningful when both ends are real dates, and a
    # negative span means the source disagrees with itself -- report nothing
    # rather than a number nobody can interpret.
    review_time_days: int | None = None
    if date_received is not None and isinstance(decision_date, dt.date):
        delta = (decision_date - date_received).days
        review_time_days = delta if delta >= 0 else None

    class_raw = _raw(classification).get("device_class") if classification is not None else None
    return DeviceEnrichmentRecord(
        submission_number=submission,
        enriched_at=enriched_at,
        submission_found=device is not None,
        endpoint=getattr(device, "endpoint", None),
        decision_code=getattr(device, "decision_code", None),
        decision_description=raw.get("decision_description") or None,
        clearance_type=raw.get("clearance_type") or None,
        date_received=date_received,
        review_time_days=review_time_days,
        statement_or_summary=raw.get("statement_or_summary") or None,
        third_party_review=parse_flag(raw.get("third_party_flag")),
        expedited_review=parse_flag(raw.get("expedited_review_flag")),
        advisory_committee_description=getattr(device, "advisory_committee_description", None),
        applicant_openfda=getattr(device, "applicant", None),
        classification_found=classification is not None,
        product_code=getattr(classification, "product_code", None),
        device_class_raw=getattr(classification, "device_class", None) or class_raw,
        unclassified_reason=_raw(classification).get("unclassified_reason") or None,
        regulation_number=getattr(classification, "regulation_number", None),
        classification_specialty=getattr(classification, "medical_specialty_description", None),
        classification_definition=getattr(classification, "definition", None),
        life_sustain_support=parse_flag(_raw(classification).get("life_sustain_support_flag")),
        implant=parse_flag(_raw(classification).get("implant_flag")),
    )


def device_classes_by_submission(spark, settings: Settings | None = None) -> dict[str, str]:
    """Read ``silver_device_enrichment`` and return the resolvable device classes.

    Only submissions whose class actually maps are included: a submission absent
    from the result is one we do not know the class of, which `build_device_record`
    then leaves as ``None``. A missing table returns ``{}`` -- silver built before
    enrichment existed and must still build without it (ADR 0013).
    """
    from registry import tables

    settings = settings or get_settings()
    if not tables.table_exists(spark, settings, ENRICHMENT_TABLE):
        logger.info(
            "No %s table; silver will build with device_class null. Run: registry enrich-openfda",
            ENRICHMENT_TABLE,
        )
        return {}

    df = tables.read_table(spark, settings, ENRICHMENT_TABLE).select(
        "submission_number", "device_class_raw", "unclassified_reason"
    )
    mapped: dict[str, str] = {}
    for row in df.collect():
        device_class = map_device_class(
            row["device_class_raw"], unclassified_reason=row["unclassified_reason"]
        )
        if device_class is not None:
            mapped[row["submission_number"]] = device_class
    return mapped


def run(spark, settings: Settings | None = None, *, limit: int | None = None) -> int:
    """Enrich every silver device and overwrite ``silver_device_enrichment``.

    Overwrite, not append: this table is a materialised view of a remote service,
    re-derivable at any time. Bronze remains the only append-only layer.
    """
    from registry import tables
    from registry.ingest.openfda_client import OpenFdaClient
    from registry.schemas import spark_schema_for

    settings = settings or get_settings()
    silver = tables.read_table(spark, settings, SILVER_TABLE).select(
        "submission_number", "product_code", "decision_date"
    )
    if limit is not None:
        silver = silver.limit(limit)
    rows = [row.asDict() for row in silver.collect()]
    logger.info("Enriching %d silver device(s) from openFDA", len(rows))

    with OpenFdaClient(settings) as client:
        records = [record.model_dump() for record in build_records(rows, client)]

    df = spark.createDataFrame(records, spark_schema_for(DeviceEnrichmentRecord))
    tables.write_table(df, settings, ENRICHMENT_TABLE, mode="overwrite")
    return len(records)
