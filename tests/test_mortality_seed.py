"""Load the hand-curated mortality seed into EvidenceRecords (ADR 0007/0014).

The seed is the registry's most consequential data: it is the only field that is
a judgement rather than an FDA-published fact. So the loader's job is less about
parsing than about refusing to accept a judgement whose provenance is missing or
misstated.
"""

from __future__ import annotations

import pytest

from registry.config.settings import Settings
from registry.transform import mortality_seed


@pytest.fixture
def seed_path(fixtures_dir):
    return fixtures_dir / "mortality_seed_sample.yaml"


@pytest.fixture
def settings(seed_path):
    return Settings(config_dir=seed_path.parent, mortality_seed_filename=seed_path.name)


class TestLoad:
    def test_builds_one_evidence_record_per_reviewed_device(self, settings):
        records = mortality_seed.load(settings)
        assert {r.submission_number for r in records} == {"K243456", "K111111", "P130020/S005"}

    def test_carries_the_confirmed_flag_and_its_provenance(self, settings):
        by_id = {r.submission_number: r for r in mortality_seed.load(settings)}
        assert by_id["K243456"].mortality_confirmed_flag is True
        assert by_id["K243456"].mortality_review_method == "llm_assisted"
        assert "perivascular" in by_id["K243456"].intended_use_text

    def test_a_negative_judgement_is_recorded_not_dropped(self, settings):
        """ "Reviewed and judged not relevant" is a different, more useful claim
        than silence, and the mart must be able to tell them apart."""
        by_id = {r.submission_number: r for r in mortality_seed.load(settings)}
        assert by_id["K111111"].mortality_confirmed_flag is False

    def test_a_pma_supplement_keeps_its_whole_key(self, settings):
        by_id = {r.submission_number: r for r in mortality_seed.load(settings)}
        assert by_id["P130020/S005"].mortality_review_method == "human"

    def test_the_keyword_stage_runs_over_the_curated_text(self, settings):
        """Stage 1 is deterministic and always populated (ADR 0007). Running it
        here means a curator's judgement can be compared against it -- a
        `confirmed=True` with no keyword hit is the interesting case to look at."""
        by_id = {r.submission_number: r for r in mortality_seed.load(settings)}
        assert by_id["K243456"].mortality_keyword_flag is True
        assert by_id["K111111"].mortality_keyword_flag is False

    def test_a_missing_seed_file_is_empty_not_an_error(self, tmp_path):
        """The mart must build before anyone has curated anything."""
        assert mortality_seed.load(Settings(config_dir=tmp_path)) == []


class TestRefusesUnprovenancedJudgements:
    """ADR 0007 rule 1: a confirmation with no provenance is invalid data, not a
    warning. These are the cases that would quietly corrupt the registry's only
    judgement column, so the loader raises rather than skips."""

    def _write(self, tmp_path, entry: str):
        p = tmp_path / "mortality_seed.yaml"
        p.write_text("version: 1\nreviewed:\n" + entry)
        return Settings(config_dir=tmp_path)

    def test_a_confirmation_without_a_review_method_is_rejected(self, tmp_path):
        settings = self._write(
            tmp_path,
            "  - submission_number: K1\n"
            "    mortality_confirmed: true\n"
            "    intended_use_text: Predicts cardiac mortality.\n"
            "    intended_use_source: https://example.test/K1\n",
        )
        with pytest.raises(ValueError, match="review_method"):
            mortality_seed.load(settings)

    def test_an_unknown_review_method_is_rejected(self, tmp_path):
        settings = self._write(
            tmp_path,
            "  - submission_number: K1\n"
            "    mortality_confirmed: true\n"
            "    review_method: vibes\n"
            "    intended_use_text: Predicts cardiac mortality.\n"
            "    intended_use_source: https://example.test/K1\n",
        )
        with pytest.raises(ValueError, match="review_method"):
            mortality_seed.load(settings)

    def test_a_judgement_without_its_evidence_is_rejected(self, tmp_path):
        """`intended_use_text` is the evidence the judgement was made from. A
        confirmation with no text behind it cannot be audited by anyone."""
        settings = self._write(
            tmp_path,
            "  - submission_number: K1\n"
            "    mortality_confirmed: true\n"
            "    review_method: human\n"
            "    intended_use_source: https://example.test/K1\n",
        )
        with pytest.raises(ValueError, match="intended_use_text"):
            mortality_seed.load(settings)

    def test_a_judgement_without_a_checkable_source_is_rejected(self, tmp_path):
        settings = self._write(
            tmp_path,
            "  - submission_number: K1\n"
            "    mortality_confirmed: true\n"
            "    review_method: human\n"
            "    intended_use_text: Predicts cardiac mortality.\n",
        )
        with pytest.raises(ValueError, match="intended_use_source"):
            mortality_seed.load(settings)

    def test_a_duplicate_submission_is_rejected(self, tmp_path):
        """Two judgements for one device means one of them silently loses."""
        entry = (
            "  - submission_number: K1\n"
            "    mortality_confirmed: true\n"
            "    review_method: human\n"
            "    intended_use_text: Predicts cardiac mortality.\n"
            "    intended_use_source: https://example.test/K1\n"
        )
        with pytest.raises(ValueError, match="K1"):
            mortality_seed.load(self._write(tmp_path, entry + entry))


