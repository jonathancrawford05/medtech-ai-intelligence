"""The curation-candidates report and the stage-1 agreement measure (PR 4C; ADR 0021).

Real where it can be: the committed `config/mortality_seed.yaml` (the 133 curated
judgements) and the device statements read from the recorded Summaries. Four of
those documents are curated negatives (K210484, K231010, K232699, K250507), which
makes them a real slice of the "negatives flagged" measure. Silver rows are
synthetic (built with `tests.monitor_fixtures.device`).
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path

import pytest

from registry.config.settings import Settings
from registry.mart import curation_candidates as cc
from registry.schemas import EvidenceRecord
from registry.transform import document_extraction as dx
from registry.transform import intended_use as iu
from registry.transform import mortality_seed
from tests.monitor_fixtures import device

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "summary_documents"
SEED = Path(__file__).parents[1] / "config" / "mortality_seed.yaml"
NOW = dt.datetime(2026, 10, 11, 12, 0, 0)
CARDIOMETABOLIC = frozenset({"cardiovascular", "metabolic"})


def _statement(number: str) -> dict:
    """The `silver_intended_use` row for a recorded Summary, as `run` would write it."""
    for path in (FIXTURE_DIR / f"{number}.json", FIXTURE_DIR / "pccp" / f"{number}.json"):
        if path.exists():
            row = json.loads(path.read_text())
            roles = dx.extract_document(row, now=NOW).model_dump()
            return iu.extract_document(row, roles, now=NOW).model_dump()
    raise FileNotFoundError(number)


def _rows(*statements: tuple[str, str]) -> dict[str, dict]:
    """Synthetic statements: (submission, text) -> a `silver_intended_use` row."""
    return {
        key: {
            "intended_use_text": text,
            "intended_use_basis": "form_3881",
            "intended_use_pages": [3],
            "source_url": f"https://example.test/{key}.pdf",
        }
        for key, text in statements
    }


class TestKeywordHits:
    def test_the_terms_behind_a_flag(self):
        text = "Predicts mortality and hemodynamic instability; MORTALITY again."
        assert mortality_seed.keyword_hits(text) == ["mortality", "hemodynamic instability"]

    def test_no_hit_is_empty(self):
        assert mortality_seed.keyword_hits("Detects atrial fibrillation.") == []
        assert mortality_seed.keyword_hits("") == []


class TestCandidates:
    def _devices(self):
        return [
            device("K1", "s", category="cardiovascular", decided=dt.date(2025, 1, 1)),
            device("K2", "s", category="cardiovascular", decided=dt.date(2026, 1, 1)),
            device("K3", "s", category="radiology"),
            device("K4", "s", category="cardiovascular"),
            device("K5", "s", category="cardiovascular"),
        ]

    def _build(self, statements, reviewed=frozenset()):
        return cc.candidates(
            self._devices(), statements, reviewed=set(reviewed), categories=CARDIOMETABOLIC
        )

    def test_cardiometabolic_hits_not_yet_reviewed_newest_first(self):
        statements = _rows(
            ("K1", "Estimates the risk of death."),
            ("K2", "Predicts hemodynamic instability."),
            ("K3", "Predicts mortality."),  # radiology, no cardiovascular language
            ("K4", "Detects atrial fibrillation."),  # no stage-1 term
            ("K5", "Predicts mortality."),  # already curated
        )
        found = self._build(statements, reviewed={"K5"})
        assert [c.submission_number for c in found] == ["K2", "K1"]
        assert found[0].keyword_hits == ["hemodynamic instability"]
        assert found[1].keyword_hits == ["risk of death"]
        assert {c.scope for c in found} == {"specialty"}

    def test_cardiovascular_language_brings_in_other_specialties(self):
        """Finding 0022: 18 coronary-plaque tools are cleared through Radiology, so a
        specialty-only scope never offered them. Their statement says "coronary"."""
        statements = _rows(
            ("K3", "Quantifies coronary artery plaque from CCTA."),  # in, by language
        )
        (only,) = self._build(statements)
        assert (only.submission_number, only.scope) == ("K3", "statement")
        assert only.keyword_hits == ["plaque"]

    @pytest.mark.parametrize(
        "text",
        [
            "Detects dental plaque and caries on intraoral scans.",  # TRIOS Dx shape
            "Flags intracranial hemorrhage risk on head CT.",  # stage-1 hit, no heart
        ],
    )
    def test_other_specialties_need_cardiovascular_language(self, text):
        assert self._build(_rows(("K3", text))) == []

    def test_each_candidate_carries_its_evidence(self):
        statements = {"K2": {**_statement("K232699"), "intended_use_text": "Predicts death."}}
        (only,) = self._build(statements)
        assert only.intended_use_basis == "form_3881"
        assert only.intended_use_pages == [3]
        assert only.source_url.endswith("/pdf23/K232699.pdf")
        assert only.intended_use_text == "Predicts death."

    def test_a_device_with_no_statement_is_not_a_candidate(self):
        """No statement read is not evidence: nothing to curate from yet."""
        statements = {"K2": {**_rows(("K2", "x"))["K2"], "intended_use_text": None}}
        assert self._build(statements) == []

    def test_metabolic_is_in_scope(self):
        devices = [device("K9", "s", category="cardiovascular")]
        devices[0]["specialty_category"] = "metabolic"
        found = cc.candidates(
            devices,
            _rows(("K9", "Hypotension prediction.")),
            reviewed=set(),
            categories=CARDIOMETABOLIC,
        )
        assert [c.submission_number for c in found] == ["K9"]

    def test_the_real_seed_excludes_what_it_reviewed(self):
        """K232699 is a curated negative in the committed seed: whatever its text
        says, it is not "next batch"."""
        reviewed = {r.submission_number for r in mortality_seed.load(Settings())}
        assert "K232699" in reviewed
        statements = {"K232699": {**_statement("K232699"), "intended_use_text": "death"}}
        devices = [device("K232699", "s", category="cardiovascular")]
        assert (
            cc.candidates(devices, statements, reviewed=reviewed, categories=CARDIOMETABOLIC) == []
        )

    def test_the_csv_holds_every_column(self, tmp_path):
        found = self._build(_rows(("K2", "Predicts death and mortality.")))
        out = tmp_path / "batch.csv"
        cc.write_csv(found, out)
        (row,) = list(csv.DictReader(out.open()))
        assert list(row) == cc.CSV_COLUMNS
        assert row["keyword_hits"] == "death; mortality"
        assert row["intended_use_pages"] == "3"


def _judgement(key: str, confirmed: bool, text: str) -> EvidenceRecord:
    return EvidenceRecord(
        submission_number=key,
        intended_use_text=text,
        intended_use_source="https://example.test",
        mortality_keyword_flag=mortality_seed.keyword_flag(text),
        mortality_confirmed_flag=confirmed,
        mortality_review_method="llm_assisted",
    )


class TestKeywordAgreement:
    def test_the_committed_seed_reproduces_finding_0014_on_curated_text(self):
        """The baseline the 4C measure is read against: 0 / 11 confirmed missed, 1
        negative (K192732) flagged, over the 133 curated devices."""
        out = cc.keyword_agreement(mortality_seed.load(Settings()), {})
        assert (out.curated, out.confirmed, out.negatives) == (133, 11, 122)
        assert out.disagrees_on_curated == []
        assert out.negatives_flagged_on_curated == ["K192732"]
        assert len(out.confirmed_without_statement) == 11
        assert out.with_statement == 0

    def test_four_real_negatives_stay_unflagged_on_their_own_statement(self):
        """Real text: the device statements of the four curated negatives in the
        recorded slice trip no stage-1 term, like their curated excerpts."""
        keys = ("K210484", "K231010", "K232699", "K250507")
        seed = [r for r in mortality_seed.load(Settings()) if r.submission_number in keys]
        statements = {k: _statement(k)["intended_use_text"] for k in keys}
        out = cc.keyword_agreement(seed, statements)
        assert (out.negatives, out.with_statement) == (4, 4)
        assert out.negatives_flagged_on_statement == []
        assert out.negatives_flagged_on_curated == []

    def test_each_side_is_measured_separately(self):
        seed = [
            _judgement("K1", True, "Estimates mortality."),  # both hit
            _judgement("K2", True, "Estimates mortality."),  # statement misses
            _judgement("K3", True, "Index of patient status."),  # curated misses
            _judgement("K4", True, "Estimates mortality."),  # no statement read
            _judgement("K5", False, "Detects AF."),  # statement flags a negative
        ]
        statements = {
            "K1": "Estimates mortality.",
            "K2": "Displays vital signs.",
            "K3": "Index based on the statistical mortality risk.",
            "K5": "Detects AF in patients at risk of sudden cardiac death.",
        }
        out = cc.keyword_agreement(seed, statements)
        assert out.disagrees_on_curated == ["K3"]
        assert out.disagrees_on_statement == ["K2"]
        assert out.confirmed_without_statement == ["K4"]
        assert out.negatives_flagged_on_statement == ["K5"]
        assert out.negatives_flagged_on_curated == []
        lines = "\n".join(out.lines())
        assert "on the device's statement    : 1 / 3  K2" in lines
        assert "[finding 0014: 0 / 11]" in lines

    def test_an_unjudged_entry_is_not_counted(self):
        unjudged = EvidenceRecord(
            submission_number="K1",
            intended_use_text="Estimates mortality.",
            mortality_keyword_flag=True,
        )
        assert cc.keyword_agreement([unjudged], {}).curated == 0


@pytest.mark.spark
class TestBuild:
    def test_reads_silver_the_statements_and_the_seed(self, spark, lakehouse):
        from registry import tables
        from registry.schemas import DeviceRecord, IntendedUseRecord, spark_schema_for

        silver = [
            device("K700001", "s", category="cardiovascular"),
            device("K232699", "s", category="cardiovascular"),  # curated negative
            device("K700002", "s", category="radiology"),
        ]
        tables.write_table(
            spark.createDataFrame(silver, spark_schema_for(DeviceRecord)),
            lakehouse,
            "silver_devices",
            mode="overwrite",
        )
        statements = [
            IntendedUseRecord(
                submission_number=key,
                extractor_version=iu.INTENDED_USE_VERSION,
                extracted_at=NOW,
                source_content_sha256="ab" * 32,
                segmentation_version=dx.EXTRACTOR_VERSION,
                intended_use_text="Predicts in-hospital mortality.",
                intended_use_basis="summary_section",
                intended_use_pages=[5],
            ).model_dump()
            for key in ("K700001", "K232699", "K700002")
        ]
        tables.write_table(
            spark.createDataFrame(statements, spark_schema_for(IntendedUseRecord)),
            lakehouse,
            iu.INTENDED_USE_TABLE,
            mode="overwrite",
        )
        assert lakehouse.mortality_seed_path == SEED  # the committed seed

        report = cc.build(spark, lakehouse)

        assert [c.submission_number for c in report.candidates] == ["K700001"]
        assert (report.devices_in_scope, report.in_scope_with_statement) == (2, 2)
        assert report.agreement.negatives_flagged_on_statement == ["K232699"]

    def test_says_what_to_run_first(self, spark, lakehouse):
        with pytest.raises(ValueError, match="build-silver"):
            cc.build(spark, lakehouse)
