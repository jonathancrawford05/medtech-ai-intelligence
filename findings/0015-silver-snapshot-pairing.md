# 0015 — What the code already provides for silver snapshot pairing

**Verified by code inspection on 2026-09-26** · grounds the decision in
[ADR 0015](../docs/adr/0015-silver-snapshot-pairing.md). This finding records *what was
checked and observed* in the current tree — not the decision, which is the ADR's job.

The question behind Issue 3 is how to diff "adjacent" silver builds. Before choosing a
mechanism, the honest first step is to see what the substrate and the current code
already give, so the choice is not made against an imagined baseline.

## Verified (read in the tree at this commit)

- **Delta time-travel reads already exist and are storage-mode-agnostic.**
  `tables.read_table` passes `versionAsOf` when a `version` is given
  (`src/registry/tables.py:46`), and the `catalog` branch takes the same option via
  `format("delta").table(...)` (`tables.py:49-50`). So "read the previous silver build"
  is already a one-liner, and it is identical locally (`path`) and on Databricks
  (`catalog`) — no new read machinery is needed.
- **Silver is overwritten each build**, `mode="overwrite"`, `merge_schema=True`
  (`src/registry/transform/bronze_to_silver.py:230`). Consequence: every build already
  creates a retained Delta version — the "previous copy" the temp-table idea would create
  by hand already exists in the table's history.
- **The build holds the snapshot id but drops it.** It ranks bronze by `ingested_at` then
  `source_snapshot_id` (`bronze_to_silver.py:183`), so the content hash is in hand at
  build time, but it is not propagated onto the silver row.
- **Silver is not self-identifying by content snapshot.** `source_snapshot_id` is a field
  of `BronzeFdaAiListRecord` (`src/registry/schemas.py:116`), **not** of `DeviceRecord`
  (silver, `schemas.py:124`). A silver version therefore cannot today be matched back to
  the bronze snapshot that produced it without an external join — which is why ADR 0015
  Decision 3 adds the stamp.
- **No Delta retention or CDF properties are configured anywhere.** A grep of `src/` for
  `logRetention` / `deletedFileRetention` / `enableChangeDataFeed` / `TBLPROPERTIES`
  returns nothing, so the defaults are in force: log retention 30 days, deleted-file
  retention 7 days (the `VACUUM` floor). Time travel is currently **un-guarded** for a
  weekly cadence — the footgun ADR 0015 Decision 5 addresses is real and unmitigated today.
- **There is no movement to diff yet.** Bronze holds two-to-three pulls sharing one
  content hash (`b1efeb680371f44c`) — a single distinct snapshot. "Empty on identical
  snapshots" is not a hypothetical edge case here; it is the live state, and any pairing
  keyed on the build event rather than the content hash would fabricate a comparison.

## Assumed / not yet verified (reasoned; the Issue 3 session must prove)

- That a Spark-Delta time-travel read of the previous version performs acceptably at this
  scale. Trivially expected at ~1,614 rows, but unmeasured here.
- That gating the rebuild on `source_snapshot_id` equality correctly suppresses a
  redundant build. It is a design (ADR 0015 Decision 4); the gate does not exist yet, so
  it is untested.
- That the retention `TBLPROPERTIES` path survives the local→Databricks move unchanged.
  ADR 0004 says table config is engine-agnostic, but the retention path specifically has
  not been exercised in either engine.

## So what

The temp-copy-and-drop instinct would build mutable lifecycle to reproduce a versioned
read that `tables.read_table(..., version=)` already exposes and that Databricks honours
unchanged. The genuinely missing pieces are small and named in ADR 0015: a
`source_snapshot_id` stamp on silver, a rebuild gate, a retention policy, and the differ
itself. Nothing here has to be *replaced*; it has to be *stamped, gated, and read back*.
