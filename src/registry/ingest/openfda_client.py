"""A thin, typed client over the openFDA device API (``api.fda.gov``).

Purpose
-------
The FDA AI-list (bronze) gives name / date / panel / product-code but not device
class, regulation, or decision detail. openFDA's ``510k``, ``pma`` and
``classification`` endpoints have those. This module fetches them by submission
number or product code and returns a typed record; joining them onto the AI list
is a separate concern (roadmap Issue 2), so this client never touches Spark.

What the live API actually does (verified 2026-09-08; see
``tests/fixtures/openfda/PROVENANCE.md`` and finding 0003):

- **There is no ``de_novo`` endpoint.** De Novo grants are served by the ``510k``
  endpoint, keyed by the ``DEN…`` number in ``k_number`` and distinguished by
  ``decision_code == "DENG"``. So the fetch map is ``K…``/``DEN…`` → ``510k``
  (field ``k_number``); ``P…`` → ``pma`` (field ``pma_number``).
- **A PMA supplement is a separate field.** ``P130020/S005`` must be split into
  ``pma_number=P130020`` and ``supplement_number=S005`` before the lookup.
- **A submission openFDA has no record for returns nothing** (404 ``NOT_FOUND``
  or empty ``results``). That is represented as ``None`` -- a normal "no record
  yet", not an error (ADR 0010).

Caching (ADR 0010): responses are cached by submission number so an unchanged
record is not refetched. The cache is in memory per client, and optionally
persisted to ``settings.openfda_cache_dir`` on disk -- deliberately *not* a Delta
table, to keep the client decoupled from Spark.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from registry.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

Endpoint = Literal["510k", "pma"]


class OpenFdaError(RuntimeError):
    """Base class for openFDA client failures."""


class OpenFdaUnavailableError(OpenFdaError):
    """openFDA could not be reached (network / 5xx) after retries."""


# ---------------------------------------------------------------------------
# Submission-number routing
# ---------------------------------------------------------------------------


def endpoint_for(submission_number: str) -> Endpoint:
    """Which openFDA endpoint serves a submission number.

    ``P…`` → ``pma``; everything else (``K…`` 510(k)s and ``DEN…`` De Novo grants,
    which openFDA also serves from the 510(k) endpoint) → ``510k``.
    """
    return "pma" if submission_number.strip().upper().startswith("P") else "510k"


def split_pma_number(submission_number: str) -> tuple[str, str | None]:
    """Split ``P130020/S005`` into ``("P130020", "S005")``; ``("P950009", None)`` if bare."""
    base, _, supplement = submission_number.strip().upper().partition("/")
    return base, (supplement or None)


# ---------------------------------------------------------------------------
# Typed records
# ---------------------------------------------------------------------------


def _openfda_block(result: dict[str, Any]) -> dict[str, Any]:
    block = result.get("openfda")
    return block if isinstance(block, dict) else {}


@dataclass(slots=True)
class OpenFdaDevice:
    """A device record from the ``510k`` or ``pma`` endpoint.

    ``raw`` keeps the full openFDA result object for anything the silver join
    needs that is not surfaced as a field here.
    """

    submission_number: str  # the queried number, echoed back (e.g. "P130020/S005")
    endpoint: Endpoint
    product_code: str | None
    device_name: str | None
    applicant: str | None
    decision_date: str | None  # raw "YYYY-MM-DD"; silver parses
    decision_code: str | None  # e.g. SESE (510k), DENG (De Novo), APPR (PMA)
    device_class: str | None  # "1" / "2" / "3" from the openfda block
    regulation_number: str | None
    advisory_committee_description: str | None
    medical_specialty_description: str | None
    supplement_number: str | None  # PMA only
    raw: dict[str, Any]

    @classmethod
    def from_result(
        cls, endpoint: Endpoint, submission_number: str, result: dict[str, Any]
    ) -> OpenFdaDevice:
        openfda = _openfda_block(result)
        return cls(
            submission_number=submission_number,
            endpoint=endpoint,
            product_code=result.get("product_code"),
            device_name=(
                result.get("device_name") or result.get("trade_name") or openfda.get("device_name")
            ),
            applicant=result.get("applicant"),
            decision_date=result.get("decision_date"),
            decision_code=result.get("decision_code"),
            device_class=openfda.get("device_class"),
            regulation_number=openfda.get("regulation_number") or result.get("regulation_number"),
            advisory_committee_description=result.get("advisory_committee_description"),
            medical_specialty_description=openfda.get("medical_specialty_description"),
            supplement_number=result.get("supplement_number"),
            raw=result,
        )


@dataclass(slots=True)
class OpenFdaClassification:
    """A product-code record from the ``classification`` endpoint."""

    product_code: str | None
    device_name: str | None
    device_class: str | None
    regulation_number: str | None
    medical_specialty_description: str | None
    definition: str | None
    raw: dict[str, Any]

    @classmethod
    def from_result(cls, result: dict[str, Any]) -> OpenFdaClassification:
        return cls(
            product_code=result.get("product_code"),
            device_name=result.get("device_name"),
            device_class=result.get("device_class"),
            regulation_number=result.get("regulation_number"),
            medical_specialty_description=result.get("medical_specialty_description"),
            definition=result.get("definition"),
            raw=result,
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class OpenFdaClient:
    """Typed access to openFDA device endpoints, with backoff and caching."""

    def __init__(self, settings: Settings | None = None, *, client: httpx.Client | None = None):
        self._settings = settings or get_settings()
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self._settings.openfda_base_url,
            timeout=self._settings.http_timeout_seconds,
            headers={"User-Agent": self._settings.http_user_agent},
            follow_redirects=True,
        )
        self._memo: dict[str, Any] = {}
        self._cache_dir = (
            Path(self._settings.openfda_cache_dir) if self._settings.openfda_cache_dir else None
        )

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OpenFdaClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- public API --------------------------------------------------------
    def fetch_submission(self, submission_number: str) -> OpenFdaDevice | None:
        """Fetch one device record by submission number, or ``None`` if openFDA has none."""
        submission = submission_number.strip().upper()
        key = f"sub:{submission}"
        hit, value = self._cache_get(key)
        if hit:
            return value if value is None else OpenFdaDevice.from_result(*value)

        endpoint = endpoint_for(submission)
        supplement: str | None = None
        if endpoint == "pma":
            base, supplement = split_pma_number(submission)
            if supplement:
                search = f'pma_number:"{base}" AND supplement_number:"{supplement}"'
                limit = 1
            else:
                search = f'pma_number:"{base}"'
                limit = 25  # a base number returns every supplement; pick the base row
        else:
            search = f'k_number:"{submission}"'
            limit = 1

        results = self._get(f"/device/{endpoint}.json", search, limit)
        result = self._select(results, endpoint, supplement)
        if result is None:
            self._cache_put(key, None)
            return None
        self._cache_put(key, (endpoint, submission, result))
        return OpenFdaDevice.from_result(endpoint, submission, result)

    def fetch_classification(self, product_code: str) -> OpenFdaClassification | None:
        """Fetch the classification record for a product code, or ``None``."""
        code = product_code.strip().upper()
        key = f"cls:{code}"
        hit, value = self._cache_get(key)
        if hit:
            return value if value is None else OpenFdaClassification.from_result(value)

        results = self._get("/device/classification.json", f'product_code:"{code}"', 1)
        result = results[0] if results else None
        self._cache_put(key, result)
        return OpenFdaClassification.from_result(result) if result is not None else None

    # -- selection ---------------------------------------------------------
    @staticmethod
    def _select(
        results: list[dict[str, Any]] | None, endpoint: Endpoint, supplement: str | None
    ) -> dict[str, Any] | None:
        """Pick the one row we asked for.

        A PMA query returns every supplement of the base number, so filter to the
        requested one client-side rather than trusting the row order: a specific
        supplement matches ``supplement_number`` exactly (``None`` if absent); a
        bare number wants the base approval (empty ``supplement_number``).
        """
        if not results:
            return None
        if endpoint == "pma":
            if supplement:
                for row in results:
                    if (row.get("supplement_number") or "").upper() == supplement.upper():
                        return row
                return None  # the requested supplement is not in the response
            for row in results:
                if not row.get("supplement_number"):
                    return row
        return results[0]

    # -- HTTP with backoff -------------------------------------------------
    def _get(self, path: str, search: str, limit: int) -> list[dict[str, Any]] | None:
        """GET an openFDA endpoint. Returns the results list, or ``None`` for not-found.

        404 / ``NOT_FOUND`` → ``None`` (no record, not an error). 429 and 5xx and
        network errors are retried with exponential backoff; a 400 is a bad query
        (our bug) and raises. Exhausting retries raises ``OpenFdaUnavailableError``.
        """
        params: dict[str, Any] = {"search": search, "limit": limit}
        if self._settings.openfda_api_key:
            params["api_key"] = self._settings.openfda_api_key

        settings = self._settings
        for attempt in range(settings.http_max_retries):
            retryable = True
            try:
                response = self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                logger.warning("Network error calling openFDA %s: %s", path, exc)
            else:
                code = response.status_code
                if code == 200:
                    payload = response.json()
                    return payload.get("results", []) or []
                if code == 404:
                    return None  # openFDA's "no matches" -- a normal miss
                if code == 400:
                    raise OpenFdaError(
                        f"openFDA rejected the query as malformed (400): search={search!r}"
                    )
                if code != 429 and code < 500:
                    logger.info("openFDA %s returned %s; treating as no record", path, code)
                    return None
                logger.warning("openFDA %s returned %s", path, code)
                retryable = code == 429 or code >= 500

            if retryable and attempt < settings.http_max_retries - 1:
                backoff = settings.http_backoff_seconds * (2**attempt)
                if backoff:
                    time.sleep(backoff)
                continue
            break

        raise OpenFdaUnavailableError(
            f"openFDA {path} did not succeed after {settings.http_max_retries} attempts "
            f"(search={search!r})."
        )

    # -- cache -------------------------------------------------------------
    def _cache_get(self, key: str) -> tuple[bool, Any]:
        """Return ``(hit, value)``. ``value`` is the cached payload (or ``None`` for a miss)."""
        if key in self._memo:
            return True, self._memo[key]
        path = self._disk_path(key)
        if path is not None and path.exists():
            payload = json.loads(path.read_text())
            value = self._decode_disk(key, payload)
            self._memo[key] = value
            return True, value
        return False, None

    def _cache_put(self, key: str, value: Any) -> None:
        self._memo[key] = value
        path = self._disk_path(key)
        if path is None or value is None:
            return  # misses are memoised in process but not persisted
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._encode_disk(key, value)))

    def _disk_path(self, key: str) -> Path | None:
        if self._cache_dir is None:
            return None
        safe = key.replace(":", "_").replace("/", "_")
        return self._cache_dir / f"{safe}.json"

    @staticmethod
    def _encode_disk(key: str, value: Any) -> Any:
        # Submission values are (endpoint, submission, result); classification is a dict.
        if key.startswith("sub:"):
            endpoint, submission, result = value
            return {"endpoint": endpoint, "submission_number": submission, "result": result}
        return {"result": value}

    @staticmethod
    def _decode_disk(key: str, payload: dict[str, Any]) -> Any:
        if key.startswith("sub:"):
            return (payload["endpoint"], payload["submission_number"], payload["result"])
        return payload["result"]
