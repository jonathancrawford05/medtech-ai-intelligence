"""openFDA enrichment (ADR 0013).

Two tiers that succeed or fail independently: the `classification` endpoint keyed
by product code (181 codes cover all 1,614 devices, so this is where
`device_class` comes from), and `510k`/`pma` keyed by submission number for the
per-device facts a product code cannot carry.

These tests drive a fake client rather than the network. The client itself is
already fixture-verified against real openFDA responses (ADR 0010); what is
unproven here is the *fan-out*, the class mapping, and the claim that tier 1 is
the cheap tier -- so that is what is asserted.
"""

from __future__ import annotations

import datetime as dt

import pytest

from registry.ingest.openfda_client import OpenFdaClassification, OpenFdaDevice
from registry.transform import enrichment


def _device(submission: str, **overrides) -> OpenFdaDevice:
    base = {
        "submission_number": submission,
        "endpoint": "510k",
        "product_code": "QAS",
        "device_name": f"Device {submission}",
        "applicant": "ACME MEDICAL, INC.",
        "decision_date": "2026-06-29",
        "decision_code": "SESE",
        "device_class": "2",
        "regulation_number": "892.2050",
        "advisory_committee_description": "Radiology",
        "medical_specialty_description": "Radiology",
        "supplement_number": None,
        "raw": {
            "date_received": "2025-11-19",
            "decision_description": "Substantially Equivalent",
            "clearance_type": "Traditional",
            "statement_or_summary": "Summary",
            "third_party_flag": "N",
            "expedited_review_flag": "",
        },
    }
    base.update(overrides)
    return OpenFdaDevice(**base)


def _classification(product_code: str, **overrides) -> OpenFdaClassification:
    base = {
        "product_code": product_code,
        "device_name": "Automated radiological image processing software",
        "device_class": "2",
        "regulation_number": "892.2050",
        "medical_specialty_description": "Radiology",
        "definition": "To provide automated radiological image processing tools.",
        "raw": {"life_sustain_support_flag": "N", "implant_flag": "N", "unclassified_reason": ""},
    }
    base.update(overrides)
    return OpenFdaClassification(**base)


class FakeClient:
    """Records every call so the tests can assert on fan-out, not just results."""

    def __init__(self, devices=None, classifications=None):
        self._devices = devices or {}
        self._classifications = classifications or {}
        self.submission_calls: list[str] = []
        self.classification_calls: list[str] = []

    def fetch_submission(self, submission_number):
        self.submission_calls.append(submission_number)
        return self._devices.get(submission_number)

    def fetch_classification(self, product_code):
        self.classification_calls.append(product_code)
        return self._classifications.get(product_code)


SILVER_ROWS = [
    {"submission_number": "K1", "product_code": "QAS", "decision_date": dt.date(2026, 6, 29)},
    {"submission_number": "K2", "product_code": "QAS", "decision_date": dt.date(2026, 6, 29)},
    {"submission_number": "K3", "product_code": "QIH", "decision_date": dt.date(2026, 6, 29)},
]


class TestDeviceClassMapping:
    """ADR 0013 Decision 3: the mapping must preserve ADR 0012's None/unclassified
    distinction, so it is a named function with a negative case, not a dict get."""

    @pytest.mark.parametrize(("raw", "expected"), [("1", "I"), ("2", "II"), ("3", "III")])
    def test_maps_the_numeric_classes(self, raw, expected):
        assert enrichment.map_device_class(raw, unclassified_reason=None) == expected

    def test_the_fdas_own_unclassified_designation_maps_to_unclassified(self):
        assert enrichment.map_device_class("U", unclassified_reason="Pre-amendment") == (
            "unclassified"
        )

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_absent_means_not_enriched_not_unclassified(self, raw):
        """The distinction ADR 0012 exists to preserve: None != "unclassified"."""
        assert enrichment.map_device_class(raw, unclassified_reason=None) is None

    def test_an_unrecognised_class_is_none_rather_than_a_guess(self):
        assert enrichment.map_device_class("7", unclassified_reason=None) is None

    def test_an_unclassified_reason_alone_is_enough(self):
        """openFDA sometimes carries the reason with a blank class."""
        assert enrichment.map_device_class("", unclassified_reason="Pre-amendment") == (
            "unclassified"
        )


class TestFlagParsing:
    @pytest.mark.parametrize(("raw", "expected"), [("Y", True), ("y", True), ("N", False)])
    def test_reads_the_fdas_y_n_flags(self, raw, expected):
        assert enrichment.parse_flag(raw) is expected

    @pytest.mark.parametrize("raw", [None, "", "  ", "unknown"])
    def test_an_absent_flag_is_none_not_false(self, raw):
        """Same rule as everywhere else here: unknown is not a negative finding."""
        assert enrichment.parse_flag(raw) is None


