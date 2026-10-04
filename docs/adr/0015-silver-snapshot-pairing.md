# 0015 — Pair adjacent silver builds by content snapshot, via Delta time travel

**Status:** Accepted in part — Decisions 3, 4 (as amended below) and 5, 2026-10-04 · Decisions 1, 2 and 6 still Proposed, to be accepted with the differ · **Date:** 2026-09-26
**Relates to:** [ADR 0001](0001-local-spark-delta-substrate.md), [ADR 0004](0004-config-driven-table-resolution.md), [ADR 0007](0007-two-stage-mortality-flag.md), [ADR 0014](0014-gold-mortality-mart.md) · roadmap Issue 3

## Context

Issue 3 (change monitoring) must diff **adjacent** silver builds to surface *movement*
— new mortality-relevant devices — as leads. Silver is derived state, rebuilt
`mode="overwrite"` from the latest bronze pull, one row per submission
(`bronze_to_silver.py`). The obvious instinct is to hold a temporary copy of the current
silver table before the load and drop it once differencing is done. That instinct is
wrong on three counts, and the substrate already provides the right primitive. This ADR
records the decision so the Issue 3 session inherits it rather than re-deriving it.

The grounding facts (verified by code inspection, [finding 0015](../../findings/0015-silver-snapshot-pairing.md)):
`tables.read_table` already exposes Delta time travel via `versionAsOf`, identically in
`path` and `catalog` modes; silver is overwritten each build, so every build is already a
retained Delta version; the build has the bronze `source_snapshot_id` in hand while
ranking, but does not carry it onto the silver row; and no Delta retention properties are
configured anywhere, so defaults are in force.

## Decision 1 — "adjacent" means adjacent *content snapshot*, not adjacent build

Bronze stamps every pull with a content-hash `source_snapshot_id` precisely because a
*build event* and a *change in the world* are different things. Pairing by build,
Delta-version number, or wall-clock time makes a harmless re-run look like a comparison,
and a manual rebuild for some unrelated reason look like a week of movement. Today's data
is exactly this trap: three bronze pulls, **one** distinct `source_snapshot_id`. The
correct pairing yields nothing; a build-event pairing would invent a diff.

## Decision 2 — pair via Delta time travel, not a temporary copy

The previous build is already retained by the overwrite's version history. Reading it is
a one-liner through the path that already exists — `tables.read_table(spark, settings,
SILVER_TABLE, version=prev)` — and it is identical on Databricks (Unity Catalog time
travel uses the same option, ADR 0004). No temporary table to create, sequence, or drop;
no lifecycle state to orphan if a run dies mid-flight.

## Decision 3 — stamp silver with the `source_snapshot_id` it consumed

`DeviceRecord` does not carry it today; it lives only on `BronzeFdaAiListRecord`, and the
build reads it while ranking bronze then drops it. Add it to the silver row (a column;
optionally also as Delta commit `userMetadata`) so each silver version is
**self-identifying**. The differ then pairs *distinct content snapshots* by matching the
stamped id across `DESCRIBE HISTORY`, never adjacent version numbers. Per ADR 0008 the
new field is added to the Pydantic model and the generated-schema parity test covers it.

## Decision 4 — gate the rebuild on snapshot change

If the latest bronze `source_snapshot_id` equals the one stamped on current silver, skip
the rebuild. This makes "empty on identical snapshots" **structural** rather than
something a test must catch, saves the compute, and models today's reality (no new world
→ no new silver version → no diff).

## Decision 5 — configure retention to outlive the build cadence

Time travel reaches back only as far as Delta retains its log and files:
`delta.logRetentionDuration` (default 30 days) and `delta.deletedFileRetentionDuration`
(default 7 days, what `VACUUM` enforces). **None are set today.** For a weekly build the
previous version must survive to the next run, so set both comfortably beyond the cadence
(≥ 90 days) on silver, and never `VACUUM` inside that window. This is the one real cost of
the time-travel approach; naming it is what keeps the differ from silently losing its
history to a routine `VACUUM`.

## Decision 6 — persist the leads (the diff output) append-only

The differ's result — added / removed / changed devices per lead category — is written to
the gold/`mart` layer **append-only**, keyed by detection date and the snapshot pair. That
append-only leads table *is* the rate-of-change time series Issue 3 needs (roadmap §2/§8),
and append-only is consistent with the bronze discipline. Silver history itself is not
retained beyond what time travel needs for the current diff.

## Consequences

- **No new storage machinery.** A stamp, a differ, a gate, and a retention property —
  against a read path that already exists. The temp-copy alternative is strictly more code.
- **Re-runnable, backfillable, auditable.** Any snapshot pair can be re-diffed after the
  fact; the copy-and-drop pattern destroys the prior state and forecloses all three.
