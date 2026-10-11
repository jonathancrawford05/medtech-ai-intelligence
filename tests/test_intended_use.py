"""Device-level intended use (Issue 4, PR 4C; ADR 0021).

Against the real text: the ten-document 4A slice and the eleven recorded PCCP
documents (``tests/fixtures/summary_documents/``), which between them carry every
source the rule reads -- 3881 forms old and new, a pair of forms (K190013), the
eSTAR form (K250507, K252103), Summary sections behind an unreadable form page
(K203469) and with no form at all (K231837, K233692) -- and two documents with no
readable form or Summary (K003301, K241847). Synthetic text only for the edge
cases the slice does not hold (``not_found``), and it says so.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from registry.schemas import IntendedUseRecord, spark_schema_for
from registry.transform import document_extraction as dx
from registry.transform import intended_use as iu

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "summary_documents"
NOW = dt.datetime(2026, 10, 11, 12, 0, 0)


def _row(number: str) -> dict:
    for path in (FIXTURE_DIR / f"{number}.json", FIXTURE_DIR / "pccp" / f"{number}.json"):
        if path.exists():
            return json.loads(path.read_text())
    raise FileNotFoundError(number)


def _roles(row: dict) -> dict:
    """The 4B extraction row for the same fetch, as `run` reads it."""
    return dx.extract_document(row, now=NOW).model_dump()


def _extract(number: str) -> IntendedUseRecord:
    row = _row(number)
    return iu.extract_document(row, _roles(row), now=NOW)


# (submission, basis, pages, first words, last words) -- read by eye off the
# normalised page text.
SLICE = [
    ("K092116", "form_3881", [7], "The EasyCell is intended", "For professional use only."),
    ("K141480", "form_3881", [3], "Cvi42 vascular analysis", "cvi42 is not restricted."),
    ("K141922", "form_3881", [3], "The IRIS Intelligent", "trained healthcare professionals."),
    ("K181892", "form_3881", [3], "This application is image", "application is being offered."),
    ("K190013", "form_3881", [3, 4], "The WellDoc BlueStar", "diagnosis, or treatment."),
    ("K203469", "summary_section", [4, 5], "Al Segmentation uses CT", "approve each structure."),
    ("K243239", "form_3881", [4], "Lung AI software device", "indicated for adults only."),
    ("K250177", "form_3881", [4], "EPIQ: The intended use", "Color M K250177"),
    ("K201992", "form_3881", [3], "The Caption Guidance", "Inferior Vena Cava (SC-IVC)."),
    ("K210484", "summary_section", [5], "The LINQ II ICM", "specifically for pediatric use."),
    ("K231010", "form_3881", [4], "AliveCor's Corvair", "interpreting the ECG signal."),
    ("K231837", "summary_section", [7], "Indications for Use Brainomix", "confirm diagnosis."),
    ("K232699", "form_3881", [3], "The Anumana Low Ejection", "jointly with clinician judgment."),
    ("K233692", "summary_section", [8], "Voluson Signature 18", "hospital or medical clinic."),
    ("K243449", "form_3881", [4], "INFINITT DPS is a software", "hematopathology specimens."),
    ("K250507", "form_estar", [5], "The Hypertension Notification", "absence of hypertension."),
    ("K252103", "form_estar", [4], "Acorn 3D Software is", "with expert clinical judgment."),
    ("K253984", "summary_section", [6], "statement: The Instrument", "indications for use. 5."),
    ("K260406", "form_3881", [4], "Brainomix 360 Hyperdensity", "original images by a physician."),
]


class TestTheRealSlice:
    @pytest.mark.parametrize(("number", "basis", "pages", "first", "last"), SLICE)
    def test_statement(self, number, basis, pages, first, last):
        rec = _extract(number)
        assert (rec.intended_use_basis, rec.intended_use_pages) == (basis, pages)
        assert rec.intended_use_text.startswith(first)
        assert rec.intended_use_text.endswith(last)

    @pytest.mark.parametrize("number", ["K003301", "K241847"])
    def test_no_readable_form_or_summary_is_unknown(self, number):
        """K003301 is scanned; K241847's letter is text but its form and Summary are
        not. Absence there is not "no intended use" (ADR 0012)."""
        rec = _extract(number)
        assert (rec.intended_use_text, rec.intended_use_basis) == (None, "no_text")
        assert rec.intended_use_pages == []

    def test_coverage_of_the_slice(self):
        """19 of 21: every document with a readable form or Summary."""
        records = [_extract(n) for n, *_ in SLICE] + [_extract("K003301"), _extract("K241847")]
        y = iu.summarise(records)
        assert (y.documents, y.found) == (21, 19)
        assert y.basis == {"form_3881": 12, "summary_section": 5, "form_estar": 2, "no_text": 2}

    @pytest.mark.parametrize("number", [n for n, *_ in SLICE])
    def test_the_text_is_verbatim_of_its_pages(self, number):
        """Evidence a curator can find in the PDF: every part of the statement is a
        span of the normalised text of the page(s) it names."""
        rec = _extract(number)
        doc = dx.Document.from_raw(number, _row(number)["page_texts"])
        pages = [doc.pages[p - 1] for p in rec.intended_use_pages]
        if rec.intended_use_basis == "form_3881":
            parts = rec.intended_use_text.split(dx.PAGE_BREAK)
            assert len(parts) == len(pages)
            assert all(part in page for part, page in zip(parts, pages, strict=True))
        else:
            assert rec.intended_use_text in dx.PAGE_BREAK.join(pages)

    def test_the_form_outranks_the_summary_section(self):
        """K243239 states its intended use on the 3881 form (p4) and again in the
        Summary (p6); the form wins (handoff §3)."""
        rec = _extract("K243239")
        assert rec.intended_use_basis == "form_3881"
        assert rec.intended_use_heading == "Indications for Use (Describe)"

    def test_an_unreadable_form_falls_back_to_the_summary(self):
        """K203469's form page is scanned (4B: unreadable); the Summary says it."""
        row = _row("K203469")
        assert _roles(row)["ifu_form_pages"] == []
        assert _extract("K203469").intended_use_heading == "INDICATIONS FOR USE"

    def test_two_forms_both_contribute(self):
        """K190013 files an OTC and an Rx form; only the Rx one names the insulin
        dose calculator."""
        text = _extract("K190013").intended_use_text
        assert text.count("The WellDoc BlueStar") == 2
        assert "insulin dose calculator" in text

    def test_the_old_form_label_is_read(self):
        """Pre-3881: "Indications for Use: The EasyCell ...", not the page title."""
        assert _extract("K092116").intended_use_heading == "Indications for Use:"

    def test_the_estar_statement_is_not_cut_at_its_own_otc_wording(self):
        """K250507 says "intended for over-the-counter (OTC) use"; only the checkbox
        ("Over-The-Counter Use (21 CFR ...)") closes the statement."""
        rec = _extract("K250507")
        assert "over-the-counter (OTC) use by adults age 22" in rec.intended_use_text
        assert "21 CFR" not in rec.intended_use_text
        assert not rec.intended_use_text.startswith("I ")  # the checkbox glyph

    def test_a_heading_pair_reads_as_one(self):
        """K231837: "Intended Use / Indications for Use Brainomix ..." -- the first
        heading alone has no statement; the pair does."""
        rec = _extract("K231837")
        assert rec.intended_use_heading == "Intended Use"
        assert "Brainomix Triage LVO is a notification-only" in rec.intended_use_text

    def test_a_statement_ending_its_page_does_not_take_the_next_page(self):
        """K231837's page 8 opens with the applicant's address, not more statement."""
        assert "Head office" not in _extract("K231837").intended_use_text

    def test_a_statement_broken_mid_sentence_continues(self):
        """K203469 breaks "expert radiation therapy | treatment planners" across pages."""
        assert "treatment planners" in _extract("K203469").intended_use_text

    def test_a_field_label_closes_the_section(self):
        """K233692: "Technology: The Voluson ... same technology as its predicate"
        is comparison text, not intended use."""
        assert "Technology:" not in _extract("K233692").intended_use_text


