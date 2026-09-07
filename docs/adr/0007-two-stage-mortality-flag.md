# 0007 — Mortality/MACE relevance is two auditable columns, not one

**Status:** Accepted · **Date:** 2026-09-06

## Context

`mortality_or_mace_risk_indicated` is the one judgement call in an otherwise
structured pipeline. Every other field in `DeviceRecord` traces back to something
the FDA published; this one is an interpretation of intended-use prose.

Collapsing it into a single boolean would make the registry's most consequential
column its least auditable — a stakeholder could not tell whether a `true` came
from a regex hit or a careful human read, and an LLM-assisted guess would be
indistinguishable from an FDA-sourced fact.

## Decision

Store the judgement as two columns plus provenance, on `EvidenceRecord`:

| Column | Meaning |
|--------|---------|
| `mortality_keyword_flag` | Stage 1. Deterministic keyword/regex pass over `intended_use_text`. Always populated, cheap, reproducible. |
| `mortality_confirmed_flag` | Stage 2. `None` until reviewed, then `True`/`False`. |
| `mortality_review_method` | `"human"` or `"llm_assisted"` — how stage 2 was reached. |
| `mortality_review_notes` | Free text for the reviewer's reasoning. |

Two rules enforce this:

1. A model validator **rejects** a record where `mortality_confirmed_flag` is set
   but `mortality_review_method` is not. A confirmation with no provenance is
   invalid data, not a warning.
2. `None` is meaningfully distinct from `False`: "nobody has reviewed this yet"
   is not the same claim as "reviewed and judged not relevant". Only
   `mortality_confirmed_flag` may gate the gold-layer mart.

## Consequences

- The registry can always answer "why is this device in the mortality mart?"
- LLM-assisted review is possible without silently blending model output into
  FDA-sourced fields — it is labelled in the data.
- Stage 1 can be re-run freely (it is deterministic) without destroying stage 2
  review work, since they are separate columns.
- Cost: the gold mart must filter on the confirmed flag, so devices awaiting
  review are excluded by default. That is the correct default — under-inclusion
  is recoverable, silent over-inclusion is not.
