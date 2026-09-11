# 0010 — openFDA client: endpoint routing, missing = None, on-disk cache

**Status:** Accepted · **Date:** 2026-09-11
**Relates to:** roadmap Issue 1; [finding 0003](../../findings/0003-openfda-live-verification.md)
(live capture) and [finding 0004](../../findings/0004-openfda-client.md) (this build)

## Context

`ingest/openfda_client.py` enriches the AI list with device class, regulation and
decision detail from `api.fda.gov`. Three questions had to be settled, and the
live API (captured 2026-09-08, finding 0003) answered the first for us rather than
our guessing:

1. **Which endpoint serves which submission number**, and how supplements work.
2. **How to represent a submission openFDA has no record for.**
3. **Where the response cache lives** — on disk, or in a bronze Delta table.

## Decision

**Endpoint routing (verified, not inferred).** There is **no `de_novo` endpoint**
(`/device/de_novo.json` 404s at the router). De Novo grants are served by the
`510k` endpoint, keyed by the `DEN…` number in `k_number` and marked
`decision_code == "DENG"`. So:

- `K…` and `DEN…` → `/device/510k.json`, `search=k_number:"…"`.
- `P…` → `/device/pma.json`. A supplement is a **separate field**, so
  `P130020/S005` is split on `/` into `pma_number:"P130020" AND
  supplement_number:"S005"`. A bare `P130020` returns every supplement; the client
  selects the base row (empty `supplement_number`). Selection is done client-side
  by matching `supplement_number`, not by trusting row order.
- Product-code lookups → `/device/classification.json`, `search=product_code:"…"`.

Exact-id OR-batching does not work on openFDA (the group is AND-ed → `NOT_FOUND`),
so the client queries **one submission number per request** — which the
submission-keyed cache wants anyway.

**Missing = `None`.** A 404 `NOT_FOUND` or an empty `results` array returns `None`:
a normal "openFDA has no record (yet)", not an error. Recent clearances can lag the
API, so a miss must be a routine outcome the caller handles, not an exception. By
contrast, network errors and 5xx (after retries) raise `OpenFdaUnavailableError`,
and a 400 raises `OpenFdaError` (a malformed query is our bug, not a miss). 429 and
5xx are retried with the shared exponential backoff (`settings.http_*`).

**Cache is on disk, keyed by submission number — not a bronze table.** The client
memoises every lookup in process, and optionally persists results to
`settings.openfda_cache_dir` (one JSON file per key) so an unchanged record is not
refetched across runs. Misses are memoised in process but not persisted. The cache
is deliberately **not** a Delta table: the client is a pure HTTP/JSON component
with no Spark dependency, and openFDA responses are not themselves a source of
truth to be historised — the AI list is (bronze), and the join that lands openFDA
fields into silver `DeviceRecord` is a separate concern (Issue 2).

The client returns typed `OpenFdaDevice` / `OpenFdaClassification` dataclasses
(each keeping the full `raw` result), **not** Pydantic/Spark models: they are an
API-shaped intermediate the Issue 2 transform maps into `DeviceRecord`, not a table
written to the lakehouse, so they stay out of `schemas.py`.

## Consequences

- The pathway *fetched from* (`K…`/`DEN…` → 510k) differs from the pathway
  *reported* in silver (`DEN…` → de_novo). Issue 2 must keep those separate; the
  `decision_code == "DENG"` marker distinguishes a De Novo grant in the 510k data.
- PMA rows carry no `medical_specialty_description`/`regulation_number` (both come
  back `Unknown`/empty); silver must take specialty from the AI-list panel. The
  `classification` endpoint fills regulation/class by product code where needed.
- Turning on `openfda_cache_dir` makes runs cheap and mostly offline after a warm
  cache; a stale cache is the trade-off (no TTL yet — acceptable while the openFDA
  data itself is slow-moving; revisit if that changes).

## Alternatives considered

**Cache openFDA responses in a bronze Delta table.** Rejected: it couples the
client to Spark/JDK for no benefit, and conflates "a cache of a lookup API" with
"the historised source list". If we later want openFDA pulls historised, that is a
deliberate bronze table written by the Issue 2 transform, not a side effect of the
client.

**Raise on a missing submission.** Rejected: propagation lag makes a not-yet-listed
submission a normal, expected outcome; forcing callers to catch an exception for
the common case is worse than a `None` they must handle explicitly.

**A `de_novo` client method.** Rejected: the endpoint does not exist. De Novo goes
through `510k`.
