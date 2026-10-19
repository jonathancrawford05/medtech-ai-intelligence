# 0024 — 4D framework built: the checks ported and proved on the recorded slice; no model run yet

**Date:** 2026-10-18 · **Status:** Built and verified offline against the recorded spike
slice (`tests/fixtures/llm_extraction/`, 43 documents, 58-row gold set). **Not yet run
against a live model**: Ollama is on the maintainer's Mac only; the first `4d.1` run and
a fresh gold draw are open (below). · **Component:** Issue 4 PR 4D,
[ADR 0022](../docs/adr/0022-llm-assisted-evidence-extraction.md), building on
[finding 0023](0023-local-llm-extraction-spike.md)

## Verified (in this session, by the test suite and `registry score-gold --recorded`)

**The port reproduces the spike scorer.** The scorer never left the Mac, so the port was
proved by recomputing the status the gold set recorded for each of its 58 rows:

| | Rows |
|---|---|
| same status as the spike | 57 |
| different status, same decision | 1 — row 11 (K250151 "Data Source Center 4 …"): spike `ok_gapped`, port `ok_numbers`. The header run sits ~180 normalised characters before "center 4", past the 150 gap; the numbers rule then finds the table row. Both accept and flag for review. |

Two fixes were needed to get there, both found by this comparison: tokens split on
punctuation (`92.26(88.05,` is two numbers), and the gapped search tries every
occurrence of a run, not only the first ("relative sensitivity of" recurs on the page
before the one K251221's quote is from).

**Every fabricated row is rejected.**

| Document | What the model invented | Status |
|---|---|---|
| K251218 | five ~98% "other" metrics on a test list without results | 5 × `value_not_in_quote` |
| K233253 | AUC 0.87 / 0.86 (×3 each) for an image-only table; "120,000 adult ward patients" | 2 × `quote_not_found` + 4 duplicates; sample size `quote_not_found` |
| K182456 | three 98.1% metrics off a font cipher | both pages flagged `garbled`: **never sent** (`skipped_no_pages`). Unfiltered, all three metrics still fail (`value_not_in_quote`) — but the endpoint quote, copied from the cipher, *passes* the substring check, which is why the garble test is needed |

**The garble test** flags exactly 6 of the 524 recorded pages in the repo (K182456 p7–8;
K141480 p4, 5, 7, 8, which 4B already reads as unreadable) and no other page.

**The checks on the 43 recorded responses** (prompt `4d-spike-4`, `CHECKS_VERSION 4d.1`):

| Field | Accepted (flagged) | Rejected | Dropped |
|---|---|---|---|
| metric | 236 (73) | 39: `measure_not_named` 21, `value_not_in_quote` 15, `quote_not_found` 2, `zero_triple` 1 | 72 duplicates (58 in K230842, as finding 0023 counted) |
| performance_reported | 42 (3) | 0 | |
| study_design | 26 | 1 | |
| sample_size | 25 | 5 | |
| multi_site | 27 | 2 (1 `no_quote`) | |
| demographics_disclosed | 25 | 2 (1 `no_quote`) | |
| clinical_endpoint | 43 | 0 | |

All 21 `measure_not_named` rejections were read by hand: every one is a genuine misname
(agreement filed as accuracy / precision / recall; mean absolute, absolute and heart-rate
error filed as accuracy; "Diagnostic Accuracy" filed as dice; PPV and false-positive
reduction filed as specificity). None is a correct value lost.

**Gold-set score of the recorded run under the new checks** (`registry score-gold
--recorded tests/fixtures/llm_extraction`):

| Field | Accepted: right / wrong / other | Precision of accepted | Rejected: right / wrong / other |
|---|---|---|---|
| metric | 14 / 2 / 0 (8 flagged) | 87.5% | 0 / 7 / 1 |
| sample_size | 5 / 0 / 1 | 100% | — |
| study_design | 4 / 1 / 0 | 80% | — |
| demographics_disclosed | 4 / 1 / 0 | 80% | — |
| multi_site | 3 / 0 / 0 | 100% | — |
| clinical_endpoint | 7 / 7 / 1 | 50% | — |

Against the spike's accept-everything-that-passes-the-quote (finding 0023: plain 8 / 8,
misnamed 0 / 6, gapped 2 / 5, table 4 / 5), the two new reject rules removed all 6
misnamed rows, the false-positive-reduction row and the `0 (0–0)` row, and **rejected no
right value**. The 2 wrong metrics still accepted are a predicate's specificity
(K213971, `applies_to` now asks) and "polarity accuracy" filed as precision (the label
says precision). The endpoint is the weak field, as the spike found; `4d.1`'s
future-event rule is what the next run measures.

