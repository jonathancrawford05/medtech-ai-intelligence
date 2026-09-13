# Architecture Decision Records

Each ADR captures one decision, the forces behind it, and what it costs. The
point is that a future session — human or agent — can read *why* something is
the way it is instead of re-litigating it or, worse, silently undoing it.

| # | Decision | Status |
|---|----------|--------|
| [0001](0001-local-spark-delta-substrate.md) | Local Spark + Delta Lake as the Databricks substrate | Accepted |
| [0002](0002-spark-delta-version-pinning.md) | Pin Spark 4.0 / Delta 4.0 / JDK 17 to match DBR 17.x LTS | Accepted |
| [0003](0003-docker-uv-over-conda.md) | Docker + uv for the local environment, not conda | Accepted |
| [0004](0004-config-driven-table-resolution.md) | Logical table names resolved through config | Accepted |
| [0005](0005-fda-ai-list-acquisition.md) | Prefer the FDA CSV export; scrape HTML only as fallback | Superseded by [0009](0009-fda-ai-list-acquisition-verified.md) |
| [0006](0006-config-package-layout.md) | Settings live at `registry.config`, not a top-level `config` package | Accepted |
| [0007](0007-two-stage-mortality-flag.md) | Mortality/MACE relevance is two auditable columns, not one | Accepted |
| [0008](0008-generated-spark-schemas.md) | Generate Spark schemas from the Pydantic models | Accepted |
| [0009](0009-fda-ai-list-acquisition-verified.md) | FDA list acquisition, live-verified: known CSV URL + link discovery | Accepted |
| [0010](0010-openfda-client.md) | openFDA client: endpoint routing, missing = None, on-disk cache | Accepted |

## Writing a new one

Copy the shape of an existing record: Context, Decision, Consequences, and
Alternatives considered. Number sequentially. Never edit an accepted ADR to
reflect a new decision — add a new ADR and mark the old one `Superseded by
NNNN`. The history is the value.
