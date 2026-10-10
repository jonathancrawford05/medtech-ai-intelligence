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
        assert "deferred: pccp" in result.output
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
