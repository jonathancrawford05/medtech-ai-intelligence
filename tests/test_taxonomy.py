"""Specialty taxonomy loader.

`config/specialty_taxonomy.yaml` has existed since Phase 0 with nothing reading
it. Roadmap Issue 2 requires unmapped panels be handled explicitly rather than
silently dropped, so the loader reports them.
"""

from __future__ import annotations

import csv

import pytest

from registry.config.settings import Settings
from registry.transform import taxonomy


@pytest.fixture
def yaml_path(tmp_path):
    p = tmp_path / "specialty_taxonomy.yaml"
    p.write_text(
        "version: 1\n"
        "default_category: other\n"
        "panels:\n"
        "  Cardiovascular: cardiovascular\n"
        "  Radiology: radiology\n"
        "mortality_relevant_categories:\n"
        "  - cardiovascular\n"
        "  - metabolic\n"
    )
    return p


@pytest.fixture
def tax(yaml_path):
    return taxonomy.load(Settings(config_dir=yaml_path.parent))


class TestLoad:
    def test_maps_a_curated_panel(self, tax):
        assert tax.category_for("Cardiovascular") == "cardiovascular"

    def test_is_insensitive_to_case_and_surrounding_space(self, tax):
        assert tax.category_for("  cardiovascular ") == "cardiovascular"

    def test_an_unmapped_panel_falls_back_to_the_default(self, tax):
        assert tax.category_for("Dental") == "other"

    def test_a_missing_panel_falls_back_to_the_default(self, tax):
        assert tax.category_for(None) == "other"
        assert tax.category_for("") == "other"

    def test_unmapped_panels_are_reported_not_silently_dropped(self, tax):
        """Roadmap Issue 2 requires this explicitly."""
        tax.category_for("Dental")
        tax.category_for("Dental")
        tax.category_for("Cardiovascular")
        assert tax.unmapped_panels == {"Dental"}

    def test_exposes_the_mortality_relevant_categories(self, tax):
        assert tax.mortality_relevant_categories == {"cardiovascular", "metabolic"}

    def test_the_repo_config_loads_and_covers_the_report_panels(self):
        """The real file must parse and map the panels the trend report leans on."""
        real = taxonomy.load(Settings())
        assert real.category_for("Cardiovascular") == "cardiovascular"
        assert real.category_for("Radiology") == "radiology"
        assert "cardiovascular" in real.mortality_relevant_categories

    def test_the_repo_config_covers_every_panel_in_the_real_fda_export(self):
        """The bug this pins: the config said "General & Plastic Surgery" while the
        FDA export says "General and Plastic Surgery", so real rows fell to "other".

        `fda_ai_list_sample.csv` is a verbatim slice of the FDA curated list, so any
        panel spelling in it is a spelling the pipeline will actually meet.
        """
        real = taxonomy.load(Settings())
        with open("tests/fixtures/fda_ai_list_sample.csv", newline="") as fh:
            panels = {row["Panel (Lead)"].strip() for row in csv.DictReader(fh)}
        assert panels, "fixture yielded no panels -- check the column name"
        for panel in sorted(panels):
            assert real.category_for(panel) != "other", f"uncurated panel: {panel!r}"
        assert real.unmapped_panels == set()

    def test_a_missing_file_fails_loudly(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            taxonomy.load(Settings(config_dir=tmp_path / "nope"))


class TestPanelLabelNormalisation:
    """FDA panel labels are not spelled consistently across sources and years.

    The same committee appears as "General and Plastic Surgery" in the curated-list
    CSV and "General & Plastic Surgery" in older exports; separators drift between
    "/", "-" and ",". Curating one spelling per committee and normalising on lookup
    beats enumerating every variant in the YAML.
    """

    @pytest.fixture
    def tax(self, tmp_path):
        p = tmp_path / "specialty_taxonomy.yaml"
        p.write_text(
            "version: 1\n"
            "default_category: other\n"
            "panels:\n"
            "  General and Plastic Surgery: surgery\n"
            "  Gastroenterology-Urology: gastroenterology\n"
            "mortality_relevant_categories: []\n"
        )
        return taxonomy.load(Settings(config_dir=p.parent))

    @pytest.mark.parametrize(
        "spelling",
        [
            "General and Plastic Surgery",
            "General & Plastic Surgery",
            "general and plastic surgery",
            "General  and   Plastic Surgery",
            "General, Plastic Surgery",
        ],
    )
    def test_ampersand_and_separator_variants_map_to_the_same_category(self, tax, spelling):
        assert tax.category_for(spelling) == "surgery"

    @pytest.mark.parametrize(
        "spelling",
        ["Gastroenterology-Urology", "Gastroenterology/Urology", "Gastroenterology Urology"],
    )
    def test_punctuation_between_words_does_not_matter(self, tax, spelling):
        assert tax.category_for(spelling) == "gastroenterology"

    def test_normalisation_does_not_collapse_genuinely_different_panels(self, tax):
        assert tax.category_for("Neurology") == "other"

    def test_an_unmapped_panel_is_reported_with_its_original_spelling(self, tax):
        tax.category_for("  Ear Nose & Throat ")
        assert tax.unmapped_panels == {"Ear Nose & Throat"}
