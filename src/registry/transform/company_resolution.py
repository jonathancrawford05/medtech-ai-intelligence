"""Resolve an FDA applicant string to a parent company.

The FDA records the applicant exactly as filed, so one company appears under many
spellings and acquisitions mean yesterday's applicant belongs to today's parent.
Automating M&A tracking is an explicit non-goal (development plan section 6), so
the source of truth is `config/company_aliases.yaml`, curated by hand.

What this module owes that curation is robust *matching*: a single alias should
cover the spelling variants the FDA actually publishes. Matching is therefore on
a normalised form — lowercased, punctuation dropped, trailing corporate suffixes
removed — so "Aidoc Medical Ltd", "Aidoc Medical, Ltd." and "AIDOC MEDICAL LTD"
all reach one entry.

An unrecognised applicant resolves to its own cleaned name rather than to null:
"we have not curated this" is not the same as "this device has no applicant", and
a readable fallback keeps the silver table usable. Misses are recorded in
`unmatched_applicants` so they can be curated deliberately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from registry.config.settings import Settings, get_settings

# Trailing corporate forms, stripped for matching only. Order matters: longer
# multi-word forms first so "pty ltd" is not left as a bare "pty".
_SUFFIXES = (
    "pty ltd",
    "co ltd",
    "s r l",
    "s p a",
    "incorporated",
    "corporation",
    "limited",
    "company",
    "inc",
    "ltd",
    "llc",
    "lp",
    "plc",
    "corp",
    "co",
    "gmbh",
    "mbh",
    "ag",
    "nv",
    "bv",
    "sa",
    "sas",
    "srl",
    "spa",
    "oy",
    "ab",
    "as",
    "kk",
    "kg",
    "pty",
    "pte",
    "sdn bhd",
    "aps",
    "sarl",
)
_PUNCT = re.compile(r"[.,/#!$%^&*;:{}=\-_`~()'\"]+")
_SPACE = re.compile(r"\s+")


def normalise(name: str) -> str:
    """Reduce an applicant string to a comparable key."""
    text = _PUNCT.sub(" ", (name or "").lower())
    text = _SPACE.sub(" ", text).strip()
    # Dotted abbreviations survive punctuation removal as separate letters:
    # "N.V." -> "n v", "S.p.A." -> "s p a". Rejoin a trailing run of
    # single-character tokens so those meet the suffix list as "nv" / "spa".
    tokens = text.split()
    tail: list[str] = []
    while tokens and len(tokens[-1]) == 1:
        tail.insert(0, tokens.pop())
    if len(tail) > 1:
        tokens.append("".join(tail))
    elif tail:
        tokens.extend(tail)
    text = " ".join(tokens)
    # Strip corporate suffixes repeatedly: "Foo Medical Co., Ltd." leaves two.
    changed = True
    while changed:
        changed = False
        for suffix in _SUFFIXES:
            if text.endswith(f" {suffix}"):
                text = text[: -len(suffix) - 1].strip()
                changed = True
    return text


def _titlecase_fallback(name: str) -> str:
    """Clean an uncurated applicant for display, preserving its own casing."""
    text = _SPACE.sub(" ", (name or "").strip().rstrip(",. "))
    # Drop one trailing corporate suffix for readability, keeping original case.
    lowered = text.lower()
    for suffix in _SUFFIXES:
        for form in (f" {suffix}", f" {suffix}."):
            if lowered.endswith(form):
                return text[: -len(form)].rstrip(",. ")
    return text


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving one applicant string."""

    resolved_name: str | None
    parent: str | None
    matched: bool


@dataclass
class CompanyLookup:
    """A loaded alias table. `resolve` records misses as a side effect."""

    _by_key: dict[str, tuple[str, str | None]]
    unmatched_applicants: set[str] = field(default_factory=set)

    def resolve(self, applicant: str | None) -> Resolution:
        if not applicant or not applicant.strip():
            return Resolution(resolved_name=None, parent=None, matched=False)

        hit = self._by_key.get(normalise(applicant))
        if hit is not None:
            resolved_name, parent = hit
            return Resolution(resolved_name=resolved_name, parent=parent, matched=True)

        self.unmatched_applicants.add(applicant.strip())
        return Resolution(resolved_name=_titlecase_fallback(applicant), parent=None, matched=False)


def load(settings: Settings | None = None) -> CompanyLookup:
    """Read the curated alias table from `config/company_aliases.yaml`."""
    settings = settings or get_settings()
    path: Path = settings.company_aliases_path
    if not path.exists():
        raise FileNotFoundError(
            f"Company alias lookup not found at {path}. It is hand-maintained config; "
            "see config/company_aliases.yaml in the repo."
        )
    raw = yaml.safe_load(path.read_text()) or {}

    by_key: dict[str, tuple[str, str | None]] = {}
    for entry in raw.get("companies") or []:
        resolved_name = str(entry["resolved_name"])
        parent = entry.get("parent")
        parent = str(parent) if parent else None
        # The resolved name is itself a valid alias.
        for alias in [resolved_name, *(entry.get("aliases") or [])]:
            by_key[normalise(str(alias))] = (resolved_name, parent)
    return CompanyLookup(_by_key=by_key)
