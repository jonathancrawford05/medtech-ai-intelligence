"""`registry inspect`: read the local lakehouse back and say whether it looks right.

This is the only way to check what a local run actually produced without writing
ad-hoc PySpark, and it earned its place by finding a real bug on its first run
against real data (findings/0008): rows sat in the `other` specialty because
`config/specialty_taxonomy.yaml` spelled a panel differently from the FDA export.

So these tests assert the report *flags* problems. A report that only prints
counts would pass a weaker suite and would not have caught that bug.
"""

from __future__ import annotations

import datetime as dt

import pytest
from pyspark.sql.types import StructType

from registry import lakehouse_report, tables
from registry.mart import mortality_relevant
from registry.schemas import BronzeFdaAiListRecord, DeviceRecord, spark_schema_for

pytestmark = pytest.mark.spark


def _bronze_row(submission: str, snapshot: str, ingested: dt.datetime, panel: str = "Radiology"):
    return (
        submission,
        f"Device {submission}",
        "Acme Medical, Inc.",
        "01/15/2024",
        panel,
        "QAS",
        "https://example.test/list",
        ingested,
        snapshot,
    )


def _silver_row(
    submission: str,
    *,
    panel: str = "Cardiovascular",
    category: str = "cardiovascular",
    decision_date: dt.date | None = dt.date(2024, 1, 15),
    device_class: str | None = None,
):
    return (
        submission,
        f"Device {submission}",
        "Acme Medical, Inc.",
        "acme medical",
        decision_date,
        "510k",
        panel,
        category,
        "QAS",
        None,  # pma_base_number
        None,  # pma_supplement_number
        device_class,
        None,  # predicate_submission_number
        None,  # predicate_age_days
        None,  # has_pccp
        None,  # pccp_summary
        None,  # cybersecurity_statement_present
        "https://example.test/list",
        "snap-a",  # source_snapshot_id (ADR 0015)
    )


@pytest.fixture
def bronze_two_pulls(spark, lakehouse):
    """Two appended pulls of the same source -- bronze's append-only invariant."""
    rows = [
        _bronze_row("K1", "snap-a", dt.datetime(2024, 3, 1, 9, 0)),
        _bronze_row("K2", "snap-a", dt.datetime(2024, 3, 1, 9, 0)),
        _bronze_row("K1", "snap-b", dt.datetime(2024, 4, 1, 9, 0)),
        _bronze_row("K2", "snap-b", dt.datetime(2024, 4, 1, 9, 0)),
    ]
    df = spark.createDataFrame(rows, spark_schema_for(BronzeFdaAiListRecord))
    tables.write_table(df, lakehouse, "bronze_fda_ai_list", mode="overwrite")
    return lakehouse


@pytest.fixture
def bronze_two_identical_pulls(spark, lakehouse):
    """Two pulls whose payloads were byte-identical, so they share a snapshot id.

    `source_snapshot_id` is a content hash, so re-ingesting an unchanged FDA export
    produces the same id with a later `ingested_at`. This is the common case, not an
    edge case -- the list does not change every day.
    """
    rows = [
        _bronze_row("K1", "same-snap", dt.datetime(2024, 3, 1, 9, 0)),
        _bronze_row("K2", "same-snap", dt.datetime(2024, 3, 1, 9, 0)),
        _bronze_row("K1", "same-snap", dt.datetime(2024, 4, 1, 9, 0)),
        _bronze_row("K2", "same-snap", dt.datetime(2024, 4, 1, 9, 0)),
    ]
    df = spark.createDataFrame(rows, spark_schema_for(BronzeFdaAiListRecord))
    tables.write_table(df, lakehouse, "bronze_fda_ai_list", mode="overwrite")
    return lakehouse


