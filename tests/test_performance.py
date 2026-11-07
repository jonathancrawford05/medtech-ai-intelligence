"""A device's headline sensitivity / specificity from one LLM run (ADR 0023).

The selection rule is tested on hand-built evidence rows, because no `4d.3` answer
is recorded in the fixtures yet: only `4d.3` answers carry `applies_to` and
`is_result`, which the rule requires. The recorded `4d-spike-4` slice is used to
show that a run which never asked for attribution ships nothing.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from registry.llm_extraction import performance as perf
from registry.llm_extraction import run
from registry.schemas import DevicePerformanceRecord, LlmEvidenceRecord

LLM = Path(__file__).parent / "fixtures" / "llm_extraction"
T0 = dt.datetime(2026, 11, 7)


def _metric(
    number: str = "K250001",
    *,
    metric: str = "sensitivity",
    value: float = 92.5,
    index: int = 0,
    context: str | None = None,
    quote: str | None = None,
    status: str = "ok",
    applies_to: str | None = "subject_device",
    is_result: bool | None = True,
    setting: str = "standalone",
    prompt: str = "4d.3",
    digest: str = "d" * 64,
) -> LlmEvidenceRecord:
    accepted = status in perf.checks.ACCEPT
    return LlmEvidenceRecord(
        submission_number=number,
        run_id="4d.3-qwen3_14b-20261021T000000Z",
        model="qwen3:14b",
        model_digest=digest,
        prompt_version=prompt,
        checks_version="4d.2",
        requested_at=T0,
        built_at=T0,
        source_content_sha256="s" * 64,
        field="metric",
        value_text=str(value),
        value_number=value,
        metric_index=index,
        metric=metric,
        label=metric.title(),
        context=context,
        applies_to=applies_to,
        setting=setting,
        is_result=is_result,
        quote=quote or f"The {metric} was {value}% (95% CI ...).",
        quote_pages=[7],
        quote_status=status,
        check_status=status,
        accepted=accepted,
        needs_review=status in perf.checks.REVIEW,
    )


def _scalar(number: str = "K250009", **kw) -> LlmEvidenceRecord:
    base = _metric(number).model_dump()
    base.update(
        field="study_design",
        value_text="retrospective",
        value_number=None,
        metric=None,
        metric_index=None,
        label=None,
        setting=None,
        applies_to=None,
        is_result=None,
    )
    base.update(kw)
    return LlmEvidenceRecord(**base)


def _one(rows: list[LlmEvidenceRecord], number: str = "K250001") -> DevicePerformanceRecord:
    (rec,) = [r for r in perf.select(rows, built_at=T0) if r.submission_number == number]
    return rec


class TestWhatCounts:
    def test_a_single_accepted_figure_is_chosen_as_a_proportion(self):
        rec = _one([_metric(value=92.5)])
        assert rec.sensitivity == pytest.approx(0.925)
        assert rec.sensitivity_as_written == 92.5
        assert rec.sensitivity_basis == "single"
        assert rec.sensitivity_pages == [7]
        assert rec.reports_sensitivity_specificity is True
        assert rec.specificity is None

    def test_a_fraction_without_a_percent_sign_stays_a_fraction(self):
        rec = _one([_metric(value=0.93, quote="Sensitivity 0.93 (0.90-0.95)")])
        assert rec.sensitivity == pytest.approx(0.93)

    def test_a_small_percentage_is_read_as_a_percentage(self):
        """0.9% sensitivity is improbable but must not become 0.9."""
        rec = _one([_metric(value=0.9, quote="sensitivity of 0.9% in the subgroup")])
        assert rec.sensitivity == pytest.approx(0.009)

    @pytest.mark.parametrize(
        "kw",
        [
            {"status": "value_not_in_quote"},  # rejected by the checks
            {"applies_to": "predicate"},  # K252284's error in gold v2
            {"applies_to": "original_device"},
            {"applies_to": None},  # not asked: the spike prompt
            {"is_result": False},  # a threshold
            {"is_result": None},
            {"setting": "reader_with_device"},
            {"metric": "ppv"},
        ],
    )
    def test_only_accepted_subject_device_standalone_results_count(self, kw):
        rec = _one([_metric(**kw)])
        assert rec.sensitivity is None and rec.specificity is None
        assert rec.reports_sensitivity_specificity is None

    def test_an_impossible_proportion_is_dropped(self):
        rec = _one([_metric(value=180.0)])
        assert rec.sensitivity is None

    def test_a_flagged_figure_is_kept_but_marked_for_review(self):
        rec = _one([_metric(status="ok_gapped")])
        assert rec.sensitivity == pytest.approx(0.925)
        assert rec.needs_review is True


class TestChoosingAmongSeveral:
    def test_the_one_overall_figure_beats_subgroups(self):
        rows = [
            _metric(value=80.6, context="age >80", index=0),
            _metric(value=84.4, context=None, index=1),
            _metric(value=88.0, context="female", index=2),
        ]
        rec = _one(rows)
        assert rec.sensitivity == pytest.approx(0.844)
        assert rec.sensitivity_basis == "overall"
        assert rec.sensitivity_candidates == 3
        assert rec.needs_review is False

    def test_an_overall_context_counts_as_overall(self):
        rows = [
            _metric(value=80.6, context="Center 1", index=0),
            _metric(value=84.4, context="Overall (all sites)", index=1),
        ]
        assert _one(rows).sensitivity_basis == "overall"

    def test_several_overall_figures_take_the_first_and_flag_it(self):
        """Per-vessel and per-patient both read as headline figures (K213657)."""
        rows = [
            _metric(value=87.4, context=None, index=3),
            _metric(value=86.9, context=None, index=0),
        ]
        rec = _one(rows)
        assert rec.sensitivity == pytest.approx(0.869)
        assert rec.sensitivity_basis == "first_of_several"
        assert rec.needs_review is True

    def test_only_subgroups_take_the_first_and_flag_it(self):
        rows = [
            _metric(value=78.2, context="Center 2", index=1),
            _metric(value=86.6, context="Center 1", index=0),
        ]
        rec = _one(rows)
        assert rec.sensitivity == pytest.approx(0.866)
        assert rec.sensitivity_basis == "first_of_several"
        assert rec.sensitivity_context == "Center 1"

    def test_the_same_figure_twice_is_one_candidate(self):
        rows = [_metric(value=92.5, index=0), _metric(value=92.5, index=4, context=None)]
        rec = _one(rows)
        assert rec.sensitivity_basis == "single"
        assert rec.sensitivity_candidates == 1

    def test_a_figure_repeated_as_overall_counts_as_overall(self):
        """A table row and the headline sentence giving the same figure."""
        rows = [
            _metric(value=84.4, context="Center 1", index=0),
            _metric(value=90.1, context="Center 2", index=1),
            _metric(value=84.4, context=None, index=2),
        ]
        rec = _one(rows)
        assert rec.sensitivity == pytest.approx(0.844)
        assert rec.sensitivity_basis == "overall"
        assert rec.sensitivity_context is None

    def test_sensitivity_and_specificity_are_chosen_independently(self):
        rows = [
            _metric(metric="sensitivity", value=93.6, index=0),
            _metric(metric="specificity", value=99.0, index=1),
        ]
        rec = _one(rows)
        assert (rec.sensitivity, rec.specificity) == (pytest.approx(0.936), pytest.approx(0.99))


class TestRowsAndRuns:
    def test_every_answered_document_has_a_row(self):
        rows = [_metric("K250001"), _scalar("K250009")]
        recs = {r.submission_number: r for r in perf.select(rows, built_at=T0)}
        assert set(recs) == {"K250001", "K250009"}
        assert recs["K250009"].reports_sensitivity_specificity is None

    def test_provenance_is_the_runs(self):
        rec = _one([_metric()])
        assert rec.review_method == "llm_assisted"
        assert rec.model_digest == "d" * 64 and rec.prompt_version == "4d.3"
        assert rec.selection_version == perf.SELECTION_VERSION

    def test_one_run_at_a_time(self):
        with pytest.raises(ValueError, match="one run"):
            perf.select([_metric(), _metric("K250002", digest="e" * 64)], built_at=T0)

    def test_a_run_that_never_asked_for_attribution_ships_nothing(self):
        """The recorded spike slice (4d-spike-4): 236 accepted metrics, none shippable."""
        evidence = run.evidence_from_recorded(LLM, built_at=T0)
        recs = perf.select(evidence, built_at=T0)
        assert len(recs) == 43
        assert not any(r.reports_sensitivity_specificity for r in recs)

    def test_the_summary_counts_what_shipped(self):
        rows = [
            _metric("K250001", value=92.5),
            _metric("K250002", metric="specificity", value=97.0, status="ok_gapped"),
            _scalar("K250009"),
        ]
        lines = perf.summarise(perf.select(rows, built_at=T0)).lines()
        text = "\n".join(lines)
        assert "documents: 3" in text
        assert "sensitivity: 1" in text and "specificity: 1" in text
        assert "needs review: 1" in text


class TestValidator:
    def test_a_value_needs_its_quote_and_page(self):
        rec = _one([_metric()]).model_dump()
        rec["sensitivity_pages"] = []
        with pytest.raises(ValueError, match="needs its figure"):
            DevicePerformanceRecord(**rec)

    def test_reports_is_never_false(self):
        rec = _one([_scalar("K250009")], "K250009").model_dump()
        rec["reports_sensitivity_specificity"] = False
        with pytest.raises(ValueError, match="never False"):
            DevicePerformanceRecord(**rec)


class TestJoinOntoTheCuratedRows:
    """build-mart: the curated judgements gain their device's shipped figures."""

    def _curated(self, number: str):
        from registry.schemas import EvidenceRecord

        return EvidenceRecord(
            submission_number=number,
            intended_use_text="Predicts risk of cardiac mortality.",
            intended_use_source="https://example.test/src",
            mortality_keyword_flag=True,
            mortality_confirmed_flag=True,
            mortality_review_method="human",
        )

    def test_a_curated_device_with_figures_gets_them_and_the_run(self):
        from registry.transform import mortality_seed

        rec = _one([_metric(value=92.5), _metric(metric="specificity", value=99.0, index=1)])
        (joined,) = mortality_seed.with_performance(
            [self._curated("K250001")], {"K250001": rec.model_dump()}
        )
        assert joined.sensitivity == pytest.approx(0.925)
        assert joined.specificity == pytest.approx(0.99)
        assert joined.reports_sensitivity_specificity is True
        assert joined.performance_run_id == rec.run_id
        # The judgement itself is untouched.
        assert joined.mortality_confirmed_flag is True
        assert joined.intended_use_text == "Predicts risk of cardiac mortality."

    def test_a_curated_device_without_figures_keeps_none(self):
        from registry.transform import mortality_seed

        (joined,) = mortality_seed.with_performance([self._curated("K250001")], {})
        assert joined.sensitivity is None and joined.performance_run_id is None

    def test_an_uncurated_device_never_enters_silver_evidence(self):
        """silver_evidence stays the curated judgements: leads and curation read it so."""
        from registry.transform import mortality_seed

        rec = _one([_metric("K259999")], "K259999")
        joined = mortality_seed.with_performance(
            [self._curated("K250001")], {"K259999": rec.model_dump()}
        )
        assert [j.submission_number for j in joined] == ["K250001"]


