"""CLI wiring. Pipeline behaviour is tested in its own modules; this checks only
what the command tells the operator."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from registry import cli


@pytest.fixture
def build_silver_returning(monkeypatch):
    def _patch(written: int):
        import registry.spark_session
        import registry.transform.bronze_to_silver

        monkeypatch.setattr(registry.spark_session, "get_spark", lambda settings: object())
        monkeypatch.setattr(
            registry.transform.bronze_to_silver, "run", lambda spark, settings: written
        )
        return CliRunner().invoke(cli.app, ["build-silver"])

    return _patch


class TestBuildSilver:
    def test_reports_the_rows_written(self, build_silver_returning):
        result = build_silver_returning(1614)
        assert result.exit_code == 0
        assert "Wrote 1614 rows to" in result.output

    def test_a_gated_build_says_nothing_was_written(self, build_silver_returning):
        """ADR 0015 Decision 4: 0 means the gate found silver current, which the
        operator should read as 'no change', not as 'an empty table was written'."""
        result = build_silver_returning(0)
        assert result.exit_code == 0
        assert "already current" in result.output
        assert "nothing written" in result.output
        assert "Wrote" not in result.output


class TestMonitor:
    """What `registry monitor` tells the operator for each run outcome."""

    def _invoke(self, monkeypatch, result):
        import registry.mart.leads
        import registry.spark_session

        monkeypatch.setattr(registry.spark_session, "get_spark", lambda settings: object())
        monkeypatch.setattr(registry.mart.leads, "run", lambda spark, settings: result)
        return CliRunner().invoke(cli.app, ["monitor"])

    def test_reports_counts_per_category_and_the_deferred_ones(self, monkeypatch):
        from registry.mart.leads import LeadRun

        result = self._invoke(
            monkeypatch, LeadRun(status="recorded", prev_snapshot_id="a", curr_snapshot_id="b")
        )
        assert result.exit_code == 0
        assert "Recorded 0 lead(s) for a -> b" in result.output
        assert "new_submission" in result.output
        assert "pccp" in result.output and "deferred: pccp" not in result.output
        assert "deferred: foundation_model" in result.output

    def test_says_when_there_is_nothing_to_diff(self, monkeypatch):
        from registry.mart.leads import LeadRun

        result = self._invoke(monkeypatch, LeadRun(status="no_pair"))
        assert result.exit_code == 0
        assert "Fewer than two distinct snapshots" in result.output

    def test_a_rerun_says_nothing_was_appended(self, monkeypatch):
        from registry.mart.leads import LeadRun

        result = self._invoke(
            monkeypatch,
            LeadRun(status="already_recorded", prev_snapshot_id="a", curr_snapshot_id="b"),
        )
        assert "already in" in result.output
        assert "nothing appended" in result.output

    def test_a_history_barrier_fails_loudly(self, monkeypatch):
        from registry.mart.leads import LeadRun

        result = self._invoke(
            monkeypatch, LeadRun(status="history_barrier", barrier_operation="REORG")
        )
        assert result.exit_code == 1
        assert "'REORG'" in result.output

    def test_suppressed_removals_are_a_warning(self, monkeypatch):
        from registry.mart.leads import LeadRun

        result = self._invoke(
            monkeypatch,
            LeadRun(
                status="recorded",
                prev_snapshot_id="a",
                curr_snapshot_id="b",
                removals_suppressed=15,
            ),
        )
        assert "15 removal lead(s) not recorded" in result.output

    def test_suppressed_relistings_are_a_warning(self, monkeypatch):
        from registry.mart.leads import LeadRun

        result = self._invoke(
            monkeypatch,
            LeadRun(
                status="recorded",
                prev_snapshot_id="a",
                curr_snapshot_id="b",
                relistings_suppressed=12,
            ),
        )
        assert "12 relisting lead(s) not recorded" in result.output

    def test_a_history_barrier_says_how_to_recover(self, monkeypatch):
        from registry.mart.leads import LeadRun

        result = self._invoke(
            monkeypatch, LeadRun(status="history_barrier", barrier_operation="REORG")
        )
        assert "registry build-silver" in result.output
        assert "before the next ingest" in result.output


class TestFetchSummaries:
    """What `registry fetch-summaries` tells the operator (ADR 0018)."""

    def _invoke(self, monkeypatch, result, args=()):
        import registry.ingest.summary_documents
        import registry.spark_session

        calls = {}

        def fake_run(spark, settings, *, limit=None, only=None):
            calls.update(limit=limit, only=only)
            return result

        monkeypatch.setattr(registry.spark_session, "get_spark", lambda settings: object())
        monkeypatch.setattr(registry.ingest.summary_documents, "run", fake_run)
        return CliRunner().invoke(cli.app, ["fetch-summaries", *args]), calls

    def test_reports_the_pass(self, monkeypatch):
        from registry.ingest.summary_documents import FetchResult

        out, calls = self._invoke(
            monkeypatch,
            FetchResult(
                attempted=3,
                written=3,
                found=2,
                not_found=1,
                text_classes={"text": 1, "mixed": 1},
                deferred=["DEN250057"],
                already_fetched=7,
            ),
        )
        assert out.exit_code == 0
        assert calls == {"limit": None, "only": None}
        assert "Fetched 3 document(s): 2 found, 1 not found" in out.output
        assert "Appended 3 row(s) to" in out.output
        assert "mixed" in out.output and "text" in out.output
        assert "7 already in bronze" in out.output
        assert "deferred: 1 De Novo/PMA Summary filing(s)" in out.output

    def test_passes_limit_and_only_through(self, monkeypatch):
        from registry.ingest.summary_documents import FetchResult

        _, calls = self._invoke(
            monkeypatch,
            FetchResult(),
            ["--limit", "5", "--only", "K181892", "--only", "k003301,K250177"],
        )
        assert calls == {"limit": 5, "only": ["K181892", "K003301", "K250177"]}

    def test_a_halt_exits_non_zero_and_says_how_to_resume(self, monkeypatch):
        from registry.ingest.summary_documents import FetchResult

        out, _ = self._invoke(
            monkeypatch,
            FetchResult(attempted=2, written=1, found=1, halted="returned 429 for u"),
        )
        assert out.exit_code == 1
        assert "429" in out.output
        assert "re-run" in out.output

    def test_a_missing_enrichment_table_is_a_clean_error(self, monkeypatch):
        import registry.ingest.summary_documents as mod
        import registry.spark_session

        def boom(spark, settings, *, limit=None, only=None):
            raise mod.SummaryFetchError("Run `registry enrich-openfda` first")

        monkeypatch.setattr(registry.spark_session, "get_spark", lambda settings: object())
        monkeypatch.setattr(mod, "run", boom)
        out = CliRunner().invoke(cli.app, ["fetch-summaries"])
        assert out.exit_code == 1
        assert "enrich-openfda" in out.output


class TestExtractDocuments:
    """What `registry extract-documents` tells the operator (ADR 0019)."""

    def test_reports_rows_and_yields(self, monkeypatch):
        import registry.spark_session
        from registry.transform import document_extraction as de

        result = de.ExtractionRun(written=3, yields=de.Yields(documents=3, with_predicate=2))
        monkeypatch.setattr(registry.spark_session, "get_spark", lambda settings: object())
        monkeypatch.setattr(de, "run", lambda spark, settings: result)
        out = CliRunner().invoke(cli.app, ["extract-documents"])
        assert out.exit_code == 0, out.output
        assert "Wrote 3 row(s)" in out.output
        assert de.EXTRACTOR_VERSION in out.output
        assert "predicate found                : 2 / 3" in out.output
        assert "1,508 / 1,541" in out.output, "the finding 0019 comparison is printed"

    def test_a_missing_bronze_table_is_a_clean_error(self, monkeypatch):
        import registry.spark_session
        from registry.transform import document_extraction as de

        def boom(spark, settings):
            raise ValueError("Run `registry fetch-summaries` first")

        monkeypatch.setattr(registry.spark_session, "get_spark", lambda settings: object())
        monkeypatch.setattr(de, "run", boom)
        out = CliRunner().invoke(cli.app, ["extract-documents"])
        assert out.exit_code == 1
        assert "fetch-summaries" in out.output


class TestEnrichPredicates:
    def test_reports_the_dates_fetched(self, monkeypatch):
        import registry.spark_session
        from registry.transform import predicate_dates

        monkeypatch.setattr(registry.spark_session, "get_spark", lambda settings: object())
        monkeypatch.setattr(predicate_dates, "run", lambda spark, settings: 7)
        out = CliRunner().invoke(cli.app, ["enrich-predicates"])
        assert out.exit_code == 0, out.output
        assert "Fetched 7 predicate decision date(s)" in out.output

    def test_extraction_must_run_first(self, monkeypatch):
        import registry.spark_session
        from registry.transform import predicate_dates

        def boom(spark, settings):
            raise ValueError("Run `registry extract-documents` first.")

        monkeypatch.setattr(registry.spark_session, "get_spark", lambda settings: object())
        monkeypatch.setattr(predicate_dates, "run", boom)
        out = CliRunner().invoke(cli.app, ["enrich-predicates"])
        assert out.exit_code == 1
        assert "extract-documents" in out.output


class TestExtractIntendedUse:
    """What `registry extract-intended-use` tells the operator (ADR 0021)."""

    def test_reports_rows_and_coverage_against_finding_0019(self, monkeypatch):
        from collections import Counter

        import registry.spark_session
        from registry.transform import intended_use as iu

        yields = iu.Yields(documents=4, basis=Counter(form_3881=2, summary_section=1, no_text=1))
        monkeypatch.setattr(registry.spark_session, "get_spark", lambda settings: object())
        monkeypatch.setattr(iu, "run", lambda spark, settings: iu.IntendedUseRun(4, yields))
        out = CliRunner().invoke(cli.app, ["extract-intended-use"])
        assert out.exit_code == 0, out.output
        assert "Wrote 4 row(s)" in out.output
        assert iu.INTENDED_USE_VERSION in out.output
        assert "intended use found             : 3 / 4 (75.0%)" in out.output
        assert "1,537 / 1,541" in out.output, "the finding 0019 ceiling is printed"

    def test_extraction_must_run_first(self, monkeypatch):
        import registry.spark_session
        from registry.transform import intended_use as iu

        def boom(spark, settings):
            raise ValueError("Run `registry extract-documents` first")

        monkeypatch.setattr(registry.spark_session, "get_spark", lambda settings: object())
        monkeypatch.setattr(iu, "run", boom)
        out = CliRunner().invoke(cli.app, ["extract-intended-use"])
        assert out.exit_code == 1
        assert "extract-documents" in out.output


class TestCurationCandidates:
    def _report(self, n: int):
        import datetime as dt

        from registry.mart import curation_candidates as cc

        rows = [
            cc.Candidate(
                submission_number=f"K70000{i}",
                device_name=f"Risk index {i}",
                applicant="Acme",
                decision_date=dt.date(2026, 1, i + 1),
                specialty_category="cardiovascular",
                product_code="DQK",
                keyword_hits=["mortality"],
                intended_use_basis="form_3881",
                intended_use_pages=[3],
                intended_use_text="Predicts in-hospital mortality.",
                source_url=f"https://example.test/K70000{i}.pdf",
                scope="statement" if i == 0 else "specialty",
            )
            for i in range(n)
        ]
        return cc.CurationReport(rows, cc.KeywordAgreement(curated=133), 160, 150)

    def _invoke(self, monkeypatch, report, args=()):
        import registry.spark_session
        from registry.mart import curation_candidates as cc

        monkeypatch.setattr(registry.spark_session, "get_spark", lambda settings: object())
        monkeypatch.setattr(cc, "build", lambda spark, settings: report)
        return CliRunner().invoke(cli.app, ["curation-candidates", *args])

    def test_prints_the_measure_and_the_batch_with_evidence(self, monkeypatch):
        out = self._invoke(monkeypatch, self._report(3))
        assert out.exit_code == 0, out.output
        assert "keyword_disagrees" in out.output
        assert (
            "3 candidate(s): 2 among 160 cardiometabolic device(s) (150 with a statement read), "
            "1 from another specialty by cardiovascular language" in " ".join(out.output.split())
        )
        assert "K700000" in out.output and "[mortality]" in out.output
        assert "form_3881 p3  https://example.test/K700000.pdf" in out.output

    def test_limit_clips_the_print_and_the_csv_has_all(self, monkeypatch, tmp_path):
        target = tmp_path / "batch.csv"
        out = self._invoke(monkeypatch, self._report(3), ["--limit", "1", "--csv", str(target)])
        assert out.exit_code == 0, out.output
        assert "... 2 more" in out.output
        assert len(target.read_text().splitlines()) == 4  # header + 3

    def test_missing_tables_say_what_to_run(self, monkeypatch):
        import registry.spark_session
        from registry.mart import curation_candidates as cc

        def boom(spark, settings):
            raise ValueError(
                "No silver_intended_use table. Run: uv run registry extract-intended-use"
            )

        monkeypatch.setattr(registry.spark_session, "get_spark", lambda settings: object())
        monkeypatch.setattr(cc, "build", boom)
        out = CliRunner().invoke(cli.app, ["curation-candidates"])
        assert out.exit_code == 1
        assert "extract-intended-use" in out.output
