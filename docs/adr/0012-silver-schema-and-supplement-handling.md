# 0012 — Optional-until-enriched silver fields; PMA supplements stay whole keys

**Status:** Accepted · **Date:** 2026-09-13
**Relates to:** [ADR 0007](0007-two-stage-mortality-flag.md), [ADR 0008](0008-generated-spark-schemas.md), [ADR 0010](0010-openfda-client.md)

Two decisions the roadmap flagged for Issue 2 (bronze→silver), settled before
building so the transform is written against a schema that admits partial rows.

## Context

`DeviceRecord` was designed as though every field arrives at once. It does not.
The FDA AI-enabled device list supplies the submission number, device name,
applicant, decision date, panel and product code. Device class, predicate lineage,
PCCP and the cybersecurity statement come from openFDA and arrive later, per
device, and sometimes not at all — openFDA legitimately returns nothing for a
submission it has not published.

As written, `device_class`, `has_pccp` and `cybersecurity_statement_present` are
required, so a list-derived row cannot be constructed at all until openFDA has
answered for it. That forces an all-or-nothing transform: one unenriched device
and either the row is dropped or the whole build fails.

Separately, the FDA list carries PMA supplements as `P130020/S005`. A supplement
is a distinct regulatory authorisation with its own decision date, not an alias
for its base approval.

## Decision 1 — openFDA-dependent fields become optional

`device_class`, `has_pccp` and `cybersecurity_statement_present` become
`… | None`, defaulting to `None`, with `None` meaning **not yet enriched**.

This follows the pattern ADR 0007 already set for the mortality flag: `None` is
"nobody has established this", which is a different claim from `False`. Applying
it consistently means a reader never has to ask whether a `False` in silver is a
finding or a placeholder.

One distinction is worth stating explicitly because it is easy to collapse:

| `device_class` value | Means |
|---|---|
| `None` | We have not enriched this row from openFDA yet |
| `"unclassified"` | The FDA itself classifies the device as unclassified |

Those are genuinely different facts and the schema now expresses both.

**Consequence:** silver can be built from the AI list alone, then enriched in
place as openFDA data arrives. Enrichment coverage becomes a measurable property
of the table (`count(device_class IS NOT NULL) / count(*)`) rather than a
precondition for building it. The cost is that every consumer must handle `None`
— the gold mart in particular must decide whether an unenriched row can qualify,
and should filter on the confirmed mortality flag as ADR 0007 requires anyway.

## Decision 2 — the raw submission number stays the key; supplements split alongside

`submission_number` keeps exactly what the FDA list published, suffix and all:
`P130020/S005` stays `P130020/S005`. Two derived columns carry the split, reusing
`openfda_client.split_pma_number` so silver and the client cannot disagree:

- `pma_base_number` — `P130020` (None for non-PMA)
- `pma_supplement_number` — `S005` (None for a base approval or non-PMA)

**Why keep the suffix in the key.** A supplement is its own authorisation with
its own decision date; `P130020` and `P130020/S005` are two events, and the
registry tracks events. Collapsing them would merge distinct decision dates,
under-count authorisations, and break the clearance-date time series the trend
report cares about. The raw value is also the join key back to bronze and the
identifier in the FDA's own deep link, so rewriting it would cost traceability
to the source row.

**Why split alongside rather than instead.** openFDA needs the base number and
supplement as separate query fields (ADR 0010), and grouping "all authorisations
for this device family" needs the base. Storing both means neither operation
has to re-parse a string, and `pathway` derivation is unaffected — the prefix is
still `P`.

## Alternatives considered

**Strip the suffix, key on the base number.** Simplest to join, and wrong: it
silently collapses separate authorisations into one row, so the row's decision
date becomes whichever pull happened to win. That is a data-correctness loss
disguised as a tidy key.

**Keep the fields required and run the transform in two passes**, building silver
only for devices openFDA has answered for. Rejected: it makes coverage invisible
(unenriched devices are simply absent, indistinguishable from devices the FDA has
not authorised) and makes the table's contents depend on openFDA availability at
build time. Optional fields keep every authorised device present and make the
enrichment gap explicit and measurable.

**A separate `silver_devices_enriched` table.** Avoids nullable columns, at the
cost of a join every consumer must remember and two tables that can drift. Not
worth it at this scale.
