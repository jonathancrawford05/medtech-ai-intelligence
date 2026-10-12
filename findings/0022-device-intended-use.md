# 0022 — Device-level intended use (PR 4C): built, and measured at full scale

**Date:** 2026-10-11 · **Status:** Built; verified on the 21 recorded Summaries and the committed seed in a cloud session. **All three full-scale measures run on the maintainer's Mac 2026-10-12** (extractor `4c.1` over 4B `4b.2`) · **Component:** Issue 4 PR 4C, `transform/intended_use.py`, `mart/curation_candidates.py`, `mart/leads.py`, `lakehouse_report.py`, `registry extract-intended-use` / `curation-candidates` ([ADR 0021](../docs/adr/0021-device-intended-use.md))

The code was written in a cloud session that cannot reach `accessdata.fda.gov` or the
maintainer's lakehouse. So this finding separates what ran here, against committed
real text, from what only the Mac can measure. The full-scale column below is empty
on purpose: those numbers do not exist yet.

## Verified here (pytest against committed fixtures; Spark 4.0.1 / Delta 4.0.1 on JDK 21)

**The statement, on 21 real Summaries** (the 4A slice plus the 11 recorded PCCP
documents). `test_intended_use.py::TestTheRealSlice` pins basis, pages, first and last
words for each:

| Basis | Documents |
|---|---|
| `form_3881` | 12 — `K092116` (pre-3881 "Indications for Use:"), `K141480`, `K141922`, `K181892`, `K190013` (two forms, OTC + Rx, both kept), `K201992`, `K231010`, `K232699`, `K243239`, `K243449`, `K250177`, `K260406` |
| `form_estar` | 2 — `K250507`, `K252103` (4B classes the eSTAR page as Summary text) |
| `summary_section` | 5 — `K203469` (its form page is scanned), `K210484`, `K231837`, `K233692`, `K253984` (no form in the PDF) |
| `no_text` | 2 — `K003301` (scanned), `K241847` (letter readable, form and Summary scanned) |

**19 / 21 read: every document with a readable form or Summary.** That matches
finding 0019's "readable somewhere" rule on this slice.

**What the real text corrected while building:**

- `K250507`'s statement says "intended for over-the-counter (OTC) use"; a stop on
  "Over-The-Counter" cut it mid-sentence. Only the checkbox with its CFR cite closes a
  statement now.
