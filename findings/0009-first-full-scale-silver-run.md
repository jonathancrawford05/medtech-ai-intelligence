# 0009 — The first 1,614-row silver run: what it confirmed, and two bugs it found

**Date:** 2026-09-15 · **Status:** Bugs fixed; curation backlog open · **Component:** `transform/`, `lakehouse_report`

The transform had only ever run over a 14-row fixture. Running it over the real
pull on a machine that can reach `fda.gov` closed the last inspection-versus-execution
gap in Phase 2 — and, as in [finding 0008](0008-taxonomy-spelling-mismatch.md),
execution found things the green suite could not.

## Confirmed at full scale

| | |
|---|---|
| Bronze | 3,228 rows across 2 pulls, 1,614 distinct submissions, **0 missing values in any raw column** |
| Silver | 1,614 rows, 1,614 distinct submissions — one row per submission, as the grain requires |
| Pathway | 510(k) 1,553 · De Novo 40 · PMA 21 |
| PMA supplements | 4, each keeping its whole key and splitting correctly (`P980016/S939` → base `P980016`) |
| Decision years | 1995–2026, rising 18 (2016) → 335 (2025); 2026 at 181 is a partial year |

The date parse holds against a 31-year span, and the oldest row (`P950009`,
AUTOPAP, 1995) is a genuine pre-amendment PMA rather than a parse artefact.

**The panel-label normalisation from finding 0008 earned its keep.** The export
spells the obstetrics panel `Obstetrics and Gynecology` where our config says
`Obstetrics/Gynecology`. Those normalise to the same key, so 5 rows mapped
correctly with no curation. Before that change they would have joined the
`other` bucket silently.

## Bug 1 — the FDA misspells its own panel

5 rows fell to `other`, all on panel **`Clinical Toxcicology`**. Our config says
`Clinical Toxicology`, which is the correct spelling; the FDA's export has the
transposition.

Normalisation collapses punctuation, case and connector words — it does not and
should not collapse transpositions. The fix is an explicit second key mapping to
the same category, with a comment saying why.

**Fuzzy matching was considered and rejected.** Edit-distance matching would have
caught this one, and would also silently map a genuinely new panel onto whichever
existing category it happened to resemble — destroying the unmapped-panel report
that is the only reason this was visible at all. A curated misspelling is
honest; a guessed one is a silent wrong answer. `test_a_misspelling_we_have_not_seen_is_still_reported`
pins that: `"Cardiovasular"` must still surface as uncurated.

## Bug 2 — `registry inspect` reported two pulls as one

The report said:

```text
  rows across all pulls       : 3,228
  distinct submission numbers : 1,614
    2026-09-13 14:57:49  b1efeb680371f44c    3,228 rows
  -> 1 pull(s) retained
```

3,228 rows at 1,614 distinct submissions is exactly two pulls, and the report
said one. The cause: it grouped on `source_snapshot_id` alone. That field is a
**content hash**, so re-ingesting an unchanged export reuses it — which is the
normal case, not an edge case, because the FDA list does not change daily.

Grouping on `(source_snapshot_id, ingested_at)` lists each pull separately and
marks a repeated hash as *unchanged since an earlier pull*. The summary line now
gives both numbers: pulls retained, and distinct source snapshots.

This one is worth noting for what it says about the inspector's own design:
`inspect` exists to make the append-only invariant visible, and it was hiding
precisely the accumulation it was built to show. A verdict tool that is wrong is
worse than no verdict tool.

## What the run made buildable

The product-code measurement it produced — **1,614 devices across 181 distinct
product codes** — is what settled the enrichment architecture in
[ADR 0013](../docs/adr/0013-openfda-enrichment-architecture.md). Keying the class
lookup on product code rather than submission is a ~9x reduction in API calls, and
that was a measurement from this run, not an assumption.

## Open — the curation backlog the run exposed

Not bugs; the inspector doing its job.

- **Three GE entities resolve separately**: `GE HealthCare` (42), `GE Medical
  Systems Ultrasound and Primary Care Diagnostics` (22), `GE Medical Systems SCS`
  (12). One parent, 76 authorisations. `config/company_aliases.yaml` needs them.
  The same is likely true elsewhere in the long tail.
- **Radiology is 76% of the registry** (1,230 of 1,614). Any trend statement that
  does not segment by specialty is a statement about radiology AI. Worth stating
  plainly in whatever the business sees.
- **Cardiovascular is 154 rows** — the mortality-relevant core is small enough to
  review by hand, which makes the two-stage flag (ADR 0007) tractable now rather
  than aspirational.
