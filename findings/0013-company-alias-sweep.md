# 0013 — Company alias sweep: the GE problem, and the tail

**Date:** 2026-09-20 · **Status:** `config/company_aliases.yaml` extended · **Component:** `config/company_aliases.yaml` (curated config; no change to `transform/company_resolution.py`)

[finding 0009](0009-first-full-scale-silver-run.md) flagged that three GE entities resolved
separately (76 authorisations across them) and that `company_aliases.yaml` had "the same
untested-against-reality hole that `specialty_taxonomy.yaml` had." This closes that for the
applicants that actually distort the top-of-tail, using only curated aliases — **no matching
logic changed** (that would be code and an ADR; fuzzy matching was rejected for good reason in
finding 0009, and the same reasoning applies here).

## Method

Ran the shipped `normalise()` + alias loader (`transform/company_resolution.py`) over every
`applicant_raw` in `silver_devices` (1,614 rows), before and after editing the YAML, and read
off `CompanyLookup` matched/unmatched. Matching is **exact on the normalised form** (lowercase,
punctuation dropped, trailing corporate suffixes stripped), so spellings whose normalised form
differs must each be listed — that is why several GE variants appear explicitly.

## The GE problem — bigger than finding 0009 said

The three entities finding 0009 named were the three largest divisions. The full picture: GE
files under the **US ultrasound division** ("GE Medical Systems Ultrasound and Primary Care
Diagnostics", with `&`/`and`, singular/plural and truncated variants that normalise to *five*
distinct keys), **SCS** (France), **Tianjin / Hualun / Hangwei** (China) and **Israel /
Functional Imaging** — plus the already-matched "GE Medical Systems, LLC". Merged, they are one
parent.

| | GE HealthCare authorisations |
|---|---|
| Before sweep (only `GE Medical Systems*` matched) | 42 |
| After sweep (all divisions merged) | **109** |

**GE HealthCare is the registry's single largest applicant — ahead of Siemens Healthineers
(96) — not the 3rd–5th place its split made it look.** Any "top applicants" view built before
this was wrong at the top.

## The rest of the tail

Whole-registry effect of the sweep:

| | Before | After |
|---|---|---|
| Applicant rows matched to a curated parent | ~390 | **760** |
| Unmatched rows | 1,224 | 854 |
| Distinct unmatched applicants | 745 | 630 |

Two kinds of fix, both high-confidence (same legal entity under spelling/division variants,
never a guess that two *different* names are one company):

1. **Missed spellings of already-curated multinationals** — the GE-style problem elsewhere:
   Philips (Nederland / Suzhou / Technologies / DMC / MR Finland / France), Siemens (Medical
   Solutions, singular "Solution"), Canon (Inc., Medical Informatics), United Imaging (Shanghai
   United Imaging Intelligence, Wuhan), Fujifilm (Healthcare Americas, Sonosite), Medtronic
   (Medicrea "(Medtronic)", Cardiac Rhythm Disease Management), Boston Scientific (Cardiac
   Diagnostic Technologies), Nanox (NanoxAI).
2. **Single-company AI/imaging applicants** consolidated to a canonical name (iSchemaView,
   Clarius, Hyperfine, Ever Fortune.AI, Overjet, Brainlab, Subtle Medical, Quantib, DiA,
   Circle Cardiovascular Imaging, Qure.ai, DeepHealth, Brainomix, BunkerHill, RaySearch, Pearl,
   MIM Software, Avicenna.AI, ScreenPoint, Tyto Care, iCAD, TheraPanacea, Esaote, AliveCor,
   Oxehealth, iRhythm, Surgical Information Sciences, Therapixel, Anumana, Radformation, Volta
   Medical). Several of these had case/spelling variants the `_titlecase_fallback` left split in
   `applicant_resolved` (e.g. "iSchemaView" vs "iSchemaview"); a curated entry unifies them.

## Left separate — deliberately

Per the handoff ("an incorrect merge is invisible and permanent; an unmerged pair is obvious
and fixable"):

- **`Merge Healthcare Incorporated`** and **`Change Healthcare Canada Company`** — these only
  brushed a GE keyword scan ("mer**ge** healthcare", "chan**ge** healthcare"). They are **not
  GE** (IBM/Merative and Optum lineage). Left separate.
- **`Varian Medical Systems`** (4) — now owned by Siemens Healthineers (acquired 2021), but it
  still files and operates under its own name. This is an **M&A merge**, which the alias file's
  own header calls out as needing deliberate curation rather than automation — flagged here for
  a decision, not merged unilaterally.
- **`Arterys`** — acquired (Tempus, 2023); same M&A caveat, left separate.
- The long low-volume tail of genuinely distinct small companies (≤4 authorisations each) is
  left to the readable `_titlecase_fallback`; it is not distortion, and curating it further is
  optional polish, not correctness.

## Validation

Re-ran resolution after the edit: **no alias-key collisions across different resolved names**;
GE consolidates to a single `GE HealthCare` entry (109); Merge/Change Healthcare remain
unmatched (correctly). A speculative `RapidAI` entry was removed after confirming no applicant
string resolves to it (the applicant is always "iSchemaView"). `config/company_aliases.yaml`
parses; 51 curated companies.

## The standing gap (unchanged, by design)

The alias table still has **no coverage test** — and it should not get a zero-miss one, for the
same reason the taxonomy didn't (finding 0009): a zero-miss test would force every genuinely new
applicant to be curated before silver could build. The right guard, if one is added later, is a
**coverage threshold** ("≥ N% of authorisation *volume* resolves to a curated parent"), not a
per-applicant assertion. `CompanyLookup.unmatched_applicants` already records every miss for
periodic manual curation, which is the intended workflow.
