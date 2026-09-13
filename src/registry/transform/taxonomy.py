"""Load the curated FDA-panel → specialty-category mapping.

`config/specialty_taxonomy.yaml` is hand-maintained: the FDA's "Panel (lead)"
names the reviewing advisory committee, not the clinical problem, so the mapping
is a curated judgement rather than a lookup we can derive.

Panels we have not curated fall back to `default_category` **and are recorded**
in `unmapped_panels`, so a new FDA panel shows up as something to curate instead
of quietly becoming "other".

Lookups are normalised (see `_normalise`) because the FDA does not spell its own
committee names consistently: the curated-list CSV says "General and Plastic
Surgery" where other exports say "General & Plastic Surgery", and the separator
in "Gastroenterology-Urology" appears as "/" elsewhere. We curate one spelling
per committee and normalise both sides rather than enumerate every variant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from registry.config.settings import Settings, get_settings

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Connector words that appear or vanish between spellings of the same committee
# ("General and Plastic Surgery" vs "General, Plastic Surgery"). Dropping them is
# safe here: no two FDA panels differ only by a connector.
_CONNECTORS = frozenset({"and", "the", "of"})


def _normalise(label: str) -> str:
    """Reduce a panel label to a spelling-insensitive lookup key."""
    tokens = _NON_ALNUM.sub(" ", label.lower()).split()
    return " ".join(t for t in tokens if t not in _CONNECTORS)


@dataclass
class SpecialtyTaxonomy:
    """A loaded taxonomy. `category_for` records misses as a side effect."""

    default_category: str
    _panels: dict[str, str]
    mortality_relevant_categories: frozenset[str]
    unmapped_panels: set[str] = field(default_factory=set)

    def category_for(self, panel: str | None) -> str:
        """Map an FDA panel label to our category, defaulting and recording misses."""
        raw = (panel or "").strip()
        key = _normalise(raw)
        if not key:
            return self.default_category
        mapped = self._panels.get(key)
        if mapped is None:
            self.unmapped_panels.add(raw)
            return self.default_category
        return mapped


def load(settings: Settings | None = None) -> SpecialtyTaxonomy:
    """Read the taxonomy from `settings.specialty_taxonomy_path`."""
    settings = settings or get_settings()
    path: Path = settings.specialty_taxonomy_path
    if not path.exists():
        raise FileNotFoundError(
            f"Specialty taxonomy not found at {path}. It is hand-maintained config; "
            "see config/specialty_taxonomy.yaml in the repo."
        )
    raw = yaml.safe_load(path.read_text()) or {}
    panels: dict[str, str] = {}
    seen: dict[str, str] = {}
    for label, category in (raw.get("panels") or {}).items():
        key = _normalise(str(label))
        if key in seen and seen[key] != str(label):
            raise ValueError(
                f"{path}: panels {seen[key]!r} and {label!r} normalise to the same "
                f"lookup key {key!r}; one of them would silently win. Keep a single "
                "spelling per committee."
            )
        seen[key] = str(label)
        panels[key] = str(category)
    return SpecialtyTaxonomy(
        default_category=str(raw.get("default_category", "other")),
        _panels=panels,
        mortality_relevant_categories=frozenset(
            str(c) for c in (raw.get("mortality_relevant_categories") or [])
        ),
    )
