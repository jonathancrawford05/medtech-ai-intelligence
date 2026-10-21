# Handoff — labelling gold set v2 (PR 4D)

**For:** an independent agent (or person) labelling the values `registry draw-gold` drew
from the `4d.3` run, and the maintainer who reviews the labels afterwards. Gold v1 was made
the same way ([finding 0023](../../findings/0023-local-llm-extraction-spike.md), "Gold set,
first pass"). Why v2 exists, and what it decides: [finding 0024](../../findings/0024-llm-extraction-framework.md),
[ADR 0022](../adr/0022-llm-assisted-evidence-extraction.md) Decision 7.

## 1. Maintainer: prepare the inputs (Mac)

```bash
uv run registry draw-gold --prompt-version 4d.3 --out labelling/gold_set_v2.csv \
  --documents labelling/documents \
  --exclude-gold tests/fixtures/llm_extraction/gold_set_v1.csv \
  --exclude K172959 --exclude K183370 --exclude K183646 --exclude K212219 --exclude K233216 \
  --exclude K200717 --exclude K213857 --exclude K233249 --exclude K233253 --exclude K242967 \
  --exclude K250902 --exclude K243812 --exclude K182149 --exclude K183282 --exclude K172507 \
  --exclude K161772 --exclude K081140
```

Give the labeller **only** `labelling/gold_set_v2.blind.csv` and `labelling/documents/`.
Keep `gold_set_v2.csv` (the key) and `gold_set_v2.strata.csv` away from them: the key
names each row's check status and why it was sampled, and the labels are only worth
something if they are made blind to both. Then paste the prompt in §2.

## 2. The labeller's prompt (paste as is)

```text
You are labelling a gold set for an extraction pipeline. A language model read FDA 510(k)
Summary documents for AI medical devices and proposed facts, each with a quote. You decide,
against the full document text, whether each proposed value is right. You did not produce
these values and you have no stake in them.

INPUTS
- gold_set_v2.blind.csv: one row per proposed value. Read the columns submission_number,
  field, value, label, ci, quote, page_cited. Fill in the five empty columns: judgement,
  correct_value, evidence_page, evidence_quote, reason.
- documents/<submission_number>.json: the document's full text, page by page ("pages":
  [{page, text}]). The text is a PDF text layer: expect split words ("Noti fi cation"),
  stray spaces ("75 .9%"), and table rows run together.

HOW TO WORK
- Find the evidence yourself, in the full text. Do not trust the model's quote or its page:
  read the document around them and elsewhere, including tables and the Indications for Use
  form. The quote may be accurate and the value still wrong (wrong device, wrong measure,
  a target rather than a result).
- Judge each row on its own. Do not try to guess why a row was chosen or whether any
  automatic check accepted it; that is deliberately hidden from you.
- Never edit row, submission_number, field, value, label, ci, quote or page_cited; never
  add, delete or reorder rows. The sheet is merged back by row number and a changed value
  is refused.

JUDGEMENT (one of y, n, partly, unsure)
- y: the value is what the document says, for what the field asks.
- n: it is not. Give correct_value.
- partly: the core value is right but a secondary part is wrong (e.g. a metric's figure is
  right but its confidence interval or subgroup is wrong). Say which in reason.
- unsure: the document does not let you decide, or two readings are equally defensible.
  Say why in reason. Use this rather than guessing.

FIELDS — what "right" means
- metric:<measure>/<setting>  (e.g. metric:sensitivity/standalone). The row claims that
  the document reports this measure with this value (and, if ci is filled, this interval
  "low-high"; "None" means no bound on that side), as a result of the subject device's
  testing. Answer n when any of these holds:
    * the figure is not in the document, or is a different figure;
    * the measure is misnamed: a PPV is not specificity, a false-positive reduction is not
      specificity, agreement or concordance between methods is not accuracy, an error
      measure (mean absolute error, RMSE, ARMS, mean difference) is not accuracy;
    * the figure belongs to a predicate or reference device, or to the original device of a
      modified device, not the device the Summary is for;
    * the figure is a target, threshold, hypothesis or acceptance criterion, not an achieved
      result ("within ±3 BPM", "lower bound above 80%");
    * a one-sided interval was given a second, invented bound.
  A correct figure for a named subgroup or dataset is y (it is a real result); say the
  subgroup in reason. "other" as the measure is right when the document's measure is none
  of the named ones (e.g. a likelihood ratio). A value written on the other scale (0.95 for
  95%) is y if it is the same figure.
- performance_reported (true/false): true if the Summary reports quantitative performance
  from standalone device testing or a clinical/reader study; false if it reports none.
- study_design (retrospective / prospective / mixed): of the primary performance test.
  mixed when that testing combines retrospective and prospective data.
- sample_size: the number of patients, recordings, images, studies or cases in the primary
  performance test, as the document writes it. Not sites, readers or hospitals; not a sum of
  datasets the document does not state as a total. If there are several test sets with no
  primary one, answer unsure.
- multi_site (true/false): true if the test data came from more than one site/institution.
  false only if the document says so.
- demographics_disclosed (true/false): true if the document reports the test data by age,
  sex, race/ethnicity or similar subgroups. false only if it says they were not reported;
  if it is simply silent the right value is "not stated".
- clinical_endpoint: what the device's own output is, from its indication or intended use.
  Take the first that applies:
    1. mortality / mace / hospitalisation / deterioration_risk: the output is the patient's
       risk, likelihood or probability of an adverse outcome, or an index or score of that
       risk (mortality, mace or hospitalisation when it names that outcome).
    2. measurement_quantification: it measures a physical quantity of the body or an image
       (a dimension, volume, flow, pressure or pressure ratio such as FFR from CT or
       angiography), even if used to assess disease.
    3. detection_diagnosis: it finds, flags or classifies a condition present now.
    4. monitoring: it displays or tracks signals, trends or a change-from-baseline index,
       with none of the outputs above.
    5. triage_notification (prioritises a worklist, notifies a specialist), image_quality,
       other.
  A clinical trial's outcome measure, or "risk" used about the disease or the study rather
  than the output, does not make rule 1. A composite status index (several vitals, labs,
  assessments) is rule 1 only if the document ties it to risk, mortality or deterioration;
  otherwise rule 4. If the device's output is genuinely two of these, answer unsure and name
  both in reason.

FILLING THE OTHER COLUMNS
- correct_value (for n, and for partly when useful): start with exactly ONE value in the
  field's own vocabulary, then any explanation after a colon or in brackets, e.g.
    "mixed"   "measurement_quantification"   "375"   "False"
    "other: false positive reduction 81.0% (CARE-AF algorithm)"
    "ppv 93.6% (post-market study)"
    "not stated (the Summary reports no demographics)"
    "not a subject-device figure (predicate IRNF 2.0 specificity 99.6%)"
  Never "A or B": that is unsure. A program reads the first word.
- evidence_page: the page number (from the JSON) where the deciding text is.
- evidence_quote: that text, copied exactly from the page, short (one sentence or one table
  row). For y, quote the evidence even if it equals the model's quote.
- reason: one sentence: why, and anything a reviewer should check.

When you are done, report: rows labelled, counts of y / n / partly / unsure, and every row
you found hard, with its row number.
```

## 3. Maintainer: review, merge, score

Review every `n`, `partly` and `unsure`, plus about 8 random `y` rows, against the PDF
(v1's practice; record the review date). Then:

```bash
uv run registry merge-gold --key labelling/gold_set_v2.csv \
  --labelled labelling/gold_set_v2.blind.csv --out tests/fixtures/llm_extraction/gold_set_v2.csv
uv run registry score-gold --gold tests/fixtures/llm_extraction/gold_set_v2.csv \
  --prompt-version 4d.3 --bar 0.9 --rows
```

Commit the merged gold set (it carries no PDF text beyond short quotes) and the strata
file beside it. The bar per field, and the weighted population estimate from
`gold_set_v2.strata.csv`, go into finding 0024. This set is the test set: do not tune a
prompt on it. A `4d.4` would need a v3 draw.
