"""The gold mortality-relevant mart — the registry's actual deliverable.

Everything upstream of this is plumbing. This is the ranked list an underwriter
reads: FDA-authorised AI devices whose intended use concerns predicting,
stratifying or detecting risk of death or a major adverse cardiac event.

**The mart filters on `mortality_confirmed_flag`, never on the keyword flag or
the specialty taxonomy.** ADR 0007 is explicit that this excludes devices nobody
has reviewed yet, and that this is the correct default: under-inclusion is
recoverable by reviewing more devices, silent over-inclusion is not recoverable
at all, because nobody downstream can tell a regex hit from a considered
judgement once it is in the table.

The specialty taxonomy is a *starting signal* for what to review, not a filter
here — a confirmed device outside cardiovascular/metabolic still qualifies, since
the panel names the reviewing committee rather than the clinical problem.

Every row carries the provenance of its judgement, so the mart can always answer
"why is this device here?" without a join back.
"""

from __future__ import annotations

import logging
from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

from registry.config.settings import Settings, get_settings
from registry.schemas import EvidenceRecord, spark_schema_for
from registry.transform.mortality_seed import EVIDENCE_TABLE

logger = logging.getLogger(__name__)

MART_TABLE = "gold_mortality_relevant"
SILVER_TABLE = "silver_devices"

# What an underwriter needs on the row, and nothing else: what the device is, who
# owns it, when it was authorised, how risky the FDA thinks it is -- and why we
# say it is mortality-relevant.
_COLUMNS = [
    "submission_number",
    "device_name",
    "applicant_resolved",
    "applicant_raw",
    "decision_date",
    "pathway",
    "device_class",
    "specialty_category",
    "specialty_panel",
    "product_code",
    "intended_use_text",
    "intended_use_source",
    "mortality_review_method",
    "mortality_review_notes",
    "mortality_keyword_flag",
    "keyword_disagrees",
    "source_url",
]


def qualifying_frame(spark, settings: Settings | None = None) -> DataFrame:
    """The mart as a DataFrame, newest authorisation first.

    Returned as a frame rather than collected rows so the schema survives an
    empty result: inferring types from a list of dicts fails the moment an
    optional column is null in every row, which is exactly the state the mart is
    in before anyone has curated anything.
    """
    from registry import tables

    settings = settings or get_settings()
    devices = tables.read_table(spark, settings, SILVER_TABLE)

    if not tables.table_exists(spark, settings, EVIDENCE_TABLE):
        logger.info(
            "No %s table; nothing has been curated, so the mart is empty. "
            "See docs/handoffs/cowork-spike-and-curation.md",
            EVIDENCE_TABLE,
        )
        evidence = spark.createDataFrame([], spark_schema_for(EvidenceRecord))
    else:
        evidence = tables.read_table(spark, settings, EVIDENCE_TABLE)

    return (
        devices.join(evidence, "submission_number", "inner")
        # `isNotNull() & isTrue()` rather than `== True`: None must not qualify,
        # and a null-safe comparison would let it through as unknown.
        .filter(F.col("mortality_confirmed_flag").isNotNull())
        .filter(F.col("mortality_confirmed_flag"))
        .withColumn(
            # Stage 1 said no, a curator said yes. Not an error -- either the
            # keyword list is too narrow or the judgement was generous -- but it
            # is the row worth a second look, so the mart surfaces it rather than
            # leaving it to be noticed.
            "keyword_disagrees",
            ~F.col("mortality_keyword_flag"),
        )
        .select(*_COLUMNS)
        .orderBy(F.col("decision_date").desc(), F.col("submission_number"))
    )


def build(spark, settings: Settings | None = None) -> list[dict[str, Any]]:
    """Collect the qualifying rows. Convenience over `qualifying_frame`."""
    return [row.asDict() for row in qualifying_frame(spark, settings).collect()]


def run(spark, settings: Settings | None = None) -> int:
    """Rebuild ``gold_mortality_relevant``. Derived state, so it overwrites.

    An empty mart is written rather than skipped: before anyone has curated, the
    honest answer is an empty table a consumer can query, not a missing one they
    have to special-case.
    """
    from registry import tables

    settings = settings or get_settings()
    df = qualifying_frame(spark, settings)
    written = df.count()

    tables.write_table(df, settings, MART_TABLE, mode="overwrite", merge_schema=True)
    logger.info("Wrote %d row(s) to %s", written, MART_TABLE)
    return written
