"""Command-line entry point.

Thin by design: each command wires config to a pipeline function and reports
what happened. All logic lives in the modules, so it stays unit-testable
without shelling out.
"""

from __future__ import annotations

import logging
from typing import Annotated

import typer

from registry.config.settings import get_settings

app = typer.Typer(
    help="FDA AI/ML-enabled medical device registry pipeline.",
    no_args_is_help=True,
    add_completion=False,
)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )


@app.command()
def config() -> None:
    """Print the resolved configuration (useful for verifying env wiring)."""
    settings = get_settings()
    typer.echo(settings.model_dump_json(indent=2, exclude={"openfda_api_key"}))
    typer.echo(f"\nbronze_fda_ai_list -> {settings.table_ref('bronze_fda_ai_list')}")


@app.command("ingest-fda-list")
def ingest_fda_list(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Fetch and parse, but do not write to bronze."
    ),
) -> None:
    """Pull the FDA AI-enabled device list into the bronze Delta table."""
    _setup_logging(verbose)
    from registry.ingest import fda_ai_list

    settings = get_settings()
    snapshot = fda_ai_list.fetch_raw(settings)
    records = snapshot.parse()
    typer.echo(
        f"Fetched {len(records)} rows from {snapshot.url} "
        f"({snapshot.content_kind}, snapshot {snapshot.snapshot_id})"
    )

    if dry_run:
        typer.echo("--dry-run: nothing written.")
        return

    from registry.spark_session import get_spark

    written = fda_ai_list.ingest_snapshot(get_spark(settings), snapshot, settings)
    typer.echo(f"Appended {written} rows to {settings.table_ref('bronze_fda_ai_list')}")


