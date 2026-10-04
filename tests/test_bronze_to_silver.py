"""bronze -> silver.

The roadmap's Issue 2 acceptance: one row per latest submission; pathway / class /
panel / company / specialty populated; schema parity via the generated StructType;
the join provably picks the latest pull; deterministic on fixtures.

Pure-Python row logic is tested without Spark; the Delta-level behaviour (latest
pull wins, schema parity) carries the `spark` marker.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

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

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_a_missing_company_stays_missing_rather_than_borrowing_the_device_name(
        self, silver_ctx, blank
    ):
        """Review nit on PR #8. `applicant_raw` is documented as "the name exactly as
        the FDA lists it"; falling back to the device name when the FDA omits the
        company writes a device name into an applicant field, and since
        `applicant_resolved` is None in that case too, no consumer can tell.

        The row is still kept -- an authorisation with no listed applicant is a real
        authorisation, and dropping it would understate the counts the trend report
        is built on.
        """
        rec = bts.build_device_record(self._bronze(applicant_raw=blank), silver_ctx)
        assert rec is not None, "a missing company must not drop the authorisation"
        assert rec.applicant_raw is None
        assert rec.applicant_resolved is None
        assert rec.device_name == "CaRi-Heart"

    def test_a_present_company_is_kept_verbatim(self, silver_ctx):
        rec = bts.build_device_record(
            self._bronze(applicant_raw="  Caristo Diagnostics Ltd  "), silver_ctx
        )
        assert rec.applicant_raw == "Caristo Diagnostics Ltd"

    def test_device_class_comes_from_the_enrichment_map_when_one_is_supplied(self, silver_ctx):
        """ADR 0013: build-silver stays offline -- it reads a map someone else
        fetched, it never calls openFDA itself."""
        ctx = replace(silver_ctx, device_classes={"K243456": "II"})
        rec = bts.build_device_record(self._bronze(), ctx)
        assert rec.device_class == "II"

    def test_a_submission_absent_from_the_map_stays_unenriched(self, silver_ctx):
        ctx = replace(silver_ctx, device_classes={"K999999": "III"})
        rec = bts.build_device_record(self._bronze(), ctx)
        assert rec.device_class is None, "absent must mean unknown, not a default"

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

    def test_the_row_carries_the_snapshot_it_was_read_from(self, silver_ctx):
        """ADR 0015 Decision 3: silver is self-identifying, row by row."""
        rec = bts.build_device_record(
            self._bronze(source_snapshot_id="b1efeb680371f44c"), silver_ctx
        )
        assert rec.source_snapshot_id == "b1efeb680371f44c"

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


class TestBuildStamp:
    """The commit-level stamp the rebuild gate compares (ADR 0015 Decision 4)."""

    def _record(self, silver_ctx, **overrides):
        row = {
            "submission_number": "K1",
            "device_name": "Device",
            "applicant_raw": "Aidoc Medical Ltd",
            "decision_date_raw": "11/01/2024",
            "panel_raw": "Radiology",
            "product_code": "QAS",
            "source_url": "https://example.org/K1",
            "ingested_at": dt.datetime(2026, 9, 1),
            "source_snapshot_id": "snapA",
        }
        row.update(overrides)
        return bts.build_device_record(row, silver_ctx)

    def test_content_hash_ignores_row_order(self, silver_ctx):
        a = self._record(silver_ctx, submission_number="K1")
        b = self._record(silver_ctx, submission_number="K2")
        assert bts.content_hash([a, b]) == bts.content_hash([b, a])

    def test_content_hash_sees_a_derived_field_change(self, silver_ctx):
        """The reason the gate cannot key on bronze alone: enrichment and the
        curated configs change silver without any new bronze snapshot."""
        plain = self._record(silver_ctx)
        enriched = plain.model_copy(update={"device_class": "II"})
        assert bts.content_hash([plain]) != bts.content_hash([enriched])

    def test_content_hash_is_a_sha256_hex_digest(self, silver_ctx):
        digest = bts.content_hash([self._record(silver_ctx)])
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")

    def test_stamp_round_trips_through_commit_metadata(self):
        stamp = bts.BuildStamp(source_snapshot_id="snapA", content_hash="ab" * 32)
        assert bts.BuildStamp.from_metadata(stamp.to_metadata()) == stamp

    @pytest.mark.parametrize(
        "raw", [None, "", "not json", "{}", '{"source_snapshot_id": "x"}', "[1]"]
    )
    def test_an_unreadable_stamp_is_none_so_the_gate_rebuilds(self, raw):
        """A silver version written before the stamp existed (or by hand) must
        never be mistaken for a current one."""
        assert bts.BuildStamp.from_metadata(raw) is None


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


@pytest.mark.spark
class TestSnapshotGate:
    """ADR 0015 Decisions 3-5: stamp, gate, retention.

    Synthetic by necessity: the live list has one distinct snapshot in bronze, so
    there is no real movement to build a fixture from (handoff §1).
    """

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

    def _pull(self, spark, settings, rows):
        """Append one pull to bronze, the way ingestion does."""
        from registry import tables
        from registry.schemas import BronzeFdaAiListRecord, spark_schema_for

        df = spark.createDataFrame(rows, schema=spark_schema_for(BronzeFdaAiListRecord))
        tables.write_table(df, settings, "bronze_fda_ai_list", mode="append")

    def _versions(self, spark, settings):
        from registry import tables

        return [h["version"] for h in tables.table_history(spark, settings, "silver_devices")]

    def _silver(self, spark, settings, version=None):
        from registry import tables

        df = tables.read_table(spark, settings, "silver_devices", version=version)
        return {r["submission_number"]: r.asDict() for r in df.collect()}

    def test_each_row_keeps_the_snapshot_it_was_read_from(self, spark, lakehouse):
        """K2 left the list at snapB, so its newest row is still snapA's. The row
        stamp is honest lineage, not the build's snapshot."""
        self._pull(
            spark,
            lakehouse,
            [
                self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 1)),
                self._bronze_row("K2", "snapA", dt.datetime(2026, 9, 1)),
            ],
        )
        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapB", dt.datetime(2026, 9, 8))])

        bts.run(spark, lakehouse)

        stamps = {k: v["source_snapshot_id"] for k, v in self._silver(spark, lakehouse).items()}
        assert stamps == {"K1": "snapB", "K2": "snapA"}

    def test_the_commit_is_stamped_with_the_latest_bronze_snapshot(self, spark, lakehouse):
        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 1))])
        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapB", dt.datetime(2026, 9, 8))])

        bts.run(spark, lakehouse)

        stamp = bts.current_stamp(spark, lakehouse)
        assert stamp is not None
        assert stamp.source_snapshot_id == "snapB"
        assert bts.latest_bronze_snapshot(spark, lakehouse) == "snapB"

    def test_a_rerun_over_the_same_bronze_writes_no_new_version(self, spark, lakehouse):
        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 1))])
        assert bts.run(spark, lakehouse) == 1
        before = self._versions(spark, lakehouse)

        assert bts.run(spark, lakehouse) == 0
        assert self._versions(spark, lakehouse) == before

    def test_a_new_pull_with_the_same_content_hash_writes_no_new_version(self, spark, lakehouse):
        """Today's live state: three pulls, one snapshot id. A build-event pairing
        would invent a diff here; the gate makes that impossible."""
        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 1))])
        bts.run(spark, lakehouse)
        before = self._versions(spark, lakehouse)

        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 8))])

        assert bts.run(spark, lakehouse) == 0
        assert self._versions(spark, lakehouse) == before

    def test_a_new_snapshot_rebuilds_and_restamps(self, spark, lakehouse):
        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 1))])
        bts.run(spark, lakehouse)
        before = self._versions(spark, lakehouse)

        self._pull(
            spark,
            lakehouse,
            [
                self._bronze_row("K1", "snapB", dt.datetime(2026, 9, 8)),
                self._bronze_row("K9", "snapB", dt.datetime(2026, 9, 8)),
            ],
        )

        assert bts.run(spark, lakehouse) == 2
        assert len(self._versions(spark, lakehouse)) == len(before) + 1
        assert bts.current_stamp(spark, lakehouse).source_snapshot_id == "snapB"

    def test_enrichment_landing_rebuilds_even_on_the_same_snapshot(self, spark, lakehouse):
        """The documented workflow is build-silver -> enrich-openfda -> build-silver.
        A gate keyed on bronze alone would skip the second build and device_class
        would never land; the content hash is what lets it through."""
        from registry import tables
        from registry.schemas import DeviceEnrichmentRecord, spark_schema_for

        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 1))])
        bts.run(spark, lakehouse)
        assert self._silver(spark, lakehouse)["K1"]["device_class"] is None

        enrichment_row = DeviceEnrichmentRecord(
            submission_number="K1",
            enriched_at=dt.datetime(2026, 9, 2),
            submission_found=False,
            classification_found=True,
            product_code="QAS",
            device_class_raw="2",
        )
        tables.write_table(
            spark.createDataFrame(
                [enrichment_row.model_dump()], spark_schema_for(DeviceEnrichmentRecord)
            ),
            lakehouse,
            "silver_device_enrichment",
            mode="overwrite",
        )

        assert bts.run(spark, lakehouse) == 1
        assert self._silver(spark, lakehouse)["K1"]["device_class"] == "II"
        assert bts.current_stamp(spark, lakehouse).source_snapshot_id == "snapA"

    def test_a_list_that_reverts_to_an_earlier_snapshot_rebuilds(self, spark, lakehouse):
        """A -> B -> A. Silver built at B must not be taken as current for A."""
        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 1))])
        self._pull(
            spark,
            lakehouse,
            [self._bronze_row("K1", "snapB", dt.datetime(2026, 9, 8), device_name="Renamed")],
        )
        bts.run(spark, lakehouse)

        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 15))])

        assert bts.run(spark, lakehouse) == 1
        assert bts.current_stamp(spark, lakehouse).source_snapshot_id == "snapA"
        assert self._silver(spark, lakehouse)["K1"]["device_name"] == "Device"

    def test_silver_from_before_the_stamp_is_rebuilt_and_gains_the_column(self, spark, lakehouse):
        """The live lakehouse holds an unstamped silver today; the first build
        under this code must replace it, not skip it."""
        from registry import tables

        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 1))])
        legacy = spark.createDataFrame(
            [("K1", "Device")], "submission_number string, device_name string"
        )
        tables.write_table(legacy, lakehouse, "silver_devices", mode="overwrite")
        assert bts.current_stamp(spark, lakehouse) is None

        assert bts.run(spark, lakehouse) == 1
        assert self._silver(spark, lakehouse)["K1"]["source_snapshot_id"] == "snapA"
        assert bts.current_stamp(spark, lakehouse).source_snapshot_id == "snapA"

    def test_retention_outlives_a_weekly_cadence(self, spark, lakehouse):
        """Decision 5: time travel is only as deep as Delta's retention."""
        from registry import tables

        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 1))])
        bts.run(spark, lakehouse)

        props = tables.table_properties(spark, lakehouse, "silver_devices")
        assert props["delta.logRetentionDuration"] == "interval 90 days"
        assert props["delta.deletedFileRetentionDuration"] == "interval 90 days"

    def test_retention_is_set_once_not_on_every_build(self, spark, lakehouse):
        from registry import tables

        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 1))])
        bts.run(spark, lakehouse)
        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapB", dt.datetime(2026, 9, 8))])
        bts.run(spark, lakehouse)

        ops = [h["operation"] for h in tables.table_history(spark, lakehouse, "silver_devices")]
        assert ops.count("SET TBLPROPERTIES") == 1

    def test_a_gated_build_still_restores_missing_retention(self, spark, lakehouse):
        """A build that wrote its rows but died before setting retention must not
        leave silver unguarded forever just because every later build is gated."""
        from registry import tables

        self._pull(spark, lakehouse, [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 1))])
        bts.run(spark, lakehouse)
        ref = lakehouse.table_ref("silver_devices")
        spark.sql(
            f"ALTER TABLE delta.`{ref}` UNSET TBLPROPERTIES "
            "('delta.logRetentionDuration', 'delta.deletedFileRetentionDuration')"
        )
        assert "delta.logRetentionDuration" not in tables.table_properties(
            spark, lakehouse, "silver_devices"
        )

        assert bts.run(spark, lakehouse) == 0

        props = tables.table_properties(spark, lakehouse, "silver_devices")
        assert props["delta.logRetentionDuration"] == "interval 90 days"
        assert props["delta.deletedFileRetentionDuration"] == "interval 90 days"

    def test_the_previous_build_is_readable_by_time_travel(self, spark, lakehouse):
        """Decision 2's read path, end to end: no temporary copy needed."""
        from registry import tables

        self._pull(
            spark,
            lakehouse,
            [self._bronze_row("K1", "snapA", dt.datetime(2026, 9, 1), device_name="Old")],
        )
        bts.run(spark, lakehouse)
        first_write = bts.current_stamp_version(spark, lakehouse)

        self._pull(
            spark,
            lakehouse,
            [self._bronze_row("K1", "snapB", dt.datetime(2026, 9, 8), device_name="New")],
        )
        bts.run(spark, lakehouse)

        assert self._silver(spark, lakehouse)["K1"]["device_name"] == "New"
        old = self._silver(spark, lakehouse, version=first_write)["K1"]
        assert old["device_name"] == "Old"
        assert old["source_snapshot_id"] == "snapA"
        history = tables.table_history(spark, lakehouse, "silver_devices")
        stamped = [h for h in history if h["version"] == first_write]
        assert bts.BuildStamp.from_metadata(stamped[0]["userMetadata"]).source_snapshot_id == (
            "snapA"
        )
