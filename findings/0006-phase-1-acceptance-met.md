# 0006 — Phase 1 acceptance met: a full-sized bronze pull, locally and on CI

**Date:** 2026-09-13 · **Status:** Closed · **Component:** `ingest/fda_ai_list.py`, `scheduled-ingest.yml`

Closes [finding 0001](0001-phase-1-live-ingestion-gap.md) and the development
plan's Phase 1 acceptance criterion: *"running it live against the real FDA list
succeeds without errors for at least 95% of rows."*

## Verified by execution

Three independent fetches on 2026-09-13, all returning **the same
`source_snapshot_id`** — the content hash agreeing across a laptop and a GitHub
runner is itself evidence the snapshot mechanism works.

| Run | Where | Result |
|-----|-------|--------|
| Dry run | macOS, developer machine | 1,614 rows fetched and parsed; `b1efeb680371f44c` |
| Real ingest | macOS, developer machine | `snapshot b1efeb680371f44c: 1614 rows (1614 distinct submission numbers); floor 1500` |
| Real ingest | CI, `ubuntu-latest`, JDK 17 ([run 34764719600](https://github.com/jonathancrawford05/medtech-ai-intelligence/actions/runs/34764719600)) | `Appended 1614 rows to bronze_fda_ai_list (snapshot b1efeb680371f44c)` → `PASS: 1614 rows >= floor 1500` |

Against the acceptance criterion: **1,614 fetched, 1,614 parsed, 1,614 written,
1,614 distinct submission numbers.** That is 100%, not a marginal pass of the 95%
floor, and the distinct count confirms no duplicate keys in the source pull.

| Claim (from finding 0001) | Status |
|---|---|
| The known CSV URL serves it without falling back | **Verified** — one 200, `Content-Type: text/csv` |
| `parse_csv` handles all real rows | **Verified** — 1,614/1,614, no `SourceFormatError` |
| ≥95% of rows parse without error | **Verified** — 100% |
| A bronze write of the full list succeeds | **Verified** — locally and on CI |
| The acceptance gate works | **Verified** — `check_bronze_rowcount.py` passed on both |

## What this also proved

The `Scheduled FDA ingest` workflow had been merged but **never run** — an
authored-but-unexecuted claim of exactly the kind this directory exists to track.
Run 34764719600 was its first execution and exercised all eleven steps, including
the three that had never run anywhere: the bronze write, the acceptance gate, and
the artifact upload (7 files, 83 KB compressed). The openFDA API key secret was
set, so the auth-check step ran too.

The full pull compressing to **83 KB** is what made the persistence decision easy
to size: this data is tiny, so storage cost was never the constraint
([ADR 0011](../docs/adr/0011-defer-durable-bronze-persistence.md)).

## What is still not true

**Neither copy of bronze is durable shared state.** The CI runner starts from an
empty lakehouse every run, so its table holds exactly one snapshot; the
developer's local table accumulates but lives on one laptop. Deliberately
deferred in [ADR 0011](../docs/adr/0011-defer-durable-bronze-persistence.md),
which names Azure ADLS Gen2 as the target and bumps artifact retention to 90 days
so the intervening weeks are captured rather than lost.

This does not qualify the Phase 1 result — that criterion is about a live pull
landing full-sized in bronze, which happened twice. It qualifies what can be
built *next*: roadmap Issue 3 needs multiple snapshots to diff, and only the
local table currently accumulates them.

## How to re-check

```bash
uv run registry ingest-fda-list --verbose
uv run python scripts/check_bronze_rowcount.py
```

Or Actions → **Scheduled FDA ingest** → Run workflow, `dry_run` unchecked.
