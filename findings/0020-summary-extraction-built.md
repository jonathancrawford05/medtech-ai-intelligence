# 0020 — Summary extraction (PR 4B): built and verified on the real slice; full-scale yields open

**Date:** 2026-10-11 · **Status:** Built; verified on the real slice, on finding 0019's recorded PCCP cases, and **at full scale** (maintainer's Mac, 2026-10-11) · **Component:** Issue 4 PR 4B, `transform/document_extraction.py`, `transform/predicate_dates.py`, `registry extract-documents` / `enrich-predicates` ([ADR 0019](../docs/adr/0019-deterministic-summary-extraction.md))

The code was written in a cloud session that **cannot reach `accessdata.fda.gov`,
`api.fda.gov`, or the maintainer's lakehouse**. So this finding separates what ran
here, against committed real text, from what only the Mac can measure. Nothing in
the "full scale" column below is filled in: those numbers do not exist yet.

## Verified here (pytest against committed fixtures; Spark 4.0.1 / Delta 4.0.1 on JDK 21)

**Predicates, on the 4A slice (real text).** Every predicate list 4A recorded from
the spike is reproduced exactly, in order, with the primary:

| Document | Predicates | Primary (basis) | Note |
|---|---|---|---|
| `K181892` | 8, all `table` | `K151212` (`stated_primary`) | the Summary says "is the primary predicate device" |
| `K141922` | `K113696`, `K122938` | `K113696` (`first_in_section`) | |
| `K190013` | `K162532` | `K162532` | reference device `K150910` excluded; `K141273`, `K162225`, `K150214`, `DEN140038` (cited for history) excluded |
| `K250177` | `K242020` | `K242020` | |
| `K243239` | `K230085` | `K230085` | |
| `K092116` | `K033840` | `K033840` | old layout: Summary first, FDA letter on pages 5–6 |
| `K203469` | `K141248` | `K141248` | printed `Kl41248`; reference `K192377` printed `KI92377` |
| `K141480`, `K241847`, `K003301` | none | — | font-garbled, scanned Summary, scanned document |

7 of 10 slice documents yield a predicate; the other 3 have no readable predicate
number at all. That is 7/7 of the documents where one is printed in the text layer.

**The raw-layer trap is real and tested.** On the raw `K203469` layer, `K192377`
does not exist (`KI92377`), and the extractor run page-for-page on raw text finds
`K141248` only from page 6's closing sentence, not under its page-5 label.
`K092116`'s own number reads `K0921 16`. Both are fixed by `normalise`, and both
are asserted.

**Segmentation.** FDA-letter / 3881-form / Summary page roles match a reading by eye
on all seven multi-part documents, including `K092116`'s letter-last layout and
`K190013`'s image letterhead. `K141480`'s Summary pages (`!"!  "#""`) are classed
`unreadable` by the letter-share floor rather than mined for numbers.

