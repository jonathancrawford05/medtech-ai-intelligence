"""Deterministic Summary extraction (Issue 4, PR 4B; ADR 0019).

Three layers, like the acquisition tests:

- **Real text** -- the ten-document slice 4A recorded on the Mac
  (``tests/fixtures/summary_documents/``). Predicates, segmentation, cybersecurity
  and the normalisation trap are asserted against it.
- **Synthetic** -- short strings for the PCCP rule and the negation edge cases.
  None of the 4A slice carries a PCCP, so until the recorded PCCP cases below exist
  these are built from finding 0019's verbatim wording, and say so.
- **Recorded PCCP cases** -- the documents finding 0019 names (corrections, the
  negation, the inherited plan, the Summary positives) plus letter-sentence
  positives picked from the maintainer's full bronze. Recorded by the
  ``live_network`` tests at the bottom on a host that can reach
  ``accessdata.fda.gov`` (``uv run pytest -m live_network -k pccp``); the offline
  assertions over them skip, naming the missing file, until they are committed.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from registry.config.settings import Settings
from registry.schemas import DocumentExtractionRecord, PredicateDateRecord, spark_schema_for
from registry.transform import document_extraction as de
from registry.transform import predicate_dates

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "summary_documents"
PCCP_DIR = FIXTURE_DIR / "pccp"
NOW = dt.datetime(2026, 10, 11, 12, 0, 0)


def _row(number: str, directory: Path = FIXTURE_DIR) -> dict:
    path = directory / f"{number}.json"
    if not path.exists():
        pytest.skip(
            f"{path.relative_to(FIXTURE_DIR.parent)} not recorded yet: run "
            "`uv run pytest -m live_network -k pccp` on a host that can reach "
            "accessdata.fda.gov (and holds the full bronze), then commit tests/fixtures/"
        )
    return json.loads(path.read_text())


def _extract(number: str, directory: Path = FIXTURE_DIR) -> DocumentExtractionRecord:
    return de.extract_document(_row(number, directory), now=NOW)


def _doc(number: str) -> tuple[de.Document, de.Segmentation]:
    doc = de.Document.from_raw(number, _row(number)["page_texts"])
    return doc, de.segment(doc)


# ---------------------------------------------------------------------------
# Normalisation (finding 0011's glyph-splitting trap)
# ---------------------------------------------------------------------------


class TestNormalise:
    def test_ligatures_are_expanded(self):
        assert de.normalise("qualiﬁed Notiﬁcation") == "qualified Notification"

    def test_whitespace_collapses(self):
        assert de.normalise("  510(k)\n \nSummary\t- Lung ") == "510(k) Summary - Lung"

    def test_dashes_and_quotes_are_folded(self):
        en_dash, right_quote = chr(0x2013), chr(0x2019)
        raw = f"K203469 {en_dash} Mr. Coronado{right_quote}s"
        assert de.normalise(raw) == "K203469 - Mr. Coronado's"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Submission K0921 16 page", "Submission K092116 page"),  # K092116 p3, real
            ("Submission K092 116 Characteristic", "Submission K092116 Characteristic"),
            ("version v2.2 (Kl41248)", "version v2.2 (K141248)"),  # K203469, real
            ("Planning v1.0 (KI92377) Note", "Planning v1.0 (K192377) Note"),  # K203469
            ("Re: k092116 Trade", "Re: K092116 Trade"),  # K092116's FDA letter
            ("Re: K 2 5 3 6 2 8", "Re: K253628"),  # finding 0011's pdf.js example
            ("DEN140038 classified", "DEN140038 classified"),
        ],
    )
    def test_split_and_misread_submission_numbers_are_rebuilt(self, raw, expected):
        assert de.normalise(raw) == expected

    def test_a_seven_digit_run_is_not_truncated_into_a_number(self):
        assert de.SUBMISSION_NUMBER.findall(de.normalise("ref K1234567 here")) == []

    def test_words_are_not_mistaken_for_numbers(self):
        assert de.normalise("KIOSK IOIOIO") == "KIOSK IOIOIO"

    def test_a_number_followed_by_a_spaced_digit_keeps_its_six(self):
        assert de.normalise("K151212 2 tools") == "K151212 2 tools"


class TestRawLayerTrap:
    """CLAUDE.md / handoff §5: matching runs on normalised text, and a test must
    fail on the raw layer. Both cases are real recorded text."""

    def test_k203469s_misread_numbers_are_invisible_to_the_raw_layer(self):
        """ "(Kl41248)" and "(KI92377)": OCR-style l/I for 1 in the text layer."""
        raw = "".join(_row("K203469")["page_texts"])
        assert "K192377" not in de.SUBMISSION_NUMBER.findall(raw)
        assert "K192377" in de.SUBMISSION_NUMBER.findall(de.normalise(raw))

    def test_k092116s_own_number_is_split_in_the_raw_layer(self):
        raw = _row("K092116")["page_texts"][2]
        assert "K092116" not in de.SUBMISSION_NUMBER.findall(raw)
        assert "K092116" in de.SUBMISSION_NUMBER.findall(de.normalise(raw))

    def test_extracting_from_unnormalised_pages_gets_k203469_wrong(self):
        """The extractor itself, fed the raw layer page-for-page: the reference
        device vanishes and the predicate is found only by the closing sentence on
        page 6, not under its page-5 label."""
        doc = de.Document("K203469", _row("K203469")["page_texts"])  # NOT from_raw
        raw = de.extract_predicates(doc, de.segment(doc))
        assert raw.reference_devices == []
        assert (raw.primary.page, raw.primary.method) == (6, "sentence")

        rec = _extract("K203469")
        assert rec.reference_device_numbers == ["K192377"]
        assert (rec.primary_predicate_page, rec.primary_predicate_method) == (5, "section")


class TestPhrase:
    @pytest.mark.parametrize(
        "text",
        [
            "Predetermined Change Control Plan",
            "Predeter mined Change Con trol Plan",
            "predetermined change-control plan",
            "Predeter- mined Change Control Plan",
        ],
    )
    def test_survives_glyph_splitting(self, text):
        assert re.search(de.phrase("predetermined change control plan"), text, re.IGNORECASE)

    def test_does_not_match_a_different_phrase(self):
        assert not re.search(de.phrase("change control plan"), "change of control", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Segmentation (real slice)
# ---------------------------------------------------------------------------

# submission -> (letter pages, 3881 form pages, summary pages), read by eye from
# the recorded text.
SEGMENTS = {
    "K092116": ([5, 6], [7], [1, 2, 3, 4]),  # old layout: Summary first, letter after
    "K141922": ([1, 2], [3], [4, 5, 6, 7]),
    "K181892": ([1, 2], [3], [4, 5, 6]),
    "K190013": ([1, 2], [3, 4], list(range(5, 13))),  # letterhead is an image
    "K203469": ([1, 2], [], [4, 5, 6]),  # page 3 (the form) is scanned
    "K243239": ([1, 2, 3], [4], list(range(5, 12))),
    "K250177": ([1, 2, 3], [4], [5, 6, 7, 8, 9, 10]),
}


class TestSegmentation:
    @pytest.mark.parametrize("number", sorted(SEGMENTS))
    def test_letter_form_and_summary_pages(self, number):
        rec = _extract(number)
        assert (rec.letter_pages, rec.ifu_form_pages, rec.summary_pages) == SEGMENTS[number]

    def test_a_scanned_document_has_no_readable_page(self):
        _, seg = _doc("K003301")
        assert set(seg.page_kinds) == {"unreadable"}

    def test_a_font_encoding_failure_is_not_text(self):
        """K141480's Summary pages read as `!"!  "#""` -- characters, not words."""
        _, seg = _doc("K141480")
        assert seg.page_kinds[3] == "unreadable"

    def test_k241847s_summary_is_scanned_but_its_letter_is_not(self):
        rec = _extract("K241847")
        assert rec.letter_pages == [1, 2]
        assert rec.summary_pages == []

    def test_summary_headings_are_found_and_prose_is_not(self):
        _, seg = _doc("K141922")
        names = [s.name for s in seg.sections]
        assert names[:3] == ["predicate_device", "indications_for_use", "substantial_equivalence"]
        # "No performance data was required" is prose, not a PERFORMANCE DATA heading.
        assert names.count("performance") == 1

    def test_a_lowercase_or_determiner_led_phrase_is_prose(self):
        text = "to the predicate device and similar intended use"
        assert not de._is_label(text, text.index("predicate"))
        assert not de._is_label(text, text.index("intended"))

    def test_a_label_after_a_field_value_is_a_heading(self):
        text = "Regulatory Class II Predicate Device Proprietary Name"
        assert de._is_label(text, text.index("Predicate"))

    def test_an_all_caps_heading_keeps_its_inner_words(self):
        text = "COMPARISON OF TECHNOLOGICAL CHARACTERISTICS WITH"
        assert de._is_label(text, text.index("TECHNOLOGICAL"))

    def test_each_page_is_addressable(self):
        doc, _ = _doc("K243239")
        offset = doc.text.index("Lung-CAD")
        assert doc.page_of(offset) == 5
        assert "Lung-CAD" in doc.quote(offset, offset + 8)


