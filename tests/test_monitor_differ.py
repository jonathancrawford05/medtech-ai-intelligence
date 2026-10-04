"""The snapshot differ (ADR 0015 Decisions 1, 2, 6).

Pure classification is tested without a JVM; pairing silver versions through
Delta history and time travel carries the `spark` marker.
"""

from __future__ import annotations

import datetime as dt

import pytest

from registry.monitor import differ
from tests.monitor_fixtures import CURR, PREV, curr_rows, device, prev_rows


def _by_key(movements):
    return {m.submission_number: m for m in movements}


class TestClassify:
    def test_new_submission_numbers_are_added(self):
        moves = differ.classify(prev_rows(), curr_rows(), PREV, CURR)
        added = sorted(m.submission_number for m in moves if m.movement == "added")
        assert added == ["DEN1000", "K700", "K800", "K900"]

    def test_field_changes_are_changed_with_the_fields_named(self):
        moves = _by_key(differ.classify(prev_rows(), curr_rows(), PREV, CURR))
        assert moves["K300"].movement == "changed"
        assert moves["K300"].changed_fields == ("specialty_category", "specialty_panel")
        assert moves["K400"].movement == "changed"
        assert moves["K400"].changed_fields == ("device_name",)

    def test_devices_missing_from_the_newest_pull_are_removed(self):
        """Silver never drops a row, so 'removed' is read off the row stamp: the
        row still carries snapA while the version is stamped snapB."""
        moves = _by_key(differ.classify(prev_rows(), curr_rows(), PREV, CURR))
        assert moves["K500"].movement == "removed"
        assert moves["K600"].movement == "removed"
        assert moves["K500"].changed_fields == ("on_list",)

    def test_identical_rows_produce_no_movement(self):
        moves = _by_key(differ.classify(prev_rows(), curr_rows(), PREV, CURR))
        assert "K100" not in moves
        assert "K200" not in moves

    def test_the_exact_movement_set(self):
        moves = differ.classify(prev_rows(), curr_rows(), PREV, CURR)
        assert [(m.submission_number, m.movement) for m in moves] == [
            ("DEN1000", "added"),
            ("K300", "changed"),
            ("K400", "changed"),
            ("K500", "removed"),
            ("K600", "removed"),
            ("K700", "added"),
            ("K800", "added"),
            ("K900", "added"),
        ]

    def test_a_snapshot_diffed_against_itself_is_empty(self):
        """Today's live state, stated as a test."""
        assert differ.classify(prev_rows(), prev_rows(), PREV, PREV) == []

    def test_a_restamp_alone_is_not_a_change(self):
        """Every row's stamp changes with each pull; that is lineage, not movement."""
        prev = [device("K1", PREV)]
        curr = [device("K1", CURR)]
        assert differ.classify(prev, curr, PREV, CURR) == []

    def test_one_movement_per_submission(self):
        """Diff grain is the submission number, even if a row changes twice over."""
        prev = [device("K1", PREV, category="radiology")]
        curr = [{**device("K1", PREV, category="cardiovascular", name="Renamed")}]
        moves = differ.classify(prev, curr, PREV, CURR)
        assert len(moves) == 1
        assert moves[0].movement == "removed"
        assert moves[0].changed_fields == (
            "device_name",
            "on_list",
            "specialty_category",
            "specialty_panel",
        )

    def test_a_device_back_on_the_list_is_a_change(self):
        prev = [device("K1", "snapOld")]
        curr = [device("K1", CURR)]
        moves = differ.classify(prev, curr, PREV, CURR)
        assert [(m.movement, m.changed_fields) for m in moves] == [("changed", ("on_list",))]

    def test_a_row_gone_from_silver_entirely_is_removed(self):
        moves = differ.classify([device("K1", PREV)], [], PREV, CURR)
        assert [(m.submission_number, m.movement) for m in moves] == [("K1", "removed")]
        assert moves[0].curr is None

    def test_movements_carry_both_sides(self):
        moves = _by_key(differ.classify(prev_rows(), curr_rows(), PREV, CURR))
        assert moves["K700"].prev is None
        assert moves["K700"].curr["specialty_category"] == "cardiovascular"
        assert moves["K300"].prev["specialty_category"] == "radiology"


class TestPullSize:
    def test_counts_rows_carried_by_that_pull(self):
        assert differ.pull_size(prev_rows(), PREV) == 6
        assert differ.pull_size(curr_rows(), CURR) == 8


