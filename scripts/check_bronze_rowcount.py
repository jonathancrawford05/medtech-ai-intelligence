"""Assert the latest bronze FDA AI-list pull is full-sized (Phase 1 acceptance).

Reads the most recent snapshot in ``bronze_fda_ai_list`` and fails if it holds
fewer than ``INGEST_MIN_ROWS`` rows (default 1500, ~95% of the ~1,615-row source
list). This is the automated form of the development plan's Phase 1 acceptance
and closes finding 0001 ("no bronze table has held the 1,615 rows"): a green run
is a live, full-sized pull actually landing in bronze.

Bronze is append-only, so a run is identified by its ``source_snapshot_id``; on
a fresh CI runner there is exactly one. Writes a one-line human summary to
``ingest-summary.txt`` for the workflow artifact and the job summary.
"""

from __future__ import annotations

import os
from pathlib import Path

from pyspark.sql import functions as F

from registry import tables
from registry.config.settings import get_settings
from registry.spark_session import get_spark

DEFAULT_MIN_ROWS = 1500  # ~95% of the ~1,615-row source list


def _default_summary_path() -> Path:
    return Path(os.environ.get("INGEST_SUMMARY_PATH", "ingest-summary.txt"))


def main(min_rows: int | None = None, summary_path: Path | None = None) -> int:
    # Read config here (not at import) so the check is unit-testable without env
    # gymnastics: a test calls main(min_rows=..., summary_path=...) directly.
    if min_rows is None:
        min_rows = int(os.environ.get("INGEST_MIN_ROWS", str(DEFAULT_MIN_ROWS)))
    if summary_path is None:
        summary_path = _default_summary_path()

    settings = get_settings()
    spark = get_spark(settings)
    df = tables.read_table(spark, settings, "bronze_fda_ai_list")

    latest = df.orderBy(F.col("ingested_at").desc()).select("source_snapshot_id").first()
    if latest is None:
        summary_path.write_text("FAIL: bronze_fda_ai_list is empty; no rows ingested\n")
        print("FAIL: bronze_fda_ai_list is empty")
        return 1

    snapshot_id = latest["source_snapshot_id"]
    snapshot = df.filter(F.col("source_snapshot_id") == snapshot_id)
    n_rows = snapshot.count()
    n_distinct = snapshot.select("submission_number").distinct().count()

    summary = (
        f"snapshot {snapshot_id}: {n_rows} rows "
        f"({n_distinct} distinct submission numbers); floor {min_rows}"
    )
    summary_path.write_text(summary + "\n")
    print(summary)

    if n_rows < min_rows:
        print(f"FAIL: {n_rows} rows < required floor {min_rows}")
        return 1
    print(f"PASS: {n_rows} rows >= floor {min_rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
