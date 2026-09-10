# Scheduled FDA ingest — operations

The `Scheduled FDA ingest` workflow (`.github/workflows/scheduled-ingest.yml`)
runs the real bronze ingest on a GitHub-hosted runner, where `fda.gov` and
`api.fda.gov` are reachable (they are blocked from every agent/dev environment —
see [findings/0003](../findings/0003-openfda-live-verification.md)).

## What it does

On a weekly schedule (Mondays 06:30 UTC) and on manual dispatch:

1. Sets up JDK 17 + `uv`, installs deps, stages the Delta JARs (same as CI).
2. Validates the openFDA API key — only if the `OPENFDA_API_KEY` secret is set.
3. Dry-run: `registry ingest-fda-list --dry-run` (fetch + parse, no write).
4. Real write: `registry ingest-fda-list` → `bronze_fda_ai_list`.
5. Acceptance gate: `scripts/check_bronze_rowcount.py` fails the run if the latest
   snapshot holds fewer than `INGEST_MIN_ROWS` rows (default 1500, ~95% of the
   ~1,615-row source list). This is the automated Phase 1 acceptance check.
6. Uploads the bronze snapshot (`lakehouse/**`) and `ingest-summary.txt` as a
   build artifact (30-day retention).

Steps 4–6 are skipped when you dispatch with **dry_run = true**.

## Adding the openFDA API key (one-time)

The key is optional for the FDA AI-list ingest (that path does not call openFDA),
but it is what the openFDA client (roadmap Issue 1) will use, and setting it now
lets the workflow's auth-check confirm it works. openFDA without a key is limited
to 1,000 requests/day; with a key, 120,000/day.

**GitHub UI:** repo → **Settings → Secrets and variables → Actions → New
repository secret**. Name it exactly `OPENFDA_API_KEY`; paste the key as the
value; save. It is write-only afterward and masked in logs.

**gh CLI (paste when prompted — the value is not echoed):**

```bash
gh secret set OPENFDA_API_KEY --repo jonathancrawford05/medtech-ai-intelligence
```

The workflow maps it to the env var the app reads:
`REGISTRY_OPENFDA_API_KEY` → `settings.openfda_api_key` (env prefix `REGISTRY_`).
Never commit the key or put it in `.env` in the repo — `.env` is git-ignored and
for local use only.

## Optional: tune the acceptance floor

If the source list's size drifts materially, set a repository **variable** (not a
secret) `INGEST_MIN_ROWS` (Settings → Secrets and variables → Actions →
Variables). The workflow falls back to `1500` when it is unset.

## Running it manually

Actions → **Scheduled FDA ingest** → **Run workflow**. Leave `dry_run` unchecked
for a real ingest, or check it to test fetch/parse and the URL/headers without
writing bronze. The row-count summary appears in the run's **Summary** panel and
in the uploaded artifact.

## Running locally instead (on a network-permitted machine)

```bash
make install                       # sync venv + stage Delta JARs
make ingest                        # uv run registry ingest-fda-list --verbose
uv run python scripts/check_bronze_rowcount.py
```

## Where the data goes, and the open decision

Today the bronze table is written to the runner's `./lakehouse` and kept only as
a build artifact — enough to prove the pipeline runs full-sized and to inspect a
pull, but **not durable shared state**. Choosing the persistent target (commit the
Delta files, push to cloud object storage, or write straight to a Databricks
Unity Catalog volume — `settings` already supports `storage_mode=catalog` with a
`catalog.schema` root) is the next decision and should be recorded as an ADR
before this workflow is relied on as the system of record.