## First live run: `4d.1` on the 11 confirmed devices (Mac, 2026-10-18)

`registry extract-evidence --only <the 11 mortality_confirmed devices>` with `qwen3:14b`.
All 11 answered `ok` (none hit the output cap, none skipped); 69 evidence rows.

| Field | Accepted (flagged) | Rejected | Statuses |
|---|---|---|---|
| metric | 26 (5) | 3 | ok 20, ok_gapped 3, ok_loose 1, ok_rescaled 2; `measure_not_named` 3 |
| performance_reported | 8 (1) | 2 | ok 3, ok_loose 2, ok_gapped 1, absent 2; `quote_not_found` 2 |
| study_design | 6 | 0 | ok 5, ok_loose 1 |
| sample_size | 4 | 1 | ok 4; `value_not_in_quote` 1 |
| multi_site | 5 | 1 | ok 4, ok_loose 1; `no_quote` 1 |
| demographics_disclosed | 4 | 1 | ok 4; `no_quote` 1 |
| clinical_endpoint | 7 | 1 | ok 7; `quote_not_found` 1 |

**Risk endpoints sent to curation: 4 of 11** (K183646, K200717, K233216, K233253). The
spike's `4d-spike-4` prompt called 9 of 11 confirmed devices a risk output; its two
others were HeartFlow K250902 and Loss of Pulse K242967. Only one of the drop is intended:
the gold set says HeartFlow K213857 is measurement, not risk. The other four of the five
lost are not yet explained. K172959 (Rothman Index, "aggregate statistical mortality risk") would be a
recall loss if it is not among them. And 3 of the 11 have no endpoint value at all
(`not_stated`). Whether the future-event rule is too strict for a *status index* is the
open question. It is read from the per-document rows before any prompt change, and
measured on the curated negatives (the 6 the spike over-called) before it is judged.

Not yet measured on this run: correctness. Only 5 of the 11 are in gold set v1, so this
is a yield, not a precision.

## Assumed (not verified here)

- The `4d.1` prompt and schema have **not been run**. Its new fields (`applies_to`,
  `is_result`, `ci_sided`, the error-measure names) are tested only with hand-built
  candidates against real pages and with synthetic Ollama replies.
- The Ollama request shape follows Ollama's documented `/api/chat` and `/api/tags`; the
  only real call is the `live_ollama` test, for the Mac.
- The gold figures are from 58 rows drawn to over-weight likely errors; they are not
  population rates (finding 0023 gives the weighted estimate). 14 / 16 still allows a
  true rate well below 87.5%.

## Still to do (this finding is updated when they are)

- [x] On the Mac: `uv run pytest -m live_ollama` (one real call), then
      `caffeinate -i uv run registry extract-evidence --only <the 11 confirmed> -v`.
- [ ] Draw a **fresh** gold set from a `4d.1` run (not the v1 rows) and score it:
      `registry score-gold --gold <new.csv> --bar <agreed>`.
- [ ] Re-run 10 documents with the same digest and seed to confirm the output repeats
      (carried from finding 0023).
- [ ] For any field below the bar on `qwen3:14b`, run `gpt-oss:20b`
      (`--model gpt-oss:20b`, `REGISTRY_LLM_THINK=low`) before deferring it.

## How to re-check

```bash
uv run pytest -q tests/test_llm_checks.py tests/test_llm_pages.py tests/test_llm_gold.py
uv run registry score-gold --recorded tests/fixtures/llm_extraction --bar 0.9
```
