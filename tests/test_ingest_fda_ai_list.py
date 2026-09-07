"""FDA AI-enabled device list ingestion -- fixture-driven, no live network.

Fixtures are a real slice of the FDA export, captured 2026-09-06 (see ADR 0009
and CONTINUATION.md). The live endpoint is exercised only by the
``@pytest.mark.live_network`` test, which CI never runs (see pyproject markers).
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
        assert len(rows) == 14

    def test_maps_columns_onto_the_bronze_schema(self, sample_csv):
        rows = mod.parse_csv(sample_csv, snapshot_id="snap1", ingested_at=dt.datetime(2026, 1, 1))
        first = rows[0]
        assert first.submission_number == "K253628"
        assert first.device_name == "Auto-Seg (SO-0012), Spine Auto-Seg (SO-0012)"
        assert first.applicant_raw == "Agada Medical, Ltd."
        assert first.panel_raw == "Radiology"
        assert first.product_code == "QIH"

    def test_stamps_provenance_on_every_row(self, sample_csv):
        ts = dt.datetime(2026, 1, 1, 12, 30)
        rows = mod.parse_csv(sample_csv, snapshot_id="snap-abc", ingested_at=ts)
        assert all(r.ingested_at == ts and r.source_snapshot_id == "snap-abc" for r in rows)

    def test_keeps_the_raw_decision_date_unparsed(self, sample_csv):
        """Bronze preserves the source's own formatting; silver does the parsing."""
        rows = mod.parse_csv(sample_csv, snapshot_id="s", ingested_at=dt.datetime(2026, 1, 1))
        assert rows[0].decision_date_raw == "06/29/2026"

    def test_builds_a_source_url_from_the_submission_number(self, sample_csv):
        rows = mod.parse_csv(sample_csv, snapshot_id="s", ingested_at=dt.datetime(2026, 1, 1))
        by_num = {r.submission_number: r for r in rows}
        assert "cfpmn" in by_num["K253628"].source_url  # 510(k) database
        assert "denovo" in by_num["DEN250057"].source_url  # De Novo database
        assert "cfpma" in by_num["P950009"].source_url  # PMA database

    def test_preserves_the_pma_supplement_suffix(self, sample_csv):
        """Bronze keeps the raw submission number verbatim, suffix and all."""
        rows = mod.parse_csv(sample_csv, snapshot_id="s", ingested_at=dt.datetime(2026, 1, 1))
        nums = {r.submission_number for r in rows}
        assert "P130020/S005" in nums

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
        assert rows[0].submission_number == "K253628"
        assert rows[1].device_name == "ADAS 3D"

    def test_html_without_a_table_raises(self):
        with pytest.raises(mod.SourceFormatError):
            mod.parse_html(
                b"<html><body>no table</body></html>",
                snapshot_id="s",
                ingested_at=dt.datetime(2026, 1, 1),
            )


class TestCsvLinkDiscovery:
    def test_finds_the_download_a_csv_link(self, sample_html):
        url = mod._discover_csv_url(sample_html, "https://www.fda.gov/medical-devices/x")
        assert url == "https://www.fda.gov/media/178541/download?attachment"

    def test_resolves_relative_links_against_the_page(self):
        html = b'<a href="/media/999/download">Download a CSV File</a>'
        assert (
            mod._discover_csv_url(html, "https://www.fda.gov/x")
            == "https://www.fda.gov/media/999/download"
        )

    def test_returns_none_when_there_is_no_csv_link(self):
        html = b'<html><body><a href="/home">Home</a></body></html>'
        assert mod._discover_csv_url(html, "https://www.fda.gov/x") is None


class TestSnapshotId:
    def test_is_stable_for_identical_payloads(self):
        assert mod.snapshot_id(b"abc") == mod.snapshot_id(b"abc")

    def test_differs_when_the_source_changes(self):
        assert mod.snapshot_id(b"abc") != mod.snapshot_id(b"abd")


