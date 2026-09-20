# 0014 — The gold mortality mart filters on the confirmed flag alone

**Status:** Accepted · **Date:** 2026-09-15
**Relates to:** [ADR 0007](0007-two-stage-mortality-flag.md), [ADR 0012](0012-silver-schema-and-supplement-handling.md), [ADR 0013](0013-openfda-enrichment-architecture.md)

## Context

ADR 0007 designed the two-stage mortality flag in 2026-09. Nothing has read
`EvidenceRecord` since: `mart/` is empty and the registry has produced no
mortality-relevant output at all. This ADR is where that design meets code.

Three inputs could plausibly decide what lands in the mart: the curated
specialty taxonomy (`cardiovascular` / `metabolic`), the deterministic stage-1
keyword flag, and the curated stage-2 confirmation. Only one of them is a
judgement about *this device's intended use*.

## Decision 1 — only `mortality_confirmed_flag` qualifies a device

Not the taxonomy, not the keyword flag, not any combination.

- **The taxonomy names the reviewing committee, not the clinical problem.** A
  cardiovascular-risk tool cleared through the Radiology panel is common — and at
  full scale radiology is 76% of the registry, so filtering on specialty would
  both admit and exclude the wrong devices wholesale. A confirmed device outside
  cardiovascular/metabolic therefore still qualifies; the taxonomy is a starting
  signal for *what to review*, which is all it was ever claimed to be.
- **A keyword hit is a lead, not an answer.** Shipping stage 1 as the filter is
  precisely the silent over-inclusion the two-stage design exists to prevent.

ADR 0007 already priced this: devices awaiting review are excluded, and that is
correct because **under-inclusion is recoverable by reviewing more devices, while
over-inclusion is not recoverable at all** — once a regex hit is in the gold
table, nobody downstream can distinguish it from a considered judgement.

The practical consequence is stark and worth stating plainly: **until curation
happens, the mart is empty.** That is the honest state of the registry, not a
defect to engineer around.

## Decision 2 — disagreement between the stages is surfaced, not resolved

The mart carries `keyword_disagrees`: true when a curator confirmed a device that
stage 1 did not flag. It is not an error — either the keyword list is too narrow
or the judgement was generous — but it is the row most worth a second look, so
the mart names it rather than leaving it to be noticed.

This is only possible because ADR 0007 kept the stages in separate columns.

## Decision 3 — the mart is written even when empty

An empty table a consumer can query beats a missing one every consumer must
special-case. It also means "nobody has curated anything" and "curation found
nothing" are distinguishable by row count rather than by a `TableNotFound`.

## Decision 4 — two `EvidenceRecord` booleans become optional

`reports_sensitivity_specificity` and `discloses_demographics` change from `bool`
to `bool | None`, defaulting to `None`.

Both are read from the 510(k) summary PDF, which nothing fetches yet (roadmap
Issue 4). As required fields they forced every curated judgement to assert
something about a document nobody had read. `None` means *no summary has been
read*; `False` would mean *the summary reports none* — the same distinction ADR
0012 drew for the openFDA fields, and the same one that made `applicant_raw`
optional. The rule this project keeps arriving at: **when a value is unknown, say
so; never borrow a neighbouring fact to satisfy a non-optional type.**

`EvidenceRecord` also gains `intended_use_source` — a URL a reader can open to
check the text a judgement was made from. A judgement nobody can audit is not
evidence, and ADR 0007's whole premise is auditability.

## Decision 5 — the seed loader refuses, it does not skip

`config/mortality_seed.yaml` is hand-curated. The loader **raises** on a
confirmation with no `review_method`, an unrecognised method, a missing
`intended_use_text` or `intended_use_source`, or a device reviewed twice.

Skipping a malformed entry would make a dropped judgement indistinguishable from
one nobody made — and this is the single column where that confusion is most
expensive. ADR 0007 rule 1 already says a confirmation with no provenance is
invalid data rather than a warning; this is that rule at the file boundary.

## Consequences

- The mart's row count is a direct measure of curation effort, and nothing else
  can inflate it.
- Adding `intended_use_source` widens `EvidenceRecord`, so the generated Spark
  schema and its parity test move together (ADR 0008 makes that automatic).
- A curating **agent** must record `review_method: llm_assisted`, not `human`.
  That distinction is the entire point of the column, and an agent session
  reading intended-use prose and forming a judgement is exactly the case the
  label was invented for.
