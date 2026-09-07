"""Stage the Delta Lake JARs onto Spark's classpath at image build time.

``delta-spark`` is a Python-only wheel: the matching Scala JARs are resolved from
Maven by Ivy the first time a session starts. Doing that at *runtime* is slow and
fails outright on a restricted or offline network, which makes containers and CI
flaky for reasons that have nothing to do with our code.

Running this once during ``docker build`` resolves the JARs and copies them into
``pyspark/jars``, so at runtime Delta is simply on the classpath -- exactly how it
behaves on Databricks, where the runtime provides it.

Usage:  python scripts/warm_delta_jars.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def spark_jars_dir() -> Path:
    import pyspark

    return Path(pyspark.__file__).parent / "jars"


def resolve_delta_jars() -> list[Path]:
    """Trigger Ivy resolution via a throwaway session, then locate the JARs."""
    from delta import configure_spark_with_delta_pip
    from pyspark.sql import SparkSession

    builder = (
        SparkSession.builder.appName("warm-delta-jars")
        .master("local[1]")
        .config("spark.driver.host", "127.0.0.1")
        .config("spark.driver.bindAddress", "127.0.0.1")
        .config("spark.ui.enabled", "false")
    )
    session = configure_spark_with_delta_pip(builder).getOrCreate()
    session.stop()

    jars: list[Path] = []
    for ivy_root in Path.home().glob(".ivy2*"):
        jars.extend(ivy_root.glob("jars/*delta*.jar"))
        jars.extend(ivy_root.glob("cache/io.delta/*/jars/*.jar"))

    # De-duplicate by filename, preferring the flat `jars/` copies.
    seen: dict[str, Path] = {}
    for jar in jars:
        seen.setdefault(jar.name.replace("io.delta_", ""), jar)
    return sorted(seen.values())


def main() -> int:
    target = spark_jars_dir()
    jars = resolve_delta_jars()
    if not jars:
        print("ERROR: no Delta JARs resolved; is Maven Central reachable?", file=sys.stderr)
        return 1

    for jar in jars:
        dest = target / jar.name.replace("io.delta_", "")
        if not dest.exists():
            shutil.copy2(jar, dest)
        print(f"staged {dest.name}")

    staged = sorted(p.name for p in target.glob("delta-*.jar"))
    print(f"\nDelta JARs on Spark classpath ({target}): {staged}")
    return 0 if staged else 1


if __name__ == "__main__":
    raise SystemExit(main())
