# 0020 — Summary extraction (PR 4B): built and verified on the real slice; full-scale yields open

**Date:** 2026-10-11 · **Status:** Built; verified on the ten-document real slice and synthetic PCCP cases; **PCCP fixtures and full-scale yields open (Mac)** · **Component:** Issue 4 PR 4B, `transform/document_extraction.py`, `transform/predicate_dates.py`, `registry extract-documents` / `enrich-predicates` ([ADR 0019](../docs/adr/0019-deterministic-summary-extraction.md))

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

## Not verified — needs the Mac

1. **The PCCP fixtures.** `uv run pytest -m live_network -k pccp` records finding
   0019's eight named cases (`K231837`, `K233692`, `K253984`, `K252103`, `K210484`,
   `K232699`, `K243449`, `K201992`) and three clearance-letter positives it picks
   from the local full bronze, into `tests/fixtures/summary_documents/pccp/`. Its
   manifest also carries the full-scale yields. Until committed, the
   `TestRecordedPccpCases` assertions skip. **They may fail on first recording** —
   the Summary-body wording of the eight named cases has never been seen by this
   session — and a failure there is the point: it is where the rule meets real text.
2. **Full-scale yields** — `uv run registry extract-documents -v` prints them:

| Measure | Spike (60) | Finding 0019 (1,541, proxy) | 4B full scale |
|---|---|---|---|
| Predicate found | 57 / 60 (95%) | ≤ 1,508 (97.9%) — upper bound | _pending_ |
| Cybersecurity present | 8 / 60 (13%) | 471 mentioned (30.6%) | _pending_ (presence ≤ mentions) |
| `no_mention` in a `mixed` doc | — | — | _pending_ (the Decision 5 gap) |
| `has_pccp` = True, `clearance_letter` | 0 / 60 | 68 | _pending_ (expect 68) |
| `correction_letter` | — | 2 | _pending_ (expect 2) |
| `summary_statement` | — | 4 | _pending_ (expect 4) |
| `summary_inherited` / `summary_negation` | — | 1 / 1 | _pending_ |

3. **Predicate decision dates.** `uv run registry enrich-predicates -v` (api.fda.gov),
   then `uv run registry build-silver` and `uv run registry inspect`: the SILVER
   section then reports `predicate_age_days` coverage.

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