class TestReusesTheStoredSegmentation:
    def test_pages_are_never_reclassified(self, monkeypatch):
        """The page roles come from `silver_document_extraction`, not a second
        `classify_page` pass (the PR 4C brief)."""
        row = _row("K243239")
        roles = _roles(row)

        def refuse(*_a, **_k):
            raise AssertionError("intended use must not re-classify pages")

        monkeypatch.setattr(dx, "classify_page", refuse)
        monkeypatch.setattr(dx, "segment", refuse)
        rec = iu.extract_document(row, roles, now=NOW)
        assert rec.intended_use_basis == "form_3881"
        assert rec.segmentation_version == dx.EXTRACTOR_VERSION

    def test_the_stored_roles_decide(self):
        """Told page 4 is Summary text, not a form, the same page reads as a
        Summary section: the stored role is what the pass trusts."""
        row = _row("K243239")
        roles = {**_roles(row), "ifu_form_pages": [], "summary_pages": [4, 5, 6, 7, 8, 9, 10, 11]}
        assert iu.extract_document(row, roles, now=NOW).intended_use_basis == "summary_section"

    def test_roles_from_another_fetch_are_refused(self):
        row = _row("K243239")
        roles = {**_roles(row), "source_content_sha256": "00" * 32}
        with pytest.raises(ValueError, match="another fetch"):
            iu.extract_document(row, roles, now=NOW)

    def test_page_kinds_round_trip_through_the_record(self):
        """`page_kinds_from_roles` rebuilds exactly what `segment` stored."""
        for number in ("K092116", "K141480", "K190013", "K203469", "K243239"):
            row = _row(number)
            doc = dx.Document.from_raw(number, row["page_texts"])
            roles = _roles(row)
            assert (
                dx.page_kinds_from_roles(
                    len(doc.pages),
                    letter_pages=roles["letter_pages"],
                    ifu_form_pages=roles["ifu_form_pages"],
                    summary_pages=roles["summary_pages"],
                )
                == dx.segment(doc).page_kinds
            )


