"""bronze -> silver: turn raw pulls into the queryable `DeviceRecord` table.

Bronze is append-only and holds every pull, so the first job is choosing which
pull to believe: **the latest `ingested_at` per submission number**. Everything
else is interpretation that bronze deliberately deferred — parsing the decision
date, deriving the pathway from the submission prefix, mapping the FDA panel to
our specialty taxonomy, and resolving the applicant to a parent company.

openFDA-dependent fields (device class, predicate lineage, PCCP, cybersecurity)
are left `None` here. They are enriched in a later pass, and `None` means "not
yet enriched" rather than "no" (ADR 0012), so silver can be built from the FDA
list alone and enrichment coverage is measurable rather than a precondition.

Silver is **derived state**, so a rebuild overwrites it. That is the one place
the append-only rule does not apply: bronze is the history, silver is a view of
its newest rows. Every overwrite is a retained Delta version, and each version
says what it was built from (ADR 0015): rows carry the `source_snapshot_id` they
were read from, and the commit carries a `BuildStamp` -- the latest bronze
snapshot plus a hash of the rows written. A build whose stamp matches the current
version's writes nothing, so an unchanged world never produces a new version for
the change monitor to diff.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from registry.config.settings import Settings, get_settings
from registry.ingest.openfda_client import split_pma_number
from registry.schemas import DeviceRecord, spark_schema_for
from registry.transform import company_resolution, enrichment, taxonomy

logger = logging.getLogger(__name__)

SILVER_TABLE = "silver_devices"
BRONZE_TABLE = "bronze_fda_ai_list"

# Date layouts the FDA export has actually used. Month-first is the US civil
# format the CSV publishes; ISO is accepted because it is unambiguous and cheap.
_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%d-%b-%Y")

# Submission-number prefixes -> pathway. Longest first so DEN is matched before
# any shorter prefix could claim it.
_PATHWAY_PREFIXES = (("DEN", "de_novo"), ("K", "510k"), ("P", "pma"))

# Time travel reaches back only as far as Delta keeps the log and the files
# (ADR 0015 Decision 5). The defaults are 30 days / 7 days, and 7 days is exactly
# one weekly build: a routine VACUUM could take the version the change monitor
# needs to diff against. 90 days outlives a weekly cadence many times over.
SILVER_RETENTION = {
    "delta.logRetentionDuration": "interval 90 days",
    "delta.deletedFileRetentionDuration": "interval 90 days",
}


def derive_pathway(submission_number: str | None) -> str | None:
    """Derive the regulatory pathway from the submission-number prefix.

    Returns None rather than guessing: an unrecognised prefix means the row is
    not something we understand, and a wrong pathway is worse than no row.
    """
    key = (submission_number or "").strip().upper()
    if not key:
        return None
    for prefix, pathway in _PATHWAY_PREFIXES:
        # The second clause guards a bare prefix with no digits ("K", "P").
        if key.startswith(prefix) and key[len(prefix) :].strip():
            return pathway
    return None


def parse_decision_date(raw: str | None) -> dt.date | None:
    """Parse the decision date bronze kept verbatim.

    Returns None for anything unparseable so one malformed row cannot fail the
    build; the row is dropped by `build_device_record` and counted.
    """
    text = (raw or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def split_supplement(submission_number: str | None) -> tuple[str | None, str | None]:
    """Split a PMA submission into (base, supplement); (None, None) for non-PMA.

    Delegates to the openFDA client's splitter so silver and the client cannot
    disagree about what `P130020/S005` means (ADR 0012).
    """
    key = (submission_number or "").strip().upper()
    if not key.startswith("P"):
        return None, None
    return split_pma_number(key)


@dataclass
class SilverContext:
    """The curated lookups a row build needs, loaded once per run."""

    taxonomy: taxonomy.SpecialtyTaxonomy
    companies: company_resolution.CompanyLookup
    # Submission number -> DeviceClass, read from `silver_device_enrichment` by the
    # caller. A plain dict, not a client: build-silver must stay offline and
    # deterministic (ADR 0013), so the fetch happens in `registry enrich-openfda`
    # and this transform only ever reads what it produced. Empty is normal -- it is
    # what silver did before enrichment existed.
    device_classes: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, settings: Settings | None = None) -> SilverContext:
        settings = settings or get_settings()
        return cls(
            taxonomy=taxonomy.load(settings),
            companies=company_resolution.load(settings),
        )


def build_device_record(row: dict[str, Any], ctx: SilverContext) -> DeviceRecord | None:
    """Build one silver row, or None when the bronze row is not usable.

    A row is dropped only when something the table's contract requires is absent
    or underivable: the key, the device name, a parsable decision date, or a
    recognisable pathway. Everything optional stays optional.
    """
    submission = (row.get("submission_number") or "").strip().upper()
    if not submission:
        return None

    device_name = (row.get("device_name") or "").strip()
    if not device_name:
        logger.debug("Dropping %s: no device name in the source row", submission)
        return None

    decision_date = parse_decision_date(row.get("decision_date_raw"))
    if decision_date is None:
        logger.debug(
            "Dropping %s: unparseable decision date %r", submission, row.get("decision_date_raw")
        )
        return None

    pathway = derive_pathway(submission)
    if pathway is None:
        logger.debug("Dropping %s: no pathway derivable from the prefix", submission)
        return None

    snapshot = (row.get("source_snapshot_id") or "").strip()
    if not snapshot:
        # Bronze requires it, so this is a hand-written or corrupt row; a silver
        # row with no lineage could not be paired back to its pull.
        logger.debug("Dropping %s: no source_snapshot_id", submission)
        return None

    panel = (row.get("panel_raw") or "").strip()
    resolution = ctx.companies.resolve(row.get("applicant_raw"))
    base, supplement = split_supplement(submission)

    return DeviceRecord(
        submission_number=submission,
        device_name=device_name,
        # None, not the device name: an applicant the FDA did not list must stay
        # visibly absent (paired with applicant_resolved=None) rather than borrow
        # another field's value.
        applicant_raw=(row.get("applicant_raw") or "").strip() or None,
        applicant_resolved=resolution.resolved_name,
        decision_date=decision_date,
        pathway=pathway,
        specialty_panel=panel,
        specialty_category=ctx.taxonomy.category_for(panel),
        product_code=(row.get("product_code") or "").strip().upper() or "UNKNOWN",
        pma_base_number=base,
        pma_supplement_number=supplement,
        # Absent from the map means "not enriched", which is what None already
        # says -- never a default class (ADR 0012).
        device_class=ctx.device_classes.get(submission),
        source_url=(row.get("source_url") or "").strip() or "",
        source_snapshot_id=snapshot,
        # has_pccp, pccp_summary, cybersecurity_statement_present and predicate
        # lineage stay None: no openFDA endpoint carries them, they are in the
        # 510(k) summary PDF (ADR 0013 Decision 4).
    )


def content_hash(records: Iterable[DeviceRecord]) -> str:
    """An order-independent SHA-256 over the rows a build would write.

    This, not the bronze snapshot alone, is what tells the gate whether a rebuild
    changes anything: enrichment and the curated taxonomy/company configs change
    silver without a new bronze pull.
    """
    lines = sorted(
        json.dumps(r.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        for r in records
    )
    digest = hashlib.sha256()
    for line in lines:
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


@dataclass(frozen=True)
class BuildStamp:
    """What one silver version was built from, recorded on its Delta commit.

    ``source_snapshot_id`` is the newest bronze pull's content hash -- the key the
    change monitor pairs versions on (ADR 0015 Decision 1). ``content_hash`` is
    `content_hash` of the rows written -- what the rebuild gate compares.
    """

    source_snapshot_id: str
    content_hash: str

    def to_metadata(self) -> str:
        return json.dumps(
            {"source_snapshot_id": self.source_snapshot_id, "content_hash": self.content_hash},
            sort_keys=True,
        )

    @classmethod
    def from_metadata(cls, raw: str | None) -> BuildStamp | None:
        """Parse a commit's userMetadata; None for anything that is not a stamp.

        None makes the gate rebuild, which is the safe answer for a version
        written before stamps existed.
        """
        try:
            data = json.loads(raw or "")
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        snapshot, digest = data.get("source_snapshot_id"), data.get("content_hash")
        if not (isinstance(snapshot, str) and snapshot and isinstance(digest, str) and digest):
            return None
        return cls(source_snapshot_id=snapshot, content_hash=digest)


def _latest_snapshot(rows: list[dict[str, Any]]) -> str:
    """The newest pull's snapshot id among the latest-per-submission rows.

    The globally newest bronze row always ranks first in its own partition, so it
    is among these rows; ties break on snapshot id exactly as the ranking does.
    """
    if not rows:
        raise ValueError("bronze has no rows; there is no snapshot to build or compare against")
    newest = max(rows, key=lambda r: (r["ingested_at"], r["source_snapshot_id"]))
    return newest["source_snapshot_id"]


def latest_bronze_rows(spark, settings: Settings | None = None) -> list[dict[str, Any]]:
    """Read bronze and keep the newest pull for each submission number."""
    from pyspark.sql import Window
    from pyspark.sql import functions as F

    from registry import tables

    settings = settings or get_settings()
    bronze = tables.read_table(spark, settings, BRONZE_TABLE)

    # Bronze holds every pull; rank by ingested_at per key and keep the newest.
    # source_snapshot_id breaks ties deterministically when two pulls share a
    # timestamp, so a rebuild of the same bronze always yields the same silver.
    newest = Window.partitionBy("submission_number").orderBy(
        F.col("ingested_at").desc(), F.col("source_snapshot_id").desc()
    )
    latest = (
        bronze.withColumn("_rank", F.row_number().over(newest))
        .filter(F.col("_rank") == 1)
        .drop("_rank")
    )
    return [r.asDict() for r in latest.collect()]


def latest_bronze_snapshot(spark, settings: Settings | None = None) -> str:
    """The `source_snapshot_id` of the newest bronze pull.

    Raises ValueError when bronze is empty: there is no snapshot, and inventing
    one would let a caller pair or gate against nothing.
    """
    return _latest_snapshot(latest_bronze_rows(spark, settings))


def _current_write(spark, settings: Settings) -> dict[str, Any] | None:
    """The newest commit on silver that wrote rows, or None when there is none."""
    from registry import tables

    if not tables.table_exists(spark, settings, SILVER_TABLE):
        return None
    writes = tables.data_write_history(spark, settings, SILVER_TABLE)
    return writes[0] if writes else None


def current_stamp(spark, settings: Settings | None = None) -> BuildStamp | None:
    """The `BuildStamp` on silver's current version; None if absent or unstamped."""
    entry = _current_write(spark, settings or get_settings())
    return BuildStamp.from_metadata(entry["userMetadata"]) if entry else None