**Cybersecurity.** `K190013` (page 11) and `K243239` (page 8) carry the clause 4A
recorded; 6 readable documents read `no_mention` (`False`); `K003301` and `K241847`
read `None` (no readable Summary page). Negations (`Cybersecurity: N/A`, "is not
applicable", "No cybersecurity …") are synthetic tests; K190013's "no new questions
with regards to software or cybersecurity" correctly does **not** read as a negation.

**PCCP.** No slice document carries a PCCP (all are pre-2025 or not PCCP clearances):
9 read `no_mention` → `False`, `K003301` reads `no_text` → `None`. The rule itself is
tested on **synthetic** text built from finding 0019's verbatim wording: the letter
sentence (also glyph-split), a correction letter that quotes the sentence it
retracts (→ `False`), the four negation shapes, the inherited `K234009` shape, and
two Summary positives.

**Silver join and the monitor.** `build-silver` copies the primary predicate, its age,
`has_pccp` and the cybersecurity flag from the extraction map; a submission absent
from the map stays `None`. A silver diff where only these fields change is **not
movement** (test in `test_monitor_differ.py`), so the first post-extraction build
records no leads.

### Slice yields (`summarise` over the ten fixtures)

```text
documents extracted            : 10
predicate found                : 7 / 10 (70.0%)
  primary chosen by             : first_in_section 6, stated_primary 1
  predicate numbers by method   : section 7, table 8
cybersecurity present          : 2 / 10 (20.0%)
  by basis                      : mention 2, no_mention 6, no_text 2
  no_mention in a mixed document: 1
has_pccp True                  : 0 / 10 (0.0%)
  by method                     : no_mention 9, no_text 1
```

## Verified at full scale (maintainer's Mac, 2026-10-11, extractor `4b.1`)

The maintainer ran `uv run pytest -m live_network -k pccp` against the full
`bronze_summary_documents`. The run recorded finding 0019's eight named cases and
three clearance-letter positives the extractor picked (`K231010`, `K250507`,
`K260406`), and wrote the full-scale yields into
`tests/fixtures/summary_documents/pccp/manifest.json`:

| Measure | Spike (60) | Finding 0019 (1,541) | 4B full scale |
|---|---|---|---|
| Predicate found | 57 / 60 (95%) | ≤ 1,508 (97.9%), an upper-bound proxy | **1,486 (96.4%)**, 22 under the ceiling |
| Primary chosen by | — | — | `first_in_section` 1,052 · `stated_primary` 324 · `first_in_sentence` 110 |
| Predicate numbers by method | — | — | `section` 1,481 · `sentence` 545 · `table` 237 |
| Cybersecurity present | 8 / 60 (13%) | 471 mentioned (30.6%) | **471 (30.6%)**; 0 negated, 1,048 `no_mention`, 22 `no_text` |
| `no_mention` in a `mixed` doc | — | — | **300** (the ADR 0019 Decision 5 gap) |
| `clearance_letter` (True) | 0 / 60 | 68 | **68**, exactly |
| `correction_letter` (False) | — | 2 | **2** (`K231837`, `K233692`) |
| `summary_negation` (False) | — | 1 | **1** (`K253984`) |
| `summary_statement` (True) | — | 4 | 6 under `4b.1`, **5 under `4b.2`** (see below) |
| `summary_inherited` (False) | — | 1 | 0 under `4b.1`, **1 under `4b.2`** (`K252103`) |
| `no_text` (None) | — | — | 3 |

**What the real text corrected.** In CI, `4b.1` read `K252103` as a positive. The
Summary names the plan as "cleared as part of K234009", then refers back to it as
"this PCCP" and "in accordance with the PCCP", and `4b.1` counted those later
mentions as new positives. `4b.2` reads a positive that follows an inherited mention
as a reference to that same plan. All eleven recorded PCCP documents now pass. The
change can affect only a document that has an inherited mention, so among the
full-scale `4b.1` positives only `K252103` and `K260714` could move. The four
recorded positives are pinned by tests and did not move.

**One new positive: `K260714`.** It reads `summary_statement` and is not among
finding 0019's 76. The likely cause is that it says "PCCP" without the spelled-out
phrase finding 0019 searched for. Nobody has read it yet, and it has no letter
sentence. It is the visible edge of the gap finding 0019 named. Re-running
`registry extract-documents` (now `4b.2`) gives the final count.

**Cybersecurity presence equals the mention count.** At full scale no document
negates its only mention, so the presence rule and finding 0019's mention count
agree: 471.

## Still open

1. **Predicate decision dates at scale.** The maintainer reports `enrich-predicates`
   and `build-silver` ran. `registry inspect` shows the `predicate_age_days`
   coverage, and it is not recorded here yet.
2. **`K260714`.** Read the stored text to confirm it is a real Summary positive.
   If it is, add it to the recorded PCCP cases.
3. **Predicate precision.** The figure above is recall only; a sample audit is
   still owed.

## Assumed

- Finding 0019's letter sentence is the FDA's standard wording. A different wording,
  or a scanned letter page, reads `no_mention`.
- A predicate label with no following heading runs at most 1,500 characters; a
  three-number section is a table. Both come from the slice, not from full scale.
- The `section` window can catch a non-predicate number printed between a predicate
  label and the next heading. Precision has not been measured at full scale; a
  sample audit belongs in the full-scale write-up.

## How to re-check

```bash
uv run pytest tests/test_document_extraction.py -m "not live_network"   # slice + synthetic
uv run pytest -m live_network -k pccp      # Mac: records the PCCP cases + manifest
uv run registry extract-documents -v       # Mac: full-scale yields (table above)
uv run registry enrich-predicates -v       # Mac: predicate dates via openFDA
uv run registry build-silver && uv run registry inspect
```
