"""Tests for the bronze row-count acceptance gate.

Like ``scripts/warm_delta_jars.py``, this is exit-code-gating ops tooling the
scheduled-ingest workflow leans on: a regression here could silently pass a short
pull or crash the job far from the cause. Spark-marked because the check runs real
DataFrame ops (``orderBy``/``filter``/``count``); ``tables.read_table`` is
monkeypatched to return a small in-memory frame, so no Delta table or IO is
touched and the boundaries (empty, below-floor, at-floor, latest-snapshot
selection) are all exercised.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from pyspark.sql.types import StringType, StructField, StructType, TimestampType

import check_bronze_rowcount as mod
from registry.config.settings import Settings

_SCHEMA = StructType(
    [
        StructField("submission_number", StringType()),
        StructField("ingested_at", TimestampType()),
        StructField("source_snapshot_id", StringType()),
    ]
)


@pytest.fixture
def patched(monkeypatch, spark):
    """Point the script at the shared local Spark session; caller supplies the frame."""
    monkeypatch.setattr(mod, "get_spark", lambda settings=None: spark)
    monkeypatch.setattr(mod, "get_settings", lambda: Settings())

    def _use(rows: list[tuple]) -> None:
        df = spark.createDataFrame(rows, schema=_SCHEMA)
        monkeypatch.setattr(mod.tables, "read_table", lambda *a, **k: df)

    return _use


@pytest.mark.spark
class TestBronzeRowcountCheck:
    def test_passes_at_or_above_floor(self, patched, tmp_path: Path):
        ts = dt.datetime(2026, 9, 1, 6, 0)
        patched([(f"K{i:06d}", ts, "snapA") for i in range(3)])
        summary = tmp_path / "ingest-summary.txt"
        rc = mod.main(min_rows=3, summary_path=summary)
        assert rc == 0
        assert "3 rows" in summary.read_text()

    def test_fails_below_floor(self, patched, tmp_path: Path):
        ts = dt.datetime(2026, 9, 1, 6, 0)
        patched([("K000001", ts, "snapA")])
        summary = tmp_path / "ingest-summary.txt"
        rc = mod.main(min_rows=2, summary_path=summary)
        assert rc == 1

    def test_fails_when_empty(self, patched, tmp_path: Path):
        patched([])
        summary = tmp_path / "ingest-summary.txt"
        rc = mod.main(min_rows=1, summary_path=summary)
        assert rc == 1
        assert "empty" in summary.read_text().lower()

    def test_counts_only_the_latest_snapshot(self, patched, tmp_path: Path):
        """Bronze is append-only; the gate must judge the newest pull, not the union."""
        old = dt.datetime(2026, 8, 1, 6, 0)
        new = dt.datetime(2026, 9, 1, 6, 0)
        rows = [(f"K{i:06d}", old, "snapOld") for i in range(5)]  # a full older pull
        rows += [("K900001", new, "snapNew")]  # a short newest pull -> must fail
        patched(rows)
        summary = tmp_path / "ingest-summary.txt"
        rc = mod.main(min_rows=3, summary_path=summary)
        assert rc == 1
        assert "snapNew" in summary.read_text()
