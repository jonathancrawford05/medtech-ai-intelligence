# Handoff — Cowork session: PDF spike + curation

**Written:** 2026-09-15 · **For:** a Claude Cowork session with the lakehouse on disk
and a web browser · **Blocks:** roadmap Issue 4 (PDF pass) and Phase 3 (gold mart)

You have two things this repo's other session cannot do: **network access to
`fda.gov` / `accessdata.fda.gov`**, and **a browser to read device pages**. Both
tasks below exist because of that. Everything here is deliberately scoped to
producing **data files**, not code — the gold mart that consumes them is being
built in parallel, and keeping your output to `config/` and `findings/` means the
two branches merge without conflict.

**Read first:** `CLAUDE.md`, then `CONTINUATION.md`, then
[ADR 0007](../adr/0007-two-stage-mortality-flag.md) and
[ADR 0013](../adr/0013-openfda-enrichment-architecture.md).

**Work on branch `cowork/spike-and-curation`.** Do not edit anything under
`src/` — if a task seems to need a code change, stop and say so rather than
making it.

---

## Task A — the PDF spike (measurement, not a pipeline)

### Why this exists

`silver_device_enrichment.statement_or_summary` says **1,541 of 1,614 devices
(95.5%) filed a public 510(k) Summary**, so a document pass could in principle
reach almost the whole registry. What nobody knows is whether those PDFs carry a
**real text layer** or are **scanned images**. That single ratio decides whether
predicate extraction is a regex over formulaic sentences (days of work) or an OCR
pipeline (a different project with a different budget).

**Your deliverable is that number, not a fetcher.** Do not build a production
acquisition pipeline. Do not write anything into `src/`.

### What to measure

1. **The URL pattern.** 510(k) summaries are believed to live at
   `https://www.accessdata.fda.gov/cdrh_docs/pdf<NN>/<K-number>.pdf`, where `NN`
   is derived from the K-number's year prefix (e.g. `K253628` → `pdf25`). **This
   is unverified — confirm it, do not assume it.** Report what actually works,
   including any redirect or alternate path you find. If the pattern differs for
   older filings, that is a finding in itself.
2. **Fetch success rate** on the sample.
3. **Text layer vs scanned image.** Extract text per page and count characters. A
   page yielding under ~100 characters is almost certainly an image. Report the
   per-document ratio and classify each document `text` / `image` / `mixed`.
4. **Does the predicate sentence match a simple pattern?** In text documents,
   find how the predicate is stated. Report the **actual sentences**, verbatim,
   not your summary of them — the point is to judge whether a regex would work,
   and that judgement needs the raw strings.
5. **Do PCCP and cybersecurity appear as section headings?** Same treatment:
   report what the headings literally say.

### Sampling

Draw **60 devices, Summary-only**, stratified across decision year — the registry
runs 1995–2026 and older filings are far likelier to be scans, so an unstratified
sample drawn from a 95%-recent population will tell you nothing about the tail.
Suggested bands: pre-2015, 2015–2018, 2019–2021, 2022–2023, 2024, 2025–2026.

```python
from registry import tables
from registry.config.settings import get_settings
from registry.spark_session import get_spark
from pyspark.sql import functions as F

settings = get_settings()
spark = get_spark(settings)
silver = tables.read_table(spark, settings, "silver_devices")
enr = tables.read_table(spark, settings, "silver_device_enrichment")

candidates = (
    silver.join(enr, "submission_number")
    .filter(F.col("statement_or_summary") == "Summary")
    .select("submission_number", "decision_date", "device_name", "pathway")
)
```

### Being a good citizen

`accessdata.fda.gov` is a public-sector site with no published rate limit. **Throttle
to roughly one request per second, set a descriptive User-Agent, and stop
immediately on any 429 or 403.** Sixty documents at 1/s is a minute — there is no
reason to go faster, and getting the source's operator annoyed would cost this
project the entire Issue 4 option.

### Deliverables

- `findings/0011-pdf-spike.md` — the numbers, the verbatim sentences, and a plain
  recommendation: **regex-viable, OCR-needed, or mixed with a split.**
- `findings/data/pdf-spike-sample.csv` — one row per sampled device:
  `submission_number, decision_date, url_tried, http_status, pages, chars_total,
  chars_per_page, classification, predicate_sentence_found, notes`.
- **Do not commit the PDFs.** Cache them outside the repo. If two or three make
  especially good regression fixtures, say so in the finding and we will decide
  deliberately.

### What would make this spike a failure

Reporting "extraction looks feasible" without the character counts behind it. The
whole value here is a number someone can act on — a qualitative impression is
what we already have.

---

## Task B — curation

Two curated data files. Both are **hand-maintained config**, which in this repo
means they are judgement recorded honestly, not output generated at scale.

