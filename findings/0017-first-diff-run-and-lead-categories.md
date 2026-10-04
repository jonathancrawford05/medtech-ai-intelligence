# 0017 — First diff run (synthetic), and which lead categories are live

**Verified by test on 2026-10-04**: local Spark 4.0.1 / Delta 4.0.1 on host JDK 21
(the JDK-17 image cannot be built in the agent container, finding 0016; CI is
authoritative), plus ruff. Covers Issue 3 PR B:
[ADR 0016](../docs/adr/0016-leads-filter-pre-curation-signals.md) (filter set),
[ADR 0017](../docs/adr/0017-leads-output-surface.md) (output surface, fail-safe walk),
and ADR 0015 Decisions 1, 2, 6.

## Why the first run is synthetic

Bronze holds one distinct live snapshot (`b1efeb680371f44c`, finding 0015), and the
FDA list has not changed since. **There is no real movement to diff.** On the live
lakehouse, `registry monitor` is expected to print "Fewer than two distinct
snapshots in silver; nothing to diff", which is the correct empty-on-identical
answer. The first real diff happens the week the FDA list changes.

## Verified (two synthetic snapshots, `tests/monitor_fixtures.py`)

The fixture's ten submissions each have one job. The leads written to
`gold_device_leads` match them exactly:

| Submission | Movement | Categories |
|---|---|---|
| K700 | added | new_submission, cardiometabolic |
| K800 | added | new_submission |
| K900 | added | new_submission, mortality_language (from `device_name`) |
| DEN1000 | added | new_submission, life_sustaining (when enriched) |
| K300 | changed: specialty panel/category | cardiometabolic |
| K400 | changed: device name | mortality_language |
| K500 | removed | cardiometabolic |

- **Movement, not stock.** K100 (radiology) and K200 (cardiovascular) are identical
  in both snapshots and produce nothing. K600 left the list with no signal and
  is not a lead.
- **Empty on identical snapshots.** This holds two ways: `classify` of a snapshot
  against itself is `[]`, and with one distinct snapshot (three pulls, one hash)
  there is no pair at all. A rebuild on the same snapshot (re-enrichment) pairs
  only its newest version.
- **Pairing is by content snapshot.** A → B → A pairs (B, A-again). Unstamped
  versions are not pairable. Silver is read through time travel.
- **No curation needed.** K900 has no evidence row and is still a lead. A curator's
  `mortality_confirmed_flag=False` on K800 does not hide it. The leads module
  never names the confirmed flag (asserted).
- **Truncated pull.** When the newest pull carries 5 of 20 rows, 15 removal leads
  are suppressed and reported rather than recorded.
- **Append-only, first detection.** A re-run on the same pair appends nothing; a
  third snapshot appends only its own leads and keeps the earlier ones.
- **Time series.** `lead_counts` buckets by detection day and by clearance month, and
  returns an empty series (not an error) before any leads table exists.
- **Schema evolution does not invent changes.** A field only one version carries (added
  to `DeviceRecord` after the older version was written) is not compared.
- **Fail-safe walk.** An operation in neither list stops the history walk:
  `current_stamp()` is `None` and the next build writes; the differ refuses to pair
  across it and `registry monitor` exits 1 naming it.
- **The tests bite.** Each of these mutations, reverted afterwards, fails at least
  one test:
  - gating leads on curation
  - not counting `added` as a lead
  - comparing the row stamp in the diff
  - skipping unknown operations (the old walk)
  - removing the pull guard

## Live vs deferred categories

| Category | State | Source |
|---|---|---|
| new_submission | **live** | silver diff |
| cardiometabolic | **live** | `specialty_category` ∈ taxonomy `mortality_relevant_categories` |
| mortality_language | **live, weak** | stage-1 keywords over `device_name`, openFDA `classification_definition` (product-code level), curated `intended_use_text` where present |
| life_sustaining | **live** where enriched | openFDA `life_sustain_support` |
| pccp | **deferred** | no source carries it (finding 0011); Issue 4 |
| foundation_model | **deferred** | no field or heuristic exists; needs a curated keyword set and an ADR |

## Assumed / not yet verified

- **Signal quality on real data.** How many real new devices each signal catches
  is unmeasured: there is no real movement yet. `mortality_language` is expected
  to be weak because no uncurated device has device-level intended-use text in
  the pipeline.
- **The 95% pull guard** is sized to the scheduled ingest's 1,500 / 1,614 floor;
  it has not seen a real truncated pull.
- **Catalog mode**: same open item as finding 0016.

## How to re-check

```bash
uv run pytest -q tests/test_leads.py tests/test_monitor_differ.py tests/test_tables.py
uv run registry monitor -v   # live today: "Fewer than two distinct snapshots"
```
