"""The leads mart (ADR 0016 filter set, ADR 0017 output surface).

The one thing under test above all: **a device is a lead before anyone curates
it.** The gold mortality mart gates on `mortality_confirmed_flag`; copying that
gate here would make the leads empty for exactly the devices they exist to catch
(handoff §4). Fixtures are synthetic by necessity -- see tests/monitor_fixtures.py.
"""

from __future__ import annotations

import datetime as dt
import inspect

import pytest

from registry.mart import leads
from registry.monitor import differ
from tests.monitor_fixtures import CURR, PREV, curr_rows, device, prev_rows

DETECTED = dt.datetime(2026, 10, 5, 9, 0)


def _ctx(**overrides):
    base = {
        "cardiometabolic_categories": frozenset({"cardiovascular", "metabolic"}),
        "definitions": {},
        "life_sustaining": {"DEN1000": True},
        "intended_use": {},
        "summary_intended_use": {},
    }
    base.update(overrides)
    return leads.LeadContext(**base)


def _versions():
    return differ.SnapshotVersion(1, PREV), differ.SnapshotVersion(3, CURR)


def _build(prev=None, curr=None, ctx=None, **kwargs):
    p, c = _versions()
    prev = prev_rows() if prev is None else prev
    curr = curr_rows() if curr is None else curr
    moves = differ.classify(prev, curr, PREV, CURR)
    return leads.build_leads(moves, ctx or _ctx(), prev=p, curr=c, detected_at=DETECTED, **kwargs)


def _summary(records):
    return {(r.submission_number, r.movement): r.categories for r in records}


class TestLeadFilter:
    def test_the_exact_leads_per_category(self):
        assert _summary(_build()) == {
            ("DEN1000", "added"): ["new_submission", "life_sustaining"],
            ("K300", "changed"): ["cardiometabolic"],
            ("K400", "changed"): ["mortality_language"],
            ("K500", "removed"): ["cardiometabolic"],
            ("K700", "added"): ["new_submission", "cardiometabolic"],
            ("K800", "added"): ["new_submission"],
            ("K900", "added"): ["new_submission", "mortality_language"],
        }

    def test_stock_is_not_a_lead(self):
        """K200 is cardiovascular but did not move: leads are movement, not stock."""
        keys = {r.submission_number for r in _build()}
        assert "K200" not in keys
        assert "K100" not in keys

    def test_a_movement_with_no_signal_is_not_a_lead(self):
        """K600 (radiology) left the list; nothing about it warrants a look."""
        assert "K600" not in {r.submission_number for r in _build()}

    def test_a_device_nobody_curated_is_a_lead(self):
        """The precision/recall split, stated as a test: no evidence row at all,
        and K900 is still a lead because of its own name."""
        lead = {r.submission_number: r for r in _build(ctx=_ctx(intended_use={}))}["K900"]
        assert lead.signal_mortality_language is True
        assert lead.mortality_language_source == "device_name"

    def test_the_product_code_definition_is_a_language_source(self):
        ctx = _ctx(definitions={"K800": "Software to predict the risk of death after surgery."})
        lead = {r.submission_number: r for r in _build(ctx=ctx)}["K800"]
        assert lead.categories == ["new_submission", "mortality_language"]
        assert lead.mortality_language_source == "classification_definition"

    def test_curated_intended_use_text_is_used_when_it_exists(self):
        ctx = _ctx(intended_use={"K800": "Estimates 30-day mortality."})
        lead = {r.submission_number: r for r in _build(ctx=ctx)}["K800"]
        assert lead.mortality_language_source == "intended_use_text"

    def test_a_change_out_of_scope_is_still_a_lead(self):
        """Signals are read on either side of a change: a device that left the
        cardiovascular panel is exactly a thing a curator should see."""
        prev = [device("K1", PREV, category="cardiovascular")]
        curr = [device("K1", CURR, category="radiology")]
        assert _summary(_build(prev, curr)) == {("K1", "changed"): ["cardiometabolic"]}

    def test_life_sustaining_is_none_when_not_enriched(self):
        lead = {r.submission_number: r for r in _build()}["K700"]
        assert lead.signal_life_sustaining is None
        assert {r.submission_number: r for r in _build()}["DEN1000"].signal_life_sustaining is True

    def test_removals_can_be_suppressed(self):
        keys = {(r.submission_number, r.movement) for r in _build(include_removals=False)}
        assert ("K500", "removed") not in keys
        assert ("K700", "added") in keys

    def test_rows_carry_the_pair_and_the_current_device_facts(self):
        lead = {r.submission_number: r for r in _build()}["K300"]
        assert (lead.prev_snapshot_id, lead.curr_snapshot_id) == (PREV, CURR)
        assert (lead.prev_version, lead.curr_version) == (1, 3)
        assert lead.detected_at == DETECTED
        assert lead.specialty_category == "cardiovascular"
        assert lead.changed_fields == ["specialty_category", "specialty_panel"]

    def test_a_removed_device_absent_from_silver_uses_its_last_row(self):
        records = _build([device("K1", PREV, category="cardiovascular")], [])
        assert [(r.submission_number, r.movement, r.device_name) for r in records] == [
            ("K1", "removed", "Device K1")
        ]

    def test_identical_snapshots_yield_no_leads(self):
        p, _ = _versions()
        moves = differ.classify(prev_rows(), prev_rows(), PREV, PREV)
        assert leads.build_leads(moves, _ctx(), prev=p, curr=p, detected_at=DETECTED) == []


