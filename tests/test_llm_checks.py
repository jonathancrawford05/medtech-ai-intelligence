"""The deterministic checks (PR 4D; ADR 0022 Decision 4), on the recorded spike slice.

Real text and real model answers: ``tests/fixtures/llm_extraction`` holds the pages
the model was sent and what ``qwen3:14b`` returned (prompt ``4d-spike-4``). Where a
test needs a value the recorded answers do not contain (a one-sided interval in the
new schema, a threshold marked ``is_result=false``), it builds a candidate by hand
against a real page, and says so.
"""

from __future__ import annotations

import csv
import json
import typing as t
from pathlib import Path

import pytest

from registry import schemas
from registry.llm_extraction import checks as C
from registry.llm_extraction.pages import Page, select_pages

LLM = Path(__file__).parent / "fixtures" / "llm_extraction"


def _pages(number: str) -> list[Page]:
    doc = json.loads((LLM / "documents" / f"{number}.json").read_text())
    return [Page(p["page"], p["text"]) for p in doc["pages"]]


def _response(number: str) -> dict:
    return json.loads((LLM / "responses" / f"{number}.json").read_text())


def _checked(number: str, pages: list[Page] | None = None) -> list[C.Checked]:
    r = _response(number)
    return C.check_response(r["content"], r["prompt_version"], pages or _pages(number))


def _metrics(number: str) -> list[C.Checked]:
    return [c for c in _checked(number) if c.candidate.field == "metric"]


def _gold() -> list[dict]:
    with (LLM / "gold_set_v1.csv").open(newline="") as fh:
        return list(csv.DictReader(fh))


def _gold_match(row: dict) -> C.Checked:
    """The recorded value a gold row judged (same figure, measure, setting and label)."""
    for c in _checked(row["submission_number"]):
        cand = c.candidate
        if c.status == "duplicate":
            continue
        if row["field"].startswith("metric:"):
            metric, setting = row["field"].removeprefix("metric:").split("/")
            if (
                cand.field == "metric"
                and cand.metric == metric
                and cand.setting == setting
                and cand.label == row["label"]
                and cand.value_number == float(row["value"])
            ):
                return c
        elif cand.field == row["field"] and cand.value_text == row["value"].lower():
            return c
    raise AssertionError(f"gold row {row['row']} has no recorded value")


class TestStatusVocabulary:
    def test_the_statuses_are_the_schema_literal(self):
        assert set(t.get_args(schemas.CheckStatus)) == C.STATUSES

    def test_accept_reject_and_drop_partition_the_statuses(self):
        assert C.ACCEPT.isdisjoint(C.REJECT) and C.ACCEPT.isdisjoint(C.DROP)
        assert C.REVIEW <= C.ACCEPT

    def test_the_silver_validator_agrees_on_what_is_accepted(self):
        assert schemas._ACCEPTED_STATUSES == C.ACCEPT


class TestNormalisation:
    def test_strict_folds_case_dashes_quotes_and_spacing(self):
        assert C.strict(
            "Sensitivity  \N{LEFT DOUBLE QUOTATION MARK}93.6%\N{RIGHT DOUBLE QUOTATION MARK} (90.9 \N{EN DASH} 95.6)"
        ) == ('sensitivity "93.6%" (90.9 - 95.6)')

    def test_control_characters_read_as_spaces(self):
        assert C.strict("a\x01b") == "a b"

    def test_loose_keeps_letters_digits_percent_and_point(self):
        assert C.loose("75 .9% per -protocol") == "75.9%perprotocol"

    def test_numbers_close_an_artefact_space_at_the_point(self):
        """K213657's text layer writes 71.8% as "71. 8%"."""
        assert 71.8 in C.numbers_in("79.4% (95% CI: 71. 8%-86.2%)")

    def test_numbers_read_thousands_separators(self):
        assert C.numbers_in("2,460 unique patients") == [2460.0]


class TestPortedLadder:
    """The spike scorer's statuses, recomputed: the gold set recorded each row's."""

    def test_reproduces_the_spike_status_on_57_of_58_gold_rows(self):
        mismatches = [
            (r["row"], r["check_status"], _gold_match(r).quote_status)
            for r in _gold()
            if _gold_match(r).quote_status != r["check_status"]
        ]
        # Row 11 (K250151, "Data Source Center 4 ..."): the header run sits ~180
        # normalised characters before "center 4", past the 150 gap, so the row is
        # found by its numbers instead. Both statuses accept and flag for review.
        assert mismatches == [("11", "ok_gapped", "ok_numbers")]

    def test_the_one_mismatch_makes_the_same_decision(self):
        row = next(r for r in _gold() if r["row"] == "11")
        checked = _gold_match(row)
        assert checked.accepted and checked.needs_review

    def test_the_page_is_where_the_quote_was_found(self):
        """Never the model's word: the spike asked for pages and was wrong a third of the time."""
        row = next(r for r in _gold() if r["row"] == "1")  # Withings, p13
        assert _gold_match(row).pages == [13]

    def test_a_gapped_quote_across_a_page_break_spans_both_pages(self):
        """K251221 row 9: the sentence breaks over the page footer between p9 and p10."""
        row = next(r for r in _gold() if r["row"] == "9")
        checked = _gold_match(row)
        assert checked.quote_status == "ok_gapped"
        assert checked.pages == [9, 10]

    def test_loose_matching_absorbs_pdf_spacing(self):
        """K223073 row 29: "multi -center" in the text layer."""
        row = next(r for r in _gold() if r["row"] == "29")
        assert _gold_match(row).quote_status == "ok_loose"


