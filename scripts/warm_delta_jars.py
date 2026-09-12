"""Stage the Delta Lake JARs onto Spark's classpath at image build time.

``delta-spark`` is a Python-only wheel: the matching Scala JARs are resolved from
Maven by Ivy the first time a session starts. Doing that at *runtime* is slow and
fails outright on a restricted or offline network, which makes containers and CI
flaky for reasons that have nothing to do with our code.

Running this once during ``docker build`` resolves the JARs and copies them into
``pyspark/jars``, so at runtime Delta is simply on the classpath -- exactly how it
behaves on Databricks, where the runtime provides it.

Two-step use in the image
-------------------------
Maven resolution is expensive (a network fetch plus a JVM start), so it must not
sit in a layer that application code invalidates. The image resolves once in the
cached dependency layer and then only *copies* afterwards::

    # cached dependency layer -- Maven runs once, before any source is copied
    python scripts/warm_delta_jars.py --stage-to /opt/delta-jars

    # after the final `uv sync`, which can reinstall pyspark and discard them
    python scripts/warm_delta_jars.py --from-dir /opt/delta-jars

``--from-dir`` needs no network and no JVM, so re-staging after a source change
costs a file copy rather than a Maven round trip.

Usage:  python scripts/warm_delta_jars.py [--stage-to DIR | --from-dir DIR]
"""

from __future__ import annotations

import argparse
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


def _copy_onto_classpath(jars: list[Path], target: Path) -> None:
    for jar in jars:
        dest = target / jar.name.replace("io.delta_", "")
        if not dest.exists():
            shutil.copy2(jar, dest)
        print(f"staged {dest.name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--stage-to",
        metavar="DIR",
        help="Resolve from Maven and also keep a copy in DIR for later --from-dir use.",
    )
    group.add_argument(
        "--from-dir",
        metavar="DIR",
        help="Copy previously staged JARs from DIR. No network and no JVM required.",
    )
    args = parser.parse_args(argv)

    target = spark_jars_dir()

    if args.from_dir:
        source = Path(args.from_dir)
        jars = sorted(source.glob("*.jar"))
        if not jars:
            print(f"ERROR: no JARs found in {source}; run --stage-to first.", file=sys.stderr)
            return 1
    else:
        jars = resolve_delta_jars()
        if not jars:
            print("ERROR: no Delta JARs resolved; is Maven Central reachable?", file=sys.stderr)
            return 1
        if args.stage_to:
            stage = Path(args.stage_to)
            stage.mkdir(parents=True, exist_ok=True)
            for jar in jars:
                shutil.copy2(jar, stage / jar.name.replace("io.delta_", ""))
            print(f"kept a copy of {len(jars)} JAR(s) in {stage}")

    _copy_onto_classpath(jars, target)

    staged = sorted(p.name for p in target.glob("delta-*.jar"))
    print(f"\nDelta JARs on Spark classpath ({target}): {staged}")
    if not staged:
        print("ERROR: Delta JARs are not on the Spark classpath.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
