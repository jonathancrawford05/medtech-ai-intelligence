# 0008 — A curated-config spelling mismatch that only execution could find

**Date:** 2026-09-13 · **Status:** Fixed · **Component:** `transform/taxonomy.py`, `config/`

## What happened

Running the silver build over a real slice of the FDA curated list and then
reading the result back, **2 of 14 rows sat in the `other` specialty**. Nothing
was broken: `make test` was green, coverage was 90%, and the taxonomy's own tests
all passed.

The cause was one character. `config/specialty_taxonomy.yaml` said:

```yaml
General & Plastic Surgery: surgery
```

The FDA's own export says `General and Plastic Surgery`. The lookup lowercased
and stripped but otherwise compared literally, so the panel missed, fell to
`default_category`, and was recorded in `unmapped_panels` — where nobody was
looking, because nothing printed it.

## Why the test suite could not have caught it

Every taxonomy test used panel labels **written by us**, in the same file as the
assertions. A hand-written fixture agrees with a hand-written config by
construction. The mapping is only wrong relative to a third party's spelling, and
no test consulted the third party.

This is the general shape: **curated config validated against curated fixtures is
self-consistent and can still be entirely wrong.**

## The fix, in two parts

1. **Normalise on lookup** (`taxonomy._normalise`): lowercase, reduce every
   non-alphanumeric run to a space, and drop connector words (`and`, `the`, `of`).
   `"General & Plastic Surgery"`, `"General and Plastic Surgery"` and
   `"General, Plastic Surgery"` all become `general plastic surgery`. Likewise
   `Gastroenterology-Urology` and `Gastroenterology/Urology`. `load()` now raises
   if two curated labels collide under normalisation, so a config that would let
   one silently overwrite another fails at load instead.
2. **Test against the real export, not our own spelling.**
   `test_the_repo_config_covers_every_panel_in_the_real_fda_export` reads the
   panel column out of `tests/fixtures/fda_ai_list_sample.csv` — a verbatim slice
   of the FDA list — and asserts no panel in it lands in `other`. This is the
   test that would have failed on day one.

The config was also corrected to the FDA's spelling. Normalisation means both
work; matching the primary source is the point.

Mutation-checked: removing the normalisation from `category_for` fails 8 tests;
removing only the connector-stripping fails 2.

## The durable lesson, and what it produced

The bug was found by *looking at the data*, and looking at the data required
writing throwaway PySpark. So the same change adds **`registry inspect`**
(`src/registry/lakehouse_report.py`) and makes it a judgement rather than a dump:
it exits non-zero, and rows in the default specialty are a **problem** that names
the offending panel, not a row in a frequency table. The next spelling drift
announces itself.

## Open

- The same class of mismatch is untested for `config/company_aliases.yaml`.
  Applicant names in the real export are far more varied than panels, so the
  equivalent check needs a coverage threshold rather than a zero-miss assertion.
