# 0022 — Device-level intended use (PR 4C): built and verified on the real slice; full-scale measures open

**Date:** 2026-10-11 · **Status:** Built; verified on the 21 recorded Summaries and the committed seed in a cloud session. **The three full-scale measures need a run on the maintainer's Mac** (the lakehouse lives there) · **Component:** Issue 4 PR 4C, `transform/intended_use.py`, `mart/curation_candidates.py`, `mart/leads.py`, `lakehouse_report.py`, `registry extract-intended-use` / `curation-candidates` ([ADR 0021](../docs/adr/0021-device-intended-use.md))

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
| (1) Intended use found | finding 0019: readable in 1,537 / 1,541 (99.7%) | _pending_ |
| (1) by basis | — | _pending_ (`form_3881` / `form_estar` / `summary_section` / `not_found` / `no_text`) |
| (2) `mortality_language_source` moves to `summary_intended_use`, every silver device as a lead | before: device text did not exist | _pending_ (and from which source; how many newly flagged) |
| (2) … over the leads already recorded | `gold_device_leads` (finding 0017: one snapshot, likely empty) | _pending_ |
| (3) `keyword_disagrees` on the 11 confirmed | 0 / 11 on the curated excerpt (finding 0014) | _pending_ on the device statement |
| (3) negatives flagged, of 122 | 1 (`K192732`) on the curated excerpt | _pending_ on the device statement |
| (3) curation candidates | — | _pending_ (count; the 21 omitted in finding 0012 are expected among them if their statements hit) |

Paste the three outputs into this table, and read a sample of the candidates and of
any newly flagged negatives before the next curation batch.

## Assumed

- The statement sits under the labels the slice shows (3881 "(Describe)", the pre-3881
  colon, the eSTAR prompt, Summary headings). A Summary that states intended use only
  in prose under another heading reads `not_found`; the full-scale `not_found` count is
  the visible edge of that.
- The 40-letter floor and the 2,500-character cap come from the slice, not full scale.
- More text means more stage-1 hits; the leads are recall-oriented (ADR 0016), and
  measure (3) shows the cost on curated negatives.

## How to re-check

```bash
uv run pytest tests/test_intended_use.py tests/test_curation_candidates.py \
  tests/test_leads.py -m "not live_network"
uv run registry extract-intended-use -v && uv run registry inspect   # Mac
uv run registry curation-candidates                                  # Mac
```
