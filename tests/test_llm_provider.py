"""The schema, the prompt and the Ollama provider (PR 4D; ADR 0022 Decisions 2 and 5).

Ollama is unreachable here, so the HTTP layer is mocked with respx in the shape of
Ollama's documented ``/api/chat`` and ``/api/tags`` replies. The answers in those
mocks are synthetic, valid ``4d.1`` JSON (the recorded answers are ``4d-spike-4``
and are exercised by the checks); one real call sits behind ``live_ollama``.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from registry.config.settings import Settings
from registry.llm_extraction import schema as sc
from registry.llm_extraction.pages import Page
from registry.llm_extraction.provider import (
    ExtractionRequest,
    ModelCall,
    OllamaProvider,
    ProviderError,
    extract,
)

BASE = "http://ollama.test:11434"
DIGEST = "bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8"
LLM = Path(__file__).parent / "fixtures" / "llm_extraction"


def _answer(**overrides) -> dict:
    body = {
        "performance_reported": {"value": True, "quote": "sensitivity was 93.6%"},
        "metrics": [
            {
                "metric": "sensitivity",
                "label": "sensitivity",
                "context": None,
                "applies_to": "subject_device",
                "setting": "standalone",
                "value": 93.6,
                "ci_low": None,
                "ci_high": None,
                "ci_sided": None,
                "is_result": True,
                "quote": "sensitivity was 93.6%",
            }
        ],
        "study_design": {"value": "retrospective", "quote": "a retrospective study"},
        "sample_size": {"value": 400, "unit": "patients", "quote": "400 patients"},
        "multi_site": {"value": None, "quote": None},
        "demographics_disclosed": {"value": None, "quote": None},
        "clinical_endpoint": {"value": "detection_diagnosis", "quote": "detects AF"},
    }
    body.update(overrides)
    return body


def _chat(content: str, *, done_reason: str = "stop", eval_count: int = 350) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "qwen3:14b",
            "message": {"role": "assistant", "content": content},
            "done": True,
            "done_reason": done_reason,
            "prompt_eval_count": 6000,
            "eval_count": eval_count,
            "eval_duration": int(eval_count / 8.0 * 1e9),
        },
    )


def _tags() -> httpx.Response:
    return httpx.Response(
        200, json={"models": [{"name": "qwen3:14b", "model": "qwen3:14b", "digest": DIGEST}]}
    )


def _settings(**kw) -> Settings:
    return Settings(ollama_base_url=BASE, **kw)


def _request() -> ExtractionRequest:
    return ExtractionRequest(
        "K240795", "ECG App", [Page(13, "The study ... sensitivity was 93.6%")]
    )


class TestSchema:
    def test_is_self_contained_for_ollamas_format(self):
        text = json.dumps(sc.json_schema())
        assert "$ref" not in text and "$defs" not in text

    def test_every_metric_field_is_required(self):
        metric = sc.json_schema()["properties"]["metrics"]["items"]
        assert set(metric["required"]) == set(sc.Metric.model_fields)
        for name in ("applies_to", "is_result", "ci_sided"):
            assert name in metric["properties"]

    def test_error_measures_and_agreement_are_names_of_their_own(self):
        names = set(sc.Metric.model_fields["metric"].annotation.__args__)
        assert {"mae", "rmse", "arms", "mean_difference", "agreement"} <= names

    def test_the_prompt_never_asks_for_a_page_or_a_count(self):
        prompt = sc.SYSTEM_PROMPT.lower()
        assert "at most" not in prompt
        assert "page" not in json.dumps(sc.json_schema()).lower()

    def test_the_endpoint_is_an_ordered_decision_with_risk_indices_first(self):
        prompt = " ".join(sc.SYSTEM_PROMPT.split())
        assert "an index or score of that risk, with or without a time window" in prompt
        assert prompt.index("1. The output is the patient's risk") < prompt.index(
            "2. The output measures one physical quantity"
        )

    def test_a_composite_index_is_never_a_measurement(self):
        """4d.2 read Rothman's "compute a patient status index" as rule 2 (K172959)."""
        prompt = " ".join(sc.SYSTEM_PROMPT.split())
        assert "A composite index or score combining several vital signs" in prompt
        assert "never this rule: it is rule 1 when the document ties it to risk" in prompt
        assert "not_stated only when it has none" in prompt

    def test_the_rules_carry_none_of_the_wording_4d1_leaked(self):
        """K212219 quoted "predicts a future episode ..." back from 4d.1's own rule."""
        assert "predicts a future" not in sc.SYSTEM_PROMPT

    def test_a_valid_answer_parses_and_extras_are_refused(self):
        assert sc.parse_output(json.dumps(_answer())).metrics[0].value == 93.6
        with pytest.raises(ValueError):
            sc.parse_output(json.dumps(_answer(page=3)))

    def test_the_user_message_labels_pages(self):
        msg = sc.user_message("ECG App", "K240795", [Page(13, "text")])
        assert msg == "Device: ECG App (K240795)\n\n=== PAGE 13 ===\ntext"


