# medtech-ai-intelligence

A registry of FDA-authorised AI/ML-enabled medical devices, built as a
bronze/silver/gold lakehouse on **Spark + Delta Lake** — locally now, on
Databricks later, without rewriting the pipeline.

> **Prototype status.** Phase 0 is complete and the FDA list ingester is built
> and fixture-tested, but it has **not yet run against the live FDA site**.
> See [`CONTINUATION.md`](CONTINUATION.md) for exactly where things stand.

---

## Why Spark locally

Databricks *is* Spark + Delta Lake + Unity Catalog. Writing the prototype against
anything else means writing the transformation logic twice. Local-mode PySpark
(`local[*]`) runs a full Spark instance in a single process — no cluster — and
`delta-spark` gives real ACID Delta tables on the local filesystem with the same
MERGE semantics, schema enforcement and time travel Databricks uses.

The full reasoning, including why not pandas, DuckDB or SQLite, is in
[ADR 0001](docs/adr/0001-local-spark-delta-substrate.md).

## Architecture

```
                  FDA AI-enabled device list        openFDA API
                  (CSV export, HTML fallback)       (510k/PMA/De Novo)   [Phase 2]
                              │                             │
                              ▼                             ▼
   BRONZE   bronze_fda_ai_list ─────────────────── bronze_openfda_records
            append-only · ingested_at · content-hash snapshot id
                              │
                              ▼
   SILVER   silver_devices ── silver_companies ── silver_evidence
            typed to DeviceRecord / CompanyRecord / EvidenceRecord
                              │
                              ▼
   GOLD     mortality_relevant      cardiovascular + metabolic risk devices,
                                    gated on a *confirmed* mortality flag
```

Every layer is a Delta table. Nothing in the pipeline names a filesystem path:
modules ask for `"silver_devices"` and `registry.tables` resolves it against the
configured storage mode ([ADR 0004](docs/adr/0004-config-driven-table-resolution.md)).

## Quick start

### With Docker (recommended — no local Java needed)

```bash
docker compose run --rm registry smoke     # Phase 0 acceptance: Spark + Delta round-trip
docker compose run --rm registry config    # show resolved settings
docker compose run --rm test               # full test suite
docker compose up notebook                 # Jupyter Lab on http://localhost:8888
```

### Natively (needs JDK 17 or 21)

```bash
make install    # uv sync + stage the Delta JARs
make test       # 61 tests
make smoke      # Spark + Delta round-trip
```

`make install` rather than a bare `uv sync` matters: it runs
`scripts/warm_delta_jars.py`, which puts the Delta JARs on Spark's classpath so
sessions start without reaching out to Maven.

```
make help       # all targets
make test-fast  # skip the JVM tests
make lint       # ruff check + format check
```

## Configuration

Everything is environment-driven with a `REGISTRY_` prefix; copy `.env.example`
to `.env` to override. Defaults are in
[`src/registry/config/settings.py`](src/registry/config/settings.py).

| Variable | Default | Purpose |
|----------|---------|---------|
| `REGISTRY_LAKEHOUSE_ROOT` | `./lakehouse` | Delta root directory, or a `catalog.schema` namespace |
| `REGISTRY_STORAGE_MODE` | `path` | `path` (local Delta) or `catalog` (Unity Catalog) |
| `REGISTRY_SPARK_MASTER` | `local[*]` | Ignored on Databricks |
| `REGISTRY_OPENFDA_API_KEY` | *(none)* | Optional; raises the openFDA rate limit |
| `REGISTRY_SOURCE_CROSS_CHECK_ENABLED` | `false` | Secondary-source reconciliation (deferred) |

Curated, hand-maintained lookups live in [`config/`](config/) as YAML —
currently the FDA-panel → specialty-category taxonomy.

## Migrating to Databricks

This is the point of the stack choice, so it is worth being precise about the
diff. Two environment variables:

```bash
REGISTRY_STORAGE_MODE=catalog
REGISTRY_LAKEHOUSE_ROOT=main.registry     # your catalog.schema
```

