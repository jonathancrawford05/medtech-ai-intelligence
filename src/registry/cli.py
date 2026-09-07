"""Command-line entry point.

Thin by design: each command wires config to a pipeline function and reports
what happened. All logic lives in the modules, so it stays unit-testable
without shelling out.
"""

from __future__ import annotations

import logging

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
