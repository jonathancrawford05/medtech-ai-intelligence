"""bronze -> silver.

The roadmap's Issue 2 acceptance: one row per latest submission; pathway / class /
panel / company / specialty populated; schema parity via the generated StructType;
the join provably picks the latest pull; deterministic on fixtures.

Pure-Python row logic is tested without Spark; the Delta-level behaviour (latest
pull wins, schema parity) carries the `spark` marker.
"""

from __future__ import annotations

import datetime as dt

import pytest

from registry.transform import bronze_to_silver as bts


class TestPathwayDerivation:
    @pytest.mark.parametrize(
        ("submission", "expected"),
        [
            ("K243456", "510k"),
            ("k243456", "510k"),
            ("DEN230011", "de_novo"),
            ("P130020", "pma"),
            ("P130020/S005", "pma"),  # a supplement is still the PMA pathway
        ],
    )
    def test_derives_from_the_submission_prefix(self, submission, expected):
        assert bts.derive_pathway(submission) == expected

    @pytest.mark.parametrize("bad", ["", "   ", "X999", "12345", None])
    def test_an_unrecognised_prefix_is_none_not_a_guess(self, bad):
        assert bts.derive_pathway(bad) is None

    def test_den_is_checked_before_the_bare_d(self):
        """DEN must not be mistaken for anything else by prefix-order accident."""
        assert bts.derive_pathway("DEN250057") == "de_novo"


class TestDecisionDateParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("11/01/2024", dt.date(2024, 11, 1)),  # FDA's US M/D/Y export
            ("2024-11-01", dt.date(2024, 11, 1)),  # ISO
            ("01/31/2024", dt.date(2024, 1, 31)),  # unambiguously month-first
        ],
    )
    def test_parses_the_formats_the_source_actually_uses(self, raw, expected):
        assert bts.parse_decision_date(raw) == expected

    @pytest.mark.parametrize("bad", ["", "   ", "not a date", "31/31/2024", None])
    def test_an_unparseable_date_is_none_not_an_exception(self, bad):
        """Bronze deliberately keeps dates raw; one bad row must not fail the build."""
        assert bts.parse_decision_date(bad) is None


class TestSupplementSplit:
    def test_a_supplement_keeps_its_whole_key_and_splits_alongside(self):
        """ADR 0012: the suffix stays in submission_number."""
        base, supp = bts.split_supplement("P130020/S005")
        assert (base, supp) == ("P130020", "S005")

    def test_a_base_pma_has_no_supplement(self):
        assert bts.split_supplement("P130020") == ("P130020", None)

    @pytest.mark.parametrize("non_pma", ["K243456", "DEN230011"])
    def test_non_pma_submissions_split_to_nothing(self, non_pma):
        assert bts.split_supplement(non_pma) == (None, None)


class TestRowBuild:
    def _bronze(self, **overrides):
        base = {
            "submission_number": "K243456",
            "device_name": "CaRi-Heart",
            "applicant_raw": "Caristo Diagnostics Ltd",
            "decision_date_raw": "11/01/2024",
            "panel_raw": "Cardiovascular",
            "product_code": "QIH",
            "source_url": "https://example.org/K243456",
            "ingested_at": dt.datetime(2026, 9, 13, 12, 0),
            "source_snapshot_id": "snap1",
        }
        base.update(overrides)
        return base

    def test_builds_a_device_record_from_the_ai_list_alone(self, silver_ctx):
        rec = bts.build_device_record(self._bronze(), silver_ctx)
        assert rec is not None
        assert rec.submission_number == "K243456"
        assert rec.pathway == "510k"
        assert rec.decision_date == dt.date(2024, 11, 1)
        assert rec.specialty_category == "cardiovascular"
        assert rec.applicant_resolved == "Caristo Diagnostics"
        # openFDA-dependent fields stay unenriched (ADR 0012)
        assert rec.device_class is None
        assert rec.has_pccp is None

    def test_a_supplement_keeps_the_full_key(self, silver_ctx):
        rec = bts.build_device_record(self._bronze(submission_number="P130020/S005"), silver_ctx)
        assert rec.submission_number == "P130020/S005"
        assert rec.pma_base_number == "P130020"
        assert rec.pma_supplement_number == "S005"
        assert rec.pathway == "pma"

    def test_a_row_without_a_usable_date_is_skipped_not_guessed(self, silver_ctx):
        """Phase 2 acceptance requires no null decision_date in silver."""
        assert bts.build_device_record(self._bronze(decision_date_raw="junk"), silver_ctx) is None

    def test_a_row_without_a_derivable_pathway_is_skipped(self, silver_ctx):
        assert bts.build_device_record(self._bronze(submission_number="X1"), silver_ctx) is None

    def test_a_missing_device_name_is_skipped(self, silver_ctx):
        assert bts.build_device_record(self._bronze(device_name=None), silver_ctx) is None

    def test_an_unmapped_panel_falls_back_and_is_recorded(self, silver_ctx):
        """A panel we have not curated defaults, and is surfaced for curation.

        Deliberately a synthetic name: every panel in the real config is mapped,
        so this asserts the fallback path rather than the state of the config.
        """
        rec = bts.build_device_record(self._bronze(panel_raw="Wholly Uncurated Panel"), silver_ctx)
        assert rec.specialty_category == "other"
        assert "Wholly Uncurated Panel" in silver_ctx.taxonomy.unmapped_panels

    def test_a_curated_panel_does_not_land_in_the_unmapped_set(self, silver_ctx):
        bts.build_device_record(self._bronze(panel_raw="Radiology"), silver_ctx)
        assert silver_ctx.taxonomy.unmapped_panels == set()


