"""Applicant → resolved parent company.

A hand-maintained lookup by design: automating M&A tracking is an explicit
non-goal. What the code owes is robust *matching* against that curation, so one
entry covers the spelling variants the FDA actually publishes.
"""

from __future__ import annotations

import pytest

from registry.config.settings import Settings
from registry.transform import company_resolution as cr


@pytest.fixture
def lookup(tmp_path):
    (tmp_path / "company_aliases.yaml").write_text(
        "version: 1\n"
        "companies:\n"
        "  - resolved_name: Aidoc\n"
        "    aliases: [Aidoc Medical, Aidoc]\n"
        "  - resolved_name: Siemens Healthineers\n"
        "    parent: Siemens AG\n"
        "    aliases: [Siemens Healthineers, Siemens Medical Solutions USA]\n"
    )
    return cr.load(Settings(config_dir=tmp_path))


class TestNormalisation:
    @pytest.mark.parametrize(
        "raw",
        [
            "Aidoc Medical Ltd",
            "Aidoc Medical, Ltd.",
            "AIDOC MEDICAL LTD",
            "  Aidoc Medical Ltd.  ",
            "Aidoc Medical Inc",
            "Aidoc Medical LLC",
        ],
    )
    def test_corporate_suffixes_and_punctuation_do_not_defeat_a_match(self, lookup, raw):
        assert lookup.resolve(raw).resolved_name == "Aidoc"

    def test_a_bare_name_still_matches(self, lookup):
        assert lookup.resolve("Aidoc").resolved_name == "Aidoc"

    @pytest.mark.parametrize("suffix", ["GmbH", "AG", "N.V.", "B.V.", "PLC", "S.A.", "Pty Ltd"])
    def test_international_suffixes_are_stripped(self, lookup, suffix):
        assert lookup.resolve(f"Siemens Healthineers {suffix}").resolved_name == (
            "Siemens Healthineers"
        )


class TestResolution:
    def test_carries_the_parent_when_curated(self, lookup):
        assert lookup.resolve("Siemens Medical Solutions USA").parent == "Siemens AG"

    def test_parent_is_none_when_not_curated(self, lookup):
        assert lookup.resolve("Aidoc Medical Ltd").parent is None

    def test_an_unknown_applicant_falls_back_to_its_own_cleaned_name(self, lookup):
        """Unknown must not mean null — the raw applicant is still the best answer."""
        result = lookup.resolve("Some Unlisted Startup, Inc.")
        assert result.resolved_name == "Some Unlisted Startup"
        assert result.matched is False

    def test_a_matched_applicant_is_flagged_as_matched(self, lookup):
        assert lookup.resolve("Aidoc Medical Ltd").matched is True

    def test_unmatched_applicants_are_recorded_for_curation(self, lookup):
        lookup.resolve("Some Unlisted Startup, Inc.")
        lookup.resolve("Another One Ltd")
        lookup.resolve("Aidoc Medical Ltd")
        assert lookup.unmatched_applicants == {"Some Unlisted Startup, Inc.", "Another One Ltd"}

    def test_a_missing_applicant_resolves_to_none(self, lookup):
        assert lookup.resolve(None).resolved_name is None
        assert lookup.resolve("   ").resolved_name is None


def test_the_repo_lookup_loads_and_resolves_report_examples():
    """The real curated file must parse and cover the trend report's named companies."""
    real = cr.load(Settings())
    assert real.resolve("Siemens Medical Solutions USA, Inc.").resolved_name == (
        "Siemens Healthineers"
    )
    assert real.resolve("Caristo Diagnostics Ltd").resolved_name == "Caristo Diagnostics"
    assert real.resolve("Coreline Soft Co., Ltd.").resolved_name == "Coreline Soft"
    assert real.resolve("Nanox AI Ltd.").resolved_name == "Nanox AI"


def test_a_missing_file_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        cr.load(Settings(config_dir=tmp_path / "nope"))
