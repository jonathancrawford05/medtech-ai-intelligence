# 0002 — Pin Spark 4.0 / Delta 4.0 / JDK 17 to match DBR 17.x LTS

**Status:** Accepted · **Date:** 2026-09-06

## Context

[0001](0001-local-spark-delta-substrate.md) only pays off if the local engine
behaves like the target. Spark and Delta are tightly coupled: a given
`delta-spark` release supports a narrow Spark range, and Spark supports a narrow
JDK range. Drifting from the Databricks Runtime's combination reintroduces the
migration risk we chose Spark to avoid.

## Decision

Pin exactly:

| Component | Version | Why |
|-----------|---------|-----|
| `pyspark` | `4.0.1` | Spark 4.0 is the engine in Databricks Runtime 17.x LTS |
| `delta-spark` | `4.0.1` | Delta 4.0 is what DBR 17.x ships |
| JDK | 17 | DBR 17.x runs on JDK 17 (Spark 4.0 also supports 21) |
| Python | 3.11 | Comfortably inside Spark 4.0's supported range |

Both Python pins are exact (`==`), not ranges. `uv.lock` is committed.

## Consequences

- Local behaviour matches the migration target closely, including Delta
  protocol versions and writer features.
- Upgrades are deliberate: bumping Spark means bumping Delta and re-checking the
  JDK together, and should reference the DBR version being targeted.
- Newer `delta-spark` releases (4.4.x, pairing with Spark 4.2) are deliberately
  *not* taken. Being ahead of the runtime is as much a mismatch as being behind.

## Notes

The build environment used to develop this ran JDK 21 and worked fine, which
confirms Spark 4.0's stated 17-or-21 support. The Docker image standardises on
17 anyway, so contributors are not relying on that.