### B1 — company aliases: the GE problem

The live run resolves three GE entities separately:

| Resolved name | Authorisations |
|---|---|
| `GE HealthCare` | 42 |
| `GE Medical Systems Ultrasound and Primary Care Diagnostics` | 22 |
| `GE Medical Systems SCS` | 12 |

One parent, 76 authorisations, split three ways — which distorts any "top
applicants" view. `config/company_aliases.yaml` already has a `GE HealthCare`
entry listing `GE Medical Systems` as an alias; matching is **exact on a
normalised form**, so the longer names do not hit it.

**Add the full names as explicit aliases.** Do **not** change the matching logic
to do prefix or fuzzy matching — that is code, it lives in
`src/registry/transform/company_resolution.py`, and it would need an ADR. (See
[finding 0009](../../findings/0009-first-full-scale-silver-run.md) for why fuzzy
matching was already rejected once on the taxonomy side: it silently swallows
genuinely new values.)

Then sweep the rest of the tail the same way — `registry inspect` prints the top
resolved companies, and `CompanyLookup.unmatched_applicants` records every miss.
Where you are **not confident** two names are the same legal parent, leave them
separate and note it. An incorrect merge is invisible and permanent; an unmerged
pair is obvious and fixable.

### B2 — the mortality seed

This is the registry's reason for existing: surfacing mortality-relevant
cardiovascular/metabolic risk-stratification AI as underwriting leads. The
mechanism exists (ADR 0007) and has **zero rows**.

**Scope: the 154 devices in `specialty_category = 'cardiovascular'`.** Small
enough to review one at a time, which is the point.

```python
silver.filter(F.col("specialty_category") == "cardiovascular").select(
    "submission_number", "device_name", "applicant_resolved", "decision_date", "product_code"
).orderBy("decision_date", ascending=False).show(200, truncate=False)
```

Produce **`config/mortality_seed.yaml`** in exactly this shape:

```yaml
version: 1
# Stage 2 of ADR 0007. Every entry is a judgement about whether the device's
# intended use concerns mortality or MACE risk stratification.
reviewed:
  - submission_number: K243456
    mortality_confirmed: true
    review_method: llm_assisted
    intended_use_text: >-
      Verbatim intended-use statement, as published. Not a paraphrase.
    intended_use_source: https://www.accessdata.fda.gov/...
    notes: >-
      Why this judgement. Name the specific thing that decided it.
```

Field rules, all load-bearing:

- **`review_method` must be `llm_assisted` unless a person read the entry.** A
  Cowork session reading intended-use text and forming a judgement *is*
  `llm_assisted` — that is precisely the case ADR 0007 invented the label for.
  Recording it as `human` would put the registry's most consequential column in a
  state where nobody can tell how it was decided, which is the exact failure that
  ADR exists to prevent. If Jonathan personally reviews some entries afterwards,
  those can be upgraded to `human` then.
- **`intended_use_text` is verbatim.** It is evidence. A paraphrase is your
  reasoning, and that belongs in `notes`.
- **`intended_use_source` is required** — a URL someone else can open and check.
- **`mortality_confirmed: false` is a real, useful answer** and should be recorded
  with the same care as `true`. "Reviewed and judged not relevant" is a different
  and more valuable claim than silence. Do not omit the negatives.
- **Omit anything you cannot decide.** An absent entry means "not yet reviewed",
  which is honest. A guess recorded as a decision is not recoverable later.

The judgement itself: does the device's intended use concern **predicting,
stratifying, or detecting risk of death or a major adverse cardiac event**? A
coronary plaque-burden quantifier feeding cardiac risk assessment is in scope. A
tool that only improves image contrast is not, even on cardiac images. When it
sits on the line, record `false` with a note saying why — a documented near-miss
is useful to whoever revisits this.

### Deliverables

- `config/company_aliases.yaml` — extended, with the GE entities merged.
- `config/mortality_seed.yaml` — new.
- `findings/0012-mortality-seed-curation.md` — how many reviewed, the
  true/false split, which calls were close and why, and any pattern you noticed
  that the curated taxonomy or alias table should learn from.

---

## Ground rules for both tasks

1. **Branch `cowork/spike-and-curation`.** Open a PR; do not merge it yourself.
2. **No edits under `src/`.** If something seems to require one, stop and report it.
3. `make lint` must pass. You are adding YAML and Markdown, so this should be free.
4. **Update `CONTINUATION.md` §1/§4 before you finish**, and add your findings
   notes. That file is the only thing that survives a context window.
5. **Report what you actually did, including what did not work.** This project has
   twice found real bugs by execution that a green test suite missed, and both
   times the finding came from someone writing down a number that looked wrong
   rather than rounding it off.
