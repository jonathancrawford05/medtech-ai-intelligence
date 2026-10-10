"""510(k) Summary acquisition (Issue 4, PR 4A; ADR 0018).

Three layers, from cheapest to most real:

- **Mechanics** -- URL candidates, the text-vs-image rule, target selection, the
  fetcher's throttle / fallback / stop-on-429-403 behaviour. HTTP is mocked with
  ``respx``; PDFs are built in memory by ``tests/pdf_builder.py`` (synthetic, for
  mechanics only -- say so, per CLAUDE.md).
- **Bronze** -- the append-only write, resume and content-hash dedupe (``spark``).
- **Real text** -- the recorded slice under ``tests/fixtures/summary_documents/``.
  ``accessdata.fda.gov`` is blocked from agent sessions and CI, so that slice is
  *recorded* by the ``live_network`` test at the bottom, on a host that can reach
  it (``uv run pytest -m live_network -k summary``), and committed. The offline
  assertions over it skip, naming the missing file, until it is.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path

import httpx
import pypdf
import pytest
import respx

from registry.config.settings import Settings
from registry.ingest import summary_documents as mod
from registry.schemas import BronzeSummaryDocumentRecord, spark_schema_for
from tests.pdf_builder import make_pdf

BASE = "https://www.accessdata.fda.gov/cdrh_docs"
LONG = "Indications for Use. " * 10  # comfortably over the 100-character page floor
PDF_HEADERS = {"content-type": "application/pdf"}

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "summary_documents"


def _settings(**overrides) -> Settings:
    # No throttle wait and no backoff sleep: tests must not take wall-clock time.
    base = {"summary_request_interval_seconds": 0.0, "http_backoff_seconds": 0.0}
    base.update(overrides)
    return Settings(**base)


def _fetcher(**overrides) -> mod.SummaryFetcher:
    return mod.SummaryFetcher(_settings(**overrides))


# ---------------------------------------------------------------------------
# URL candidates (finding 0011: pdf{int(yy)}, then bare pdf/)
# ---------------------------------------------------------------------------


class TestCandidateUrls:
    def test_recent_filing_uses_the_two_digit_year(self):
        assert mod.candidate_urls("K253628", BASE) == [
            f"{BASE}/pdf25/K253628.pdf",
            f"{BASE}/pdf/K253628.pdf",
        ]

    def test_the_year_directory_has_no_leading_zero(self):
        """K093456 lives under pdf9/, not pdf09/ -- the latter 404s (finding 0011)."""
        assert mod.candidate_urls("K093456", BASE)[0] == f"{BASE}/pdf9/K093456.pdf"

    def test_the_oldest_filings_fall_back_to_bare_pdf(self):
        assert mod.candidate_urls("K003301", BASE)[-1] == f"{BASE}/pdf/K003301.pdf"

    def test_input_is_normalised(self):
        assert mod.candidate_urls(" k253628 ", BASE)[0] == f"{BASE}/pdf25/K253628.pdf"

    def test_a_trailing_slash_on_the_base_does_not_double_up(self):
        assert mod.candidate_urls("K253628", BASE + "/")[0] == f"{BASE}/pdf25/K253628.pdf"

    @pytest.mark.parametrize("number", ["DEN250057", "P130020", "P130020/S005", "K12", "X123456"])
    def test_non_510k_numbers_are_not_fetchable(self, number):
        """De Novo and PMA documents live elsewhere and are deferred (ADR 0018)."""
        assert mod.is_fetchable(number) is False
        with pytest.raises(ValueError, match="deferred"):
            mod.candidate_urls(number, BASE)


# ---------------------------------------------------------------------------
# Text measurement (finding 0011's rule)
# ---------------------------------------------------------------------------


class TestCharCount:
    def test_whitespace_is_not_counted(self):
        assert mod.char_count(" a b\n\tc ") == 3

    def test_empty_page(self):
        assert mod.char_count("") == 0


class TestClassifyPages:
    """A page under 100 non-whitespace characters is an image page."""

    def test_all_pages_text(self):
        assert mod.classify_pages([2040, 1395, 100]) == "text"

    def test_no_text_pages_is_image(self):
        assert mod.classify_pages([0, 0, 0, 0, 0, 0, 0]) == "image"  # K003301's profile

    def test_some_text_pages_is_mixed(self):
        assert mod.classify_pages([2257, 1814, 0, 1674]) == "mixed"  # K203469's profile

    def test_the_floor_is_exclusive_below_100(self):
        """K182875's 93-character page made it `mixed` in the spike."""
        assert mod.classify_pages([2344, 93]) == "mixed"
        assert mod.classify_pages([99]) == "image"
        assert mod.classify_pages([100]) == "text"

    def test_no_pages_has_no_class(self):
        assert mod.classify_pages([]) is None


