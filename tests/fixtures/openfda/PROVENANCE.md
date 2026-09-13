# openFDA fixtures — provenance and findings

Real openFDA responses captured on **2026-09-08** against the live API
(`https://api.fda.gov`, `meta.last_updated` = `2026-08-31`). Captured through a
browser on a network-permitted host, because `api.fda.gov` is blocked at the
egress proxy of every agent environment (cloud workspace, dev VM, and the
desktop-app Linux VM behind `device_bash` all get `403 connect_rejected`). No
API key was used — the unauthenticated limit (240/min, 1000/day) is ample for a
one-off capture.

The submission numbers are the real AI/ML-list slice in
`tests/fixtures/fda_ai_list_sample.csv`, so these fixtures join directly against
the bronze sample.

## Trimming

Responses are **real but trimmed**: the high-cardinality `openfda` arrays
(`registration_number`, `fei_number`, and `k_number` on the classification
record) are truncated to the first 2–3 entries with a `"...(+N more; trimmed
for fixture)"` sentinel. Every other field is verbatim. Trim the sentinel entry
(or the whole array) further when slicing these for unit tests.

## Files

| File | Endpoint | Query | Result |
|---|---|---|---|
| `openfda_510k_K253628.json` | `510k` | `k_number:K253628` | 1 hit (QIH, radiology) |
| `openfda_510k_K253148.json` | `510k` | `k_number:K253148` | 1 hit (MNR, anesthesiology) |
| `openfda_510k_K260714.json` | `510k` | `k_number:K260714` | 1 hit (QDQ, `clearance_type:Special`) |
| `openfda_510k_DEN250057.json` | `510k` | `k_number:DEN250057` | 1 hit — a **De Novo** grant (`decision_code:DENG`) |
| `openfda_pma_P130020.json` | `pma` | `pma_number:P130020` | 6 rows: base + S001–S005, incl. **S005** |
| `openfda_pma_P250008.json` | `pma` | `pma_number:P250008` | 2 rows (base + S001) |
| `openfda_pma_P950009.json` | `pma` | `pma_number:P950009` | 3 of 25 supplements (`total:25`) |
| `openfda_classification_QIH.json` | `classification` | `product_code:QIH` | 1 hit (class 2, reg 892.2050) |
| `openfda_pma_NOT_FOUND.json` | any | a query with no match | the `NOT_FOUND` error envelope |

## Findings that change Issue 1's design

1. **There is no `de_novo` endpoint.** `GET /device/de_novo.json` returns a bare
   `Cannot GET /device/de_novo.json` (an Express routing 404, *not* a JSON
   `NOT_FOUND` envelope). Do not build a `de_novo` client method.

2. **De Novo grants live in the `510k` endpoint**, keyed by the `DEN…` number in
   the `k_number` field, and are distinguished by `decision_code:"DENG"` (vs
   `"SESE"` for a substantial-equivalence 510(k)) and `clearance_type:"Direct"`.
   So the pathway map for *fetching* is: `K…` and `DEN…` → `510k` (field
   `k_number`); `P…` → `pma` (field `pma_number`). This differs from the
   silver-layer pathway derivation (`DEN…`→de_novo) in roadmap Issue 2 — the
   fetch endpoint and the reported pathway are not the same thing; keep them
   separate.

3. **PMA supplements are a separate field.** openFDA stores `pma_number:"P130020"`
   and `supplement_number:"S005"` in two fields; there is no `"P130020/S005"`
   anywhere. The bronze value `P130020/S005` must be split on `/` before the
   lookup (`pma_number:P130020 AND supplement_number:S005`). This answers the
   ADR question the roadmap raised about supplement-suffix handling on the
   *lookup* side.

4. **Exact-ID OR batching does not work.** `pma_number:(P250008 P130020 P950009)`
   returns `NOT_FOUND` (the group is AND-ed, so nothing matches all three). Query
   **one submission number per request** — which is what the roadmap's caching
   design (keyed by submission number) wants anyway. Batching, if ever needed, is
   a `skip`/`limit` paginated scan, not an OR of ids.

5. **PMA records carry little classification detail.** On every `pma` row,
   `openfda.medical_specialty_description` is `"Unknown"` and
   `openfda.regulation_number` is `""`; only `device_class` is populated (`"3"`).
   Specialty/panel for PMA devices must come from the AI-list `Panel (Lead)`
   column, not from openFDA. For `510k`/De Novo rows the `openfda` block *does*
   carry `medical_specialty_description` and `regulation_number`.

6. **Recent clearances are present.** June 2026 510(k)s and a March 2026 PMA all
   resolved against an API snapshot dated 2026-08-31 — the propagation lag is
   short, but a not-yet-propagated submission is still possible, so treat a
   `NOT_FOUND` / empty `results` as a normal "no record yet" (return `None`), not
   an error.

7. **Product codes can disagree.** The AI list gives `NMN` as P950009's primary
   product code; openFDA's `pma` record gives `MNM`. When they differ, they are
   from different FDA systems — decide in the silver ADR which is authoritative
   (recommend: keep both, treat the AI-list code as the AI-relevant one).

## How to replay in tests

Each file is a complete HTTP response body. With `respx`, mock the endpoint URL
and return the file's bytes, e.g. `respx.get(url__regex=r".*/device/pma\.json").
mock(return_value=httpx.Response(200, json=json.loads(path.read_text())))`. Use
`openfda_pma_NOT_FOUND.json` with `httpx.Response(404, ...)` for the miss path.