# ---------------------------------------------------------------------------
# Predicates (real slice; the spike recovered predicates in 57/60)
# ---------------------------------------------------------------------------

# submission -> (predicates in document order, primary, basis). The lists for
# K181892, K141922, K190013 and K250177 are the ones 4A's slice recorded from the
# spike; the rest were read by eye.
PREDICATES = {
    "K092116": (["K033840"], "K033840", "first_in_section"),
    "K141922": (["K113696", "K122938"], "K113696", "first_in_section"),
    "K181892": (
        ["K151212", "K130552", "K130542", "K112051", "K141745", "K082427", "K140587", "K063559"],
        "K151212",
        "stated_primary",  # "The already-cleared Ziostation2 (K151212) is the primary predicate"
    ),
    "K190013": (["K162532"], "K162532", "first_in_section"),
    "K203469": (["K141248"], "K141248", "first_in_section"),
    "K243239": (["K230085"], "K230085", "first_in_section"),
    "K250177": (["K242020"], "K242020", "first_in_section"),
}


class TestPredicates:
    @pytest.mark.parametrize("number", sorted(PREDICATES))
    def test_predicates_and_primary(self, number):
        rec = _extract(number)
        numbers, primary, basis = PREDICATES[number]
        assert rec.predicate_numbers == numbers
        assert (rec.primary_predicate, rec.primary_predicate_basis) == (primary, basis)

    def test_the_eight_predicate_table_is_a_table(self):
        rec = _extract("K181892")
        assert set(rec.predicate_methods) == {"table"}
        assert set(rec.predicate_pages) == {5}  # the table sits a page after its label

    @pytest.mark.parametrize("number", sorted(PREDICATES))
    def test_the_subject_is_never_its_own_predicate(self, number):
        assert number not in _extract(number).predicate_numbers

    def test_a_reference_device_is_not_a_predicate(self):
        """K190013: "Predicate Device: K162532 ... Reference Device: K150910"."""
        rec = _extract("K190013")
        assert "K150910" not in rec.predicate_numbers
        assert rec.reference_device_numbers == ["K150910"]

    def test_numbers_cited_for_other_reasons_are_not_predicates(self):
        """K190013 cites K141273, K162225, K150214 and DEN140038 for history."""
        rec = _extract("K190013")
        assert not {"K141273", "K162225", "K150214", "DEN140038"} & set(rec.predicate_numbers)

    def test_a_column_shared_label_resolves_to_the_predicate(self):
        """K203469: "Predicate Device Name: Reference Device: ... (Kl41248) ... (KI92377)
        Note: Referenced for ..." -- the prose names K141248 as the predicate."""
        rec = _extract("K203469")
        assert rec.reference_device_numbers == ["K192377"]

    @pytest.mark.parametrize("number", ["K003301", "K141480", "K241847"])
    def test_no_readable_predicate_means_an_empty_list(self, number):
        """Scanned (K003301, K241847's Summary) or font-garbled (K141480)."""
        rec = _extract(number)
        assert rec.predicate_numbers == [] and rec.primary_predicate is None

    def test_every_predicate_carries_page_method_section_and_quote(self):
        rec = _extract("K243239")
        assert rec.predicate_pages == [5]
        assert rec.predicate_methods == ["section"]
        assert rec.predicate_sections == ["predicate_device"]
        assert "K230085" in rec.primary_predicate_quote
        assert rec.primary_predicate_page == 5

    def test_slice_yield_matches_the_spike_shape(self):
        """7 of the 7 text-layer Summaries with a printed predicate number yield it."""
        found = [n for n in PREDICATES if _extract(n).predicate_numbers]
        assert len(found) == len(PREDICATES)


