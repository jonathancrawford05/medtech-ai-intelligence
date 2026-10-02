# 0014 — Widening the stage-1 mortality keyword list, from curation evidence

**Date:** 2026-09-26 · **Status:** `transform/mortality_seed.py` keyword pass widened · **Component:** `transform/mortality_seed.py` (stage-1 keyword regex; no change to the seed loader, the mart, or ADR 0007/0014 behaviour)

[Finding 0012](0012-mortality-seed-curation.md) curated 133 cardiovascular devices into
`config/mortality_seed.yaml` (11 `mortality_confirmed=true`, 122 false) and left an open note,
carried into `CONTINUATION.md` item 11: the stage-1 keyword list was under-recall against the
devices curation had actually confirmed, and it should be widened **from evidence**, not from
one cherry-picked example. This closes that. It changes only the deterministic keyword regex —
stage 1 remains a *lead flag*, never the mart filter (ADR 0014); the mart still gates on
`mortality_confirmed_flag` alone, so nothing here can change which rows reach gold.

## Why widen at all, if it never filters

`keyword_flag()` exists so that a curated `confirmed=true` row that the keyword pass *missed*
shows up as `keyword_disagrees` in the report — the row worth looking at twice. A stage-1 list
that misses most confirmed devices makes that column meaningless: everything disagrees, so
nothing stands out. Widening it to match the vocabulary curation has now seen means a future
`keyword_disagrees` genuinely flags "the curator confirmed something the deterministic pass had
no word for" — a real signal that the keyword list has fallen behind the evidence again.

## Method

Extracted the compiled `_MORTALITY_KEYWORDS` regex from the file (both the committed `HEAD`
version and the working-tree edit) and applied each to the `intended_use_text` of all 133
reviewed seed entries. `keyword_disagrees` = `confirmed=true` **and** keyword pass returns
`False`. Deterministic, no Spark, no network — the same function the loader calls.

## Result

| | confirmed missed by stage 1 (`keyword_disagrees`) | negatives also flagged (of 122) |
|---|---|---|
| **before** (shipped regex) | 8 of 11 | 1 (`K192732`, on "life threatening") |
| **after** (widened regex)  | **0 of 11** | **1** (same `K192732` — unchanged) |

The widening closes all 8 remaining disagreements **and introduces zero new false hits on the
122 curated negatives.** The single negative that the keyword pass flags (`K192732`) was already
flagged by the shipped regex's "life threatening" term and is unaffected — it is the expected,
harmless case of "keyword says maybe, curation said no," which is exactly what stage 1 is
allowed to do.

## Terms added, and the device each recovers

Seven alternations were appended. Each was taken from the verbatim intended-use language of a
device curation had **already confirmed** in finding 0012 — not invented:

| term added | recovers (confirmed device) | matched substring |
|---|---|---|
| `hemodynamic instabilit\w*` | K200717, K212219 (AHI), K233216 (CLEWICU) | "hemodynamic instability" |
| `hypotens\w*` | K183646 (Acumen HPI) | "Hypotension" |
| `loss of pulse` | K242967 (Loss of Pulse alarm) | "Loss of Pulse" |
| `h(?:ae\|e)morrhag\w*` | K233249 (APPRAISE-HRI) | "hemorrhage" |
| `deteriorat\w*` | K200717 (co-catch) | "deterioration" |
| `plaque\w*` | K213857, K250902 (HeartFlow) | "plaque" |
| `hypoperfus\w*` | — (companion term) | — |

`hypoperfus\w*` recovers none of the eight on its own; it is kept because the "Global
Hypoperfusion Index" phrasing was attested in the finding-0012 evidence for the CLEWICU family
and costs nothing to guard against. The three confirmed devices the shipped regex *already*
caught (K172959, K183370, K233253) still match. The British/American `h(?:ae|e)morrhag`
spelling is covered deliberately, since FDA summaries mix both.

## What was deliberately not done

- **No tuning to a single example.** Every term traces to a distinct confirmed device in the
  curated seed; the change was validated against all 133 rows at once.
- **No change to the mart, the loader, or the flag semantics.** ADR 0007's two-stage design and
  ADR 0014's "confirmed flag is the only gate" are untouched. Stage 1 is still recall-oriented
  and disposable; over-inclusion here is free because it never reaches gold.
- **No claim that the list is now complete.** It matches the vocabulary of *this* sample. The
  `keyword_disagrees` column is the mechanism by which the next confirmed-but-unmatched device
  will surface, and the next widening should again come from accumulated cases, not guesswork.

## Tests

`tests/test_mortality_seed.py::TestStage1KeywordCoverage` asserts the evidence phrasings above
return `True`, that routine cardiology text (arrhythmia, stethoscope, pulse-rate spot
measurement) still returns `False`, and that the original terms still match — so a future edit
that drops recall or over-broadens fails loudly.