def _pccp(rows: list[dict], values: dict[str, bool | None]) -> list[dict]:
    """The same silver rows with the document pass's `has_pccp` filled in."""
    return [
        {**r, "has_pccp": values[r["submission_number"]]} if r["submission_number"] in values else r
        for r in rows
    ]


class TestPccpSignal:
    """ADR 0020: `has_pccp` (from the document pass, ADR 0019) tags a movement,
    and is never itself a movement (PR #14's rule for derived fields)."""

    def test_populating_has_pccp_for_existing_devices_records_no_leads(self):
        """The first build after `extract-documents` fills `has_pccp` for every
        Summary filer at once. That is our pipeline catching up, not the FDA
        moving: it must not turn the 1,541 existing devices into leads."""
        p, c = _versions()
        before = prev_rows()  # has_pccp None everywhere: not yet extracted
        after = [
            {**r, "source_snapshot_id": CURR, "has_pccp": i % 2 == 0}
            for i, r in enumerate(prev_rows())
        ]
        assert any(r["has_pccp"] for r in after)
        moves = differ.classify(before, after, PREV, CURR)
        assert moves == []
        assert leads.build_leads(moves, _ctx(), prev=p, curr=c, detected_at=DETECTED) == []

    def test_a_has_pccp_flip_alone_records_no_leads(self):
        """A rule change (extractor version bump) that flips True <-> False."""
        p, c = _versions()
        before = _pccp(prev_rows(), {"K100": True, "K200": False})
        after = [
            {**r, "source_snapshot_id": CURR}
            for r in _pccp(prev_rows(), {"K100": False, "K200": True})
        ]
        moves = differ.classify(before, after, PREV, CURR)
        assert leads.build_leads(moves, _ctx(), prev=p, curr=c, detected_at=DETECTED) == []

    def test_a_new_device_with_a_pccp_is_tagged(self):
        lead = {r.submission_number: r for r in _build(curr=_pccp(curr_rows(), {"K800": True}))}[
            "K800"
        ]
        assert lead.categories == ["new_submission", "pccp"]
        assert lead.signal_pccp is True

    def test_a_changed_device_with_a_pccp_on_either_side_is_a_lead(self):
        """K100 (radiology, no other signal) gets renamed. Without a PCCP that is
        not a lead; with one on either side it is."""
        renamed = [
            {**r, "device_name": "Renamed"} if r["submission_number"] == "K100" else r
            for r in curr_rows()
        ]
        assert "K100" not in {r.submission_number for r in _build(curr=renamed)}

        for prev, curr in (
            (_pccp(prev_rows(), {"K100": True}), renamed),
            (prev_rows(), _pccp(renamed, {"K100": True})),
        ):
            lead = {r.submission_number: r for r in _build(prev=prev, curr=curr)}["K100"]
            assert lead.movement == "changed"
            assert lead.categories == ["pccp"]
            assert lead.signal_pccp is True

    def test_a_removed_device_with_a_pccp_is_a_lead(self):
        """K600 (radiology) leaving the list is not a lead on its own; a device
        that was cleared with a change-control plan leaving it is."""
        prev = _pccp(prev_rows(), {"K600": True})
        curr = _pccp(curr_rows(), {"K600": True})
        lead = {r.submission_number: r for r in _build(prev=prev, curr=curr)}["K600"]
        assert (lead.movement, lead.categories) == ("removed", ["pccp"])

    @pytest.mark.parametrize(
        ("prev", "curr", "signal"),
        [
            (None, None, None),  # no Summary, or not yet extracted: unknown
            (None, False, False),
            (False, False, False),
            (True, False, True),  # either side holding counts
            (None, True, True),
        ],
    )
    def test_signal_is_true_false_or_unknown(self, prev, curr, signal):
        """ADR 0012: None means "not read", never "no PCCP". K300 moves panel
        (a cardiometabolic lead either way), so its row exists in every case."""
        lead = {
            r.submission_number: r
            for r in _build(
                prev=_pccp(prev_rows(), {"K300": prev}), curr=_pccp(curr_rows(), {"K300": curr})
            )
        }["K300"]
        assert lead.signal_pccp is signal
        assert ("pccp" in lead.categories) is (signal is True)

    def test_no_pccp_is_not_a_category(self):
        """False is "read, no plan": it records nothing, like `life_sustaining`."""
        leads_ = _build(curr=_pccp(curr_rows(), {"K800": False}))
        lead = {r.submission_number: r for r in leads_}["K800"]
        assert lead.categories == ["new_submission"]
        assert lead.signal_pccp is False