class TestExtractPages:
    def test_one_string_per_page_in_order(self):
        pages = mod.extract_pages(make_pdf(["First page K181892", "Second page"]))
        assert len(pages) == 2
        assert "K181892" in pages[0]
        assert "Second" in pages[1]

    def test_a_page_with_no_text_layer_is_empty(self):
        pages = mod.extract_pages(make_pdf([LONG, ""]))
        assert mod.char_count(pages[1]) == 0

    def test_garbage_raises_a_pdf_error(self):
        with pytest.raises(mod.PdfExtractionError):
            mod.extract_pages(b"<html>not a pdf</html>")

    def test_one_unreadable_page_does_not_lose_the_document(self, broken_page):
        """A page pypdf chokes on reads as empty; its neighbours keep their text."""
        pages = mod.extract_pages(make_pdf([LONG, "BROKEN page", "Third page K250177"]))
        assert len(pages) == 3
        assert pages[0].startswith("Indications")
        assert pages[1] == ""
        assert "K250177" in pages[2]

    def test_the_unreadable_pages_are_reported(self, broken_page):
        result = mod.read_pages(make_pdf(["BROKEN one", LONG, "BROKEN three"]))
        assert result.unreadable_pages == [1, 3]
        assert result.texts[1].startswith("Indications")


@pytest.fixture
def broken_page(monkeypatch):
    """Make pypdf raise on any page whose text contains BROKEN -- the per-page
    failure mode (a malformed content stream or font) without a corrupt real file."""
    original = pypdf.PageObject.extract_text

    def extract_text(self, *args, **kwargs):
        text = original(self, *args, **kwargs)
        if "BROKEN" in text:
            raise KeyError("/Font")
        return text

    monkeypatch.setattr(pypdf.PageObject, "extract_text", extract_text)


# ---------------------------------------------------------------------------
# The bronze record (ADR 0008: schema generated from the model)
# ---------------------------------------------------------------------------


def _record(**overrides) -> BronzeSummaryDocumentRecord:
    base = {
        "submission_number": "K181892",
        "url": f"{BASE}/pdf18/K181892.pdf",
        "urls_tried": [f"{BASE}/pdf18/K181892.pdf"],
        "http_status": 200,
        "content_type": "application/pdf",
        "content_sha256": "a" * 64,
        "byte_count": 1234,
        "page_count": 2,
        "page_texts": [LONG, ""],
        "page_char_counts": [mod.char_count(LONG), 0],
        "text_class": "mixed",
        "extractor": "pypdf==test",
        "fetched_at": dt.datetime(2026, 10, 10, 9, 0),
        "ingested_at": dt.datetime(2026, 10, 10, 9, 5),
        "source_snapshot_id": "a" * 16,
    }
    base.update(overrides)
    return BronzeSummaryDocumentRecord(**base)


class TestBronzeRecord:
    def test_a_valid_record(self):
        rec = _record()
        assert rec.page_count == 2
        assert rec.extraction_error is None

    def test_submission_number_is_normalised(self):
        assert _record(submission_number=" k181892 ").submission_number == "K181892"

    def test_page_arrays_must_match_the_page_count(self):
        """Parallel arrays are only safe if their lengths cannot drift apart."""
        with pytest.raises(ValueError, match="page"):
            _record(page_count=3)
        with pytest.raises(ValueError, match="page"):
            _record(page_char_counts=[1])

    def test_a_text_class_needs_a_document(self):
        """A class with no content hash would claim to describe bytes nobody has."""
        with pytest.raises(ValueError, match="content_sha256"):
            _record(content_sha256=None)

    def test_a_miss_is_representable(self):
        rec = _record(
            url=f"{BASE}/pdf/K181892.pdf",
            http_status=404,
            content_type=None,
            content_sha256=None,
            byte_count=None,
            page_count=0,
            page_texts=[],
            page_char_counts=[],
            text_class=None,
            extractor=None,
            source_snapshot_id=None,
        )
        assert rec.text_class is None

    def test_unknown_text_class_is_rejected(self):
        with pytest.raises(ValueError):
            _record(text_class="scanned")

    def test_the_spark_mirror_is_generated(self):
        from pyspark.sql.types import ArrayType, LongType, StringType, TimestampType

        fields = {f.name: f for f in spark_schema_for(BronzeSummaryDocumentRecord).fields}
        assert list(fields) == list(BronzeSummaryDocumentRecord.model_fields)
        assert isinstance(fields["page_texts"].dataType, ArrayType)
        assert isinstance(fields["page_texts"].dataType.elementType, StringType)
        assert isinstance(fields["page_char_counts"].dataType.elementType, LongType)
        assert isinstance(fields["fetched_at"].dataType, TimestampType)
        assert fields["content_sha256"].nullable is True
        assert fields["submission_number"].nullable is False
        assert fields["ingested_at"].nullable is False

    def test_the_table_is_registered(self):
        from registry.schemas import TABLE_MODELS

        assert TABLE_MODELS[mod.BRONZE_TABLE] is BronzeSummaryDocumentRecord
        assert mod.BRONZE_TABLE == "bronze_summary_documents"


