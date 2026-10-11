"""Read the lakehouse back and report whether it looks right.

`registry inspect` is the answer to "the pipeline said it wrote 1,614 rows -- now
what?". Without it the only way to check a local run is ad-hoc PySpark in a REPL,
which is how data problems go unnoticed.

The report is a **judgement, not a dump**: `Report.ok` is False when something is
actually wrong, so the command exits non-zero and can be wired into a scheduled
local refresh. What counts as wrong:

* no bronze table at all (nothing has been ingested);
* a silver row with a null `submission_number` or `decision_date` -- both are
  non-optional in `DeviceRecord`, so a null means the parse quietly failed;
* silver rows sitting in the taxonomy's default category, which means an FDA panel
  we have not curated (findings/0008 -- this is exactly how the "General and
  Plastic Surgery" spelling mismatch was found).

Missing openFDA enrichment is *not* a failure: ADR 0012 leaves those fields null
until the enrichment pass exists, so the report prints coverage and moves on.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from registry import tables
from registry.config.settings import Settings, get_settings
from registry.mart import leads, mortality_relevant
from registry.schemas import LEAD_CATEGORIES
from registry.transform import taxonomy

# Raw columns bronze should have captured for every row. Emptiness here means the
# source changed shape or the parser lost a column, which silver cannot fix.
_BRONZE_COLUMNS = ("device_name", "applicant_raw", "decision_date_raw", "panel_raw", "product_code")

# Populated by `registry enrich-openfda`; null until it runs (ADR 0012).
_OPENFDA_COLUMNS = ("device_class",)

# NOT obtainable from openFDA at any coverage -- no endpoint carries them. They are
# in the 510(k) summary PDF (ADR 0013 Decision 4, roadmap Issue 4). Reporting these
# as "0% coverage" alongside the openFDA fields told a reader that a pass which had
# just succeeded at 100% had not run.
_DOCUMENT_ONLY_COLUMNS = (
    "predicate_submission_number",
    "predicate_age_days",
    "has_pccp",
    "pccp_summary",
    "cybersecurity_statement_present",
)
# The subset the document pass fills (ADR 0019). pccp_summary waits for 4D.
_EXTRACTED_COLUMNS = tuple(c for c in _DOCUMENT_ONLY_COLUMNS if c != "pccp_summary")


@dataclass
class Report:
    """Lines to print, plus whether anything in them is a problem."""

    lines: list[str] = field(default_factory=list)
    ok: bool = True

    def say(self, line: str = "") -> None:
        self.lines.append(line)

    def rule(self, title: str) -> None:
        self.say()
        self.say("=" * 72)
        self.say(title)
        self.say("=" * 72)

    def problem(self, line: str) -> None:
        self.ok = False
        self.say(f"  PROBLEM  {line}")


def _counts(df: DataFrame, column: str, limit: int = 20) -> list[tuple[str, int]]:
    rows = (
        df.groupBy(column)
        .count()
        .orderBy(F.desc("count"), F.col(column).asc_nulls_last())
        .limit(limit)
        .collect()
    )
    return [(r[column] if r[column] is not None else "<null>", r["count"]) for r in rows]


def _render(report: Report, pairs: list[tuple[str, int]], label: str) -> None:
    width = max((len(str(k)) for k, _ in pairs), default=len(label))
    for key, count in pairs:
        report.say(f"  {key!s:<{width}}  {count:>7,}")


def _bronze_section(report: Report, bronze: DataFrame) -> None:
    total = bronze.count()
    distinct = bronze.select("submission_number").distinct().count()
    report.rule("BRONZE  bronze_fda_ai_list")
    report.say(f"  rows across all pulls       : {total:,}")
    report.say(f"  distinct submission numbers : {distinct:,}")

    report.say()
    report.say("  pull history (bronze is append-only, so pulls accumulate):")
    # Group on (snapshot, ingested_at), not the snapshot alone: `source_snapshot_id`
    # is a content hash, so re-ingesting an unchanged export reuses it. Grouping on
    # the hash alone merged those pulls into one line carrying their summed row
    # count -- "1 pull" holding twice the distinct submissions.
    pulls = (
        bronze.groupBy("source_snapshot_id", "ingested_at")
        .agg(F.count("*").alias("rows"))
        .orderBy("ingested_at", "source_snapshot_id")
        .collect()
    )
    seen_snapshots: set[str] = set()
    for row in pulls:
        snapshot = row["source_snapshot_id"]
        # A repeat of a hash we have already listed means the source was unchanged
        # between pulls -- worth saying, because it is the normal case and it
        # explains why the row count grows while the content does not.
        note = "  (unchanged since an earlier pull)" if snapshot in seen_snapshots else ""
        seen_snapshots.add(snapshot)
        report.say(f"    {row['ingested_at']}  {snapshot}  {row['rows']:>7,} rows{note}")
    report.say(
        f"  -> {len(pulls)} pull(s) retained, {len(seen_snapshots)} distinct source snapshot(s)"
    )

    report.say()
    report.say("  completeness of the raw columns:")
    for column in _BRONZE_COLUMNS:
        missing = bronze.filter(F.col(column).isNull() | (F.trim(F.col(column)) == "")).count()
        report.say(f"    {column:<20} missing {missing:>7,} / {total:,}")

    report.say()
    report.say("  panels, as the FDA spells them:")
    _render(report, _counts(bronze, "panel_raw", limit=30), "panel_raw")


def _silver_section(report: Report, silver: DataFrame, settings: Settings) -> None:
    rows = silver.count()
    report.rule("SILVER  silver_devices")
    report.say(f"  rows                        : {rows:,}")
    report.say(
        f"  distinct submission numbers : {silver.select('submission_number').distinct().count():,}"
    )

    # `submission_number` and `decision_date` are non-optional in DeviceRecord; a
    # null here means a parse failure that got written anyway.
    for column in ("submission_number", "decision_date"):
        nulls = silver.filter(F.col(column).isNull()).count()
        if nulls:
            report.problem(f"{nulls:,} silver row(s) have a null {column}")

    report.say()
    report.say("  pathway split:")
    _render(report, _counts(silver, "pathway"), "pathway")

    report.say()
    report.say("  specialty categories:")
    _render(report, _counts(silver, "specialty_category", limit=30), "specialty_category")

    # Two different facts land in the default category and only one is fixed by
    # editing the taxonomy, so they are reported separately: a panel we have not
    # curated is a curation backlog item, a row with no panel at all is a gap in
    # the source (or in parsing) that no taxonomy entry can close.
    default_category = taxonomy.load(settings).default_category
    defaulted = silver.filter(F.col("specialty_category") == default_category)
    blank_panel = F.col("specialty_panel").isNull() | (F.trim(F.col("specialty_panel")) == "")

    uncurated = defaulted.filter(~blank_panel)
    uncurated_count = uncurated.count()
    if uncurated_count:
        panels = sorted(
            {
                r["specialty_panel"].strip()
                for r in uncurated.select("specialty_panel").distinct().collect()
            }
        )
        report.problem(
            f"{uncurated_count:,} row(s) fell to '{default_category}'. Uncurated FDA "
            f"panel(s): {', '.join(repr(p) for p in panels)}. Add them to "
            f"{settings.specialty_taxonomy_path}."
        )

    missing_count = defaulted.filter(blank_panel).count()
    if missing_count:
        report.problem(
            f"{missing_count:,} row(s) have no panel at all, so they fell to "
            f"'{default_category}'. Nothing to curate -- check the source column and "
            "the bronze parse."
        )

    report.say()
    report.say("  openFDA enrichment (run `registry enrich-openfda`; 0% means not yet run):")
    for column in _OPENFDA_COLUMNS:
        filled = silver.filter(F.col(column).isNotNull()).count()
        pct = 100 * filled / rows if rows else 0.0
        report.say(f"    {column:<32} {filled:>7,} / {rows:,}  ({pct:.1f}%)")

    report.say()
    filled = {c: silver.filter(F.col(c).isNotNull()).count() for c in _EXTRACTED_COLUMNS}
    if any(filled.values()):
        # The document pass has run (ADR 0019): its coverage is now a real number.
        # The denominator is every silver row, most of which filed no Summary.
        report.say("  document pass (`registry extract-documents`, ADR 0019):")
        for column, count in filled.items():
            pct = 100 * count / rows if rows else 0.0
            report.say(f"    {column:<32} {count:>7,} / {rows:,}  ({pct:.1f}%)")
        report.say("    pccp_summary stays null until LLM-assisted extraction (Issue 4, PR 4D).")
    else:
        # Deliberately no percentage: a coverage figure implies a pass that could
        # have filled the column, and it has not run.
        report.say("  awaiting the document pass (roadmap Issue 4) -- not in any openFDA endpoint:")
        report.say(f"    {', '.join(_DOCUMENT_ONLY_COLUMNS)}")
        report.say(
            "    These live in the 510(k) summary PDF: run `registry extract-documents`, "
            "then `registry build-silver`."
        )

    supplements = silver.filter(F.col("pma_supplement_number").isNotNull())
    supplement_count = supplements.count()
    report.say()
    report.say(f"  PMA supplements (whole key kept, split alongside): {supplement_count:,}")
    for row in (
        supplements.select("submission_number", "pma_base_number", "pma_supplement_number")
        .limit(5)
        .collect()
    ):
        report.say(
            f"    {row['submission_number']:<16} base={row['pma_base_number']} "
            f"supplement={row['pma_supplement_number']}"
        )

    report.say()
    report.say("  top resolved companies:")
    _render(report, _counts(silver, "applicant_resolved", limit=15), "applicant_resolved")

    report.say()
    report.say("  authorisations by decision year:")
    years = silver.groupBy(F.year("decision_date").alias("year")).count().orderBy("year").collect()
    for row in years:
        year = row["year"] if row["year"] is not None else "<null>"
        report.say(f"    {year}  {row['count']:>7,}")


def _enrichment_section(report: Report, enrichment: DataFrame) -> None:
    rows = enrichment.count()
    report.rule("ENRICHMENT  silver_device_enrichment  (openFDA, ADR 0013)")
    report.say(f"  rows                        : {rows:,}")

    # The two tiers succeed independently, so one coverage number would hide a
    # half-failed pass: tier 1 is 181 product-code calls, tier 2 is one per device.
    for label, column in (
        ("tier 1  classification (by product code)", "classification_found"),
        ("tier 2  submission (510k / pma)", "submission_found"),
    ):
        hits = enrichment.filter(F.col(column)).count()
        pct = 100 * hits / rows if rows else 0.0
        report.say(f"    {label:<42} {hits:>7,} / {rows:,}  ({pct:.1f}%)")
        if rows and hits == 0:
            report.problem(f"{label.strip()} resolved nothing -- check openFDA reachability")

    report.say()
    report.say("  device class, as openFDA reports it:")
    _render(report, _counts(enrichment, "device_class_raw"), "device_class_raw")

    report.say("  FDA life-sustain/support flag (a stage-1 mortality signal, ADR 0007):")
    _render(report, _counts(enrichment, "life_sustain_support"), "life_sustain_support")

    report.say("  510(k) summary availability -- this scopes the future PDF pass:")
    _render(report, _counts(enrichment, "statement_or_summary"), "statement_or_summary")

    known = enrichment.filter(F.col("review_time_days").isNotNull())
    known_count = known.count()
    report.say()
    report.say(f"  FDA review time, where both dates are known ({known_count:,} rows):")
    if known_count:
        stats = known.agg(
            F.min("review_time_days").alias("min"),
            F.expr("percentile_approx(review_time_days, 0.5)").alias("median"),
            F.avg("review_time_days").alias("mean"),
            F.max("review_time_days").alias("max"),
        ).collect()[0]
        report.say(
            f"    min {stats['min']:,}d   median {int(stats['median']):,}d   "
            f"mean {stats['mean']:.0f}d   max {stats['max']:,}d"
        )


def _documents_section(report: Report, documents: DataFrame, enrichment: DataFrame) -> None:
    """The 510(k) Summary fetch (ADR 0018), judged on each document's latest fetch."""
    from pyspark.sql.window import Window

    report.rule("DOCUMENTS  bronze_summary_documents  (510(k) Summaries, ADR 0018)")
    report.say(f"  fetches across all runs        : {documents.count():>7,}")

    newest = Window.partitionBy("submission_number").orderBy(F.desc("fetched_at"))
    latest = (
        documents.withColumn("_rank", F.row_number().over(newest))
        .filter(F.col("_rank") == 1)
        .drop("_rank")
    )
    found = latest.filter(F.col("content_sha256").isNotNull())
    found_count = found.count()
    missed = latest.count() - found_count

    summaries = (
        enrichment.filter(F.lower(F.trim(F.col("statement_or_summary"))) == "summary")
        .select("submission_number")
        .distinct()
    )
    eligible = summaries.count()
    covered = found.join(summaries, "submission_number").count()
    pct = 100 * covered / eligible if eligible else 0.0
    report.say(f"  Summary filings with a PDF     : {covered:,} / {eligible:,}  ({pct:.1f}%)")
    report.say(f"  latest fetch found nothing     : {missed:,}")
    if found_count == 0:
        report.problem("no Summary PDF was obtained -- check accessdata.fda.gov reachability")

    report.say("  text layer (finding 0011: 51 text / 8 mixed / 1 image of 60):")
    _render(report, _counts(found, "text_class"), "text_class")


