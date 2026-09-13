"""Load the curated FDA-panel → specialty-category mapping.

`config/specialty_taxonomy.yaml` is hand-maintained: the FDA's "Panel (lead)"
names the reviewing advisory committee, not the clinical problem, so the mapping
is a curated judgement rather than a lookup we can derive.

Panels we have not curated fall back to `default_category` **and are recorded**
in `unmapped_panels`, so a new FDA panel shows up as something to curate instead
of quietly becoming "other".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from registry.config.settings import Settings, get_settings


@dataclass
class SpecialtyTaxonomy:
    """A loaded taxonomy. `category_for` records misses as a side effect."""

    default_category: str
    _panels: dict[str, str]
    mortality_relevant_categories: frozenset[str]
    unmapped_panels: set[str] = field(default_factory=set)

    def category_for(self, panel: str | None) -> str:
        """Map an FDA panel label to our category, defaulting and recording misses."""
        key = (panel or "").strip().lower()
        if not key:
            return self.default_category
        mapped = self._panels.get(key)
        if mapped is None:
            self.unmapped_panels.add((panel or "").strip())
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
    panels = {str(k).strip().lower(): str(v) for k, v in (raw.get("panels") or {}).items()}
    return SpecialtyTaxonomy(
        default_category=str(raw.get("default_category", "other")),
        _panels=panels,
        mortality_relevant_categories=frozenset(
            str(c) for c in (raw.get("mortality_relevant_categories") or [])
        ),
    )