# ---------------------------------------------------------------------------
# The fetcher
# ---------------------------------------------------------------------------


class TestFetcher:
    @respx.mock
    def test_a_summary_is_fetched_hashed_and_split_into_pages(self):
        pdf = make_pdf([LONG, ""])
        respx.get(f"{BASE}/pdf18/K181892.pdf").mock(
            return_value=httpx.Response(200, content=pdf, headers=PDF_HEADERS)
        )
        with _fetcher() as fetcher:
            doc = fetcher.fetch("K181892")

        assert doc.found is True
        assert doc.http_status == 200
        assert doc.url == f"{BASE}/pdf18/K181892.pdf"
        assert doc.urls_tried == [f"{BASE}/pdf18/K181892.pdf"]
        assert doc.content_sha256 == hashlib.sha256(pdf).hexdigest()
        assert doc.byte_count == len(pdf)
        assert doc.page_count == 2
        assert doc.page_char_counts == [mod.char_count(LONG), 0]
        assert doc.text_class == "mixed"
        assert doc.extractor == mod.EXTRACTOR
        assert doc.extraction_error is None

    @respx.mock
    def test_falls_back_to_bare_pdf_when_the_year_directory_404s(self):
        first = respx.get(f"{BASE}/pdf0/K003301.pdf").mock(return_value=httpx.Response(404))
        respx.get(f"{BASE}/pdf/K003301.pdf").mock(
            return_value=httpx.Response(200, content=make_pdf(["", ""]), headers=PDF_HEADERS)
        )
        doc = _fetcher().fetch("K003301")

        assert first.called
        assert doc.url == f"{BASE}/pdf/K003301.pdf"
        assert doc.urls_tried == [f"{BASE}/pdf0/K003301.pdf", f"{BASE}/pdf/K003301.pdf"]
        assert doc.text_class == "image"

    @respx.mock
    def test_a_document_on_neither_candidate_is_a_recorded_miss(self):
        respx.get(url__regex=r".*/K999999\.pdf").mock(return_value=httpx.Response(404))
        doc = _fetcher().fetch("K999999")

        assert doc.found is False
        assert doc.http_status == 404
        assert doc.url == f"{BASE}/pdf/K999999.pdf"  # the last URL tried
        assert len(doc.urls_tried) == 2
        assert doc.content_sha256 is None
        assert doc.page_count == 0
        assert doc.text_class is None

    @respx.mock
    def test_content_type_not_the_url_decides_it_is_a_pdf(self):
        """An HTML page served with 200 at a .pdf URL is not a Summary (ADR 0009's rule)."""
        respx.get(f"{BASE}/pdf18/K181892.pdf").mock(
            return_value=httpx.Response(
                200, content=b"<html>moved</html>", headers={"content-type": "text/html"}
            )
        )
        bare = respx.get(f"{BASE}/pdf/K181892.pdf").mock(
            return_value=httpx.Response(200, content=make_pdf([LONG]), headers=PDF_HEADERS)
        )
        doc = _fetcher().fetch("K181892")
        assert bare.called
        assert doc.found is True
        assert doc.url == f"{BASE}/pdf/K181892.pdf"

    @respx.mock
    def test_pdf_magic_bytes_count_when_the_content_type_is_generic(self):
        respx.get(f"{BASE}/pdf18/K181892.pdf").mock(
            return_value=httpx.Response(
                200,
                content=make_pdf([LONG]),
                headers={"content-type": "application/octet-stream"},
            )
        )
        assert _fetcher().fetch("K181892").found is True

    @respx.mock
    def test_an_unparseable_pdf_is_kept_with_its_hash_and_the_error(self):
        """The bytes arrived: record that fact, and why no text came out."""
        body = b"%PDF-1.4\n garbage with no xref"
        respx.get(f"{BASE}/pdf18/K181892.pdf").mock(
            return_value=httpx.Response(200, content=body, headers=PDF_HEADERS)
        )
        doc = _fetcher().fetch("K181892")
        assert doc.found is True
        assert doc.content_sha256 == hashlib.sha256(body).hexdigest()
        assert doc.page_count == 0
        assert doc.text_class is None
        assert doc.extraction_error

    @respx.mock
    def test_an_unreadable_page_is_kept_empty_and_named(self, broken_page):
        respx.get(f"{BASE}/pdf18/K181892.pdf").mock(
            return_value=httpx.Response(
                200, content=make_pdf([LONG, "BROKEN", LONG]), headers=PDF_HEADERS
            )
        )
        doc = _fetcher().fetch("K181892")
        assert doc.page_count == 3
        assert doc.page_char_counts[1] == 0
        assert doc.text_class == "mixed"
        assert doc.extraction_error is not None and "page 2" in doc.extraction_error

    @pytest.mark.parametrize("status", [429, 403])
    @respx.mock
    def test_rate_limit_or_forbidden_halts_without_retrying(self, status):
        """Be a good citizen: a 429/403 stops the whole pass (handoff §1)."""
        route = respx.get(f"{BASE}/pdf18/K181892.pdf").mock(return_value=httpx.Response(status))
        with pytest.raises(mod.FetchHaltedError) as info:
            _fetcher(http_max_retries=4).fetch("K181892")
        assert route.call_count == 1
        assert info.value.status == status

    @respx.mock
    def test_server_errors_are_retried_then_succeed(self):
        route = respx.get(f"{BASE}/pdf18/K181892.pdf").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, content=make_pdf([LONG]), headers=PDF_HEADERS),
            ]
        )
        doc = _fetcher(http_max_retries=3).fetch("K181892")
        assert route.call_count == 2
        assert doc.found is True

    @respx.mock
    def test_exhausted_retries_raise_unavailable(self):
        respx.get(f"{BASE}/pdf18/K181892.pdf").mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(mod.SummarySourceUnavailableError):
            _fetcher(http_max_retries=2).fetch("K181892")

    @respx.mock
    def test_the_user_agent_comes_from_settings(self):
        route = respx.get(f"{BASE}/pdf18/K181892.pdf").mock(
            return_value=httpx.Response(200, content=make_pdf([LONG]), headers=PDF_HEADERS)
        )
        _fetcher(http_user_agent="registry-test/1.0").fetch("K181892")
        assert route.calls.last.request.headers["user-agent"] == "registry-test/1.0"

    @respx.mock
    def test_the_host_comes_from_settings(self):
        """No hard-coded host in pipeline code: the base URL is a setting."""
        route = respx.get("https://mirror.example/docs/pdf18/K181892.pdf").mock(
            return_value=httpx.Response(200, content=make_pdf([LONG]), headers=PDF_HEADERS)
        )
        _fetcher(summary_documents_base_url="https://mirror.example/docs").fetch("K181892")
        assert route.called

    def test_a_deferred_number_is_refused_before_any_request(self):
        with pytest.raises(ValueError, match="deferred"):
            _fetcher().fetch("DEN250057")


