"""Bronze responses, silver evidence and the paced run (PR 4D; ADR 0022 Decisions 1, 6).

Pure-Python tests run the recorded spike answers through the same code path silver
uses; the Spark test round-trips both tables. The paced run uses a fake provider:
pacing is about what the loop does between calls, not what a model says.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from registry.config.settings import Settings
from registry.llm_extraction import run
from registry.llm_extraction import schema as sc
from registry.llm_extraction.provider import ModelCall
from registry.schemas import BronzeLlmResponseRecord, LlmEvidenceRecord

LLM = Path(__file__).parent / "fixtures" / "llm_extraction"
T0 = dt.datetime(2026, 10, 18, 9, 0)


def _answer() -> str:
    return json.dumps(
        {
            "performance_reported": {"value": True, "quote": "sensitivity of 93.6%"},
            "metrics": [],
            "study_design": {"value": "not_stated", "quote": None},
            "sample_size": {"value": None, "unit": "not_stated", "quote": None},
            "multi_site": {"value": None, "quote": None},
            "demographics_disclosed": {"value": None, "quote": None},
            "clinical_endpoint": {"value": "not_stated", "quote": None},
        }
    )


class FakeProvider:
    model = "fake:1b"

    def __init__(self, speeds: list[float] | None = None):
        self.speeds = list(speeds or [])
        self.calls: list[str] = []

    def digest(self) -> str:
        return "f" * 64

    def options(self) -> dict:
        return {"temperature": 0, "seed": 42, "num_predict": 3000, "think": False}

    def generate(self, system, user, json_schema):
        self.calls.append(user.split("\n", 1)[0])
        tok_s = self.speeds.pop(0) if self.speeds else 9.0
        return ModelCall(_answer(), done_reason="stop", eval_count=300, gen_tok_s=tok_s)


def _doc(
    number: str, *, text: str = "A clinical study: sensitivity of 93.6% in patients. "
) -> run.DocumentInput:
    return run.DocumentInput(number, f"Device {number}", "a" * 64, [text * 5], [1])


def _cipher_doc() -> run.DocumentInput:
    doc = json.loads((LLM / "documents" / "K182456.json").read_text())
    texts = {p["page"]: p["text"] for p in doc["pages"]}
    page_texts = [texts.get(n, "") for n in range(1, max(texts) + 1)]
    return run.DocumentInput("K182456", doc["device_name"], "b" * 64, page_texts, sorted(texts))


def _settings(**kw) -> Settings:
    return Settings(llm_batch_size=2, llm_rest_seconds=300, llm_min_tok_s=6.0, **kw)


def _run(docs, provider, settings=None):
    writes: list[list[BronzeLlmResponseRecord]] = []
    sleeps: list[float] = []
    outcome = run.run_documents(
        docs,
        provider,
        settings or _settings(),
        run_id="r1",
        write=writes.append,
        sleep=sleeps.append,
        now=lambda: T0,
    )
    return outcome, writes, sleeps


class TestPacedRun:
    def test_writes_each_batch_then_rests(self):
        provider = FakeProvider()
        outcome, writes, sleeps = _run([_doc(f"K00000{i}") for i in range(5)], provider)
        assert [len(w) for w in writes] == [2, 2, 1]
        assert sleeps == [300, 300]
        assert outcome.statuses == {"ok": 5}

    def test_stops_when_generation_slows_and_keeps_what_it_has(self):
        provider = FakeProvider(speeds=[9.0, 5.4, 9.0])
        outcome, writes, _ = _run([_doc(f"K00000{i}") for i in range(3)], provider)
        assert len(provider.calls) == 2
        assert sum(len(w) for w in writes) == 2
        assert outcome.stopped and "5.4 tok/s" in outcome.stopped

    def test_a_document_with_only_cipher_pages_is_never_sent(self):
        provider = FakeProvider()
        outcome, _, _ = _run([_cipher_doc()], provider)
        assert provider.calls == []
        (rec,) = outcome.records
        assert rec.status == "skipped_no_pages"
        assert rec.pages_garbled == [7, 8]
        assert rec.pages_sent == [] and rec.model_digest is None

    def test_unsent_documents_do_not_count_toward_a_batch(self):
        provider = FakeProvider()
        _, writes, sleeps = _run([_cipher_doc(), _doc("K000001"), _cipher_doc()], provider)
        assert sleeps == []
        assert [len(w) for w in writes] == [3]

    def test_every_row_carries_provenance(self):
        outcome, _, _ = _run([_doc("K000001")], FakeProvider())
        (rec,) = outcome.records
        assert rec.run_id == "r1"
        assert rec.model == "fake:1b" and rec.model_digest == "f" * 64
        assert rec.prompt_version == sc.PROMPT_VERSION
        assert json.loads(rec.options_json)["seed"] == 42
        assert rec.source_content_sha256 == "a" * 64
        assert rec.pages_sent == [1]

    def test_an_unscoped_run_is_refused_before_anything_else(self):
        with pytest.raises(ValueError, match="unscoped"):
            run.extract_run(None, _settings(), FakeProvider())

    def test_run_ids_name_the_prompt_model_and_time(self):
        assert run.new_run_id("qwen3:14b", "4d.1", T0) == "4d.1-qwen3_14b-20261018T090000Z"


class TestEvidenceFromRecordedResponses:
    @pytest.fixture(scope="class")
    def evidence(self) -> list[LlmEvidenceRecord]:
        return run.evidence_from_recorded(LLM, built_at=T0)

    def test_every_recorded_document_is_checked(self, evidence):
        assert len({e.submission_number for e in evidence}) == 43

    def test_every_row_is_llm_assisted_with_the_full_digest(self, evidence):
        assert {e.review_method for e in evidence} == {"llm_assisted"}
        assert {e.model_digest for e in evidence} == {
            "bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8"
        }
        assert {e.prompt_version for e in evidence} == {"4d-spike-4"}
        assert {e.run_id for e in evidence} == {"4d-spike-4-qwen3_14b-recorded"}

    def test_rejected_values_are_kept_and_marked(self, evidence):
        statuses = {e.check_status for e in evidence if not e.accepted}
        assert {"value_not_in_quote", "quote_not_found", "measure_not_named"} <= statuses

    def test_every_accepted_quote_has_its_page(self, evidence):
        assert all(e.quote_pages for e in evidence if e.accepted and e.quote)

    def test_risk_endpoints_are_curation_leads_never_flags(self, evidence):
        leads = sorted(e.submission_number for e in evidence if e.curation_lead)
        assert "K230842" in leads and "K231038" in leads  # SignalHF, GHI (finding 0023)
        assert all(
            e.field == "clinical_endpoint" and e.accepted for e in evidence if e.curation_lead
        )
        assert not any("mortality" in name for name in LlmEvidenceRecord.model_fields)

    def test_two_prompt_versions_are_summarised_apart(self, evidence):
        """A build holding 4d.1 and 4d.2 answers listed every lead twice, pooled."""
        later = [e.model_copy(update={"prompt_version": "4d.2"}) for e in evidence]
        runs = run.summarise_runs(evidence + later)
        assert [y.run for y in runs] == ["qwen3:14b / 4d-spike-4", "qwen3:14b / 4d.2"]
        assert all(y.responses == 43 for y in runs)
        assert runs[0].curation_leads == runs[1].curation_leads
        assert runs[1].lines()[0] == "qwen3:14b / 4d.2: responses checked: 43"

    def test_the_yields_report_every_field(self, evidence):
        lines = run.summarise(evidence, 43).lines()
        assert lines[0] == "responses checked: 43"
        assert any(line.strip().startswith("metric") for line in lines)


class TestLatestOk:
    def _row(self, number, at, status="ok", digest="d", prompt="4d.1"):
        return {
            "submission_number": number,
            "requested_at": at,
            "status": status,
            "model_digest": digest,
            "prompt_version": prompt,
        }

    def test_newest_ok_per_document_model_and_prompt(self):
        rows = [
            self._row("K1", T0),
            self._row("K1", T0 + dt.timedelta(days=1)),
            self._row("K1", T0 + dt.timedelta(days=2), status="skipped_output_cap"),
            self._row("K1", T0, digest="other"),
        ]
        picked = run.latest_ok(rows)
        assert len(picked) == 2
        assert max(
            r["requested_at"] for r in picked if r["model_digest"] == "d"
        ) == T0 + dt.timedelta(days=1)


@pytest.mark.spark
class TestSparkRoundTrip:
    def test_responses_append_and_evidence_rebuilds(self, spark, lakehouse):
        """Bronze page text + 4B roles -> a paced run -> bronze -> silver, then a resume."""
        from registry import tables
        from registry.schemas import (
            BronzeSummaryDocumentRecord,
            DocumentExtractionRecord,
            spark_schema_for,
        )

        text = "A clinical study: sensitivity of 93.6% in 400 patients. " * 4
        bronze = BronzeSummaryDocumentRecord(
            submission_number="K240001",
            url="https://example.test/K240001.pdf",
            urls_tried=["https://example.test/K240001.pdf"],
            http_status=200,
            content_sha256="c" * 64,
            page_count=2,
            page_texts=["FDA letter " * 20, text],
            page_char_counts=[200, 200],
            text_class="text",
            fetched_at=T0,
            ingested_at=T0,
        )
        roles = DocumentExtractionRecord(
            submission_number="K240001",
            extractor_version="4b.2",
            extracted_at=T0,
            source_content_sha256="c" * 64,
            source_fetched_at=T0,
            page_count=2,
            letter_pages=[1],
            summary_pages=[2],
            cybersecurity_basis="no_mention",
            cybersecurity_statement_present=False,
            pccp_method="no_mention",
            has_pccp=False,
        )
        for model, name in (
            (bronze, run.DOCUMENTS_TABLE),
            (roles, run.EXTRACTION_TABLE),
        ):
            df = spark.createDataFrame([model.model_dump()], schema=spark_schema_for(type(model)))
            tables.write_table(df, lakehouse, name, mode="overwrite")

        provider = FakeProvider()
        outcome = run.extract_run(spark, lakehouse, provider, limit=5, sleep=lambda s: None)
        assert outcome.statuses == {"ok": 1}
        assert provider.calls == ["Device: K240001 (K240001)"]  # no silver_devices yet

        build = run.build_evidence(spark, lakehouse)
        (row,) = run.read_evidence(spark, lakehouse)
        assert build.written == 1
        assert row.field == "performance_reported" and row.accepted
        assert row.quote_pages == [2]

        again = run.extract_run(spark, lakehouse, provider, limit=5, sleep=lambda s: None)
        assert again.records == []  # answered for this digest and prompt: not re-sent
        assert tables.read_table(spark, lakehouse, run.RESPONSES_TABLE).count() == 1
