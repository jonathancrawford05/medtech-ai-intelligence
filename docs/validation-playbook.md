# Validation Playbook

How every component and every PR in this repo gets validated, the same way each
time. It operationalises the six-step review framework so a future session — human
or agent — can check a change against reality instead of trusting that it works.

> Companion docs: `CONTINUATION.md` (current state), `docs/adr/` (why decisions were
> made), `findings/` in the project (validation write-ups). This file is the *how*.

---

## 1. The six steps, made concrete for this repo

| Step | What it means here | Where it lands |
|------|--------------------|----------------|
| **1. ANALYZE** | State what the change must satisfy *before* writing it. Which logical tables (`settings.table_ref`) and Pydantic schemas does it touch? What is the acceptance check? | A sentence in the PR / a test name |
| **2. PROMPT / build** | Write the failing test first, then the code. Every module here was built test-first — keep it that way. | `tests/`, then `src/` |
| **3. REVIEW** | Read the diff against the repo conventions in §3 below, not just for correctness. | PR review |
| **4. REFINE** | Any decision a future session might re-litigate becomes an ADR. Never edit an accepted ADR — add one and mark the old `Superseded by NNNN`. | `docs/adr/NNNN-*.md` |
| **5. VALIDATE** | Run the checks in §4, in the right environment (§2). Confirm fixtures reflect the *real* source when one exists. | `make lint && make test`, Docker/CI, live-network |
| **6. DOCUMENT** | Update `CONTINUATION.md` (§1/§2/§4), add a `findings/` note, and reference the ADR. Leaving the handoff stale is worse than leaving it empty. | `CONTINUATION.md`, project `findings/` |

---

## 2. Environment matrix — what can run where

Different checks need different environments. Do not assume one machine does all of
it; this is where "passed locally" quietly means "the important part never ran".

| Environment | JDK | Network | Docker | Runs |
|-------------|-----|---------|--------|------|
| **Dev VM** (behind the desktop app) | 11 | `fda.gov`/`api.fda.gov` **blocked** (egress 403); PyPI OK | none | `ruff`, non-Spark tests (`-m "not spark"`) |
| **Cloud workspace** (agent) | — | same block | daemon absent | scratch, PyPI-only work |
| **Docker image** (`--target dev`) | **17** | build-time PyPI/Maven | n/a (is the image) | Spark tests, `registry smoke` — the Phase 0 acceptance |
| **CI** (GitHub Actions) | 17 | full | buildx | full suite `-m "not live_network"`, image build + in-image smoke |
| **A machine that can reach fda.gov** | any | full | — | `-m live_network`, `registry ingest-fda-list --dry-run` |

Consequences to remember:

- **Spark 4.0 requires JDK 17+.** The dev VM has JDK 11, so its Spark tests and
  `registry smoke` do not run there — the image or CI is their home.
- **`fda.gov` is blocked from every agent environment.** Live-source checks are done
  through a browser on an unrestricted network, or on the owner's machine.
- **Docker `JAVA_HOME` is arch-derived** (symlinked to `/opt/java`), so the image
  builds natively on arm64 and amd64. `--platform linux/amd64` is optional, for
  CI/Databricks arch parity — not required to build locally.
- **The dev-VM mount forbids delete/rename.** Put the uv venv *outside* the mount
  (`UV_PROJECT_ENVIRONMENT=$HOME/venvs/…`), redirect `TMPDIR` off the mount for
  pytest, and clear a stale `.git/index.lock` from the native shell if one is left.

---

## 3. Review checklist — the conventions a change must not break

- [ ] **No filesystem paths in pipeline code.** Resolve tables via
      `settings.table_ref()` / `registry.tables`, never a literal path — this is what
      keeps the Databricks migration a config change (ADR 0004).
- [ ] **Bronze is append-only.** Every pull stamped with `ingested_at` and a
      content-hash `source_snapshot_id`; no overwrite of a `bronze_` table.
- [ ] **Schemas stay generated.** Spark `StructType`s are derived from the Pydantic
      models; `tests/test_schemas.py` enforces parity (ADR 0008). Don't hand-write a
      mirror.