def current_stamp_version(spark, settings: Settings | None = None) -> int | None:
    """The Delta version number of silver's current data write."""
    entry = _current_write(spark, settings or get_settings())
    return entry["version"] if entry else None


def _ensure_retention(spark, settings: Settings) -> None:
    """Apply `SILVER_RETENTION` if silver does not already carry it (writes no commit when it does)."""
    from registry import tables

    if tables.set_table_properties(spark, settings, SILVER_TABLE, SILVER_RETENTION):
        logger.info("Set time-travel retention on %s: %s", SILVER_TABLE, SILVER_RETENTION)


def run(spark=None, settings: Settings | None = None) -> int:
    """Rebuild `silver_devices` from the newest bronze pull.

    Returns rows written, or 0 when the gate found silver already current. 0 is
    unambiguous: a build with no usable rows raises rather than writing.
    """
    from registry import tables
    from registry.spark_session import get_spark

    settings = settings or get_settings()
    spark = spark or get_spark(settings)
    ctx = SilverContext.load(settings)
    ctx = replace(ctx, device_classes=enrichment.device_classes_by_submission(spark, settings))
    if ctx.device_classes:
        logger.info("Joined %d device classes from enrichment", len(ctx.device_classes))

    rows = latest_bronze_rows(spark, settings)
    records = [rec for row in rows if (rec := build_device_record(row, ctx)) is not None]

    dropped = len(rows) - len(records)
    if dropped:
        logger.warning("Dropped %d of %d bronze rows as unusable for silver", dropped, len(rows))
    if ctx.taxonomy.unmapped_panels:
        logger.warning(
            "Panels with no curated specialty mapping (defaulted): %s",
            ", ".join(sorted(ctx.taxonomy.unmapped_panels)),
        )
    if ctx.companies.unmatched_applicants:
        logger.info(
            "Applicants with no curated company entry (%d): %s",
            len(ctx.companies.unmatched_applicants),
            ", ".join(sorted(ctx.companies.unmatched_applicants)[:10]),
        )

    if not records:
        raise ValueError("bronze produced no usable silver rows; refusing to write an empty table")

    stamp = BuildStamp(
        source_snapshot_id=_latest_snapshot(rows), content_hash=content_hash(records)
    )
    if current_stamp(spark, settings) == stamp:
        logger.info(
            "%s is already built from snapshot %s with identical rows; nothing written",
            SILVER_TABLE,
            stamp.source_snapshot_id,
        )
        # Still check retention: a build that wrote its rows and died before the
        # property step would otherwise leave silver unguarded behind the gate.
        _ensure_retention(spark, settings)
        return 0

    df = spark.createDataFrame(
        [r.model_dump() for r in records], schema=spark_schema_for(DeviceRecord)
    )
    # Silver is derived state: rebuild replaces it rather than appending.
    tables.write_table(
        df,
        settings,
        SILVER_TABLE,
        mode="overwrite",
        merge_schema=True,
        user_metadata=stamp.to_metadata(),
    )
    _ensure_retention(spark, settings)
    logger.info(
        "Wrote %d rows to %s from snapshot %s",
        len(records),
        SILVER_TABLE,
        stamp.source_snapshot_id,
    )
    return len(records)