@pytest.mark.spark
class TestSnapshotPairing:
    """Decisions 1 and 2: pair the newest version of each distinct snapshot id,
    read through time travel. Never adjacent version numbers, never ingested_at."""

    def _bronze_row(self, submission, snapshot, ingested_at, **overrides):
        row = {
            "submission_number": submission,
            "device_name": f"Device {submission}",
            "applicant_raw": "Aidoc Medical Ltd",
            "decision_date_raw": "11/01/2024",
            "panel_raw": "Radiology",
            "product_code": "QAS",
            "source_url": f"https://example.org/{submission}",
            "ingested_at": ingested_at,
            "source_snapshot_id": snapshot,
        }
        row.update(overrides)
        return row

    def _pull(self, spark, settings, rows):
        from registry import tables
        from registry.schemas import BronzeFdaAiListRecord, spark_schema_for

        df = spark.createDataFrame(rows, schema=spark_schema_for(BronzeFdaAiListRecord))
        tables.write_table(df, settings, "bronze_fda_ai_list", mode="append")

    def test_no_silver_means_no_pair(self, spark, lakehouse):
        assert differ.latest_pair(spark, lakehouse) is None

    def test_one_snapshot_means_no_pair(self, spark, lakehouse):
        """Three pulls, one content hash: the live state. Nothing to diff."""
        from registry.transform import bronze_to_silver as bts

        for day in (1, 8, 15):
            self._pull(
                spark, lakehouse, [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, day))]
            )
            bts.run(spark, lakehouse)
        assert differ.latest_pair(spark, lakehouse) is None

    def test_pairs_the_two_newest_distinct_snapshots(self, spark, lakehouse):
        from registry.transform import bronze_to_silver as bts

        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 1))])
        bts.run(spark, lakehouse)
        self._pull(
            spark,
            lakehouse,
            [
                self._bronze_row("K1", "snapB", dt.datetime(2026, 9, 8)),
                self._bronze_row("K2", "snapB", dt.datetime(2026, 9, 8)),
            ],
        )
        bts.run(spark, lakehouse)

        prev, curr = differ.latest_pair(spark, lakehouse)
        assert (prev.source_snapshot_id, curr.source_snapshot_id) == ("snapA", "snapB")
        assert prev.version < curr.version

        prev_keys = {r["submission_number"] for r in differ.read_version(spark, lakehouse, prev)}
        curr_keys = {r["submission_number"] for r in differ.read_version(spark, lakehouse, curr)}
        assert (prev_keys, curr_keys) == ({"K1"}, {"K1", "K2"})

    def test_a_rebuild_on_the_same_snapshot_pairs_its_newest_version(self, spark, lakehouse):
        """Re-enrichment writes a new version with the same snapshot id. It is the
        same world: one distinct snapshot, so still no pair -- and once a second
        snapshot arrives, the newest version of the first is what it pairs with."""
        from registry import tables
        from registry.schemas import DeviceEnrichmentRecord, spark_schema_for
        from registry.transform import bronze_to_silver as bts

        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 1))])
        bts.run(spark, lakehouse)
        enriched = DeviceEnrichmentRecord(
            submission_number="K1",
            enriched_at=dt.datetime(2026, 9, 2),
            submission_found=False,
            classification_found=True,
            product_code="QAS",
            device_class_raw="2",
        )
        tables.write_table(
            spark.createDataFrame(
                [enriched.model_dump()], spark_schema_for(DeviceEnrichmentRecord)
            ),
            lakehouse,
            "silver_device_enrichment",
            mode="overwrite",
        )
        assert bts.run(spark, lakehouse) == 1  # same snapshot, new content
        assert differ.latest_pair(spark, lakehouse) is None
        enriched_version = bts.current_stamp_version(spark, lakehouse)

        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapB", dt.datetime(2026, 9, 8))])
        bts.run(spark, lakehouse)

        prev, curr = differ.latest_pair(spark, lakehouse)
        assert prev.version == enriched_version
        assert (
            differ.classify(
                differ.read_version(spark, lakehouse, prev),
                differ.read_version(spark, lakehouse, curr),
                prev.source_snapshot_id,
                curr.source_snapshot_id,
            )
            == []
        )

    def test_a_list_that_reverts_pairs_the_newest_versions(self, spark, lakehouse):
        """A -> B -> A: the pair is (B, A-again), not (A, B)."""
        from registry.transform import bronze_to_silver as bts

        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 1))])
        bts.run(spark, lakehouse)
        self._pull(
            spark,
            lakehouse,
            [self._bronze_row("K1", "snapB", dt.datetime(2026, 9, 8), device_name="Renamed")],
        )
        bts.run(spark, lakehouse)
        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 15))])
        bts.run(spark, lakehouse)

        prev, curr = differ.latest_pair(spark, lakehouse)
        assert (prev.source_snapshot_id, curr.source_snapshot_id) == ("snapB", "snapA")
        assert curr.version > prev.version

    def test_unstamped_versions_are_ignored(self, spark, lakehouse):
        """A silver version written before ADR 0015 cannot be paired by content."""
        from registry import tables
        from registry.transform import bronze_to_silver as bts

        legacy = spark.createDataFrame(
            [("K1", "Device")], "submission_number string, device_name string"
        )
        tables.write_table(legacy, lakehouse, "silver_devices", mode="overwrite")
        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 1))])
        bts.run(spark, lakehouse)

        versions = differ.snapshot_versions(spark, lakehouse)
        assert [v.source_snapshot_id for v in versions] == ["snapA"]
        assert differ.latest_pair(spark, lakehouse) is None

    def test_never_pairs_across_an_unknown_operation_and_reports_it(
        self, spark, lakehouse, monkeypatch
    ):
        """An unlisted operation between two builds might have changed the rows,
        so A and B are not a trustworthy pair: report the barrier, diff nothing.
        Two snapshots built after it pair normally."""
        from registry import tables
        from registry.transform import bronze_to_silver as bts

        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 1))])
        bts.run(spark, lakehouse)
        ref = lakehouse.table_ref("silver_devices")
        spark.sql(f"ALTER TABLE delta.`{ref}` ALTER COLUMN device_name COMMENT 'display name'")
        op = tables.table_history(spark, lakehouse, "silver_devices")[0]["operation"]
        monkeypatch.setattr(
            tables, "METADATA_ONLY_OPERATIONS", tables.METADATA_ONLY_OPERATIONS - {op}
        )
        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapB", dt.datetime(2026, 9, 8))])
        bts.run(spark, lakehouse)

        pairing = differ.pairing(spark, lakehouse)
        assert pairing.pair is None
        assert pairing.barrier["operation"] == op
        assert differ.latest_pair(spark, lakehouse) is None

        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapC", dt.datetime(2026, 9, 15))])
        bts.run(spark, lakehouse)
        prev, curr = differ.latest_pair(spark, lakehouse)
        assert (prev.source_snapshot_id, curr.source_snapshot_id) == ("snapB", "snapC")
        assert differ.pairing(spark, lakehouse).barrier is None
