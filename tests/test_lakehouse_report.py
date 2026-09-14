"""`registry inspect`: read the local lakehouse back and say whether it looks right.

This is the only way to check what a local run actually produced without writing
ad-hoc PySpark, and it earned its place by finding a real bug on its first run
against real data (findings/0008): rows sat in the `other` specialty because
`config/specialty_taxonomy.yaml` spelled a panel differently from the FDA export.

So these tests assert the report *flags* problems. A report that only prints
counts would pass a weaker suite and would not have caught that bug.
"""

from __future__ import annotations

import datetime as dt

import pytest
from pyspark.sql.types import StructType

from registry import lakehouse_report, tables
from registry.schemas import BronzeFdaAiListRecord, DeviceRecord, spark_schema_for

pytestmark = pytest.mark.spark


def _bronze_row(submission: str, snapshot: str, ingested: dt.datetime, panel: str = "Radiology"):
    return (
        submission,
        f"Device {submission}",
        "Acme Medical, Inc.",
        "01/15/2024",
        panel,
        "QAS",
        "https://example.test/list",
        ingested,
        snapshot,
    )


def _silver_row(
    submission: str,
    *,
    panel: str = "Cardiovascular",
    category: str = "cardiovascular",
    decision_date: dt.date | None = dt.date(2024, 1, 15),
):
    return (
        submission,
        f"Device {submission}",
        "Acme Medical, Inc.",
        "acme medical",
        decision_date,
        "510k",
        panel,
        category,
        "QAS",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        "https://example.test/list",
    )


@pytest.fixture
def bronze_two_pulls(spark, lakehouse):
    """Two appended pulls of the same source -- bronze's append-only invariant."""
    rows = [
        _bronze_row("K1", "snap-a", dt.datetime(2024, 3, 1, 9, 0)),
        _bronze_row("K2", "snap-a", dt.datetime(2024, 3, 1, 9, 0)),
        _bronze_row("K1", "snap-b", dt.datetime(2024, 4, 1, 9, 0)),
        _bronze_row("K2", "snap-b", dt.datetime(2024, 4, 1, 9, 0)),
    ]
    df = spark.createDataFrame(rows, spark_schema_for(BronzeFdaAiListRecord))
    tables.write_table(df, lakehouse, "bronze_fda_ai_list", mode="overwrite")
    return lakehouse


def _write_silver(spark, settings, rows, *, allow_nulls: bool = False):
    schema = spark_schema_for(DeviceRecord)
    if allow_nulls:
        # DeviceRecord's generated schema makes submission_number/decision_date
        # non-nullable, so the pipeline cannot write these nulls -- that is the
        # first line of defence. The report's null check is the backstop for a
        # table written before a schema change or repaired by hand, so the only
        # honest way to exercise it is to relax the schema here.
        schema = StructType([f.__class__(f.name, f.dataType, True) for f in schema.fields])
    tables.write_table(
        spark.createDataFrame(rows, schema), settings, "silver_devices", mode="overwrite"
    )


class TestBuildReport:
    def test_says_what_to_run_when_bronze_is_missing(self, spark, lakehouse):
        report = lakehouse_report.build_report(spark, lakehouse)
        assert report.ok is False
        assert any("ingest-fda-list" in line for line in report.lines), report.lines

    def test_shows_pull_history_not_just_a_row_count(self, spark, bronze_two_pulls):
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        text = "\n".join(report.lines)
        # Two pulls must be visible as two snapshots: bronze being append-only is
        # the invariant a reader is checking, and one total row count hides it.
        assert "snap-a" in text and "snap-b" in text

    def test_names_the_panels_behind_rows_in_the_default_specialty(self, spark, bronze_two_pulls):
        _write_silver(
            spark,
            bronze_two_pulls,
            [
                _silver_row("K1"),
                _silver_row("K2", panel="Nonexistent Panel", category="other"),
            ],
        )
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        text = "\n".join(report.lines)
        assert "Nonexistent Panel" in text, "an uncurated panel must be named, not just counted"
        assert report.ok is False, "rows in the default specialty are a finding, not a statistic"

    def test_separates_a_missing_panel_from_an_uncurated_one(self, spark, bronze_two_pulls):
        """Review nit on PR #8. Both land in the default category, but "the FDA listed
        no panel" and "a panel nobody has curated" are different facts with different
        fixes -- only the second is answered by editing the taxonomy. Reporting them
        together also renders as `Uncurated FDA panel(s): ''`, which reads as a bug."""
        _write_silver(
            spark,
            bronze_two_pulls,
            [
                _silver_row("K1", panel="Nonexistent Panel", category="other"),
                _silver_row("K2", panel="", category="other"),
                _silver_row("K3", panel="   ", category="other"),
            ],
        )
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        text = "\n".join(report.lines)
        assert report.ok is False
        assert "Nonexistent Panel" in text
        assert "''" not in text, "an empty panel must not be listed as an uncurated label"
        uncurated_line = next(line for line in report.lines if "Uncurated FDA panel" in line)
        assert "1 row" in uncurated_line, uncurated_line
        missing_line = next(line for line in report.lines if "no panel" in line)
        assert "2 row" in missing_line, missing_line

    def test_a_missing_panel_alone_is_still_reported(self, spark, bronze_two_pulls):
        _write_silver(spark, bronze_two_pulls, [_silver_row("K1", panel="", category="other")])
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        text = "\n".join(report.lines)
        assert report.ok is False
        assert "no panel" in text
        assert "Uncurated FDA panel" not in text, "nothing to curate when the FDA listed nothing"

    def test_is_clean_when_every_panel_is_curated(self, spark, bronze_two_pulls):
        _write_silver(spark, bronze_two_pulls, [_silver_row("K1"), _silver_row("K2")])
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        assert report.ok is True, "\n".join(report.lines)

    def test_flags_a_null_decision_date_in_silver(self, spark, bronze_two_pulls):
        _write_silver(
            spark,
            bronze_two_pulls,
            [_silver_row("K1"), _silver_row("K2", decision_date=None)],
            allow_nulls=True,
        )
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        assert report.ok is False
        assert any("decision_date" in line for line in report.lines)

    def test_reports_enrichment_coverage_so_zero_percent_is_visible(self, spark, bronze_two_pulls):
        """ADR 0012 leaves these null until the openFDA pass runs; 0% must be legible
        as "not enriched yet" rather than looking like a silently empty column."""
        _write_silver(spark, bronze_two_pulls, [_silver_row("K1")])
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        text = "\n".join(report.lines)
        assert "device_class" in text and "0.0%" in text
        assert report.ok is True, "missing enrichment is expected, not a failure"

    def test_a_silverless_lakehouse_is_reported_not_an_error(self, spark, bronze_two_pulls):
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        assert report.ok is True
        assert any("build-silver" in line for line in report.lines)
