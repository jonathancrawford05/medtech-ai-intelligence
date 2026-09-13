"""Specialty taxonomy loader.

`config/specialty_taxonomy.yaml` has existed since Phase 0 with nothing reading
it. Roadmap Issue 2 requires unmapped panels be handled explicitly rather than
silently dropped, so the loader reports them.
"""

from __future__ import annotations

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

    def test_a_missing_file_fails_loudly(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            taxonomy.load(Settings(config_dir=tmp_path / "nope"))
