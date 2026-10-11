# Handoff — Issue 4: the 510(k) document pass (acquisition and information extraction)

**For:** Claude Code cloud sessions in the `medtech-registry` environment (JDK 17,
Python 3.11), one PR at a time, plus a few steps that **must run on the maintainer's
Mac** because `fda.gov` / `accessdata.fda.gov` are blocked from every agent
environment (CLAUDE.md, finding 0011). Each PR below says which is which.

**Goal:** turn the 510(k) Summary PDFs into structured, provenance-carrying evidence:
predicate lineage, device-level intended use, a cybersecurity presence flag, and
(later) performance-study facts. This is the step that lifts the registry from
"what the FDA list says" to "what the device actually claims".

---

## 0. Read first (a fresh session has none of this context)

1. `CLAUDE.md` — invariants. Bronze append-only and schemas-from-Pydantic matter most here.
2. `findings/0011-pdf-spike.md` and `findings/data/pdf-spike-sample.csv` — **the measured
   facts this brief builds on.** Read the whole finding.
3. `docs/adr/0013-openfda-enrichment-architecture.md` — Decision 4 grouped predicate,
   PCCP and cybersecurity as "in the PDF". Finding 0011 thought PCCP absent; finding 0019
   (full scale) shows it is signalled by the FDA clearance letter — read 0019 before 4B.
4. `docs/adr/0016-leads-filter-pre-curation-signals.md` — `mortality_language` is weak
   *because* no device-level intended use exists yet. PR 4C fixes that.
5. `docs/adr/0007` and `0014` — evidence provenance and the confirmed-flag gate. Extraction
   produces **evidence**, never a confirmed judgement.
6. `src/registry/ingest/openfda_client.py` — the house pattern for a throttled, cached,
   fixture-tested HTTP client (ADR 0010). Copy its shape.
7. `docs/pr-review-routine.md` — the review rubric; §7 below adds Issue 4 gates.

## 1. What is already known (finding 0011, 60-document stratified spike)

- **URL:** `https://www.accessdata.fda.gov/cdrh_docs/pdf{int(yy)}/<K>.pdf`, where `yy`
  is the two-digit year in the submission number; the oldest filings use bare
  `cdrh_docs/pdf/<K>.pdf`. Every sampled device resolved on the first or second
  candidate. De Novo (`DEN…`) and PMA (`P…`) documents live elsewhere and are **not
  verified** — 4A verifies or explicitly defers them.
- **Text layer:** 59/60 have one; 8/60 are `mixed` (a scanned FDA letter or form page
  bundled ahead of a text Summary). The single scanned document is the oldest (2000).
- **Normalise first.** The text layer splits glyphs ("Noti fi cation", "K 2 5 3 6 2 8").
  Every match must run on a normalised or de-spaced copy.
- **Predicate:** `\b(K|DEN|P)\d{6}\b` near "predicate" / "substantially equivalent"
  recovered predicates in **57/60**. Multi-predicate tables exist (`K181892`: 8).
- **PCCP:** 0/60 by Summary heading in the spike, but **superseded by finding 0019**: at
  full scale 68 clearance letters state a cleared PCCP verbatim. **Cybersecurity: 8/60**, post-2019,
  a short formulaic clause.
- Availability: `silver_device_enrichment.statement_or_summary` = Summary 1,541,
  Statement 11, null 62. Only `Summary` filings have a document to fetch.
- Throttle ~1 request/second and stop on 429/403. Be a good citizen.

## 2. Architecture (decide in 4A's ADR; this is the recommended shape)

```text
accessdata.fda.gov ──(Mac / network-permitted runner only)──► bronze_summary_documents
                                                                (append-only, per page text)
bronze_summary_documents ──(anywhere, deterministic)──► silver_document_extraction
silver_document_extraction ──► silver_devices (predicate, cybersecurity)   [build-silver join]
                           └──► leads mortality_language (intended use)    [ADR 0016 amended]
                           └──► curation candidates (intended_use_text)    [seed workflow]
```

- **Acquisition is separate from extraction.** Fetching needs `fda.gov`; extraction does
  not. Storing the per-page text in bronze lets every extractor iterate offline, in any
  session, against real text — and lets extraction be re-run without re-fetching.
