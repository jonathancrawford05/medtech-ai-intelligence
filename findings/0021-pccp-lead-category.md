# 0021 — PCCP lead category and the adoption-by-year table (PR 4B+): built; the real-lakehouse table is open

**Date:** 2026-10-11 · **Status:** Built and verified on synthetic rows in a cloud session; the full-scale `inspect` table is **not yet run** (it needs the maintainer's lakehouse) · **Component:** Issue 4 PR 4B+, `mart/leads.py`, `lakehouse_report.py` ([ADR 0020](../docs/adr/0020-pccp-lead-category.md))

## Verified here (pytest; synthetic rows, by necessity)

The cloud session has no lakehouse and cannot reach `fda.gov`. Silver snapshots
with real movement do not exist anywhere yet (one distinct snapshot, finding
0017), so the lead tests use the synthetic snapshots in `tests/monitor_fixtures.py`.

- **Populating `has_pccp` records no leads.** `test_leads.py::TestPccpSignal`:
  every existing device goes from `has_pccp = None` to `True`/`False` between two
  snapshots → `differ.classify` returns no movement, `build_leads` returns `[]`. A
  `True` ↔ `False` flip (an extractor-version bump) records nothing either. The
  differ-level test from PR 4B (`test_the_document_pass_alone_is_not_movement`)
  still holds; `SOURCE_FIELDS` is unchanged.
- **Signal semantics.** `signal_pccp` is `True` when either side is true, `False`
  when read and true on neither, `None` when neither side was read; the `pccp`
  category is set exactly when the signal is `True`. An added device, a changed
  device (PCCP on either side) and a removed device are each tagged; a renamed
  radiology device with no PCCP is still not a lead.
- **Schema.** `LeadRecord.signal_pccp` is nullable in the generated Spark schema;
  `pccp` passes the category validator; `foundation_model` is still rejected and
  is the only entry left in `DEFERRED_CATEGORIES`.
- **The existing leads table widens.** A plain Delta append to a
  `gold_device_leads` written before `signal_pccp` failed with a schema mismatch
  (reproduced in a test); `registry monitor` now appends with schema merge, and the
  older rows read `None`. Without this, the first lead after this PR would have
  crashed `monitor` on the maintainer's lakehouse.
- **The year table.** `test_pccp_trend.py` builds `(year, pccp_method)` rows shaped
  like finding 0019's counts (160 / 217 / 224 / 327 / 173 documents; 0 / 1 / 17 /
  31 / 19 letter sentences) plus Summary positives, and asserts the
  `clearance_letter` column renders finding 0019's cells exactly —
  `0 / 160 (0.0%)`, `1 / 217 (0.5%)`, `17 / 224 (7.6%)`, `31 / 327 (9.5%)`,
  `19 / 173 (11.0%)` — with the Summary positives in their own column, not moving
  the letter column. `test_lakehouse_report.py` checks the Spark join of
  `silver_document_extraction` to `silver_devices` and the orphan count.

This shows the computation and rendering match finding 0019's method. It does
**not** show the real lakehouse reproduces finding 0019's numbers. That needs a run.

## Open: the real table (maintainer's Mac)

```bash
uv run registry extract-documents -v   # 4b.2, if not yet re-run
uv run registry build-silver
uv run registry inspect                # EXTRACTION -> "PCCP share by decision year"
```

Expected: the `clearance_letter` column equals finding 0019's table (68 letter
positives total, all in 2023–2026), and `summary_statement` totals 5 at `4b.2`
(`K201992`, `K210484`, `K232699`, `K243449`, `K260714`), spread across whatever
years those devices were decided. If a letter cell differs, the likely cause is
the denominator. Finding 0019 bucketed "Summary documents that year". This table
buckets extracted Summaries by silver's `decision_date`, and a document whose
device is not in silver is counted on its own line. Record the observed table here.

## Assumed

- Finding 0019's year buckets used the FDA-list decision date, the same date
  silver carries. Finding 0019 does not state this outright.
- A new device usually reaches silver before its Summary is fetched, so its first
  lead will often read `signal_pccp = None`. Not measured: no second snapshot exists.

## How to re-check

```bash
uv run pytest -q tests/test_pccp_trend.py tests/test_leads.py -m "not spark"
uv run pytest -q tests/test_lakehouse_report.py -k pccp    # needs JDK 17+
```
