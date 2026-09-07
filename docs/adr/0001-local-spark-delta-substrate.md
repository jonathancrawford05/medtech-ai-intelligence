# 0001 — Local Spark + Delta Lake as the Databricks substrate

**Status:** Accepted · **Date:** 2026-09-06

## Context

The registry is a prototype, but its intended home is Databricks. The risk with
prototypes is that the transformation logic gets written against whatever was
convenient locally and then has to be rewritten at migration time — at which
point the prototype has taught us about the data but not about the pipeline.

Databricks *is* Spark + Delta Lake + Unity Catalog. Anything we write against a
different engine is a translation problem deferred, not avoided.

## Decision

Use local-mode PySpark (`local[*]`) with open-source Delta Lake (`delta-spark`)
as the development substrate. Persist every layer (bronze/silver/gold) as Delta
tables.

Local mode runs a complete Spark instance in a single process — no cluster — and
`delta-spark` provides genuine ACID Delta tables on the local filesystem with the
same MERGE semantics, schema enforcement and time travel that Databricks uses.

## Consequences

- Transformation code is written once, against the Spark DataFrame API.
- Migration is a matter of pointing reads and writes at a different catalog
  (see [0004](0004-config-driven-table-resolution.md)), not rewriting logic.
- We pay for it in startup cost: Spark needs a JVM, and a local session takes a
  few seconds to come up. Test suites must account for that (the SparkSession
  fixture is session-scoped, and JVM-free tests are marked so they can be run
  alone via `make test-fast`).
- Local resource footprint is heavier than a single-file engine would be.

## Alternatives considered

**Pandas only.** Fastest to write, but MERGE/upsert, schema evolution and time
travel would all be hand-rolled and then thrown away at migration. That is the
opposite of the goal.

**DuckDB (+ delta-rs for reading Delta).** Genuinely excellent and much lighter;
worth keeping as a documented fallback if Spark's local footprint becomes
painful. Rejected as the default because DuckDB's SQL and write path are not the
engine Databricks runs, so business logic written as DuckDB SQL needs a
translation pass later — the very cost we are trying to avoid.

**SQLite.** Fine for tiny curated lookups, and indeed the specialty taxonomy is
just a YAML file. Not suitable for the versioned, growing device and evidence
tables that are the point of the registry.