# ---------------------------------------------------------------------------
# Cybersecurity presence
# ---------------------------------------------------------------------------


def _synthetic(*summary: str, letter: str = "") -> tuple[de.Document, de.Segmentation]:
    """A letter page plus Summary pages. SYNTHETIC -- built for edge cases only."""
    head = (
        "U.S. Food & Drug Administration 10903 New Hampshire Avenue Re: K240001 "
        "Trade/Device Name: Example Dear Ms. Smith: We have reviewed your section 510(k) "
        "premarket notification and have determined the device is substantially "
        "equivalent to legally marketed predicate devices. "
    )
    # Long enough to clear the 100-character readable floor on its own.
    body = "510(k) Summary Predicate Device: K230085 Device Description " + (
        "The device analyses images. " * 4
    )
    doc = de.Document.from_raw("K240001", [head + letter, *(body + s for s in summary)])
    return doc, de.segment(doc)


class TestCybersecurity:
    @pytest.mark.parametrize(("number", "page"), [("K190013", 11), ("K243239", 8)])
    def test_the_recorded_clauses_are_present(self, number, page):
        rec = _extract(number)
        assert rec.cybersecurity_statement_present is True
        assert rec.cybersecurity_basis == "mention"
        assert rec.cybersecurity_page == page
        assert "ybersecurity" in rec.cybersecurity_quote

    def test_no_mention_in_readable_summary_text_is_false(self):
        rec = _extract("K181892")
        assert rec.cybersecurity_statement_present is False
        assert rec.cybersecurity_basis == "no_mention"

    @pytest.mark.parametrize("number", ["K003301", "K241847"])
    def test_no_readable_summary_is_unknown_not_false(self, number):
        """ADR 0012: None is "could not read", never "no". K241847's letter is text
        but its Summary pages are scanned."""
        rec = _extract(number)
        assert rec.cybersecurity_statement_present is None
        assert rec.cybersecurity_basis == "no_text"

    @pytest.mark.parametrize(
        "clause",
        ["Cybersecurity: N/A.", "Cybersecurity is not applicable.", "No cybersecurity controls."],
    )
    def test_a_negated_mention_is_false(self, clause):
        result = de.extract_cybersecurity(*_synthetic(clause))
        assert (result.present, result.basis) == (False, "negated")
        assert result.page == 2

    def test_a_negation_does_not_hide_a_real_statement(self):
        result = de.extract_cybersecurity(
            *_synthetic(
                "Cybersecurity: N/A for the viewer.", "Cybersecurity testing was performed."
            )
        )
        assert result.present is True

    def test_a_mention_further_away_from_no_is_still_present(self):
        """K190013's "There are no new questions with regards to software or cybersecurity"."""
        result = de.extract_cybersecurity(
            *_synthetic("There are no new questions with regards to software or cybersecurity.")
        )
        assert result.present is True

    def test_a_letter_mention_does_not_count(self):
        result = de.extract_cybersecurity(*_synthetic("Plain text.", letter="cybersecurity"))
        assert result.basis == "no_mention"

    def test_a_split_word_still_counts(self):
        result = de.extract_cybersecurity(*_synthetic("Cyber- security testing was performed."))
        assert result.present is True