def _extraction_section(report: Report, extraction: DataFrame) -> None:
    """What the deterministic extractors read out of the Summaries (ADR 0019)."""
    from registry.schemas import DocumentExtractionRecord
    from registry.transform import document_extraction

    report.rule("EXTRACTION  silver_document_extraction  (predicates, cybersecurity, PCCP)")
    records = [DocumentExtractionRecord.model_validate(r.asDict()) for r in extraction.collect()]
    versions = sorted({r.extractor_version for r in records})
    report.say(f"  extractor version(s)           : {', '.join(versions) or '-'}")
    for line in document_extraction.summarise(records).lines():
        report.say(f"  {line}")


@dataclass
class PccpYear:
    """One decision year of the PCCP adoption trend (ADR 0020)."""

    year: int | None
    documents: int = 0  # Summaries extracted for devices decided that year
    clearance_letter: int = 0  # the FDA letter sentence (finding 0019's column)
    summary_statement: int = 0  # Summary-body positives without a letter sentence

    @property
    def has_pccp(self) -> int:
        return self.clearance_letter + self.summary_statement

    def _cell(self, count: int) -> str:
        pct = 100 * count / self.documents if self.documents else 0.0
        return f"{count:,} / {self.documents:,} ({pct:.1f}%)"

    @property
    def letter_cell(self) -> str:
        return self._cell(self.clearance_letter)

    @property
    def has_pccp_cell(self) -> str:
        return self._cell(self.has_pccp)


