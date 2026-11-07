"""The thin model interface (handoff §3 rule 7; ADR 0022 Decision 5).

One function, `extract`: (pages, schema, prompt version) -> validated JSON plus the
model metadata a row needs for its audit trail. A `Provider` only turns a system
and a user message into raw text; everything that decides whether the text is
usable lives here, so swapping ``qwen3:14b`` for ``gpt-oss:20b`` (or Ollama for
something else) changes no rule.

What finding 0023 measured, and this module encodes:

- temperature 0, a fixed seed, ``think: false`` for ``qwen3:14b``, the JSON schema
  from `schema.ExtractionOutput` in Ollama's ``format``;
- an output cap (``num_predict``, ~3,000 tokens): 3 of 153 spike documents ran past
  4,000 tokens, one for 24 minutes;
- one retry, then skip: a capped, unparsable or failed answer is retried once and
  then recorded as skipped, so one document can never stall a run;
- the full model digest on every result, read from the server, never assumed.

Model calls happen only on a host running Ollama (the maintainer's Mac). Tests use
recorded responses and mocked HTTP; real calls are marked ``live_ollama``.
"""

from __future__ import annotations

import time
import typing as t
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from registry.config.settings import Settings, get_settings
from registry.llm_extraction import schema as sc
from registry.llm_extraction.pages import Page

ResultStatus = t.Literal[
    "ok",
    "skipped_output_cap",  # hit num_predict on every attempt
    "skipped_invalid",  # not JSON, or not the schema, on every attempt
    "skipped_error",  # the server failed (timeout, 5xx) on every attempt
]


class ProviderError(RuntimeError):
    """The model server is unusable for the whole run (not reachable, model not pulled)."""


@dataclass(frozen=True)
class ExtractionRequest:
    submission_number: str
    device_name: str
    pages: list[Page]
    prompt_version: str = sc.PROMPT_VERSION


@dataclass(frozen=True)
class ModelCall:
    """One attempt, as the server answered it."""

    content: str | None
    done_reason: str | None = None
    eval_count: int | None = None
    prompt_eval_count: int | None = None
    wall_seconds: float | None = None
    gen_tok_s: float | None = None
    error: str | None = None


@dataclass
class ExtractionResult:
    submission_number: str
    status: ResultStatus
    model: str
    model_digest: str
    prompt_version: str
    options: dict[str, Any]
    attempts: int
    content: str | None = None  # the raw text of the attempt kept (the last one)
    output: sc.ExtractionOutput | None = None
    done_reason: str | None = None
    eval_count: int | None = None
    prompt_eval_count: int | None = None
    wall_seconds: float | None = None  # summed over attempts
    gen_tok_s: float | None = None  # of the last attempt
    error: str | None = None
    errors: list[str] = field(default_factory=list)


class Provider(Protocol):
    """What `extract` needs from a model server."""

    model: str

    def digest(self) -> str:
        """The served model's full content digest."""
        ...

    def options(self) -> dict[str, Any]:
        """The generation settings sent with every call, as recorded on each row."""
        ...

    def generate(self, system: str, user: str, json_schema: dict[str, Any]) -> ModelCall: ...


def _capped(call: ModelCall, cap: int | None) -> bool:
    if call.done_reason == "length":
        return True
    return cap is not None and call.eval_count is not None and call.eval_count >= cap