# ---------------------------------------------------------------------------
# PCCP (finding 0019) -- synthetic until the recorded cases exist
# ---------------------------------------------------------------------------

# Finding 0019's verbatim letter sentence.
LETTER_SENTENCE = (
    "FDA's substantial equivalence determination also included the review and clearance "
    "of your Predetermined Change Control Plan (PCCP)."
)


class TestPccpSynthetic:
    """SYNTHETIC strings in finding 0019's wording; the recorded cases follow below."""

    def test_the_clearance_letter_sentence_is_true(self):
        result = de.extract_pccp(*_synthetic("Plain.", letter=LETTER_SENTENCE))
        assert (result.has_pccp, result.method, result.page) == (True, "clearance_letter", 1)
        assert "review and clearance" in result.quote

    def test_the_letter_sentence_survives_glyph_splitting(self):
        split = LETTER_SENTENCE.replace("Predetermined", "Predeter mined").replace(
            "Control", "Con trol"
        )
        assert de.extract_pccp(*_synthetic("Plain.", letter=split)).method == "clearance_letter"

    def test_a_correction_letter_is_false_even_though_it_quotes_the_sentence(self):
        correction = (
            "This letter corrects our substantially equivalent letter of June 1, 2023. That "
            "letter inadvertently indicated that " + LETTER_SENTENCE + " Your 510(k) did not "
            "include a PCCP."
        )
        result = de.extract_pccp(*_synthetic("Plain.", letter=correction))
        assert (result.has_pccp, result.method) == (False, "correction_letter")

    @pytest.mark.parametrize(
        "clause",
        [
            "No Predetermined Change Control Plan is included in this submission.",  # K253984
            "PCCP: N/A.",
            "This submission does not include a PCCP.",
            "A Predetermined Change Control Plan was not included.",
        ],
    )
    def test_an_explicit_negation_is_false(self, clause):
        result = de.extract_pccp(*_synthetic(clause))
        assert (result.has_pccp, result.method, result.page) == (False, "summary_negation", 2)

    def test_an_inherited_plan_is_false_and_names_its_clearance(self):
        """K252103: "PCCP (cleared as part of K234009)"."""
        result = de.extract_pccp(
            *_synthetic("The model updates under its PCCP (cleared as part of K234009).")
        )
        assert (result.has_pccp, result.method) == (False, "summary_inherited")
        assert result.reference_submission == "K234009"

    @pytest.mark.parametrize(
        "clause",
        [
            "This submission includes a Predetermined Change Control Plan (PCCP).",
            "The purpose of this submission is to modify the PCCP.",  # K201992's shape
        ],
    )
    def test_a_summary_statement_is_true(self, clause):
        result = de.extract_pccp(*_synthetic(clause))
        assert (result.has_pccp, result.method) == (True, "summary_statement")

    def test_a_positive_outranks_a_negation_elsewhere(self):
        result = de.extract_pccp(
            *_synthetic(
                "PCCP: N/A for the viewer.", "The algorithm includes a PCCP for retraining."
            )
        )
        assert result.has_pccp is True

    def test_the_letter_sentence_outranks_summary_mentions(self):
        result = de.extract_pccp(
            *_synthetic("No PCCP changes to labelling.", letter=LETTER_SENTENCE)
        )
        assert result.method == "clearance_letter"

    def test_pccp_inside_another_word_is_not_a_mention(self):
        result = de.extract_pccp(*_synthetic("Code PCCPX1 and xPCCP are part numbers."))
        assert result.method == "no_mention"