- Leading checkbox glyphs ("I The Hypertension …") are trimmed; trailing ones ("… I Z").
- A section running to the next heading swept comparison text (`K233692`'s
  "Technology: … same technology as its predicate") and the next page's footer
  (`K231837`'s address). Field labels close a section, and a statement that ends its
  page stops there; one broken mid-sentence continues (`K203469`, `K231010`).

**Verbatim.** Every statement is a span of the normalised text of the page(s) it names
(each form part of its own page): asserted for all 19.

**No re-segmentation.** With `classify_page` and `segment` patched to raise, the pass
still reads `K243239` from its stored roles. The rebuilt page kinds equal 4B's
`segment` output on five documents. Told a page is Summary rather than form, the pass
reads it as Summary — the stored role decides. Roles from another fetch (sha mismatch)
are refused.

**The no-flood rule.** `test_leads.py::TestSummaryIntendedUse`: a statement full of
mortality language for every device, plus a content-identical rebuild, gives **no
movement and no leads**. Over the fixture pair (10 devices, 7 leads without
statements), statements for all 10 leave the unmoved `K100`/`K200` out; the only new
lead is `K600`, a removal with no other signal whose own statement now carries the
language. Structurally: no `silver_devices` field and no `differ.SOURCE_FIELDS` entry
names intended use. The Spark test runs `leads.run` with `silver_intended_use` on disk:
same seven leads, `K900`'s source `summary_intended_use`.

**Stage 1 on the curated devices, baseline.** Over the committed seed (133 curated:
11 confirmed, 122 negatives) on the curated excerpts, `keyword_agreement` reproduces
finding 0014 exactly: **0 / 11** `keyword_disagrees`, **1** negative flagged
(`K192732`). Four curated negatives are in the recorded slice (`K210484`, `K231010`,
`K232699`, `K250507`); on their own full statements stage 1 flags **0 of 4**, as on
their excerpts. None of the 21 statements trips stage 1 (none is a mortality device).

## Full scale — the maintainer runs this on the Mac

```bash
uv run registry extract-intended-use -v              # (1) coverage vs finding 0019
uv run registry inspect                              # (2) INTENDED USE: the source shift
uv run registry curation-candidates --csv candidates.csv   # (3) keyword_disagrees + the batch
```

| Measure | Baseline | 4C full scale |
|---|---|---|
| (1) Intended use found | finding 0019: readable in 1,537 / 1,541 (99.7%) | **1,515 / 1,541 (98.3%)**, 22 under the ceiling |
| (1) by basis | — | `form_3881` **914** · `summary_section` **497** · `form_estar` **104** · `not_found` **14** · `no_text` **12** |
| (2) `mortality_language_source` moves to `summary_intended_use`, every silver device as a lead (1,614) | before: 26 devices had a source | **89 move** (from `intended_use_text` 12, `device_name` 7, no source 70); **70 newly flagged**; 7 unchanged; 1,518 no source either way |
| (2) … over the leads already recorded | `gold_device_leads` (finding 0017: one snapshot) | **n/a**: no `gold_device_leads` table yet on the lakehouse (`monitor` has not recorded a pair) |
| (3) curated devices with a statement read | — | **132 / 133** |
| (3) `keyword_disagrees` on the 11 confirmed | 0 / 11 on the curated excerpt (finding 0014) | **0 / 11** on the device statement; every confirmed device has one |
| (3) negatives flagged, of 122 | 1 (`K192732`) on the curated excerpt | **2** on the device statement: `K192732`, **`K243349`** (new) |
| (3) curation candidates | — | **5** among 168 cardiometabolic devices (154 with a statement read); **28** after the scope and hemorrhage changes (5 `specialty` + 23 `statement`) |

### Measures (1) and (2), read (maintainer's Mac, 2026-10-12, `extract-intended-use -v` and `inspect`)

**Coverage: 1,515 / 1,541 (98.3%), 22 under finding 0019's ceiling.** The gap is two
different things:

- **`no_text` 12 (finding 0019: 4 unreadable).** 4B classes a page as unreadable when
  it is under 100 characters or under 50% letters, which is stricter than finding 0019's
  regex over raw text. So 8 documents where finding 0019 saw the wording somewhere have
  no form or Summary page that 4B accepted. This pass inherits 4B's page roles by
  design (ADR 0021 Decision 1). Recovering those 8 would mean changing 4B's floor, not
  this pass.
- **`not_found` 14.** These have readable form or Summary pages but no statement under
  any label this pass knows. This is the visible edge of the "labels" assumption below.

The form carries the statement in 1,018 documents (914 on the 3881, 104 on the eSTAR),
66% of what was read. The Summary section covers the other 497.

**The source shift: 89 of 1,614 devices move to their own statement.** Before 4C, 26
devices had any `mortality_language` source: 19 move and 7 keep theirs. The 7 are
devices with no statement, or whose statement has no stage-1 term while their name or
definition does. The other **70 are newly flagged**: their name, product-code definition
and curated text never hit, and their statement does. That raises the share of devices
carrying the signal from 26 to 96 (1.6% → 5.9%).

**What the 70 are (maintainer's Mac, 2026-10-12, the snippet in How to re-check).** 63 are
radiology devices. Two stage-1 terms account for 63 of the 70:

| Term | Devices | What they are | Reading |
|---|---|---|---|
| `hemorrhag…` | **44**, all radiology | Intracranial-hemorrhage / subdural triage (Viz ICH, Rapid ICH, BriefCase, qER, CINA, Brainomix 360 Triage ICH, JLK-ICH, …) and stroke ASPECTS tools whose statement excludes "hemorrhagic" cases | **Noise for this purpose.** They detect a bleed; they do not stratify mortality risk. Finding 0012's rule records detect/diagnose devices as negatives. The term was added in finding 0014 for `K233249` (hemorrhage *risk*), and it matches every bleed detector. |
| `plaque…` | **19**: 18 radiology, 1 dental | Coronary plaque / calcium quantification (Cleerly Labs ×3, CardIQ Suite ×3, AVIEW CAC ×2, HealthCCS ×3, Autoplaque, TeraRecon Calcification, Salix Central, cvi42, Careverse CoronaryDoc, uOmnispace.CT, BriefCase-Quantification); `K260082` TRIOS Dx is **dental** plaque | **Real signal, mostly.** Finding 0012 put coronary plaque quantification in scope (HeartFlow `K213857`/`K250902` are confirmed). These are the "cardiovascular-risk tool cleared through Radiology" ADR 0016 warned about. The curation report misses them because it is scoped to cardiometabolic *specialty*. The dental hit is a false positive. |
| `survival` | 4, metabolic | Tissue of Origin tests (the 4 non-`K231038` curation candidates) | Noise |
| `mace` | 1, neurology | `K143643` Brainscope Ahead 200 | Almost certainly the Military Acute Concussion Evaluation, not major adverse cardiac events. Noise. |
| `death` | 1, radiology | `K201103` Xeleris V | Unread; likely incidental |
| `life threatening` | 1, cardiovascular | `K243349` BodyGuardian Remote Monitoring | The curated negative newly flagged by measure (3). An arrhythmia monitor whose statement mentions life-threatening arrhythmias. Detects rather than stratifies, so the curator's `false` stands. |

**So `mortality_language` got broader, but not better, in proportion.** About 18 of the
70 new flags are devices curation would want to read: the coronary plaque tools. About
50 are noise, 44 of them from the hemorrhage term. Stage 1 never filters the mart
(ADR 0014), so nothing here is wrong. But in the leads, ICH triage would now be the
largest group tagged `mortality_language`. **Both follow-ups were folded into this PR
(2026-10-12, at the maintainer's request):**

1. **The hemorrhage term is narrowed to a risk frame:** `h(?:ae|e)morrhag\w* risk |
   risk (?:of|for) h(?:ae|e)morrhag\w*`. The evidence is the same kind finding 0014
   used. Across the 133 curated excerpts, the bare term hit exactly one device,
   `K233249`, and only as "hemorrhage risk". That device still matches; the
   ICH-detection phrasings do not (`test_mortality_seed.py`). The finding 0014 baseline
   on the committed seed still holds: 0 / 11 confirmed missed, 1 negative (`K192732`)
   flagged.
2. **The curation report now scopes by language as well as specialty.** A device from
   any specialty is in scope when its statement names the heart (coronary, cardiac,
   cardio…, heart, myocard…, atheroscler…), and each row says why (`scope`:
   `specialty` | `statement`). The stage-1 hit is still required. So the coronary-plaque
   tools come in, while dental plaque and head-CT hemorrhage tools stay out
   (`test_curation_candidates.py`).

**Measures (2) and (3) above were taken before this change.** The candidates have
been re-run (below). The `inspect` source shift has not; the expectation is that newly
flagged drops from 70 to about 26 as the 44 hemorrhage hits go.

### The candidates after the change (maintainer's Mac, 2026-10-12, `candidates.csv`)

There are **28 candidates, against the 5 before.** I had expected about 23; the
difference is 5 devices.

- **`specialty` 5:** the same five as before. `K231038` (GHI) and the four Tissue of
  Origin tests, whose statements say they are *not* intended "to predict disease course
  or survival". The "survival" hit is a negation; curating them as negatives retires
  them.
- **`statement` 23:**
  - **22 coronary plaque / calcium tools.** These are the 18 plaque tools from the 70,
    plus 4 whose *name* already said "Plaque" and so were not "newly flagged": DEEPVESSEL
    Plaque `K260497`, cvi42 Coronary Plaque `K251027`, Salix Coronary Plaque `K251837`
    and CaRi-Plaque `K242240`. All are in scope by finding 0012's plaque rule. The
    strongest for the seed are the two AVIEW CAC clearances (`K243696`, `K233211`): "risk
    stratification based on calcium score, gender, and age". Next come HealthCCS /
    HealthCCSng and BriefCase-Quantification: "calcified plaques …, which present a risk
    for coronary artery disease".
  - **`K201103` Xeleris V:** noise. It hits "death" on "Brain death" and is in scope
    because its Alcyone CFR module names "Coronary" / "Myocardial".
- **Out, as designed:** dental `K260082` (no heart language), and every
  intracranial-hemorrhage tool (the narrowed term).

**Extraction quality, seen in the batch.** All of these statements are verbatim, but
three are noisy in ways a follow-up could tighten:

- **`K260497` (eSTAR):** the text layer puts the form's field prompts before the
  statement. The eSTAR span between "Please provide your Indications for Use below."
  and "Please select the types of uses" is therefore empty, so the extractor fell back
  to the Summary section. That section carries the prompts and "He lp Te xt" debris.
- **`K243038`:** a 3881 form whose number field reads "Submission Number (if known)" and
  whose page lacks "FORM FDA 3881". 4B classed the page as Summary, so the whole form,
  boilerplate included, came through as `summary_section`.
- **`K231038`, `K080896`:** the statement breaks across a page without a full stop, so
  the next page's opening came along (by design, for `K203469`-style breaks).

### Measure (3), read (maintainer's Mac, 2026-10-12, `registry curation-candidates --csv candidates.csv`)

**Stage 1 holds on the full statements.** The confirmed devices are still all caught
(0 / 11 `keyword_disagrees`), and the cost of more text on the 122 negatives is one
extra flag, `K243349` (BodyGuardian): its statement mentions "life threatening"
arrhythmias. That is the caveat reading ADR 0021 anticipated, and the curator's `false`
stands.

**The 5 candidates:**

| Submission | Decided | Category | Terms | Source | Reading |
|---|---|---|---|---|---|
| `K231038` | 2023-07-26 | cardiovascular | hypoperfusion, hemodynamic instability, deterioration, hypotension | `summary_section` p5–6 | Global Hypoperfusion Index (Edwards). Finding 0012 **omitted** it for want of a clean statement and flagged it for manual review; 4C now supplies the statement. The one real candidate. |
| `K173839` | 2018-03-15 | metabolic | survival | `form_3881` p3 | Tissue of Origin test (Cancer Genetics) |
| `K120489` | 2012-05-17 | metabolic | survival | `form_3881` p11 | Pathwork Tissue of Origin, FFPE |
| `K092967` | 2010-06-08 | metabolic | survival | `form_3881` p17 | Pathwork Tissue of Origin, FFPE |
| `K080896` | 2008-07-30 | metabolic | survival | `summary_section` p2–3 | Pathwork Tissue of Origin, fresh-frozen |

The four Tissue of Origin tests are the same family. They are "metabolic" only because
their panel is Clinical Chemistry (`config/specialty_taxonomy.yaml`), and "survival"
is the only term they hit. They are probably not mortality-risk devices, but a
curator should confirm that and record them as negatives, so that they stop
reappearing as candidates.

**Assumed, not yet checked:** that the other 20 devices finding 0012 omitted are absent
from the list because their statements trip no stage-1 term, not because no statement
was read. 14 of the 168 in-scope devices have no statement; the candidates CSV does not
list them.

Before the next curation batch, read a sample of the candidates and of any newly flagged negatives.

## Assumed

- The statement sits under the labels the slice shows (3881 "(Describe)", the pre-3881
  colon, the eSTAR prompt, Summary headings). A Summary that states intended use only
  in prose under another heading reads `not_found`; the full-scale `not_found` count is
  the visible edge of that.
- The 40-letter floor and the 2,500-character cap come from the slice, not full scale.
- More text means more stage-1 hits; the leads are recall-oriented (ADR 0016), and
  measure (3) shows the cost on curated negatives.

## How to re-check

The matched terms over the 70 newly flagged devices (Mac; one-off, not pipeline code):

```python
from collections import Counter
from registry import tables
from registry.config.settings import get_settings
from registry.mart import leads
from registry.spark_session import get_spark
from registry.transform.mortality_seed import keyword_hits

s = get_settings()
spark = get_spark(s)
ctx = leads.LeadContext.load(spark, s)
rows = [
    r.asDict()
    for r in tables.read_table(spark, s, "silver_devices")
    .select("submission_number", "device_name", "specialty_category")
    .collect()
]
new = [
    r
    for r in rows
    if leads._language_source(r, ctx.without_summary_text()) is None
    and leads._language_source(r, ctx) == "summary_intended_use"
]
print(len(new), Counter(r["specialty_category"] for r in new))
print(
    Counter(t for r in new for t in keyword_hits(ctx.summary_intended_use[r["submission_number"]]))
)
```

```bash
uv run pytest tests/test_intended_use.py tests/test_curation_candidates.py \
  tests/test_leads.py -m "not live_network"
uv run registry extract-intended-use -v && uv run registry inspect   # Mac
uv run registry curation-candidates                                  # Mac
```
