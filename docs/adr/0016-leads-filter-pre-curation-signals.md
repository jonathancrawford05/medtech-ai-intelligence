# 0016 — Leads are filtered on pre-curation signals, never the confirmed flag

**Status:** Accepted · **Date:** 2026-10-04
**Relates to:** [ADR 0007](0007-two-stage-mortality-flag.md), [ADR 0014](0014-gold-mortality-mart.md), [ADR 0015](0015-silver-snapshot-pairing.md), [ADR 0017](0017-leads-output-surface.md) · roadmap Issue 3 · [handoff §4](../handoffs/issue-3-change-monitoring.md)

## Context

Issue 3 turns snapshot diffs (ADR 0015) into **leads**: movement in the FDA list
that a curator should look at. The registry already has one filter for
"mortality-relevant": the gold mart gates on `mortality_confirmed_flag` alone
(ADR 0014). That filter is right for its job, and it is the obvious thing to copy.
This ADR records why the leads surface must not copy it, and what it filters on
instead.

## Decision 1 — precision for the mart, recall for the leads

The two surfaces answer different questions and fail in opposite directions:

| | Gold mortality mart (ADR 0014) | Leads (`gold_device_leads`) |
|---|---|---|
| Question | Which devices *are* mortality-relevant? | What *moved* that a curator should look at? |
| Optimises | Precision | Recall + triage |
| Unrecoverable failure | Silent over-inclusion | A new device never surfaced |
| Gate | `mortality_confirmed_flag` | Pre-curation signals only |

A brand-new device has, by definition, not been curated: it has no
`mortality_confirmed_flag`. A leads surface gated on that flag would be empty for
exactly the devices it exists to catch. **The leads code never reads
`mortality_confirmed_flag`** — not as a gate, not as a tiebreak. A curator's
`False` does not hide a device's movement either: the curated judgement answers
"is it mortality-relevant", not "did it change". `LeadContext` loads evidence
*text* only, and `tests/test_leads.py` asserts both the behaviour and that the
module never names the field.

## Decision 2 — the live signals

Each is its own column and category; none collapses to a count.

- **`new_submission`** — the submission number is absent from the previous
  snapshot. Every added device is a lead on this alone: it is the roadmap's first
  category.
- **`cardiometabolic`** — `specialty_category` is in the taxonomy's
  `mortality_relevant_categories` (cardiovascular, metabolic), read from
  `config/specialty_taxonomy.yaml` rather than hard-coded. A starting signal, as
  the taxonomy always claimed: it names the reviewing panel, not the problem.
- **`mortality_language`** — the stage-1 keyword pass (`mortality_seed.keyword_flag`,
  widened from evidence in finding 0014) over the text that exists before
  curation, in precedence order: curated `intended_use_text` where a curator
  happened to capture it, the openFDA product-code `classification_definition`,
  then the `device_name`. `mortality_language_source` records which one hit.
  **Known limit:** no device-specific intended-use text exists for an uncurated
  device anywhere in the pipeline today. The product-code definition is shared by
  every device with that code, and the device name is short. This is a weak,
  recall-only signal until roadmap Issue 4 brings the 510(k) summary text.
- **`life_sustaining`** — openFDA's life-sustain/support flag (enrichment tier 1).
  `None` when the device is not enriched, which is not the same as `False`
  (ADR 0012's distinction).

**Which movements are leads.** Every `added` device. A `changed` or `removed`
device only when a signal holds on *either* side of the change: a device leaving
the cardiovascular panel deserves the same look as one joining it. A movement with
no signal (a radiology device leaving the list) is not a lead, and an unchanged
cardiovascular device is stock, not movement.

## Decision 3 — PCCP and foundation-model clearances are deferred, explicitly

The roadmap names both as leading indicators. Neither can be populated today, so
neither ships as a column. An always-empty column reads as "none found", which is
a claim nobody has measured.

- **PCCP**: absent from the public 510(k) Summary text (finding 0011, 0/60) and
  from every openFDA endpoint. Revisit with Issue 4's document pass.
- **Foundation-model clearance**: no FDA or openFDA field marks it, and no
  heuristic is defined. It needs a curated keyword set (built like the mortality
  seed, with its own ADR and a coverage test) before it can be a category.

Both are listed with their reasons in `leads.DEFERRED_CATEGORIES`, printed by
`registry monitor`, and rejected by `LeadRecord`'s category validator, so neither
can enter the table by accident.

## Decision 4 — removals only from a full-sized pull

A removal is read off the row stamp (ADR 0015 amendment), so it fires for every
device missing from the newest pull. A truncated local ingest would flood the
leads with false removals; the ≥1,500-row check runs only in the scheduled
workflow. Removal leads are therefore recorded only when the newest pull carried
at least 95% of the previous pull's rows (`MIN_PULL_FRACTION`, about the
scheduled ingest's 1,500 / 1,614 floor). Additions and changes are still recorded,
and the suppressed count is reported.

## Consequences

- New devices surface the moment they clear, before any curation, which is the
  business value of Issue 3.
- The leads are noisy by design. Every new radiology device is a
  `new_submission` lead; consumers filter on the category columns. Precision
  stays the gold mart's job.
- `mortality_language` is weak until Issue 4. Its source column makes that
  visible per row rather than hiding it.

## Alternatives considered

- **Gate leads on `mortality_confirmed_flag`, like the gold mart.** Rejected: it
  empties the surface for new devices (Decision 1). This is the mistake the
  handoff warns a future contributor is most likely to make.
- **Gate leads on the stage-1 `mortality_keyword_flag` from `silver_evidence`.**
  Rejected as the gate: that flag exists only for curated devices, so it has the
  same blind spot. The same keyword *pass* is used instead, over text every device
  has.
- **Only cardiometabolic movements are leads.** Rejected: the panel names the
  reviewing committee, and a cardiovascular-risk tool cleared through Radiology is
  common (ADR 0014). `new_submission` keeps recall; the category columns allow
  triage.
- **Ship PCCP / foundation-model as always-null columns.** Rejected (Decision 3).