- **Bronze stores text, not PDFs.** Record per page: `page_number`, `text`,
  `char_count`; per document: `submission_number`, `url`, `http_status`, `fetched_at`,
  `content_sha256` (of the PDF bytes), `page_count`, `text_class`
  (`text` | `mixed` | `image`). Append-only and stamped like bronze_fda_ai_list. Whether
  to also keep PDF bytes (a blob table or an on-disk cache keyed by sha256) is a 4A ADR
  decision — the spike rule was "don't commit PDFs", which still holds for git.
- **Fixtures are real slices** (CLAUDE.md). The cloud session cannot reach `fda.gov`, so
  4A ships a `live_network` test that **records** a fixture slice when the maintainer runs
  it on the Mac — the same pattern Issue 1 used for openFDA. Recommended slice: `K003301`
  (scanned, bare `pdf/`), `K181892` (8-predicate table), `K243239` (cybersecurity clause),
  `K141480` and `K241847` (predicate named without a parsable number), plus ~5 ordinary
  recent Summaries from different years. Store extracted page text as fixtures, never PDFs.

## 3. The PR sequence

Each PR is a fresh branch off `main` after the previous one merges. Each has a ready
prompt in §6.

### PR 4A — acquisition (`ingest/summary_documents.py`) · cloud builds, Mac runs

- Throttled, cached fetcher with the `pdf{yy}` → bare `pdf/` fallback; stop on 429/403;
  `User-Agent` from settings. URLs built from `Settings` (no hard-coded host in pipeline code).
- PDF text extraction per page (`pypdf` or `pdfminer.six` — pick one in the ADR; the spike
  used pdf.js, so re-measure the glyph-splitting behaviour with the chosen library).
