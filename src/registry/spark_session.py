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
import re
import shutil
import subprocess
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


# Spark 4.0 runs on JDK 17 or 21; 17 matches Databricks Runtime 17.x (ADR 0002).
MINIMUM_JAVA_MAJOR = 17

_JAVA_VERSION_RE = re.compile(r"(?:version\s+\"?|^openjdk\s+)(\d+)(?:[.\"\s]|$)", re.MULTILINE)

_NO_JVM_HELP = """Spark needs a JVM and no usable one was found.

Spark 4.0 requires JDK {minimum}+ ({found}).

Any one of these fixes it:

  1. Run it in the project image, which already carries JDK 17:
       docker compose run --rm registry ingest-fda-list
       docker compose run --rm test
  2. Install a JDK locally, then re-sync so the Delta JARs are staged:
       macOS:  brew install --cask temurin@17
       Debian: apt-get install -y openjdk-17-jdk-headless
       then:   make install
  3. Let CI do it -- the "Scheduled FDA ingest" workflow runs the real ingest on a
     runner that has JDK 17 and can reach fda.gov (docs/scheduled-ingest.md).

If a JDK is installed but not on PATH, set JAVA_HOME to it."""


class JavaRuntimeError(RuntimeError):
    """No usable JVM for Spark. Carries the remedies, not just the symptom."""


def _parse_java_major(output: str) -> int | None:
    """Extract the major version from `java -version` output.

    Handles both the legacy `1.8.0_401` form (major 8) and the modern
    `17.0.20.1` form, across the `java version "..."` and bare `openjdk N` layouts.
    """
    match = _JAVA_VERSION_RE.search(output or "")
    if match is None:
        return None
    major = int(match.group(1))
    if major == 1:
        # Legacy "1.8.0_x" scheme: the real major is the second component.
        legacy = re.search(r"version\s+\"?1\.(\d+)", output)
        return int(legacy.group(1)) if legacy else None
    return major


def _probe_java_version() -> tuple[int | None, str]:
    """Return (major version or None, raw output) from the java on PATH/JAVA_HOME."""
    java = None
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidate = Path(java_home) / "bin" / "java"
        if candidate.exists():
            java = str(candidate)
    java = java or shutil.which("java")
    if java is None:
        return None, "no `java` on PATH and JAVA_HOME is unset or invalid"

    try:
        # `java -version` writes to stderr on every JDK worth supporting.
        result = subprocess.run(
            [java, "-version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"could not run `{java} -version`: {exc}"

    output = f"{result.stderr}\n{result.stdout}".strip()
    return _parse_java_major(output), output


def _require_jvm() -> None:
    """Fail early and legibly when Spark could not start for lack of a JVM.

    Without this, a machine with no JDK dies inside py4j with JAVA_GATEWAY_EXITED,
    which names neither the cause nor the fix (findings/0005).
    """
    major, raw = _probe_java_version()
    if major is not None and major >= MINIMUM_JAVA_MAJOR:
        logger.debug("JVM preflight passed: Java %s", major)
        return

    found = (
        f"found Java {major}"
        if major is not None
        else f"none detected: {raw.splitlines()[0] if raw else 'unknown'}"
    )
    raise JavaRuntimeError(_NO_JVM_HELP.format(minimum=MINIMUM_JAVA_MAJOR, found=found))


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
    # Check for a JVM before py4j does, so a missing JDK reports itself in one
    # readable sentence instead of JAVA_GATEWAY_EXITED (findings/0005).
    _require_jvm()
    _session = _build_local_session(settings)
    _session.sparkContext.setLogLevel("WARN")
    return _session


def stop_spark() -> None:
    """Stop and clear the cached session. Mainly for tests and CLI teardown."""
    global _session
    if _session is not None:
        _session.stop()
        _session = None
