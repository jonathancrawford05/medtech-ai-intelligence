# CONTINUATION

Handoff state for the next session (human or agent). **Read this first, then
`docs/adr/README.md`.** Update this file at the end of every working session —
it is the only thing that survives a context window.

**Last updated:** 2026-09-06 · **Branch:** `claude/project-setup-docker-uv-579h0b`
**Suite:** 61 tests passing, ruff clean.

---

## 1. Where the build actually is

| Phase | Status | Notes |
|-------|--------|-------|
| **0 — Scaffolding** | ✅ Done | uv + Docker, `get_spark()`, Delta round-trip, CI, ADRs |
| **1 — Ingestion** | 🟡 Partial | FDA AI list ingester built and fixture-tested. **Never run against the live site.** openFDA client not started. |
| **2 — Silver transforms** | 🔲 Not started | `schemas.py` is finished, which is the bulk of the design work |
| **3 — Evidence & gold mart** | 🔲 Not started | Schema support for the two-stage flag is in place |
| **4 — Monitoring** | 🔲 Not started | |
| **5 — Databricks dry run** | 🟡 Mechanism built | `storage_mode=catalog` implemented and tested; not run against a real workspace |

Scope was deliberately kept narrow (one source, bronze only) per the brief:
get a working vertical slice before adding sources.

---

## 2. Start here — the blocking item

**The FDA acquisition path has never touched the real site.**

The environment this was built in blocks `fda.gov` and `api.fda.gov` at the
network policy, so every URL and column header in the ingester is *inferred* from
research, not observed. See [ADR 0005](docs/adr/0005-fda-ai-list-acquisition.md).

First task for the next session with network access:

```bash
uv run registry ingest-fda-list --dry-run --verbose   # fetch + parse, write nothing
```

Then confirm, in order:

1. **Does an export candidate work?** `_candidate_urls()` in
   `src/registry/ingest/fda_ai_list.py` tries `?export=csv` and
   `/export?format=csv` before falling back to HTML. If both 404, find the real
   export URL (it may be a `/media/NNNNN/download` link on the page) and add it.
2. **Do the headers match `_HEADER_ALIASES`?** A `SourceFormatError` naming the
   headers it saw means they do not — add the real ones to the alias table.
3. **Does the row count look right?** Public reporting put the list around
   1,000+ authorisations. Tens of rows means pagination is truncating the export.
4. **Replace the fixtures.** `tests/fixtures/fda_ai_list_sample.csv` is
   hand-written. Once a real payload is in hand, save a trimmed slice of it as
   the fixture so the tests assert against reality. Keep the messy fixture — it
   encodes the defensive cases.
5. Then drop the "unverified" caveat from ADR 0005 and this section.

If the environment still blocks `fda.gov`, that is an environment network-policy
setting, not a code problem — ask the repo owner to allow `www.fda.gov` and
`api.fda.gov`.

---

## 3. What exists, and where

```
src/registry/
  config/settings.py     env-driven settings + table_ref() resolution
  spark_session.py       get_spark(); the ONLY local-vs-Databricks difference
  tables.py              read/write/exists by LOGICAL table name
  schemas.py             Pydantic models + generated Spark StructType mirrors
  cli.py                 `registry config | smoke | ingest-fda-list`
  ingest/fda_ai_list.py  CSV-export-first, HTML-fallback bronze ingester
  transform/ mart/ monitor/   empty packages, Phases 2-4
config/specialty_taxonomy.yaml   curated FDA panel -> our category
scripts/warm_delta_jars.py       stages Delta JARs at image build time
docs/adr/                        why things are the way they are
```

### Conventions that matter

- **Never name a filesystem path in pipeline code.** Go through
  `settings.table_ref()` / `registry.tables`. This is what makes the Databricks
  migration a config change ([ADR 0004](docs/adr/0004-config-driven-table-resolution.md)).
- **Bronze is append-only.** Every pull is stamped with `ingested_at` and a
  content-hash `source_snapshot_id`. `write_table` warns if you overwrite a
  `bronze_` table.
- **TDD.** Tests were written before each module here and should continue to be.
  Fixtures live in `tests/fixtures/`; CI never hits the network.
- **Markers.** `-m "not spark"` skips JVM tests; `live_network` tests are never
  run in CI.

---

## 4. Next tasks, in the order they make sense

1. **Verify the FDA path live** (section 2). Everything downstream inherits its
   assumptions from this.
2. **`ingest/openfda_client.py`** — thin `api.fda.gov` wrapper for 510(k), PMA,
   De Novo and classification endpoints. Needs: response caching keyed by
   submission number (avoid re-fetching unchanged records), rate-limit backoff,
   and fixture-based tests. Record real responses as fixtures the first time.
3. **`transform/bronze_to_silver.py`** — join the AI list against openFDA by
   submission number to populate `DeviceRecord`. This is where the raw decision
   date gets parsed, `pathway` is derived from the submission-number prefix
   (`K…`→510k, `DEN…`→de_novo, `P…`→pma), and predicate lineage lands.
   Note bronze may hold several pulls: select the latest `ingested_at` per
   submission number before joining.
4. **`transform/company_resolution.py`** — start with a hand-maintained lookup in
   `config/`, ~20 applicants by volume. No M&A scraping (explicit non-goal).
5. **Taxonomy loader** — read `config/specialty_taxonomy.yaml` via
   `settings.specialty_taxonomy_path` to populate `specialty_category`. The file
   exists; nothing reads it yet.
6. Phases 3–5 per the development plan.

---

## 5. Known gaps and traps

- **Docker image is written but never built.** No Docker daemon was available in
  the build environment. The `docker` CI job builds it and runs the Phase 0 check
  inside it — watch that job on the first push. If `pip install uv==0.8.17` is a
  problem, the canonical alternative is
  `COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /bin/`.
- **Local-mode driver binding.** Spark binds to `127.0.0.1` for both
  `spark.driver.host` and `spark.driver.bindAddress` when the master is `local*`.
  This is not cosmetic: in this container the hostname resolved to `192.0.2.2`
  (a TEST-NET address), the driver advertised an address it was not listening on,
  and every Spark test failed with "Connection refused". Do not remove that
  pairing — and if you set one, set both.
- **Delta JARs are not in the `delta-spark` wheel.** They are Ivy-resolved from
  Maven on first session start, which is slow and fails on a restricted network.
  `scripts/warm_delta_jars.py` stages them into `pyspark/jars`;
  `_delta_jars_on_classpath()` then skips Maven entirely. Run
  `make install` (not bare `uv sync`) to get this.
- **`get_settings()` is `lru_cache`d.** Tests that manipulate `REGISTRY_*` env
  vars should construct `Settings()` directly, or call
  `get_settings.cache_clear()`. A `conftest.py` autouse fixture strips stray
  `REGISTRY_*` vars so a developer's shell cannot change test outcomes.
- **Spark tests are slow** (~30s of the ~40s suite) because of JVM startup. The
  SparkSession fixture is session-scoped; keep it that way.

---

## 6. Working agreement for agent sessions

- Work on the branch named at the top of this file unless told otherwise.
- Write the test first. Every module here was built that way.
- Run `make lint && make test` before committing; both must be clean.
- Record any decision a future session might otherwise re-litigate as a new ADR
  in `docs/adr/` — do not edit an accepted one, supersede it.
- Update sections 1, 2 and 4 of this file before ending a session. Leaving it
  stale is worse than leaving it empty.
- Keep scope narrow: one source working end to end beats four half-wired.
