# Handoff — Issue 3: FDA-list change monitoring (`monitor/`, `mart/`)

**For:** a fresh Claude Code session on the real toolchain (JDK 17 / Py 3.11, full
Spark suite, clean git). **Not** a Cowork/browser session — this issue never touches
`fda.gov`, so it needs no live network and no browser. It needs the things Cowork's
dev VM lacks: JDK 17, Python 3.11, and `make test` with Spark.

**Branch:** take a fresh branch off `main` (e.g. `issue-3-change-monitoring`). Put the
branch name at the top of `CONTINUATION.md` as the working branch.

**One-line goal:** turn the append-only registry into a *movement detector* — given two
successive snapshots of the device list, emit the **new and changed devices that are
worth a lead**, not raw counts.

---

## 0. Read first (a fresh session has none of this context)

In this order:

1. `CLAUDE.md` — the five architecture invariants. Every one of them constrains this
   issue.
2. `CONTINUATION.md` §1/§2/§4 — current pipeline state.
3. `docs/roadmap.md` → **Issue 3** — the original brief and its Definition of Done
   (this handoff supersedes nothing there; it makes it execution-ready).
4. `docs/validation-playbook.md` — *how* to validate in this repo.
5. `docs/pr-review-routine.md` — the severity rubric and the test-integrity gate your
   PR will be reviewed against. The issue-specific checklist at the bottom of this
   handoff **extends** that document; it does not replace it.
6. `docs/adr/0007-two-stage-mortality-flag.md` and
   `docs/adr/0014-gold-mortality-mart.md` — the mortality-flag semantics and the
   gold-mart "confirmed flag is the only gate" principle. The distinction in §4 below
   depends on understanding these.
7. `findings/0012-mortality-seed-curation.md` and
   `findings/0014-stage1-keyword-widening.md` — what the curated seed contains and how
   the stage-1 keyword pass now behaves.
8. `src/registry/mart/mortality_relevant.py` — the existing gold mart. The new leads
   mart should sit beside it and follow the same construction idioms
   (`qualifying_frame` / `build` / `run`, `settings.table_ref` via `registry.tables`,
   frame-not-rows so an empty result keeps its schema).

---

## 1. Where the repo is now (state you inherit)

- **Bronze → silver → enrichment are built and have run over the real 1,614-row pull.**
  `silver_devices` has one row per submission; openFDA enrichment (`device_class`,
  life-sustain flag, 510(k) summary availability) is 100% populated.
- **The gold mart is built.** `gold_mortality_relevant` gates on
  `mortality_confirmed_flag` only and currently holds **11 curated rows**
  (`config/mortality_seed.yaml`, 11 true / 122 false).
- **`registry inspect` now has a GOLD section** (this same PR) that reports the mart
  rows, `keyword_disagrees`, the specialty split and the leads newest-first.
- **`src/registry/monitor/` is an empty package** — just `__init__.py`. This issue is
  where it gets filled. `src/registry/mart/` already holds `mortality_relevant.py`; the
  leads mart is a new sibling module.
- **The live list is static right now.** The last three bronze pulls share one content
  hash (`source_snapshot_id`), so there is exactly **one distinct snapshot** in bronze
  today. **You cannot integration-test movement against live data — there is no
  movement.** Build and test against synthetic fixtures (§5), exactly as the roadmap's
  acceptance requires ("two synthetic snapshots that differ by a known set of rows").

---

## 2. Why this issue is the point of the whole registry

Everything upstream is plumbing that answers "what is authorised today." The business
value for Munich Re underwriting/partnership is **rate of change** — a *new*
mortality-relevant cardiovascular/metabolic device is a lead the moment it clears, not
at the next quarterly review. Bronze is append-only with a content-hash
`source_snapshot_id` precisely so consecutive snapshots can be diffed. This issue turns
that latent capability into an output a stakeholder queries.