- `text_class` from per-page character counts (the spike's rule).
- `bronze_summary_documents` (Pydantic record → generated schema, parity test).
- CLI: `registry fetch-summaries [--limit N] [--only K…]`, fetching only `Summary`
  submissions not yet in bronze (by submission + sha256), resumable.
- `live_network` fixture-recording test for the slice above; unit tests on the recorded
  text. **Maintainer step:** run the recording test on the Mac, commit the fixtures to the
  PR branch, then run `registry fetch-summaries` for the full ~1,541.
- ADR: acquisition and storage (text in bronze; PDF bytes yes/no; De Novo/PMA scope).
- Finding: the full-scale fetch — success rate, `text_class` distribution vs the spike's.

### PR 4B — deterministic extraction (`transform/document_extraction.py`) · cloud only

- Normalisation (collapse intra-word spacing; de-ligature) with tests on the real fixtures.
- Section segmentation: the FDA 3881 Indications-for-Use form, and the Summary's
  "Predicate Device(s)", "Indications for Use"/"Intended Use", "Substantial Equivalence"
  headings. Record the page and section each value came from.
- Predicates: all predicate numbers per document (a list), plus how found
  (`section` | `sentence` | `table`). `DeviceRecord.predicate_submission_number` is
  singular: decide in the ADR whether it holds the primary predicate (first named in the
  predicate section) while the full list lives in the extraction table.
- `predicate_age_days` needs the predicate's decision date. Most predicates are not AI
  devices, so they are not in silver: fetch through the existing openFDA client (Issue 1)
  and cache — an enrichment step, not a PDF step.
- Cybersecurity presence flag (finding 0019: 30.6% mention it; define and measure the
  presence rule). **`has_pccp` from the clearance-letter sentence** (finding 0019: 68
  positives), with FDA correction letters ("inadvertently indicated") and explicit negations
  as negatives and the other Summary-body mentions classified; `pccp_summary` stays `None`.
  A new ADR amends ADR 0013 Decision 4 accordingly — *not* the "PCCP absent" ADR once planned
  in PR #11.
- `silver_document_extraction` with `extractor_version`; `build-silver` joins it to fill
  the predicate and cybersecurity fields (ADR 0012's optional-until-enriched fields).
- Finding: full-scale yields vs the spike (57/60 predicate, 8/60 cybersecurity).

### PR 4B+ — PCCP lead category · cloud only (after 4B merges)

Agreed 2026-10-11 on finding 0019's evidence (PCCP in 0% of 2022 clearances, 11.0% of
2026's): bring PCCP forward rather than wait for 4C.

- **New ADR superseding the PCCP part of ADR 0016 Decision 3** (the deferral). Do not edit
  0016. The foundation-model deferral stays.
- **A `pccp` signal and category in the leads mart**: `signal_pccp` from silver's
  `has_pccp` (4B), `True` / `False` / `None` with ADR 0012's distinction (`None` = no
  Summary fetched or not yet extracted, never "no PCCP"). Remove `pccp` from
  `leads.DEFERRED_CATEGORIES`, add it to `LEAD_CATEGORIES`; the `LeadRecord` validator and
  schema parity follow.
- **Signal, not trigger.** `has_pccp` is derived (from the document, not the FDA list), so
  by PR #14's rule it never decides *whether* a device moved: an added or changed device
  is tagged `pccp` when either side has `has_pccp` true. Populating `has_pccp` for the
  existing 1,541 devices must therefore produce **no** leads — assert that in a test.
- **The adoption trend is stock, not movement.** Add PCCP share by decision year to
  `registry inspect` (the finding 0019 table, recomputed from silver), so the trend is
  visible without waiting for new clearances.
- Measure: the `inspect` table should reproduce finding 0019's year-by-year counts.

### PR 4C — device-level intended use · cloud only

- Extract the intended-use text (3881 form first, Summary section second), verbatim, with
  page and source.
- Feed it to the leads as the **highest-precedence** `mortality_language` source
  (`summary_intended_use`), above the product-code definition. This amends ADR 0016
  Decision 2's precedence — record it in a new ADR, do not edit 0016.
- Offer it to curation: a report of cardiovascular/metabolic devices whose summary
  intended use trips the stage-1 keyword pass but which are not yet in
  `config/mortality_seed.yaml` — the next curation batch, with evidence attached.
- Measure: how many leads' `mortality_language_source` moves to the device-level text,
  and how stage-1 `keyword_disagrees` changes on the 133 curated devices.

### PR 4D — LLM-assisted structured extraction · cloud builds, gold set first

Deterministic regex is right for predicates and presence flags. Study facts are not
regex-shaped: sensitivity/specificity/AUC values, study design and size, whether
demographics are disclosed (`EvidenceRecord.discloses_demographics`,
`reports_sensitivity_specificity` — both waiting on this), and the clinical endpoint
(mortality, MACE, hospitalisation, detection). Design rules:

1. **Pydantic schema per extraction** with `bool | None` semantics (unknown ≠ false).
2. **Every extracted value carries a verbatim quote and a page number**, and a
   deterministic post-check rejects any value whose quote is not a substring of the
   normalised page text. That is the hallucination guard; it is non-negotiable.
3. Provenance: `review_method="llm_assisted"`, the model id, prompt version, and run id
   on every row — the same audit trail as the mortality seed (ADR 0007).
4. **Gold set before scale.** 40–60 documents, stratified by year and panel, labelled by a
   human (LLM pre-labels allowed, human-confirmed). Report precision/recall per field.
   No full run until a field clears an agreed bar; fields that do not clear it stay deferred.
5. Provider-agnostic interface; the provider/model choice, cost estimate (≈1,541 docs),
   and where it runs (it needs the bronze text, i.e. the Mac lakehouse, until durable
   storage exists) are the 4D ADR.
6. Output is evidence for curation and leads — it never sets `mortality_confirmed_flag`.

## 4. What stays out of scope

- OCR for the scanned tail: route `image` documents to a deferred bucket; revisit only if
  the full-scale count makes it worth it.
- `pccp_summary` (what a PCCP covers) before 4D; 4B populates only `has_pccp`.
- Durable storage (ADR 0011) — separate decision; until then the Mac lakehouse is the
  system of record (`docs/runbook-local-monitoring.md`).

## 5. Definition of Done (each PR)

The roadmap's DoD, plus: real-text fixtures for every extractor (synthetic only for edge
cases, and say so); a finding that reports full-scale numbers against the spike's; ADRs
for every decision named above; CONTINUATION §1/§2/§4 updated; `make lint && make test`
green; the review checklist below.