def _synthetic(pages: list[str], kinds: list[str]) -> iu.IntendedUse:
    doc = dx.Document.from_raw("K999999", pages)
    return iu.extract(doc, dx.sections_for(doc, kinds))


FILLER = "The device records data and displays it to the clinician for later review. " * 3


class TestSyntheticEdges:
    """Synthetic: shapes the 21 real documents do not hold."""

    def test_readable_pages_without_a_statement_are_not_found(self):
        found = _synthetic(["Device Description " + FILLER], ["summary"])
        assert (found.text, found.basis) == (None, "not_found")

    def test_a_cross_reference_is_not_a_statement(self):
        found = _synthetic(
            ["Intended Use: See Indications for Use below. Device Description " + FILLER],
            ["summary"],
        )
        assert found.basis == "not_found"

    def test_a_form_with_no_statement_falls_through(self):
        form = "FORM FDA 3881 Indications for Use (Describe) Type of Use Prescription Use " + FILLER
        summary = "Indications for Use: The software estimates the risk of death. " + FILLER
        found = _synthetic([form, summary], ["ifu_form", "summary"])
        assert found.basis == "summary_section"
        assert found.text.startswith("The software estimates the risk of death.")

    def test_an_unbounded_section_is_capped(self):
        found = _synthetic(["Indications for Use: " + FILLER * 20], ["summary"])
        assert len(found.text) <= iu._SECTION_WINDOW