class TestFetch:
    @respx.mock
    def test_uses_the_known_csv_export_first(self, sample_csv):
        settings = Settings()
        route = respx.get(settings.fda_ai_list_csv_url).mock(
            return_value=httpx.Response(
                200, content=sample_csv, headers={"content-type": "text/csv"}
            )
        )
        snap = mod.fetch_raw(settings)
        assert route.called
        assert snap.content_kind == "csv"
        assert snap.url == settings.fda_ai_list_csv_url
        assert len(snap.parse()) == 14

    @respx.mock
    def test_discovers_the_csv_link_when_the_known_url_is_gone(self, sample_html, sample_csv):
        # Model a media-id change: the configured URL 404s, but the page still
        # advertises the real one, which the ingester scrapes and follows.
        settings = Settings(fda_ai_list_csv_url="https://www.fda.gov/media/000000/download")
        discovered = "https://www.fda.gov/media/178541/download?attachment"
        respx.get(settings.fda_ai_list_csv_url).mock(return_value=httpx.Response(404))
        respx.get(settings.fda_ai_list_url).mock(
            return_value=httpx.Response(
                200, content=sample_html, headers={"content-type": "text/html"}
            )
        )
        csv_route = respx.get(discovered).mock(
            return_value=httpx.Response(
                200, content=sample_csv, headers={"content-type": "text/csv"}
            )
        )
        snap = mod.fetch_raw(settings)
        assert csv_route.called
        assert snap.content_kind == "csv"
        assert snap.url == discovered
        assert len(snap.parse()) == 14

    @respx.mock
    def test_falls_back_to_html_when_no_csv_is_reachable(self, sample_html):
        # Known URL and the discovered link both 404; the page itself is parsed.
        settings = Settings(fda_ai_list_csv_url="https://www.fda.gov/media/000000/download")
        respx.get(settings.fda_ai_list_csv_url).mock(return_value=httpx.Response(404))
        respx.get(settings.fda_ai_list_url).mock(
            return_value=httpx.Response(
                200, content=sample_html, headers={"content-type": "text/html"}
            )
        )
        respx.get("https://www.fda.gov/media/178541/download?attachment").mock(
            return_value=httpx.Response(404)
        )
        snap = mod.fetch_raw(settings)
        assert snap.content_kind == "html"
        rows = snap.parse()
        assert len(rows) == 2
        assert rows[0].submission_number == "K253628"

    @respx.mock
    def test_retries_on_transient_server_errors(self, sample_csv):
        settings = Settings(http_backoff_seconds=0.0, http_max_retries=3)
        respx.get(settings.fda_ai_list_csv_url).mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, content=sample_csv, headers={"content-type": "text/csv"}),
            ]
        )
        assert mod.fetch_raw(settings).content_kind == "csv"

    @respx.mock
    def test_gives_up_when_every_source_fails(self):
        settings = Settings(http_backoff_seconds=0.0, http_max_retries=2)
        respx.get(settings.fda_ai_list_csv_url).mock(return_value=httpx.Response(404))
        respx.get(settings.fda_ai_list_url).mock(return_value=httpx.Response(503))
        with pytest.raises(mod.SourceUnavailableError):
            mod.fetch_raw(settings)


@pytest.mark.live_network
class TestLiveEndpoint:
    """Hits the real FDA site. Never runs in CI; run manually with

    ``uv run pytest -m live_network`` on a network that can reach fda.gov.
    """

    def test_fetches_the_real_list_as_csv(self):
        snap = mod.fetch_raw(Settings())
        rows = snap.parse()
        assert snap.content_kind == "csv"
        assert len(rows) > 1000  # public reporting puts the list at ~1,600
        assert all(r.submission_number for r in rows)


@pytest.mark.spark
class TestBronzeIngestion:
    def test_writes_rows_into_the_bronze_delta_table(self, spark, lakehouse, sample_csv):
        snap = mod.RawSnapshot(payload=sample_csv, content_kind="csv", url="http://x")
        written = mod.ingest_snapshot(spark, snap, settings=lakehouse)

        from registry import tables

        assert written == 14
        df = tables.read_table(spark, lakehouse, "bronze_fda_ai_list")
        assert df.count() == 14
        assert "ingested_at" in df.columns
        assert "source_snapshot_id" in df.columns

    def test_second_pull_appends_rather_than_overwrites(self, spark, lakehouse, sample_csv):
        """Bronze keeps the history of every pull, per the development plan."""
        snap = mod.RawSnapshot(payload=sample_csv, content_kind="csv", url="http://x")
        mod.ingest_snapshot(spark, snap, settings=lakehouse)
        mod.ingest_snapshot(spark, snap, settings=lakehouse)

        from registry import tables

        df = tables.read_table(spark, lakehouse, "bronze_fda_ai_list")
        assert df.count() == 28
        assert df.select("source_snapshot_id").distinct().count() == 1

    def test_schema_matches_the_bronze_model(self, spark, lakehouse, sample_csv):
        from registry import tables
        from registry.schemas import BronzeFdaAiListRecord, spark_schema_for

        snap = mod.RawSnapshot(payload=sample_csv, content_kind="csv", url="http://x")
        mod.ingest_snapshot(spark, snap, settings=lakehouse)

        df = tables.read_table(spark, lakehouse, "bronze_fda_ai_list")
        assert df.columns == [f.name for f in spark_schema_for(BronzeFdaAiListRecord).fields]
