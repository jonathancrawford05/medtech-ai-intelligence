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

### Reading the rows (2026-10-19)

The per-document endpoints and every rejected or flagged row, from the raw answers:

- **The future-event rule works for the devices it describes**: HPI K183646, CLEWICU
  K200717 / K233216 and eCART K233253 are `deterioration_risk`. K212219 is also right
  in value, but its quote ("predicts a future episode of hemodynamic instability") is
  the **prompt's own wording** echoed back, so `quote_not_found` rejected it (failure
  mode 4, caught by the check).
- **It is too strict for risk indices.** Rothman K172959 ("based on the aggregate
  statistical mortality risk") and APPRAISE-HRI K233249 ("screen ... for hemorrhage
  risk") came out `detection_diagnosis`. Neither predicts an event within a stated
  window, yet each one's output *is* a patient's risk.
- **No endpoint at all for three** (`not_stated`): K183370, K213857, K250902. Only
  K183370 and K213857 were near-empty (146 output tokens), and the spike's answer for
  K213857 on the same five pages was as thin (168 tokens), so that document has little
  to give. K250902 answered in full (718 tokens) but left the endpoint unset. The
  context window is ruled out: the largest prompt in either run was 6,626 tokens of
  16,384 (`prompt_eval_count`), and no page was trimmed or garbled.
- **Check gaps, fixed offline in checks `4d.2`** (no model re-run; `build-evidence`
  re-applies them):
  - `ok_rescaled` accepted "an eCART of 93" as a rate of 0.93 (K233253, a threshold).
    Rescaling now needs a `%` on the quote's figure (0.95 vs "95%"), or a fraction for
    a percentage value (78 vs "0.78").
  - `measure_not_named` rejected "PPPV was 12% (95% CI: 11-14%)" filed as `ppv`
    (K233216). A three-letter acronym may now carry one qualifying letter in front.
  - The string `"null"` written as a quote now counts as no quote (K183646).
- **Rejections that were right**: a lead time filed as a mean difference (K200717); a
  summed sample size, 6,534 + 5,069 = 11,603 (K233216, the arithmetic the prompt
  forbids); HeartFlow's mean volume error filed as a mean difference (K250902); two
  `false`s without a quote (K213857).

### The six curated negatives the spike over-called (Mac, 2026-10-19)

`--only K243812 K182149 K183282 K172507 K161772 K081140`, the same digest and prompt.
All 6 answered `ok`. The run now holds 17 responses and 104 evidence rows (checks `4d.2`
re-applied with `build-evidence`).

Risk endpoints across the 17: K081140, K183646, K200717, K233216, K233253. Of the six
negatives, only **VISENSIA K081140** is called a risk output, and the gold set marks it
`unsure` (a patient status index). Volta K243812, FFRangio K182149, Biovitals K183282
and SOZO K172507 were the four the spike got wrong, and under `4d.1` none of them is a
risk call. So the future-event rule bought its precision on exactly the rows the gold
set said were wrong. What it cost on the confirmed side is Rothman, APPRAISE-HRI and,
through prompt leakage, K212219.

It is not consistent about status indices yet: it calls VISENSIA a risk output and the
Rothman Index (which says "mortality risk") a detection.

### Gold rows for the 17 (`score-gold --prompt-version 4d.1 --rows`, 2026-10-20)

13 gold rows fall on these documents (45 are for documents not run). On the 8 judged
endpoint rows the spike prompt was right on 1, with 6 false risk calls. `4d.1` is right
on 3, wrong on 2, abstains (`not_stated`) on 3, and makes no false risk call. The errors
left:

- **Measurement vs detection.** HeartFlow FFRct K161772 is called `detection_diagnosis`.
  HeartFlow K213857 / K250902 and Volta K243812 abstain.
- **Monitoring vs detection.** Biovitals K183282 is called `detection_diagnosis`.
- **Study design.** eCART K233253's "retrospective and prospective" is still
  `retrospective` (the spike's error; gold `mixed`).

The harness scored an abstention inside an answered document as `unjudged`. It is a miss,
and now scores `wrong` when the gold names a value.

**Prompt `4d.2`** (not yet run): the endpoint becomes an ordered decision. (1) A risk,
likelihood or probability of an adverse outcome, *or an index or score of that risk*,
with or without a time window. (2) A computed quantity, FFR from CT or angiography
included. (3) Detection now. (4) Monitoring, a change-from-baseline index included.
(5) Triage and image quality. It always picks one when there is an indication sentence,
and its rules carry no sentence the model can echo as a quote. Study design is `mixed`
when the testing combines both. Measure on the same 17 documents against `4d.1`.

### Prompt `4d.2` on the same 17 (Mac, 2026-10-20)

All 17 answered `ok`. Gold rows, `4d.1` → `4d.2`:

| | `4d.1` | `4d.2` |
|---|---|---|
| endpoint, judged rows: right / wrong / missed | 3 / 2 / 3 | **5 / 2 / 1** |
| study design right | 1 / 2 | **2 / 2** (eCART `mixed`) |
| false risk calls on the four fixed negatives | 0 | 0 |

The HeartFlow filings K213857 / K250902 are now `measurement_quantification` (right). The
errors left: Biovitals K183282 `measurement_quantification` (gold `monitoring`), HeartFlow
FFRct K161772 `detection_diagnosis` (gold measurement), Volta K243812 still abstains.
VISENSIA K081140 moved to `monitoring`, one of the gold set's two acceptable answers.

**The risk indices did not come back.** Rothman K172959 and K183370 became
`measurement_quantification`: rule 2's "computes a quantity" caught "compute a patient
status index". APPRAISE-HRI K233249 ("screen ... for hemorrhage risk") stayed
`detection_diagnosis`. K212219's value became `detection_diagnosis` with a real quote
("a frequently updated binary output ... to describe a patient's hemodynamic status"),
which is defensible: that output describes the present. Risk calls on the 17 are the same
four as under `4d.1`: HPI K183646, CLEWICU K200717 / K233216, eCART K233253.

`4d.2` gave more unquoted `false`s (`multi_site` 5, `demographics_disclosed` 3; all
rejected by `no_quote`), probably from "always choose a value" read beyond the endpoint.

The build summary pooled both prompt versions (each lead listed twice). It now prints one
summary per model and prompt.

**Prompt `4d.3`** (not yet run): rule 2 is one physical quantity, a composite index of
vitals, labs or assessments goes to rule 1 when tied to risk and to rule 4 otherwise, and
screening for the risk of an outcome counts for rule 1.

**Caveat: these rows are no longer a test set.** Gold v1 has now steered three prompt
versions on the same 9 endpoint rows. A prompt's score on them is a development
figure; the bar is judged only on a fresh draw from documents not used here.

### Prompt `4d.3` on the same 17 (Mac, 2026-10-21): where tuning stops

Judged gold endpoint rows: **6 right, 1 wrong, 1 abstained** (`4d.2`: 5 / 2 / 1). Biovitals
K183282 is now `monitoring` (right). HeartFlow FFRct K161772 is still `detection_diagnosis`
and Volta K243812 still abstains. No fixed negative went back to a risk call.

The risk indices still did not come back. Rothman K172959 is `monitoring`; its quote is
the "compute a patient status index" sentence, not the "mortality risk" one. The second
Rothman filing K183370 is `measurement_quantification`. APPRAISE-HRI K233249 is
`detection_diagnosis`, and K212219 is `measurement_quantification`. Risk calls are the
same four future-event predictors under `4d.1`, `4d.2` and `4d.3`.

**Reading.** Across three prompts the risk call is stable and precise for devices that
predict an event, and does not reach status or risk indices. Rule changes move which
non-risk label those get, not whether they are risk. That is a limit of this model and
prompt shape, so tuning on the 9 gold rows stops here. The indices are already in the
seed (both Rothman filings confirmed by a curator), which is where ADR 0022 Decision 8
sends risk endpoints anyway. A fresh draw decides whether the endpoint field ships,
probably as "risk-predictor flag only".

**`registry draw-gold`** (built for this). It samples one run's values by stratum with a
seed, from documents not in any `--exclude-gold` set, and includes rejected values,
which v1 never drew, so recall lost to the checks becomes measurable. It writes a key, a
blind labelling sheet without check status or sampling reason (shuffled), and per-stratum
population counts for weighting. `registry merge-gold` puts the labels back by row and
refuses a sheet whose values were edited. `extract-evidence --exclude-gold` keeps held-out
documents from costing model time.

Gold baseline under checks `4d.2`: unchanged (metrics 14 / 16, endpoint 7 / 14).

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
- [ ] Draw a **fresh** gold set from a `4d.3` run on documents outside v1 and the 17 (`registry draw-gold`) and score it:
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
