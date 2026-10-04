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
