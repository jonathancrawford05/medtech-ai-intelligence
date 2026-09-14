# 0007 — Silver build: what it is verified to do, and what it leaves unenriched

**Date:** 2026-09-13 · **Status:** Open (enrichment pass outstanding) · **Component:** `transform/`

Roadmap Issue 2. Records what `bronze_to_silver` is tested to guarantee, and —
more usefully — the parts of `DeviceRecord` it deliberately does not populate.

## Verified by execution

`make test` on this branch: **192 passing, 90% coverage**, ruff and markdownlint clean.

| Acceptance criterion (roadmap Issue 2) | Status |
|---|---|
| One row per submission, from the latest pull | **Verified** — `test_one_row_per_submission_from_the_latest_pull` |
| Schema parity via the generated `StructType` | **Verified** — `test_silver_matches_the_generated_schema` |
| No null `submission_number` / `decision_date` | **Verified** — `test_no_null_keys_or_dates` |
| Deterministic on fixtures | **Verified** — rebuild replaces rather than appends |
| `pathway` derived from the submission prefix | **Verified** — incl. `P130020/S005` → `pma` |
| Panel → specialty, unmapped handled explicitly | **Verified** — defaults *and* records in `unmapped_panels` |
| Company resolution from a curated lookup | **Verified** — 20 companies, 44 aliases |

Two rules were **mutation-tested** rather than assumed, being the ones whose
silent failure would be hardest to notice:

- Reversing the window ordering (oldest pull wins) fails
  `test_one_row_per_submission_from_the_latest_pull` and nothing else.
- Switching the silver write from `overwrite` to `append` fails
  `test_rerunning_replaces_rather_than_appends` and nothing else.

## What silver does NOT yet contain

Per [ADR 0012](../docs/adr/0012-silver-schema-and-supplement-handling.md), these
are `None` in every row the current transform writes:

| Field | Source |
|---|---|
| `device_class` | openFDA classification endpoint |
| `predicate_submission_number`, `predicate_age_days` | openFDA 510(k) record |
| `has_pccp`, `pccp_summary` | openFDA / device summary text |
| `cybersecurity_statement_present` | openFDA / device summary text |

**This is the honest state of the table, not a bug.** The openFDA client exists
and is fixture-verified (ADR 0010), but nothing wires it into the transform yet.
Enrichment coverage is therefore currently **0%**, and is measurable at any time:

```sql
SELECT count(*) FILTER (WHERE device_class IS NOT NULL) * 1.0 / count(*) FROM silver_devices
```

Anyone reading silver should treat a `None` there as "not yet enriched", never as
"no" — and the gold mart must not infer absence of a PCCP from a null.

## Not yet run against real data

Every test above uses synthetic bronze rows. The transform has **never run over
the 1,614-row live pull** — the same inspection-versus-execution gap that finding
0001 tracked for ingestion, now one layer up. Closing it:

```bash
uv run registry ingest-fda-list --verbose   # if bronze is stale
uv run registry build-silver --verbose
```

Worth capturing when it runs: rows written versus 1,614 (the drop count is the
interesting number), the `unmapped_panels` warning, and how many applicants fell
through to the uncurated fallback — the last two are the curation backlog for
`config/specialty_taxonomy.yaml` and `config/company_aliases.yaml`.

## Judgement calls worth knowing about

- **A row is dropped** only when the key, device name, decision date or pathway is
  missing or underivable. Each drop is logged at debug and the total warned; a
  malformed row cannot fail the build.
- **An uncurated applicant resolves to its own cleaned name**, not to null —
  "we have not curated this" is different from "this has no applicant".
- **Silver is derived state**, so a rebuild overwrites. That is the one place the
  append-only rule does not apply: bronze is the history, silver is a view of it.
