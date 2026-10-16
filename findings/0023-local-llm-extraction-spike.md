# 0023 — 4D spike: study-evidence extraction with a local LLM (interim, 83 / 153)

**Date:** 2026-10-15 · **Status:** Interim. The cardiovascular/metabolic run is 83 of 153
documents done; the numbers below are updated when it finishes. No hand-checked gold set yet.
· **Component:** Issue 4 PR 4D (LLM-assisted extraction), provider decision in
[the 4D handoff](../docs/handoffs/issue-4-document-pass.md) §3 rule 5 (PR #22)

The 4D plan asks whether a local model can pull study evidence (performance figures, study
design, sample size, demographics, clinical endpoint) out of the 510(k) Summaries already in
`bronze_summary_documents`, with every value carrying a verbatim quote that a deterministic
check can verify. This spike answers that on real documents before any 4D code is written.
It is a spike, not the pipeline: it reads the lakehouse once, runs outside the package, and
writes nothing to Delta.

## Setup

| | |
|---|---|
| Model | `qwen3:14b` via Ollama, digest `bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8` |
| Settings | temperature 0, seed 42, `think: false`, `num_ctx` 16384, JSON schema in Ollama's `format` |
| Hardware | MacBook Air 13" (M4, 24 GB, fanless) |
| Input | only the pages 4B classed as Summary (`summary_pages`), labelled `=== PAGE n ===`; documents over 40k characters keep their keyword-bearing pages (2 of 153) |
| Documents | all cardiovascular + metabolic devices with readable Summary text: 153 (11 `mortality_confirmed=true`, 120 curated negatives, 22 unreviewed), confirmed first |
| Fields | `performance_reported`, `metrics[]` (metric, label, context, setting, value, CI), `study_design`, `sample_size` + unit, `multi_site`, `demographics_disclosed`, `clinical_endpoint` — each value with a quote |

The page number is **not** asked of the model. The check finds the quote and takes the page
from where it is found (the model's own page numbers were wrong on about a third of quotes in
prompt v1).

## The deterministic checks (the hallucination guard)

Every value is accepted or rejected by code, never by the model. In order:

| Status | Rule | Accepted? |
|---|---|---|
| `ok` | quote is a substring of the page text (NFKC, quotes/dashes unified, whitespace collapsed, case-folded, trailing punctuation dropped) | yes |
| `ok_loose` | same, ignoring all characters but letters, digits, `%` and `.` (PDF artefacts such as `75 .9%`, `per -protocol`) | yes |
| `ok_gapped` | the quote's words appear verbatim, in order, as runs of ≥ 2 words with gaps ≤ 150 characters (a page footer inside a sentence) | yes, flagged |
| `ok_numbers` | quote not found, but the metric's value and both CI bounds appear in order on one page, with at most 3 numbers between them (table rows with `n/N` counts) | yes, flagged |
| `ok_rescaled` | value appears in its quote on the other scale (0.95 vs 95%) | yes, flagged |
| `value_not_in_quote` | quote found, but the metric value, a CI bound or the sample size is not a number in it | **no** |
| `quote_not_found` | none of the above | **no** |
| `duplicate` | same metric, setting, value and CI already listed | dropped |
| `absent` | `performance_reported=false` with no quote and no metrics (an absence has no sentence) | yes |

## How the prompt got here

| Version | Docs | What changed | What it showed |
|---|---|---|---|
| v1 | 2 | first schema, model gives page | 11/19 quotes on the stated page; 7 right text, wrong page; 1 reworded |
| v2 | 14 (+ crash) | page dropped; metric `setting` added | padding to the "at most 8" metrics; "dice" used as a catch-all; 11 exact duplicates; endpoint all `detection_diagnosis`; generation fell 10 → 5 tok/s, then the Mac crashed |
| v3 | 20 | no padding; label; `deterioration_risk`, `monitoring` endpoints; sample = patients not sites; batching + overheating stop | quotes stitched with dataset labels; the model copied wording from the prompt itself into invented figures (K251218); endpoint found 4 of 7 risk devices |
| **v4** | **83** | one contiguous quote, no prefixes; per-metric `context`; values as written; names only if the document uses them; never quote the instructions; clearer risk-index rule | below |

On the same first 20 documents, v4 against v3: metrics passing the quote check 54/56 (v3
43/58); risk devices labelled `deterioration_risk` 7/7 (v3 4/7); same speed.

## Results so far (v4, 83 documents)

**Fill rates** (accepted, non-null):

| Field | Filled | Values |
|---|---|---|
| `clinical_endpoint` | 79 / 83 | detection_diagnosis 59, deterioration_risk 12, measurement_quantification 7, monitoring 1 |
| `performance_reported` | 63 / 83 | true 44, false 19 |
| `demographics_disclosed` | 36 / 83 | true 35, false 1 |
| `multi_site` | 36 / 83 | true 36 |
| `sample_size` | 33 / 83 | — |
| `study_design` | 33 / 83 | retrospective 19, prospective 14 |
| metrics | 38 / 83 documents | 310 accepted; sensitivity and specificity dominate; all `standalone` (no reader studies in this specialty so far) |

The low fill on design, size and demographics is mostly the documents, not the model: many
cardiac-monitor and ECG Summaries describe bench testing or cite a predicate without
reporting a clinical study. Where `performance_reported` is false or null but the text names
sensitivity/specificity, the text usually names the measures without results ("performance
testing included the evaluation of metrics such as sensitivity, specificity…"); 2 of 11 such
documents (K252984, K242737) are borderline accuracy-specification tables.

**Quote check:**

| | Accepted | Rejected | Duplicates dropped |
|---|---|---|---|
| Metrics | 310 (ok 221, loose 14, gapped 38, numbers 36, rescaled 1) | 20 (6%) | 93 |
| Scalar fields | 276 (+ 206 null, 5 absent) | 11 (4%) | — |

**Clinical endpoint against the curated seed:**

| Seed | Risk endpoint (mortality / MACE / hospitalisation / deterioration_risk) | Other |
|---|---|---|
| confirmed (11) | 9 | 2 (HeartFlow K250902, Loss of Pulse K242967 — confirmed for reasons other than a risk output) |
| negative (72 so far) | 3 | 69 |

The 3 negatives: SignalHF K230842 (heart-failure decompensation risk) and Tempus ECG-AF
K233549 ("signs associated with" AF within 12 months) are defensible risk outputs and worth
a curator's second look; Volta K243812 is wrong (the quote is a trial endpoint). The two
HeartFlow filings disagree with each other (K213857 risk, K250902 detection) — a labelling
question for the gold set. None of the 22 unreviewed devices has been reached yet.

## Failure modes

1. **Fabrication fills gaps the text points at.** When a Summary refers to results it does
   not contain in text — a table that is only an image (eCART K233253: "test characteristics
   … are shown in" a figure) or a list of tests without results (K251218) — the model
   supplies plausible figures (AUC 0.87, "120,000 adult ward patients", ~98% agreement) with
   invented or result-free quotes. The check rejected every one. This is the main risk and the
   reason the check is not optional.
2. **Prompt leakage.** In v3 the model quoted the prompt's own example wording
   ("Dice/segmentation overlap of 0.95") as if it were document text. v4 tells it never to quote
   the instructions; the check catches it regardless.
3. **Right number, wrong name.** 13 of 222 named metrics (6%) carry the wrong measure, e.g. a
   "false positive reduction of 90.6%" as specificity, mean absolute error as
   `mean_difference`, a sample size of 125 as accuracy. The quote check cannot see this; the
   hand-check must.
4. **Table rows.** Quotes rebuilt from table cells with the column header added (K250151,
   K231335) fail the substring check although the figures are real; `ok_numbers` recovers them
   without admitting any fabrication seen so far.
5. **Runaway output.** K230842 produced 78 metric rows (9,258 tokens, 24 minutes), 58 of them
   duplicates. Rare (1 of 83) but costly.
6. **Arithmetic.** The model sums datasets (6,534 + 5,069 = 11,603 for K233216) or rounds a
   bound (98.1% written 0.9817); both are rejected as values not in their quote, which is the
   intended policy.

## Throughput

Median 120 s per document (p90 315 s), median 487 output tokens, generation median
7.9 tok/s (range 4.8–11.0). 83 documents = 3.9 hours of model time. Generation is the cost:
a 14B model on a base M4 runs at about 10 tok/s cool, and the fanless Air throttles to
5–6 tok/s after ~45 minutes of continuous load. Batches of 5 with 5-minute rests, a stop
below 6 tok/s and a 20-minute cool-down between sittings keep it at 7–10 tok/s; sittings must
keep the Mac awake through the cool-down (`caffeinate -i` around the whole loop), or it sleeps
and the loop stalls. Full scale (1,519 readable Summaries) would be about 70 hours of model
time on this machine — overnight runs over a week, which is why this spike covers the
cardiovascular/metabolic subset that feeds the mortality leads.

## What this means for 4D

- The quote check is the design, not a safeguard on the side: page derived from the quote,
  the status ladder above, and value-in-quote for every number. The rejected values are
  exactly the fabrications.
- Ask for evidence the document can give: values as written, measure names the document uses,
  dataset/subgroup as `context`, and no counts ("at most N") that invite padding.
- Cap output (`num_predict`) and skip a document after one failed retry, so a runaway
  generation cannot stall a run.
- Treat `clinical_endpoint = deterioration_risk / mortality / mace / hospitalisation` as a
  **curation lead**, never as `mortality_confirmed_flag` (ADR 0007): it found 9 of 11
  confirmed devices and 2 plausible misses among the negatives.
- Metric names need the hand-check before any field ships; numbers and quotes are the more
  reliable part.

## Still to do (this finding is updated when they are)

- [ ] Finish the 153 (70 left) and update every table above; first look at the 22 unreviewed
      devices' endpoints.
- [ ] Hand-check 40–60 rows from `review-*.csv` (metric names, `ok_gapped` / `ok_numbers`
      rows, the endpoint disagreements) as the first gold set; report precision per field.
- [ ] Re-run 10 documents with the same digest and seed to confirm the output repeats.
- [ ] Compare `gemma4:e4b` (lighter) and `gpt-oss:20b` on the confirmed 11 for the fields that
      fail the gold bar.

## Reproduce

The spike scripts live in `logs/spike-4d/` on the maintainer's Mac (git-ignored; they move
into the 4D PR as the provider and check modules): `inputs-cvmet.jsonl` (built from
`bronze_summary_documents` + `silver_document_extraction.summary_pages` +
`silver_devices.specialty_category`), `run_ollama.py` (prompt `4d-spike-4`), `score.py` (the
checks above), `run_all.sh` (batched sittings).

```text
caffeinate -i bash logs/spike-4d/run_all.sh
python3 logs/spike-4d/score.py logs/spike-4d/outputs-qwen3_14b-4d-spike-4-inputs-cvmet.jsonl
```
