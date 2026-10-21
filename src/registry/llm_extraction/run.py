"""Model calls into bronze, checked evidence into silver (ADR 0022 Decisions 1 and 6).

Two tables, split the way the rest of the lakehouse is split:

``bronze_llm_responses``  append-only; one row per document per call, the model's
                          raw text verbatim, skipped documents included. The calls
                          are the expensive input (~2 min a document on the Mac,
                          ~70 h for every Summary), so they are kept, never redone
                          to change a rule.
``silver_llm_evidence``   rebuilt from bronze by `checks`, one row per proposed value
                          (rejected ones too), for the newest ``ok`` response per
                          (document, model digest, prompt version).

`extract_run` is the only part that calls a model, and only through a `Provider`.
It refuses an unscoped run (``--only`` or ``--limit`` is required; the handoff says
to scope runs), paces itself for the fanless Air (a rest after every
``llm_batch_size`` documents, a stop once generation falls below
``llm_min_tok_s``), writes each batch before the next starts, and resumes: a
document with a non-error response for the same digest and prompt is not re-sent.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from registry.config.settings import Settings, get_settings
from registry.llm_extraction import checks
from registry.llm_extraction import schema as sc
from registry.llm_extraction.pages import Page, PageSelection, select_pages
from registry.llm_extraction.provider import (
    ExtractionRequest,
    ExtractionResult,
    Provider,
    extract,
)
from registry.schemas import BronzeLlmResponseRecord, LlmEvidenceRecord

logger = logging.getLogger(__name__)

RESPONSES_TABLE = "bronze_llm_responses"
EVIDENCE_TABLE = "silver_llm_evidence"
DOCUMENTS_TABLE = "bronze_summary_documents"
EXTRACTION_TABLE = "silver_document_extraction"
DEVICES_TABLE = "silver_devices"

UNSCOPED_RUN = (
    "Refusing an unscoped run: pass --only K... or --limit N "
    "(all ~1,519 Summaries are ~70 h of model time; finding 0023)."
)

# Statuses that mean "this document has its answer for this model and prompt".
# skipped_error is not one: a server failure is retried by the next run.
_DONE = frozenset({"ok", "skipped_output_cap", "skipped_invalid", "skipped_no_pages"})


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def new_run_id(model: str, prompt_version: str, at: dt.datetime | None = None) -> str:
    stamp = (at or _utcnow()).strftime("%Y%m%dT%H%M%SZ")
    return f"{prompt_version}-{model.replace(':', '_').replace('/', '_')}-{stamp}"


def _sha(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


# ---------------------------------------------------------------------------
# One document
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentInput:
    """What one call needs: the bronze fetch's text and the pages 4B located."""

    submission_number: str
    device_name: str
    content_sha256: str
    page_texts: list[str]
    located_pages: list[int]  # ifu_form_pages + summary_pages

    def select(self, max_chars: int) -> PageSelection:
        return select_pages(self.page_texts, self.located_pages, max_chars=max_chars)


def response_record(
    run_id: str,
    doc: DocumentInput,
    selection: PageSelection,
    result: ExtractionResult | None,
    *,
    model: str,
    prompt_version: str,
    options: Mapping[str, Any],
    requested_at: dt.datetime,
    ingested_at: dt.datetime,
) -> BronzeLlmResponseRecord:
    """Stamp one call (or one document never sent: ``result=None``) as a bronze row."""
    common = {
        "run_id": run_id,
        "submission_number": doc.submission_number,
        "source_content_sha256": doc.content_sha256,
        "pages_sent": selection.page_numbers,
        "pages_garbled": selection.garbled,
        "pages_trimmed": selection.trimmed,
        "prompt_version": prompt_version,
        "options_json": json.dumps(dict(options), sort_keys=True),
        "requested_at": requested_at,
        "ingested_at": ingested_at,
    }
    if result is None:
        return BronzeLlmResponseRecord(
            **common,
            model=model,
            status="skipped_no_pages",
            attempts=0,
            source_snapshot_id=_sha(run_id, doc.submission_number, "skipped_no_pages")[:16],
        )
    return BronzeLlmResponseRecord(
        **common,
        model=result.model,
        model_digest=result.model_digest,
        status=result.status,
        attempts=result.attempts,
        content=result.content,
        done_reason=result.done_reason,
        eval_count=result.eval_count,
        prompt_eval_count=result.prompt_eval_count,
        wall_seconds=result.wall_seconds,
        gen_tok_s=result.gen_tok_s,
        error=result.error,
        source_snapshot_id=_sha(result.content or result.status, doc.submission_number)[:16],
    )