class TestPccpOnTheSlice:
    @pytest.mark.parametrize(
        "number",
        [
            "K092116",
            "K141480",
            "K141922",
            "K181892",
            "K190013",
            "K203469",
            "K241847",
            "K243239",
            "K250177",
        ],
    )
    def test_readable_documents_without_a_pccp_are_false(self, number):
        rec = _extract(number)
        assert (rec.has_pccp, rec.pccp_method) == (False, "no_mention")

    def test_a_wholly_scanned_document_is_unknown(self):
        rec = _extract("K003301")
        assert (rec.has_pccp, rec.pccp_method) == (None, "no_text")


# ---------------------------------------------------------------------------
# Recorded PCCP cases (finding 0019) -- skip until recorded on the Mac
# ---------------------------------------------------------------------------

# Finding 0019's named Summary-body cases: submission -> (has_pccp, method, why).
PCCP_CASES: dict[str, tuple[bool, str, str]] = {
    "K231837": (
        False,
        "correction_letter",
        "FDA correction: SE letter 'inadvertently indicated' a PCCP",
    ),
    "K233692": (
        False,
        "correction_letter",
        "FDA correction: SE letter 'inadvertently indicated' a PCCP",
    ),
    "K253984": (False, "summary_negation", "'No Predetermined Change Control Plan is included'"),
    "K252103": (False, "summary_inherited", "'PCCP (cleared as part of K234009)'"),
    "K210484": (True, "summary_statement", "positive in the Summary, no letter sentence"),
    "K232699": (True, "summary_statement", "positive in the Summary, no letter sentence"),
    "K243449": (True, "summary_statement", "positive in the Summary, no letter sentence"),
    "K201992": (True, "summary_statement", "a submission to modify a PCCP"),
}
# Letter-sentence positives are not named in finding 0019; the recorder picks them
# from the maintainer's full bronze and lists them here.
PCCP_MANIFEST = PCCP_DIR / "manifest.json"


