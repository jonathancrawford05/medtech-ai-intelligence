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
from typing import Any

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
    user_metadata: str | None = None,
) -> None:
    """Write a DataFrame to a logical table.

    Defaults to ``append`` because bronze tables must never lose history.
    ``merge_schema`` allows additive source changes (a new FDA column) to land
    without a manual migration. ``user_metadata`` is recorded on the Delta commit
    and read back through `table_history` -- how a version says what it was
    built from (ADR 0015).
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
    if user_metadata is not None:
        writer = writer.option("userMetadata", user_metadata)

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


def _delta_table(spark: SparkSession, settings: Settings, name: str):
    from delta.tables import DeltaTable

    ref = settings.table_ref(name)
    if settings.is_catalog_mode:
        return DeltaTable.forName(spark, ref)
    return DeltaTable.forPath(spark, ref)


def table_history(
    spark: SparkSession, settings: Settings | None = None, name: str = ""
) -> list[dict[str, Any]]:
    """The table's Delta commit log, newest first.

    Each entry carries ``version``, ``operation`` and ``userMetadata`` -- enough to
    find a version by what it was built from rather than by its number.
    """
    settings = settings or get_settings()
    history = _delta_table(spark, settings, name).history()
    rows = history.select("version", "operation", "userMetadata").orderBy(history["version"].desc())
    return [r.asDict() for r in rows.collect()]


def table_properties(
    spark: SparkSession, settings: Settings | None = None, name: str = ""
) -> dict[str, str]:
    """The table's current ``TBLPROPERTIES``."""
    settings = settings or get_settings()
    return dict(_delta_table(spark, settings, name).detail().first()["properties"] or {})


def set_table_properties(
    spark: SparkSession,
    settings: Settings | None = None,
    name: str = "",
    properties: dict[str, str] | None = None,
) -> bool:
    """Set ``properties`` on a table, only where they differ. True when it wrote.

    Each ``SET TBLPROPERTIES`` is its own Delta commit, so an unconditional set on
    every build would bury the data versions under metadata-only ones.
    """
    settings = settings or get_settings()
    wanted = properties or {}
    current = table_properties(spark, settings, name)
    changed = {k: v for k, v in wanted.items() if current.get(k) != v}
    if not changed:
        return False

    ref = settings.table_ref(name)
    target = ref if settings.is_catalog_mode else f"delta.`{ref}`"
    assignments = ", ".join(f"'{k}' = '{v}'" for k, v in sorted(changed.items()))
    spark.sql(f"ALTER TABLE {target} SET TBLPROPERTIES ({assignments})")
    return True
