# 0004 — openFDA client built and validated against real fixtures

**Date:** 2026-09-11 · **Status:** Verified (unit) · **Component:** `ingest/openfda_client.py`

## The finding

`ingest/openfda_client.py` is built test-first and passes against **real** openFDA
responses (the fixtures captured in finding 0003). Its behaviour is pinned to what
the live API actually does, not to an inferred schema — the mistake ADR 0005 made
and [finding 0001](0001-phase-1-live-ingestion-gap.md) called out. Design decisions
are recorded in [ADR 0010](../docs/adr/0010-openfda-client.md).

## Verified (ran and saw the result)

`pytest tests/test_openfda_client.py -m "not live_network"` — **25 passed** in a
clean venv (httpx + respx + pydantic-settings; no Spark/JDK needed). `ruff check`
and `ruff format --check` clean. Covered:

- **Routing:** `K…`/`DEN…` → `510k`, `P…` → `pma`; `P130020/S005` splits into
  base plus supplement and the request carries both; the client selects the
  requested supplement client-side (not by row order).
- **De Novo:** `DEN250057` resolves via `510k` with `decision_code == "DENG"`.
- **Missing = `None`:** a 404 `NOT_FOUND` body and an empty `results` array both
  return `None`, not an exception.
- **Caching:** a repeat `fetch_submission` hits the network once (`respx` call
  count = 1); a disk cache (`openfda_cache_dir`) is reused by a fresh client
  instance without a second request.
- **Backoff:** a `429` then `200` succeeds; a persistent `503` raises
  `OpenFdaUnavailableError`.
- **API key:** sent as `api_key=` only when `settings.openfda_api_key` is set.
- **Classification:** `product_code:"QIH"` → class `2`, regulation `892.2050`.

## Assumed / not yet verified

- **Live:** two `@pytest.mark.live_network` tests hit the real `api.fda.gov`
  (fetch `K253628`; a bogus number → `None`). They are **not run in CI** and were
  **not run here** — `api.fda.gov` is blocked from every agent/dev environment.
  Run them on a network-permitted host (or CI) to confirm end to end.
- **No Spark/silver yet.** The client returns API-shaped dataclasses; mapping them
  onto `DeviceRecord` and choosing the reported `pathway` is Issue 2.

## How to re-check

```bash
uv run pytest -q tests/test_openfda_client.py -m "not live_network"   # unit
uv run pytest -m live_network                                          # on a net-permitted host
uv run ruff check src/registry/ingest/openfda_client.py
```
