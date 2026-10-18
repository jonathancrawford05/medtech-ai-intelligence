"""What the model is asked for: the output schema and the prompt (ADR 0022 Decision 2).

The schema is the spike's (`tests/fixtures/llm_extraction/prompt_4d-spike-4.json`),
changed where the first gold set said it was wrong (finding 0023):

- every metric names the device it belongs to (``applies_to``): the spike credited a
  predicate's specificity to the subject device (K213971);
- error measures are their own names (``mae``, ``rmse``, ``arms``,
  ``mean_difference``) and method concordance is ``agreement``, never ``accuracy``;
- a confidence interval says which side it bounds (``ci_sided``), so a one-sided bound
  is never padded into an invented two-sided interval (K182149, K213657);
- a stated threshold or acceptance criterion is not a result (``is_result``, K241633);
- no "at most N" counts anywhere (they invite padding);
- the endpoint's risk call needs a future event within a time window.

There is no page field. The model gives a quote; `checks` finds it and records the
page (asked for a page, the model was wrong about a third of the time).

The JSON schema sent in Ollama's ``format`` is generated from these models
(`json_schema`), so the prompt and the parser cannot drift apart. ``bool | None``
everywhere: unknown is not false.
"""

from __future__ import annotations

import json
import typing as t
from typing import Any

from pydantic import BaseModel, ConfigDict

PROMPT_VERSION = "4d.1"