@pytest.mark.spark
class TestSparkRoundTrip:
    def test_evidence_to_performance_to_the_curated_row(self, spark, lakehouse, tmp_path):
        from registry import tables
        from registry.llm_extraction import run as llm_run
        from registry.schemas import EvidenceRecord, spark_schema_for
        from registry.transform import mortality_seed

        evidence = [
            _metric("K250001", value=92.5),
            _metric("K250001", metric="specificity", value=97.0, index=1),
            _scalar("K250009"),
        ]
        tables.write_table(
            spark.createDataFrame(
                [e.model_dump() for e in evidence], schema=spark_schema_for(LlmEvidenceRecord)
            ),
            lakehouse,
            llm_run.EVIDENCE_TABLE,
            mode="overwrite",
        )
        build = perf.run(spark, lakehouse)
        assert build.written == 2
        rows = perf.by_submission(spark, lakehouse)
        assert rows["K250001"]["sensitivity"] == pytest.approx(0.925)
        assert rows["K250009"]["reports_sensitivity_specificity"] is None

        seed = tmp_path / "seed.yaml"
        seed.write_text(
            """\
version: 1
reviewed:
  - submission_number: K250001
    mortality_confirmed: true
    review_method: human
    intended_use_text: Predicts risk of cardiac mortality.
    intended_use_source: https://example.test/src
"""
        )
        settings = lakehouse.model_copy(
            update={"config_dir": tmp_path, "mortality_seed_filename": "seed.yaml"}
        )
        assert mortality_seed.run(spark, settings) == 1
        (row,) = tables.read_table(spark, settings, mortality_seed.EVIDENCE_TABLE).collect()
        assert row["specificity"] == pytest.approx(0.97)
        assert row["performance_run_id"] == evidence[0].run_id
        assert set(spark_schema_for(EvidenceRecord).fieldNames()) <= set(row.asDict())

    def test_without_evidence_it_says_what_to_run(self, spark, lakehouse):
        with pytest.raises(ValueError, match="build-evidence"):
            perf.run(spark, lakehouse)