def _letter_positives() -> list[str]:
    if not PCCP_MANIFEST.exists():
        pytest.skip(
            "tests/fixtures/summary_documents/pccp/manifest.json not recorded yet: run "
            "`uv run pytest -m live_network -k pccp` on the Mac, then commit tests/fixtures/"
        )
    return json.loads(PCCP_MANIFEST.read_text())["letter_positives"]


class TestRecordedPccpCases:
    @pytest.mark.parametrize("number", sorted(PCCP_CASES))
    def test_named_case(self, number):
        has_pccp, method, _why = PCCP_CASES[number]
        rec = _extract(number, PCCP_DIR)
        assert (rec.has_pccp, rec.pccp_method) == (has_pccp, method), rec.pccp_quote
        assert rec.pccp_page is not None and rec.pccp_quote

    def test_the_inherited_plan_names_k234009(self):
        assert _extract("K252103", PCCP_DIR).pccp_reference_submission == "K234009"

    def test_letter_positives_read_true_from_the_letter(self):
        positives = _letter_positives()
        assert positives, "the recorder found no letter-sentence positive in bronze"
        for number in positives:
            rec = _extract(number, PCCP_DIR)
            assert (rec.has_pccp, rec.pccp_method) == (True, "clearance_letter"), number
            assert rec.pccp_page in rec.letter_pages, (number, rec.pccp_page, rec.letter_pages)

    def test_corrections_never_carry_the_positive(self):
        """Finding 0019: the letter sentence never co-occurs with a correction letter --
        but a correction quotes the sentence, so precedence must read it False."""
        for number in ("K231837", "K233692"):
            assert _extract(number, PCCP_DIR).has_pccp is False


# ---------------------------------------------------------------------------
# The record: invariants the schema enforces
# ---------------------------------------------------------------------------


def _record(**overrides) -> dict:
    base = {
        "submission_number": "k240001",
        "extractor_version": de.EXTRACTOR_VERSION,
        "extracted_at": NOW,
        "source_content_sha256": "ab" * 32,
        "source_fetched_at": NOW,
        "page_count": 3,
        "predicate_numbers": ["K230085"],
        "predicate_pages": [2],
        "predicate_methods": ["section"],
        "predicate_sections": ["predicate_device"],
        "primary_predicate": "K230085",
        "cybersecurity_statement_present": False,
        "cybersecurity_basis": "no_mention",
        "has_pccp": True,
        "pccp_method": "clearance_letter",
        "pccp_page": 1,
    }
    base.update(overrides)
    return base


class TestRecord:
    def test_a_valid_record(self):
        rec = DocumentExtractionRecord(**_record())
        assert rec.submission_number == "K240001"

    def test_predicate_arrays_must_be_parallel(self):
        with pytest.raises(ValidationError, match="parallel"):
            DocumentExtractionRecord(**_record(predicate_pages=[]))

    def test_an_unknown_predicate_method_is_rejected(self):
        with pytest.raises(ValidationError, match="unknown predicate methods"):
            DocumentExtractionRecord(**_record(predicate_methods=["guess"]))

    def test_the_primary_must_be_one_of_the_predicates(self):
        with pytest.raises(ValidationError, match="primary_predicate"):
            DocumentExtractionRecord(**_record(primary_predicate="K999999"))

    @pytest.mark.parametrize(
        ("has", "method"), [(None, "no_mention"), (False, "no_text"), (None, "clearance_letter")]
    )
    def test_has_pccp_is_none_exactly_when_there_is_no_text(self, has, method):
        with pytest.raises(ValidationError, match="no_text"):
            DocumentExtractionRecord(**_record(has_pccp=has, pccp_method=method))

    def test_a_pccp_read_from_text_needs_its_page(self):
        """Handoff §5: has_pccp only with page + method."""
        with pytest.raises(ValidationError, match="page"):
            DocumentExtractionRecord(**_record(pccp_page=None))

    def test_an_unknown_pccp_method_is_rejected(self):
        with pytest.raises(ValidationError):
            DocumentExtractionRecord(**_record(pccp_method="heading"))

    def test_cybersecurity_none_needs_no_text(self):
        with pytest.raises(ValidationError, match="no_text"):
            DocumentExtractionRecord(**_record(cybersecurity_statement_present=None))

    def test_the_record_never_carries_a_mortality_judgement(self):
        """ADR 0014: extraction is evidence, never the confirmed flag."""
        assert not [f for f in DocumentExtractionRecord.model_fields if "mortality" in f]

    def test_the_spark_mirror_has_parity(self):
        names = [f.name for f in spark_schema_for(DocumentExtractionRecord).fields]
        assert names == list(DocumentExtractionRecord.model_fields)