class TestOllamaRequest:
    @respx.mock
    def test_sends_the_schema_the_cap_and_no_thinking(self):
        respx.get(f"{BASE}/api/tags").mock(return_value=_tags())
        chat = respx.post(f"{BASE}/api/chat").mock(return_value=_chat(json.dumps(_answer())))
        result = extract(OllamaProvider(_settings()), _request())
        assert result.status == "ok"
        body = json.loads(chat.calls.last.request.content)
        assert body["model"] == "qwen3:14b"
        assert body["format"] == sc.json_schema()
        assert body["stream"] is False
        assert body["think"] is False
        assert body["options"] == {
            "temperature": 0,
            "seed": 42,
            "num_ctx": 16384,
            "num_predict": 3000,
        }
        assert body["messages"][0] == {"role": "system", "content": sc.SYSTEM_PROMPT}
        assert "=== PAGE 13 ===" in body["messages"][1]["content"]

    @respx.mock
    def test_records_the_full_digest_and_speed(self):
        respx.get(f"{BASE}/api/tags").mock(return_value=_tags())
        respx.post(f"{BASE}/api/chat").mock(return_value=_chat(json.dumps(_answer())))
        result = extract(OllamaProvider(_settings()), _request())
        assert result.model_digest == DIGEST
        assert result.prompt_version == sc.PROMPT_VERSION
        assert result.gen_tok_s == 8.0
        assert result.eval_count == 350
        assert result.options["think"] is False

    @respx.mock
    def test_no_think_field_for_a_model_without_the_switch(self):
        respx.get(f"{BASE}/api/tags").mock(
            return_value=httpx.Response(
                200, json={"models": [{"name": "gpt-oss:20b", "digest": "abc"}]}
            )
        )
        chat = respx.post(f"{BASE}/api/chat").mock(return_value=_chat(json.dumps(_answer())))
        extract(OllamaProvider(_settings(llm_think="omit"), model="gpt-oss:20b"), _request())
        assert "think" not in json.loads(chat.calls.last.request.content)

    @respx.mock
    def test_a_thinking_level_is_sent_as_given(self):
        """gpt-oss takes a level, not a boolean."""
        respx.get(f"{BASE}/api/tags").mock(
            return_value=httpx.Response(
                200, json={"models": [{"name": "gpt-oss:20b", "digest": "abc"}]}
            )
        )
        chat = respx.post(f"{BASE}/api/chat").mock(return_value=_chat(json.dumps(_answer())))
        extract(OllamaProvider(_settings(llm_think="low"), model="gpt-oss:20b"), _request())
        assert json.loads(chat.calls.last.request.content)["think"] == "low"

    @respx.mock
    def test_a_model_not_pulled_stops_the_run(self):
        respx.get(f"{BASE}/api/tags").mock(return_value=httpx.Response(200, json={"models": []}))
        with pytest.raises(ProviderError, match="not pulled"):
            OllamaProvider(_settings()).digest()

    @respx.mock
    def test_an_unreachable_server_stops_the_run(self):
        respx.get(f"{BASE}/api/tags").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(ProviderError, match="not reachable"):
            OllamaProvider(_settings()).digest()