class TestCommittedSeed:
    """The *real* committed seed (config/mortality_seed.yaml), not the fixture.

    A malformed hand-edit to the curated seed otherwise surfaces only at
    ``build-mart`` time; this pins it in the fast (non-spark) suite. The counts are
    a canary -- update them deliberately when the curation itself changes.
    """

    def test_the_committed_seed_loads_and_every_judgement_is_auditable(self):
        # Settings() with its default config_dir resolves to the repo's config/.
        records = mortality_seed.load(Settings())

        # It loads at all: the loader raises on malformed data (ADR 0014 Decision 5).
        assert len(records) == 133
        assert sum(1 for r in records if r.mortality_confirmed_flag is True) == 11

        # ADR 0007: every judgement carries auditable provenance.
        for r in records:
            assert r.mortality_review_method in {"human", "llm_assisted"}
            assert r.intended_use_text.strip()
            assert (r.intended_use_source or "").strip()

        # One judgement per device (the loader enforces this; pin it here too).
        subs = [r.submission_number for r in records]
        assert len(subs) == len(set(subs))


class TestStage1KeywordCoverage:
    """Stage 1 is a cheap, recall-oriented lead flag (ADR 0007), widened from the
    curation evidence in findings/0012 and 0014: 8 of the 11 confirmed devices used
    mortality-relevant language stage 1 did not recognise. It is still never the
    mart filter (ADR 0014) -- widening it only shrinks the keyword_disagrees gap."""

    @pytest.mark.parametrize(
        "text",
        [
            "likelihood of future hemodynamic instability",        # CLEWICU / AHI
            "likelihood of future hypotensive events",             # Acumen HPI
            "the Global Hypoperfusion Index",                      # Edwards GHI
            "identify loss of pulse events",                       # Loss of Pulse Detection
            "screen U.S. Service members for hemorrhage risk",     # APPRAISE-HRI
            "early warning for impending patient deterioration",   # eCART
            "plaque identification and characterization",          # HeartFlow
        ],
    )
    def test_flags_the_evidence_backed_phrasings(self, text):
        assert mortality_seed.keyword_flag(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "assessment of arrhythmias using ECG data",  # rhythm detection, not risk
            "an electronic stethoscope for auscultation",  # heart sounds
            "non-invasive spot measurement of pulse rate",  # vitals -- not "loss of pulse"
        ],
    )
    def test_does_not_flag_routine_diagnostic_or_monitoring_text(self, text):
        assert mortality_seed.keyword_flag(text) is False

    def test_the_original_terms_still_match(self):
        assert mortality_seed.keyword_flag("aggregate statistical mortality risk") is True
        assert mortality_seed.keyword_flag("risk of a major adverse cardiac event") is True