# Every device's own Summary statement says "mortality": the most a populated
# `silver_intended_use` could ever push into the keyword pass.
_MORTAL = "Software that estimates the risk of in-hospital mortality."


def _statements(keys) -> dict[str, str]:
    return dict.fromkeys(keys, _MORTAL)


class TestSummaryIntendedUse:
    """ADR 0021: the device's own intended use (PR 4C) is the first text the
    stage-1 pass reads, and -- derived from the document, read at build time, never
    a silver_devices column -- it tags a movement and never makes one."""

    ALL = ("K100", "K200", "K300", "K400", "K500", "K600", "K700", "K800", "K900", "DEN1000")

    def test_populating_it_for_existing_devices_records_no_leads(self):
        """The first `extract-intended-use` run reads ~1,537 statements at once, and
        the next silver build is content-identical (the statement is not a silver
        column). That is our pipeline catching up, not the FDA moving: no leads,
        however much mortality language the statements carry."""
        p, c = _versions()
        before = prev_rows()
        after = [{**r, "source_snapshot_id": CURR} for r in prev_rows()]
        moves = differ.classify(before, after, PREV, CURR)
        assert moves == []
        ctx = _ctx(summary_intended_use=_statements(self.ALL))
        assert leads.build_leads(moves, ctx, prev=p, curr=c, detected_at=DETECTED) == []

    def test_it_never_turns_stock_into_a_lead(self):
        """Over the real fixture pair: with a statement for every device, the leads
        are exactly the movements -- K100/K200 (unmoved) stay out; only K600 (a
        removal with no other signal) gains a lead, from its own text."""
        without = _summary(_build())
        with_text = _summary(_build(ctx=_ctx(summary_intended_use=_statements(self.ALL))))
        assert not {k for k, _ in with_text} & {"K100", "K200"}
        assert set(with_text) - set(without) == {("K600", "removed")}
        assert all("mortality_language" in cats for cats in with_text.values()), (
            "every movement carries the statement's language"
        )

    def test_it_is_not_a_silver_column(self):
        """Structural half of the no-flood rule: the differ cannot see it."""
        from registry.schemas import DeviceRecord

        assert not any("intended_use" in f for f in DeviceRecord.model_fields)
        assert not any("intended_use" in f for f in differ.SOURCE_FIELDS)

    def test_it_outranks_every_other_source(self):
        ctx = _ctx(
            summary_intended_use={"K900": "Predicts death within 30 days."},
            intended_use={"K900": "Estimates 30-day mortality."},
            definitions={"K900": "Software to predict the risk of death."},
        )
        lead = {r.submission_number: r for r in _build(ctx=ctx)}["K900"]
        assert lead.mortality_language_source == "summary_intended_use"

    def test_a_statement_without_the_language_falls_through(self):
        """Precedence picks the first text that *hits*: K900's statement says nothing
        about death, so its name still carries the signal."""
        ctx = _ctx(summary_intended_use={"K900": "Analyses ECG recordings."})
        lead = {r.submission_number: r for r in _build(ctx=ctx)}["K900"]
        assert lead.mortality_language_source == "device_name"

    def test_the_source_shift_measure(self):
        """The 4C measure: which rows' source moves to the device's own text."""
        rows = [r for r in curr_rows() if r["submission_number"] in ("K100", "K900", "K800")]
        ctx = _ctx(
            summary_intended_use={"K100": _MORTAL, "K900": _MORTAL, "K800": "Analyses ECGs."},
        )
        assert leads.language_source_shift(rows, ctx) == {
            (None, "summary_intended_use"): 1,  # K100: newly flagged
            ("device_name", "summary_intended_use"): 1,  # K900: moves
            (None, None): 1,  # K800: read, no language
        }


