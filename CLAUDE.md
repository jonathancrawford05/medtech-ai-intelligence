# CLAUDE.md — working rules for this repo

Read this first, then `CONTINUATION.md` (current state) and `docs/adr/` (why).
These are the invariants a change must respect; a PR review (human or agent) checks
against them. See `docs/validation-playbook.md` for *how* to validate, and
`docs/pr-review-routine.md` for the review prompt itself.

## What this project is
A Python-native, Spark + Delta Lake registry of FDA-authorized AI/ML medical devices,
designed to migrate to Databricks unchanged. Its purpose is to surface how medical
technology is evolving — especially **mortality-relevant cardiovascular/metabolic
risk-stratification AI** — as leads for Munich Re's underwriting and partnership work.
Build off **primary sources** (the FDA curated list + openFDA), never a single
third-party tracker's counts.

## Architecture invariants (do not break)
1. **No filesystem paths in pipeline code.** Resolve tables by logical name via
   `settings.table_ref()` / `registry.tables` — this keeps the Databricks migration a
   config change, not a code change (ADR 0004). `storage_mode=path` locally,
   `catalog` on Databricks.
2. **Bronze is append-only.** Every pull is stamped with `ingested_at` and a
   content-hash `source_snapshot_id`; never overwrite a `bronze_` table. Dedupe and
   parsing are silver concerns, not bronze.
3. **Spark schemas are generated from the Pydantic models**, not hand-written;
   `tests/test_schemas.py` enforces field-for-field parity (ADR 0008).
4. **Settings are `REGISTRY_`-prefixed and `lru_cache`d.** Tests that vary env build
   `Settings()` directly or call `get_settings.cache_clear()`; a conftest autouse
   fixture strips stray `REGISTRY_*`.
5. **Acquisition is resilient by design** (ADR 0009): known CSV URL → discover the CSV
   link off the page → HTML table fallback. Content-Type decides parsing, not the URL.

## How we work
- **TDD.** Write the failing test first; every module here was built that way.
- **Markers.** `spark` = needs a JVM (slow); `live_network` = hits real FDA/openFDA
  endpoints and **never runs in CI**. Tag new tests correctly.
- **Fixtures reflect reality.** When a live source exists, the fixture is a real slice
  of it; keep the hand-written "messy" fixtures for defensive edge cases.
- **Decisions are ADRs.** Anything a future session might re-litigate gets a numbered
  ADR in `docs/adr/`. Never edit an accepted ADR — add one and mark the old
  `Superseded by NNNN`.
- **Update the handoff.** Before ending a session, update `CONTINUATION.md` §1/§2/§4
  and add a `findings/` note (in the project) for anything a reviewer looks for later.
- **Keep scope narrow:** one source working end to end beats four half-wired.

## Commands
```bash
make lint                         # ruff check + format --check
make test                         # full suite  (needs JDK 17 → use Docker if local JDK < 17)
uv run pytest -q -m "not spark"   # fast, no JVM
make docker-build                 # build the dev image
docker compose run --rm test      # full suite inside the JDK-17 image
uv run pytest -m live_network     # ONLY where fda.gov is reachable
```

## Environment traps (see the validation playbook for the full matrix)
- **Spark 4.0 needs JDK 17+.** Run Spark tests / `registry smoke` in the image or CI,
  not on a JDK-11 box.
- **`fda.gov` is blocked from agent environments.** Live-source checks run on a machine
  that can reach it (or via a browser).
- **Docker `JAVA_HOME` is arch-derived** (symlinked to `/opt/java` at build), so
  `make docker-build` / `docker compose run --rm test` work natively on arm64 and
  amd64. `--platform linux/amd64` is optional (CI/DBR parity), not required.

## Git
- Work on the branch named at the top of `CONTINUATION.md` unless told otherwise.
- `make lint && make test` must be clean before committing.
- Commit-message trailer for agent commits:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