def pccp_by_year(rows: Iterable[tuple[int | None, str]]) -> list[PccpYear]:
    """``(decision year, pccp_method)`` per extracted Summary -> one row per year.

    The two positive methods are kept apart so the letter column is finding 0019's
    table exactly, and the Summary-body positives stay visible beside it. Every other
    method (negatives, ``no_text``) counts only toward the year's documents.
    """
    years: dict[int | None, PccpYear] = {}
    for year, method in rows:
        y = years.setdefault(year, PccpYear(year))
        y.documents += 1
        if method == "clearance_letter":
            y.clearance_letter += 1
        elif method == "summary_statement":
            y.summary_statement += 1
    return sorted(years.values(), key=lambda y: (y.year is None, y.year or 0))


def pccp_year_lines(years: list[PccpYear]) -> list[str]:
    if not years:
        return []
    lines = [
        f"{'year':<6}{'documents':>10}  {'clearance_letter':<22}"
        f"{'summary_statement':>17}  {'has_pccp (either)':<22}"
    ]
    for y in years:
        label = str(y.year) if y.year is not None else "<null>"
        lines.append(
            f"{label:<6}{y.documents:>10,}  {y.letter_cell:<22}"
            f"{y.summary_statement:>17,}  {y.has_pccp_cell:<22}"
        )
    return lines