class TestTheConfirmedFlagIsNeverTheGate:
    def test_the_module_never_reads_the_confirmed_flag(self):
        """Belt and braces on the P0 the handoff names. The behavioural tests
        prove leads appear without curation; this stops a future edit copying the
        gold mart's filter in at all."""
        assert "mortality_confirmed_flag" not in inspect.getsource(leads)


class TestDeferredCategories:
    def test_only_foundation_model_is_still_deferred(self):
        """ADR 0020 brings PCCP live; the foundation-model deferral stands."""
        assert set(leads.DEFERRED_CATEGORIES) == {"foundation_model"}
        assert "heuristic" in leads.DEFERRED_CATEGORIES["foundation_model"]

    def test_pccp_is_live(self):
        assert "pccp" in leads.LEAD_CATEGORIES

    def test_deferred_categories_are_not_live(self):
        assert set(leads.DEFERRED_CATEGORIES).isdisjoint(leads.LEAD_CATEGORIES)


class TestPullCompleteness:
    @pytest.mark.parametrize(
        ("prev", "curr", "complete"),
        [
            (1614, 1614, True),
            (1614, 1534, True),
            (1614, 1533, False),
            (1614, 200, False),
            (0, 10, True),
        ],
    )
    def test_removals_need_a_full_sized_pull(self, prev, curr, complete):
        assert leads.pull_complete(prev, curr) is complete


