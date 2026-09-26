# 0011 — The 510(k) summary PDF spike: a text layer exists, and predicate is regex-viable

**Date:** 2026-09-20 · **Status:** Measured; recommendation below · **Component:** roadmap Issue 4 (PDF pass) — measurement only, no `src/` change

This is the number the handoff asked for, with the character counts behind it, not a
qualitative impression. It answers one question: if a document pass reads the 1,541
public Summaries (`silver_device_enrichment.statement_or_summary = 'Summary'`, 95.5%
of the registry), does it get a **real text layer** it can regex, or **scanned images**
that need OCR?

**Answer: overwhelmingly a real text layer. A predicate-extraction pass is regex work,
not an OCR project — with a small, measurable scanned tail concentrated in pre-2010
filings.**

## Method

- **Sample:** 60 devices, `statement_or_summary = 'Summary'` only (all are 510(k) `K`
  numbers), drawn 10 per decision-year band (`random_state=42`), stratified pre-2015 /
  2015–2018 / 2019–2021 / 2022–2023 / 2024 / 2025–2026, because the registry is 95%
  recent and an unstratified draw would say nothing about the older tail. Row-level
  data: [`data/pdf-spike-sample.csv`](data/pdf-spike-sample.csv).
- **Fetch:** over a browser on a network that can reach `accessdata.fda.gov`
  (`fda.gov` is blocked from both the build/dev egress and the desktop dev VM — see
  CONTINUATION §5). Throttled to ~1 request/second; **stop-on-429/403** was armed and
  never tripped. Requests carried an ordinary browser user-agent (a real browser
  session), not a scripted client — `fetch()` cannot set a custom UA, so the descriptive
  UA the handoff suggested was not achievable this way; the 1/s throttle and stop-on-rate-limit
  citizenship rules held.
- **Text measurement:** each PDF parsed with pdf.js; characters counted **per page**
  (whitespace stripped). Per the handoff, a page under ~100 characters is treated as an
  image. Document class: `image` = 0 pages ≥100 chars, `text` = all pages ≥100,
  `mixed` = some.
- **No PDFs were committed or cached to disk.** They were fetched and parsed in memory;
  only the character counts, classifications and extracted sentences below and in the
  CSV leave the session. See "Regression fixtures" for the two worth capturing
  deliberately.

## The URL pattern — verified, and it is not what was assumed

The believed pattern was `pdf<NN>` with `NN` the K-number's 2-digit year. **The real
directory is `pdf` + the year as an integer with no leading zero:**

- `K253628` → `cdrh_docs/pdf25/K253628.pdf` ✅ (recent years: `pdfNN` and integer form coincide)
- `K093456` → `cdrh_docs/pdf9/…` ✅ — **not** `pdf09` (that 404s)
- `K033840` → `cdrh_docs/pdf3/…` ✅
- `K003301` (2000) → bare `cdrh_docs/pdf/…` ✅ — the oldest filings drop the number entirely

A fetcher should build `pdf{int(yy)}` first and fall back to bare `pdf/` for the oldest
filings. Every device in the sample resolved on the first or second candidate.

## Fetch success and text layer

| | |
|---|---|
| Fetched (HTTP 200) | **60 / 60 (100%)** |
| Text layer (`text`) | 51 |
| `mixed` (text + some scanned/blank pages) | 8 |
| Scanned `image`, no text | **1** (`K003301`, 2000) |
| Predicate statement present | 59 / 60 |
| Parsable predicate K-number(s) extracted | 57 / 60 |
| Cybersecurity discussion present | 8 / 60 (~13%) |
| **PCCP discussion present** | **0 / 60** |

By band, text-layer coverage was **9/10 pre-2015 and 10/10 in every band from 2015 on.**
The single scanned document is the oldest sampled filing. The `mixed` documents are the
normal case, not a failure: the FDA clearance letter and the Indications-for-Use form
(FDA 3881) that are bundled ahead of the Summary are sometimes a scanned page, while the
Summary body itself is text.

## How the predicate is stated (verbatim) — and why regex is viable

Every document carries the FDA's boilerplate substantial-equivalence sentence, which is
identical across filings and therefore not the useful signal:

> "…substantially equivalent (for the indications for use stated in the enclosure) to
> legally marketed predicate devices marketed in interstate commerce prior to May 28,
> 1976…"

The **actual predicate identity** lives in the Summary body, and it is formulaic. Three
shapes cover the sample, all machine-extractable (quotes are verbatim from the text
layer; note the kerning artifacts):

