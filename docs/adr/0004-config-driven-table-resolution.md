# 0004 — Logical table names resolved through config

**Status:** Accepted · **Date:** 2026-09-06

## Context

[0001](0001-local-spark-delta-substrate.md) promises that migrating to Databricks
touches almost no code. That promise is only kept if no transformation module
ever names a storage location. The moment one module writes
`spark.read.format("delta").load("./lakehouse/silver_devices")`, migration means
grepping for string literals.

There is also a shape difference, not just a path difference: local Delta tables
are addressed by *directory*, Unity Catalog tables by *identifier*, and they need
different Spark calls (`.load()`/`.save()` vs `.table()`/`.saveAsTable()`).

## Decision

Transformation code addresses tables by logical name only — `"silver_devices"` —
and two pieces of infrastructure resolve it:

- `Settings.table_ref(name)` turns a logical name into a reference, driven by
  `REGISTRY_STORAGE_MODE` (`path` | `catalog`) and `REGISTRY_LAKEHOUSE_ROOT`.
- `registry.tables.read_table` / `write_table` / `table_exists` pick the right
  Spark call for the mode.

```text
path     mode: "./lakehouse"  + "silver_devices" -> ./lakehouse/silver_devices
catalog  mode: "main.registry" + "silver_devices" -> main.registry.silver_devices
```

`catalog` mode validates that the root really is a `catalog.schema` namespace, so
a half-configured deployment fails at startup rather than writing to a directory
literally named `main.registry`.

## Consequences

- Migration is two environment variables plus whatever auth the workspace needs.
- The mode branch exists in exactly one module, and it is unit-tested in both
  modes without needing a Databricks workspace.
- Callers must go through `registry.tables`. A module reaching for
  `spark.read.format("delta").load(...)` directly is a review finding, not a
  style preference — it silently breaks the migration guarantee.

## Consequences for testing

Because resolution is config-driven, tests point `lakehouse_root` at a `tmp_path`
and get complete isolation for free.
