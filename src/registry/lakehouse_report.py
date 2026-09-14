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

from dataclasses import dataclass, field

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from registry import tables
from registry.config.settings import Settings, get_settings
from registry.transform import taxonomy

# Raw columns bronze should have captured for every row. Emptiness here means the
# source changed shape or the parser lost a column, which silver cannot fix.
_BRONZE_COLUMNS = ("device_name", "applicant_raw", "decision_date_raw", "panel_raw", "product_code")

# Populated by the openFDA enrichment pass; all null until it runs (ADR 0012).
_ENRICHMENT_COLUMNS = (
    "device_class",
    "predicate_submission_number",
    "has_pccp",
    "cybersecurity_statement_present",
)


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
    pulls = (
        bronze.groupBy("source_snapshot_id")
        .agg(F.min("ingested_at").alias("ingested_at"), F.count("*").alias("rows"))
        .orderBy("ingested_at")
        .collect()
    )
    for row in pulls:
        report.say(f"    {row['ingested_at']}  {row['source_snapshot_id']}  {row['rows']:>7,} rows")
    report.say(f"  -> {len(pulls)} pull(s) retained")

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
    report.say("  openFDA enrichment coverage (0% is expected until that pass runs, ADR 0012):")
    for column in _ENRICHMENT_COLUMNS:
        filled = silver.filter(F.col(column).isNotNull()).count()
        pct = 100 * filled / rows if rows else 0.0
        report.say(f"    {column:<32} {filled:>7,} / {rows:,}  ({pct:.1f}%)")

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
    return report