def _write_silver(spark, settings, rows, *, allow_nulls: bool = False):
    schema = spark_schema_for(DeviceRecord)
    if allow_nulls:
        # DeviceRecord's generated schema makes submission_number/decision_date
        # non-nullable, so the pipeline cannot write these nulls -- that is the
        # first line of defence. The report's null check is the backstop for a
        # table written before a schema change or repaired by hand, so the only
        # honest way to exercise it is to relax the schema here.
        schema = StructType([f.__class__(f.name, f.dataType, True) for f in schema.fields])
    tables.write_table(
        spark.createDataFrame(rows, schema), settings, "silver_devices", mode="overwrite"
    )


class TestBuildReport:
    def test_says_what_to_run_when_bronze_is_missing(self, spark, lakehouse):
        report = lakehouse_report.build_report(spark, lakehouse)
        assert report.ok is False
        assert any("ingest-fda-list" in line for line in report.lines), report.lines

    def test_shows_pull_history_not_just_a_row_count(self, spark, bronze_two_pulls):
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        text = "\n".join(report.lines)
        # Two pulls must be visible as two snapshots: bronze being append-only is
        # the invariant a reader is checking, and one total row count hides it.
        assert "snap-a" in text and "snap-b" in text

    def test_counts_repeat_pulls_of_identical_content_as_separate_pulls(
        self, spark, bronze_two_identical_pulls
    ):
        """Found on the first 1,614-row run: bronze held 3,228 rows across two pulls
        of an unchanged export, and the report said "1 pull(s) retained" -- because it
        grouped on the content hash alone. A row count at twice the distinct
        submissions with one pull listed is self-contradictory, and it hides exactly
        the accumulation the append-only invariant is there to make visible."""
        report = lakehouse_report.build_report(spark, bronze_two_identical_pulls)
        text = "\n".join(report.lines)
        assert "2 pull(s) retained" in text, text
        # Each pull is listed with its own timestamp and its own row count, not summed.
        assert "2024-03-01" in text and "2024-04-01" in text
        pull_lines = [ln for ln in report.lines if "same-snap" in ln]
        assert len(pull_lines) == 2, pull_lines
        assert all("4 rows" not in ln for ln in pull_lines), pull_lines

    def test_names_the_panels_behind_rows_in_the_default_specialty(self, spark, bronze_two_pulls):
        _write_silver(
            spark,
            bronze_two_pulls,
            [
                _silver_row("K1"),
                _silver_row("K2", panel="Nonexistent Panel", category="other"),
            ],
        )
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        text = "\n".join(report.lines)
        assert "Nonexistent Panel" in text, "an uncurated panel must be named, not just counted"
        assert report.ok is False, "rows in the default specialty are a finding, not a statistic"

    def test_separates_a_missing_panel_from_an_uncurated_one(self, spark, bronze_two_pulls):
        """Review nit on PR #8. Both land in the default category, but "the FDA listed
        no panel" and "a panel nobody has curated" are different facts with different
        fixes -- only the second is answered by editing the taxonomy. Reporting them
        together also renders as `Uncurated FDA panel(s): ''`, which reads as a bug."""
        _write_silver(
            spark,
            bronze_two_pulls,
            [
                _silver_row("K1", panel="Nonexistent Panel", category="other"),
                _silver_row("K2", panel="", category="other"),
                _silver_row("K3", panel="   ", category="other"),
            ],
        )
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        text = "\n".join(report.lines)
        assert report.ok is False
        assert "Nonexistent Panel" in text
        assert "''" not in text, "an empty panel must not be listed as an uncurated label"
        uncurated_line = next(line for line in report.lines if "Uncurated FDA panel" in line)
        assert "1 row" in uncurated_line, uncurated_line
        missing_line = next(line for line in report.lines if "no panel" in line)
        assert "2 row" in missing_line, missing_line

    def test_a_missing_panel_alone_is_still_reported(self, spark, bronze_two_pulls):
        _write_silver(spark, bronze_two_pulls, [_silver_row("K1", panel="", category="other")])
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        text = "\n".join(report.lines)
        assert report.ok is False
        assert "no panel" in text
        assert "Uncurated FDA panel" not in text, "nothing to curate when the FDA listed nothing"

    def test_is_clean_when_every_panel_is_curated(self, spark, bronze_two_pulls):
        _write_silver(spark, bronze_two_pulls, [_silver_row("K1"), _silver_row("K2")])
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        assert report.ok is True, "\n".join(report.lines)

    def test_flags_a_null_decision_date_in_silver(self, spark, bronze_two_pulls):
        _write_silver(
            spark,
            bronze_two_pulls,
            [_silver_row("K1"), _silver_row("K2", decision_date=None)],
            allow_nulls=True,
        )
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        assert report.ok is False
        assert any("decision_date" in line for line in report.lines)

    def test_reports_enrichment_coverage_so_zero_percent_is_visible(self, spark, bronze_two_pulls):
        """ADR 0012 leaves device_class null until the openFDA pass runs; 0% must be
        legible as "not enriched yet" rather than a silently empty column."""
        _write_silver(spark, bronze_two_pulls, [_silver_row("K1")])
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        text = "\n".join(report.lines)
        assert "device_class" in text and "0.0%" in text
        assert report.ok is True, "missing enrichment is expected, not a failure"

    def test_does_not_call_the_pdf_only_fields_unenriched(self, spark, bronze_two_pulls):
        """Found on the first live enrichment run. The report grouped all four
        unenriched fields under "0% is expected until that pass runs" -- so after a
        100%-successful openFDA pass it showed device_class at 100% and the other
        three at 0%, telling a reader the pass had not run.

        Three of those four are not obtainable from openFDA at all (ADR 0013
        Decision 4). They must be reported as awaiting a *different* pass, never as
        coverage of the one that just succeeded.
        """
        _write_silver(spark, bronze_two_pulls, [_silver_row("K1", device_class="II")])
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        text = "\n".join(report.lines)

        openfda_line = next(ln for ln in report.lines if "device_class" in ln and "%" in ln)
        assert "100.0%" in openfda_line, openfda_line

        assert "has_pccp" in text and "Issue 4" in text
        pdf_lines = [ln for ln in report.lines if "has_pccp" in ln]
        assert all("%" not in ln for ln in pdf_lines), (
            "a percentage implies a fetch that could have filled it",
            pdf_lines,
        )
        assert report.ok is True

    def test_a_silverless_lakehouse_is_reported_not_an_error(self, spark, bronze_two_pulls):
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        assert report.ok is True
        assert any("build-silver" in line for line in report.lines)