# ---------------------------------------------------------------------------
# Yields
# ---------------------------------------------------------------------------


class TestYields:
    def test_summarise_counts_the_slice(self):
        records = [_extract(p.stem) for p in sorted(FIXTURE_DIR.glob("K*.json"))]
        y = de.summarise(records)
        assert y.documents == 10
        assert y.with_predicate == 7
        assert y.primary_basis == {"first_in_section": 6, "stated_primary": 1}
        assert y.cybersecurity == {"mention": 2, "no_mention": 6, "no_text": 2}
        assert y.cybersecurity_no_mention_mixed == 1  # K203469
        assert y.pccp == {"no_mention": 9, "no_text": 1}
        assert y.has_pccp == {"False": 9, "None": 1}

    def test_the_lines_name_finding_0019s_numbers(self):
        text = "\n".join(de.Yields(documents=1, with_predicate=1).lines())
        assert "1,508 / 1,541" in text and "471 / 1,541" in text and "68 letter" in text


# ---------------------------------------------------------------------------
# Predicate decision dates (openFDA, faked)
# ---------------------------------------------------------------------------


class _FakeOpenFda:
    def __init__(self, dates: dict[str, str | None]):
        self.dates, self.calls = dates, []

    def fetch_submission(self, number):
        self.calls.append(number)
        if number not in self.dates:
            return None

        class _Device:
            endpoint = "510k"
            decision_date = self.dates[number]

        return _Device()


class TestPredicateDates:
    def test_one_call_per_distinct_number_and_misses_are_kept(self):
        client = _FakeOpenFda({"K230085": "2023-06-01"})
        records = predicate_dates.build_records(["K230085", "k230085 ", "K111111"], client, now=NOW)
        assert client.calls == ["K111111", "K230085"]
        by = {r.submission_number: r for r in records}
        assert by["K230085"].decision_date == dt.date(2023, 6, 1)
        assert by["K111111"].found is False and by["K111111"].decision_date is None

    def test_a_malformed_date_is_none(self):
        (rec,) = predicate_dates.build_records(["K1"], _FakeOpenFda({"K1": "June"}), now=NOW)
        assert rec.found is True and rec.decision_date is None

    @pytest.mark.parametrize(
        ("device", "predicate", "expected"),
        [
            (dt.date(2025, 4, 24), dt.date(2023, 6, 1), 693),
            (dt.date(2025, 4, 24), None, None),
            (None, dt.date(2023, 6, 1), None),
            (dt.date(2020, 1, 1), dt.date(2023, 6, 1), None),  # predicate after device
        ],
    )
    def test_age(self, device, predicate, expected):
        assert predicate_dates.predicate_age_days(device, predicate) == expected

    def test_the_spark_mirror_has_parity(self):
        names = [f.name for f in spark_schema_for(PredicateDateRecord).fields]
        assert names == list(PredicateDateRecord.model_fields)


# ---------------------------------------------------------------------------
# Spark: bronze -> silver_document_extraction -> silver_devices
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
    tables.write_table(df, settings, de.BRONZE_TABLE, mode="append")


