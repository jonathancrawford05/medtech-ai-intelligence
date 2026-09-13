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

## Where the data goes

Decided in [ADR 0011](adr/0011-defer-durable-bronze-persistence.md): durable cloud
persistence is **deferred** until there is a business Azure tenancy, with
**Azure ADLS Gen2 named as the target**.

Until then:

- The runner's `./lakehouse` starts empty every run, so the CI table holds exactly
  one snapshot. That is fine for what this workflow is for — proving the pipeline
  runs full-sized and detecting source drift — but it is **not** shared state.
- Each run uploads the full snapshot as a build artifact with **90-day retention**.
  That is the off-machine archive: weekly captures that can be replayed into a real
  table once Azure exists. Bronze history is not reconstructible — the FDA list is
  mutable and you can only ever fetch *now* — so capturing each week matters even
  while there is nowhere durable to put it.
- The **developer's local `./lakehouse` is the working store**, refreshed weekly
  via `make ingest`. It is the only copy that accumulates a queryable history, so
  it wants an ordinary backup.

When Azure arrives, no pipeline code changes: set
`REGISTRY_LAKEHOUSE_ROOT=abfss://…` (still `storage_mode=path`), or
`storage_mode=catalog` for a Unity Catalog table over the same container. See the
ADR for the connector JARs and the single-writer LogStore caveat.