Distinct from CI's `live-network` job: that guards the *source's shape*
(URL/headers/row-count drift). This monitors *content movement* for leads.

---

## 3. Scope — what to build

- **`src/registry/monitor/…`** — diff the two snapshots and classify movement.
- **`src/registry/mart/…`** — a **leads** output (a gold/mart table or a report
  artifact) that stakeholders query. Keep clearance date as a **time series** (rate of
  change matters), not a static snapshot.
- Decide and wire the **alerting surface** (table / log / notification).

Lead categories the roadmap names (surface each as its own signal, do not collapse to a
count): **new** submission numbers; new **cardiovascular/metabolic-panel** devices;
devices whose intended-use carries **mortality/risk-prediction language**;
**PCCP-flagged** devices; **foundation-model** clearances.

---

## 4. The one design decision that matters most (ANALYZE this before coding)

**The leads mart is not the gold mart, and must not inherit its filter.**

The gold mart (`mortality_relevant.py`) gates on `mortality_confirmed_flag` — curated
truth, optimised for **precision**: a human/LLM judged each row. That is correct for "the
list an underwriter reads," where silent over-inclusion is unrecoverable (ADR 0007/0014).

A leads mart has the opposite job. A brand-new device **has not been curated yet** — by
definition it has no `mortality_confirmed_flag`. If the leads surface gated on the
confirmed flag it would be **empty for exactly the devices it exists to catch.** So the
leads filter must run on the **signals available before curation**: the stage-1
`mortality_keyword_flag` (now widened, finding 0014), the cardiovascular/metabolic
`specialty_category`, the openFDA life-sustain flag, and intended-use language. Leads
optimise for **recall + triage**: "here is what changed that a curator should look at,"
not "here is confirmed truth." Record this precision-vs-recall split in the ADR — it is
the thing a future contributor is most likely to get wrong by copying the gold mart.

**Second open question — what are "the two most recent silver builds"?** Silver is
currently a single current table, rebuilt from the latest bronze pull; it is not
retained per snapshot. Bronze *is* per-snapshot (append-only, `source_snapshot_id`). So
decide, in an ADR: do you (a) build silver per bronze snapshot and diff two silver
builds, or (b) diff at bronze on `source_snapshot_id` then enrich only the delta? (b) is
cheaper and respects append-only; (a) reuses existing transforms. Either way the diff
key is `submission_number` and the snapshot key is `source_snapshot_id` — never a
filesystem path or an `ingested_at` timestamp equality.

**Two known data-availability gaps** (VALIDATE — do not silently emit empty columns):

- **PCCP is not available yet.** Finding 0011 established PCCP is absent from the public
  510(k) Summary text (0/60) and it is in no openFDA endpoint. The PCCP lead category
  therefore **cannot be populated until roadmap Issue 4 (the PDF/other-source pass)
  exists.** Either defer the category with an explicit TODO tied to Issue 4, or wire the
  column as always-null with a comment — do not present an unpopulated PCCP flag as if it
  were real signal.
- **"Foundation-model clearance" has no structured field.** There is no FDA/openFDA flag
  for this. It needs a defined heuristic (an intended-use / device-description keyword
  set, curated like the mortality seed) before it can be a category. Define it in the ADR
  or defer it; do not ship an undefined filter.

---

## 5. TDD plan (the acceptance is fixture-driven)

Write the failing test first (CLAUDE.md). Fixtures: **two synthetic snapshots that differ
by a known set of rows** — a handful added, one panel-changed, one intended-use-changed,
and rows that are identical across both. Assert:

- The correct **new/changed leads per category** are returned (name the exact submission
  numbers in the fixture, assert on them by value — no truthiness checks; the review's
  test-integrity gate rejects weak assertions).
- **Empty on identical snapshots** — diffing a snapshot against itself yields no leads.
  This is the case the live data is in today, so it is not hypothetical.
