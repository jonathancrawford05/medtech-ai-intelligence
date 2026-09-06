"""FDA AI-enabled device list ingestion -- fixture-driven, no live network.

The live endpoint is exercised only by ``@pytest.mark.live_network`` tests, which
CI never runs (see pyproject markers).
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
import respx

from registry.config.settings import Settings
from registry.ingest import fda_ai_list as mod


@pytest.fixture
def sample_csv(fixtures_dir):
    return (fixtures_dir / "fda_ai_list_sample.csv").read_bytes()


@pytest.fixture
def messy_csv(fixtures_dir):
    return (fixtures_dir / "fda_ai_list_messy.csv").read_bytes()


@pytest.fixture
def sample_html(fixtures_dir):
    return (fixtures_dir / "fda_ai_list_sample.html").read_bytes()


class TestCsvParsing:
    def test_parses_all_rows(self, sample_csv):
        rows = mod.parse_csv(sample_csv, snapshot_id="snap1", ingested_at=dt.datetime(2026, 1, 1))
        assert len(rows) == 5

    def test_maps_columns_onto_the_bronze_schema(self, sample_csv):
        rows = mod.parse_csv(sample_csv, snapshot_id="snap1", ingested_at=dt.datetime(2026, 1, 1))
        first = rows[0]
        assert first.submission_number == "K243456"
        assert first.device_name == "CaRi-Heart"
        assert first.applicant_raw == "Caristo Diagnostics Ltd"
        assert first.panel_raw == "Radiology"
        assert first.product_code == "QIH"

    def test_stamps_provenance_on_every_row(self, sample_csv):
        ts = dt.datetime(2026, 1, 1, 12, 30)
        rows = mod.parse_csv(sample_csv, snapshot_id="snap-abc", ingested_at=ts)
        assert all(r.ingested_at == ts and r.source_snapshot_id == "snap-abc" for r in rows)

    def test_keeps_the_raw_decision_date_unparsed(self, sample_csv):
        """Bronze preserves the source's own formatting; silver does the parsing."""
        rows = mod.parse_csv(sample_csv, snapshot_id="s", ingested_at=dt.datetime(2026, 1, 1))
        assert rows[0].decision_date_raw == "11/01/2024"

    def test_builds_a_source_url_from_the_submission_number(self, sample_csv):
        rows = mod.parse_csv(sample_csv, snapshot_id="s", ingested_at=dt.datetime(2026, 1, 1))
        by_num = {r.submission_number: r for r in rows}
        assert "K243456" in by_num["K243456"].source_url
        assert "cfpmn" in by_num["K243456"].source_url  # 510(k) database
        assert "cfpma" in by_num["P230015"].source_url  # PMA database

    def test_tolerates_renamed_headers(self, messy_csv):
        """The FDA has renamed these columns before; aliases keep ingestion alive."""
        rows = mod.parse_csv(messy_csv, snapshot_id="s", ingested_at=dt.datetime(2026, 1, 1))
        assert rows[0].submission_number == "K243456"
        assert rows[0].device_name == "CaRi-Heart"

    def test_skips_blank_rows_and_rows_without_a_key(self, messy_csv):
        rows = mod.parse_csv(messy_csv, snapshot_id="s", ingested_at=dt.datetime(2026, 1, 1))
        assert all(r.submission_number for r in rows)
        assert "Missing Submission Number" not in [r.device_name for r in rows]

    def test_normalises_whitespace_and_case_in_the_key(self, messy_csv):
        rows = mod.parse_csv(messy_csv, snapshot_id="s", ingested_at=dt.datetime(2026, 1, 1))
        assert rows[0].submission_number == "K243456"
        assert rows[0].product_code == "QIH"

    def test_keeps_duplicate_submission_numbers_in_bronze(self, messy_csv):
        """Bronze records what the source said; dedupe is a silver concern."""
        rows = mod.parse_csv(messy_csv, snapshot_id="s", ingested_at=dt.datetime(2026, 1, 1))
        assert [r.submission_number for r in rows].count("K243456") == 2

    def test_unrecognised_header_set_raises(self):
        with pytest.raises(mod.SourceFormatError, match="submission number"):
            mod.parse_csv(b"Foo,Bar\n1,2\n", snapshot_id="s", ingested_at=dt.datetime(2026, 1, 1))

    def test_empty_payload_raises(self):
        with pytest.raises(mod.SourceFormatError):
            mod.parse_csv(b"", snapshot_id="s", ingested_at=dt.datetime(2026, 1, 1))


