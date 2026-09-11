"""openFDA client tests -- fixture-driven, no live network.

Fixtures under ``tests/fixtures/openfda/`` are real openFDA responses captured
2026-09-08 (see that directory's ``PROVENANCE.md`` and finding 0003). The real
endpoints are exercised only by the ``@pytest.mark.live_network`` test, which CI
never runs.

The captured facts these tests pin down (all verified against the live API):
- De Novo (``DEN…``) grants are served by the ``510k`` endpoint, not a ``de_novo``
  one, and carry ``decision_code == "DENG"``.
- A PMA supplement is a separate field: ``P130020/S005`` splits into
  ``pma_number=P130020`` + ``supplement_number=S005``.
- A submission openFDA has no record for comes back as ``None``, not an error.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from registry.config.settings import Settings
from registry.ingest import openfda_client as mod


@pytest.fixture
def openfda_dir(fixtures_dir):
    return fixtures_dir / "openfda"


def _body(openfda_dir, name: str) -> dict:
    return json.loads((openfda_dir / name).read_text())


K510 = r"https://api\.fda\.gov/device/510k\.json"
PMA = r"https://api\.fda\.gov/device/pma\.json"
CLASSIF = r"https://api\.fda\.gov/device/classification\.json"


def _client(**overrides):
    # backoff 0 so retry tests do not sleep
    return mod.OpenFdaClient(Settings(http_backoff_seconds=0.0, **overrides))


class TestPathwayRouting:
    def test_k_number_routes_to_510k(self):
        assert mod.endpoint_for("K253628") == "510k"

    def test_den_number_routes_to_510k(self):
        # De Novo grants live in the 510(k) endpoint -- there is no de_novo endpoint.
        assert mod.endpoint_for("DEN250057") == "510k"

    def test_p_number_routes_to_pma(self):
        assert mod.endpoint_for("P130020") == "pma"

    def test_supplement_suffix_is_split(self):
        assert mod.split_pma_number("P130020/S005") == ("P130020", "S005")

    def test_bare_pma_number_has_no_supplement(self):
        assert mod.split_pma_number("P950009") == ("P950009", None)


class TestFetch510k:
    @respx.mock
    def test_returns_typed_record_for_a_k_number(self, openfda_dir):
        route = respx.get(url__regex=K510).mock(
            return_value=httpx.Response(200, json=_body(openfda_dir, "openfda_510k_K253628.json"))
        )
        rec = _client().fetch_submission("K253628")
        assert route.called
        assert rec is not None
        assert rec.endpoint == "510k"
        assert rec.submission_number == "K253628"
        assert rec.product_code == "QIH"
        assert rec.device_class == "2"
        assert rec.regulation_number == "892.2050"
        assert rec.decision_code == "SESE"
        assert rec.decision_date == "2026-06-29"
        assert rec.device_name == "Auto-Seg (SO-0012), Spine Auto-Seg (SO-0012)"

    @respx.mock
    def test_de_novo_grant_comes_from_510k_with_deng(self, openfda_dir):
        respx.get(url__regex=K510).mock(
            return_value=httpx.Response(200, json=_body(openfda_dir, "openfda_510k_DEN250057.json"))
        )
        rec = _client().fetch_submission("DEN250057")
        assert rec is not None
        assert rec.endpoint == "510k"
        assert rec.decision_code == "DENG"
        assert rec.product_code == "SIF"
        assert rec.device_class == "2"


class TestFetchPma:
    @respx.mock
    def test_supplement_is_split_and_matched(self, openfda_dir):
        route = respx.get(url__regex=PMA).mock(
            return_value=httpx.Response(200, json=_body(openfda_dir, "openfda_pma_P130020.json"))
        )
        rec = _client().fetch_submission("P130020/S005")
        assert route.called
        # The request must carry both the base number and the supplement.
        sent = route.calls.last.request.url
        assert "P130020" in str(sent) and "S005" in str(sent)
        assert rec is not None
        assert rec.endpoint == "pma"
        assert rec.submission_number == "P130020/S005"
        assert rec.supplement_number == "S005"
        assert rec.decision_date == "2025-11-21"
        assert rec.product_code == "OTE"
        assert rec.device_class == "3"
        # openFDA PMA rows carry no specialty; silver must take it from the AI list.
        assert rec.medical_specialty_description == "Unknown"

    @respx.mock
    def test_bare_pma_number_selects_the_base_record(self, openfda_dir):
        respx.get(url__regex=PMA).mock(
            return_value=httpx.Response(200, json=_body(openfda_dir, "openfda_pma_P130020.json"))
        )
        rec = _client().fetch_submission("P130020")
        assert rec is not None
        assert rec.supplement_number in (None, "")


class TestMissingSubmission:
    @respx.mock
    def test_not_found_404_returns_none(self, openfda_dir):
        respx.get(url__regex=PMA).mock(
            return_value=httpx.Response(404, json=_body(openfda_dir, "openfda_pma_NOT_FOUND.json"))
        )
        assert _client().fetch_submission("P999999") is None

    @respx.mock
    def test_empty_results_returns_none(self):
        respx.get(url__regex=K510).mock(
            return_value=httpx.Response(
                200, json={"meta": {"results": {"total": 0}}, "results": []}
            )
        )
        assert _client().fetch_submission("K000000") is None


class TestCaching:
    @respx.mock
    def test_repeat_fetch_hits_the_network_once(self, openfda_dir):
        route = respx.get(url__regex=K510).mock(
            return_value=httpx.Response(200, json=_body(openfda_dir, "openfda_510k_K253628.json"))
        )
        client = _client()
        first = client.fetch_submission("K253628")
        second = client.fetch_submission("K253628")
        assert route.call_count == 1
        assert first == second

    @respx.mock
    def test_disk_cache_persists_across_client_instances(self, openfda_dir, tmp_path):
        route = respx.get(url__regex=K510).mock(
            return_value=httpx.Response(200, json=_body(openfda_dir, "openfda_510k_K253628.json"))
        )
        cache = str(tmp_path / "openfda_cache")
        a = _client(openfda_cache_dir=cache)
        a.fetch_submission("K253628")
        # A brand-new client with the same cache dir must not re-hit the network.
        b = _client(openfda_cache_dir=cache)
        rec = b.fetch_submission("K253628")
        assert route.call_count == 1
        assert rec is not None and rec.product_code == "QIH"


class TestBackoff:
    @respx.mock
    def test_retries_on_429_then_succeeds(self, openfda_dir):
        respx.get(url__regex=K510).mock(
            side_effect=[
                httpx.Response(429),
                httpx.Response(200, json=_body(openfda_dir, "openfda_510k_K253628.json")),
            ]
        )
        rec = _client(http_max_retries=3).fetch_submission("K253628")
        assert rec is not None and rec.product_code == "QIH"

    @respx.mock
    def test_gives_up_on_persistent_server_error(self):
        respx.get(url__regex=K510).mock(return_value=httpx.Response(503))
        with pytest.raises(mod.OpenFdaUnavailableError):
            _client(http_max_retries=2).fetch_submission("K253628")


class TestClassification:
    @respx.mock
    def test_fetches_classification_by_product_code(self, openfda_dir):
        route = respx.get(url__regex=CLASSIF).mock(
            return_value=httpx.Response(
                200, json=_body(openfda_dir, "openfda_classification_QIH.json")
            )
        )
        rec = _client().fetch_classification("QIH")
        assert route.called
        assert rec is not None
        assert rec.product_code == "QIH"
        assert rec.device_class == "2"
        assert rec.regulation_number == "892.2050"
        assert rec.medical_specialty_description == "Radiology"


class TestApiKey:
    @respx.mock
    def test_api_key_is_sent_when_configured(self, openfda_dir):
        route = respx.get(url__regex=K510).mock(
            return_value=httpx.Response(200, json=_body(openfda_dir, "openfda_510k_K253628.json"))
        )
        _client(openfda_api_key="secret-key").fetch_submission("K253628")
        assert "api_key=secret-key" in str(route.calls.last.request.url)

    @respx.mock
    def test_no_api_key_param_when_unset(self, openfda_dir):
        route = respx.get(url__regex=K510).mock(
            return_value=httpx.Response(200, json=_body(openfda_dir, "openfda_510k_K253628.json"))
        )
        _client().fetch_submission("K253628")
        assert "api_key" not in str(route.calls.last.request.url)


@pytest.mark.live_network
class TestLiveEndpoint:
    """Hits the real api.fda.gov. Never runs in CI; run with
    ``uv run pytest -m live_network`` on a network that can reach it.
    """

    def test_fetches_a_real_510k_record(self):
        rec = mod.OpenFdaClient(Settings()).fetch_submission("K253628")
        assert rec is not None
        assert rec.product_code == "QIH"

    def test_missing_submission_returns_none(self):
        assert mod.OpenFdaClient(Settings()).fetch_submission("K000000") is None
