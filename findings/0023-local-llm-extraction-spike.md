# 0023 — 4D spike: study-evidence extraction with a local LLM (153 cardiometabolic Summaries)

**Date:** 2026-10-15, full run completed 2026-10-16 · **Status:** Run complete, 153 / 153
documents. No hand-checked gold set yet: every figure below is a deterministic-check figure,
not a precision figure.
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
| **v4** | **153** | one contiguous quote, no prefixes; per-metric `context`; values as written; names only if the document uses them; never quote the instructions; clearer risk-index rule | below |

On the same first 20 documents, v4 against v3: metrics passing the quote check 54/56 (v3
43/58); risk devices labelled `deterioration_risk` 7/7 (v3 4/7); same speed. v4 then ran
unchanged over all 153 (an interim write-up at 83 documents gave the same picture).

## Results (v4, 153 documents)

**Fill rates** (accepted, non-null):

| Field | Filled | Values |
|---|---|---|
| `clinical_endpoint` | 148 / 153 | detection_diagnosis 109, deterioration_risk 17, measurement_quantification 17, monitoring 5 |
| `performance_reported` | 115 / 153 | true 72, false 43 |
| `sample_size` | 54 / 153 | median 536 |
| `demographics_disclosed` | 49 / 153 | true 45, false 4 |
| `multi_site` | 46 / 153 | true 46 |
| `study_design` | 45 / 153 | retrospective 25, prospective 19, mixed 1 |
| metrics | 66 / 153 documents | 428 accepted: sensitivity 154, specificity 100, accuracy 52, PPV 36, other 31, NPV 28, precision 11, mean difference 5, AUC 5, recall 4, Dice 2; all standalone but one |