@app.command("build-silver")
def build_silver(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Rebuild `silver_devices` from the newest bronze pull.

    Writes nothing when silver is already built from the same bronze snapshot with
    identical rows (ADR 0015), so a re-run never mints a version to diff.
    """
    _setup_logging(verbose)
    from registry.spark_session import get_spark
    from registry.transform import bronze_to_silver

    settings = get_settings()
    written = bronze_to_silver.run(get_spark(settings), settings)
    if written:
        typer.echo(f"Wrote {written} rows to {settings.table_ref('silver_devices')}")
    else:
        typer.echo(
            f"{settings.table_ref('silver_devices')} is already current "
            "(same bronze snapshot, identical rows); nothing written"
        )


@app.command("enrich-openfda")
def enrich_openfda(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    limit: int = typer.Option(
        0, "--limit", help="Enrich only the first N devices (for a cheap trial run)."
    ),
) -> None:
    """Fetch openFDA facts for every silver device into `silver_device_enrichment`.

    Separate from `build-silver` on purpose (ADR 0013): rebuilding silver stays
    offline and free, and re-fetching is a deliberate act.
    """
    _setup_logging(verbose)
    from registry.spark_session import get_spark
    from registry.transform import enrichment

    settings = get_settings()
    written = enrichment.run(get_spark(settings), settings, limit=limit or None)
    typer.echo(f"Enriched {written} devices into {settings.table_ref('silver_device_enrichment')}")


@app.command("fetch-summaries")
def fetch_summaries(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    limit: int = typer.Option(0, "--limit", help="Fetch at most N documents (a cheap trial)."),
    only: Annotated[
        list[str] | None,
        typer.Option(
            "--only",
            help="Fetch exactly these K-numbers (repeat, or comma-separate), even if fetched.",
        ),
    ] = None,
) -> None:
    """Fetch 510(k) Summary PDFs into `bronze_summary_documents` as per-page text.

    Needs accessdata.fda.gov, so it runs on a network-permitted host only (ADR 0018).
    Resumable: documents already in bronze are skipped, so re-run after a halt.
    """
    _setup_logging(verbose)
    from registry.ingest import summary_documents
    from registry.spark_session import get_spark

    numbers = [n.strip().upper() for value in only or [] for n in value.split(",") if n.strip()]
    settings = get_settings()
    try:
        result = summary_documents.run(
            get_spark(settings), settings, limit=limit or None, only=numbers or None
        )
    except summary_documents.SummaryFetchError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        f"Fetched {result.attempted} document(s): {result.found} found, "
        f"{result.not_found} not found, {result.unchanged} unchanged since the last fetch"
    )
    typer.echo(
        f"Appended {result.written} row(s) to {settings.table_ref(summary_documents.BRONZE_TABLE)}"
    )
    for text_class, count in sorted(result.text_classes.items()):
        typer.echo(f"  {text_class:<12} {count}")
    if result.already_fetched:
        typer.echo(f"Skipped {result.already_fetched} already in bronze.")
    if result.deferred:
        typer.echo(
            f"  deferred: {len(result.deferred)} De Novo/PMA Summary filing(s) -- "
            "document URLs not verified (ADR 0018)"
        )
    if result.halted:
        typer.echo(
            f"Stopped early: {result.halted}. Everything fetched so far is in bronze; "
            "wait, then re-run to resume.",
            err=True,
        )
        raise typer.Exit(code=1)


@app.command("build-mart")
def build_mart(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Load the curated mortality seed and rebuild the gold mortality mart.

    Only devices with a *confirmed* judgement qualify (ADR 0007): a keyword hit is
    a lead to review, never an answer.
    """
    _setup_logging(verbose)
    from registry.mart import mortality_relevant
    from registry.spark_session import get_spark
    from registry.transform import mortality_seed

    settings = get_settings()
    spark = get_spark(settings)

    curated = mortality_seed.run(spark, settings)
    typer.echo(f"Loaded {curated} curated judgement(s) into {mortality_seed.EVIDENCE_TABLE}")

    written = mortality_relevant.run(spark, settings)
    typer.echo(f"Wrote {written} row(s) to {settings.table_ref(mortality_relevant.MART_TABLE)}")
    if curated and not written:
        typer.echo("Note: judgements exist but none are confirmed True.")


@app.command()
def monitor(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Diff the two most recent silver snapshots and append leads to `gold_device_leads`.

    Leads are built from pre-curation signals (ADR 0016) and recorded once per
    snapshot pair (ADR 0017), so re-running is safe.
    """
    _setup_logging(verbose)
    from registry.mart import leads
    from registry.spark_session import get_spark

    settings = get_settings()
    result = leads.run(get_spark(settings), settings)
    table = settings.table_ref(leads.LEADS_TABLE)

    if result.status == "no_pair":
        typer.echo("Fewer than two distinct snapshots in silver; nothing to diff.")
    elif result.status == "history_barrier":
        typer.echo(
            f"Not diffing: unrecognised Delta operation {result.barrier_operation!r} sits "
            "between the snapshots in silver's history, so the pair cannot be trusted. "
            "To set a new baseline, run `registry build-silver` against the current "
            "bronze now, before the next ingest; the following pull then diffs against "
            "it. If the operation never changes rows, classify it in registry/tables.py.",
            err=True,
        )
        raise typer.Exit(code=1)
    elif result.status == "already_recorded":
        typer.echo(
            f"Leads for {result.prev_snapshot_id} -> {result.curr_snapshot_id} are already "
            f"in {table}; nothing appended."
        )
    else:
        typer.echo(
            f"Recorded {len(result.leads)} lead(s) for {result.prev_snapshot_id} -> "
            f"{result.curr_snapshot_id} in {table}"
        )
        for category, count in result.counts.items():
            typer.echo(f"  {category:<20} {count}")
        if result.removals_suppressed:
            typer.echo(
                f"Warning: {result.removals_suppressed} removal lead(s) not recorded; the "
                "newest pull looks truncated.",
                err=True,
            )
        if result.relistings_suppressed:
            typer.echo(
                f"Warning: {result.relistings_suppressed} relisting lead(s) not recorded; "
                "the previous pull looks truncated.",
                err=True,
            )
    for category, reason in leads.DEFERRED_CATEGORIES.items():
        typer.echo(f"  deferred: {category} -- {reason}")


@app.command()
def inspect() -> None:
    """Report what the local lakehouse holds, and exit non-zero if it looks wrong."""
    from registry import lakehouse_report
    from registry.spark_session import get_spark

    settings = get_settings()
    report = lakehouse_report.build_report(get_spark(settings), settings)
    for line in report.lines:
        typer.echo(line)
    if not report.ok:
        raise typer.Exit(code=1)


@app.command()
def smoke() -> None:
    """Verify Spark + Delta work end to end (the Phase 0 acceptance check)."""
    from registry import tables
    from registry.spark_session import get_spark

    settings = get_settings()
    spark = get_spark(settings)
    typer.echo(f"Spark {spark.version} up.")

    df = spark.createDataFrame([(1, "ok")], "id int, status string")
    tables.write_table(df, settings, "_smoke_test", mode="overwrite")
    count = tables.read_table(spark, settings, "_smoke_test").count()
    typer.echo(f"Delta round-trip OK ({count} row).")


if __name__ == "__main__":
    app()