- **Databricks-identical**, so the migration stays a config change (ADR 0004).
- **Cost:** retention must be *actively managed* (Decision 5) — the sole footgun. And
  silver gains a `source_snapshot_id` field plus its parity-test line (ADR 0008).

## Alternatives considered

- **Temporary copy of current silver before load, dropped after differencing** (the
  original proposal). Rejected. It reinvents time travel with mutable create/drop
  lifecycle; a crash between copy and load orphans state; it can only ever diff the one
  immediately-previous build (no re-run, backfill, or audit); and it is the least portable
  to Databricks (catalog permissions and cost on every create+drop). More code, fewer
  capabilities, worse failure modes — and, fatally, it pairs by build event, which
  Decision 1 rejects.
- **Diff at bronze, enrich only the delta.** Deferred, not rejected. Diffing bronze on
  `source_snapshot_id` and enriching only new/changed submissions is the *cost*
  optimisation, and it respects append-only. But it sees source-list movement only, blind
  to changes in derived/enriched fields (device_class, specialty, intended-use language).
  At ~1,614 rows weekly with cached enrichment, enrichment is not the bottleneck, so this
  is premature. Revisit — its own ADR — if enrichment cost ever bites.
- **MERGE-upsert silver + Change Data Feed.** Deferred. CDF is the native row-level-change
  answer, but it is **useless with overwrite** — an overwrite reads as delete-all +
  insert-all, so CDF reports every row changed. It pays off only if the silver write
  becomes a `MERGE` keyed on `submission_number`, a real increase in build complexity. The
  future path if per-row change provenance (which field changed, when) is ever needed; not
  worth the complexity for a weekly full-refresh of 1,614 rows now.

## Amendment — 2026-10-04, on implementing Decisions 3–5

Recorded while implementing, before acceptance, so the accepted text matches the code.
Decisions 1, 2 and 6 are untouched.

**Decision 3, refined — two stamps, because silver rows come from different pulls.**
Silver keeps the newest bronze row *per submission*, so a device that has left the list
keeps the row (and the snapshot id) of the last pull that carried it. "The
`source_snapshot_id` silver consumed" is therefore two different facts, and each is
stamped where it is true:

- **Row:** `DeviceRecord.source_snapshot_id` (required, non-null) is the snapshot of the
  bronze row that row was read from — honest lineage.
- **Commit:** the overwrite records a `BuildStamp` as Delta `userMetadata` —
  `{"source_snapshot_id": <newest pull's id>, "content_hash": <sha256 of the rows>}`. This
  is what makes a *version* self-identifying, and it is what `DESCRIBE HISTORY` exposes,
  which is where Decision 3 already said the differ pairs. Read via
  `tables.table_history`.

A consequence the differ (Decision 6) should use: a row whose stamp differs from its
version's commit stamp is a device **no longer on the list**. Diffing silver alone could
never show a removal otherwise, because the row is never dropped.

**Decision 4, amended — gate on snapshot *and* content, not snapshot alone.** Silver is
not a function of bronze alone: `build-silver` also reads `silver_device_enrichment`
(`device_class`, ADR 0013) and the curated taxonomy and company configs. The documented
workflow is `ingest → enrich-openfda → build-silver`, so a gate keyed on the bronze
snapshot alone would skip the post-enrichment build and `device_class` would never land
(likewise any taxonomy or alias edit). The gate therefore builds the rows in memory
(trivial at ~1,614) and **skips the write only when both the newest bronze snapshot id
and the content hash equal the current version's stamp**. "No new version when nothing
changed" stays structural — the property Decision 4 was for — without a manual `--force`
to remember. Pairing (Decision 1) is still by bronze snapshot id: two versions with the
same snapshot id but different content (re-enrichment) are the same world, and the
differ compares the newest version of each distinct snapshot.

**Decision 5, as implemented.** `delta.logRetentionDuration` and
`delta.deletedFileRetentionDuration` are both `interval 90 days` on `silver_devices`,
set after the write only when they differ from the table's current properties (each
`SET TBLPROPERTIES` is its own commit; the gate looks past such metadata-only commits).

## Status note

Decisions 3–5 are implemented and tested (`tests/test_bronze_to_silver.py::TestSnapshotGate`,
`TestBuildStamp`; `tests/test_schemas.py::TestSnapshotStamp`) and accepted as amended above.
Decisions 1, 2 and 6 are accepted when the Issue 3 differ lands with its tests
([handoff §4](../handoffs/issue-3-change-monitoring.md)); the time-travel read path they rely
on is already exercised by `test_the_previous_build_is_readable_by_time_travel`.
