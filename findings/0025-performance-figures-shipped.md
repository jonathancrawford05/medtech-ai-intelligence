# 0025 — Sensitivity / specificity shipped from the LLM pass: built, not yet run on the Mac

**Date:** 2026-11-07 · **Status:** Built, tested offline, and run once over the real `4d.3` evidence on
the Mac (2026-11-08: 15 of 56 documents carry a figure). The 5 flagged devices are not yet spot-checked.
· **Component:** [ADR 0023](../docs/adr/0023-shipping-llm-performance-figures.md),
following the maintainer's decision recorded in [finding 0024](0024-llm-extraction-framework.md)

## Verified (in this session, by the test suite)

- **Only accepted subject-device results count** (`tests/test_performance.py`). A row
  rejected by the checks, attributed to a predicate or an original device, not asked
  (`applies_to` None), marked a threshold (`is_result` false or None), from a reader
  study, or naming another measure never becomes a shipped figure.
- **A run that never asked for attribution ships nothing.** The recorded `4d-spike-4`
  slice has 236 accepted metrics over 43 documents. `select` gives 43 rows, none with a
  figure.
- **One figure per measure**, by `perf.2`: `single`, `overall` (one figure not tied to a
  subgroup beats subgroup figures), or `first_of_several` (flagged). Duplicate values
  count once. Sensitivity and specificity are chosen independently. A context counts as
  overall only when every word in it names the whole set ("all female" is a subgroup),
  and a document whose rows come from two runs is refused.
- **Scale.** 92.5 becomes 0.925; 0.93 with no `%` stays 0.93; "0.9%" becomes 0.009; 180
  is dropped.
- **Never False.** `reports_sensitivity_specificity` is True with a figure and None
  without one; the validator refuses False.
- **The curated rows get the figures by a join.** Through Spark, evidence goes to
  `silver_device_performance`, then into the curated row of `silver_evidence`. A curated
  device without a row keeps None, and an uncurated device never enters `silver_evidence`.

## First real run (Mac, 2026-11-08, rule `perf.2`)

`registry build-performance -v` over the `4d.3` run in the lakehouse (`qwen3:14b`), then
`registry build-mart`:

| | |
|---|---|
| documents (rows written) | 56 |
| with a figure (`reports_sensitivity_specificity` True) | **15** (27%) |
| sensitivity / specificity chosen | 15 / 14 |
| chosen as | `single` 21, `overall` 2, `first_of_several` 6 |
| devices flagged `needs_review` | 5 |
| curated judgements loaded by `build-mart` / gold mart rows | 133 / 11 (unchanged) |

Of 29 chosen figures, 21 were the only candidate. In 6 the rule had to guess, and those
devices are flagged. The 15-in-56 rate matches gold v2's reading that many
cardiometabolic Summaries report no standalone sensitivity or specificity (ECG monitors,
bench testing). The 41 without a figure are None, not "does not report". The mart is
unchanged, as ADR 0023 says: its column list does not carry the figures.

## Assumed (not verified here)

- The rule is tested on hand-built evidence rows: no `4d.3` answer is recorded in the
  fixtures, and only `4d.3` answers carry `applies_to` and `is_result`.
- Whether the 6 `first_of_several` choices picked the figure a reader would call the
  headline is not yet checked (spot-check below).

## To do (this finding is updated when it is done)

- [x] On the Mac, over the `4d.3` run already in the lakehouse (56 documents; above):

  ```bash
  uv run registry build-performance -v
  uv run registry build-mart
  ```

  Counts recorded above. Still open: spot-check the 5 `needs_review` devices against
  their PDFs.
- [ ] Record a few real `4d.3` responses as fixtures (like `responses/`), so the rule is
      also tested on real answers.

## How to re-check

```bash
uv run pytest -q tests/test_performance.py
```
