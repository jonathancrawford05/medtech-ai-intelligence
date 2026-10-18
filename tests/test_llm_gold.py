"""The gold-set harness (PR 4D; ADR 0022 Decision 7), scoring the recorded spike run.

`gold_set_v1.csv` was drawn from the spike's accepted values and labelled against
the full text (finding 0023). Scored with this PR's checks, the numbers below are
the baseline a new prompt or model is compared to.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from registry.llm_extraction import gold, run
from registry.schemas import LlmEvidenceRecord

LLM = Path(__file__).parent / "fixtures" / "llm_extraction"


@pytest.fixture(scope="module")
def rows() -> list[gold.GoldRow]:
    return gold.load_gold(LLM / "gold_set_v1.csv")


@pytest.fixture(scope="module")
def result(rows) -> gold.GoldScore:
    return gold.score(rows, run.evidence_from_recorded(LLM, built_at=dt.datetime(2026, 10, 18)))


def _cells(t: gold.Tally) -> tuple[int, int, int, int, int, int, int]:
    return (
        t.accepted_right,
        t.accepted_wrong,
        t.accepted_other,
        t.rejected_right,
        t.rejected_wrong,
        t.rejected_other,
        t.missing,
    )


class TestLoad:
    def test_reads_all_58_rows_and_splits_metric_fields(self, rows):
        assert len(rows) == 58
        first = rows[0]
        assert (first.field, first.metric, first.setting) == ("metric", "sensitivity", "standalone")
        assert {r.judgement for r in rows} == {"y", "n", "partly", "unsure"}


class TestRecordedRunScore:
    def test_every_gold_row_finds_its_recorded_value(self, result):
        assert all(s.evidence is not None for s in result.scored)

    def test_metrics(self, result):
        """16 accepted, 14 right (87.5%); every rejection was a wrong value."""
        t = result.by_field["metric"]
        assert _cells(t) == (14, 2, 0, 0, 7, 1, 0)
        assert t.precision == pytest.approx(14 / 16)
        assert t.flagged == 8

    def test_scalar_fields(self, result):
        assert _cells(result.by_field["sample_size"]) == (5, 0, 1, 0, 0, 0, 0)
        assert _cells(result.by_field["study_design"]) == (4, 1, 0, 0, 0, 0, 0)
        assert _cells(result.by_field["demographics_disclosed"]) == (4, 1, 0, 0, 0, 0, 0)
        assert _cells(result.by_field["multi_site"]) == (3, 0, 0, 0, 0, 0, 0)

    def test_the_endpoint_is_the_weak_field(self, result):
        """Finding 0023: the seed was right more often than the model."""
        t = result.by_field["clinical_endpoint"]
        assert _cells(t) == (7, 7, 1, 0, 0, 0, 0)
        assert t.precision == 0.5

    def test_by_stratum_the_plain_accepts_are_8_of_8(self, result):
        assert _cells(result.by_stratum["metric: plain accept"]) == (8, 0, 0, 0, 0, 0, 0)

    def test_by_stratum_the_misnamed_rows_are_all_rejected(self, result):
        assert _cells(result.by_stratum["metric: name looks wrong"]) == (0, 0, 0, 0, 5, 1, 0)

    def test_by_status_the_new_reject_rules_only_caught_wrong_values(self, result):
        assert _cells(result.by_status["measure_not_named"]) == (0, 0, 0, 0, 6, 1, 0)
        assert _cells(result.by_status["zero_triple"]) == (0, 0, 0, 0, 1, 0, 0)

    def test_by_status_gapped_quotes_are_review_only(self, result):
        """Finding 0023: right 2 of 5 in the gold set; flagged, never plain-accepted."""
        t = result.by_status["ok_gapped"]
        assert t.flagged == t.accepted
        assert t.precision is not None and t.precision < 0.5

    def test_the_bar_is_reported_not_decided(self, result):
        assert result.by_field["multi_site"].clears(0.9, min_judged=3)
        assert not result.by_field["multi_site"].clears(0.9, min_judged=10)
        assert not result.by_field["clinical_endpoint"].clears(0.9, min_judged=1)
        assert any("not yet" in line for line in result.lines(bar=0.9))


def _ev(**kw) -> LlmEvidenceRecord:
    base = {
        "submission_number": "K1",
        "run_id": "r",
        "model": "m",
        "model_digest": "d",
        "prompt_version": "4d.1",
        "checks_version": "4d.1",
        "requested_at": dt.datetime(2026, 10, 18),
        "built_at": dt.datetime(2026, 10, 18),
        "source_content_sha256": "s",
        "field": "clinical_endpoint",
        "value_text": "monitoring",
        "quote": "q",
        "quote_pages": [1],
        "quote_status": "ok",
        "check_status": "ok",
        "accepted": True,
        "needs_review": False,
    }
    return LlmEvidenceRecord(**{**base, **kw})


def _gold(**kw) -> gold.GoldRow:
    base = {
        "row": 1,
        "submission_number": "K1",
        "field": "clinical_endpoint",
        "value": "deterioration_risk",
        "metric": None,
        "setting": None,
        "label": None,
        "check_status": "ok",
        "why_sampled": "s",
        "judgement": "n",
        "correct_value": "monitoring",
    }
    return gold.GoldRow(**{**base, **kw})


class TestVerdictForANewRun:
    """A fresh run may say something other than what the gold row judged."""

    def test_a_corrected_value_is_right(self):
        assert gold.verdict(_gold(), _ev(value_text="monitoring")) == "right"

    def test_the_old_wrong_value_is_still_wrong(self):
        assert gold.verdict(_gold(), _ev(value_text="deterioration_risk")) == "wrong"

    def test_a_third_value_is_wrong_when_the_truth_is_named(self):
        assert gold.verdict(_gold(), _ev(value_text="detection_diagnosis")) == "wrong"

    def test_a_free_text_correction_cannot_judge_a_new_value(self):
        g = _gold(correct_value="deterioration_risk or monitoring", judgement="n")
        assert gold.verdict(g, _ev(value_text="other")) == "unjudged"

    def test_null_is_right_when_the_truth_is_not_stated(self):
        g = _gold(
            field="demographics_disclosed", value="False", correct_value="not stated (no data)"
        )
        assert gold.verdict(g, None) == "right"

    def test_a_renamed_metric_is_judged_by_the_corrected_name(self):
        g = _gold(
            field="metric",
            value="9.25",
            metric="accuracy",
            setting="standalone",
            correct_value="other (absolute mean error) 9.25 ms",
        )
        renamed = _ev(field="metric", metric="other", value_number=9.25, value_text="9.25")
        assert gold.verdict(g, renamed) == "right"
        assert gold.verdict(g, _ev(field="metric", metric="mae", value_number=9.25)) == "wrong"

    def test_partly_and_unsure_are_other(self):
        assert gold.verdict(_gold(judgement="unsure"), _ev()) == "other"

    def test_runs_are_scored_one_model_and_prompt_at_a_time(self):
        with pytest.raises(ValueError, match="score one at a time"):
            gold.score([_gold()], [_ev(), _ev(model_digest="e")])