The low fill on design, size and demographics is mostly the documents, not the model: many
cardiac-monitor, ECG and older (pre-2015) Summaries describe bench testing or cite a
predicate without reporting a clinical study. Where `performance_reported` is false or null
but the text names sensitivity/specificity, the text usually names the measures without
results ("performance testing included the evaluation of metrics such as sensitivity,
specificity…").

**Quote check:**

| | Accepted | Rejected | Duplicates dropped |
|---|---|---|---|
| Metrics | 428 (ok 307, loose 28, gapped 48, numbers 44, rescaled 1) | 39 (8%: 34 value not in quote, 5 quote not found) | 100 |
| Scalar fields | 450 (+ 432 null, 8 absent) | 28 (6%: 13 quote not found, 11 `false` without a quote, 4 value not in quote) | — |

Rejections cluster: 32 of 153 documents have any, and 12 documents account for over half.

**Clinical endpoint against the curated seed:**

| Seed | Risk endpoint (mortality / MACE / hospitalisation / deterioration_risk) | Other |
|---|---|---|
| confirmed (11) | 9 | 2 (HeartFlow K250902, Loss of Pulse K242967 — confirmed for reasons other than a risk output) |
| negative (120) | 6 | 114 |
| unreviewed (22) | 2 | 20 |

The 6 negatives: four are defensible risk or status-index outputs worth a curator's second
look — SignalHF K230842 (heart-failure decompensation risk), Tempus ECG-AF K233549 ("signs
associated with" AF within 12 months), VISENSIA K081140 (a patient status index, the same
construct as the confirmed Rothman Index) and Biovitals K183282 (a biovitals index); two are
wrong — Volta K243812 (the quote is a trial endpoint) and FFRangio K182149 (a fractional
flow reserve measurement). The 2 unreviewed are both curation leads: **Global Hypoperfusion
Index K231038** (risk of a hypoperfusion event; finding 0022 had already added it as a
previously omitted candidate) and SOZO K172507 (heart-failure fluid status). The two
HeartFlow filings disagree with each other (K213857 risk, K250902 detection) — a labelling
question for the gold set.

## Failure modes

1. **Fabrication fills gaps the text points at.** When a Summary refers to results it does
   not contain in text — a table that is only an image (eCART K233253: "test characteristics
   … are shown in" a figure) or a list of tests without results (K251218) — the model
   supplies plausible figures (AUC 0.87, "120,000 adult ward patients", ~98% agreement) with
   invented or result-free quotes. The check rejected every one. This is the main risk and the
   reason the check is not optional.
2. **Invented interval bounds.** Where a Summary gives a one-sided bound ("sensitivity was
   93.5% with a lower one-sided 95% CI of 87.8%", FFRangio K182149; DeepVessel FFR K213657),
   the model writes a two-sided interval and invents the upper bound. The value and lower
   bound are right; the CI check rejects the row. 4D needs a one-sided field, not a forced
   pair.
3. **Unreadable text read anyway.** Study Watch K182456 has a font-encoded text layer
   ("2C@ 1OP?T 5<O>C…") that 4A classed as `text`; the model returned 98.1% accuracy,
   sensitivity and specificity from it. Rejected, but it shows 4A's text class does not catch
   cipher-encoded fonts — a document-pass data-quality item, not only a 4D one.
4. **Prompt leakage.** In v3 the model quoted the prompt's own example wording
   ("Dice/segmentation overlap of 0.95") as if it were document text. v4 tells it never to quote
   the instructions; the check catches it regardless.
5. **Right number, wrong name.** 38 of 397 named metrics (10%) carry the wrong measure; 21 of
   them are error measures filed as `accuracy` (average root-mean-square error, mean absolute
   error, heart-rate error), the rest a "false positive reduction" or PPV filed as specificity.
   The quote check cannot see this; 4D's schema needs error measures (MAE, RMSE, ARMS) as their
   own names, and the hand-check must cover names.
6. **Table rows.** Quotes rebuilt from table cells with the column header added (K250151,
   K231335), or the wrong row's figures attached to a quote (Eko K213794), fail the substring
   check; `ok_numbers` recovers the real rows without admitting any fabrication seen.
7. **Runaway output.** 3 of 153 documents (K230842, K250151, K242129) ran past 4,000 output
   tokens; K230842 wrote 78 metric rows (9,258 tokens, 24 minutes), 58 of them duplicates.
8. **Unsupported negatives and arithmetic.** 11 `false` values for `demographics_disclosed` or
   `multi_site` came without a quote (the rule is null unless the text says so), and the model
   sums datasets (6,534 + 5,069 = 11,603) or rounds a bound (98.1% as 0.9817); all are rejected,
   which is the intended policy.

## Throughput

Median 93 s per document (p90 248 s), median 348 output tokens, generation median
8.3 tok/s (range 4.8–11.1). 153 documents = 5.9 hours of model time, about 21 hours wall
clock with rests, cool-downs and one stall while the Mac slept. Generation is the cost:
a 14B model on a base M4 runs at about 10 tok/s cool, and the fanless Air throttles to
5–6 tok/s after ~45 minutes of continuous load. Batches of 5 with 5-minute rests, a stop
below 6 tok/s and a 20-minute cool-down between sittings keep it at 7–10 tok/s; sittings must
keep the Mac awake through the cool-down (`caffeinate -i` around the whole loop), or it sleeps
and the loop stalls. Full scale (1,519 readable Summaries) would be about 70 hours of model
time on this machine — overnight runs over a week, which is why this spike covered the
cardiovascular/metabolic subset that feeds the mortality leads.

## What this means for 4D

- The quote check is the design, not a safeguard on the side: page derived from the quote,
  the status ladder above, and value-in-quote for every number and every CI bound. Every
  fabrication seen was rejected; what it cannot catch is a real number under the wrong name.
- Ask for evidence the document can give: values as written, measure names the document uses,
  dataset/subgroup as `context`, error measures (MAE, RMSE) named as such, one-sided bounds as
  one-sided, and no counts ("at most N") that invite padding.
- Cap output (`num_predict`) and skip a document after one failed retry, so a runaway
  generation cannot stall a run.
- Treat `clinical_endpoint = deterioration_risk / mortality / mace / hospitalisation` as a
  **curation lead**, never as `mortality_confirmed_flag` (ADR 0007): it found 9 of 11
  confirmed devices, flagged 6 of 120 negatives (4 defensible) and 2 of 22 unreviewed devices.
- Skip pages 4A's text layer cannot really read: add a cipher/garble test to the text class
  (failure mode 3) before any LLM sees the page.
- Metric names need the hand-check before any field ships; numbers and quotes are the more
  reliable part.

## Still to do (this finding is updated when they are)

- [x] Finish the 153 (done 2026-10-16).
- [ ] Hand-check 40–60 rows from `review-*.csv` (metric names, `ok_gapped` / `ok_numbers`
      rows, the endpoint disagreements) as the first gold set; report precision per field.
- [ ] Send the 4 defensible negatives (K230842, K233549, K081140, K183282) and the 2
      unreviewed risk devices (K231038, K172507) to the next curation batch.
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
