# 0010 — The first live openFDA enrichment: 100% on both tiers, and what the numbers say

**Date:** 2026-09-15 · **Status:** Verified · **Component:** `transform/enrichment.py`

Run on a Mac that can reach `api.fda.gov` (blocked from agent environments), over
the real 1,614-row pull. This is the first time the enrichment path has touched
the live API — fixtures could prove the wiring but never the transport.

## Both tiers resolved completely

| Tier | Endpoint | Result |
|---|---|---|
| 1 | `classification` by product code | **1,614 / 1,614 (100%)** |
| 2 | `510k` / `pma` by submission | **1,614 / 1,614 (100%)** |

`device_class` is now **100% populated** in silver. Two things this settles that
fixtures could not:

- **De Novo resolves through the `510k` endpoint.** All 40 De Novo grants
  returned, confirming ADR 0010's note that there is no `de_novo` endpoint and
  `DEN…` numbers are served from `510k`. This was the most likely place for tier 2
  to fall short and it did not.
- **PMA supplements resolve on their whole key.** All 21 PMAs including the 4
  supplements (`P980016/S939` and friends) returned, so keeping the full key
  (ADR 0012) costs nothing at lookup time.

**ADR 0012's `None` vs `"unclassified"` distinction fired on real data.** openFDA
returned `device_class` = `U` for exactly one device. Silver shows 100%
populated, which means that row mapped to `"unclassified"` — so it carried a
non-empty `unclassified_reason`, and the mapping preserved the FDA's own
designation rather than collapsing it to unknown. The one case the ADR was
written for occurred, and behaved.

## What the data says

**AI/ML devices are overwhelmingly moderate-risk, predicate-based clearances.**

| Class | Devices |
|---|---|
| II | 1,598 (99.0%) |
| III | 12 (0.7%) |
| I | 3 |
| unclassified | 1 |

With 1,553 of 1,614 cleared via 510(k), the shape of the registry is: **almost
every authorised AI device reached market by demonstrating equivalence to a
predicate, not by independent premarket evidence.** That is the single most
underwriting-relevant fact the registry has produced so far, and it is an
argument for prioritising predicate lineage (Issue 4) over almost anything else —
if equivalence is the mechanism, the evidence at the root of each chain is the
thing worth knowing.

**FDA review time** (1,614 rows, both dates known): min 7d, **median 139d**, mean
161d, max 1,136d. The mean sitting well above the median is the expected
right-skew from a few long reviews. This is a trend the curated list cannot
express at all.

**The Issue 4 ceiling is now a number, not a guess.**

| `statement_or_summary` | Devices |
|---|---|
| Summary — a public PDF exists | **1,541 (95.5%)** |
| null — PMA / De Novo, no such field | 62 |
| Statement — no public document | 11 |

A document pass could reach ~95% of the registry. Only 11 devices are
structurally unreachable. That is a strong input to the build-or-not decision,
and it cost nothing beyond a field we were already fetching.

## A claim this run corrected

ADR 0013 asserted that `life_sustain_support_flag` would be "a far better stage-1
input to the two-stage mortality flag than keyword matching". The live pass
returns **True for 3 of 1,614 devices**, against 154 in the cardiovascular
specialty alone.

It is a high-precision, near-zero-recall signal. Whatever it flags deserves
attention; it cannot be the stage-1 filter, because stage 1 must not miss. The
ADR has been corrected in place (it is still unmerged) and the flag is
re-characterised as **rule-in only**.

The claim was made from the field's *name*, before any data existed to test it —
the same failure mode as finding 0008's curated-config-checked-against-curated-fixtures,
one level up: a design assertion validated against nothing but its own
plausibility. Worth remembering that an ADR's confident tone is not evidence.

## A bug this run found

`registry inspect` grouped all four unenriched `DeviceRecord` fields under
*"0% is expected until that pass runs"*. After a 100%-successful pass it printed
`device_class` at 100% and the other three at 0% under that same heading —
telling a reader the enrichment had not run.

Three of those four cannot be filled by openFDA at all. They are now reported
separately, as awaiting the document pass, **with no percentage** — a coverage
figure implies a fetch that could have filled the column.

Third time the inspector has been the thing that was wrong (cf. finding 0009's
pull-history bug). The pattern is consistent: a report that summarises across a
category is only correct while the category is homogeneous, and these keep
turning out not to be.
