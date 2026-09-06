# 0008 — Generate Spark schemas from the Pydantic models

**Status:** Accepted · **Date:** 2026-09-06

## Context

Each Pydantic model needs a matching Spark `StructType` so ingestion writes land
in typed Delta tables. The development plan allowed either hand-writing the
mirror alongside the model or generating it.

Two hand-maintained copies of the same schema drift. Someone adds a field to the
Pydantic model, forgets the StructType, and the failure surfaces later as a
confusing Spark error or — worse — a silently dropped column.

## Decision

Generate the mirror: `spark_schema_for(Model)` walks `model_fields` and maps
annotations to Spark types.

- `str`→`StringType`, `bool`→`BooleanType`, `int`→`LongType`,
  `float`→`DoubleType`, `date`→`DateType`, `datetime`→`TimestampType`
- `Literal["a","b"]` → `StringType` (a constrained string to Spark)
- `list[str]` → `ArrayType(StringType)`
- `X | None` → nullable; a bare `X` → `nullable=False`
- anything else raises `TypeError` at generation time

A parametrised test asserts field-for-field, order-preserving parity for every
model, so adding a field to a model without the generator supporting its type
fails the suite immediately.

## Consequences

- Model and Spark schema cannot drift.
- Nullability is derived from the type annotation rather than guessed, which
  matters for Delta schema enforcement.
- Field *order* is the model's declaration order, so Delta table columns are
  stable and readable.
- The generator supports the type vocabulary these models actually use. Adding
  a nested `BaseModel` or a `dict` field means extending `_spark_type` first —
  by design, it refuses rather than guessing.

## Alternatives considered

**Hand-written StructTypes.** More explicit at the point of definition, but the
drift risk is exactly the failure mode we care about, and a parity test would be
needed anyway — at which point the generator is less code.

**Infer the schema from the DataFrame.** Lets Spark guess types from data;
produces `StringType` for everything on an empty batch and silently varies with
input. Unacceptable for typed Delta tables.