- The **time-series / gold query works** (clearance counts over time are queryable).
- Schema parity if you add a Pydantic record for a lead row (ADR 0008 — generate the
  Spark schema, add the parity test; `tests/test_schemas.py` is the pattern).

Mark anything needing a JVM `spark`; nothing here should be `live_network`.

---

## 6. Definition of Done (from the roadmap, plus this issue's specifics)

- Failing test first; fixtures synthetic-by-necessity here (no live movement exists) —
  state that reason in the PR so the reviewer does not flag "synthetic where a live
  source exists."
- Invariants held: `settings.table_ref()` for every table; bronze untouched and
  append-only; schemas generated from Pydantic; settings cache-safe in tests.
- `make lint && make test` green (Spark in the image / CI); coverage floor only moves up.
- **New ADR(s)** for: the leads precision-vs-recall filter set (§4), the snapshot-pair
  strategy (§4), and the output/alerting surface. Supersede, never edit an accepted ADR.
- Update `CONTINUATION.md` §1/§2/§4; add a `findings/` note recording the first diff run
  and the categories that are live vs deferred (PCCP, foundation-model).
- Run the checklist below (which extends `docs/pr-review-routine.md`) on the PR.

---

## 7. PR-review checklist for this issue (extends `docs/pr-review-routine.md`)

Apply the full severity rubric and **test-integrity gate** from
`docs/pr-review-routine.md` unchanged. These are the **issue-specific gates** to add to
the standard gate table — same `pass | fail | n/a | insufficient-context` discipline,
one `path:line` of evidence each:

| Gate (Issue 3 specific) | Why it matters |
|---|---|
| Leads filter runs on **pre-curation signals**, NOT `mortality_confirmed_flag` (§4) | A confirmed-flag gate makes the leads surface empty for new devices — the exact failure it must avoid. **P0 if the leads mart copies the gold-mart filter.** |
| Snapshot pairing keyed on `source_snapshot_id`, not `ingested_at` equality or a path | Append-only correctness; two pulls with the same content hash are the *same* snapshot and must diff to empty. |
| Diff grain is one comparison per `submission_number` | Wrong grain = phantom or missed leads. Same class of bug as the silver join-grain rule. |
| **Empty-on-identical** proven by a test, not just asserted in prose | This is today's real data state; if it is not tested it is not true. |
| PCCP category is deferred/null-with-comment, not presented as live signal (§4) | Finding 0011: PCCP is not in any current source. Emitting it as real is silent misinformation → domain-correctness P0/P1. |
| Foundation-model category has a defined heuristic or is explicitly deferred | An undefined filter is unreviewable and non-deterministic. |
| A new lead-row schema (if any) is generated from a Pydantic model + parity test (ADR 0008) | Hand-written Spark schema is a P0 invariant break. |
| Time-series output preserved (per-date, not a single snapshot count) | Rate-of-change is the business value; a static count loses it. |
| New ADR(s) present for the §4 decisions and the output surface | These are decisions a future session will re-litigate. Missing ADR = P1. |

**Domain lens to add** to the routine's step 3: *Monitoring/leads* — a device appears as
a lead **before** anyone curates it; confirm the filter reflects that, and confirm no
lead category is populated from a source that does not yet exist (PCCP, foundation-model).

---

## 8. Why this belongs in a Claude Code session, not Cowork

- **No FDA network, no browser.** The whole issue runs against silver/bronze already in
  the lakehouse and synthetic fixtures. Cowork's one unique capability here — a browser
  on a network that can reach `fda.gov` — is not needed.
- **It needs the toolchain Cowork's dev VM lacks:** JDK 17 (Spark 4.0), Python 3.11
  (the code uses `StrEnum`), and `make test` with Spark. In the VM the suite cannot run;
  in a Claude Code session on the real machine (or the Docker JDK-17 image) it can.
- **Clean git.** A Claude Code session commits and pushes directly; no
  base64-across-the-bridge, no delete-permission dance for stale `.git` locks.