def evidence_records(
    response: BronzeLlmResponseRecord | Mapping[str, Any],
    pages: Sequence[Page],
    *,
    built_at: dt.datetime,
) -> list[LlmEvidenceRecord]:
    """Judge one ``ok`` response against the pages it was sent: one row per value."""
    r = response if isinstance(response, Mapping) else response.model_dump()
    if r["status"] != "ok":
        return []
    sent = set(r["pages_sent"])
    pages = [p for p in pages if p.page in sent]
    out = []
    for c in checks.check_response(r["content"], r["prompt_version"], pages):
        cand = c.candidate
        out.append(
            LlmEvidenceRecord(
                submission_number=r["submission_number"],
                run_id=r["run_id"],
                model=r["model"],
                model_digest=r["model_digest"],
                prompt_version=r["prompt_version"],
                checks_version=checks.CHECKS_VERSION,
                requested_at=r["requested_at"],
                built_at=built_at,
                source_content_sha256=r["source_content_sha256"],
                field=cand.field,
                value_text=cand.value_text,
                value_number=cand.value_number,
                value_bool=cand.value_bool,
                sample_unit=cand.sample_unit,
                metric_index=cand.index,
                metric=cand.metric,
                label=cand.label,
                context=cand.context,
                applies_to=cand.applies_to,
                setting=cand.setting,
                ci_low=cand.ci_low,
                ci_high=cand.ci_high,
                ci_sided=cand.ci_sided,
                is_result=cand.is_result,
                quote=cand.quote,
                quote_pages=c.pages,
                quote_status=c.quote_status,
                check_status=c.status,
                accepted=c.accepted,
                needs_review=c.needs_review,
                curation_lead=(
                    c.accepted
                    and cand.field == "clinical_endpoint"
                    and cand.value_text in sc.RISK_ENDPOINTS
                ),
            )
        )
    return out