class TestFabricationIsRejected:
    """Finding 0023 failure modes 1 and 3: every invented figure must die here."""

    def test_k251218_every_invented_result_is_rejected(self):
        """A test list without results: five ~98% "other" metrics, no number in any quote."""
        metrics = _metrics("K251218")
        assert len(metrics) == 5
        assert {m.status for m in metrics} == {"value_not_in_quote"}
        assert not any(m.accepted for m in metrics)

    def test_k233253_the_image_table_figures_are_rejected(self):
        """eCART: "test characteristics ... are shown in" a figure; the model wrote AUCs."""
        metrics = _metrics("K233253")
        assert [m.candidate.value_number for m in metrics] == [0.87, 0.86, 0.87, 0.86, 0.87, 0.86]
        assert {m.status for m in metrics} == {"quote_not_found", "duplicate"}
        assert not any(m.accepted for m in metrics)

    def test_k233253_the_invented_cohort_size_is_rejected(self):
        size = next(c for c in _checked("K233253") if c.candidate.field == "sample_size")
        assert size.candidate.value_number == 120_000
        assert size.status == "quote_not_found"

    def test_k182456_the_cipher_page_never_reaches_the_model(self):
        """Both located pages are a font cipher: nothing is sent, so nothing is extracted."""
        doc = json.loads((LLM / "documents" / "K182456.json").read_text())
        texts = {p["page"]: p["text"] for p in doc["pages"]}
        page_texts = [texts.get(n, "") for n in range(1, max(texts) + 1)]
        selection = select_pages(page_texts, texts, max_chars=40_000)
        assert selection.pages == []
        assert selection.garbled == [7, 8]

    def test_k182456_even_unfiltered_its_figures_are_rejected(self):
        """Belt and braces: the three 98.1% metrics read off the cipher fail the checks."""
        metrics = _metrics("K182456")
        assert [m.candidate.value_number for m in metrics] == [98.1, 98.1, 98.1]
        assert not any(m.accepted for m in metrics)

    def test_k182456_unfiltered_the_cipher_quote_would_pass_as_text(self):
        """Why the garble test exists: a copied cipher sentence *is* a substring of
        the page, so the quote check alone accepts the endpoint read off it."""
        endpoint = next(c for c in _checked("K182456") if c.candidate.field == "clinical_endpoint")
        assert endpoint.accepted

    def test_no_recorded_fabrication_survives_anywhere(self):
        """Every metric in the three fabrication documents, across every status."""
        for number in ("K251218", "K233253", "K182456"):
            assert not [m for m in _metrics(number) if m.accepted], number