def _pccp_trend_section(report: Report, silver: DataFrame, extraction: DataFrame) -> None:
    """PCCP adoption by decision year: stock, not movement (ADR 0020)."""
    joined = extraction.select("submission_number", "pccp_method").join(
        silver.select(
            "submission_number",
            F.year("decision_date").alias("year"),
            F.lit(True).alias("_in_silver"),
        ),
        "submission_number",
        "left",
    )
    collected = joined.select("year", "pccp_method", "_in_silver").collect()
    # Two different reasons a Summary has no decision year to bucket: its device is
    # not in silver at all, or its silver row has a null decision_date (a parse gap
    # the SILVER section already flags). Counted apart so the line names the right one.
    no_row = sum(1 for r in collected if r["_in_silver"] is None)
    no_date = sum(1 for r in collected if r["_in_silver"] and r["year"] is None)
    years = pccp_by_year((r["year"], r["pccp_method"]) for r in collected if r["year"] is not None)
    report.say()
    report.say("  PCCP share by decision year (ADR 0020; stock, not movement -- finding 0019's")
    report.say("  table is the clearance_letter column):")
    for line in pccp_year_lines(years):
        report.say(f"    {line}")
    if no_row:
        report.say(f"    ({no_row:,} document(s) not in silver_devices: no decision year)")
    if no_date:
        report.say(f"    ({no_date:,} document(s) whose silver row has a null decision_date)")