- **Labelled field.** `K250177`: "…Device Class Class II Review Panel Radiology
  **Predicate Device K242020**; EPIQ Series Diagnostic…". `K190013`: "**Predicate
  Device: K162532** (WellDoc® BlueStar®…) **Reference Device: K150910**…".
- **Predicate table.** `K181892`: "Device Name 510(k) Number Ziostation2 **K151212** CT
  PULMONARY ANALYSIS **K130552** SYNAPSE 3D LUNG AND ABDOMEN ANALYSIS **K130542**…" — a
  device-name/510(k)-number table listing multiple predicates.
- **Prose, older filings.** `K033840` (2003): "…substantially equivalent to the
  Romanowsky stain manual light microscopic process… and DiffMaster Octavia™ Hematology
  Analyzer, **510(k) number K003301**, CellaVision AB."

A `\bK\d{6}\b` scan (plus `DEN`/`P` variants) recovered predicate submission numbers in
**57 of 60** documents. The three misses: the scanned `K003301` (no text); and two text
documents (`K141480`, `K241847`) where the predicate is named by device name and the
K-number sits on a scanned page or is absent from the Summary body.

**One real caveat for whoever writes the extractor.** The pdf.js text layer splits
glyphs unpredictably — "Notification" comes out "Noti fi cation", "Classification" as
"Classi fi cation", "devices" as "devic es", "K253628" sometimes as "K 2 5 3 6 2 8". A
naïve regex over the raw layer will miss real hits. Matching must first normalise the
layer (collapse intra-word whitespace, or match against a de-spaced copy). Every count
above was produced with whitespace-insensitive matching; the 0/60 PCCP result was
re-checked against a fully de-spaced copy, so it is a true absence, not a spacing miss.

## PCCP and cybersecurity — a correction to ADR 0013's assumption

ADR 0013 Decision 4 grouped predicate lineage, PCCP and the cybersecurity statement
together as "in the 510(k) summary PDF". The data says otherwise:

- **PCCP: 0 / 60**, including recent 2024–2025 AI devices that carry PCCPs upstream in
  the FDA AI list. The Predetermined Change Control Plan is simply **not written into
  the public 510(k) Summary text** — not as a heading, not as prose. A document pass
  will **not** recover `has_pccp` / `pccp_summary` from these PDFs. That field needs a
  different source (the AI-list column upstream, or the full decision file), and the
  ADR's "committed to re-checking if the FDA adds structured PCCP fields" note is the
  right track — the Summary is a dead end for it.
- **Cybersecurity: 8 / 60 (~13%)**, present only from 2019 on, as a short formulaic
  clause when present. `K243239`: "Cybersecurity testing was performed in accordance
  with Cybersecurity in Medical Devices: Quality System Considerations and Content of
  Premarket Submissions." Extractable where it appears, but low-recall and only a
  presence/absence flag, not structured content.

## Recommendation

**Predicate lineage: regex-viable. Build it, not an OCR pipeline.** Scope it as:

1. Fetch `Summary`-flagged 510(k)s at `cdrh_docs/pdf{int(yy)}/<K>.pdf` (fallback bare
   `pdf/`), throttled ~1/s, honouring 429/403.
2. Classify text vs scanned by per-page character count; expect ~98% text on the
   registry's recency profile, with the scanned tail almost entirely pre-2010. Route the
   `image`/scanned minority to a deferred OCR bucket rather than blocking the whole pass
   on it — that is the "mixed with a split" outcome, and the split is small.
3. Normalise the text layer (whitespace-insensitive) **before** matching, then extract
   predicate submission numbers with `\b(K|DEN|P)\d{6}\b` near "Predicate"/"substantially
   equivalent"/a 510(k)-number table.
4. **Do not** expect PCCP from these PDFs; treat cybersecurity as a low-recall presence flag.

## What would make this a failure — avoided

Reporting "extraction looks feasible" without the counts. The counts are here: 100%
fetch, 59/60 text, 57/60 predicate numbers recovered, 0/60 PCCP. A `keyword_disagrees`-style
follow-up should re-run this at full scale before any `src/` acquisition code is written.

## Regression fixtures (decide deliberately — not committed here)

Two documents are unusually good fixtures if the team wants them captured:

- `K003301` (2000, bare `pdf/`, 7 pages, **0 extractable chars**) — the canonical
  scanned-image case, to pin the text-vs-image classifier.
- `K181892` (2018, `pdf18`, predicate **table** with 8 K-numbers) — the multi-predicate
  table shape, to pin the extractor against the hardest layout.

Per the handoff, the PDFs themselves are not committed; these are named so the decision
to capture them is a deliberate one.
