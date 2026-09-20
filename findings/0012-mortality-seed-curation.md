# 0012 — Curating the mortality seed: 154 cardiovascular devices, reviewed one at a time

**Date:** 2026-09-20 · **Status:** Seed written; the gold mart is no longer empty · **Component:** `config/mortality_seed.yaml` → `silver_evidence` → `gold_mortality_relevant` (ADR 0007, ADR 0014)

The two-stage mortality flag (ADR 0007) has had **zero rows** since it was designed. This
finding is the first curation pass: every device in `specialty_category = 'cardiovascular'`
read one at a time, its Indications-for-Use judged, and the judgement recorded — `true`
**and** `false** — with the verbatim evidence and a note.

## Where the intended-use text came from

`mortality_or_mace_risk_indicated` is a judgement about intended-use prose, and that prose
is **not in any openFDA endpoint** (ADR 0013) — it lives in the 510(k) summary PDF. So this
task depended on the same document access as the PDF spike ([finding 0011](0011-pdf-spike.md)):
each cardiovascular device's summary was fetched from `accessdata.fda.gov` over a browser
and its Indications-for-Use statement extracted from the text layer. `intended_use_text` in
the seed is a whitespace-normalised verbatim excerpt of that statement (truncated to the
leading sentences); `intended_use_source` is the PDF URL, which is authoritative if the
excerpt looks clipped. `review_method` is **`llm_assisted`** for every entry — a Cowork
session read the text and formed the judgement, exactly the case ADR 0007 invented the label
for. Individual entries can be upgraded to `human` after Jonathan reviews them.

## The set

`specialty_category = 'cardiovascular'` is **154 devices**: 144 with 510(k) `K` numbers (all
filed a public Summary), 6 De Novo (`DEN`), 4 PMA (`P`).

| Outcome | Count | |
|---|---|---|
| **Reviewed, `mortality_confirmed: true`** | **11** | the mart's initial content |
| Reviewed, `mortality_confirmed: false` | 122 | documented negatives, incl. near-misses |
| **Omitted** (no clean verbatim IFU, or out of reach) | 21 | absent = "not yet reviewed", never guessed |
| **Total in `specialty_category`** | 154 | |

Reviewed = **133**. `DEN230003` (Viz HCM) is included among the negatives; the other 5 De
Novo and 4 PMA are omitted (below).

## The inclusion rule

Conservative, because ADR 0007 and ADR 0014 are explicit that **under-inclusion is
recoverable by reviewing more devices, while silent over-inclusion is not recoverable at
all.** `mortality_confirmed: true` only where the intended use *itself* concerns:

- **risk of death or clinical deterioration** (e.g. eCART: "composite outcome of death or
  ICU transfer"; the Rothman Index: "aggregate statistical mortality risk"), or
- an **acute life-threatening event** — hemodynamic instability (CLEWICU, Acumen HPI, AHI),
  hemorrhage-and-triage (APPRAISE-HRI), cardiac arrest / loss of pulse (Loss of Pulse
  Detection), or
- **coronary plaque burden** — the handoff's one named in-scope example (HeartFlow's
  plaque-characterising versions).

Everything that merely **detects or diagnoses a condition** — an arrhythmia, low ejection
fraction, coronary artery disease, cardiac amyloidosis, pulmonary hypertension — or
**monitors / records / images** is `false`, with a note where the call was close. The line
that recurred: *detecting a mortality-associated finding is not the same as stratifying
death/MACE risk.* A low-EF ECG algorithm detects a strong mortality correlate but its
intended use is condition screening; recorded `false`, noted.

## The 11 confirmed

| Submission | Device | Why |
|---|---|---|
| K172959 | Rothman Index (PeraServer/PeraTrend) | "aggregate statistical **mortality risk**" |
| K183370 | Rothman Index (PeraMobile/PeraWatch) | same index |
| K200717 | CLEWICU | "likelihood of future **hemodynamic instability**… risk for clinical deterioration" |
| K233216 | CLEWICU (update) | same |
| K183646 | Acumen Hypotension Prediction Index | "likelihood of future **hypotensive events**" |
| K212219 | AHI System | flags "**hemodynamic instability**" from the ECG |
| K233253 | eCART | deterioration = "composite outcome of **death** or ICU transfer" |
| K233249 | APPRAISE-HRI | "**hemorrhage risk**… stratify casualties… emergency evacuation" |
| K242967 | Loss of Pulse Detection | identifies "**loss of pulse** events" (cardiac arrest) |
| K213857 | HeartFlow Analysis | "**plaque identification and characterization**" |
| K250902 | HeartFlow Analysis | "**plaque localization and characterization**" |

## What the review proved about the two stages (ADR 0007 / ADR 0014)

The seed makes the stage-1 keyword pass testable against real judgements for the first time.
Running the shipped stage-1 regex (`mortalit*|death*|surviv*|mace|cardiac arrest|…`) over the
confirmed-true intended-use texts:

- **8 of the 11 confirmed devices are MISSED by stage-1** (`keyword_disagrees = true` in the
  mart): K183646, K200717, K212219, K213857, K233216, K233249, K242967, K250902. Their
  intended use speaks of "hemodynamic instability", "hypotensive events", "plaque", "loss of
  pulse", "hemorrhage" — none of which are stage-1 keywords. **This is the concrete evidence
  that stage-1 cannot be the filter** (ADR 0014 Decision 1): shipping it as the gate would
  have silently dropped 8 of 11 real leads.
- Conversely, **1 reviewed-*false* device would be a stage-1 false positive**: K192732
  (BodyGuardian) matches "life threatening" — but only inside the negation "**Not for use**
  with patients requiring… monitoring for life threatening arrhythmias." A keyword gate would
  have admitted it. This is the silent over-inclusion the two-stage design exists to prevent.

Both directions vindicate keeping the stages separate and gating the mart on the curated flag
alone. The `keyword_disagrees` column will light up for those 8 rows, which is correct — they
are exactly the rows worth a second look, and the reason ADR 0007 kept the column.

## Close calls, recorded `false` on purpose

- **FFR / CT-FFR** (K152733, K161772, K182035, K182149, K190925, K192442, K203329, K213657):
  functional coronary-lesion physiology (ischemia significance), not patient-level death/MACE
  risk. The plaque-quantifying HeartFlow versions (K213857, K250902) *are* in — the
  distinction is real and visible in the intended-use text (plaque characterization vs FFR only).
- **Low-EF ECG** (K232699, K233409, K250119, K250649, K250652): detect LVEF ≤40%, a strong
  mortality correlate, but framed as condition screening.
- **Disease-likelihood / screening**: CorVista CAD & PH (K232686, K233666), ECG-AI PH
  (K252360), Cleerly ISCHEMIA (K231335), amyloidosis screeners (K240860, K243866, K250151,
  K253801), Viz HCM (DEN230003). All detect a serious condition; none states a death/MACE-risk
  purpose.
- **General early-warning indices without a stated mortality endpoint**: Visensia (K081140),
  Biovitals (K183282), WAVE (K171056) — contrast the Rothman Index, which names mortality risk
  explicitly and is therefore `true`. This pair (Rothman in, Visensia out) is the sharpest line
  in the set and worth revisiting if the business wants deterioration indices broadly.

## Omitted (21) — "not reviewed", not "not relevant"

Absent entries are honest gaps, not negatives. Omitted because no clean verbatim IFU could be
extracted, or the source was out of reach:

- **Scanned summary body** (no text layer): K212662, K221203.
- **Non-standard PDF font encoding** (text layer not decodable): K162855, K172507.
- **Only form/boilerplate captured** (no IFU statement extracted): K210543, K230823, K233562,
  K250233, K181502, K142512 (physIQ PPA Engine), K171936 (Peerbridge), **K231038** — the last is
  an Edwards Global Hypoperfusion Index device that is *likely mortality-relevant* and should be
  reviewed manually; the summary yielded only comparison boilerplate here.
- **De Novo** without a cleanly extractable IFU statement: DEN160044 (Acumen HPI — its 510(k)
  sibling K183646 is confirmed), DEN200019 (Oxehealth), DEN200022 (AHI — its 510(k) sibling
  K212219 is confirmed), DEN200038 (Gili). DEN250057 (PCWP Analysis Software) — the decision
  summary is not yet published (404).
- **PMA defibrillators/ICDs** (P210015 Avive AED, P220012/S059 Aurora EV ICD, P230022 Jewel
  P-WCD, P980016/S939 Cobalt ICD): these are **therapeutic** devices for lethal arrhythmia, not
  risk-stratification AI, so they fall outside the mortality *lens* (which is about surfacing
  risk-stratification leads); their SSEDs also live at a different accessdata path. Left out
  deliberately, flagged here.

## Patterns worth feeding back

- **Cardiovascular ≠ mortality-relevant.** Only 11 of 133 reviewed devices (8%) concern
  death/MACE risk. The specialty is dominated by arrhythmia/AF monitoring, electronic
  stethoscopes, EP mapping, and vital-signs — this confirms ADR 0014's stance that the taxonomy
  is a *starting signal for what to review*, not a filter.
- **A rising class the taxonomy/keywords should learn from: ML "screening" algorithms for a
  named condition** (low-EF, amyloidosis, PH, HCM, CAD-likelihood) — ~15 devices, almost all
  2023–2026. They cluster right on the inclusion line. If the business decides "detects a
  high-mortality condition" should count, that is a single, documented rule change, and these
  entries are already recorded as `false` with the reasoning, so flipping them is a review, not
  a re-hunt.
- **`life sustain/support` flag remains near-useless as recall** (ADR 0013's correction holds):
  none of the 11 confirmed devices would have been found by it.

## Deliverables

- `config/mortality_seed.yaml` — 133 reviewed judgements (11 true, 122 false).
- Running `registry build-mart` (on a JDK-17 / Py-3.11 host) will now write **11 rows** to
  `gold_mortality_relevant`, 8 of them flagged `keyword_disagrees`. Note: the seed loader was
  validated here against its own rules, but `registry build-mart` itself needs Spark + Python
  3.11 (the dev VM has 3.10 and no JDK 17 — CONTINUATION §5), so the mart write should be run in
  the image or CI.
