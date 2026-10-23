"""Which pages the model sees (handoff §3 rules 4 and 7; ADR 0022 Decision 3).

Only the pages 4B located: the 3881 form (``ifu_form_pages``) and the Summary
(``summary_pages``), in document order. A page the garble test flags
(`summary_documents.page_text_class`) is dropped first: its text layer is a font
cipher, and the model reads figures out of it anyway (K182456). A document with
no page left is never sent.

Very long inputs keep their study-bearing pages: past ``max_chars`` (the spike's
40k), pages are taken in order of whether they carry a performance keyword, then
in document order, until the budget is spent, and sent in document order.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from registry.ingest.summary_documents import page_text_class


@dataclass(frozen=True)
class Page:
    page: int  # 1-based, in the PDF
    text: str


@dataclass(frozen=True)
class PageSelection:
    pages: list[Page]
    garbled: list[int] = field(default_factory=list)
    trimmed: list[int] = field(default_factory=list)  # dropped for length

    @property
    def page_numbers(self) -> list[int]:
        return [p.page for p in self.pages]


_STUDY_TERMS = re.compile(
    r"sensitiv|specific|auc|area under|predictive value|accura|clinical|performance|"
    r"validation|study|subjects|patients",
    re.IGNORECASE,
)


def select_pages(
    page_texts: Sequence[str],
    page_numbers: Iterable[int],
    *,
    max_chars: int,
) -> PageSelection:
    """The pages to send, from a document's page texts and 4B's located page numbers."""
    wanted = sorted({n for n in page_numbers if 1 <= n <= len(page_texts)})
    kept: list[Page] = []
    garbled: list[int] = []
    for n in wanted:
        text = page_texts[n - 1] or ""
        if page_text_class(text) == "garbled":
            garbled.append(n)
        elif text.strip():
            kept.append(Page(n, text))
    if sum(len(p.text) for p in kept) <= max_chars:
        return PageSelection(kept, garbled)
    ranked = sorted(kept, key=lambda p: (not _STUDY_TERMS.search(p.text), p.page))
    budget, chosen = max_chars, set()
    for p in ranked:
        if len(p.text) <= budget:
            chosen.add(p.page)
            budget -= len(p.text)
    return PageSelection(
        [p for p in kept if p.page in chosen],
        garbled,
        [p.page for p in kept if p.page not in chosen],
    )
