# Development Roadmap — agent-ready task briefs

Self-contained briefs for the Claude Code agent, to hand off **one at a time, in
order**. Before starting any of them the agent must read `CLAUDE.md` and
`docs/validation-playbook.md`, and follow `docs/pr-review-routine.md` on the
resulting PR. Each brief inherits the **Definition of Done** at the bottom.

Sequence: **1. openFDA client → 2. bronze→silver → 3. change monitoring.** Each
unlocks the next: silver needs the openFDA join; monitoring needs silver to reason
about specialty/intended-use.

---

## Issue 1 — openFDA client (`ingest/openfda_client.py`)

**Why.** The FDA AI list gives name / date / panel / product-code but not device
class, pathway detail, or predicate lineage. openFDA (`api.fda.gov`) has those in the
510(k), PMA, De Novo and device-classification endpoints. openFDA records are **not**
flagged as AI — our bronze AI list is what identifies which ones are; the join is
Issue 2. This issue is just the client.

**Scope.**

- A thin, typed client over `api.fda.gov`: fetch by submission number
  (`510k`, `pma`, `de_novo`) and by product code (`classification`); handle pagination.
- Rate-limit backoff (openFDA is ~240 req/min unauthenticated, higher with a key;
  `settings.openfda_api_key` already exists — read it, never hard-code).
- Response caching keyed by submission number so unchanged records are not refetched.
- Record **real** responses as fixtures on the first run (a `live_network` test), then
  trim to a slice for the unit tests. CI never hits the network.

**Decisions → ADR.** Cache location/strategy (on-disk vs a bronze table); how to
represent a submission number that openFDA does not return.

**Files.** `src/registry/ingest/openfda_client.py`; `tests/test_openfda_client.py`;
`tests/fixtures/openfda_*.json`.

**Non-goals.** MAUDE adverse events (later); the silver join (Issue 2).

**Acceptance.** Given a submission number, returns a typed record (or `None`) per
endpoint; a cache hit avoids a second HTTP call (assert via `respx` call count);
backoff retries on 429; fixtures are real slices; `-m "not live_network"` green.

---

## Issue 2 — bronze→silver transform (`transform/bronze_to_silver.py`)

**Why.** Turn raw pulls into the queryable `DeviceRecord` silver table by joining the
latest AI-list pull against openFDA (Issue 1). This is where the registry becomes
useful.

**Scope.**

- **Select the latest `ingested_at` per `submission_number`** before joining — bronze
  is append-only and holds every pull.
- Derive `pathway` from the submission prefix (`K…`→510k, `DEN…`→de_novo, `P…`→pma).
  **Decide handling of PMA supplement suffixes** like `P130020/S005` (keep raw, or
  strip for the openFDA/deep-link lookup) — ADR.
- Parse the raw decision date here (bronze deliberately kept it unparsed).
- Populate device class and predicate lineage from the openFDA record.
- **Company resolution** (`transform/company_resolution.py`): a hand-maintained lookup
  in `config/` (~20 top applicants by volume) → resolved parent company. **No M&A
  scraping** (explicit non-goal).
- **Taxonomy**: load `config/specialty_taxonomy.yaml` via
  `settings.specialty_taxonomy_path` to set `specialty_category`; handle unmapped
  panels explicitly (don't silently drop).
- Set the two-stage mortality/MACE relevance flag (schema support exists, ADR 0007).
  The intended-use `mortality_or_mace_risk_indicated` flag is **hand-curated intent,
  not inferred** — build the mechanism + a seed list (cardiovascular/metabolic risk
  tools like the report's CaRi-Heart / AVIEW CAC examples), flag the rest for review.

**Decisions → ADR.** Supplement-suffix handling; company-resolution source of truth;
how mortality relevance is seeded vs curated.

**Files.** `transform/bronze_to_silver.py`, `transform/company_resolution.py`,
a `config/` company-alias lookup, tests + fixtures.

**Acceptance.** Silver has one row per latest submission; pathway / class / panel /
company / specialty populated; schema parity via the generated `StructType`; the join
provably picks the latest pull; deterministic on fixtures; Spark tests pass in the
image/CI.

---

## Issue 3 — FDA-list change monitoring (`monitor/…`, `mart/…`)

**Why.** The registry's business value is spotting *movement* — new mortality-relevant
devices as leads (report §6, §8). Bronze is append-only with `source_snapshot_id`, so
consecutive snapshots can be diffed. **This is distinct from the CI `live-network`
job:** that guards the source's *shape* (URL/headers/row-count); this monitors *content*
movement for leads.

**Scope.**

- Diff the two most recent silver builds: **new** submission numbers, and specifically
  new **cardiovascular/metabolic-panel** devices, devices whose intended-use carries
  mortality/risk-prediction language, PCCP-flagged devices, and foundation-model
  clearances (the categories the report calls out as leading indicators).
- Emit a structured **leads** output (a gold/`mart` table or a report artifact) that
  underwriting/partnership stakeholders query — not raw counts. Keep clearance date as
  a **time series** (rate-of-change matters, report §2), not a static snapshot.
- Decide and wire the alerting surface (table / log / notification).

**Decisions → ADR.** The "what counts as a lead" filter set; the output surface.

**Files.** `src/registry/monitor/…`, `src/registry/mart/…`, tests + fixtures (two
synthetic snapshots that differ by a known set of rows).

**Acceptance.** Given two snapshots, returns the correct new/changed leads per category;
empty on identical snapshots; the time-series/gold query works; deterministic on
fixtures.

---

## Definition of Done (every issue)

- **TDD**: failing test first. Fixtures are **real** where a live source exists (record
  once via a `live_network` test, then trim); mark `spark` / `live_network` correctly.
- **Invariants held** (`CLAUDE.md`): table access via `settings.table_ref()`; bronze
  append-only; schemas generated from the Pydantic models; settings cache-safe in tests.
- `make lint && make test` green (Spark in the Docker image / CI); coverage stays above
  the floor.
- Any real decision → a **new ADR** (supersede, don't edit). Update `CONTINUATION.md`
  §1/§4 and add a `findings/` note.
- Run `docs/pr-review-routine.md` on the PR before merge.
