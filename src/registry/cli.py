"""Command-line entry point.

Thin by design: each command wires config to a pipeline function and reports
what happened. All logic lives in the modules, so it stays unit-testable
without shelling out.
"""

from __future__ import annotations

import logging
from pathlib import Path
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


@app.command("extract-documents")
def extract_documents(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Read predicates, cybersecurity and PCCP out of the stored Summary text.

    Offline and deterministic (ADR 0019): reads `bronze_summary_documents`, rebuilds
    `silver_document_extraction`, and prints the yields to compare with finding 0019.
    Run `build-silver` afterwards to carry the results onto `silver_devices`.
    """
    _setup_logging(verbose)
    from registry.spark_session import get_spark
    from registry.transform import document_extraction

    settings = get_settings()
    try:
        result = document_extraction.run(get_spark(settings), settings)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    table = settings.table_ref(document_extraction.EXTRACTION_TABLE)
    typer.echo(
        f"Wrote {result.written} row(s) to {table} "
        f"(extractor {document_extraction.EXTRACTOR_VERSION})"
    )
    for line in result.yields.lines():
        typer.echo(f"  {line}")


@app.command("extract-intended-use")
def extract_intended_use(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Read each device's own intended-use statement, verbatim, out of its Summary.

    Offline (ADR 0021): reuses the page roles `extract-documents` stored rather than
    re-segmenting, rebuilds `silver_intended_use`, and prints coverage against
    finding 0019's 99.7%. `monitor` and `curation-candidates` read the result.
    """
    _setup_logging(verbose)
    from registry.spark_session import get_spark
    from registry.transform import intended_use

    settings = get_settings()
    try:
        result = intended_use.run(get_spark(settings), settings)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    table = settings.table_ref(intended_use.INTENDED_USE_TABLE)
    typer.echo(
        f"Wrote {result.written} row(s) to {table} (extractor {intended_use.INTENDED_USE_VERSION})"
    )
    for line in result.yields.lines():
        typer.echo(f"  {line}")


@app.command("curation-candidates")
def curation_candidates(
    csv_path: Annotated[
        Path | None, typer.Option("--csv", help="Also write the batch to this CSV file.")
    ] = None,
    limit: Annotated[int, typer.Option(help="Candidates to print (the CSV has all).")] = 50,
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """The next curation batch: devices whose own intended use trips the stage-1
    keyword pass but which config/mortality_seed.yaml has not reviewed -- in a
    cardiometabolic specialty, or another one whose statement speaks of the heart.

    Also measures stage 1 on the curated devices: the curator's excerpt vs the
    device's full statement (`keyword_disagrees`). Evidence only; writes no flag.
    """
    _setup_logging(verbose)
    from registry.mart import curation_candidates as cc
    from registry.spark_session import get_spark

    settings = get_settings()
    try:
        report = cc.build(get_spark(settings), settings)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo("Stage 1 on the curated devices (ADR 0007; finding 0014 is the baseline):")
    for line in report.agreement.lines():
        typer.echo(f"  {line}")
    by_language = sum(c.scope == "statement" for c in report.candidates)
    typer.echo(
        f"\n{len(report.candidates)} candidate(s): {len(report.candidates) - by_language} among "
        f"{report.devices_in_scope} cardiometabolic device(s) "
        f"({report.in_scope_with_statement} with a statement read), {by_language} from "
        "another specialty by cardiovascular language:"
    )
    for c in report.candidates[:limit]:
        when = f"{c.decision_date:%Y-%m-%d}" if c.decision_date else "----------"
        pages = ",".join(map(str, c.intended_use_pages))
        typer.echo(
            f"  {c.submission_number:<10} {when}  {c.specialty_category:<14} {c.scope:<9} "
            f"[{', '.join(c.keyword_hits)}]  {c.device_name[:50]}"
        )
        typer.echo(f"      {c.intended_use_basis} p{pages}  {c.source_url or ''}")
        typer.echo(f"      {c.intended_use_text[:200]}")
    if len(report.candidates) > limit:
        typer.echo(f"  ... {len(report.candidates) - limit} more (use --csv for all)")
    if csv_path is not None:
        cc.write_csv(report.candidates, csv_path)
        typer.echo(f"Wrote {len(report.candidates)} candidate(s) to {csv_path}")


@app.command("extract-evidence")
def extract_evidence(
    only: Annotated[
        list[str] | None, typer.Option("--only", help="Submission(s) to send, in this order.")
    ] = None,
    limit: Annotated[int | None, typer.Option(help="At most this many documents.")] = None,
    specialty: Annotated[
        list[str] | None,
        typer.Option("--specialty", help="Only these specialty categories (e.g. cardiovascular)."),
    ] = None,
    model: Annotated[
        str | None, typer.Option(help="Ollama model; default REGISTRY_LLM_MODEL.")
    ] = None,
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Ask a local model (Ollama) for study evidence, then check every value.

    Mac only: needs a running Ollama server (ADR 0022). Scoped by design -- --only or
    --limit is required -- and paced for a fanless laptop (rests between batches,
    stops below REGISTRY_LLM_MIN_TOK_S). Resumable. Run it under `caffeinate -i`.
    Appends the raw answers to bronze_llm_responses, then rebuilds silver_llm_evidence.
    """
    _setup_logging(verbose)
    from registry.llm_extraction import run as llm_run
    from registry.llm_extraction.provider import OllamaProvider, ProviderError
    from registry.spark_session import get_spark

    if not only and limit is None:
        typer.echo(llm_run.UNSCOPED_RUN, err=True)
        raise typer.Exit(code=1)
    settings = get_settings()
    spark = get_spark(settings)
    try:
        outcome = llm_run.extract_run(
            spark,
            settings,
            OllamaProvider(settings, model=model),
            only=only or (),
            limit=limit,
            specialties=specialty or (),
        )
    except (ValueError, ProviderError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    statuses = ", ".join(f"{s} {n}" for s, n in sorted(outcome.statuses.items())) or "none"
    typer.echo(f"Run {outcome.run_id}: {len(outcome.records)} document(s) ({statuses})")
    if outcome.stopped:
        typer.echo(f"Stopped early: {outcome.stopped}")
    if outcome.records:
        _report_evidence(llm_run.build_evidence(spark, settings), settings)


def _report_evidence(build, settings) -> None:
    from registry.llm_extraction import run as llm_run

    typer.echo(f"Wrote {build.written} row(s) to {settings.table_ref(llm_run.EVIDENCE_TABLE)}")
    for line in build.yields.lines():
        typer.echo(f"  {line}")


@app.command("build-evidence")
def build_evidence(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Re-check every recorded model answer into silver_llm_evidence. Offline.

    Run after a change to the checks: bronze_llm_responses keeps every answer, so a
    rule change never needs the model again (ADR 0022).
    """
    _setup_logging(verbose)
    from registry.llm_extraction import run as llm_run
    from registry.spark_session import get_spark

    settings = get_settings()
    try:
        build = llm_run.build_evidence(get_spark(settings), settings)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _report_evidence(build, settings)


_DEFAULT_GOLD = Path("tests/fixtures/llm_extraction/gold_set_v1.csv")


@app.command("score-gold")
def score_gold(
    gold_path: Annotated[
        Path, typer.Option("--gold", help="The labelled gold CSV.")
    ] = _DEFAULT_GOLD,
    recorded: Annotated[
        Path | None,
        typer.Option(help="Score recorded responses in this directory instead of the lakehouse."),
    ] = None,
    prompt_version: Annotated[
        str | None, typer.Option(help="Which prompt's run to score (lakehouse).")
    ] = None,
    model_digest: Annotated[
        str | None, typer.Option(help="Which model's run to score, by digest prefix (lakehouse).")
    ] = None,
    bar: Annotated[
        float | None, typer.Option(help="Report each field against this precision.")
    ] = None,
    min_judged: Annotated[
        int, typer.Option(help="Judged rows a field needs to clear the bar.")
    ] = 10,
    rows: Annotated[bool, typer.Option("--rows", help="Also print every scored gold row.")] = False,
) -> None:
    """Score one run against a gold set: per field, per check status, per stratum.

    The precision that matters is of what the checks accept. A field ships only when
    it clears the agreed bar on a fresh gold draw (handoff §3 rule 6); this reports,
    it never decides.
    """
    from registry.llm_extraction import gold
    from registry.llm_extraction import run as llm_run

    if recorded is not None:
        evidence = llm_run.evidence_from_recorded(recorded)
    else:
        from registry.spark_session import get_spark

        settings = get_settings()
        evidence = llm_run.read_evidence(get_spark(settings), settings)
    evidence = [
        e
        for e in evidence
        if (prompt_version is None or e.prompt_version == prompt_version)
        and (model_digest is None or e.model_digest.startswith(model_digest))
    ]
    try:
        result = gold.score(gold.load_gold(gold_path), evidence)
    except ValueError as exc:
        typer.echo(f"{exc}\n(pick one with --prompt-version / --model-digest)", err=True)
        raise typer.Exit(code=1) from exc
    combo = {(e.model, e.model_digest[:12], e.prompt_version) for e in evidence}
    typer.echo(f"Gold {gold_path.name}: {len(result.scored)} row(s) against {sorted(combo)}")
    for line in result.lines(bar=bar, min_judged=min_judged):
        typer.echo(line)
    if rows:
        typer.echo("ROWS")
        for line in result.row_lines():
            typer.echo(line)


@app.command("enrich-predicates")
def enrich_predicates(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Fetch each primary predicate's decision date from openFDA (for predicate_age_days).

    Needs api.fda.gov, so it runs where `enrich-openfda` runs; cached like it (ADR 0010).
    """
    _setup_logging(verbose)
    from registry.spark_session import get_spark
    from registry.transform import predicate_dates

    settings = get_settings()
    try:
        written = predicate_dates.run(get_spark(settings), settings)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"Fetched {written} predicate decision date(s) into "
        f"{settings.table_ref(predicate_dates.DATES_TABLE)}"
    )


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