- [ ] **Settings are cached.** `get_settings()` is `lru_cache`d; tests that vary
      `REGISTRY_*` construct `Settings()` directly or call `get_settings.cache_clear()`.
- [ ] **Markers are honoured.** `spark` = needs a JVM; `live_network` = hits real
      endpoints, never in CI. New JVM/network tests get the right marker.
- [ ] **Fixtures reflect reality.** When a live source exists, the fixture is a real
      slice of it; keep hand-written "messy" fixtures for defensive edge cases.

---

## 4. Per-PR validation gate (definition of done)

1. [ ] `make lint` clean (ruff check + format).
2. [ ] `make test` green locally for non-Spark (`-m "not spark"`); Spark tests green in
       the Docker image or CI.
3. [ ] New behaviour has a test written *before* it (TDD), and it fails without the change.
4. [ ] Any acquisition/parsing change validated against a **real** payload, not only
       hand-written fixtures.
5. [ ] Decisions recorded as an ADR; superseded ADRs marked, not edited.
6. [ ] `CONTINUATION.md` §1/§2/§4 updated; a `findings/` note added for anything a
       reviewer would look for later.
7. [ ] If the image or its deps changed: `docker build --platform linux/amd64
       --target dev` succeeds (incl. the Delta-JAR classpath assertion) and in-image
       `registry smoke` prints `Spark … up.` + `Delta round-trip OK`.

---

## 5. Component validation matrix

What "validated" specifically means for each planned component. ✅ = done.

### Ingestion — FDA AI list  ✅ (2026-09-06, ADR 0009)
- [x] CSV export URL confirmed live; known-URL → page-discovery → HTML fallback.
- [x] Headers map through `_HEADER_ALIASES`; full list (~1,600 rows), not truncated.
- [x] Real-slice fixture; parser builds correct per-pathway `source_url`.
- [ ] Re-run `-m live_network` + `ingest-fda-list --dry-run --verbose` on a network with
      `fda.gov` access to reconfirm end to end.

### openFDA client (`ingest/openfda_client.py`) — next
- [ ] Thin `api.fda.gov` wrapper for 510(k)/PMA/De Novo/classification endpoints.
- [ ] Response caching keyed by submission number (no re-fetch of unchanged records).
- [ ] Rate-limit backoff; record real responses as fixtures on first run.
- [ ] Fixture-based unit tests + one `live_network` test recording a real payload.

### Transform: bronze → silver (`transform/bronze_to_silver.py`)
- [ ] Select the latest `ingested_at` per submission number before joining (bronze may
      hold several pulls).
- [ ] `pathway` derived from the submission prefix (`K…`→510k, `DEN…`→de_novo, `P…`→pma);
      **decide handling of supplement suffixes** like `P130020/S005` (strip for the deep
      link, or keep — see findings note).
- [ ] Raw decision date parsed here (bronze keeps it raw); predicate lineage populated.
- [ ] Schema parity via generated `StructType`; append/overwrite semantics correct.

### Company resolution (`transform/company_resolution.py`)
- [ ] Hand-maintained lookup in `config/` (~20 top applicants by volume); no M&A scraping.
- [ ] Coverage check: what fraction of rows resolve; unresolved fall through cleanly.

### Taxonomy loader
- [ ] Reads `config/specialty_taxonomy.yaml` via `settings.specialty_taxonomy_path` to
      populate `specialty_category`; unmapped panels handled explicitly.

### Monitoring (Phase 4) & Databricks dry run (Phase 5)
- [ ] Databricks: `storage_mode=catalog` + `table_ref` resolution exercised against a
      real workspace (currently only mechanism-tested).

---

## 6. Quick command reference

```bash
# Local (dev VM / any non-JVM machine): lint + fast tests
make lint
uv run pytest -q -m "not spark"          # venv outside the mount; TMPDIR off the mount

# Full substrate (needs JDK 17 → image or CI)
make docker-build                                  # build the JDK-17 dev image
docker compose run --rm test                       # full suite (Spark incl.) in the image
docker compose run --rm registry registry smoke    # Phase 0 acceptance only

# Live source (only where fda.gov is reachable)
uv run registry ingest-fda-list --dry-run --verbose
uv run pytest -m live_network
```