@pytest.mark.spark
class TestSparkRun:
    def test_extracts_the_newest_found_fetch_per_document(self, spark, lakehouse):
        from registry import tables

        newer = _row("K243239")
        older = {**newer, "fetched_at": "2026-01-01T00:00:00", "content_sha256": "00" * 32}
        miss = {
            **_row("K181892"),
            "fetched_at": "2026-12-01T00:00:00",
            "content_sha256": None,
            "content_type": None,
            "byte_count": None,
            "page_count": 0,
            "page_texts": [],
            "page_char_counts": [],
            "text_class": None,
            "http_status": 404,
        }
        _bronze(spark, lakehouse, [older, newer, _row("K181892"), miss])

        result = de.run(spark, lakehouse)
        assert result.written == 2
        rows = {
            r["submission_number"]: r
            for r in tables.read_table(spark, lakehouse, de.EXTRACTION_TABLE).collect()
        }
        assert rows["K243239"]["source_content_sha256"] == newer["content_sha256"]
        # A later miss does not erase the document that was found.
        assert rows["K181892"]["primary_predicate"] == "K151212"
        assert result.yields.with_predicate == 2

        facts = de.facts_by_submission(spark, lakehouse)
        assert facts["K243239"] == de.DocumentFacts("K230085", False, True)

    def test_a_missing_bronze_table_is_a_clear_error(self, spark, lakehouse):
        with pytest.raises(ValueError, match="fetch-summaries"):
            de.run(spark, lakehouse)

    def test_facts_are_empty_before_extraction(self, spark, lakehouse):
        assert de.facts_by_submission(spark, lakehouse) == {}
        assert predicate_dates.dates_by_submission(spark, lakehouse) == {}


# ---------------------------------------------------------------------------
# Live: record the PCCP cases (Mac only; never CI)
# ---------------------------------------------------------------------------


def _write_fixture(doc, recorded_at: dt.datetime) -> None:
    from registry.ingest import summary_documents as sd

    record = sd.to_record(doc, ingested_at=recorded_at)
    payload = json.loads(record.model_dump_json())
    (PCCP_DIR / f"{doc.submission_number}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    )


@pytest.mark.live_network
class TestLivePccpRecording:
    """Hits accessdata.fda.gov and RECORDS finding 0019's PCCP cases as fixtures.

    On the Mac: ``uv run pytest -m live_network -k pccp``, then commit
    ``tests/fixtures/summary_documents/pccp/``. Throttled at ~1 req/s and stops on
    429/403, like the real pass.
    """

    def test_record_the_named_pccp_cases(self):
        from registry.ingest import summary_documents as sd

        PCCP_DIR.mkdir(parents=True, exist_ok=True)
        recorded_at = dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0)
        with sd.SummaryFetcher(Settings()) as fetcher:
            for number in sorted(PCCP_CASES):
                doc = fetcher.fetch(number)
                assert doc.found, f"{number}: no PDF at {doc.urls_tried} ({doc.http_status})"
                _write_fixture(doc, recorded_at)

    @pytest.mark.spark
    def test_record_letter_positives_from_the_full_bronze(self, spark):
        """Pick letter-sentence positives from the local full bronze, re-fetch them
        live, and write the manifest -- including the full-scale yields, so the
        finding's numbers are committed with the fixtures that back them."""
        from registry import tables
        from registry.ingest import summary_documents as sd

        settings = Settings()
        if not tables.table_exists(spark, settings, de.BRONZE_TABLE):
            pytest.skip(f"no {de.BRONZE_TABLE} at {settings.table_ref(de.BRONZE_TABLE)}")
        records = [de.extract_document(r) for r in de.latest_documents(spark, settings)]
        positives = sorted(
            r.submission_number for r in records if r.pccp_method == "clearance_letter"
        )
        assert positives, "no clearance-letter positive in bronze: the rule or the text changed"
        # Earliest, middle and latest number: spread across the adoption years.
        picks = sorted({positives[0], positives[len(positives) // 2], positives[-1]})

        PCCP_DIR.mkdir(parents=True, exist_ok=True)
        recorded_at = dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0)
        with sd.SummaryFetcher(settings) as fetcher:
            for number in picks:
                doc = fetcher.fetch(number)
                assert doc.found, f"{number}: no PDF at {doc.urls_tried} ({doc.http_status})"
                _write_fixture(doc, recorded_at)

        by_method: dict[str, list[str]] = {}
        for r in records:
            if r.pccp_method not in ("no_mention", "no_text"):
                by_method.setdefault(r.pccp_method, []).append(r.submission_number)
        PCCP_MANIFEST.write_text(
            json.dumps(
                {
                    "recorded_at": recorded_at.isoformat(),
                    "extractor_version": de.EXTRACTOR_VERSION,
                    "letter_positives": picks,
                    "named_cases": {k: v[2] for k, v in sorted(PCCP_CASES.items())},
                    "full_scale_yields": de.summarise(records).lines(),
                    "full_scale_pccp_by_method": {
                        k: sorted(v) for k, v in sorted(by_method.items())
                    },
                },
                indent=2,
            )
            + "\n"
        )