class TestBuildEnrichment:
    def test_queries_each_product_code_once_not_each_device(self):
        """ADR 0013 Decision 2 -- the whole reason tier 1 is keyed by product code.
        If this regresses the pass silently costs an order of magnitude more calls."""
        client = FakeClient(classifications={"QAS": _classification("QAS")})
        enrichment.build_records(SILVER_ROWS, client)
        assert sorted(client.classification_calls) == ["QAS", "QIH"]

    def test_populates_device_class_from_the_classification_tier(self):
        client = FakeClient(classifications={"QAS": _classification("QAS", device_class="3")})
        records = {r.submission_number: r for r in enrichment.build_records(SILVER_ROWS, client)}
        assert records["K1"].device_class_raw == "3"
        assert records["K1"].classification_found is True
        assert enrichment.map_device_class("3", unclassified_reason=None) == "III"

    def test_derives_review_time_from_date_received_and_decision_date(self):
        client = FakeClient(devices={"K1": _device("K1")})
        records = {r.submission_number: r for r in enrichment.build_records(SILVER_ROWS, client)}
        # 2025-11-19 -> 2026-06-29
        assert records["K1"].date_received == dt.date(2025, 11, 19)
        assert records["K1"].review_time_days == 222

    def test_review_time_is_none_when_the_date_is_missing_not_zero(self):
        client = FakeClient(devices={"K1": _device("K1", raw={})})
        records = {r.submission_number: r for r in enrichment.build_records(SILVER_ROWS, client)}
        assert records["K1"].date_received is None
        assert records["K1"].review_time_days is None

    def test_records_the_summary_flag_that_scopes_the_future_pdf_pass(self):
        client = FakeClient(
            devices={
                "K1": _device("K1"),
                "K2": _device("K2", raw={"statement_or_summary": "Statement"}),
            }
        )
        records = {r.submission_number: r for r in enrichment.build_records(SILVER_ROWS, client)}
        assert records["K1"].statement_or_summary == "Summary"
        assert records["K2"].statement_or_summary == "Statement"

    def test_a_row_is_still_written_when_both_tiers_miss(self):
        """A miss is a fact worth storing -- otherwise a re-run refetches it forever."""
        records = {
            r.submission_number: r for r in enrichment.build_records(SILVER_ROWS, FakeClient())
        }
        assert set(records) == {"K1", "K2", "K3"}
        assert records["K1"].submission_found is False
        assert records["K1"].classification_found is False
        assert records["K1"].device_class_raw is None

    def test_the_two_tiers_are_independent(self):
        """Tier 1 succeeding must not be reported as tier 2 succeeding, or coverage
        for `device_class` and for review time become indistinguishable."""
        client = FakeClient(classifications={"QAS": _classification("QAS")})
        records = {r.submission_number: r for r in enrichment.build_records(SILVER_ROWS, client)}
        assert records["K1"].classification_found is True
        assert records["K1"].submission_found is False

    def test_a_failed_product_code_lookup_is_not_retried_per_device(self):
        client = FakeClient()
        enrichment.build_records(SILVER_ROWS, client)
        assert client.classification_calls.count("QAS") == 1

    def test_stamps_every_record_with_one_enriched_at(self):
        records = list(enrichment.build_records(SILVER_ROWS, FakeClient()))
        assert len({r.enriched_at for r in records}) == 1


class TestJoinIntoSilver:
    """`build-silver` must stay offline and deterministic (ADR 0013 Decision 1):
    it reads the enrichment table if one exists, and never calls openFDA."""

    pytestmark = pytest.mark.spark

    @pytest.mark.spark
    def test_device_class_is_populated_from_the_enrichment_table(self, spark, lakehouse):
        from registry import tables
        from registry.schemas import DeviceEnrichmentRecord, spark_schema_for

        rows = [
            DeviceEnrichmentRecord(
                submission_number="K1",
                enriched_at=dt.datetime(2026, 9, 15, 9, 0),
                submission_found=True,
                classification_found=True,
                device_class_raw="2",
            ).model_dump(),
            DeviceEnrichmentRecord(
                submission_number="K2",
                enriched_at=dt.datetime(2026, 9, 15, 9, 0),
                submission_found=False,
                classification_found=False,
            ).model_dump(),
        ]
        tables.write_table(
            spark.createDataFrame(rows, spark_schema_for(DeviceEnrichmentRecord)),
            lakehouse,
            enrichment.ENRICHMENT_TABLE,
            mode="overwrite",
        )
        classes = enrichment.device_classes_by_submission(spark, lakehouse)
        assert classes == {"K1": "II"}, "an unenriched row must be absent, not mapped to a default"

    @pytest.mark.spark
    def test_a_missing_enrichment_table_is_not_an_error(self, spark, lakehouse):
        """Silver built fine before enrichment existed and must still build fine."""
        assert enrichment.device_classes_by_submission(spark, lakehouse) == {}