Measure = t.Literal[
    "sensitivity",
    "specificity",
    "auc",
    "ppv",
    "npv",
    "accuracy",
    "precision",
    "recall",
    "f1",
    "dice",
    "kappa",
    "icc",
    "correlation",
    "agreement",
    "mae",
    "rmse",
    "arms",
    "mean_difference",
    "other",
]
AppliesTo = t.Literal["subject_device", "predicate", "original_device"]
Setting = t.Literal["standalone", "reader_with_device", "reader_without_device", "other"]
CiSided = t.Literal["two", "lower", "upper"]
StudyDesign = t.Literal["retrospective", "prospective", "mixed", "not_stated"]
SampleUnit = t.Literal[
    "patients", "images", "studies", "cases", "recordings", "other", "not_stated"
]
ClinicalEndpoint = t.Literal[
    "mortality",
    "mace",
    "hospitalisation",
    "deterioration_risk",
    "detection_diagnosis",
    "measurement_quantification",
    "monitoring",
    "triage_notification",
    "image_quality",
    "other",
    "not_stated",
]
# The endpoint values that make a curation lead, never a flag (handoff §3 rule 8).
RISK_ENDPOINTS = frozenset({"mortality", "mace", "hospitalisation", "deterioration_risk"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Metric(_Strict):
    metric: Measure
    label: str
    context: str | None
    applies_to: AppliesTo
    setting: Setting
    value: float
    ci_low: float | None
    ci_high: float | None
    ci_sided: CiSided | None
    is_result: bool
    quote: str


class BoolFact(_Strict):
    value: bool | None
    quote: str | None


class StudyDesignFact(_Strict):
    value: StudyDesign
    quote: str | None


class SampleSizeFact(_Strict):
    value: int | None
    unit: SampleUnit
    quote: str | None


class EndpointFact(_Strict):
    value: ClinicalEndpoint
    quote: str | None


class ExtractionOutput(_Strict):
    """One document's answer, exactly as the model must return it."""

    performance_reported: BoolFact
    metrics: list[Metric]
    study_design: StudyDesignFact
    sample_size: SampleSizeFact
    multi_site: BoolFact
    demographics_disclosed: BoolFact
    clinical_endpoint: EndpointFact


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            return _inline(defs[node["$ref"].rsplit("/", 1)[-1]], defs)
        return {k: _inline(v, defs) for k, v in node.items() if k not in ("$defs", "title")}
    if isinstance(node, list):
        return [_inline(v, defs) for v in node]
    return node


def json_schema() -> dict[str, Any]:
    """`ExtractionOutput` as a self-contained JSON schema (no ``$ref``) for ``format``."""
    raw = ExtractionOutput.model_json_schema()
    return _inline(raw, raw.get("$defs", {}))


SYSTEM_PROMPT = """\
You extract facts from FDA 510(k)/De Novo summary documents for AI medical devices.
Rules:
- Use ONLY the document text given. Never write a figure, name or sentence that is not in the
  document. These instructions are not part of the document: never quote them.
- Every non-null value needs a quote: ONE contiguous passage copied exactly, character for
  character, from one place in the text, with its spacing and punctuation. Never add a label
  or heading in front of it, never join pieces from different places, never fix typos.
  Keep quotes short (one sentence or one table row, under 300 characters). The quote must
  itself contain the value it supports, written as the document writes it.
- If the document does not state something, use null (or "not_stated") with quote null.
  Unknown is not false: use false only when the text explicitly says so.
- performance_reported: true if the summary reports quantitative performance from standalone
  device testing or a clinical/reader study.
- metrics: each distinct performance figure the document states, once; an empty list if there
  are none. Never list the same figure twice. value, ci_low and ci_high are copied exactly as
  the document writes them (98.1% stays 98.1, never 0.981).
  label = the measure's name as the document writes it. metric = the matching name from the
  list, and only a name the document itself uses for that measure; otherwise "other".
  Error measures keep their own names: mean absolute error is mae, root-mean-square error is
  rmse, average root-mean-square error is arms, a mean difference or bias is mean_difference;
  never accuracy. Agreement or concordance between two methods is agreement, never accuracy.
  A positive predictive value is ppv and a false-positive reduction is other, never
  specificity.
  context = the dataset, subgroup, model or threshold the figure belongs to, as the document
  names it (for example a dataset name above a list of results), or null.
  applies_to = whose figure it is: subject_device (the device this summary is for),
  predicate (a predicate or reference device), original_device (the earlier version of a
  modified device). Read table column headings for this.
  is_result = true for a figure the testing achieved; false for a target, threshold,
  hypothesis or acceptance criterion ("within 3 BPM", "lower bound above 80%").
  Confidence intervals: ci_sided = two when the document gives both bounds, lower or upper
  when it gives one bound (for example "a lower one-sided 95% CI of 87.8%": ci_low 87.8,
  ci_high null, ci_sided lower). Never fill in a bound the document does not write. With no
  interval, ci_low, ci_high and ci_sided are null.
  Skip read times, lead times and other non-accuracy figures.
  setting: "standalone" for the device alone, "reader_with_device" / "reader_without_device"
  for clinicians in a reader study.
- study_design / sample_size / multi_site describe the primary performance test dataset.
  sample_size counts patients, recordings, images, studies or cases in that test, never
  sites, hospitals, units or readers, and is a number the document writes (never a sum you
  work out); unit says which.
- demographics_disclosed: true only with a quote that reports the test data by age, sex,
  race/ethnicity or similar subgroups; false only with a quote saying they were not reported;
  otherwise null.
- clinical_endpoint: what the device's output is about; quote its indication or intended-use
  sentence. deterioration_risk only when the output predicts a future event within a time
  window (for example heart-failure decompensation within 30 days, hypotension within
  15 minutes); use mortality, mace or hospitalisation when the output names that outcome.
  A trial's outcome endpoint, a "risk assessment" that measures anatomy or physiology now,
  and a status display are not deterioration_risk. detection_diagnosis: finds or classifies a
  condition present now. measurement_quantification: measures anatomy or physiology
  (including fractional flow reserve). monitoring: displays or tracks signals with no risk or
  diagnostic output. triage_notification: prioritises a worklist or notifies a specialist."""


def user_message(device_name: str, submission_number: str, pages: t.Sequence[Any]) -> str:
    """The user turn: a device header, then each page as ``=== PAGE n ===``."""
    head = f"Device: {device_name} ({submission_number})"
    body = "\n\n".join(f"=== PAGE {p.page} ===\n{p.text}" for p in pages)
    return f"{head}\n\n{body}"


def prompt_spec() -> dict[str, Any]:
    """The prompt as recorded beside a run (the shape of the spike's prompt file)."""
    return {"prompt_version": PROMPT_VERSION, "system": SYSTEM_PROMPT, "schema": json_schema()}


def parse_output(content: str) -> ExtractionOutput:
    """The model's raw ``content`` -> a validated answer (raises on any mismatch)."""
    return ExtractionOutput.model_validate(json.loads(content))
