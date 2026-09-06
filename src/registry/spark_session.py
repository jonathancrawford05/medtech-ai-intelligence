"""SparkSession factory -- the ONLY file that differs between local and Databricks.

Local mode builds a `local[*]` session and registers the Delta extensions.
On Databricks, Delta is already configured by the runtime, so this collapses to
``SparkSession.builder.getOrCreate()`` and the local config block is skipped
entirely (detected via ``DATABRICKS_RUNTIME_VERSION``).

Delta JAR resolution
--------------------
``delta-spark`` ships Python only; the matching Scala JARs are fetched from
Maven on first use. Resolving from Maven at *runtime* makes startup slow and
fails on an offline or restricted network, so the Docker image bakes the JARs
onto Spark's classpath at build time (see ``scripts/warm_delta_jars.py``).
This module prefers those pre-staged JARs and only falls back to Maven
resolution when running outside the image.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pyspark.sql import SparkSession

from registry.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_DELTA_EXTENSION = "io.delta.sql.DeltaSparkSessionExtension"
_DELTA_CATALOG = "org.apache.spark.sql.delta.catalog.DeltaCatalog"

_session: SparkSession | None = None


def _on_databricks() -> bool:
    return bool(os.environ.get("DATABRICKS_RUNTIME_VERSION"))


def _delta_jars_on_classpath() -> bool:
    """True when the Delta JARs are already staged in Spark's jars directory."""
    try:
        import pyspark

        jars_dir = Path(pyspark.__file__).parent / "jars"
    except Exception:  # pragma: no cover - pyspark is a hard dependency
        return False
    return any(jars_dir.glob("delta-spark*.jar")) and any(jars_dir.glob("delta-storage*.jar"))


def _build_local_session(settings: Settings) -> SparkSession:
    builder = (
        SparkSession.builder.appName(settings.spark_app_name)
        .master(settings.spark_master)
        .config("spark.sql.extensions", _DELTA_EXTENSION)
        .config("spark.sql.catalog.spark_catalog", _DELTA_CATALOG)
        .config("spark.driver.memory", settings.spark_driver_memory)
        .config("spark.sql.shuffle.partitions", str(settings.spark_shuffle_partitions))
        .config("spark.ui.enabled", str(settings.spark_ui_enabled).lower())
        .config("spark.sql.session.timeZone", "UTC")
    )

    if settings.spark_master.startswith("local"):
        # In a container the hostname often resolves to an address the driver
        # cannot actually reach (we have seen 192.0.2.2, a TEST-NET-1 address).
        # `host` is what the driver *advertises* and `bindAddress` is what it
        # listens on -- pin both to loopback so they cannot disagree.
        builder = builder.config("spark.driver.host", "127.0.0.1").config(
            "spark.driver.bindAddress", "127.0.0.1"
        )

    if _delta_jars_on_classpath():
        logger.debug("Using pre-staged Delta JARs from Spark's jars directory.")
        return builder.getOrCreate()

    logger.info("Delta JARs not pre-staged; resolving from Maven (first run may be slow).")
    from delta import configure_spark_with_delta_pip

    return configure_spark_with_delta_pip(builder).getOrCreate()


def get_spark(settings: Settings | None = None) -> SparkSession:
    """Return the process-wide SparkSession, creating it on first call.

    On Databricks this returns the runtime-provided session untouched.
    """
    global _session
    if _session is not None:
        return _session

    if _on_databricks():
        logger.info("Databricks runtime detected; using the runtime-provided session.")
        _session = SparkSession.builder.getOrCreate()
        return _session

    settings = settings or get_settings()
    _session = _build_local_session(settings)
    _session.sparkContext.setLogLevel("WARN")
    return _session


def stop_spark() -> None:
    """Stop and clear the cached session. Mainly for tests and CLI teardown."""
    global _session
    if _session is not None:
        _session.stop()
        _session = None