class TestThrottle:
    """~1 request/second across *every* request, fallbacks included."""

    @respx.mock
    def test_requests_are_spaced_by_the_interval(self):
        respx.get(url__regex=r".*/K181892\.pdf").mock(return_value=httpx.Response(404))
        now = [100.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        fetcher = mod.SummaryFetcher(
            _settings(summary_request_interval_seconds=1.0),
            sleep=sleep,
            clock=lambda: now[0],
        )
        fetcher.fetch("K181892")  # two candidate requests

        # The first request goes immediately; the second waits out the interval.
        assert sleeps == [pytest.approx(1.0)]

    @respx.mock
    def test_time_already_elapsed_counts_toward_the_interval(self):
        respx.get(url__regex=r".*/K181892\.pdf").mock(return_value=httpx.Response(404))
        now = [100.0]
        sleeps: list[float] = []

        def clock() -> float:
            now[0] += 0.4  # each request "takes" 0.4s
            return now[0]

        fetcher = mod.SummaryFetcher(
            _settings(summary_request_interval_seconds=1.0), sleep=sleeps.append, clock=clock
        )
        fetcher.fetch("K181892")
        assert len(sleeps) == 1
        assert 0.0 < sleeps[0] < 1.0


class TestPdfArchive:
    """ADR 0018: PDF bytes optionally kept on disk by sha256 -- never in git or Delta."""

    @respx.mock
    def test_bytes_are_archived_by_hash_when_configured(self, tmp_path):
        pdf = make_pdf([LONG])
        respx.get(f"{BASE}/pdf18/K181892.pdf").mock(
            return_value=httpx.Response(200, content=pdf, headers=PDF_HEADERS)
        )
        archive = tmp_path / "pdfs"
        doc = _fetcher(summary_pdf_archive_dir=str(archive)).fetch("K181892")

        stored = archive / f"{doc.content_sha256}.pdf"
        assert stored.read_bytes() == pdf
        assert [p.name for p in archive.iterdir()] == [stored.name]  # no temp file left

    @respx.mock
    def test_no_archive_by_default(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        respx.get(f"{BASE}/pdf18/K181892.pdf").mock(
            return_value=httpx.Response(200, content=make_pdf([LONG]), headers=PDF_HEADERS)
        )
        _fetcher().fetch("K181892")
        assert list(tmp_path.iterdir()) == []

    @respx.mock
    def test_an_unwritable_archive_does_not_fail_the_fetch(self, tmp_path):
        blocked = tmp_path / "not_a_dir"
        blocked.write_text("")
        respx.get(f"{BASE}/pdf18/K181892.pdf").mock(
            return_value=httpx.Response(200, content=make_pdf([LONG]), headers=PDF_HEADERS)
        )
        assert _fetcher(summary_pdf_archive_dir=str(blocked)).fetch("K181892").found


# ---------------------------------------------------------------------------
# Target selection
# ---------------------------------------------------------------------------


def _enriched(number: str, kind: str | None = "Summary") -> dict:
    return {"submission_number": number, "statement_or_summary": kind}


_ROWS = (
    _enriched("K250177"),
    _enriched("K181892"),
    _enriched("K230001", "Statement"),
    _enriched("K230002", None),
    _enriched("DEN250057"),
    _enriched("P130020/S005"),
    _enriched("K003301", " summary "),
)


class TestSelectTargets:
    ROWS = _ROWS

    def test_only_summary_510ks_are_targets_in_a_stable_order(self):
        plan = mod.select_targets(self.ROWS, already_fetched=set())
        assert plan.targets == ["K003301", "K181892", "K250177"]

    def test_summary_de_novo_and_pma_are_reported_as_deferred(self):
        plan = mod.select_targets(self.ROWS, already_fetched=set())
        assert plan.deferred == ["DEN250057", "P130020/S005"]

    def test_already_fetched_documents_are_skipped(self):
        """Resumable: a re-run picks up where the last one stopped."""
        plan = mod.select_targets(self.ROWS, already_fetched={"K181892"})
        assert plan.targets == ["K003301", "K250177"]
        assert plan.already_fetched == 1

    def test_limit_caps_the_targets(self):
        plan = mod.select_targets(self.ROWS, already_fetched=set(), limit=2)
        assert plan.targets == ["K003301", "K181892"]

    def test_only_names_exact_documents_and_refetches(self):
        """`--only` is a deliberate request: fetched or not, Summary-flagged or not."""
        plan = mod.select_targets(
            self.ROWS, already_fetched={"K181892"}, only=["k181892", "K999999", "K181892"]
        )
        assert plan.targets == ["K181892", "K999999"]

    def test_only_still_defers_unfetchable_numbers(self):
        plan = mod.select_targets([], already_fetched=set(), only=["DEN250057", "K181892"])
        assert plan.targets == ["K181892"]
        assert plan.deferred == ["DEN250057"]


# ---------------------------------------------------------------------------
# The pass: fetch -> bronze (pure orchestration, Spark faked out)
# ---------------------------------------------------------------------------


class _FakeFetcher:
    """Returns canned documents; raises a canned error for a named submission."""

    def __init__(self, raise_on: dict[str, Exception] | None = None):
        self.calls: list[str] = []
        self.raise_on = raise_on or {}

    def fetch(self, submission_number: str) -> mod.SummaryDocument:
        self.calls.append(submission_number)
        if submission_number in self.raise_on:
            raise self.raise_on[submission_number]
        body = f"pdf-bytes-{submission_number}".encode()
        return mod.SummaryDocument(
            submission_number=submission_number,
            url=f"{BASE}/pdf/{submission_number}.pdf",
            urls_tried=[f"{BASE}/pdf/{submission_number}.pdf"],
            http_status=200,
            fetched_at=dt.datetime(2026, 10, 10, 9, 0),
            content_type="application/pdf",
            content_sha256=hashlib.sha256(body).hexdigest(),
            byte_count=len(body),
            page_texts=[LONG],
            extractor="fake",
        )


class TestFetchDocuments:
    def test_writes_in_batches(self):
        batches: list[list[BronzeSummaryDocumentRecord]] = []
        result = mod.fetch_documents(
            ["K1", "K2", "K3"],
            _FakeFetcher(),
            write=batches.append,
            batch_size=2,
            existing_hashes=set(),
        )
        assert [len(b) for b in batches] == [2, 1]
        assert result.written == 3
        assert result.found == 3
        assert result.text_classes == {"text": 3}
        assert result.halted is None

    def test_a_halt_flushes_what_was_fetched_then_stops(self):
        """Nothing fetched before a 429 is lost, and nothing after it is requested."""
        batches: list[list[BronzeSummaryDocumentRecord]] = []
        fetcher = _FakeFetcher(raise_on={"K2": mod.FetchHaltedError(429, "u")})
        result = mod.fetch_documents(
            ["K1", "K2", "K3"],
            fetcher,
            write=batches.append,
            batch_size=50,
            existing_hashes=set(),
        )
        assert fetcher.calls == ["K1", "K2"]
        assert [r.submission_number for b in batches for r in b] == ["K1"]
        assert result.halted is not None and "429" in result.halted

    def test_unavailable_also_halts_after_flushing(self):
        batches: list[list[BronzeSummaryDocumentRecord]] = []
        fetcher = _FakeFetcher(raise_on={"K2": mod.SummarySourceUnavailableError("down")})
        result = mod.fetch_documents(
            ["K1", "K2"], fetcher, write=batches.append, batch_size=50, existing_hashes=set()
        )
        assert result.written == 1
        assert result.halted is not None

    def test_an_unchanged_document_is_not_appended_twice(self):
        """Dedupe is by submission + content hash: same bytes, no new row."""
        fake = _FakeFetcher()
        sha = fake.fetch("K1").content_sha256
        batches: list[list[BronzeSummaryDocumentRecord]] = []
        result = mod.fetch_documents(
            ["K1", "K2"],
            _FakeFetcher(),
            write=batches.append,
            batch_size=50,
            existing_hashes={("K1", sha)},
        )
        assert [r.submission_number for b in batches for r in b] == ["K2"]
        assert result.unchanged == 1
        assert result.written == 1

    def test_rows_are_stamped_for_bronze(self):
        batches: list[list[BronzeSummaryDocumentRecord]] = []
        when = dt.datetime(2026, 10, 10, 12, 0)
        mod.fetch_documents(
            ["K1"],
            _FakeFetcher(),
            write=batches.append,
            batch_size=50,
            existing_hashes=set(),
            now=lambda: when,
        )
        row = batches[0][0]
        assert row.ingested_at == when
        assert row.fetched_at == dt.datetime(2026, 10, 10, 9, 0)
        assert row.source_snapshot_id == row.content_sha256[:16]

    def test_misses_are_written_and_counted(self):
        class Missing(_FakeFetcher):
            def fetch(self, submission_number):
                return mod.SummaryDocument(
                    submission_number=submission_number,
                    url=f"{BASE}/pdf/{submission_number}.pdf",
                    urls_tried=[f"{BASE}/pdf/{submission_number}.pdf"],
                    http_status=404,
                    fetched_at=dt.datetime(2026, 10, 10, 9, 0),
                )

        batches: list[list[BronzeSummaryDocumentRecord]] = []
        result = mod.fetch_documents(
            ["K1"], Missing(), write=batches.append, batch_size=50, existing_hashes=set()
        )
        assert result.not_found == 1
        assert result.found == 0
        assert batches[0][0].http_status == 404


# ---------------------------------------------------------------------------
# Spark: the append-only bronze table, end to end with a faked network
# ---------------------------------------------------------------------------


@pytest.mark.spark
class TestRunAgainstBronze:
    def _seed_enrichment(self, spark, settings, rows):
        from registry import tables
        from registry.schemas import DeviceEnrichmentRecord

        records = [
            DeviceEnrichmentRecord(
                submission_number=r["submission_number"],
                enriched_at=dt.datetime(2026, 10, 1),
                submission_found=True,
                statement_or_summary=r["statement_or_summary"],
                classification_found=False,
            ).model_dump()
            for r in rows
        ]
        df = spark.createDataFrame(records, spark_schema_for(DeviceEnrichmentRecord))
        tables.write_table(df, settings, "silver_device_enrichment", mode="overwrite")

    def test_appends_and_resumes_without_refetching(self, spark, lakehouse):
        from registry import tables

        self._seed_enrichment(
            spark,
            lakehouse,
            [_enriched("K181892"), _enriched("K250177"), _enriched("K230001", "Statement")],
        )
        first = _FakeFetcher()
        result = mod.run(spark, lakehouse, fetcher=first, limit=1)
        assert first.calls == ["K181892"]
        assert result.written == 1

        second = _FakeFetcher()
        result = mod.run(spark, lakehouse, fetcher=second)
        assert second.calls == ["K250177"]  # K181892 is already in bronze

        bronze = tables.read_table(spark, lakehouse, mod.BRONZE_TABLE)
        assert sorted(r["submission_number"] for r in bronze.collect()) == ["K181892", "K250177"]
        assert [
            h["operation"] for h in tables.table_history(spark, lakehouse, mod.BRONZE_TABLE)
        ] == [
            "WRITE",
            "WRITE",
        ]

    def test_only_refetches_but_an_unchanged_hash_is_not_duplicated(self, spark, lakehouse):
        from registry import tables

        self._seed_enrichment(spark, lakehouse, [_enriched("K181892")])
        mod.run(spark, lakehouse, fetcher=_FakeFetcher())

        again = _FakeFetcher()
        result = mod.run(spark, lakehouse, fetcher=again, only=["K181892"])
        assert again.calls == ["K181892"]
        assert result.unchanged == 1
        assert tables.read_table(spark, lakehouse, mod.BRONZE_TABLE).count() == 1

    def test_page_text_round_trips_through_delta(self, spark, lakehouse):
        from registry import tables

        self._seed_enrichment(spark, lakehouse, [_enriched("K181892")])
        mod.run(spark, lakehouse, fetcher=_FakeFetcher())
        row = tables.read_table(spark, lakehouse, mod.BRONZE_TABLE).first()
        assert row["page_texts"] == [LONG]
        assert row["page_char_counts"] == [mod.char_count(LONG)]
        assert row["text_class"] == "text"

    def test_without_enrichment_it_says_what_to_run(self, spark, lakehouse):
        with pytest.raises(mod.SummaryFetchError, match="enrich-openfda"):
            mod.run(spark, lakehouse, fetcher=_FakeFetcher())

    def test_only_works_without_enrichment(self, spark, lakehouse):
        """The fixture slice and spot checks must not need a full enrichment pass."""
        result = mod.run(spark, lakehouse, fetcher=_FakeFetcher(), only=["K181892"])
        assert result.written == 1


# ---------------------------------------------------------------------------
# The real slice (finding 0011 / handoff §2): recorded on a network-permitted host
# ---------------------------------------------------------------------------

# submission -> what finding 0011 measured, and why it is in the slice.
SLICE: dict[str, dict] = {
    "K003301": {"why": "scanned, bare pdf/ (2000)", "dir": "pdf", "class": "image"},
    "K181892": {
        "why": "8-predicate table",
        "dir": "pdf18",
        "class": "text",
        "pages": 6,
        "predicates": [
            "K151212",
            "K130552",
            "K130542",
            "K112051",
            "K141745",
            "K082427",
            "K140587",
            "K063559",
        ],
    },
    "K243239": {"why": "cybersecurity clause", "dir": "pdf24", "cybersecurity": True},
    "K141480": {"why": "predicate named without a parsable number", "dir": "pdf14"},
    "K241847": {
        "why": "predicate named without a parsable number; scanned tail",
        "dir": "pdf24",
        "class": "mixed",
    },
    "K092116": {"why": "ordinary 2010; pdf9 (no leading zero)", "dir": "pdf9"},
    "K141922": {"why": "ordinary 2015", "dir": "pdf14", "predicates": ["K113696", "K122938"]},
    "K190013": {
        "why": "ordinary 2019; labelled predicate + reference device",
        "dir": "pdf19",
        "predicates": ["K162532"],
        "cybersecurity": True,
    },
    "K203469": {"why": "ordinary 2021; mixed", "dir": "pdf20", "class": "mixed"},
    "K250177": {"why": "ordinary 2025; labelled field", "dir": "pdf25", "predicates": ["K242020"]},
}


def _despaced(doc: dict) -> str:
    """Finding 0011: the text layer splits glyphs, so match on a de-spaced copy."""
    return re.sub(r"\s+", "", "".join(doc["page_texts"]))


def _load(number: str) -> dict:
    path = FIXTURE_DIR / f"{number}.json"
    if not path.exists():
        pytest.skip(
            f"{path.name} not recorded yet: run `uv run pytest -m live_network -k summary` "
            "on a host that can reach accessdata.fda.gov, then commit tests/fixtures/"
        )
    return json.loads(path.read_text())


class TestRecordedSlice:
    """Assertions over the real recorded text. These carry the PR's claims about
    what 510(k) Summaries look like through pypdf -- the re-measure of finding 0011
    the handoff asked for."""

    @pytest.mark.parametrize("number", sorted(SLICE))
    def test_each_recording_is_a_valid_bronze_record(self, number):
        doc = _load(number)
        rec = BronzeSummaryDocumentRecord(
            **{k: v for k, v in doc.items() if k in BronzeSummaryDocumentRecord.model_fields}
        )
        assert rec.submission_number == number
        assert rec.http_status == 200
        assert rec.content_sha256 is not None

    @pytest.mark.parametrize("number", sorted(SLICE))
    def test_the_url_follows_the_directory_rule(self, number):
        doc = _load(number)
        assert doc["url"] == f"{BASE}/{SLICE[number]['dir']}/{number}.pdf"

    @pytest.mark.parametrize("number", sorted(SLICE))
    def test_counts_and_class_are_reproducible_from_the_text(self, number):
        """The stored measurements must follow from the stored text, not be trusted."""
        doc = _load(number)
        counts = [mod.char_count(t) for t in doc["page_texts"]]
        assert doc["page_char_counts"] == counts
        assert doc["page_count"] == len(doc["page_texts"])
        assert doc["text_class"] == mod.classify_pages(counts)

    @pytest.mark.parametrize("number", [n for n, s in SLICE.items() if "class" in s])
    def test_text_class_matches_the_spike(self, number):
        assert _load(number)["text_class"] == SLICE[number]["class"]

    @pytest.mark.parametrize("number", [n for n, s in SLICE.items() if "class" not in s])
    def test_ordinary_summaries_have_a_text_layer(self, number):
        assert _load(number)["text_class"] in ("text", "mixed")

    def test_the_eight_predicate_table_page_count(self):
        assert _load("K181892")["page_count"] == SLICE["K181892"]["pages"]

    @pytest.mark.parametrize("number", [n for n, s in SLICE.items() if "predicates" in s])
    def test_known_predicates_are_present_in_the_despaced_text(self, number):
        text = _despaced(_load(number))
        missing = [k for k in SLICE[number]["predicates"] if k not in text]
        assert not missing, f"{number}: predicates not in text layer: {missing}"

    @pytest.mark.parametrize("number", [n for n, s in SLICE.items() if s.get("cybersecurity")])
    def test_the_cybersecurity_clause_is_present(self, number):
        assert "cybersecurity" in _despaced(_load(number)).lower()

    def test_every_slice_document_is_recorded(self):
        """Fails, rather than skips, until the slice is recorded: the parametrized
        checks above skip per missing file, and a skip must never be the steady
        state. Record with `uv run pytest -m live_network -k summary` on a host
        that can reach accessdata.fda.gov, then commit tests/fixtures/."""
        missing = sorted(n for n in SLICE if not (FIXTURE_DIR / f"{n}.json").exists())
        assert not missing, f"summary fixtures not recorded: {missing}"
        assert (FIXTURE_DIR / "manifest.json").exists()

    def test_no_pdf_bytes_are_committed(self):
        """Text, never PDFs, in git (handoff §2)."""
        assert not list(FIXTURE_DIR.glob("*.pdf"))


@pytest.mark.live_network
class TestLiveRecording:
    """Hits the real accessdata.fda.gov and RECORDS the slice above as fixtures.

    Never runs in CI or agent sessions. On a network-permitted host:
    ``uv run pytest -m live_network -k summary``, then commit
    ``tests/fixtures/summary_documents/``. Throttled at the configured ~1 req/s
    and stops on 429/403 like the real pass.
    """

    def test_record_the_fixture_slice(self):
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        recorded_at = dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0)
        manifest = []
        with mod.SummaryFetcher(Settings()) as fetcher:
            for number in sorted(SLICE):
                doc = fetcher.fetch(number)
                assert doc.found, f"{number}: no PDF at {doc.urls_tried} ({doc.http_status})"
                record = mod.to_record(doc, ingested_at=recorded_at)
                payload = json.loads(record.model_dump_json())
                (FIXTURE_DIR / f"{number}.json").write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
                )
                raw = "".join(doc.page_texts)
                manifest.append(
                    {
                        "submission_number": number,
                        "why": SLICE[number]["why"],
                        "url": doc.url,
                        "content_sha256": doc.content_sha256,
                        "page_count": doc.page_count,
                        "text_class": doc.text_class,
                        # The glyph-splitting re-measure for this extractor:
                        # submission numbers a naive regex finds on the raw layer
                        # vs on a de-spaced copy.
                        "k_numbers_raw": len(set(re.findall(r"\bK\d{6}\b", raw))),
                        "k_numbers_despaced": len(
                            set(re.findall(r"K\d{6}", re.sub(r"\s+", "", raw)))
                        ),
                    }
                )
        (FIXTURE_DIR / "manifest.json").write_text(
            json.dumps(
                {
                    "recorded_at": recorded_at.isoformat(),
                    "extractor": mod.EXTRACTOR,
                    "documents": manifest,
                },
                indent=2,
            )
            + "\n"
        )
        assert len(manifest) == len(SLICE)
