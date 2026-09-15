"""The gold mortality-relevant mart (ADR 0007 / 0014).

This is the registry's actual deliverable: the ranked list an underwriter reads.
ADR 0007's cost clause is the thing under test -- the mart filters on the
*confirmed* flag, so a device nobody has reviewed is excluded even if stage 1
fired on it. Under-inclusion is recoverable; silent over-inclusion is not.
"""

from __future__ import annotations

import datetime as dt

import pytest

from registry import tables
from registry.mart import mortality_relevant
from registry.schemas import DeviceRecord, EvidenceRecord, spark_schema_for

pytestmark = pytest.mark.spark


def _device(submission: str, *, category: str = "cardiovascular", device_class: str | None = "II"):
    return DeviceRecord(
        submission_number=submission,
        device_name=f"Device {submission}",
        applicant_raw="Acme Medical, Inc.",
        applicant_resolved="Acme Medical",
        decision_date=dt.date(2025, 6, 1),
        pathway="510k",
        specialty_panel="Cardiovascular",
        specialty_category=category,
        product_code="QIH",
        device_class=device_class,
        source_url="https://example.test/list",
    ).model_dump()


def _evidence(submission: str, *, confirmed: bool | None, keyword: bool = True, method="human"):
    return EvidenceRecord(
        submission_number=submission,
        intended_use_text="Predicts risk of cardiac mortality.",
        intended_use_source="https://example.test/src",
        mortality_keyword_flag=keyword,
        mortality_confirmed_flag=confirmed,
        mortality_review_method=method if confirmed is not None else None,
    ).model_dump()


@pytest.fixture
def seeded(spark, lakehouse):
    def _seed(devices, evidence):
        tables.write_table(
            spark.createDataFrame(devices, spark_schema_for(DeviceRecord)),
            lakehouse,
            "silver_devices",
            mode="overwrite",
        )
        tables.write_table(
            spark.createDataFrame(evidence, spark_schema_for(EvidenceRecord)),
            lakehouse,
            "silver_evidence",
            mode="overwrite",
        )
        return lakehouse

    return _seed


class TestOnlyConfirmedDevicesQualify:
    def test_a_confirmed_device_is_included(self, spark, seeded):
        settings = seeded([_device("K1")], [_evidence("K1", confirmed=True)])
        rows = mortality_relevant.build(spark, settings)
        assert [r["submission_number"] for r in rows] == ["K1"]

    def test_an_unreviewed_device_is_excluded_even_when_stage_1_fired(self, spark, seeded):
        """ADR 0007's cost clause, stated as a test. A keyword hit is a lead to
        review, never an answer -- shipping it as one is the silent
        over-inclusion the two-stage design exists to prevent."""
        settings = seeded([_device("K1")], [_evidence("K1", confirmed=None, keyword=True)])
        assert mortality_relevant.build(spark, settings) == []

    def test_a_device_with_no_evidence_row_at_all_is_excluded(self, spark, seeded):
        settings = seeded([_device("K1")], [])
        assert mortality_relevant.build(spark, settings) == []

    def test_a_reviewed_and_rejected_device_is_excluded(self, spark, seeded):
        settings = seeded([_device("K1")], [_evidence("K1", confirmed=False)])
        assert mortality_relevant.build(spark, settings) == []

    def test_specialty_does_not_override_the_confirmed_flag(self, spark, seeded):
        """A confirmed device outside the cardiovascular/metabolic categories still
        qualifies: the taxonomy is a starting signal, the judgement is the answer."""
        settings = seeded([_device("K1", category="radiology")], [_evidence("K1", confirmed=True)])
        assert [r["submission_number"] for r in mortality_relevant.build(spark, settings)] == ["K1"]


class TestTheRowAnUnderwriterReads:
    def test_carries_the_provenance_of_the_judgement(self, spark, seeded):
        """The mart must always be able to answer "why is this here?" (ADR 0007)."""
        settings = seeded([_device("K1")], [_evidence("K1", confirmed=True, method="llm_assisted")])
        row = mortality_relevant.build(spark, settings)[0]
        assert row["mortality_review_method"] == "llm_assisted"
        assert row["intended_use_source"] == "https://example.test/src"
        assert "cardiac mortality" in row["intended_use_text"]

    def test_carries_the_commercial_identifiers(self, spark, seeded):
        settings = seeded([_device("K1")], [_evidence("K1", confirmed=True)])
        row = mortality_relevant.build(spark, settings)[0]
        assert row["applicant_resolved"] == "Acme Medical"
        assert row["device_class"] == "II"
        assert row["decision_date"] == dt.date(2025, 6, 1)

    def test_flags_where_the_curator_and_stage_1_disagree(self, spark, seeded):
        """A confirmation with no keyword hit is the row worth a second look --
        either the keyword list is too narrow or the judgement was generous."""
        settings = seeded([_device("K1")], [_evidence("K1", confirmed=True, keyword=False)])
        assert mortality_relevant.build(spark, settings)[0]["keyword_disagrees"] is True

    def test_agreement_is_not_flagged(self, spark, seeded):
        settings = seeded([_device("K1")], [_evidence("K1", confirmed=True, keyword=True)])
        assert mortality_relevant.build(spark, settings)[0]["keyword_disagrees"] is False


class TestRun:
    def test_writes_the_gold_table_and_reports_the_count(self, spark, seeded):
        settings = seeded(
            [_device("K1"), _device("K2")],
            [_evidence("K1", confirmed=True), _evidence("K2", confirmed=False)],
        )
        assert mortality_relevant.run(spark, settings) == 1
        written = tables.read_table(spark, settings, mortality_relevant.MART_TABLE)
        assert written.count() == 1

    def test_an_empty_mart_is_written_not_skipped(self, spark, seeded):
        """Before anyone curates, the mart is legitimately empty -- and an empty
        table a consumer can query beats a missing one they must special-case."""
        settings = seeded([_device("K1")], [])
        assert mortality_relevant.run(spark, settings) == 0
        assert tables.read_table(spark, settings, mortality_relevant.MART_TABLE).count() == 0
