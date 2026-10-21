# 0022 — LLM-assisted study-evidence extraction: a local model proposes, deterministic checks decide

**Status:** Accepted · **Date:** 2026-10-18
**Relates to:** [ADR 0007](0007-two-stage-mortality-flag.md), [ADR 0008](0008-generated-spark-schemas.md), [ADR 0012](0012-silver-schema-and-supplement-handling.md), [ADR 0014](0014-gold-mortality-mart.md), [ADR 0018](0018-summary-document-acquisition.md), [ADR 0019](0019-deterministic-summary-extraction.md), [ADR 0021](0021-device-intended-use.md) · [finding 0023](../../findings/0023-local-llm-extraction-spike.md), [finding 0024](../../findings/0024-llm-extraction-framework.md) · roadmap Issue 4, PR 4D ([handoff §3](../handoffs/issue-4-document-pass.md))

## Context

Predicates, presence flags and the intended-use statement sit under fixed labels,
and 4B/4C read them with rules. Study facts do not: sensitivity, specificity and AUC
with their intervals, study design and size, whether demographics are disclosed,
and what the device's output is about. `EvidenceRecord.reports_sensitivity_specificity`
and `discloses_demographics` have waited on this since Phase 2.

The 4D spike (finding 0023) ran `qwen3:14b` locally over 153 cardiometabolic
Summaries and drew a 58-row gold set. It answered whether this can work, and how:

- The model fabricates when the text points at results it does not contain (an
  image-only table, a list of tests without results). A verbatim-quote check
  rejected every fabrication seen. **The check is the design**, not a safeguard.
- What the check cannot see is a real number under the wrong name (10% of named
  metrics), a predicate's figure credited to the subject device, a threshold read
  as a result, and an invented upper bound on a one-sided interval.
- Asked for a page, the model was wrong a third of the time.
- A font-cipher text layer (`K182456`) reads as `text` to 4A and as data to the model.
- 3 of 153 documents ran past 4,000 output tokens; one took 24 minutes.
- A 14B model on the maintainer's fanless M4 Air runs ~2 minutes a document and
  throttles after ~45 minutes. All 1,519 readable Summaries would be ~70 hours.

The provider decision (Ollama, `qwen3:14b` primary, `gpt-oss:20b` comparison, on the
Mac) was made on 2026-10-11 (handoff §3 rule 7). This ADR records the framework.

## Decision 1 — raw answers in bronze, checked evidence in silver

Two tables, split like the rest of the lakehouse (CLAUDE.md invariant 2):

| Table | Grain | Write | What |
|---|---|---|---|
| `bronze_llm_responses` (`BronzeLlmResponseRecord`) | one call about one document | **append** | the model's raw `content` verbatim, the pages sent / dropped as garbled / trimmed, model, full digest, prompt version, options, attempts, tokens, speed, status — skipped documents included |
| `silver_llm_evidence` (`LlmEvidenceRecord`) | one proposed value | overwrite | each value judged by the checks: quote, derived page(s), quote status, final status, `accepted`, `needs_review`, `curation_lead`, full provenance |

The calls are the expensive, slow input, so they are kept and never redone to change
a rule: `registry build-evidence` re-checks every stored answer offline. Silver keeps
**rejected** values too (`accepted=false`), so a rule change is measurable and a
reviewer can see what was refused. It is rebuilt from the newest `ok` response per
(document, model digest, prompt version), so a `qwen3:14b` and a `gpt-oss:20b` run of
the same documents sit side by side. Both schemas are generated from the Pydantic
models (ADR 0008).

## Decision 2 — the schema (prompt `4d.1`)

Start from the spike's `4d-spike-4` schema; change it where the gold set said
(`llm_extraction/schema.py`):

- each metric names whose figure it is: `applies_to` = `subject_device` /
  `predicate` / `original_device` (K213971, K173839);
- error measures are their own names (`mae`, `rmse`, `arms`, `mean_difference`);
  method concordance is `agreement`; never `accuracy`;
- an interval carries `ci_low`, `ci_high` and `ci_sided` (`two` / `lower` /
  `upper`), so a one-sided bound stays one-sided (K182149, K213657);
