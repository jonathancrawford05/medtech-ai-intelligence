# 0005 — First live ingest attempt: acquisition proven, bronze write blocked by a missing JVM

**Date:** 2026-09-13 · **Status:** Partly open · **Component:** `ingest/fda_ai_list.py`, `spark_session.py`

The first attempt to run the real ingest from a developer machine (macOS, Apple
Silicon). It closes the **acquisition** half of
[finding 0001](0001-phase-1-live-ingestion-gap.md) by execution rather than
inspection, and it surfaced a first-run failure mode worth guarding.

## Verified by execution

`uv run registry ingest-fda-list --dry-run --verbose`, 2026-09-13 11:55 UTC:

```text
HTTP Request: GET https://www.fda.gov/media/178541/download?attachment "HTTP/1.1 200 OK"
Content-Type: text/csv; charset=UTF-8
Content-Disposition: inline; filename=aiml-devices-csv.csv
Last-Modified: Fri, 04 Sep 2026 11:54:10 GMT
Fetched 1614 rows from https://www.fda.gov/media/178541/download?attachment (csv, snapshot b1efeb680371f44c)
```

| Claim | Status |
|-------|--------|
| The known CSV URL still resolves (tier 1, no discovery, no HTML fallback) | **Verified** — one 200 on `settings.fda_ai_list_csv_url` |
| Content-Type drives parsing as CSV | **Verified** — `text/csv`, parsed as csv |
| The export is the full list | **Verified** — 1,614 rows, matching ADR 0009's ~1,615 |
| `parse_csv` handles all 1,614 real rows | **Verified** — no `SourceFormatError`, no skipped-row warnings |
| A bronze write of the full list succeeds | **Still never executed** — see below |

This is the first evidence that the acquisition path works end to end against the
real source from a real machine, not a browser. The row count also gives the
scheduled workflow's `INGEST_MIN_ROWS=1500` floor a measured basis: 1,614 actual
against a 1,500 gate is ~93% headroom, so the gate trips on real truncation
rather than on normal week-to-week drift.

## The failure that followed

The same command without `--dry-run` died in py4j:

```text
The operation couldn't be completed. Unable to locate a Java Runtime.
PySparkRuntimeError: [JAVA_GATEWAY_EXITED] Java gateway process exited before sending its port number.
```

No JDK on the machine. Spark 4.0 needs 17+, which `CLAUDE.md` already documented
as an environment trap — but nothing in the code checked it, so a documented,
expected condition surfaced as a ~60-line traceback naming neither cause nor cure.
Two further details the traceback gave away:

- `--dry-run` returns before `get_spark()`, so **the dry run had already
  succeeded**; only the write path failed. Worth knowing when triaging: a failure
  here does not implicate acquisition.
- The log line `Delta JARs not pre-staged; resolving from Maven` shows the venv
  came from a bare `uv sync` rather than `make install`. Even with a JDK present,
  that path is slow and needs Maven reachable.

## What changed as a result

`spark_session._require_jvm()` now runs before the local session is built. It
probes `JAVA_HOME` then `PATH`, parses both the modern (`17.0.20.1`) and legacy
(`1.8.0_401`) version layouts, and raises `JavaRuntimeError` naming all three
remedies — the Docker image, a local JDK install plus `make install`, or the
Scheduled FDA ingest workflow. It is skipped on Databricks, where the runtime
supplies its own JVM.

## How to close the rest

The bronze write still needs to run at full size. Easiest is the workflow, which
needs no local JDK and applies the acceptance gate:

Actions → **Scheduled FDA ingest** → Run workflow (leave `dry_run` unchecked).

Locally, either `docker compose run --rm registry ingest-fda-list`, or install a
JDK and use `make install && make ingest`. Record the rows-written count and the
`check_bronze_rowcount.py` result in a follow-up finding; that closes finding 0001
and the development plan's Phase 1 acceptance criterion.
