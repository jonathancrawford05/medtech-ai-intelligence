# Runbook — weekly change monitoring on a local lakehouse

How to run the registry end to end on the Mac until durable storage exists
([ADR 0011](adr/0011-defer-durable-bronze-persistence.md)). The scheduled GitHub
workflow starts from an empty lakehouse every run, so **only a lakehouse that
persists between runs accumulates silver history, and only silver history produces
leads** ([ADR 0015](adr/0015-silver-snapshot-pairing.md),
ADR 0017 (`docs/adr/0017-leads-output-surface.md`, arriving with PR #14)). Today that is `./lakehouse` on this
machine. Back it up; it is not reconstructible.

Run from the repo root on `main`, on a JDK-17 host (CLAUDE.md). `registry monitor` and
the leads table arrive with Issue 3 PR B (#14); until then the script skips step 5. One
command does the whole sequence:

```bash
scripts/local_weekly_run.sh            # logs to logs/weekly-<timestamp>.log
```

## The sequence, and what to look for at each step

| # | Command | Healthy output | Investigate when |
|---|---|---|---|
| 1 | `uv run registry ingest-fda-list -v` | `Appended ~1614 rows to bronze_fda_ai_list (snapshot <hash>)` | Rows well under ~1,500 (a truncated pull; see step 5), or a fetch error. The snapshot hash is the content hash: **the same hash as last week means the FDA list has not changed**, which is normal and frequent. |
| 2 | `uv run registry enrich-openfda -v` | Mostly cache hits; new submissions fetched | Many misses or 429s. Only new devices need network; re-runs are near-free. |
| 3 | `uv run registry build-silver -v` | `Wrote N rows …` when the list or enrichment changed, otherwise `… is already current (same bronze snapshot, identical rows); nothing written` | It writes on a week where nothing changed: something upstream moved (a config edit, enrichment) — fine if you did that. |
| 4 | `uv run registry build-mart -v` | Row count of `gold_mortality_relevant` (11 today) | The count drops without a seed change. |
| 5 | `uv run registry monitor -v` | One of the four statuses below | Exit code 1 (history barrier). |
| 6 | `uv run registry inspect` | Exit 0; GOLD section shows the confirmed devices | Exit non-zero: read the flagged section. |

### Reading `registry monitor`

- **`Fewer than two distinct snapshots in silver; nothing to diff.`** Expected until
  the FDA list changes at least once after the snapshot stamp landed (PR #13). Not an
  error. The leads table is still created, empty.
- **`Recorded N lead(s) for <prev> -> <curr>`** followed by a count per category
  (`new_submission`, `cardiometabolic`, `mortality_language`, `life_sustaining`).
  This is the payoff. `new_submission` is every added device and is noisy by design;
  triage on the other categories (ADR 0016).
- **`Leads for <prev> -> <curr> are already in …; nothing appended.`** A re-run of a
  pair already recorded. Safe.
- **`Not diffing: unrecognised Delta operation …`** (exit 1). Something other than
  the pipeline touched silver (or Delta added an operation). See below.
- **`Warning: N removal lead(s) not recorded; the newest pull looks truncated.`**
  The newest pull carried < 95% of the previous one. Re-run step 1 before trusting
  removals; additions and changes were still recorded.
- The `deferred:` lines (PCCP, foundation-model) print every run by design.

### Recovering from a history barrier

The walk over silver's history stops at a Delta operation it cannot classify, so the
monitor refuses to pair across it. To re-establish a baseline **without losing a
week's movement**:

1. Before the next ingest, run `uv run registry build-silver -v` against the
   **current** bronze. The barrier makes the gate rebuild, so this stamps a fresh
   version of the current snapshot above the barrier.
2. If the operation is known to leave rows alone, add it to
   `METADATA_ONLY_OPERATIONS` in `src/registry/tables.py` in a reviewed PR instead.

If you ingest first, the new snapshot becomes the only stamped version above the
barrier and that week's movement is never diffed.

### Looking at the leads directly

`inspect` does not report `gold_device_leads` yet. Until it does:

```bash
uv run python - <<'PY'
from registry.config.settings import get_settings
from registry.spark_session import get_spark
from registry.mart import leads
s = get_settings(); spark = get_spark(s)
leads.lead_counts(spark, s, grain="detection").show(50, truncate=False)
from registry import tables
(tables.read_table(spark, s, leads.LEADS_TABLE)
   .select("detected_at", "submission_number", "movement", "categories",
           "changed_fields", "device_name", "applicant_resolved", "specialty_category")
   .orderBy("detected_at", ascending=False).show(50, truncate=False))
PY
```

## Cadence

The FDA updates its AI-device list irregularly; weeks with an unchanged snapshot are
normal. Running weekly costs a minute when nothing moved. To schedule it, a
`launchd` agent or `cron` entry pointing at `scripts/local_weekly_run.sh` is enough;
the laptop must be awake and on a network that reaches `fda.gov`.
