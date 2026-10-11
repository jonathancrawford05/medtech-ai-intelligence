"""PCCP share by decision year in `registry inspect` (ADR 0020).

The adoption trend is **stock, not movement**: it is recomputed from silver on
every `inspect`, so it is visible without waiting for a new clearance. The table
is split by `pccp_method` so its `clearance_letter` column is finding 0019's table
(the letter sentence over the Summary documents that year) and the Summary-body
positives -- the visible edge of the letter-sentence gap -- sit beside it.

Pure: no Spark. The Spark wiring is in `tests/test_lakehouse_report.py`.
"""

from __future__ import annotations

from registry import lakehouse_report as lr

# Finding 0019, "PCCP adoption by decision year": (year, letter sentences, documents).
FINDING_0019 = [
    (2022, 0, 160),
    (2023, 1, 217),
    (2024, 17, 224),
    (2025, 31, 327),
    (2026, 19, 173),
]
FINDING_0019_CELLS = {
    2022: "0 / 160 (0.0%)",
    2023: "1 / 217 (0.5%)",
    2024: "17 / 224 (7.6%)",
    2025: "31 / 327 (9.5%)",
    2026: "19 / 173 (11.0%)",
}


def _rows(year: int, letters: int, documents: int, summary: int = 0) -> list[tuple]:
    """`documents` (year, pccp_method) pairs: the positives, then the rest."""
    rest = documents - letters - summary
    return (
        [(year, "clearance_letter")] * letters
        + [(year, "summary_statement")] * summary
        + [(year, "no_mention")] * rest
    )


def _finding_0019_shaped(summary: dict[int, int] | None = None) -> list[tuple]:
    summary = summary or {}
    return [r for y, k, n in FINDING_0019 for r in _rows(y, k, n, summary.get(y, 0))]


class TestPccpByYear:
    def test_counts_per_year_by_method(self):
        years = lr.pccp_by_year(_finding_0019_shaped({2024: 2}))
        by_year = {y.year: y for y in years}
        assert [y.year for y in years] == [2022, 2023, 2024, 2025, 2026]
        assert (by_year[2024].documents, by_year[2024].clearance_letter) == (224, 17)
        assert by_year[2024].summary_statement == 2
        assert by_year[2024].has_pccp == 19

    def test_the_clearance_letter_column_reproduces_finding_0019(self):
        """The measure the handoff names: the inspect table's letter column is
        finding 0019's year-by-year table, cell for cell."""
        years = {y.year: y for y in lr.pccp_by_year(_finding_0019_shaped({2024: 1, 2026: 1}))}
        assert {y: years[y].letter_cell for y in years} == FINDING_0019_CELLS
        assert sum(y.clearance_letter for y in years.values()) == 68

    def test_summary_positives_are_shown_separately(self):
        """A Summary-body positive is not a letter sentence: it must not move the
        letter column (which would stop it matching finding 0019)."""
        years = {y.year: y for y in lr.pccp_by_year(_finding_0019_shaped({2026: 1}))}
        assert years[2026].letter_cell == "19 / 173 (11.0%)"
        assert years[2026].summary_statement == 1
        assert years[2026].has_pccp_cell == "20 / 173 (11.6%)"

    def test_negatives_and_unread_documents_count_only_as_documents(self):
        rows = [
            (2025, "correction_letter"),
            (2025, "summary_negation"),
            (2025, "summary_inherited"),
            (2025, "no_text"),
            (2025, "clearance_letter"),
        ]
        [year] = lr.pccp_by_year(rows)
        assert (year.documents, year.clearance_letter, year.summary_statement) == (5, 1, 0)

    def test_a_device_with_no_decision_year_is_kept_last(self):
        years = lr.pccp_by_year([(None, "no_mention"), (2024, "clearance_letter")])
        assert [y.year for y in years] == [2024, None]

    def test_the_rendered_table(self):
        lines = lr.pccp_year_lines(lr.pccp_by_year(_finding_0019_shaped({2024: 1})))
        assert "clearance_letter" in lines[0] and "summary_statement" in lines[0]
        row_2024 = next(ln for ln in lines if ln.lstrip().startswith("2024"))
        assert "17 / 224 (7.6%)" in row_2024
        assert "18 / 224 (8.0%)" in row_2024  # letter + Summary, the has_pccp column
        for year, cell in FINDING_0019_CELLS.items():
            line = next(ln for ln in lines if ln.lstrip().startswith(str(year)))
            assert cell in line, line

    def test_no_documents_renders_nothing(self):
        assert lr.pccp_year_lines([]) == []