def source_shift_lines(shift: Mapping[tuple[str | None, str | None], int]) -> list[str]:
    """Render `leads.language_source_shift`: what moves to the device's own text."""
    total = sum(shift.values())
    moved = {b: n for (b, a), n in shift.items() if a == "summary_intended_use" and b != a}
    lines = [
        f"rows                           : {total:,}",
        f"source moves to summary_intended_use: {sum(moved.values()):,}"
        + (
            "  (from "
            + ", ".join(f"{b or 'no source'} {n:,}" for b, n in sorted(moved.items(), key=str))
            + ")"
            if moved
            else ""
        ),
        f"  of which newly flagged       : {moved.get(None, 0):,}",
        f"unchanged                      : "
        f"{sum(n for (b, a), n in shift.items() if a == b and a is not None):,}",
        f"no source either way           : {shift.get((None, None), 0):,}",
    ]
    return lines


def _intended_use_section(
    report: Report, spark: SparkSession, settings: Settings, silver: DataFrame
) -> None:
    """Device-level intended use and what it does to `mortality_language` (ADR 0021)."""
    from registry.schemas import IntendedUseRecord
    from registry.transform import intended_use

    report.rule("INTENDED USE  silver_intended_use  (the device's own statement, ADR 0021)")
    rows = tables.read_table(spark, settings, intended_use.INTENDED_USE_TABLE).collect()
    records = [IntendedUseRecord.model_validate(r.asDict()) for r in rows]
    versions = sorted({r.extractor_version for r in records})
    report.say(f"  extractor version(s)           : {', '.join(versions) or '-'}")
    for line in intended_use.summarise(records).lines():
        report.say(f"  {line}")

    ctx = leads.LeadContext.load(spark, settings)
    devices = [r.asDict() for r in silver.select("submission_number", "device_name").collect()]
    report.say()
    report.say("  mortality_language source before -> after, every silver device as a lead:")
    for line in source_shift_lines(leads.language_source_shift(devices, ctx)):
        report.say(f"    {line}")
    if tables.table_exists(spark, settings, leads.LEADS_TABLE):
        recorded = [
            r.asDict()
            for r in tables.read_table(spark, settings, leads.LEADS_TABLE)
            .select("submission_number", "device_name")
            .collect()
        ]
        report.say("  ... and over the leads already recorded:")
        for line in source_shift_lines(leads.language_source_shift(recorded, ctx)):
            report.say(f"    {line}")


# How many confirmed devices `inspect` lists. The section prints the total first, and
# the header says when the list is clipped, so a long mart is never silently truncated.
GOLD_DEVICES_SHOWN = 15


