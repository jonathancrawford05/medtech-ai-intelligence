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

> **Status 2026-09-15 — enrichment built; two acceptance criteria cannot be met here.**
> Transform, taxonomy loader and company resolution are built and tested
> ([finding 0007](../findings/0007-silver-build.md)); decisions in
> [ADR 0012](adr/0012-silver-schema-and-supplement-handling.md). Run at full scale
> over the real 1,614-row pull ([finding 0009](../findings/0009-first-full-scale-silver-run.md)).
> openFDA enrichment is built and wired ([ADR 0013](adr/0013-openfda-enrichment-architecture.md)),
> populating `device_class` plus a research surface in `silver_device_enrichment`.
>
> **This issue's "predicate lineage" criterion is not achievable from openFDA.** No
> endpoint carries predicates, PCCP or the cybersecurity statement — they are in the
> 510(k) summary PDF. Split out as Issue 4 rather than left as a permanently open
> checkbox here. **Also outstanding:** the two-stage mortality flag.

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
- Populate device class from the openFDA record. ~~and predicate lineage~~ — see
  the status note: predicate lineage is not in the API (Issue 4).
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

## Issue 4 — document-derived fields (`ingest/summaries/…`)

> **Status 2026-09-15 — deferred pending business buy-in.** Scoped, not started.

**Why.** `predicate_submission_number`, `predicate_age_days`, `has_pccp`,
`pccp_summary` and `cybersecurity_statement_present` are `None` in silver and no
openFDA endpoint can fill them. They exist only in the 510(k) summary PDF and the
decision summary at `accessdata.fda.gov`. Predicate lineage in particular is what
turns the registry from a list into a *genealogy* — which devices descend from
which, and how old the evidence at the root actually is.

**What already exists to scope it.** `silver_device_enrichment.statement_or_summary`
records, per device, whether a public **Summary** was filed (a fetchable document)
or only a **Statement** (no public document). The size and the ceiling of this
work are therefore a query, not an estimate.

**Scope, in the order value arrives.**

- Count what is actually fetchable from the enrichment table before writing any
  fetcher.
- Acquire and cache summary PDFs, same content-hash + `ingested_at` discipline as
  bronze. Assume some are scanned images and will need OCR; assume some 404.
- **Predicate extraction first.** 510(k) summaries state the predicate in a
  formulaic sentence; this is pattern extraction, not document understanding, and
  it unlocks the lineage graph.
- PCCP and cybersecurity presence next — likewise closer to a section-heading
  search than to NLP.
- Only then consider anything model-assisted, and if so, under ADR 0007's
  two-stage rule: a logged, reviewable pass, never a silent judgement.

**Decisions → ADR.** Document acquisition and caching; what "extraction confidence"
means and how a low-confidence extraction is represented (almost certainly `None`
plus a recorded attempt, consistent with ADR 0012).

**Acceptance.** Fetchable-document count reported; predicate extracted for a
majority of devices that filed a Summary, with every extraction traceable to the
source document and page.

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