class TestEnrichmentSection:
    """`registry inspect` must report the two openFDA tiers separately (ADR 0013):
    they succeed independently, so one number would hide a half-failed pass."""

    def _write_enrichment(self, spark, settings, rows):
        from registry.schemas import DeviceEnrichmentRecord

        tables.write_table(
            spark.createDataFrame(rows, spark_schema_for(DeviceEnrichmentRecord)),
            settings,
            "silver_device_enrichment",
            mode="overwrite",
        )

    def _enrichment_row(self, submission, **overrides):
        from registry.schemas import DeviceEnrichmentRecord

        base = {
            "submission_number": submission,
            "enriched_at": dt.datetime(2026, 9, 15, 9, 0),
            "submission_found": True,
            "classification_found": True,
            "device_class_raw": "2",
            "statement_or_summary": "Summary",
            "review_time_days": 222,
            "life_sustain_support": False,
        }
        base.update(overrides)
        return DeviceEnrichmentRecord(**base).model_dump()

    def test_says_what_to_run_when_enrichment_is_missing(self, spark, bronze_two_pulls):
        _write_silver(spark, bronze_two_pulls, [_silver_row("K1")])
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        assert any("enrich-openfda" in line for line in report.lines), report.lines
        assert report.ok is True, "never having enriched is not a failure"

    def test_reports_the_two_tiers_separately(self, spark, bronze_two_pulls):
        _write_silver(spark, bronze_two_pulls, [_silver_row("K1"), _silver_row("K2")])
        self._write_enrichment(
            spark,
            bronze_two_pulls,
            [
                self._enrichment_row("K1"),
                # tier 1 hit, tier 2 missed -- the case a single number would hide
                self._enrichment_row("K2", submission_found=False, review_time_days=None),
            ],
        )
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        text = "\n".join(report.lines)
        assert "tier 1" in text and "tier 2" in text
        assert "2 / 2  (100.0%)" in text, text  # classification
        assert "1 / 2  (50.0%)" in text, text  # submission
        assert report.ok is True

    def test_a_tier_resolving_nothing_is_a_problem(self, spark, bronze_two_pulls):
        """Zero hits across a whole tier is a broken pass, not a coverage statistic."""
        _write_silver(spark, bronze_two_pulls, [_silver_row("K1")])
        self._write_enrichment(
            spark,
            bronze_two_pulls,
            [self._enrichment_row("K1", classification_found=False, device_class_raw=None)],
        )
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        assert report.ok is False
        assert any("openFDA reachability" in line for line in report.lines)

    def test_summarises_review_time_and_summary_availability(self, spark, bronze_two_pulls):
        _write_silver(spark, bronze_two_pulls, [_silver_row("K1")])
        self._write_enrichment(spark, bronze_two_pulls, [self._enrichment_row("K1")])
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        text = "\n".join(report.lines)
        assert "median 222d" in text, text
        assert "Summary" in text


