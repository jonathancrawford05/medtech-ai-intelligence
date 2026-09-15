# 0013 — openFDA enrichment: a separate table, two tiers, and what the API cannot give us

**Status:** Accepted · **Date:** 2026-09-15
**Relates to:** [ADR 0010](0010-openfda-client.md), [ADR 0012](0012-silver-schema-and-supplement-handling.md)

## Context

`silver_devices` has carried `device_class`, `predicate_submission_number`,
`has_pccp` and `cybersecurity_statement_present` as `None` since it was built.
ADR 0012 and roadmap Issue 2 both describe those as fields that "come from
openFDA". Checking that against the API's real responses before building shows it
is true of one of them.

The complete field set the `510k` endpoint returns:

```text
address_1 address_2 advisory_committee advisory_committee_description applicant
city clearance_type contact country_code date_received decision_code
decision_date decision_description device_name expedited_review_flag k_number
openfda postal_code product_code review_advisory_committee state
statement_or_summary third_party_flag zip_code
```

There is no predicate field, no PCCP field, and no cybersecurity field — on any
endpoint. Those three live in the 510(k) summary PDF and the decision summary at
`accessdata.fda.gov`: unstructured documents, a different acquisition problem
with different failure modes. Enriching from openFDA populates `device_class` and
nothing else on the existing schema.

That is not a reason to fetch less. The endpoints carry a good deal that the
curated AI list does not, and the classification endpoint carries FDA-assigned
structured flags that bear directly on the mortality lens.

## Decision 1 — enrichment lands in its own table, not in `silver_devices`

A new `silver_device_enrichment` table, one row per `submission_number`, holds
everything openFDA returns that we have a use for. `build-silver` left-joins it
to populate `DeviceRecord.device_class`; everything else stays in the enrichment
table for consumers to join.

**Why not enrich in place.** Rebuilding silver is cheap and expected — it is
derived state and a rebuild overwrites (ADR 0012). If enrichment lived inside the
transform, every rebuild would re-issue ~1,800 API calls, which makes the cheap
operation expensive and couples a local transform to a remote service's
availability. Separating them means `build-silver` stays offline and
deterministic, and re-enriching is a deliberate act.

**Why not widen `DeviceRecord` with every harvested field.** The fields below are
a research surface, not yet a product one; promoting them into the silver schema
commits us to them before anything reads them. `device_class` is promoted because
ADR 0012 already declared it part of the row.

## Decision 2 — two tiers, product code first

| Tier | Endpoint | Keyed by | Calls for the 1,614-row pull |
|---|---|---|---|
| 1 | `classification` | product code | **181** |
| 2 | `510k` / `pma` | submission number | 1,614 |

Tier 1 is where `device_class` comes from, and 181 distinct product codes cover
all 1,614 devices — measured, not assumed. Running it first means the field ADR
0012 cares about is populated by roughly a tenth of the calls a naive
per-submission pass would need, and it is the tier worth re-running when
classifications change.

Tier 2 adds per-submission facts a product code cannot carry (below). Both tiers
write through the disk cache ADR 0010 already built, so a re-run after a partial
failure costs only the calls that did not land.

**Harvested in tier 1** (per product code): `device_class`,
`unclassified_reason`, `medical_specialty_description`, `regulation_number`,
`definition`, `life_sustain_support_flag`, `implant_flag`.

**Harvested in tier 2** (per submission): `decision_code`, `date_received`,
`statement_or_summary`, `clearance_type`, `third_party_flag`,
`expedited_review_flag`, `advisory_committee_description`, openFDA's own
`applicant`, plus `review_time_days` derived as `decision_date - date_received`.

Three of these earn their place beyond completeness:

- **`life_sustain_support_flag`** is an FDA-assigned structured flag on the device
  classification. It is a far better stage-1 input to the two-stage mortality flag
  (ADR 0007) than keyword matching, and it costs nothing extra.
- **`review_time_days`** is a trend the curated list cannot express at all, and it
  is the kind of thing the underwriting audience asks about unprompted.
- **`statement_or_summary`** is the fetchability flag for the PDF work below.

## Decision 3 — `device_class` mapping is explicit and tested

openFDA returns `"1"` / `"2"` / `"3"`; `DeviceClass` is
`Literal["I","II","III","unclassified"]`. The mapping is a named, tested function,
not an inline dict, because it carries the distinction ADR 0012 exists to
preserve: an empty or unrecognised class maps to `None` (not enriched), and only
the FDA's own unclassified designation maps to `"unclassified"`. A silent
`str.upper()` or a `.get(x, "unclassified")` default would collapse exactly the
two facts that ADR is about.

## Decision 4 — the PDF fields stay `None`, and we record what would fetch them

`predicate_submission_number`, `predicate_age_days`, `has_pccp`, `pccp_summary`
and `cybersecurity_statement_present` remain `None` after enrichment. They are
deferred, not abandoned: the business case is being made on what exists, and PDF
processing is a decision to take with that buy-in rather than ahead of it.

What this ADR does now is make that future work scopeable instead of speculative.
`statement_or_summary` distinguishes a 510(k) that filed a **Summary** (a public
document containing predicate and, on recent submissions, PCCP and cybersecurity
discussion) from one that filed a **Statement** (no public document — the
information is only available on request). Recording it means the size and the
ceiling of the PDF pass are a query against the enrichment table, not a guess.

`classification.definition` and, for PMAs, `supplement_reason` and `ao_statement`
are free text already in hand. They are short and formulaic rather than
narrative, so they are candidates for simple pattern extraction well before
anything resembling document NLP is warranted.

## Consequences

- `build-silver` gains a dependency on a table that may not exist. A missing
  enrichment table is not an error: silver builds with `device_class` null, which
  is exactly what it does today.
- Enrichment coverage becomes two numbers, not one — tier 1 and tier 2 can
  succeed independently, and `registry inspect` reports them separately.
- The enrichment table is keyed by submission number and carries `enriched_at`,
  so staleness is visible. It is **not** append-only: it is a materialised view of
  a remote service, re-derivable at any time, so a re-run overwrites. Bronze
  remains the only append-only layer.
- We are committed to re-checking the PDF-only claim if the FDA adds structured
  PCCP fields. The AI list itself gained a PCCP column upstream once; the API may
  follow.
