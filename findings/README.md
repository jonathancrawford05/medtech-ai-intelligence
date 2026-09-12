# Findings

Validation write-ups: **what was actually checked, what was observed, and what is
still unproven.** Step 6 of the six-step framework in
[`docs/validation-playbook.md`](../docs/validation-playbook.md) lands here.

## What goes where

Three doc types, three different questions. Keeping them apart is what stops this
directory from silently becoming a second copy of `docs/adr/`.

| Doc | Answers | Example |
|-----|---------|---------|
| `docs/adr/` | **Why** we chose this | "Prefer the CSV export over scraping" |
| `CONTINUATION.md` | **Where** the build is right now | "Phase 1 partial; openFDA not started" |
| `findings/` | **What we verified, and how we know** | "Acquisition was checked by browser inspection; the pipeline has never run against the live source" |

The distinction that earns this directory its place: an ADR records a decision and
the evidence *for that decision*. A finding records the **gap between what a
document claims and what has actually been executed**. Those gaps are invisible in
an ADR, because an ADR is written by someone who believes they are done.

## When to add one

- A component was validated — record what ran, where, and the result.
- A claim is in the repo that nobody has tested. Say so plainly. An untested claim
  recorded as untested is useful; an untested claim recorded as fact is a trap.
- A diagnostic cost real time to track down. Record the literal error text so the
  next person can find it by searching.
- An acceptance criterion from the development plan is still unmet.

Do **not** add one to restate a decision — that is an ADR — or to track current
state, which is `CONTINUATION.md`.

## Conventions

- `NNNN-short-slug.md`, numbered sequentially, never renumbered.
- Open with a status line: what was verified, by what method, on what date.
- Separate **Verified** (someone ran it and saw the result) from **Assumed**
  (reasoned, inspected, or inherited). Being explicit about which is the point.
- Close with **How to re-check** — the command or procedure, so the finding can be
  refuted rather than merely believed.
- Findings are not superseded like ADRs; they are dated observations. When one is
  overtaken by events, add a `**Closed:**` line at the top pointing to what closed
  it, and leave the body intact.