class TestDocumentsSection:
    """`registry inspect` reports the Summary fetch (ADR 0018): how many Summary
    filings have a document, and the text-layer split -- the numbers finding 0011's
    spike measured on 60 and the full-scale run must be compared against."""

    def _setup(self, spark, settings, documents):
        from registry.schemas import BronzeSummaryDocumentRecord, DeviceEnrichmentRecord

        _write_silver(spark, settings, [_silver_row("K1")])
        enrichment = [
            DeviceEnrichmentRecord(
                submission_number=n,
                enriched_at=dt.datetime(2026, 9, 15, 9, 0),
                submission_found=True,
                classification_found=True,
                statement_or_summary=kind,
            ).model_dump()
            for n, kind in (
                ("K100001", "Summary"),
                ("K100002", "Summary"),
                ("K100003", "Summary"),
                ("K100004", "Statement"),
            )
        ]
        tables.write_table(
            spark.createDataFrame(enrichment, spark_schema_for(DeviceEnrichmentRecord)),
            settings,
            "silver_device_enrichment",
            mode="overwrite",
        )
        if documents is not None:
            tables.write_table(
                spark.createDataFrame(
                    [BronzeSummaryDocumentRecord(**d).model_dump() for d in documents],
                    spark_schema_for(BronzeSummaryDocumentRecord),
                ),
                settings,
                "bronze_summary_documents",
                mode="append",
            )

    @staticmethod
    def _doc(number, *, found=True, text_class="text", fetched=dt.datetime(2026, 10, 10, 9)):
        pages = ["x" * 200] if text_class == "text" else [""]
        return {
            "submission_number": number,
            "url": f"https://example.test/{number}.pdf",
            "urls_tried": [f"https://example.test/{number}.pdf"],
            "http_status": 200 if found else 404,
            "content_sha256": (number + "0" * 64)[:64] if found else None,
            "page_count": len(pages) if found else 0,
            "page_texts": pages if found else [],
            "page_char_counts": [len(p) for p in pages] if found else [],
            "text_class": text_class if found else None,
            "fetched_at": fetched,
            "ingested_at": fetched,
        }

    def test_says_what_to_run_when_nothing_is_fetched(self, spark, bronze_two_pulls):
        self._setup(spark, bronze_two_pulls, None)
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        assert any("fetch-summaries" in line for line in report.lines), report.lines
        assert report.ok is True

    def test_reports_coverage_and_text_classes_from_the_latest_fetch(self, spark, bronze_two_pulls):
        self._setup(
            spark,
            bronze_two_pulls,
            [
                # K100001 missed once, then found: the latest fetch is what counts.
                self._doc("K100001", found=False, fetched=dt.datetime(2026, 10, 9)),
                self._doc("K100001"),
                self._doc("K100002", text_class="image"),
                self._doc("K100003", found=False),
            ],
        )
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        text = "\n".join(report.lines)
        assert "bronze_summary_documents" in text
        assert "fetches across all runs        :       4" in text, text
        assert "Summary filings with a PDF     : 2 / 3  (66.7%)" in text, text
        assert "latest fetch found nothing     : 1" in text, text
        assert "image" in text and "text" in text
        assert report.ok is True

    def test_a_pass_that_found_nothing_is_a_problem(self, spark, bronze_two_pulls):
        self._setup(spark, bronze_two_pulls, [self._doc("K100001", found=False)])
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        assert report.ok is False
        assert any("accessdata" in line for line in report.lines)


