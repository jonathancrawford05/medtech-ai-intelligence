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

import enum
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from pyspark.sql import DataFrame, SparkSession

from registry.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# Bronze is append-only by contract: we keep the history of what each source
# looked like at every pull, so a source changing shape is auditable after the fact.
BRONZE_PREFIX = "bronze_"

# The history walk classifies every Delta commit three ways (ADR 0015, ADR 0017).
#
# Data writes rewrite rows: each is a version of the table's contents, read for
# its stamp. RESTORE is one deliberately -- it rewrites rows without a build
# stamp, so it reads as "unstamped" and forces the next silver build to write.
DATA_WRITE_OPERATIONS = frozenset(
    {
        "WRITE",
        "CREATE TABLE AS SELECT",
        "CREATE OR REPLACE TABLE AS SELECT",
        "REPLACE TABLE AS SELECT",
        "MERGE",
        "UPDATE",
        "DELETE",
        "RESTORE",
    }
)

# Metadata-only commits leave the rows alone and are looked past: OPTIMIZE (which
# Databricks auto-compaction commits unprompted) rewrites files, not contents.
METADATA_ONLY_OPERATIONS = frozenset(
    {
        "SET TBLPROPERTIES",
        "UNSET TBLPROPERTIES",
        "VACUUM START",
        "VACUUM END",
        "OPTIMIZE",
        "CHANGE COLUMN",
        "ADD COLUMNS",
        "UPGRADE PROTOCOL",
    }
)
# Anything in neither list is UNKNOWN, and the walk fails safe on it: an
# unlisted operation might have rewritten the rows, so stepping past it to an
# older stamp could let the rebuild gate skip a needed rebuild (silver silently
# stale) or the differ pair across a change it cannot see. The walk stops there
# and reports the commit as an unstamped write. Adding an operation to a list is
# a reviewed decision; forgetting one costs a rebuild, never correctness.


class HistoryKind(enum.Enum):
    DATA_WRITE = "data_write"
    METADATA_ONLY = "metadata_only"
    UNKNOWN = "unknown"


def classify_operation(operation: str) -> HistoryKind:
    """Which of the three kinds a Delta commit's ``operation`` is."""
    if operation in DATA_WRITE_OPERATIONS:
        return HistoryKind.DATA_WRITE
    if operation in METADATA_ONLY_OPERATIONS:
        return HistoryKind.METADATA_ONLY
    return HistoryKind.UNKNOWN


@dataclass(frozen=True)
class HistoryWalk:
    """The data-writing versions, newest first, and where the walk had to stop.

    ``writes`` ends with the unknown commit (``userMetadata`` cleared, so it
    parses as unstamped) when the walk stopped; ``barrier`` is that commit as
    Delta reported it, or None when the walk reached the start of history.
    """

    writes: list[dict[str, Any]]
    barrier: dict[str, Any] | None


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


def walk_history(history: Iterable[dict[str, Any]]) -> HistoryWalk:
    """Walk newest-first history: keep data writes, skip metadata-only, stop at unknown."""
    writes: list[dict[str, Any]] = []
    for entry in history:
        kind = classify_operation(entry["operation"])
        if kind is HistoryKind.DATA_WRITE:
            writes.append(entry)
        elif kind is HistoryKind.UNKNOWN:
            logger.warning(
                "Unrecognised Delta operation %r at version %s; treating it as an "
                "unstamped write and not reading history past it. If it never "
                "changes rows, add it to METADATA_ONLY_OPERATIONS in registry/tables.py.",
                entry["operation"],
                entry["version"],
            )
            writes.append({**entry, "userMetadata": None})
            return HistoryWalk(writes=writes, barrier=entry)
    return HistoryWalk(writes=writes, barrier=None)


def data_writes(history: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The data-writing history entries, order preserved (see `walk_history`)."""
    return walk_history(history).writes


def data_write_history(
    spark: SparkSession, settings: Settings | None = None, name: str = ""
) -> list[dict[str, Any]]:
    """`table_history` narrowed to the commits that wrote rows, newest first.

    The walk the silver rebuild gate and the change monitor share: each entry is
    one version of the table's *contents*, with the stamp it was written with.
    """
    return data_writes(table_history(spark, settings, name))


def walk_table_history(
    spark: SparkSession, settings: Settings | None = None, name: str = ""
) -> HistoryWalk:
    """`walk_history` over a table's Delta log, for callers that must report a stop."""
    return walk_history(table_history(spark, settings, name))


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
    detail = _delta_table(spark, settings, name).detail().first()
    current = dict(detail["properties"] or {})
    changed = {k: v for k, v in wanted.items() if current.get(k) != v}
    if not changed:
        return False

    if settings.is_catalog_mode:
        target = settings.table_ref(name)
    else:
        # Delta's delta.`path` SQL identifier resolves only absolute paths, and the
        # default lakehouse_root is "./lakehouse". DESCRIBE DETAIL's `location` is
        # the absolute, scheme-qualified path the table actually lives at.
        target = f"delta.`{detail['location']}`"
    assignments = ", ".join(f"'{k}' = '{v}'" for k, v in sorted(changed.items()))
    spark.sql(f"ALTER TABLE {target} SET TBLPROPERTIES ({assignments})")
    return True
