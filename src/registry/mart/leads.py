"""The leads mart: movement worth a curator's look (ADR 0016, ADR 0017).

**This is not the gold mortality mart, and it must not inherit its filter.** The
gold mart gates on the curated confirmed flag -- precision, because silent
over-inclusion in "the list an underwriter reads" is unrecoverable (ADR 0007 /
0014). A lead has the opposite job: a brand-new device has not been curated yet,
so a curated gate would make this surface empty for exactly the devices it
exists to catch. Leads optimise for **recall and triage** and are built only
from signals available before curation:

``new_submission``      a submission number the previous snapshot did not have.
``cardiometabolic``     specialty category in the taxonomy's
                        ``mortality_relevant_categories`` (cardiovascular, metabolic).
``mortality_language``  the stage-1 keyword pass (`mortality_seed.keyword_flag`,
                        widened in finding 0014) over the text that exists
                        pre-curation: the device name, the openFDA product-code
                        definition, and curated intended-use text where a
                        curator happened to capture it. Device-level intended
                        use for new devices arrives with roadmap Issue 4.
``life_sustaining``     openFDA's life-sustain/support flag (enrichment tier 1).

Which movements are leads: every ``added`` device (``new_submission`` is itself
the roadmap's first category); a ``changed`` or ``removed`` device only when a
signal holds on either side of the change -- a device leaving the cardiovascular
panel is as worth a look as one joining it.

Removals are recorded only from a full-sized pull: the stamp mismatch that
identifies a removal fires for every device missing from the newest pull, so a
truncated pull would flood the leads with false removals. The mirror holds too:
after a short *previous* pull, devices that merely come back on the list (no
FDA-list field changed) are not recorded. Additions and other changes are.

The table is append-only and recorded once per snapshot pair (first detection),
so it is the rate-of-change time series the roadmap asks for; `lead_counts`
queries it by detection date or by clearance month.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

from registry.config.settings import Settings, get_settings
from registry.monitor import differ
from registry.schemas import LEAD_CATEGORIES, LeadRecord, spark_schema_for
from registry.transform import taxonomy
from registry.transform.enrichment import ENRICHMENT_TABLE
from registry.transform.mortality_seed import EVIDENCE_TABLE, keyword_flag

logger = logging.getLogger(__name__)

LEADS_TABLE = "gold_device_leads"

# Categories the roadmap names that no current source can populate. Deferred
# explicitly rather than shipped as always-empty columns: an empty column reads
# as "none found", which is a claim nobody has measured.
DEFERRED_CATEGORIES = {
    "pccp": (
        "No current source carries PCCP: absent from the public 510(k) Summary text "
        "(finding 0011, 0/60) and from every openFDA endpoint. Revisit with roadmap "
        "Issue 4's document pass."
    ),
    "foundation_model": (
        "No FDA or openFDA field marks a foundation-model clearance, and no curated "
        "keyword heuristic is defined yet. Needs one (curated like the mortality "
        "seed, with its own ADR) before it can be a category."
    ),
}

# Removals are recorded only when the newest pull carried at least this share of
# the previous pull's rows (~ the scheduled ingest's 1,500 / 1,614 floor).
MIN_PULL_FRACTION = 0.95

# Precedence when more than one text hits: the most device-specific first.
_LANGUAGE_SOURCES = ("intended_use_text", "classification_definition", "device_name")


@dataclass(frozen=True)
class LeadContext:
    """The pre-curation lookups a lead build needs, loaded once per run."""

    cardiometabolic_categories: frozenset[str]
    # submission -> openFDA product-code definition (enrichment tier 1)
    definitions: dict[str, str] = field(default_factory=dict)
    # submission -> openFDA life-sustain flag; absent = not enriched
    life_sustaining: dict[str, bool | None] = field(default_factory=dict)
    # submission -> curated intended-use text, where curation captured it.
    # Text only: the curated judgement is deliberately not loaded.
    intended_use: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, spark, settings: Settings | None = None) -> LeadContext:
        from registry import tables

        settings = settings or get_settings()
        definitions: dict[str, str] = {}
        sustaining: dict[str, bool | None] = {}
        if tables.table_exists(spark, settings, ENRICHMENT_TABLE):
            rows = (
                tables.read_table(spark, settings, ENRICHMENT_TABLE)
                .select("submission_number", "classification_definition", "life_sustain_support")
                .collect()
            )
            for r in rows:
                if r["classification_definition"]:
                    definitions[r["submission_number"]] = r["classification_definition"]
                sustaining[r["submission_number"]] = r["life_sustain_support"]

        intended_use: dict[str, str] = {}
        if tables.table_exists(spark, settings, EVIDENCE_TABLE):
            rows = (
                tables.read_table(spark, settings, EVIDENCE_TABLE)
                .select("submission_number", "intended_use_text")
                .collect()
            )
            intended_use = {r["submission_number"]: r["intended_use_text"] for r in rows}

        return cls(
            cardiometabolic_categories=taxonomy.load(settings).mortality_relevant_categories,
            definitions=definitions,
            life_sustaining=sustaining,
            intended_use=intended_use,
        )


def _language_source(row: dict[str, Any], ctx: LeadContext) -> str | None:
    key = row["submission_number"]
    texts = {
        "intended_use_text": ctx.intended_use.get(key),
        "classification_definition": ctx.definitions.get(key),
        "device_name": row.get("device_name"),
    }
    for source in _LANGUAGE_SOURCES:
        if keyword_flag(texts[source] or ""):
            return source
    return None


def _signals(rows: list[dict[str, Any]], ctx: LeadContext) -> dict[str, Any]:
    """Signals over one or both sides of a movement (any side holding counts)."""
    sources = [s for r in rows if (s := _language_source(r, ctx))]
    key = rows[0]["submission_number"]
    return {
        "signal_cardiometabolic": any(
            r["specialty_category"] in ctx.cardiometabolic_categories for r in rows
        ),
        "signal_mortality_language": bool(sources),
        "mortality_language_source": sources[0] if sources else None,
        "signal_life_sustaining": ctx.life_sustaining.get(key),
    }


def build_leads(
    movements: list[differ.Movement],
    ctx: LeadContext,
    *,
    prev: differ.SnapshotVersion,
    curr: differ.SnapshotVersion,
    detected_at: dt.datetime,
    include_removals: bool = True,
) -> list[LeadRecord]:
    """Turn classified movements into lead rows. Pure: no Spark, no curation."""
    records: list[LeadRecord] = []
    for move in movements:
        if move.movement == "removed" and not include_removals:
            continue
        # Current side first, so its text wins the source and its facts fill the row.
        sides = [r for r in (move.curr, move.prev) if r is not None]
        signals = _signals(sides, ctx)
        new = move.movement == "added"
        categories = [
            name
            for name, on in (
                ("new_submission", new),
                ("cardiometabolic", signals["signal_cardiometabolic"]),
                ("mortality_language", signals["signal_mortality_language"]),
                ("life_sustaining", signals["signal_life_sustaining"] is True),
            )
            if on
        ]
        if not categories:
            continue
        facts = sides[0]
        records.append(
            LeadRecord(
                detected_at=detected_at,
                prev_snapshot_id=prev.source_snapshot_id,
                curr_snapshot_id=curr.source_snapshot_id,
                prev_version=prev.version,
                curr_version=curr.version,
                submission_number=move.submission_number,
                movement=move.movement,
                changed_fields=list(move.changed_fields),
                categories=categories,
                device_name=facts["device_name"],
                applicant_resolved=facts.get("applicant_resolved"),
                decision_date=facts["decision_date"],
                pathway=facts["pathway"],
                specialty_category=facts["specialty_category"],
                specialty_panel=facts["specialty_panel"],
                product_code=facts["product_code"],
                device_class=facts.get("device_class"),
                signal_new_submission=new,
                source_url=facts["source_url"],
                **signals,
            )
        )
    return records


def pull_complete(prev_size: int, curr_size: int) -> bool:
    """Whether the newest pull is full-sized enough to trust its absences."""
    if prev_size <= 0:
        return True
    return curr_size >= MIN_PULL_FRACTION * prev_size


@dataclass
class LeadRun:
    """What one monitor run did, for the CLI and the log."""

    status: str  # "no_pair" | "history_barrier" | "already_recorded" | "recorded"
    prev_snapshot_id: str | None = None
    curr_snapshot_id: str | None = None
    leads: list[LeadRecord] = field(default_factory=list)
    removals_suppressed: int = 0
    relistings_suppressed: int = 0
    barrier_operation: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        return {c: sum(c in r.categories for r in self.leads) for c in LEAD_CATEGORIES}


def _already_recorded(spark, settings: Settings, prev: str, curr: str) -> bool:
    from registry import tables

    if not tables.table_exists(spark, settings, LEADS_TABLE):
        return False
    hits = tables.read_table(spark, settings, LEADS_TABLE).filter(
        (F.col("prev_snapshot_id") == prev) & (F.col("curr_snapshot_id") == curr)
    )
    return hits.limit(1).count() > 0


def run(spark, settings: Settings | None = None, *, now: dt.datetime | None = None) -> LeadRun:
    """Diff the two most recent snapshots and append their leads to the gold table."""
    from registry import tables

    settings = settings or get_settings()
    pairing = differ.pairing(spark, settings)
    if pairing.pair is None:
        if pairing.barrier is not None:
            return LeadRun(status="history_barrier", barrier_operation=pairing.barrier["operation"])
        return LeadRun(status="no_pair")

    prev, curr = pairing.pair
    result = LeadRun(
        status="recorded",
        prev_snapshot_id=prev.source_snapshot_id,
        curr_snapshot_id=curr.source_snapshot_id,
    )
    if _already_recorded(spark, settings, prev.source_snapshot_id, curr.source_snapshot_id):
        result.status = "already_recorded"
        return result

    prev_rows = differ.read_version(spark, settings, prev)
    curr_rows = differ.read_version(spark, settings, curr)
    moves = differ.classify(prev_rows, curr_rows, prev.source_snapshot_id, curr.source_snapshot_id)
    records = build_leads(
        moves,
        LeadContext.load(spark, settings),
        prev=prev,
        curr=curr,
        detected_at=now or dt.datetime.now(dt.UTC).replace(tzinfo=None),
    )

    prev_size = differ.pull_size(prev_rows, prev.source_snapshot_id)
    curr_size = differ.pull_size(curr_rows, curr.source_snapshot_id)
    if not pull_complete(prev_size, curr_size):
        kept = [r for r in records if r.movement != "removed"]
        result.removals_suppressed = len(records) - len(kept)
        records = kept
        logger.warning(
            "Snapshot %s carried %d rows against %d in %s (< %.0f%%); not recording "
            "%d removal lead(s) from a pull that may be truncated",
            curr.source_snapshot_id,
            curr_size,
            prev_size,
            prev.source_snapshot_id,
            MIN_PULL_FRACTION * 100,
            result.removals_suppressed,
        )
    if not pull_complete(curr_size, prev_size):
        # The mirror image: a short *previous* pull makes the next full pull bring
        # every device it missed back on the list (independent review of PR #14).
        relisted = {m.submission_number for m in moves if differ.is_relisting_only(m)}
        kept = [r for r in records if r.submission_number not in relisted]
        result.relistings_suppressed = len(records) - len(kept)
        records = kept
        logger.warning(
            "Snapshot %s carried %d rows against %d in %s (< %.0f%%); not recording "
            "%d relisting lead(s) that only undo a pull that may have been truncated",
            prev.source_snapshot_id,
            prev_size,
            curr_size,
            curr.source_snapshot_id,
            MIN_PULL_FRACTION * 100,
            result.relistings_suppressed,
        )
    result.leads = records

    # Append-only. An empty first run still creates the table, so consumers can
    # query it before any lead exists (the gold mart's convention, ADR 0014).
    if records or not tables.table_exists(spark, settings, LEADS_TABLE):
        df = spark.createDataFrame(
            [r.model_dump() for r in records], schema=spark_schema_for(LeadRecord)
        )
        tables.write_table(df, settings, LEADS_TABLE, mode="append")
    logger.info(
        "Recorded %d lead(s) for %s -> %s",
        len(records),
        prev.source_snapshot_id,
        curr.source_snapshot_id,
    )
    return result


_GRAINS = {
    "detection": lambda: F.to_date("detected_at"),
    "decision_month": lambda: F.trunc("decision_date", "month"),
}


def lead_counts(spark, settings: Settings | None = None, *, grain: str = "detection") -> DataFrame:
    """Leads per period per category: ``(period, category, leads)``, ordered.

    ``detection`` buckets by the day the movement was seen (the rate of change in
    the list); ``decision_month`` by clearance month (the rate of authorisation).
    """
    from registry import tables

    if grain not in _GRAINS:
        raise ValueError(f"unknown grain {grain!r}; expected one of {sorted(_GRAINS)}")
    settings = settings or get_settings()
    if tables.table_exists(spark, settings, LEADS_TABLE):
        source = tables.read_table(spark, settings, LEADS_TABLE)
    else:
        # Nothing has moved yet: an empty series with the right columns.
        source = spark.createDataFrame([], spark_schema_for(LeadRecord))
    return (
        source.select(_GRAINS[grain]().alias("period"), F.explode("categories").alias("category"))
        .groupBy("period", "category")
        .agg(F.count(F.lit(1)).alias("leads"))
        .orderBy("period", "category")
    )