class TestGoldSection:
    """`registry inspect` must surface the gold mart -- the registry's actual
    deliverable. Before this the verdict tool was blind to it: a full mart and an
    empty one were indistinguishable from `inspect` (it showed neither), so nobody
    could confirm from the verdict that curation had landed."""

    def _reach_gold(self, spark, settings):
        """Seed the tables `inspect` walks before it reaches the gold section."""
        from registry.schemas import DeviceEnrichmentRecord

        _write_silver(spark, settings, [_silver_row("K1"), _silver_row("K2")])
        rows = [
            DeviceEnrichmentRecord(
                submission_number=s_,
                enriched_at=dt.datetime(2026, 9, 15, 9, 0),
                submission_found=True,
                classification_found=True,
                device_class_raw="2",
                statement_or_summary="Summary",
                review_time_days=222,
                life_sustain_support=False,
            ).model_dump()
            for s_ in ("K1", "K2")
        ]
        tables.write_table(
            spark.createDataFrame(rows, spark_schema_for(DeviceEnrichmentRecord)),
            settings,
            "silver_device_enrichment",
            mode="overwrite",
        )

    def _write_evidence(self, spark, settings, rows):
        from registry.schemas import EvidenceRecord

        tables.write_table(
            spark.createDataFrame(rows, spark_schema_for(EvidenceRecord)),
            settings,
            "silver_evidence",
            mode="overwrite",
        )

    def _evidence(self, submission, *, confirmed, keyword, text="Predicts risk of death."):
        from registry.schemas import EvidenceRecord

        return EvidenceRecord(
            submission_number=submission,
            intended_use_text=text,
            intended_use_source="https://example.test/src",
            mortality_keyword_flag=keyword,
            mortality_confirmed_flag=confirmed,
            mortality_review_method="llm_assisted" if confirmed is not None else None,
        ).model_dump()

    def test_says_what_to_run_when_the_mart_is_missing(self, spark, bronze_two_pulls):
        self._reach_gold(spark, bronze_two_pulls)
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        assert any("build-mart" in line for line in report.lines), report.lines
        assert report.ok is True, "never having built the mart is not a failure"

    def test_an_empty_mart_is_reported_not_a_failure(self, spark, bronze_two_pulls):
        """An empty mart is the honest pre-curation state (ADR 0014), so it is
        reported, not flagged."""
        self._reach_gold(spark, bronze_two_pulls)
        mortality_relevant.run(spark, bronze_two_pulls)  # no evidence -> empty mart
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        text = "\n".join(report.lines)
        assert "GOLD" in text and "empty" in text, text
        assert report.ok is True

    def test_reports_the_confirmed_devices_and_the_keyword_disagreement(
        self, spark, bronze_two_pulls
    ):
        self._reach_gold(spark, bronze_two_pulls)
        self._write_evidence(
            spark,
            bronze_two_pulls,
            [
                # confirmed, but stage-1 keyword missed it -> keyword_disagrees
                self._evidence("K1", confirmed=True, keyword=False),
                # reviewed and rejected -> never in the mart
                self._evidence("K2", confirmed=False, keyword=True),
            ],
        )
        assert mortality_relevant.run(spark, bronze_two_pulls) == 1
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        text = "\n".join(report.lines)
        assert "GOLD" in text
        assert "K1" in text and "K2" not in text.split("GOLD", 1)[1]
        # "leads" means gold_device_leads only (the LEADS section); the gold mart
        # holds confirmed devices, so its section must not borrow the word.
        gold_section = text.split("GOLD", 1)[1].split("gold_device_leads", 1)[0]
        assert "lead" not in gold_section.lower(), gold_section
        assert "confirmed devices" in gold_section
        disagree_line = next(line for line in report.lines if "keyword_disagrees (curator" in line)
        assert "1 / 1" in disagree_line, disagree_line
        assert report.ok is True
        assert not any("showing the newest" in line for line in report.lines), (
            "one row fits the list, so it must not claim to be truncated"
        )

    def test_says_when_the_confirmed_list_is_clipped(self, spark, bronze_two_pulls, monkeypatch):
        """The list is capped; the header must say so rather than silently drop rows
        (PR #12 review nit). The cap is patched down so two rows exercise it."""
        monkeypatch.setattr(lakehouse_report, "GOLD_DEVICES_SHOWN", 1)
        self._reach_gold(spark, bronze_two_pulls)
        self._write_evidence(
            spark,
            bronze_two_pulls,
            [
                self._evidence("K1", confirmed=True, keyword=True),
                self._evidence("K2", confirmed=True, keyword=True),
            ],
        )
        assert mortality_relevant.run(spark, bronze_two_pulls) == 2
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        header = next(
            line for line in report.lines if "the confirmed devices, newest first" in line
        )
        assert "(showing the newest 1 of 2)" in header, header
        gold = "\n".join(report.lines).split("the confirmed devices, newest first", 1)[1]
        listed = [s for s in ("K1", "K2") if f"    {s} " in gold]
        assert len(listed) == 1, gold


