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