def latest_ok(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The newest ``ok`` response per (document, model digest, prompt version)."""
    best: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for r in rows:
        if r["status"] != "ok":
            continue
        key = (r["submission_number"], r["model_digest"], r["prompt_version"])
        if key not in best or r["requested_at"] > best[key]["requested_at"]:
            best[key] = r
    return [best[k] for k in sorted(best)]


# ---------------------------------------------------------------------------
# Yields
# ---------------------------------------------------------------------------


@dataclass
class EvidenceYields:
    responses: int = 0
    run: str | None = None  # "model / prompt version" when the table holds several
    by_field_status: Counter[tuple[str, str]] = field(default_factory=Counter)
    curation_leads: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        head = f"{self.run}: " if self.run else ""
        out = [f"{head}responses checked: {self.responses:,}"]
        for name in checks.FIELDS:
            row = {s: n for (f, s), n in self.by_field_status.items() if f == name}
            if not row:
                continue
            accepted = sum(n for s, n in row.items() if s in checks.ACCEPT)
            flagged = sum(n for s, n in row.items() if s in checks.REVIEW)
            rejected = sum(n for s, n in row.items() if s in checks.REJECT)
            dropped = sum(n for s, n in row.items() if s in checks.DROP)
            detail = ", ".join(f"{s} {n}" for s, n in sorted(row.items()))
            out.append(
                f"  {name:<23}: accepted {accepted} ({flagged} flagged), rejected {rejected}"
                + (f", duplicates {dropped}" if dropped else "")
                + f"   [{detail}]"
            )
        if self.curation_leads:
            out.append(
                "  risk endpoints -> curation (never a flag): " + ", ".join(self.curation_leads)
            )
        return out


def summarise_runs(records: Iterable[LlmEvidenceRecord]) -> list[EvidenceYields]:
    """One summary per (model, prompt version): runs are compared, never pooled."""
    groups: dict[tuple[str, str], list[LlmEvidenceRecord]] = {}
    for rec in records:
        groups.setdefault((rec.model, rec.prompt_version), []).append(rec)
    out = []
    for (model, prompt), recs in sorted(groups.items()):
        y = summarise(recs, len({r.submission_number for r in recs}))
        y.run = f"{model} / {prompt}"
        out.append(y)
    return out


def summarise(records: Iterable[LlmEvidenceRecord], responses: int) -> EvidenceYields:
    y = EvidenceYields(responses=responses)
    for rec in records:
        y.by_field_status[(rec.field, rec.check_status)] += 1
        if rec.curation_lead:
            y.curation_leads.append(rec.submission_number)
    y.curation_leads.sort()
    return y


# ---------------------------------------------------------------------------
# Recorded responses (the fixtures; the Mac's spike output has the same shape)
# ---------------------------------------------------------------------------

# The spike finished its 153 documents on this date (finding 0023).
_SPIKE_RECORDED_AT = dt.datetime(2026, 10, 16)


def load_recorded(directory: Path) -> tuple[list[BronzeLlmResponseRecord], dict[str, list[Page]]]:
    """Recorded responses as bronze rows, and the pages each was sent.

    ``directory`` holds ``responses/<K>.json`` (the model's answer with its metadata)
    and ``documents/<K>.json`` (the pages sent), as in
    ``tests/fixtures/llm_extraction``. The page text has no fetch sha there, so the
    row's ``source_content_sha256`` is a hash of the pages (marked ``recorded:``).
    """
    rows: list[BronzeLlmResponseRecord] = []
    pages: dict[str, list[Page]] = {}
    for path in sorted((directory / "responses").glob("*.json")):
        r = json.loads(path.read_text())
        d = json.loads((directory / "documents" / path.name).read_text())
        number = r["submission_number"]
        pages[number] = [Page(p["page"], p["text"]) for p in d["pages"]]
        sha = "recorded:" + _sha(*(p.text for p in pages[number]))[:48]
        run_id = f"{r['prompt_version']}-{r['model'].replace(':', '_')}-recorded"
        rows.append(
            BronzeLlmResponseRecord(
                run_id=run_id,
                submission_number=number,
                source_content_sha256=sha,
                pages_sent=[p.page for p in pages[number]],
                model=r["model"],
                model_digest=r["model_digest"],
                prompt_version=r["prompt_version"],
                options_json=json.dumps(r.get("options", {}), sort_keys=True),
                status="ok",
                attempts=1,
                content=r["content"],
                eval_count=r.get("eval_count"),
                prompt_eval_count=r.get("prompt_eval_count"),
                wall_seconds=r.get("wall_seconds"),
                gen_tok_s=r.get("gen_tok_s"),
                requested_at=_SPIKE_RECORDED_AT,
                ingested_at=_SPIKE_RECORDED_AT,
                source_snapshot_id=_sha(r["content"], number)[:16],
            )
        )
    return rows, pages


def evidence_from_recorded(
    directory: Path, *, built_at: dt.datetime | None = None
) -> list[LlmEvidenceRecord]:
    """Check every recorded response offline (no Spark, no model)."""
    built_at = built_at or _utcnow()
    rows, pages = load_recorded(directory)
    out: list[LlmEvidenceRecord] = []
    for r in rows:
        out += evidence_records(r, pages[r.submission_number], built_at=built_at)
    return out


# ---------------------------------------------------------------------------
# A paced run over many documents (the Mac)
# ---------------------------------------------------------------------------


@dataclass
class RunOutcome:
    run_id: str
    records: list[BronzeLlmResponseRecord] = field(default_factory=list)
    stopped: str | None = None  # why the run ended early, if it did

    @property
    def statuses(self) -> Counter[str]:
        return Counter(r.status for r in self.records)


def run_documents(
    docs: Sequence[DocumentInput],
    provider: Provider,
    settings: Settings,
    *,
    run_id: str,
    write: Callable[[list[BronzeLlmResponseRecord]], None],
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], dt.datetime] = _utcnow,
) -> RunOutcome:
    """Send each document once; write after every batch; rest; stop when throttled."""
    outcome = RunOutcome(run_id)
    options = provider.options()
    pending: list[BronzeLlmResponseRecord] = []
    sent_in_batch = 0

    def flush() -> None:
        if pending:
            write(list(pending))
            outcome.records.extend(pending)
            pending.clear()

    for i, doc in enumerate(docs):
        selection = doc.select(settings.llm_max_input_chars)
        requested_at = now()
        result = None
        if selection.pages:
            request = ExtractionRequest(doc.submission_number, doc.device_name, selection.pages)
            result = extract(provider, request)
            sent_in_batch += 1
        pending.append(
            response_record(
                run_id,
                doc,
                selection,
                result,
                model=provider.model,
                prompt_version=sc.PROMPT_VERSION,
                options=options,
                requested_at=requested_at,
                ingested_at=now(),
            )
        )
        logger.info(
            "%s: %s (%s tok/s)",
            doc.submission_number,
            pending[-1].status,
            pending[-1].gen_tok_s,
        )
        if (
            result is not None
            and result.gen_tok_s is not None
            and result.gen_tok_s < settings.llm_min_tok_s
        ):
            outcome.stopped = (
                f"generation fell to {result.gen_tok_s} tok/s (< {settings.llm_min_tok_s}) "
                f"after {doc.submission_number}: let the machine cool, then re-run to resume"
            )
            break
        if sent_in_batch >= settings.llm_batch_size and i < len(docs) - 1:
            flush()
            sent_in_batch = 0
            logger.info("Batch written; resting %.0f s", settings.llm_rest_seconds)
            sleep(settings.llm_rest_seconds)
    flush()
    return outcome


# ---------------------------------------------------------------------------
# Spark
# ---------------------------------------------------------------------------


def document_inputs(
    spark,
    settings: Settings,
    *,
    only: Sequence[str] = (),
    specialties: Sequence[str] = (),
) -> list[DocumentInput]:
    """Every document 4B located pages for, with the text of the same fetch.

    Joined on (submission, content_sha256) so the page roles describe the text
    sent (ADR 0021's rule). Newest decision first; ``only`` keeps its own order.
    """
    from pyspark.sql import functions as F

    from registry import tables

    if not tables.table_exists(spark, settings, EXTRACTION_TABLE):
        raise ValueError(
            f"No {EXTRACTION_TABLE} table. Run `registry extract-documents` first: "
            "the model is sent only the pages 4B located (ADR 0022)."
        )
    roles = tables.read_table(spark, settings, EXTRACTION_TABLE).select(
        "submission_number",
        F.col("source_content_sha256").alias("content_sha256"),
        "ifu_form_pages",
        "summary_pages",
    )
    bronze = (
        tables.read_table(spark, settings, DOCUMENTS_TABLE)
        .where("content_sha256 IS NOT NULL")
        .select("submission_number", "content_sha256", "page_texts")
        .dropDuplicates(["submission_number", "content_sha256"])
    )
    joined = roles.join(bronze, ["submission_number", "content_sha256"], "inner")
    if tables.table_exists(spark, settings, DEVICES_TABLE):
        devices = tables.read_table(spark, settings, DEVICES_TABLE).select(
            "submission_number", "device_name", "specialty_category", "decision_date"
        )
        joined = joined.join(devices, "submission_number", "left")
    else:
        joined = (
            joined.withColumn("device_name", F.lit(None).cast("string"))
            .withColumn("specialty_category", F.lit(None).cast("string"))
            .withColumn("decision_date", F.lit(None).cast("date"))
        )
    if only:
        joined = joined.where(F.col("submission_number").isin([k.upper() for k in only]))
    if specialties:
        joined = joined.where(F.col("specialty_category").isin(list(specialties)))
    rows = [r.asDict() for r in joined.collect()]
    if only:
        order = {k.upper(): i for i, k in enumerate(only)}
        rows.sort(key=lambda r: order[r["submission_number"]])
    else:
        rows.sort(
            key=lambda r: (r["decision_date"] or dt.date.min, r["submission_number"]),
            reverse=True,
        )
    return [
        DocumentInput(
            submission_number=r["submission_number"],
            device_name=r["device_name"] or r["submission_number"],
            content_sha256=r["content_sha256"],
            page_texts=list(r["page_texts"] or []),
            located_pages=sorted(set(r["ifu_form_pages"] or []) | set(r["summary_pages"] or [])),
        )
        for r in rows
    ]


def _done(spark, settings: Settings, digest: str, prompt_version: str) -> set[str]:
    from pyspark.sql import functions as F

    from registry import tables

    if not tables.table_exists(spark, settings, RESPONSES_TABLE):
        return set()
    rows = (
        tables.read_table(spark, settings, RESPONSES_TABLE)
        .where(F.col("prompt_version") == prompt_version)
        .select("submission_number", "model_digest", "status")
        .collect()
    )
    return {
        r["submission_number"]
        for r in rows
        if r["status"] in _DONE and (r["model_digest"] in (digest, None))
    }


def _append_responses(spark, settings: Settings, records: list[BronzeLlmResponseRecord]) -> None:
    from registry import tables
    from registry.schemas import spark_schema_for

    df = spark.createDataFrame(
        [r.model_dump() for r in records], schema=spark_schema_for(BronzeLlmResponseRecord)
    )
    tables.write_table(df, settings, RESPONSES_TABLE, mode="append")


def extract_run(
    spark,
    settings: Settings,
    provider: Provider,
    *,
    only: Sequence[str] = (),
    limit: int | None = None,
    specialties: Sequence[str] = (),
    sleep: Callable[[float], None] = time.sleep,
) -> RunOutcome:
    """Call the model on a scoped set of documents and append the answers to bronze."""
    if not only and limit is None:
        raise ValueError(UNSCOPED_RUN)
    digest = provider.digest()  # fails fast if the server or model is missing
    done = _done(spark, settings, digest, sc.PROMPT_VERSION)
    docs = [
        d
        for d in document_inputs(spark, settings, only=only, specialties=specialties)
        if d.submission_number not in done
    ]
    if limit is not None:
        docs = docs[:limit]
    run_id = new_run_id(provider.model, sc.PROMPT_VERSION)
    logger.info("Run %s: %d document(s) (%d already answered)", run_id, len(docs), len(done))
    return run_documents(
        docs,
        provider,
        settings,
        run_id=run_id,
        write=lambda recs: _append_responses(spark, settings, recs),
        sleep=sleep,
    )


@dataclass
class EvidenceBuild:
    written: int
    yields: list[EvidenceYields]  # one per (model, prompt version)


def build_evidence(spark, settings: Settings | None = None) -> EvidenceBuild:
    """Rebuild ``silver_llm_evidence`` from every ``ok`` response in bronze. Offline."""
    from registry import tables
    from registry.schemas import spark_schema_for

    settings = settings or get_settings()
    if not tables.table_exists(spark, settings, RESPONSES_TABLE):
        raise ValueError(f"No {RESPONSES_TABLE} table. Run `registry extract-evidence` first.")
    responses = latest_ok(
        r.asDict() for r in tables.read_table(spark, settings, RESPONSES_TABLE).collect()
    )
    if not responses:
        raise ValueError(f"No ok response in {RESPONSES_TABLE}; nothing to check")
    wanted = spark.createDataFrame(
        sorted({(r["submission_number"], r["source_content_sha256"]) for r in responses}),
        "submission_number string, content_sha256 string",
    )
    texts = {
        (row["submission_number"], row["content_sha256"]): list(row["page_texts"] or [])
        for row in tables.read_table(spark, settings, DOCUMENTS_TABLE)
        .select("submission_number", "content_sha256", "page_texts")
        .dropDuplicates(["submission_number", "content_sha256"])
        .join(wanted, ["submission_number", "content_sha256"], "inner")
        .collect()
    }
    built_at = _utcnow()
    records: list[LlmEvidenceRecord] = []
    for r in responses:
        page_texts = texts.get((r["submission_number"], r["source_content_sha256"]))
        if page_texts is None:
            logger.warning("%s: its fetch is not in bronze; skipped", r["submission_number"])
            continue
        pages = [Page(n, page_texts[n - 1]) for n in r["pages_sent"] if n <= len(page_texts)]
        records += evidence_records(r, pages, built_at=built_at)
    df = spark.createDataFrame(
        [rec.model_dump() for rec in records], schema=spark_schema_for(LlmEvidenceRecord)
    )
    tables.write_table(df, settings, EVIDENCE_TABLE, mode="overwrite", merge_schema=True)
    logger.info("Wrote %d rows to %s", len(records), EVIDENCE_TABLE)
    return EvidenceBuild(len(records), summarise_runs(records))


def read_evidence(spark, settings: Settings | None = None) -> list[LlmEvidenceRecord]:
    from registry import tables

    settings = settings or get_settings()
    return [
        LlmEvidenceRecord.model_validate(r.asDict())
        for r in tables.read_table(spark, settings, EVIDENCE_TABLE).collect()
    ]