- `is_result`: a stated threshold or acceptance criterion is not a result (K241633);
- `sample_size` carries its own `unit`;
- no "at most N" anywhere; no page field;
- the endpoint's `deterioration_risk` needs a future event within a time window.

`bool | None` throughout: unknown is not false (ADR 0012). The JSON schema sent in
Ollama's `format` is generated from the same models (`json_schema()`, `$ref`s
inlined), so prompt and parser cannot drift. Only `4d.1` can be *sent*; older
versions (`4d-spike-4`, the recorded fixtures) are only *scored*, with
`applies_to` / `is_result` `None` ("not asked") and the side read from the bounds.

## Decision 3 — the model sees only 4B's located pages, minus garbled ones

Input is the 3881 form and Summary pages 4B stored (`ifu_form_pages`,
`summary_pages`), from the bronze fetch with the same `content_sha256` (ADR 0021's
join). A page the new **garble test** flags never reaches the model; a document with
no page left is recorded as `skipped_no_pages` and never sent. Past 40,000
characters, keyword-bearing pages are kept first (the spike's rule).

The garble test (`summary_documents.page_text_class` → `text` / `garbled` /
`image`) flags a page with ≥ 5% control characters, ≥ 5% cipher symbols
(`<`, `>`, `?`, `@`, `=` and the like), or, at ≥ 100 words, < 2% English function words. Calibrated on
all 524 recorded pages (the 4D slice and 4A's Summary slice, PCCP cases included): it flags exactly K182456's two cipher pages and K141480's
four control-character pages (which 4B already reads as unreadable), nothing else.
It is a **read-time** test over stored text, not a new bronze column: bronze rows are
never rewritten, and a derived column would split old and new fetches across two
rules. 4B's own `classify_page` is unchanged (its alpha floor stays; adopting the
garble test there is a 4B version bump with its own measure, not part of 4D).

## Decision 4 — the deterministic checks decide

Ported from the spike scorer (`llm_extraction/checks.py`; `CHECKS_VERSION` is bumped on every rule change); the
scorer itself never left the Mac, so the port is proved by recomputing the gold
set's recorded statuses: **57 / 58 identical**, the 58th a different flagged-accept
status for the same decision (finding 0024).

The quote ladder, first match wins: `ok` (substring of one page after NFKC, quote and
dash folding, whitespace collapse, case-fold, trailing punctuation dropped) and
`ok_loose` (letters, digits, `%`, `.` only) **accept**; `ok_gapped` (word runs of ≥ 2
within 150 characters, across a page break if need be) and `ok_numbers` (no quote,
but the value and CI bound(s) in order on one page with ≤ 3 numbers between; metrics
with an interval only) **accept, flagged for review**; else `quote_not_found`. A found
quote must contain every number it supports — value and each CI bound, or the sample
size — else `value_not_in_quote`, unless on the other scale, a proportion and its percentage only (`ok_rescaled`, flagged).

Rules on top, all **reject** unless noted: `duplicate` (same metric, setting, value
and CI; dropped), `not_a_result`, `ci_inconsistent` (side disagrees with bounds),
`zero_triple` (`0 (0–0)`), `measure_not_named` (neither quote nor label names the
claimed measure — 0 / 6 such rows were right), `no_quote` (a non-null scalar without
one); `absent` (`performance_reported=false`, no metrics) accepts.

**The page is derived**: wherever the check found the quote or the numbers.

## Decision 5 — the provider interface

One function, `provider.extract(provider, request)`: (pages, schema, prompt version) →
validated `ExtractionOutput` + metadata. A `Provider` only turns system + user text
into raw text, so swapping model or server changes no rule. `OllamaProvider`:

- `/api/chat`, non-streaming, `format` = the schema, temperature 0, seed 42,
  `num_ctx` 16384, **`num_predict` 3000** (the output cap), `think: false` for
  `qwen3:14b` (`REGISTRY_LLM_THINK`: `false`, a level such as `low` for `gpt-oss:20b`,
  or `omit`);
- the **full digest** read from `/api/tags`, never assumed; a missing server or model
  stops the run before any document;
- **one retry, then skip**: a capped (`done_reason=length`, or `eval_count` at the
  cap), unparsable, off-schema or failed answer is retried once and then recorded as
  `skipped_output_cap` / `skipped_invalid` / `skipped_error`. Attempts are identical
  (temperature 0, fixed seed): the retry covers a transient failure; a deterministic
  runaway costs two capped calls and is skipped. `skipped_error` is retried by the next
  run; the other skips are answers and are not.

Every setting is `REGISTRY_LLM_*` / `REGISTRY_OLLAMA_BASE_URL` in `Settings`.

## Decision 6 — runs are scoped, paced and resumable

`registry extract-evidence` refuses to run without `--only` or `--limit` (optionally
`--specialty`): the handoff says to scope runs, and an unscoped run is ~70 hours.
It writes each batch of `llm_batch_size` (5) documents before resting
`llm_rest_seconds` (300), stops once generation falls below `llm_min_tok_s` (6.0),
and skips documents that already have a non-error answer for the same digest and
prompt, so re-running resumes. Run it under `caffeinate -i`.

## Decision 7 — the gold-set harness reports; a person decides

`registry score-gold` scores one (model digest, prompt version) run against a
labelled gold CSV, per field, per check status and per sampling stratum: every gold
row lands in {accepted, rejected, missing} × {right, wrong, other}. The gold judgement
carries over when the run proposed the same value (and measure); otherwise the row's
`correct_value` decides when it names exactly one value, else the row is `unjudged`.
The number that matters is the **precision of what the checks accept**. A field ships
only when it clears the maintainer's bar on a **fresh** gold draw (handoff §3 rule 6);
the harness prints the comparison and never decides. `--recorded DIR` scores the
fixtures offline. `registry draw-gold` draws the fresh set: by stratum, seeded, from
documents outside every gold set used for development, rejected values included, as a key
plus a blind labelling sheet (no check status, no sampling reason); `registry merge-gold`
joins the labels back by row.

## Decision 8 — evidence, never a flag

Every row is `review_method="llm_assisted"` with model, digest, prompt version, run
id and checks version — the audit trail ADR 0007 asks of the mortality seed. An
accepted `mortality` / `mace` / `hospitalisation` / `deterioration_risk` endpoint
sets `curation_lead`; nothing in this pass writes `mortality_confirmed_flag`, and
`LlmEvidenceRecord` has no mortality field (ADR 0014). Nothing here populates
`EvidenceRecord` or `silver_devices` yet: no field has cleared a bar.

## Consequences

- The model can be swapped, re-prompted or compared without touching a rule, and a
  rule can change without the model: every stored answer is re-checked offline.
- Every fabrication in the recorded slice is rejected, and tests pin it (K251218,
  K233253, K182456). What the checks still cannot see is a correctly quoted figure
  that is semantically wrong (a predicate's figure before `applies_to` was asked, a
  PPV quoted as specificity with "specificity" elsewhere in the label). That is what
  the gold set measures, and why `ok_gapped` stays review-only.
- On the recorded run the endpoint is the weak field (50% of accepted gold rows
  right); `4d.1`'s future-event rule is the fix to measure on the next draw.
- Two more steps in the Mac run order, after `extract-documents`:
  `extract-evidence --only … | --limit N` (model time) and `score-gold`;
  `build-evidence` whenever the checks change.
- `live_ollama` joins `live_network` as a marker CI never runs.

## Alternatives considered

- **Parse into silver in the same step, no bronze.** Rejected: a rule change would
  need the model again (~2 min a document), and the raw answer is the evidence a
  reviewer audits.
- **Trust the model's page or a self-check prompt.** Rejected: the page was wrong a
  third of the time, and a model cannot be the judge of its own fabrication.
- **Reject `ok_gapped` and `ok_numbers` outright.** Rejected: table rows and
  sentences broken by a footer are where most real results sit (`ok_numbers` 4 / 5
  right in the gold set); flagging keeps them, visibly.
- **Store the garble class in bronze.** Rejected: bronze is never rewritten, so old
  and new fetches would carry different rules.
- **A hosted model.** Not reopened here; decided on 2026-10-11 (local, no document
  leaves the machine, digest-pinned). The provider interface leaves the door open.