class TestRetryThenSkip:
    @respx.mock
    def test_a_capped_answer_is_retried_once_then_skipped(self):
        """K230842's runaway: 9,258 tokens in the spike; here it stops at the cap."""
        respx.get(f"{BASE}/api/tags").mock(return_value=_tags())
        chat = respx.post(f"{BASE}/api/chat").mock(
            return_value=_chat('{"metrics": [', done_reason="length", eval_count=3000)
        )
        result = extract(OllamaProvider(_settings()), _request())
        assert chat.call_count == 2
        assert result.status == "skipped_output_cap"
        assert result.attempts == 2
        assert result.output is None

    @respx.mock
    def test_an_invalid_answer_then_a_valid_one_is_kept(self):
        respx.get(f"{BASE}/api/tags").mock(return_value=_tags())
        respx.post(f"{BASE}/api/chat").mock(
            side_effect=[_chat("not json"), _chat(json.dumps(_answer()))]
        )
        result = extract(OllamaProvider(_settings()), _request())
        assert result.status == "ok"
        assert result.attempts == 2
        assert result.errors and "JSONDecodeError" in result.errors[0]

    @respx.mock
    def test_an_answer_off_the_schema_is_invalid(self):
        respx.get(f"{BASE}/api/tags").mock(return_value=_tags())
        respx.post(f"{BASE}/api/chat").mock(return_value=_chat(json.dumps({"metrics": []})))
        result = extract(OllamaProvider(_settings()), _request())
        assert result.status == "skipped_invalid"
        assert result.attempts == 2

    @respx.mock
    def test_server_errors_are_retried_then_skipped(self):
        respx.get(f"{BASE}/api/tags").mock(return_value=_tags())
        chat = respx.post(f"{BASE}/api/chat").mock(return_value=httpx.Response(500))
        result = extract(OllamaProvider(_settings()), _request())
        assert chat.call_count == 2
        assert result.status == "skipped_error"

    def test_hitting_the_token_count_counts_as_capped(self):
        """Older servers may not send done_reason; eval_count at the cap is enough."""

        class Capped:
            model = "m"

            def digest(self):
                return "d"

            def options(self):
                return {"num_predict": 100}

            def generate(self, system, user, json_schema):
                return ModelCall(json.dumps(_answer()), done_reason=None, eval_count=100)

        assert extract(Capped(), _request()).status == "skipped_output_cap"

    def test_only_the_current_prompt_can_be_sent(self):
        req = ExtractionRequest("K1", "x", [Page(1, "t")], prompt_version="4d-spike-4")
        with pytest.raises(ValueError, match="recorded responses only"):
            extract(None, req)  # type: ignore[arg-type]

    def test_a_request_needs_pages(self):
        with pytest.raises(ValueError, match="no pages"):
            extract(None, ExtractionRequest("K1", "x", []))  # type: ignore[arg-type]


@pytest.mark.live_ollama
def test_live_one_document_on_the_local_model():
    """On the Mac: `uv run pytest -m live_ollama` with Ollama running and qwen3:14b pulled.

    One small real document (GHI, K231038). Proves the request shape against a real
    server: the answer validates against the 4d.1 schema and carries the digest.
    """
    doc = json.loads((LLM / "documents" / "K231038.json").read_text())
    pages = [Page(p["page"], p["text"]) for p in doc["pages"]]
    result = extract(
        OllamaProvider(Settings()),
        ExtractionRequest(doc["submission_number"], doc["device_name"], pages),
    )
    assert result.status == "ok", result.errors
    assert len(result.model_digest) == 64
    assert result.output is not None
