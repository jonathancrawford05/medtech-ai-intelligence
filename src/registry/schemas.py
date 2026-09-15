"""Pydantic models and their generated Spark ``StructType`` mirrors.

Every model here has a matching Spark schema so ingestion writes land in typed
Delta tables. The mirrors are *generated* from the Pydantic models rather than
hand-written beside them -- two hand-maintained copies of the same schema drift
apart, and ``tests/test_schemas.py`` enforces field-for-field parity.
"""

from __future__ import annotations

import datetime as dt
import types
import typing as t

from pydantic import BaseModel, Field, field_validator, model_validator
from pyspark.sql.types import (
    ArrayType,
    BooleanType,
    DataType,
    DateType,
    DoubleType,
    LongType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

Pathway = t.Literal["510k", "de_novo", "pma"]
DeviceClass = t.Literal["I", "II", "III", "unclassified"]
ReviewMethod = t.Literal["human", "llm_assisted"]

_SCALARS: dict[type, DataType] = {
    str: StringType(),
    bool: BooleanType(),
    int: LongType(),
    float: DoubleType(),
    dt.date: DateType(),
    dt.datetime: TimestampType(),
}


def _unwrap_optional(annotation: t.Any) -> tuple[t.Any, bool]:
    """Split ``X | None`` into ``(X, nullable)``."""
    origin = t.get_origin(annotation)
    if origin in (t.Union, types.UnionType):
        args = [a for a in t.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
        raise TypeError(f"Unsupported union annotation: {annotation!r}")
    return annotation, False


def _spark_type(annotation: t.Any) -> DataType:
    """Map a Python annotation to a Spark ``DataType``."""
    inner, _ = _unwrap_optional(annotation)
    origin = t.get_origin(inner)

    # Literal["510k", ...] is a constrained string as far as Spark is concerned.
    if origin is t.Literal:
        if not all(isinstance(a, str) for a in t.get_args(inner)):
            raise TypeError(f"Unsupported non-string Literal: {inner!r}")
        return StringType()

    if origin in (list, t.List):  # noqa: UP006 - matching typing.List too
        (elem,) = t.get_args(inner)
        return ArrayType(_spark_type(elem), containsNull=False)

    if inner in _SCALARS:
        return _SCALARS[inner]

    raise TypeError(f"Unsupported annotation for Spark schema generation: {inner!r}")


def spark_schema_for(model: type[BaseModel]) -> StructType:
    """Generate the Spark ``StructType`` mirroring ``model``.

    Field order matches declaration order, and a field is nullable exactly when
    its annotation admits ``None``.
    """
    fields = []
    for name, info in model.model_fields.items():
        annotation = info.annotation
        _, nullable = _unwrap_optional(annotation)
        fields.append(StructField(name, _spark_type(annotation), nullable=nullable))
    return StructType(fields)


def _non_blank(value: str) -> str:
    if not value or not value.strip():
        raise ValueError("value must not be blank")
    return value.strip()


# ---------------------------------------------------------------------------
# Bronze
# ---------------------------------------------------------------------------


class BronzeFdaAiListRecord(BaseModel):
    """One row of the FDA AI-enabled device list, as pulled.

    Deliberately loose: bronze preserves what the source said, including rows we
    cannot yet parse. Cleaning happens on the way to silver.
    """

    submission_number: str
    device_name: str | None = None
    applicant_raw: str | None = None
    decision_date_raw: str | None = None
    panel_raw: str | None = None
    product_code: str | None = None
    source_url: str | None = None
    # Provenance: which pull this row came from, and what the source looked like.
    ingested_at: dt.datetime
    source_snapshot_id: str


# ---------------------------------------------------------------------------
# Silver
# ---------------------------------------------------------------------------


class DeviceRecord(BaseModel):
    """A single FDA-authorised AI-enabled device (development plan section 3)."""

    submission_number: str  # primary key, e.g. "K243456"
    device_name: str
    # Name exactly as the FDA lists it, or None when the FDA lists no company.
    # Never a substitute value: writing the device name here (as an earlier draft
    # did) is indistinguishable from a real applicant to every consumer.
    applicant_raw: str | None = None
    applicant_resolved: str | None = None  # populated by company_resolution.py
    decision_date: dt.date
    pathway: Pathway
    specialty_panel: str  # the FDA's own panel label
    specialty_category: str  # our curated taxonomy (config/specialty_taxonomy.yaml)
    product_code: str

    # PMA supplements keep their whole identifier in `submission_number`
    # (`P130020/S005`) because a supplement is its own authorisation; these carry
    # the split for openFDA lookups and device-family grouping (ADR 0012).
    pma_base_number: str | None = None
    pma_supplement_number: str | None = None

    # Optional until enrichment runs (ADR 0012). `None` means "not yet enriched",
    # which is a different claim from `False` — and for device_class, a different
    # claim from the FDA's own "unclassified".
    device_class: DeviceClass | None = None
    predicate_submission_number: str | None = None
    predicate_age_days: int | None = None
    has_pccp: bool | None = None
    pccp_summary: str | None = None
    cybersecurity_statement_present: bool | None = None

    source_url: str  # link back to the FDA record, for auditability

    @field_validator("submission_number", "product_code", mode="before")
    @classmethod
    def _upper_strip(cls, v: t.Any) -> t.Any:
        return _non_blank(v).upper() if isinstance(v, str) else v

    @field_validator("device_name", "applicant_raw", mode="before")
    @classmethod
    def _strip(cls, v: t.Any) -> t.Any:
        return _non_blank(v) if isinstance(v, str) else v


class DeviceEnrichmentRecord(BaseModel):
    """openFDA-derived facts for one submission (`silver_device_enrichment`).

    Kept out of ``DeviceRecord`` deliberately (ADR 0013): rebuilding silver is a
    cheap, offline, deterministic operation, and it stays that way only if the
    remote fetch lives in its own table. ``build_silver`` left-joins this to fill
    ``DeviceRecord.device_class``; everything else is a research surface consumers
    join to, not part of the silver contract.

    Two tiers, independently populated, so `*_found` is per tier:

    ``classification_found``
        Tier 1 -- the ``classification`` endpoint, keyed by **product code**. 181
        codes cover all 1,614 devices, so this is where ``device_class`` is worth
        getting from.
    ``submission_found``
        Tier 2 -- ``510k`` / ``pma``, keyed by **submission number**. Per-device
        facts a product code cannot carry.
    """

    submission_number: str  # FK to DeviceRecord
    enriched_at: dt.datetime

    # -- tier 2: the submission endpoint ------------------------------------
    submission_found: bool
    endpoint: str | None = None  # "510k" or "pma"
    decision_code: str | None = None  # SESE, DENG, APPR, ...
    decision_description: str | None = None
    clearance_type: str | None = None
    date_received: dt.date | None = None
    # decision_date - date_received. The curated AI list cannot express this at
    # all, and FDA review duration is a trend the underwriting audience asks for.
    review_time_days: int | None = None
    # "Summary" (a public 510(k) summary PDF exists) vs "Statement" (none does).
    # This is the fetchability flag that scopes the future PDF pass (ADR 0013).
    statement_or_summary: str | None = None
    third_party_review: bool | None = None
    expedited_review: bool | None = None
    advisory_committee_description: str | None = None
    applicant_openfda: str | None = None  # the FDA's own spelling, for cross-check

    # -- tier 1: the classification endpoint --------------------------------
    classification_found: bool
    product_code: str | None = None
    device_class_raw: str | None = None  # "1"/"2"/"3" as openFDA returns it
    unclassified_reason: str | None = None
    regulation_number: str | None = None
    classification_specialty: str | None = None  # FDA's specialty, vs our panel
    classification_definition: str | None = None  # short free text, not narrative
    # FDA-assigned structured flag. A far better stage-1 input to the two-stage
    # mortality flag (ADR 0007) than keyword matching, and it costs nothing extra.
    life_sustain_support: bool | None = None
    implant: bool | None = None

    @field_validator("submission_number", mode="before")
    @classmethod
    def _upper_strip(cls, v: t.Any) -> t.Any:
        return _non_blank(v).upper() if isinstance(v, str) else v


class EvidenceRecord(BaseModel):
    """Evidence-quality fields extracted from device summary text.

    The mortality flag is the one judgement call in an otherwise structured
    pipeline, so it is stored as two auditable stages rather than one opaque
    boolean:

    ``mortality_keyword_flag``
        Stage 1 -- cheap deterministic keyword/regex pass. Always populated.
    ``mortality_confirmed_flag``
        Stage 2 -- ``None`` until a human or a logged LLM pass reviews it, then
        True/False. Only this field may gate the gold-layer mart.
    """

    submission_number: str  # FK to DeviceRecord

    # Read from the 510(k) summary PDF, which nothing fetches yet (roadmap Issue
    # 4). `None` means "no summary has been read", which is a different claim
    # from False ("the summary reports no sensitivity/specificity") -- the same
    # distinction ADR 0012 drew for the openFDA fields.
    reports_sensitivity_specificity: bool | None = None
    sensitivity: float | None = Field(default=None, ge=0.0, le=1.0)
    specificity: float | None = Field(default=None, ge=0.0, le=1.0)
    discloses_demographics: bool | None = None
    demographic_summary: str | None = None

    # The evidence the mortality judgement was made from, verbatim.
    intended_use_text: str
    intended_use_source: str | None = None

    mortality_keyword_flag: bool
    mortality_confirmed_flag: bool | None = None
    mortality_review_method: ReviewMethod | None = None
    mortality_review_notes: str | None = None

    @field_validator("submission_number", mode="before")
    @classmethod
    def _upper_strip(cls, v: t.Any) -> t.Any:
        return _non_blank(v).upper() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _confirmation_needs_provenance(self) -> EvidenceRecord:
        if self.mortality_confirmed_flag is not None and self.mortality_review_method is None:
            raise ValueError(
                "mortality_confirmed_flag requires mortality_review_method "
                "('human' or 'llm_assisted') so the confirmation stays auditable"
            )
        return self


class CompanyRecord(BaseModel):
    """A resolved applicant/parent company."""

    resolved_name: str  # primary key
    raw_name_variants: list[str] = Field(default_factory=list)
    current_parent: str | None = None
    acquisition_history: list[str] = Field(default_factory=list)
    cumulative_device_count: int = Field(default=0, ge=0)
    primary_specialty_focus: str | None = None

    @field_validator("resolved_name", mode="before")
    @classmethod
    def _strip(cls, v: t.Any) -> t.Any:
        return _non_blank(v) if isinstance(v, str) else v


# Convenience: logical table name -> model, so callers do not hardcode schemas.
TABLE_MODELS: dict[str, type[BaseModel]] = {
    "bronze_fda_ai_list": BronzeFdaAiListRecord,
    "silver_devices": DeviceRecord,
    "silver_evidence": EvidenceRecord,
    "silver_companies": CompanyRecord,
}
