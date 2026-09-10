# 0003 — openFDA path verified live; scheduled ingest built to close the Phase 1 gap

**Date:** 2026-09-08 · **Status:** Partly verified — see below · **Component:**
`ingest/openfda_client.py` (planned, roadmap Issue 1), `tests/fixtures/openfda/`,
`.github/workflows/scheduled-ingest.yml`, `scripts/check_bronze_rowcount.py`

## The finding

Two things were established: (a) **why** the agent environments can never touch
the FDA sources, and (b) **what** openFDA actually returns — captured against the
live API, not inferred. A scheduled workflow now exists that will run the real
bronze write on a network-permitted runner, which is what closes
[finding 0001](0001-phase-1-live-ingestion-gap.md) — but it has **not run yet**,
so 0001 stays open until its first green run.

### Why the "blocked" claim is real, and what it is not

`fda.gov` and `api.fda.gov` are refused at the egress proxy of **every** agent
environment — the cloud workspace, the dev VM, and the desktop-app Linux VM
behind `device_bash` all return `403 connect_rejected` (an org egress allowlist;
`api.github.com` and package registries pass, arbitrary hosts do not). This is a
property of the **agent sandbox**, not of the FDA sources (which are fully public)
and not of where the prototype runs. A GitHub Actions runner, a container on the
user's own infra, or the user's own machine all reach FDA normally. The capture
below was therefore done through a browser on a permitted network.

## Verified (executed against the live API, 2026-09-08; `meta.last_updated` 2026-08-31)

Real responses for the AI-list slice in `tests/fixtures/fda_ai_list_sample.csv`
were captured and saved (trimmed) under `tests/fixtures/openfda/` — see that
directory's `PROVENANCE.md` for the per-file table and replay instructions.
Findings that **change Issue 1's design**, all confirmed by execution:

1. **No `de_novo` endpoint exists.** `GET /device/de_novo.json` → bare
   `Cannot GET /device/de_novo.json` (an Express routing 404, not a JSON
   `NOT_FOUND`). Do not build a `de_novo` client method.
2. **De Novo grants live in the `510k` endpoint**, keyed by the `DEN…` number in
   `k_number`, distinguished by `decision_code:"DENG"` (vs `"SESE"`) /
   `clearance_type:"Direct"`. Fetch map: `K…`,`DEN…` → `510k`; `P…` → `pma`.
3. **PMA supplements are a separate field.** openFDA stores `pma_number:"P130020"`
   and `supplement_number:"S005"` separately — no `"P130020/S005"` exists. The bronze
   value must be split on `/` before lookup. (Answers the roadmap's supplement
   ADR question on the lookup side.)
4. **Exact-ID OR batching fails.** `pma_number:(A B C)` is AND-ed → `NOT_FOUND`.
   Query one submission number per request (which the caching design wants anyway).
5. **PMA rows carry no specialty/regulation.** `openfda.medical_specialty_description`
   is `"Unknown"` and `regulation_number` is `""` on every `pma` row; only
   `device_class` is set. Specialty for PMA devices must come from the AI-list
   `Panel (Lead)` column. `510k`/De Novo rows *do* carry both.
6. **Recent clearances are present** (June 2026 510(k)s, a March 2026 PMA all
   resolved), but a not-yet-propagated submission is still possible — treat empty
   `results` / `NOT_FOUND` as "no record yet" (`None`), not an error.
7. **Product codes can disagree** between the AI list and openFDA (P950009: list
   `NMN` vs openFDA `MNM`). Decide authority in the silver ADR.

## Assumed / not yet verified

- **The full-sized live bronze pull.** The scheduled workflow performs
  `fetch_raw → parse_csv → ingest_snapshot → bronze` and then
  `scripts/check_bronze_rowcount.py` asserts ≥ `INGEST_MIN_ROWS` (default 1500,
  ~95% of ~1,615). This is wired but **has not executed** — no bronze table has
  yet held the full list. Finding 0001 remains **Open** until the first green run.
- **The openFDA client itself** (Issue 1) is not built; only its fixtures and the
  design constraints above are established.
- The openFDA **API key** is validated by the workflow's auth-check step, but only
  once the `OPENFDA_API_KEY` secret is set (see `docs/scheduled-ingest.md`).

## What was built

- `tests/fixtures/openfda/` — 9 real (trimmed) response fixtures + `PROVENANCE.md`.
- `.github/workflows/scheduled-ingest.yml` — weekly + manual; dry-run, then real
  bronze write, then the row-count acceptance check, then uploads the bronze
  snapshot + summary as an artifact. Reads `OPENFDA_API_KEY` from repo secrets.
- `scripts/check_bronze_rowcount.py` — the automated Phase 1 acceptance gate.

## How to re-check

- **Live openFDA shapes:** in a browser or on any permitted host,
  `https://api.fda.gov/device/510k.json?search=k_number:DEN250057&limit=1` and
  `…/device/pma.json?search=pma_number:P130020&limit=25`.
- **The bronze pull (closes 0001):** run the `Scheduled FDA ingest` workflow
  (Actions → Run workflow), or locally on a permitted network:
  `make ingest && uv run python scripts/check_bronze_rowcount.py`. A green run
  with `≥ 1500` rows closes finding 0001 — add its `**Closed:**` line then.