@pytest.mark.spark
class TestTransform:
    def _rows(self, spark, records):
        from registry.schemas import BronzeFdaAiListRecord, spark_schema_for

        return spark.createDataFrame(records, schema=spark_schema_for(BronzeFdaAiListRecord))

    def _bronze_row(self, submission, snapshot, ingested_at, device_name="Device"):
        return {
            "submission_number": submission,
            "device_name": device_name,
            "applicant_raw": "Aidoc Medical Ltd",
            "decision_date_raw": "11/01/2024",
            "panel_raw": "Radiology",
            "product_code": "QAS",
            "source_url": f"https://example.org/{submission}",
            "ingested_at": ingested_at,
            "source_snapshot_id": snapshot,
        }

    def test_one_row_per_submission_from_the_latest_pull(self, spark, lakehouse):
        """The core join rule: bronze holds every pull, silver holds the newest."""
        from registry import tables

        old = dt.datetime(2026, 9, 1, 12, 0)
        new = dt.datetime(2026, 9, 8, 12, 0)
        df = self._rows(
            spark,
            [
                self._bronze_row("K1", "snapA", old, device_name="Old Name"),
                self._bronze_row("K2", "snapA", old),
                self._bronze_row("K1", "snapB", new, device_name="New Name"),
                self._bronze_row("K2", "snapB", new),
            ],
        )
        tables.write_table(df, lakehouse, "bronze_fda_ai_list", mode="overwrite")

        written = bts.run(spark, lakehouse)
        silver = tables.read_table(spark, lakehouse, "silver_devices")

        assert written == 2
        assert silver.count() == 2
        names = {r["submission_number"]: r["device_name"] for r in silver.collect()}
        assert names["K1"] == "New Name", "the later pull must win"

    def test_silver_matches_the_generated_schema(self, spark, lakehouse):
        from registry import tables
        from registry.schemas import DeviceRecord, spark_schema_for

        df = self._rows(spark, [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 1))])
        tables.write_table(df, lakehouse, "bronze_fda_ai_list", mode="overwrite")
        bts.run(spark, lakehouse)

        silver = tables.read_table(spark, lakehouse, "silver_devices")
        assert silver.columns == [f.name for f in spark_schema_for(DeviceRecord).fields]

    def test_no_null_keys_or_dates(self, spark, lakehouse):
        """Phase 2 acceptance: no null submission_number or decision_date."""
        from pyspark.sql import functions as F

        from registry import tables

        df = self._rows(
            spark,
            [
                self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 1)),
                self._bronze_row("K2", "snapA", dt.datetime(2026, 9, 1)),
            ],
        )
        tables.write_table(df, lakehouse, "bronze_fda_ai_list", mode="overwrite")
        bts.run(spark, lakehouse)

        silver = tables.read_table(spark, lakehouse, "silver_devices")
        bad = silver.filter(
            F.col("submission_number").isNull() | F.col("decision_date").isNull()
        ).count()
        assert bad == 0

    def test_rerunning_replaces_rather_than_appends(self, spark, lakehouse):
        """Silver is derived state: a rebuild must not double it."""
        from registry import tables

        df = self._rows(spark, [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 1))])
        tables.write_table(df, lakehouse, "bronze_fda_ai_list", mode="overwrite")

        bts.run(spark, lakehouse)
        bts.run(spark, lakehouse)

        assert tables.read_table(spark, lakehouse, "silver_devices").count() == 1
