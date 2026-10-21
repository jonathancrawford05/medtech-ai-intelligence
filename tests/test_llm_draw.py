"""Drawing a fresh gold set (ADR 0022 Decision 7), from the recorded spike run.

The recorded run is all gold-v1 documents plus three failure-mode extras, so excluding
v1 would leave almost nothing; most tests exclude a named subset instead.
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

import pytest
from typer.testing import CliRunner

from registry import cli
from registry.llm_extraction import draw as dr
from registry.llm_extraction import gold, run

LLM = Path(__file__).parent / "fixtures" / "llm_extraction"
V1 = LLM / "gold_set_v1.csv"


@pytest.fixture(scope="module")
def evidence():
    return run.evidence_from_recorded(LLM, built_at=dt.datetime(2026, 10, 21))


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


class TestDraw:
    def test_is_reproducible_by_seed(self, evidence):
        a = dr.draw(evidence, seed=7)
        assert a.rows == dr.draw(evidence, seed=7).rows
        assert a.rows != dr.draw(evidence, seed=8).rows

    def test_draws_each_stratum_up_to_its_quota(self, evidence):
        result = dr.draw(evidence, seed=7)
        for name, quota in dr.DEFAULT_QUOTAS.items():
            assert result.drawn[name] == min(quota, result.population[name]), name
        assert len(result.rows) == sum(result.drawn.values())

    def test_samples_rejected_values_too(self, evidence):
        """v1 drew accepted values only, so it could not see recall lost to the checks."""
        result = dr.draw(evidence, seed=7)
        rejected = [r for r in result.rows if r["why_sampled"].endswith("rejected")]
        assert rejected
        assert all(r["check_status"] in dr.checks.REJECT for r in rejected)

    def test_never_draws_a_dropped_duplicate(self, evidence):
        rows = dr.draw(evidence, seed=7, quotas={"metric: plain accept": 1000}).rows
        assert all(r["check_status"] != "duplicate" for r in rows)

    def test_excluded_documents_are_never_drawn(self, evidence):
        v1 = dr.submissions_in([V1])
        result = dr.draw(evidence, seed=7, exclude=v1)
        # The two failure-mode extras outside v1 (K230842 is itself a v1 document).
        assert {r["submission_number"] for r in result.rows} == {"K251218", "K182456"}
        assert result.documents == 2

    def test_risk_endpoints_are_their_own_stratum(self, evidence):
        rows = dr.draw(evidence, seed=7).rows
        risk = [r for r in rows if r["why_sampled"] == "endpoint: risk"]
        assert risk and all(r["value"] in dr.sc.RISK_ENDPOINTS for r in risk)

    def test_one_run_at_a_time(self, evidence):
        other = [e.model_copy(update={"prompt_version": "4d.3"}) for e in evidence[:3]]
        with pytest.raises(ValueError, match="one run"):
            dr.draw([*evidence, *other], seed=7)


class TestFilesAndMerge:
    def test_the_blind_sheet_hides_status_and_reason(self, evidence, tmp_path):
        key, blind, strata = dr.write(dr.draw(evidence, seed=7), tmp_path / "gold_set_v2.csv")
        assert blind.name == "gold_set_v2.blind.csv" and strata.name == "gold_set_v2.strata.csv"
        header = _read(blind)[0].keys()
        assert "check_status" not in header and "why_sampled" not in header
        assert list(_read(key)[0].keys()) == list(dr.GOLD_COLUMNS)
        assert {r["stratum"] for r in _read(strata)} == set(dr.DEFAULT_QUOTAS)

    def test_round_trip_label_merge_score(self, evidence, tmp_path):
        """Draw, label every row y on the blind sheet, merge, and score the same run."""
        key, blind, _ = dr.write(dr.draw(evidence, seed=7), tmp_path / "v2.csv")
        sheet = _read(blind)
        for r in sheet:
            r["judgement"] = "y"
        with blind.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(sheet[0].keys()))
            w.writeheader()
            w.writerows(sheet)
        out = tmp_path / "gold_set_v2.csv"
        assert dr.merge(key, blind, out) == len(sheet)

        result = gold.score(gold.load_gold(out), evidence)
        assert set(result.by_stratum) <= set(dr.DEFAULT_QUOTAS)
        assert all(s.evidence is not None for s in result.scored)
        # Every row labelled right: accepted ones count as precision, rejected as recall lost.
        drawn_rejected = sum(1 for k in _read(key) if k["why_sampled"] == "metric: rejected")
        assert result.by_stratum["metric: rejected"].rejected_right == drawn_rejected > 0

    def test_merge_refuses_a_sheet_that_no_longer_matches(self, evidence, tmp_path):
        key, blind, _ = dr.write(dr.draw(evidence, seed=7), tmp_path / "v2.csv")
        sheet = _read(blind)
        sheet[0]["value"] = "99.9"
        with blind.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(sheet[0].keys()))
            w.writeheader()
            w.writerows(sheet)
        with pytest.raises(ValueError, match="value differs"):
            dr.merge(key, blind, tmp_path / "out.csv")


class TestDocumentsForTheLabeller:
    def test_every_page_with_text_is_written(self, tmp_path):
        import json

        doc = run.DocumentInput("K240001", "Device", "a" * 64, ["letter", "", "summary", "  "], [3])
        (path,) = dr.write_documents([doc], tmp_path / "documents")
        data = json.loads(path.read_text())
        assert path.name == "K240001.json"
        assert [p["page"] for p in data["pages"]] == [1, 3]  # blank pages dropped
        assert data["pages_sent_to_model"] == [3]
        assert data["device_name"] == "Device"


class TestCli:
    def test_draw_gold_on_the_recorded_run(self, tmp_path):
        out = CliRunner().invoke(
            cli.app,
            [
                "draw-gold",
                "--out",
                str(tmp_path / "v2.csv"),
                "--prompt-version",
                "4d-spike-4",
                "--recorded",
                str(LLM),
                "--exclude",
                "K240795",
            ],
        )
        assert out.exit_code == 0, out.output
        assert "seed 20261021" in out.output and "1 excluded" in out.output
        assert (tmp_path / "v2.blind.csv").exists()

    def test_documents_need_the_lakehouse(self, tmp_path):
        out = CliRunner().invoke(
            cli.app,
            [
                "draw-gold",
                "--out",
                str(tmp_path / "v2.csv"),
                "--prompt-version",
                "4d-spike-4",
                "--recorded",
                str(LLM),
                "--documents",
                str(tmp_path / "docs"),
            ],
        )
        assert out.exit_code == 1
        assert "needs the lakehouse" in out.output