class TestHtmlParsing:
    def test_parses_the_html_table_fallback(self, sample_html):
        rows = mod.parse_html(sample_html, snapshot_id="s", ingested_at=dt.datetime(2026, 1, 1))
        assert len(rows) == 2
        assert rows[0].submission_number == "K243456"
        assert rows[1].device_name == "AVIEW CAC"

    def test_html_without_a_table_raises(self):
        with pytest.raises(mod.SourceFormatError):
            mod.parse_html(
                b"<html><body>no table</body></html>",
                snapshot_id="s",
                ingested_at=dt.datetime(2026, 1, 1),
            )


class TestSnapshotId:
    def test_is_stable_for_identical_payloads(self):
        assert mod.snapshot_id(b"abc") == mod.snapshot_id(b"abc")

    def test_differs_when_the_source_changes(self):
        assert mod.snapshot_id(b"abc") != mod.snapshot_id(b"abd")


class TestFetch:
    @respx.mock
    def test_prefers_the_csv_export(self, sample_csv):
        settings = Settings()
        route = respx.get(url__startswith=settings.fda_ai_list_url).mock(
            return_value=httpx.Response(
                200, content=sample_csv, headers={"content-type": "text/csv"}
            )
        )
        snap = mod.fetch_raw(settings)
        assert route.called
        assert snap.content_kind == "csv"
        assert len(snap.parse()) == 5

    @respx.mock
    def test_falls_back_to_html_when_the_csv_export_is_gone(self, sample_html):
        settings = Settings()

        def responder(request):
            if "export" in str(request.url) or "csv" in str(request.url).lower():
                return httpx.Response(404)
            return httpx.Response(200, content=sample_html, headers={"content-type": "text/html"})

        respx.get(url__startswith=settings.fda_ai_list_url).mock(side_effect=responder)
        snap = mod.fetch_raw(settings)
        assert snap.content_kind == "html"
        assert len(snap.parse()) == 2

    @respx.mock
    def test_retries_on_transient_server_errors(self, sample_csv):
        settings = Settings(http_backoff_seconds=0.0, http_max_retries=3)
        responses = [
            httpx.Response(503),
            httpx.Response(200, content=sample_csv, headers={"content-type": "text/csv"}),
        ]
        respx.get(url__startswith=settings.fda_ai_list_url).mock(side_effect=responses)
        assert mod.fetch_raw(settings).content_kind == "csv"

    @respx.mock
    def test_gives_up_after_max_retries(self):
        settings = Settings(http_backoff_seconds=0.0, http_max_retries=2)
        respx.get(url__startswith=settings.fda_ai_list_url).mock(return_value=httpx.Response(503))
        with pytest.raises(mod.SourceUnavailableError):
            mod.fetch_raw(settings)


@pytest.mark.spark
class TestBronzeIngestion:
    def test_writes_rows_into_the_bronze_delta_table(self, spark, lakehouse, sample_csv):
        snap = mod.RawSnapshot(payload=sample_csv, content_kind="csv", url="http://x")
        written = mod.ingest_snapshot(spark, snap, settings=lakehouse)

        from registry import tables

        assert written == 5
        df = tables.read_table(spark, lakehouse, "bronze_fda_ai_list")
        assert df.count() == 5
        assert "ingested_at" in df.columns
        assert "source_snapshot_id" in df.columns

    def test_second_pull_appends_rather_than_overwrites(self, spark, lakehouse, sample_csv):
        """Bronze keeps the history of every pull, per the development plan."""
        snap = mod.RawSnapshot(payload=sample_csv, content_kind="csv", url="http://x")
        mod.ingest_snapshot(spark, snap, settings=lakehouse)
        mod.ingest_snapshot(spark, snap, settings=lakehouse)

        from registry import tables

        df = tables.read_table(spark, lakehouse, "bronze_fda_ai_list")
        assert df.count() == 10
        assert df.select("source_snapshot_id").distinct().count() == 1

    def test_schema_matches_the_bronze_model(self, spark, lakehouse, sample_csv):
        from registry import tables
        from registry.schemas import BronzeFdaAiListRecord, spark_schema_for

        snap = mod.RawSnapshot(payload=sample_csv, content_kind="csv", url="http://x")
        mod.ingest_snapshot(spark, snap, settings=lakehouse)

        df = tables.read_table(spark, lakehouse, "bronze_fda_ai_list")
        assert df.columns == [f.name for f in spark_schema_for(BronzeFdaAiListRecord).fields]