class TestRecord:
    def _ok(self, **kw):
        base = {
            "submission_number": "k243239",
            "extractor_version": iu.INTENDED_USE_VERSION,
            "extracted_at": NOW,
            "source_content_sha256": "ab" * 32,
            "segmentation_version": dx.EXTRACTOR_VERSION,
            "intended_use_text": "Detects things.",
            "intended_use_basis": "form_3881",
            "intended_use_pages": [4],
        }
        return IntendedUseRecord(**{**base, **kw})

    def test_a_valid_record(self):
        assert self._ok().submission_number == "K243239"

    @pytest.mark.parametrize("basis", ["not_found", "no_text"])
    def test_text_is_none_exactly_when_unread(self, basis):
        with pytest.raises(ValidationError, match="exactly when"):
            self._ok(intended_use_basis=basis)
        assert self._ok(intended_use_basis=basis, intended_use_text=None, intended_use_pages=[])

    def test_text_needs_its_pages(self):
        with pytest.raises(ValidationError, match="page"):
            self._ok(intended_use_pages=[])

    def test_an_unknown_basis_is_rejected(self):
        with pytest.raises(ValidationError):
            self._ok(intended_use_basis="llm")

    def test_the_record_never_carries_a_mortality_judgement(self):
        assert not any("mortality" in f for f in IntendedUseRecord.model_fields)

    def test_the_spark_mirror_has_parity(self):
        names = [f.name for f in spark_schema_for(IntendedUseRecord).fields]
        assert names == list(IntendedUseRecord.model_fields)


class TestYields:
    def test_the_lines_name_finding_0019s_ceiling(self):
        lines = iu.summarise([_extract("K243239"), _extract("K003301")]).lines()
        assert any("1 / 2 (50.0%)" in line and "1,537 / 1,541" in line for line in lines)


# ---------------------------------------------------------------------------
# Spark: bronze + silver_document_extraction -> silver_intended_use
# ---------------------------------------------------------------------------


def _bronze(spark, settings, rows: list[dict]) -> None:
    from registry import tables
    from registry.schemas import BronzeSummaryDocumentRecord

    records = [
        BronzeSummaryDocumentRecord(
            **{k: v for k, v in r.items() if k in BronzeSummaryDocumentRecord.model_fields}
        )
        for r in rows
    ]
    df = spark.createDataFrame(
        [r.model_dump() for r in records], schema=spark_schema_for(BronzeSummaryDocumentRecord)
    )
    tables.write_table(df, settings, dx.BRONZE_TABLE, mode="append")


@pytest.mark.spark
class TestSparkRun:
    def test_reads_the_fetch_its_page_roles_describe(self, spark, lakehouse):
        from registry import tables

        _bronze(spark, lakehouse, [_row("K243239"), _row("K203469"), _row("K003301")])
        dx.run(spark, lakehouse)
        # A newer fetch lands after extraction: its sha has no page roles yet, so
        # the pass keeps reading the fetch 4B segmented.
        newer = {
            **_row("K243239"),
            "fetched_at": "2026-12-01T00:00:00",
            "content_sha256": "11" * 32,
        }
        _bronze(spark, lakehouse, [newer])

        result = iu.run(spark, lakehouse)

        assert result.written == 3
        assert result.yields.found == 2
        assert result.yields.unmatched == 0
        rows = {
            r["submission_number"]: r
            for r in tables.read_table(spark, lakehouse, iu.INTENDED_USE_TABLE).collect()
        }
        assert rows["K243239"]["source_content_sha256"] == _row("K243239")["content_sha256"]
        assert rows["K243239"]["source_url"].endswith("/pdf24/K243239.pdf")
        assert rows["K203469"]["intended_use_basis"] == "summary_section"
        assert rows["K003301"]["intended_use_text"] is None

        texts = iu.texts_by_submission(spark, lakehouse)
        assert set(texts) == {"K243239", "K203469"}
        assert texts["K243239"].startswith("Lung AI software device")

    def test_needs_the_document_extraction_first(self, spark, lakehouse):
        _bronze(spark, lakehouse, [_row("K243239")])
        with pytest.raises(ValueError, match="extract-documents"):
            iu.run(spark, lakehouse)

    def test_texts_are_empty_before_the_pass(self, spark, lakehouse):
        assert iu.texts_by_submission(spark, lakehouse) == {}
