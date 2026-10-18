"""The garble test and the page selection (PR 4D; ADR 0022 Decision 3).

Calibrated on every recorded page in the repo: the 4D slice and 4A's real Summary
slice (PCCP cases included). A page is garbled when it has characters but no words.
"""

from __future__ import annotations

import json
from pathlib import Path

from registry.ingest.summary_documents import page_is_garbled, page_text_class
from registry.llm_extraction.pages import select_pages

FIXTURES = Path(__file__).parent / "fixtures"


def _all_recorded_pages() -> dict[tuple[str, int], str]:
    pages: dict[tuple[str, int], str] = {}
    for path in (FIXTURES / "llm_extraction" / "documents").glob("*.json"):
        doc = json.loads(path.read_text())
        for p in doc["pages"]:
            pages[(doc["submission_number"], p["page"])] = p["text"]
    for path in (FIXTURES / "summary_documents").rglob("K*.json"):
        doc = json.loads(path.read_text())
        for i, text in enumerate(doc.get("page_texts") or [], start=1):
            pages[(doc["submission_number"], i)] = text
    return pages


class TestGarbleTest:
    def test_flags_exactly_the_cipher_pages_in_every_recorded_document(self):
        """K182456's two Summary pages (letters and symbols for letters) and K141480's
        four control-character pages (4B already reads those as unreadable)."""
        pages = _all_recorded_pages()
        assert len(pages) > 350
        garbled = sorted(k for k, text in pages.items() if page_text_class(text) == "garbled")
        assert garbled == [
            ("K141480", 4),
            ("K141480", 5),
            ("K141480", 7),
            ("K141480", 8),
            ("K182456", 7),
            ("K182456", 8),
        ]

    def test_the_k182456_cipher_has_letters_so_the_alpha_floor_misses_it(self):
        """Why 4A/4B did not catch it: over half its characters are letters."""
        doc = json.loads((FIXTURES / "llm_extraction" / "documents" / "K182456.json").read_text())
        for p in doc["pages"]:
            chars = [c for c in p["text"] if not c.isspace()]
            assert sum(c.isalpha() for c in chars) / len(chars) >= 0.5
            assert page_is_garbled(p["text"])

    def test_a_short_page_is_image_not_garbled(self):
        assert page_text_class("Page 3 of 9") == "image"

    def test_a_table_page_with_few_words_is_text(self):
        table = "Sensitivity Specificity\n" + "92.6% 98.8% 85.5% 99.6% (90.1-95.0)\n" * 20
        assert page_text_class(table) == "text"

    def test_a_letter_only_cipher_is_caught_by_the_function_word_rule(self):
        """Synthetic: a cipher onto letters alone has no symbols, but no "the"/"of" either."""
        letters = "abcdefghijklmnopqrstuvwxyz"
        shift = str.maketrans(letters, letters[7:] + letters[:7])
        real = (FIXTURES / "llm_extraction" / "documents" / "K253801.json").read_text()
        text = max((p["text"] for p in json.loads(real)["pages"]), key=len)
        assert page_text_class(text) == "text"
        assert page_text_class(text.lower().translate(shift)) == "garbled"


class TestSelectPages:
    def test_keeps_located_pages_in_document_order(self):
        texts = ["letter " * 30, "form " * 30, "summary " * 30, "more " * 30]
        sel = select_pages(texts, [4, 2, 3], max_chars=10_000)
        assert sel.page_numbers == [2, 3, 4]

    def test_ignores_pages_outside_the_document(self):
        sel = select_pages(["a page " * 20], [1, 7], max_chars=10_000)
        assert sel.page_numbers == [1]

    def test_drops_blank_pages_silently_and_garbled_pages_on_record(self):
        doc = json.loads((FIXTURES / "llm_extraction" / "documents" / "K182456.json").read_text())
        cipher = {p["page"]: p["text"] for p in doc["pages"]}
        texts = ["", "a readable summary page with the study " * 5, "", "", "", "", cipher[7]]
        sel = select_pages(texts, [1, 2, 7], max_chars=10_000)
        assert sel.page_numbers == [2]
        assert sel.garbled == [7]

    def test_a_long_input_keeps_its_study_pages(self):
        filler = "Device description and technological characteristics. " * 40
        study = "Clinical study: sensitivity was 93% in 400 patients. " * 40
        texts = [filler, study, filler, study]
        sel = select_pages(texts, [1, 2, 3, 4], max_chars=len(study) * 2 + 10)
        assert sel.page_numbers == [2, 4]
        assert sel.trimmed == [1, 3]