### PR-review checklist additions (extend `docs/pr-review-routine.md`)

| Gate | Why |
|---|---|
| No `fda.gov` request outside a `live_network`-marked test or the fetch CLI | CI and agent sessions cannot reach it |
| Bronze document table append-only, stamped with `content_sha256` and `fetched_at` | Same discipline as the FDA list |
| Matching runs on normalised text, with a test that fails on the raw layer | The glyph-splitting trap (finding 0011) |
| Every extracted value has page + method (+ quote for LLM) | Provenance (ADR 0007) |
| LLM values pass the verbatim-substring check, with a test that a fabricated quote is rejected | Hallucination guard |
| `has_pccp` only from the clearance-letter sentence or a classified Summary statement, with page + method; correction letters and negations read as `False` | Finding 0019 |
| Extraction never writes `mortality_confirmed_flag` | ADR 0014 |

## 6. Session prompts (paste one per session, in order)

**4A (cloud):**

```text
Read CLAUDE.md, then docs/handoffs/issue-4-document-pass.md in full (it lists what else
to read). Implement PR 4A only: the 510(k) Summary acquisition pass into an append-only
bronze_summary_documents table, the fetch-summaries CLI, and a live_network test that
records the real fixture slice named in §2 when run on a network-permitted host. You
cannot reach fda.gov: build against the recording test's contract, and say in the PR
that the maintainer must run it on the Mac and commit the fixtures before merge. Write
the acquisition ADR. TDD; make lint && make test green; update CONTINUATION; run the
review routine plus §5's checklist. Open the PR; do not merge. Stop after 4A.
```

**Maintainer, after 4A's code is reviewed (Mac):**

```bash
uv run pytest -m live_network -k summary      # records the fixture slice
git add tests/fixtures && git commit -m "Record real summary fixtures" && git push
# after merge:
uv run registry fetch-summaries -v            # ~1,541 docs at ~1/s ≈ 30 min
uv run registry inspect
```

**4B (cloud):**

```text
Read CLAUDE.md and docs/handoffs/issue-4-document-pass.md. Implement PR 4B only:
normalisation, section segmentation, predicate and cybersecurity extraction into
silver_document_extraction, the build-silver join, predicate decision dates via the
openFDA client, has_pccp from the FDA clearance-letter sentence with corrections and
negations handled (read findings/0019 first: it overturns finding 0011 on PCCP), and a new
ADR amending ADR 0013 Decision 4 accordingly. Use the committed real fixtures, and add
fixtures for the PCCP cases finding 0019 names (record them on the Mac like 4A's slice).
Report full-scale yields against finding 0019. Open the PR; do not merge. Stop after 4B.
```

**4B+ (cloud, after 4B merges):**

```text
Read CLAUDE.md, docs/handoffs/issue-4-document-pass.md (§3 "PR 4B+") and findings/0019.
Implement PR 4B+ only: a pccp lead category from silver's has_pccp, via a new ADR that
supersedes the PCCP part of ADR 0016 Decision 3 (do not edit 0016). has_pccp is a signal,
never a movement trigger (PR #14's rule): add a test that populating has_pccp for existing
devices records no leads. Add PCCP share by decision year to registry inspect and show it
reproduces finding 0019's table. Open the PR; do not merge.
```

**4C (cloud):**

```text
Read CLAUDE.md and docs/handoffs/issue-4-document-pass.md. Implement PR 4C only:
device-level intended-use extraction, wired as the top-precedence mortality_language
source for leads (new ADR amending ADR 0016 Decision 2's precedence), and the
curation-candidates report. Report the before/after measures named in §3. Open the PR;
do not merge.
```

**4D (cloud, after a gold set exists):**

```text
Read CLAUDE.md and docs/handoffs/issue-4-document-pass.md §3 PR 4D. Implement the
LLM-assisted extraction framework with the verbatim-quote validation rule, provenance,
and a gold-set evaluation harness. Do not run at full scale. Write the provider/cost
ADR. Open the PR; do not merge.
```
