# CONTINUATION

Handoff state for the next session (human or agent). **Read this first, then
`docs/adr/README.md`.** Update this file at the end of every working session —
it is the only thing that survives a context window.

**Last updated:** 2026-09-13 · **Branch:** `claude/silver-bronze-to-silver` (PR open)
**Suite:** 210 passing, ruff + markdownlint clean; `live_network`
tests are deselected outside a network-permitted host — see §5.
**PRs #1, #2, #5, #6 merged to `main`.** Note #3 and #4 were stacked onto
branches rather than `main` and did not land until #6 brought them across —
target `main` unless a stack is deliberate.

---

## 1. Where the build actually is

| Phase | Status | Notes |
|-------|--------|-------|
| **0 — Scaffolding** | ✅ Done | uv + Docker, `get_spark()`, Delta round-trip, CI, ADRs |
| **1 — Ingestion** | ✅ Done | **Phase 1 acceptance met 2026-09-13** — a full-sized live pull landed in bronze both locally and on CI ([run 34764719600](https://github.com/jonathancrawford05/medtech-ai-intelligence/actions/runs/34764719600)): 1,614 rows fetched, parsed and written, 100% against the ≥95% criterion ([finding 0006](findings/0006-phase-1-acceptance-met.md), closing [0001](findings/0001-phase-1-live-ingestion-gap.md)). openFDA client built and verified against real fixtures (ADR 0010, [finding 0004](findings/0004-openfda-client.md)). Bronze is **not durably persisted** — deliberately deferred, see [ADR 0011](docs/adr/0011-defer-durable-bronze-persistence.md). |
| **2 — Silver transforms** | 🟡 Partial | `bronze_to_silver` builds `silver_devices` from the newest bronze pull — latest-`ingested_at` join, pathway from the submission prefix, date parsing, panel→specialty taxonomy, curated company resolution ([finding 0007](findings/0007-silver-build.md)). openFDA-dependent fields (`device_class`, predicate lineage, PCCP, cybersecurity) are **`None` until an enrichment pass exists** — coverage is currently 0% ([ADR 0012](docs/adr/0012-silver-schema-and-supplement-handling.md)). Never yet run over the 1,614-row live pull. **`registry inspect`** reads the lakehouse back and returns a verdict ([finding 0008](findings/0008-taxonomy-spelling-mismatch.md)). |
| **3 — Evidence & gold mart** | 🔲 Not started | Schema support for the two-stage flag is in place |
| **4 — Monitoring** | 🔲 Not started | |
| **5 — Databricks dry run** | 🟡 Mechanism built | `storage_mode=catalog` implemented and tested; not run against a real workspace |

Scope was deliberately kept narrow (one source, bronze only) per the brief:
get a working vertical slice before adding sources.

---

## 2. FDA acquisition — verified live, and the pipeline run end to end ✅

**Resolved.** The FDA path has now been checked against the real site (via a
browser on an unrestricted network; `fda.gov` is still blocked from the build
and dev-VM egress). Findings, and the code change they drove, are in
[ADR 0009](docs/adr/0009-fda-ai-list-acquisition-verified.md):

- The CSV/Excel/XML exports **do** exist. The CSV is at
  `https://www.fda.gov/media/178541/download?attachment` (a "Download a CSV File"
  link) — **not** the `?export=csv` / `/export?format=csv` URLs ADR 0005 guessed,
  which both 404. The old code silently fell back to HTML scraping on every run.
- Headers match `_HEADER_ALIASES` exactly; no alias changes were needed.
- The export is the full list — 1,615 data rows (~1,614 table entries), not a
  truncated page.

Code now tries the known CSV URL first, then **discovers** the CSV link off the
page if that url is gone, then falls back to HTML (see `fetch_raw` /
`_discover_csv_url`). `settings.fda_ai_list_csv_url` is env-overridable. Fixtures
were replaced with a real slice; `fda_ai_list_messy.csv` is kept for the defensive
cases. A `@pytest.mark.live_network` test hits the real endpoint (never in CI; run
it from a network that can reach `fda.gov`).

Next session with `fda.gov` access should run, to reconfirm:

```bash
uv run registry ingest-fda-list --dry-run --verbose   # expect ~1,600 rows, csv
uv run pytest -m live_network                          # end-to-end against the live site
```

That live run is now **automated**: `.github/workflows/scheduled-ingest.yml`
(weekly + manual) runs the real ingest on a network-permitted runner, then
`scripts/check_bronze_rowcount.py` asserts the pull is full-sized (≥
`INGEST_MIN_ROWS`, default 1500). Its first green run is what closes finding 0001;
until then that finding stays open. See `docs/scheduled-ingest.md` (incl. adding
the `OPENFDA_API_KEY` secret).

## 3. What exists, and where

```text
src/registry/
  config/settings.py     env-driven settings + table_ref() resolution
  spark_session.py       get_spark(); the ONLY local-vs-Databricks difference
  tables.py              read/write/exists by LOGICAL table name
  schemas.py             Pydantic models + generated Spark StructType mirrors
  cli.py                 `registry config | smoke | ingest-fda-list`
  ingest/fda_ai_list.py  CSV-export-first, HTML-fallback bronze ingester
  ingest/openfda_client.py  typed api.fda.gov client (510k/pma/classification), cache + backoff
  transform/ mart/ monitor/   empty packages, Phases 2-4
config/specialty_taxonomy.yaml   curated FDA panel -> our category
scripts/warm_delta_jars.py       stages Delta JARs (--stage-to / --from-dir)
scripts/check_bronze_rowcount.py Phase-1 acceptance gate (>= INGEST_MIN_ROWS)
.github/workflows/scheduled-ingest.yml  weekly/manual live ingest -> bronze + gate
tests/fixtures/openfda/          real (trimmed) openFDA captures + PROVENANCE.md
docs/adr/                        why things are the way they are
docs/pr-review-routine.md        review prompt: P0-P3 rubric + test-integrity gate
docs/validation-playbook.md      how each component gets validated
findings/                        what was actually verified, and what was not
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

1. **Verify the FDA path live** — acquisition **done**; the bronze write is not.
   ADR 0009 verified the source by browser inspection, and a live dry run on
   2026-09-13 fetched and parsed all 1,614 rows ([finding 0005](findings/0005-first-live-ingest-attempt.md)).
   A full-size bronze **write** has still never run — that is the remaining half
   of [finding 0001](findings/0001-phase-1-live-ingestion-gap.md) and of the
   plan's Phase 1 acceptance. Easiest close: Actions → *Scheduled FDA ingest*.
2. ~~**`ingest/openfda_client.py`**~~ — **done** (ADR 0010, finding 0004). Typed
   `api.fda.gov` client over 510(k)/PMA/classification with submission-keyed
   caching (in-memory + optional `openfda_cache_dir`), 429/5xx backoff, and
   real-fixture tests. Note: no `de_novo` endpoint exists — De Novo grants come
   from `510k` (`decision_code=DENG`); PMA supplements split on `/`. Remaining:
   run its `live_network` tests on a net-permitted host.
3. **`transform/bronze_to_silver.py`** — join the AI list against openFDA by
   submission number to populate `DeviceRecord`. This is where the raw decision
   date gets parsed, `pathway` is derived from the submission-number prefix
   (`K…`→510k, `DEN…`→de_novo, `P…`→pma), and predicate lineage lands.
   Note bronze may hold several pulls: select the latest `ingested_at` per
   submission number before joining.
4. **`transform/company_resolution.py`** — start with a hand-maintained lookup in
   `config/`, ~20 applicants by volume. No M&A scraping (explicit non-goal).
5. ~~**Taxonomy loader**~~ — **done**. Reads `config/specialty_taxonomy.yaml`,
   normalises panel spellings, records uncurated panels in `unmapped_panels`.
6. **Run the pipeline over the real 1,614-row pull and inspect it.** On a host
   that can reach `fda.gov`:

   ```bash
   uv run registry ingest-fda-list      # append a real pull to bronze
   uv run registry build-silver         # rebuild silver from it
   uv run registry inspect              # non-zero exit == something is wrong
   ```

   At 1,614 rows `inspect` will almost certainly name panels the taxonomy has
   not curated — that is the point, and curating them is the follow-up.
7. **openFDA enrichment pass.** `device_class`, predicate lineage, PCCP and the
   cybersecurity flag are all `None` today; `inspect` reports 0.0% coverage for
   each. This is the largest remaining gap in Phase 2.
8. **Apply the finding-0008 lesson to `config/company_aliases.yaml`** — curated
   config checked only against curated fixtures agrees with itself and can still
   be wrong. Needs a coverage-threshold test against the real export, not a
   zero-miss one.
9. Phases 3–5 per the development plan.

---

## 5. Known gaps and traps

- **Curated config is not validated by curated fixtures.** `specialty_taxonomy.yaml`
  spelled a panel `General & Plastic Surgery` where the FDA export says
  `General and Plastic Surgery`; every taxonomy test passed because every test
  used *our* spelling. Two real rows silently became `other`. Lookups are now
  normalised and a test reads panel labels straight out of the real-export
  fixture ([finding 0008](findings/0008-taxonomy-spelling-mismatch.md)). The same
  hole is still open for `company_aliases.yaml`.
- **`make test` locally will show 3 failures without network.** They are the
  `live_network` tests; CI deselects them. Use `uv run pytest -m "not live_network"`
  on a blocked host.

- **Docker image built + Phase 0 verified (2026-09-06).** Built on Apple Silicon
  and `registry smoke` inside the image printed `Spark 4.0.1 up.` +
  `Delta round-trip OK (1 row).` This also confirms the Delta-JAR staging-order fix
  held — the in-build classpath assertion passed, so the image is not silently
  missing its JARs. If `pip install uv==0.8.17` ever breaks, the canonical
  alternative is `COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /bin/`.
- **JDK arch handling (fixed 2026-09-06).** `JAVA_HOME` used to be hard-coded to
  `…-openjdk-amd64`, which broke native arm64 builds (`docker compose` on Apple
  Silicon) with `JAVA_GATEWAY_EXITED`. The Dockerfile now derives `JAVA_HOME` from
  the installed JDK (symlinked to `/opt/java`), so plain `make docker-build` /
  `docker compose run --rm test` work on both arm64 and amd64. Add
  `--platform linux/amd64` only if you deliberately want amd64 parity with CI/DBR.
- **Spark 4.0 needs JDK 17+.** The dev VM behind the desktop app has only JDK 11,
  so the 10 Spark tests and `registry smoke` are not run there — the image (JDK 17)
  or CI is the place for them.
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