class TestLeadsSection:
    """`registry inspect` must surface `gold_device_leads` (ADR 0017) -- otherwise
    the only way to see what `registry monitor` recorded is ad-hoc PySpark.

    Leads come from silver snapshots, not from enrichment or the gold mart, so the
    section must appear whenever silver exists -- a lakehouse that has not run
    `enrich-openfda` or `build-mart` can still have leads."""

    def _lead(
        self,
        submission,
        *,
        detected_at=dt.datetime(2026, 10, 1, 9, 0),
        movement="added",
        changed_fields=(),
        categories=("new_submission",),
    ):
        from registry.schemas import LeadRecord

        return LeadRecord(
            detected_at=detected_at,
            prev_snapshot_id="snap-a",
            curr_snapshot_id="snap-b",
            prev_version=0,
            curr_version=1,
            submission_number=submission,
            movement=movement,
            changed_fields=list(changed_fields),
            categories=list(categories),
            device_name=f"Device {submission}",
            applicant_resolved="acme medical",
            decision_date=dt.date(2026, 9, 1),
            pathway="510k",
            specialty_category="cardiovascular",
            specialty_panel="Cardiovascular",
            product_code="QAS",
            signal_new_submission=movement == "added",
            signal_cardiometabolic="cardiometabolic" in categories,
            signal_mortality_language="mortality_language" in categories,
            source_url="https://example.test/list",
        ).model_dump()

    def _write_leads(self, spark, settings, rows):
        from registry.mart.leads import LEADS_TABLE
        from registry.schemas import LeadRecord

        tables.write_table(
            spark.createDataFrame(rows, spark_schema_for(LeadRecord)),
            settings,
            LEADS_TABLE,
            mode="overwrite",
        )

    def _leads_text(self, report):
        text = "\n".join(report.lines)
        assert "LEADS  gold_device_leads" in text, text
        return text.split("LEADS  gold_device_leads", 1)[1]

    def test_says_to_run_monitor_when_the_table_is_missing(self, spark, bronze_two_pulls):
        _write_silver(spark, bronze_two_pulls, [_silver_row("K1")])
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        assert any("registry monitor" in line for line in report.lines), report.lines
        assert report.ok is True, "never having run the monitor is not a failure"

    def test_an_empty_table_is_reported_not_a_failure(self, spark, bronze_two_pulls):
        """`registry monitor` creates the table on an empty first run (ADR 0017),
        and no movement between snapshots is the normal case."""
        _write_silver(spark, bronze_two_pulls, [_silver_row("K1")])
        self._write_leads(spark, bronze_two_pulls, [])
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        section = self._leads_text(report)
        assert "rows" in section and "empty" in section, section
        assert report.ok is True

    def test_reports_totals_categories_and_the_newest_leads(self, spark, bronze_two_pulls):
        _write_silver(spark, bronze_two_pulls, [_silver_row("K1")])
        self._write_leads(
            spark,
            bronze_two_pulls,
            [
                self._lead(
                    "K9",
                    detected_at=dt.datetime(2026, 10, 1, 9, 0),
                    categories=("new_submission", "cardiometabolic"),
                ),
                self._lead(
                    "K1",
                    detected_at=dt.datetime(2026, 10, 8, 9, 0),
                    movement="changed",
                    changed_fields=("device_name", "specialty_panel"),
                    categories=("cardiometabolic",),
                ),
            ],
        )
        report = lakehouse_report.build_report(spark, bronze_two_pulls)
        section = self._leads_text(report)
        lines = section.splitlines()

        total = next(line for line in lines if "rows" in line)
        assert total.rstrip().endswith("2"), total

        def count_for(category):
            line = next(line for line in lines if line.strip().startswith(category))
            return int(line.split()[-1])

        assert count_for("cardiometabolic") == 2
        assert count_for("new_submission") == 1
        # Every live category is listed, so a zero is visible rather than absent.
        assert count_for("mortality_language") == 0
        assert count_for("life_sustaining") == 0

        k1 = next(line for line in lines if "    K1 " in line)
        assert "changed" in k1 and "device_name,specialty_panel" in k1, k1
        k9 = next(line for line in lines if "    K9 " in line)
        assert "added" in k9, k9
        assert section.index("    K1 ") < section.index("    K9 "), "newest detection first"
        assert not any("showing the newest" in line for line in lines)
        assert report.ok is True

    def test_says_when_the_leads_list_is_clipped(self, spark, bronze_two_pulls, monkeypatch):
        monkeypatch.setattr(lakehouse_report, "LEADS_SHOWN", 1)
        _write_silver(spark, bronze_two_pulls, [_silver_row("K1")])
        self._write_leads(spark, bronze_two_pulls, [self._lead("K1"), self._lead("K2")])
        section = self._leads_text(lakehouse_report.build_report(spark, bronze_two_pulls))
        assert "(showing the newest 1 of 2)" in section, section
        listed = [s for s in ("K1", "K2") if f"    {s} " in section]
        assert len(listed) == 1, section

    def test_appears_after_the_gold_section_when_the_mart_exists(self, spark, bronze_two_pulls):
        TestGoldSection()._reach_gold(spark, bronze_two_pulls)
        mortality_relevant.run(spark, bronze_two_pulls)
        self._write_leads(spark, bronze_two_pulls, [self._lead("K1")])
        text = "\n".join(lakehouse_report.build_report(spark, bronze_two_pulls).lines)
        assert text.index("GOLD  gold_mortality_relevant") < text.index("LEADS  gold_device_leads")