class TestRulesTheGoldSetAdded:
    def test_a_metric_whose_quote_and_label_never_name_it_is_rejected(self):
        """The six "name looks wrong" gold rows: 0 / 6 were right."""
        rows = [r for r in _gold() if r["why_sampled"] == "metric: name looks wrong"]
        assert len(rows) == 6
        assert {_gold_match(r).status for r in rows} == {"measure_not_named"}

    def test_the_named_measure_rule_never_rejects_a_plain_accept(self):
        rows = [r for r in _gold() if r["why_sampled"] == "metric: plain accept"]
        assert len(rows) == 8
        assert all(_gold_match(r).accepted for r in rows)

    def test_a_label_naming_the_measure_is_enough(self):
        """K243866 row 8: the quote is a bare table row ("2% 0.558 0.992"), the label says PPV."""
        row = next(r for r in _gold() if r["row"] == "8")
        assert _gold_match(row).status == "ok"

    def test_short_measure_names_must_stand_alone(self):
        cand = C.Candidate(
            "metric", "Caucasian patients 0.91", metric="auc", label="x", value_number=0.91
        )
        assert not C.names_measure(cand)
        assert C.names_measure(C.Candidate("metric", "AUC 0.91", metric="auc", label="x"))

    def test_a_zero_triple_is_rejected(self):
        """K250151 row 14: "0 (0-0)" built from a "-" cell."""
        row = next(r for r in _gold() if r["row"] == "14")
        checked = _gold_match(row)
        assert checked.quote_status == "ok_numbers"
        assert checked.status == "zero_triple"

    def test_an_invented_upper_bound_is_rejected(self):
        """K182149: "93.5% with a lower one-sided 95% CI of 87.8%"; the model wrote 96.6 too."""
        sens = next(m for m in _metrics("K182149") if m.candidate.metric == "sensitivity")
        assert (sens.candidate.ci_low, sens.candidate.ci_high) == (87.8, 96.6)
        assert sens.status == "value_not_in_quote"

    def test_a_one_sided_bound_in_the_new_schema_is_accepted(self):
        """Hand-built (the recorded answers predate ci_sided) against K182149's real page."""
        cand = C.Candidate(
            "metric",
            "The per vessel sensitivity of FFRangio™ was 93.5% with a lower one-sided 95% CI of 87.8%.",
            metric="sensitivity",
            label="sensitivity",
            value_number=93.5,
            ci_low=87.8,
            ci_sided="lower",
            applies_to="subject_device",
            setting="standalone",
            is_result=True,
        )
        (checked,) = C.check([cand], C.DocumentText(_pages("K182149")))
        assert checked.status == "ok"

    def test_ci_side_must_agree_with_the_bounds(self):
        cand = C.Candidate(
            "metric",
            "The per vessel sensitivity of FFRangio™ was 93.5% with a lower one-sided 95% CI of 87.8%.",
            metric="sensitivity",
            label="sensitivity",
            value_number=93.5,
            ci_low=87.8,
            ci_sided="two",
            is_result=True,
        )
        (checked,) = C.check([cand], C.DocumentText(_pages("K182149")))
        assert checked.status == "ci_inconsistent"

    def test_a_threshold_marked_as_not_a_result_is_rejected(self):
        """K241633 row 19: "within +/- 3 BPM ARMS" is the hypothesis, not the achieved value."""
        cand = C.Candidate(
            "metric",
            "The hypothesis that the IVC App can measure PR within ± 3 BPM ARMS is accepted.",
            metric="arms",
            label="ARMS",
            value_number=3,
            is_result=False,
        )
        (checked,) = C.check([cand], C.DocumentText(_pages("K241633")))
        assert checked.quote_status == "ok"
        assert checked.status == "not_a_result"

    def test_the_same_figure_named_as_its_own_measure_is_accepted(self):
        """The ARMS row, named as the schema now asks, passes the name rule."""
        cand = C.Candidate(
            "metric",
            "The hypothesis that the IVC App can measure PR within ± 3 BPM ARMS is accepted.",
            metric="arms",
            label="ARMS",
            value_number=3,
            is_result=True,
        )
        (checked,) = C.check([cand], C.DocumentText(_pages("K241633")))
        assert checked.status == "ok"


class TestDuplicatesAndScalars:
    def test_the_runaway_documents_exact_duplicates_are_dropped(self):
        """K230842: 78 metric rows, 58 of them exact duplicates (finding 0023 failure mode 7)."""
        metrics = _metrics("K230842")
        assert len(metrics) == 78
        assert sum(m.status == "duplicate" for m in metrics) == 58

    def test_a_false_without_a_quote_is_rejected(self):
        """K191370: `false` for multi-site and demographics, no sentence behind either."""
        unsupported = {
            c.candidate.field: c.status
            for c in _checked("K191370")
            if c.candidate.field in ("multi_site", "demographics_disclosed")
        }
        assert unsupported == {"multi_site": "no_quote", "demographics_disclosed": "no_quote"}

    def test_performance_not_reported_needs_no_quote(self):
        cand = C.Candidate("performance_reported", None, value_text="false", value_bool=False)
        (checked,) = C.check([cand], C.DocumentText(_pages("K171056")))
        assert checked.status == "absent" and checked.accepted

    def test_but_not_when_metrics_were_listed(self):
        metric = C.Candidate("metric", "x", metric="other", label="x", value_number=1.0)
        absent = C.Candidate("performance_reported", None, value_text="false", value_bool=False)
        checked = C.check([metric, absent], C.DocumentText(_pages("K171056")))
        assert checked[1].status == "no_quote"

    def test_a_sample_size_must_be_written_in_its_quote(self):
        """K230812: 321 proposed, the quote says 274 patients."""
        size = next(c for c in _checked("K230812") if c.candidate.field == "sample_size")
        assert size.candidate.value_number == 321
        assert size.status == "value_not_in_quote"

    def test_a_rescaled_value_is_accepted_but_flagged(self):
        cand = C.Candidate(
            "metric", "AUC 0.95", metric="auc", label="AUC", value_number=95.0, is_result=True
        )
        doc = C.DocumentText([Page(1, "The AUC 0.95 was observed on the hold-out set.")])
        (checked,) = C.check([cand], doc)
        assert checked.status == "ok_rescaled"
        assert checked.accepted and checked.needs_review


class TestCandidates:
    def test_spike_answers_carry_no_attribution_and_infer_the_side(self):
        cands = C.candidates(_response("K161772")["content"], C.SPIKE_PROMPT_VERSION)
        lower = next(c for c in cands if c.field == "metric" and c.ci_high is None and c.ci_low)
        assert lower.ci_sided == "lower"
        assert lower.applies_to is None and lower.is_result is None

    def test_null_and_not_stated_scalars_are_not_candidates(self):
        cands = C.candidates(_response("K182456")["content"], C.SPIKE_PROMPT_VERSION)
        assert {c.field for c in cands} == {"metric", "performance_reported", "clinical_endpoint"}

    def test_current_prompt_answers_must_match_the_schema(self):
        bad = json.dumps({"performance_reported": {"value": True, "quote": "x"}})
        with pytest.raises(ValueError):
            C.candidates(bad, "4d.1")