and that is the whole configuration change. Mechanically:

| File | Change |
|------|--------|
| `spark_session.py` | **None.** It detects `DATABRICKS_RUNTIME_VERSION` and returns the runtime-provided session, skipping the local Delta config entirely. |
| `registry/config/settings.py` | **None** — the two variables above are set in the job environment. |
| `tables.py` | **None.** `catalog` mode already emits `spark.read.table()` / `saveAsTable()`. |
| Everything in `ingest/`, `transform/`, `mart/` | **None.** No module names a path. |

Remaining work is workspace-side, not code-side: authentication for scheduled
jobs, and creating the target catalog/schema. `catalog` mode is unit-tested but
has not been exercised against a real workspace — that is Phase 5.

**Fallback worth knowing about.** If Spark's local footprint becomes annoying on
a laptop, DuckDB with `delta-rs` reads the same Delta tables. It is a genuinely
good lightweight option, but its SQL and write path are not Databricks' engine,
so business logic written there needs a translation pass later — which is why it
is documented as a fallback and not the default
([ADR 0001](docs/adr/0001-local-spark-delta-substrate.md)).

## Data model

Defined as Pydantic models in
[`src/registry/schemas.py`](src/registry/schemas.py), each with a Spark
`StructType` **generated** from it so the two cannot drift
([ADR 0008](docs/adr/0008-generated-spark-schemas.md)).

- **`DeviceRecord`** — one authorised device: submission number, pathway,
  panel, product code, class, predicate lineage, PCCP and cybersecurity flags,
  and a `source_url` back to the FDA record.
- **`EvidenceRecord`** — evidence quality: sensitivity/specificity, demographic
  disclosure, intended-use text, and the mortality/MACE flag.
- **`CompanyRecord`** — resolved applicant, name variants, parent, device count.

### The one judgement call

`mortality_or_mace_risk_indicated` is the only field in the registry that is an
interpretation rather than a published fact, so it is stored as **two** columns:
a deterministic `mortality_keyword_flag` from a regex pass, and a
`mortality_confirmed_flag` that stays `None` until a human or a *labelled*
LLM-assisted pass reviews it. A confirmed flag without a recorded
`mortality_review_method` is rejected outright. Only the confirmed flag gates the
gold mart ([ADR 0007](docs/adr/0007-two-stage-mortality-flag.md)).

## Development

Tests are written first; `tests/fixtures/` holds recorded payloads and **CI never
touches the network**.

```bash
uv run pytest -q                     # everything
uv run pytest -q -m "not spark"      # skip JVM tests (fast)
uv run pytest -q -m live_network     # hits the real FDA site; never in CI
```

CI (`.github/workflows/ci.yml`) runs lint plus the suite on JDK 17, and
separately builds the Docker image and runs the Phase 0 acceptance check inside
it, so the image cannot rot unnoticed.

Optional: `uv run pre-commit install`.

## Repository map

| Path | What |
|------|------|
| `src/registry/config/` | Environment-driven settings and table resolution |
| `src/registry/spark_session.py` | `get_spark()` — the only local-vs-Databricks difference |
| `src/registry/tables.py` | Read/write Delta tables by logical name |
| `src/registry/schemas.py` | Pydantic models + generated Spark schemas |
| `src/registry/ingest/` | Source acquisition → bronze |
| `src/registry/transform/` | Bronze → silver (Phase 2) |
| `src/registry/mart/` | Gold-layer marts (Phase 3) |
| `src/registry/monitor/` | New-device diffing (Phase 4) |
| `config/` | Hand-curated YAML lookups |
| `docs/adr/` | Architecture decision records |
| `CONTINUATION.md` | Current state and next steps |

## Non-goals for the prototype

No production alerting (log/CSV is enough), no automated M&A scraping, no
fully-automated mortality judgement without a reviewable log, and no UI — the
notebook and the gold Delta table are the deliverable.