def extract(
    provider: Provider, request: ExtractionRequest, *, max_attempts: int = 2
) -> ExtractionResult:
    """Ask the model about one document; validate; retry once; else skip.

    Only the current `schema.PROMPT_VERSION` can be asked for: the system text and
    the schema are defined by it. Every attempt is identical (temperature 0, fixed
    seed): a retry covers a transient failure, not a different answer.
    """
    if request.prompt_version != sc.PROMPT_VERSION:
        raise ValueError(
            f"prompt {request.prompt_version!r} cannot be sent; this build sends "
            f"{sc.PROMPT_VERSION!r} (older versions are scored from recorded responses only)"
        )
    if not request.pages:
        raise ValueError(f"{request.submission_number}: no pages to send")
    options = provider.options()
    cap = options.get("num_predict")
    user = sc.user_message(request.device_name, request.submission_number, request.pages)
    json_schema = sc.json_schema()
    digest = provider.digest()

    result = ExtractionResult(
        submission_number=request.submission_number,
        status="skipped_error",
        model=provider.model,
        model_digest=digest,
        prompt_version=request.prompt_version,
        options=options,
        attempts=0,
    )
    wall = 0.0
    for _ in range(max_attempts):
        result.attempts += 1
        call = provider.generate(sc.SYSTEM_PROMPT, user, json_schema)
        wall += call.wall_seconds or 0.0
        result.content = call.content
        result.done_reason = call.done_reason
        result.eval_count = call.eval_count
        result.prompt_eval_count = call.prompt_eval_count
        result.gen_tok_s = call.gen_tok_s
        result.wall_seconds = wall
        if call.error is not None:
            result.status, result.error = "skipped_error", call.error
        elif _capped(call, cap):
            result.status = "skipped_output_cap"
            result.error = f"output cap reached ({call.eval_count} tokens)"
        else:
            try:
                result.output = sc.parse_output(call.content or "")
            except (ValueError, ValidationError) as exc:
                result.status = "skipped_invalid"
                result.error = f"{type(exc).__name__}: {str(exc)[:300]}"
            else:
                result.status, result.error = "ok", None
                return result
        result.errors.append(result.error or "")
    return result


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


_THINK: dict[str, bool | str | None] = {"false": False, "true": True, "omit": None}


class OllamaProvider:
    """`Provider` over Ollama's ``/api/chat`` (non-streaming) and ``/api/tags``."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        model: str | None = None,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings or get_settings()
        self.model = model or self.settings.llm_model
        self._client = client or httpx.Client(
            base_url=self.settings.ollama_base_url,
            timeout=self.settings.llm_timeout_seconds,
        )
        self._clock = clock
        self._digest: str | None = None

    def options(self) -> dict[str, Any]:
        """The generation settings, as recorded on every row. ``think`` is sent as
        Ollama's top-level switch (qwen3 thinks unless told not to; gpt-oss takes a level); ``None`` omits it."""
        s = self.settings
        return {
            "temperature": 0,
            "seed": s.llm_seed,
            "num_ctx": s.llm_num_ctx,
            "num_predict": s.llm_num_predict,
            "think": _THINK.get(s.llm_think, s.llm_think),
        }

    def digest(self) -> str:
        if self._digest is None:
            try:
                response = self._client.get("/api/tags")
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise ProviderError(
                    f"Ollama is not reachable at {self.settings.ollama_base_url}: {exc}"
                ) from exc
            names = {self.model, f"{self.model}:latest"}
            for entry in response.json().get("models", []):
                if entry.get("name") in names or entry.get("model") in names:
                    self._digest = entry["digest"]
                    break
            else:
                raise ProviderError(
                    f"model {self.model!r} is not pulled (`ollama pull {self.model}`)"
                )
        return self._digest

    def generate(self, system: str, user: str, json_schema: dict[str, Any]) -> ModelCall:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "format": json_schema,
            "stream": False,
        }
        options = self.options()
        think = options.pop("think")
        body["options"] = options
        if think is not None:
            body["think"] = think
        started = self._clock()
        try:
            response = self._client.post("/api/chat", json=body)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return ModelCall(
                None, wall_seconds=self._clock() - started, error=f"{type(exc).__name__}: {exc}"
            )
        wall = self._clock() - started
        eval_count = data.get("eval_count")
        eval_ns = data.get("eval_duration")
        tok_s = eval_count / (eval_ns / 1e9) if eval_count and eval_ns else None
        return ModelCall(
            content=(data.get("message") or {}).get("content"),
            done_reason=data.get("done_reason"),
            eval_count=eval_count,
            prompt_eval_count=data.get("prompt_eval_count"),
            wall_seconds=round(wall, 1),
            gen_tok_s=None if tok_s is None else round(tok_s, 1),
        )
