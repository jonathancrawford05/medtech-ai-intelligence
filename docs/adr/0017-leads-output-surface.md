# 0017 — Leads output surface: an append-only gold table, a CLI, and a fail-safe history walk

**Status:** Accepted · **Date:** 2026-10-04
**Relates to:** [ADR 0004](0004-config-driven-table-resolution.md), [ADR 0008](0008-generated-spark-schemas.md), [ADR 0014](0014-gold-mortality-mart.md), [ADR 0015](0015-silver-snapshot-pairing.md), [ADR 0016](0016-leads-filter-pre-curation-signals.md) · roadmap Issue 3
**Supersedes:** the "Reading history" paragraph of ADR 0015's amendment (Decision 3 below)

## Context

Issue 3 asks for a leads output that stakeholders query, a time series rather
than a static count, and a decided alerting surface. ADR 0015 Decision 6 said the
diff output is persisted append-only in gold, keyed by detection date and the
snapshot pair. This ADR fixes the concrete surface. It also tightens the history
walk that the differ and the silver rebuild gate share, because the differ is now
the second consumer of that walk.

## Decision 1 — `gold_device_leads`, append-only, one record per snapshot pair

- **Table.** `gold_device_leads`, resolved by logical name (ADR 0004). Its schema is
  generated from `schemas.LeadRecord` (ADR 0008, parity test). There is one row
  per (snapshot pair, submission, movement). Each row carries `detected_at`, both
  snapshot ids and versions, `movement`, `changed_fields`, `categories`, one
  boolean column per signal, `mortality_language_source`, and the device's facts
  including `decision_date`.
- **Append-only**, like bronze. Leads are history: the table *is* the
  rate-of-change series. Nothing overwrites or deletes it.
- **First detection.** If any lead already exists for a (previous, current)
  snapshot pair, a re-run appends nothing (`already_recorded`). Re-running the
  monitor is always safe. Leads are not revised later: re-enrichment on the same
  snapshot does not rewrite them, and the next snapshot's diff sees whatever
  changed. Two known limitations of keying on the pair of snapshot ids:
  - A list that oscillates A→B→A→B records the second A→B as `already_recorded`.
    That is rare enough to accept; the first A→B's leads stand.
  - A pair that yields no leads leaves no row, so a re-run diffs it again and
    reports 0 leads. This has no data effect. A run-log table would fix both and
    is a deliberate follow-up.
- **Empty but present.** The first run creates the table even with zero leads,
  so consumers can query it before anything moves (ADR 0014's convention).
- **Time series.** `leads.lead_counts(grain=...)` returns `(period, category,
  leads)`. It buckets by `detection` day, which is the rate of change in the
  list, or by `decision_month`, the clearance month, which is the rate of
  authorisation.

## Decision 2 — the alerting surface is the table plus `registry monitor`

`registry monitor` runs the diff, appends the leads, and prints the snapshot
pair, a count per live category, any suppressed removals (to stderr), and the
deferred categories with their reasons. It exits non-zero on a history barrier
(Decision 3). There is **no push notification yet**: no consumer has asked for
one, and the scheduled-ingest workflow can call `registry monitor` and surface its
output when that changes. A notification channel gets its own ADR then.

## Decision 3 — the history walk fails safe on unknown Delta operations

ADR 0015's amendment walked history over an allowlist of data-writing
operations and **skipped everything else**. Skipping is wrong for operations
nobody has listed. If an unlisted operation ever rewrote silver's rows, the walk
would step past it to an older stamp. The gate would then match that stale stamp
and skip a rebuild that was needed, leaving silver silently stale. The differ
would also pair across a change it cannot see.

`tables.walk_history` now classifies every commit three ways:

| Kind | Operations | Walk |
|---|---|---|
| Data write | `DATA_WRITE_OPERATIONS`: `WRITE`, `CREATE [OR REPLACE] TABLE AS SELECT`, `REPLACE TABLE AS SELECT`, `MERGE`, `UPDATE`, `DELETE`, `RESTORE` | a version to read its stamp from |
| Metadata-only | `METADATA_ONLY_OPERATIONS`: `SET`/`UNSET TBLPROPERTIES`, `VACUUM START`/`END`, `OPTIMIZE`, `CHANGE COLUMN`, `ADD COLUMNS`, `UPGRADE PROTOCOL` | skipped |
| Anything else | — | **stop**: reported as an unstamped write, with a warning naming the operation |

Consequences of the stop:

- **Gate.** `current_stamp()` is `None`, so the next `build-silver` writes. The
  cost of an unlisted harmless operation is one rebuild, never a stale table.
- **Differ.** It never pairs two versions across the stop. If fewer than two
  stamped snapshots sit above it, `differ.pairing()` returns no pair together
  with the barrier. `registry monitor` then reports it and exits 1 rather than
  diffing silently. Two snapshots built after the stop pair normally.
- **Recovery (runbook).** In the weekly `ingest → build-silver → monitor`
  sequence, only one stamped snapshot sits above a fresh stop, so an FDA change in
  that same cycle would never be diffed. On a barrier exit, run `registry
  build-silver` against the *current* bronze before the next ingest. That sets a
  new baseline above the stop, and the next pull diffs against it. If the
  operation is known never to change rows, add it to `METADATA_ONLY_OPERATIONS`
  instead. Expect Databricks maintenance operations (e.g. `REORG`) to show up as
  unknowns in the Phase 5 dry run.

Adding an operation to either list is a reviewed decision. Leaving one out costs
a rebuild.

## Consequences

- Stakeholders query one table. The leads are reproducible from silver's
  history, as long as Delta retention (ADR 0015 Decision 5) still holds the
  versions.
- Re-runs are idempotent at the pair level; scheduling the monitor needs no lock
  beyond Delta's own.
- `test_tables.py::test_skips_everything_else` became
  `test_skips_known_metadata_only_operations`. Its `"SOME FUTURE OPERATION"` case
  moved to `TestUnknownOperations`, which asserts the new stop behaviour. This is
  the intended behaviour change of Decision 3, not a weakening.

## Alternatives considered

- **Overwrite a "latest leads" table each run.** Rejected: it destroys the time
  series, which is the business value (roadmap §2).
- **Key idempotency on version numbers, not snapshot ids.** Rejected: a
  re-enrichment build would re-record the same movement as a new lead.
- **A report artifact (CSV/Markdown) instead of a table.** Deferred: a table is
  queryable and Databricks-portable. A report can be rendered from
  `lead_counts` when someone needs one.
- **Keep skipping unknown operations, as ADR 0015's amendment did.** Rejected
  (Decision 3): it fails open.