def _gold_section(report: Report, mart: DataFrame) -> None:
    rows = mart.count()
    report.rule("GOLD  gold_mortality_relevant  (confirmed devices, ADR 0007/0014)")
    report.say(f"  rows (devices with a confirmed mortality/MACE judgement) : {rows:,}")

    # An empty mart is the honest pre-curation state, not a defect: the mart gates
    # on the confirmed flag alone, so it stays empty until someone curates the seed
    # (ADR 0014). Reporting it is the point -- before this section, `inspect` could
    # not tell a full mart from an empty one; it showed neither.
    if rows == 0:
        report.say(
            "  empty -- nothing has a confirmed mortality judgement yet. Seed "
            "config/mortality_seed.yaml, then run: uv run registry build-mart"
        )
        return

    # `keyword_disagrees`: a curator confirmed a device the stage-1 keyword pass did
    # not flag -- the row most worth a second look (ADR 0014 Decision 2), and a live
    # measure of how much the stage-1 keyword list is still missing.
    disagrees = mart.filter(F.col("keyword_disagrees")).count()
    report.say(
        f"  stage-1 keyword_disagrees (curator confirmed, keyword missed) : "
        f"{disagrees:,} / {rows:,}"
    )

    report.say()
    report.say("  specialty categories of the confirmed devices (the panel is not the filter):")
    _render(report, _counts(mart, "specialty_category"), "specialty_category")

    report.say()
    report.say("  review method (ADR 0007 provenance -- every judgement is audited):")
    _render(report, _counts(mart, "mortality_review_method"), "mortality_review_method")

    report.say()
    shown = (
        f" (showing the newest {GOLD_DEVICES_SHOWN:,} of {rows:,})"
        if rows > GOLD_DEVICES_SHOWN
        else ""
    )
    report.say(f"  the confirmed devices, newest first{shown} (* = stage-1 keyword_disagrees):")
    for row in (
        mart.orderBy(F.col("decision_date").desc_nulls_last(), F.col("submission_number"))
        .select(
            "submission_number",
            "decision_date",
            "device_name",
            "applicant_resolved",
            "keyword_disagrees",
        )
        .limit(GOLD_DEVICES_SHOWN)
        .collect()
    ):
        star = " *" if row["keyword_disagrees"] else ""
        name = (row["device_name"] or "")[:40]
        applicant = row["applicant_resolved"] or ""
        report.say(
            f"    {row['submission_number']:<11} {row['decision_date']}  {name:<40}  {applicant}{star}"
        )


# How many leads `inspect` lists, newest detection first; clipped like the gold list.
LEADS_SHOWN = 15


def _leads_section(report: Report, lead_rows: DataFrame) -> None:
    rows = lead_rows.count()
    report.rule("LEADS  gold_device_leads  (movement worth a curator's look, ADR 0016/0017)")
    report.say(f"  rows (append-only, one per movement per snapshot pair) : {rows:,}")

    # No movement between snapshots is the normal case -- the FDA list does not
    # change every week -- and `registry monitor` creates the table on an empty
    # first run (ADR 0017), so empty is reported, not flagged.
    if rows == 0:
        report.say("  empty -- no movement worth a look has been recorded yet.")
        return

    # Every live category, zeros included: a missing line would read as "not
    # measured", a zero says "measured, nothing found". A lead carries one or more
    # categories, so these do not sum to the row count.
    report.say()
    report.say("  leads per category (a lead can carry several):")
    by_category = {
        r["category"]: r["count"]
        for r in lead_rows.select(F.explode("categories").alias("category"))
        .groupBy("category")
        .count()
        .collect()
    }
    width = max(len(c) for c in LEAD_CATEGORIES)
    for category in LEAD_CATEGORIES:
        report.say(f"    {category:<{width}}  {by_category.get(category, 0):>7,}")

    report.say()
    shown = f" (showing the newest {LEADS_SHOWN:,} of {rows:,})" if rows > LEADS_SHOWN else ""
    report.say(f"  the leads, newest detection first{shown}:")
    for row in (
        lead_rows.orderBy(F.col("detected_at").desc(), F.col("submission_number"))
        .select(
            "submission_number",
            "detected_at",
            "movement",
            "changed_fields",
            "categories",
            "device_name",
        )
        .limit(LEADS_SHOWN)
        .collect()
    ):
        changed = ",".join(row["changed_fields"] or []) or "-"
        name = (row["device_name"] or "")[:40]
        report.say(
            f"    {row['submission_number']:<11} {row['detected_at']:%Y-%m-%d}  "
            f"{row['movement']:<8} changed={changed}  [{','.join(row['categories'])}]  {name}"
        )