@pytest.mark.spark
class TestRun:
    def _silver(self, spark, settings, rows, snapshot):
        from registry import tables
        from registry.schemas import DeviceRecord, spark_schema_for
        from registry.transform import bronze_to_silver as bts

        stamp = bts.BuildStamp(snapshot, bts.content_hash([DeviceRecord(**r) for r in rows]))
        tables.write_table(
            spark.createDataFrame(rows, spark_schema_for(DeviceRecord)),
            settings,
            "silver_devices",
            mode="overwrite",
            merge_schema=True,
            user_metadata=stamp.to_metadata(),
        )

    def _two_snapshots(self, spark, settings):
        self._silver(spark, settings, prev_rows(), PREV)
        self._silver(spark, settings, curr_rows(), CURR)

    def _gold(self, spark, settings):
        from registry import tables

        return tables.read_table(spark, settings, leads.LEADS_TABLE)

    def test_records_the_leads_for_a_new_pair(self, spark, lakehouse):
        self._two_snapshots(spark, lakehouse)

        result = leads.run(spark, lakehouse, now=DETECTED)

        assert result.status == "recorded"
        assert (result.prev_snapshot_id, result.curr_snapshot_id) == (PREV, CURR)
        rows = {
            (r["submission_number"], r["movement"]): list(r["categories"])
            for r in self._gold(spark, lakehouse).collect()
        }
        assert rows == {
            ("DEN1000", "added"): ["new_submission"],  # not enriched in this test
            ("K300", "changed"): ["cardiometabolic"],
            ("K400", "changed"): ["mortality_language"],
            ("K500", "removed"): ["cardiometabolic"],
            ("K700", "added"): ["new_submission", "cardiometabolic"],
            ("K800", "added"): ["new_submission"],
            ("K900", "added"): ["new_submission", "mortality_language"],
        }
        assert result.counts == {
            "new_submission": 4,
            "cardiometabolic": 3,
            "mortality_language": 2,
            "life_sustaining": 0,
            "pccp": 0,
        }

    def test_the_gold_table_has_the_generated_schema(self, spark, lakehouse):
        from registry.schemas import LeadRecord, spark_schema_for

        self._two_snapshots(spark, lakehouse)
        leads.run(spark, lakehouse, now=DETECTED)

        # Names and types: Delta relaxes nullability when a write creates the table,
        # so a full StructType comparison would compare Delta's policy, not ours.
        def shape(schema):
            return [(f.name, f.dataType.simpleString()) for f in schema.fields]

        assert shape(self._gold(spark, lakehouse).schema) == shape(spark_schema_for(LeadRecord))

    def test_appends_to_a_table_written_before_signal_pccp(self, spark, lakehouse):
        """ADR 0020 adds a column to an append-only table that already exists on the
        maintainer's lakehouse. The append must widen it, and old rows read None."""
        from pyspark.sql.types import StructType

        from registry import tables
        from registry.schemas import LeadRecord, spark_schema_for

        old_schema = StructType(
            [f for f in spark_schema_for(LeadRecord).fields if f.name != "signal_pccp"]
        )
        old = _build()[0].model_dump()
        old.pop("signal_pccp")
        old["prev_snapshot_id"], old["curr_snapshot_id"] = "older-a", "older-b"
        tables.write_table(
            spark.createDataFrame([old], old_schema), lakehouse, leads.LEADS_TABLE, mode="append"
        )
        self._two_snapshots(spark, lakehouse)

        result = leads.run(spark, lakehouse, now=DETECTED)

        assert result.status == "recorded"
        gold = self._gold(spark, lakehouse)
        assert "signal_pccp" in gold.columns
        older = gold.filter("prev_snapshot_id = 'older-a'").collect()
        assert [r["signal_pccp"] for r in older] == [None]
        assert gold.filter(f"prev_snapshot_id = '{PREV}'").count() == len(result.leads)

    def test_a_rerun_on_the_same_pair_appends_nothing(self, spark, lakehouse):
        """Leads are first detections: re-running the monitor is safe."""
        self._two_snapshots(spark, lakehouse)
        leads.run(spark, lakehouse, now=DETECTED)
        first = self._gold(spark, lakehouse).count()

        again = leads.run(spark, lakehouse, now=DETECTED + dt.timedelta(days=1))

        assert again.status == "already_recorded"
        assert self._gold(spark, lakehouse).count() == first

    def test_a_third_snapshot_appends_and_keeps_history(self, spark, lakehouse):
        """Append-only: the leads table is the rate-of-change time series."""
        self._two_snapshots(spark, lakehouse)
        leads.run(spark, lakehouse, now=DETECTED)
        restamped = [
            {**r, "source_snapshot_id": "snapC"} if r["source_snapshot_id"] == CURR else r
            for r in curr_rows()
        ]
        third = [*restamped, device("K1100", "snapC", category="cardiovascular")]
        self._silver(spark, lakehouse, third, "snapC")

        result = leads.run(spark, lakehouse, now=DETECTED + dt.timedelta(days=7))

        assert result.status == "recorded"
        pairs = {
            (r["curr_snapshot_id"], r["submission_number"])
            for r in self._gold(spark, lakehouse).collect()
        }
        assert ("snapC", "K1100") in pairs
        assert (CURR, "K700") in pairs
        assert len([p for p in pairs if p[0] == "snapC"]) == 1

    def test_one_snapshot_records_nothing(self, spark, lakehouse):
        from registry import tables

        self._silver(spark, lakehouse, prev_rows(), PREV)
        result = leads.run(spark, lakehouse, now=DETECTED)
        assert result.status == "no_pair"
        assert result.leads == []
        assert not tables.table_exists(spark, lakehouse, leads.LEADS_TABLE)

    def test_an_uncurated_or_rejected_device_is_still_a_lead(self, spark, lakehouse):
        """A curator saying False (or nobody saying anything) does not hide
        movement. Evidence and enrichment are read from their tables."""
        from registry import tables
        from registry.schemas import (
            DeviceEnrichmentRecord,
            EvidenceRecord,
            spark_schema_for,
        )

        self._two_snapshots(spark, lakehouse)
        rejected = EvidenceRecord(
            submission_number="K800",
            intended_use_text="Reports risk of in-hospital mortality.",
            intended_use_source="https://example.test/K800",
            mortality_keyword_flag=True,
            mortality_confirmed_flag=False,
            mortality_review_method="human",
        )
        tables.write_table(
            spark.createDataFrame([rejected.model_dump()], spark_schema_for(EvidenceRecord)),
            lakehouse,
            "silver_evidence",
            mode="overwrite",
        )
        sustaining = DeviceEnrichmentRecord(
            submission_number="DEN1000",
            enriched_at=dt.datetime(2026, 9, 2),
            submission_found=False,
            classification_found=True,
            classification_definition="A ventilator.",
            life_sustain_support=True,
        )
        tables.write_table(
            spark.createDataFrame(
                [sustaining.model_dump()], spark_schema_for(DeviceEnrichmentRecord)
            ),
            lakehouse,
            "silver_device_enrichment",
            mode="overwrite",
        )

        result = leads.run(spark, lakehouse, now=DETECTED)

        by_key = {r.submission_number: r for r in result.leads}
        assert by_key["K800"].categories == ["new_submission", "mortality_language"]
        assert by_key["K800"].mortality_language_source == "intended_use_text"
        assert by_key["DEN1000"].categories == ["new_submission", "life_sustaining"]

    def test_the_summary_statement_is_read_and_floods_nothing(self, spark, lakehouse):
        """`silver_intended_use` is read by table: K900's statement becomes its source,
        and statements for the unmoved K100/K200 record nothing."""
        from registry import tables
        from registry.schemas import IntendedUseRecord, spark_schema_for
        from registry.transform import intended_use

        self._two_snapshots(spark, lakehouse)
        statements = [
            IntendedUseRecord(
                submission_number=key,
                extractor_version=intended_use.INTENDED_USE_VERSION,
                extracted_at=DETECTED,
                source_content_sha256="ab" * 32,
                segmentation_version="4b.2",
                intended_use_text=_MORTAL,
                intended_use_basis="form_3881",
                intended_use_pages=[3],
            ).model_dump()
            for key in ("K100", "K200", "K900")
        ]
        tables.write_table(
            spark.createDataFrame(statements, spark_schema_for(IntendedUseRecord)),
            lakehouse,
            intended_use.INTENDED_USE_TABLE,
            mode="overwrite",
        )

        result = leads.run(spark, lakehouse, now=DETECTED)

        by_key = {r.submission_number: r for r in result.leads}
        assert not {"K100", "K200"} & set(by_key)
        assert by_key["K900"].mortality_language_source == "summary_intended_use"
        assert len(result.leads) == 7  # the same seven movements as without statements

    def test_a_truncated_pull_suppresses_removals(self, spark, lakehouse):
        """Independent review of PR #13: the row-vs-commit mismatch flags every
        device missing from the newest pull, so a short pull must not flood the
        leads with false removals."""
        many = [device(f"K{i}", PREV, category="cardiovascular") for i in range(1, 21)]
        self._silver(spark, lakehouse, many, PREV)
        short = [{**r, "source_snapshot_id": CURR} for r in many[:5]] + many[5:]
        self._silver(spark, lakehouse, short, CURR)

        result = leads.run(spark, lakehouse, now=DETECTED)

        assert result.status == "recorded"
        assert result.removals_suppressed == 15
        assert [r for r in result.leads if r.movement == "removed"] == []

    def test_a_full_pull_after_a_truncated_one_suppresses_relistings(self, spark, lakehouse):
        """Independent review of PR #14 (P2): the mirror of the removal guard. A
        short pull built into silver, then a full one, would bring every missing
        device back as changed(on_list) -- the same flood the other way."""
        full = [device(f"K{i}", "snapOld", category="cardiovascular") for i in range(1, 21)]
        short = [{**r, "source_snapshot_id": PREV} for r in full[:5]] + full[5:]
        self._silver(spark, lakehouse, short, PREV)
        restored = [{**r, "source_snapshot_id": CURR} for r in full]
        restored.append(device("K99", CURR, category="cardiovascular", decided=dt.date(2026, 9, 1)))
        self._silver(spark, lakehouse, restored, CURR)

        result = leads.run(spark, lakehouse, now=DETECTED)

        assert result.status == "recorded"
        assert result.relistings_suppressed == 15
        assert result.removals_suppressed == 0
        assert [(r.submission_number, r.movement) for r in result.leads] == [("K99", "added")]

    def test_relistings_after_a_full_pull_are_kept(self, spark, lakehouse):
        """The guard is about truncation, not about relisting as such."""
        prev = [device(f"K{i}", PREV, category="cardiovascular") for i in range(1, 21)]
        prev.append(device("K50", "snapOld", category="cardiovascular"))
        self._silver(spark, lakehouse, prev, PREV)
        curr = [{**r, "source_snapshot_id": CURR} for r in prev]
        self._silver(spark, lakehouse, curr, CURR)

        result = leads.run(spark, lakehouse, now=DETECTED)

        assert result.relistings_suppressed == 0
        assert [(r.submission_number, r.movement, r.changed_fields) for r in result.leads] == [
            ("K50", "changed", ["on_list"])
        ]

    def test_an_unknown_operation_blocks_the_diff_and_says_so(self, spark, lakehouse, monkeypatch):
        from registry import tables

        self._silver(spark, lakehouse, prev_rows(), PREV)
        ref = lakehouse.table_ref("silver_devices")
        spark.sql(f"ALTER TABLE delta.`{ref}` ALTER COLUMN device_name COMMENT 'display name'")
        op = tables.table_history(spark, lakehouse, "silver_devices")[0]["operation"]
        monkeypatch.setattr(
            tables, "METADATA_ONLY_OPERATIONS", tables.METADATA_ONLY_OPERATIONS - {op}
        )
        self._silver(spark, lakehouse, curr_rows(), CURR)

        result = leads.run(spark, lakehouse, now=DETECTED)

        assert result.status == "history_barrier"
        assert result.barrier_operation == op
        assert not tables.table_exists(spark, lakehouse, leads.LEADS_TABLE)


