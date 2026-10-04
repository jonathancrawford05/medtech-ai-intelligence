# 0016 — Silver snapshot stamp, rebuild gate and retention: what was verified

**Verified by test on 2026-10-04**, local Spark 4.0.1 / Delta 4.0.1 on host **JDK 21**
(the repo's JDK-17 image could not be built in the agent container, see below), plus
ruff. Implements [ADR 0015](../docs/adr/0015-silver-snapshot-pairing.md) Decisions 3–5
as amended. Follows [finding 0015](0015-silver-snapshot-pairing.md), which listed the
three assumptions below as unproven.

## Verified

- **Rows carry lineage.** Every `silver_devices` row now has a non-null
  `source_snapshot_id` from the bronze row it was read from. A device present at
  `snapA` and gone at `snapB` keeps `snapA`; the device still listed gets `snapB`
  (`TestSnapshotGate::test_each_row_keeps_the_snapshot_it_was_read_from`).
- **Commits carry a build stamp.** The overwrite records
  `{"source_snapshot_id", "content_hash"}` as Delta `userMetadata`, and it reads back
  through `DeltaTable.history()` in path mode. Finding 0015's assumption that a
  version can be found by what it was built from is now true for path mode.
- **The gate suppresses redundant builds** (finding 0015's second assumption). Same
  bronze → `run` returns 0 and the version list is unchanged. A *new pull with the same
  content hash* — the live state today, three pulls one snapshot — also writes nothing.
- **The gate still lets real changes through.** A new snapshot rebuilds and restamps;
  enrichment landing with no new bronze rebuilds and `device_class` arrives; a list that
  reverts A → B → A rebuilds rather than mistaking B's silver for A's.
- **The tests bite.** Mutation check: gating on the bronze snapshot alone fails
  `test_enrichment_landing_rebuilds_even_on_the_same_snapshot`; removing the gate fails
  both no-new-version tests. Restored before commit.
- **Retention is set, once.** `delta.logRetentionDuration` and
  `delta.deletedFileRetentionDuration` are `interval 90 days` after the first build, and
  a second build does not add a second `SET TBLPROPERTIES` commit.
- **Time travel returns the previous build** with its own row and commit stamps, through
  the existing `tables.read_table(..., version=)`, with no temporary copy.
- **An unstamped silver is upgraded, not skipped.** A silver table written without the
  column or the metadata (the live lakehouse today) is rebuilt and gains the column via
  `mergeSchema`.

## Assumed / not yet verified

- **Catalog mode.** `table_history` / `table_properties` use `DeltaTable.forName` and the
  `ALTER TABLE <ref> SET TBLPROPERTIES` form there; neither is exercised against a real
  Unity Catalog workspace (finding 0015's third assumption stays open for catalog mode).
- **The JDK-17 image.** All Spark tests ran on JDK 21. `deb.debian.org` is denied by the
  agent environment's egress policy, so `docker compose build` fails at the
  `apt-get install openjdk-17-jdk-headless` step (HTTP 403). CI's image run is the
  authoritative JDK-17 result.
- **The real lakehouse's first stamped build.** Not run here (no lakehouse in the agent
  container). Expected: one rebuild that adds the column and sets retention, then
  0-row "already current" on every re-run until the FDA list changes.

## How to re-check

```bash
uv run pytest -q -m spark tests/test_bronze_to_silver.py -k "SnapshotGate or BuildStamp"
uv run registry build-silver -v   # on a real lakehouse: first run writes, second says "already current"
```

Inspect a real table's stamp with
`DeltaTable.forPath(spark, path).history().select("version", "operation", "userMetadata")`.
