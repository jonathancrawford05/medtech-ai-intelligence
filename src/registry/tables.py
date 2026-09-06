"""Config-driven Delta table access.

Transformation code addresses tables by *logical name* -- ``"silver_devices"``,
not ``"./lakehouse/silver_devices"``. This module turns that name into the right
Spark call for the configured storage mode:

===========  ==========================  ==============================
mode         read                        write
===========  ==========================  ==============================
PATH         ``.format("delta").load()``  ``.format("delta").save()``
CATALOG      ``spark.read.table()``       ``.saveAsTable()``
===========  ==========================  ==============================

Because the branch lives here and nowhere else, migrating to Databricks does not
touch a single transformation module.
"""

from __future__ import annotations

import logging

from pyspark.sql import DataFrame, SparkSession

from registry.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# Bronze is append-only by contract: we keep the history of what each source
# looked like at every pull, so a source changing shape is auditable after the fact.
BRONZE_PREFIX = "bronze_"


def read_table(
    spark: SparkSession,
    settings: Settings | None = None,
    name: str = "",
    *,
    version: int | None = None,
) -> DataFrame:
    """Read a logical table. ``version`` selects a Delta time-travel snapshot."""
    settings = settings or get_settings()
    ref = settings.table_ref(name)

    reader = spark.read
    if version is not None:
        reader = reader.option("versionAsOf", version)

    if settings.is_catalog_mode:
        # Unity Catalog time travel uses the same option via format("delta").
        return reader.table(ref) if version is None else reader.format("delta").table(ref)
    return reader.format("delta").load(ref)


def write_table(
    df: DataFrame,
    settings: Settings | None = None,
    name: str = "",
    *,
    mode: str = "append",
    merge_schema: bool = False,
    partition_by: list[str] | None = None,
) -> None:
    """Write a DataFrame to a logical table.

    Defaults to ``append`` because bronze tables must never lose history.
    ``merge_schema`` allows additive source changes (a new FDA column) to land
    without a manual migration.
    """
    settings = settings or get_settings()
    ref = settings.table_ref(name)

    if name.startswith(BRONZE_PREFIX) and mode == "overwrite":
        logger.warning(
            "Overwriting bronze table %s discards ingestion history; "
            "bronze writes should normally append.",
            name,
        )

    writer = df.write.format("delta").mode(mode)
    if merge_schema:
        writer = writer.option("mergeSchema", "true")
    if partition_by:
        writer = writer.partitionBy(*partition_by)

    if settings.is_catalog_mode:
        writer.saveAsTable(ref)
    else:
        writer.save(ref)


def table_exists(spark: SparkSession, settings: Settings | None = None, name: str = "") -> bool:
    """True when the logical table has been created."""
    settings = settings or get_settings()
    ref = settings.table_ref(name)

    if settings.is_catalog_mode:
        return spark.catalog.tableExists(ref)

    from delta.tables import DeltaTable

    return DeltaTable.isDeltaTable(spark, ref)