@pytest.mark.spark
class TestLeadCounts:
    """The time-series query: rate of change, not a static count."""

    def _record(self, spark, settings, records):
        from registry import tables
        from registry.schemas import LeadRecord, spark_schema_for

        tables.write_table(
            spark.createDataFrame([r.model_dump() for r in records], spark_schema_for(LeadRecord)),
            settings,
            leads.LEADS_TABLE,
            mode="append",
        )

    def _lead(self, sub, detected, decided, categories):
        from registry.schemas import LeadRecord

        return LeadRecord(
            detected_at=detected,
            prev_snapshot_id="a",
            curr_snapshot_id=f"s{detected:%m%d}",
            prev_version=1,
            curr_version=2,
            submission_number=sub,
            movement="added",
            categories=categories,
            device_name=f"Device {sub}",
            decision_date=decided,
            pathway="510k",
            specialty_category="cardiovascular",
            specialty_panel="Cardiovascular",
            product_code="QIH",
            signal_new_submission=True,
            signal_cardiometabolic="cardiometabolic" in categories,
            signal_mortality_language=False,
            source_url="https://example.test",
        )

    def test_counts_by_detection_and_category(self, spark, lakehouse):
        w1, w2 = dt.datetime(2026, 10, 5), dt.datetime(2026, 10, 12)
        self._record(
            spark,
            lakehouse,
            [
                self._lead("K1", w1, dt.date(2026, 9, 3), ["new_submission", "cardiometabolic"]),
                self._lead("K2", w1, dt.date(2026, 9, 20), ["new_submission"]),
                self._lead("K3", w2, dt.date(2026, 10, 1), ["new_submission", "cardiometabolic"]),
            ],
        )
        rows = [tuple(r) for r in leads.lead_counts(spark, lakehouse, grain="detection").collect()]
        assert rows == [
            (dt.date(2026, 10, 5), "cardiometabolic", 1),
            (dt.date(2026, 10, 5), "new_submission", 2),
            (dt.date(2026, 10, 12), "cardiometabolic", 1),
            (dt.date(2026, 10, 12), "new_submission", 1),
        ]

    def test_counts_by_decision_month(self, spark, lakehouse):
        w1 = dt.datetime(2026, 10, 5)
        self._record(
            spark,
            lakehouse,
            [
                self._lead("K1", w1, dt.date(2026, 9, 3), ["new_submission", "cardiometabolic"]),
                self._lead("K2", w1, dt.date(2026, 9, 20), ["new_submission"]),
                self._lead("K3", w1, dt.date(2026, 10, 1), ["new_submission"]),
            ],
        )
        rows = [
            tuple(r) for r in leads.lead_counts(spark, lakehouse, grain="decision_month").collect()
        ]
        assert rows == [
            (dt.date(2026, 9, 1), "cardiometabolic", 1),
            (dt.date(2026, 9, 1), "new_submission", 2),
            (dt.date(2026, 10, 1), "new_submission", 1),
        ]

    def test_no_leads_table_is_an_empty_series_not_an_error(self, spark, lakehouse):
        """The live state until the FDA list first moves."""
        df = leads.lead_counts(spark, lakehouse)
        assert df.columns == ["period", "category", "leads"]
        assert df.count() == 0

    def test_an_unknown_grain_is_rejected(self, spark, lakehouse):
        with pytest.raises(ValueError, match="grain"):
            leads.lead_counts(spark, lakehouse, grain="weekly")