def _curated_sections(report: Report, spark: SparkSession, settings: Settings) -> None:
    """Enrichment, the Summary fetch, then the gold mart -- each needs enrichment."""
    if not tables.table_exists(spark, settings, "silver_device_enrichment"):
        report.say()
        report.say(
            "No silver_device_enrichment table yet. Run: uv run registry enrich-openfda --verbose"
        )
        return

    enrichment = tables.read_table(spark, settings, "silver_device_enrichment")
    _enrichment_section(report, enrichment)

    # The Summary fetch targets what enrichment marks `Summary` (ADR 0018), so its
    # section follows enrichment; it gates nothing downstream.
    if tables.table_exists(spark, settings, "bronze_summary_documents"):
        _documents_section(
            report, tables.read_table(spark, settings, "bronze_summary_documents"), enrichment
        )
        if tables.table_exists(spark, settings, "silver_document_extraction"):
            extraction = tables.read_table(spark, settings, "silver_document_extraction")
            _extraction_section(report, extraction)
            _pccp_trend_section(
                report, tables.read_table(spark, settings, "silver_devices"), extraction
            )
            if tables.table_exists(spark, settings, "silver_intended_use"):
                _intended_use_section(
                    report, spark, settings, tables.read_table(spark, settings, "silver_devices")
                )
            else:
                report.say()
                report.say(
                    "No silver_intended_use table yet. Run: uv run registry extract-intended-use"
                )
        else:
            report.say()
            report.say(
                "No silver_document_extraction table yet. Run: uv run registry extract-documents"
            )
    else:
        report.say()
        report.say(
            "No bronze_summary_documents table yet. Run, on a host that can reach "
            "accessdata.fda.gov: uv run registry fetch-summaries --verbose"
        )

    if not tables.table_exists(spark, settings, mortality_relevant.MART_TABLE):
        report.say()
        report.say(
            "No gold_mortality_relevant table yet. Run: uv run registry build-mart --verbose"
        )
        return

    _gold_section(report, tables.read_table(spark, settings, mortality_relevant.MART_TABLE))


def build_report(spark: SparkSession, settings: Settings | None = None) -> Report:
    """Inspect the configured lakehouse and return the lines plus a verdict."""
    settings = settings or get_settings()
    report = Report()
    report.say(
        f"lakehouse root: {settings.lakehouse_root}  (storage_mode={settings.storage_mode.value})"
    )

    if not tables.table_exists(spark, settings, "bronze_fda_ai_list"):
        report.problem(
            "no bronze_fda_ai_list table. Run: uv run registry ingest-fda-list --verbose"
        )
        return report

    _bronze_section(report, tables.read_table(spark, settings, "bronze_fda_ai_list"))

    if not tables.table_exists(spark, settings, "silver_devices"):
        report.say()
        report.say("No silver_devices table yet. Run: uv run registry build-silver --verbose")
        return report

    _silver_section(report, tables.read_table(spark, settings, "silver_devices"), settings)
    _curated_sections(report, spark, settings)

    # Leads are diffed from silver snapshots, not from enrichment or the mart, so
    # the section follows silver regardless of how far the curated chain got.
    if not tables.table_exists(spark, settings, leads.LEADS_TABLE):
        report.say()
        report.say("No gold_device_leads table yet. Run: uv run registry monitor")
        return report

    _leads_section(report, tables.read_table(spark, settings, leads.LEADS_TABLE))
    return report
