"""Pure helpers in `registry.tables` that need no JVM.

The Delta-backed functions are exercised through their callers' Spark tests
(`test_bronze_to_silver.py::TestSnapshotGate`)."""

from __future__ import annotations

import pytest

from registry import tables


def _entry(version: int, operation: str, metadata: str | None = None) -> dict:
    return {"version": version, "operation": operation, "userMetadata": metadata}


class TestDataWrites:
    """ADR 0015: a version is found by what it was built from, so history must be
    walked over the commits that actually wrote rows -- an allowlist, because a
    denylist misses every metadata-only operation nobody thought of."""

    @pytest.mark.parametrize(
        "operation",
        [
            "WRITE",
            "CREATE TABLE AS SELECT",
            "CREATE OR REPLACE TABLE AS SELECT",
            "REPLACE TABLE AS SELECT",
            "MERGE",
            "UPDATE",
            "DELETE",
            "RESTORE",
        ],
    )
    def test_keeps_operations_that_write_rows(self, operation):
        assert tables.data_writes([_entry(3, operation)]) == [_entry(3, operation)]

    @pytest.mark.parametrize(
        "operation",
        [
            "SET TBLPROPERTIES",
            "UNSET TBLPROPERTIES",
            "CHANGE COLUMN",
            "ADD COLUMNS",
            "OPTIMIZE",
            "VACUUM START",
            "VACUUM END",
            "UPGRADE PROTOCOL",
        ],
    )
    def test_skips_known_metadata_only_operations(self, operation):
        # "SOME FUTURE OPERATION" used to be a case here. It moved to
        # TestUnknownOperations: an unlisted operation now stops the walk as an
        # unstamped write instead of being skipped (fail safe, PR B).
        assert tables.data_writes([_entry(3, operation)]) == []

    def test_keeps_order_and_payload(self):
        history = [
            _entry(4, "OPTIMIZE"),
            _entry(3, "WRITE", '{"a": 1}'),
            _entry(2, "SET TBLPROPERTIES"),
            _entry(1, "WRITE", '{"a": 0}'),
        ]
        assert tables.data_writes(history) == [
            _entry(3, "WRITE", '{"a": 1}'),
            _entry(1, "WRITE", '{"a": 0}'),
        ]


class TestClassifyOperation:
    """Three-way: a version to use, a commit to look past, or a stop."""

    @pytest.mark.parametrize("operation", sorted(tables.DATA_WRITE_OPERATIONS))
    def test_data_writes(self, operation):
        assert tables.classify_operation(operation) is tables.HistoryKind.DATA_WRITE

    @pytest.mark.parametrize("operation", sorted(tables.METADATA_ONLY_OPERATIONS))
    def test_metadata_only(self, operation):
        assert tables.classify_operation(operation) is tables.HistoryKind.METADATA_ONLY

    @pytest.mark.parametrize("operation", ["SOME FUTURE OPERATION", "REORG", "DROP COLUMNS", ""])
    def test_anything_else_is_unknown(self, operation):
        assert tables.classify_operation(operation) is tables.HistoryKind.UNKNOWN

    def test_the_two_lists_do_not_overlap(self):
        assert tables.DATA_WRITE_OPERATIONS.isdisjoint(tables.METADATA_ONLY_OPERATIONS)


class TestUnknownOperations:
    """Fail safe: an operation nobody listed might have rewritten the rows, so
    the walk must not step past it to an older stamp (that would let the gate
    skip a needed rebuild, and the differ pair across a change it cannot see)."""

    def _history(self):
        return [
            _entry(6, "WRITE", '{"stamp": "new"}'),
            _entry(5, "OPTIMIZE"),
            _entry(4, "SOME FUTURE OPERATION", '{"stamp": "looks-valid"}'),
            _entry(3, "WRITE", '{"stamp": "old"}'),
        ]

    def test_the_walk_stops_at_an_unknown_operation_as_an_unstamped_write(self):
        assert tables.data_writes(self._history()) == [
            _entry(6, "WRITE", '{"stamp": "new"}'),
            _entry(4, "SOME FUTURE OPERATION", None),
        ]

    def test_an_unknown_operation_on_top_leaves_no_usable_stamp(self):
        walk = tables.walk_history(self._history()[2:])
        assert walk.writes == [_entry(4, "SOME FUTURE OPERATION", None)]
        assert walk.barrier == _entry(4, "SOME FUTURE OPERATION", '{"stamp": "looks-valid"}')

    def test_a_walk_without_unknowns_has_no_barrier(self):
        walk = tables.walk_history([_entry(2, "OPTIMIZE"), _entry(1, "WRITE", "m")])
        assert walk.writes == [_entry(1, "WRITE", "m")]
        assert walk.barrier is None

    def test_the_operation_is_named_in_a_warning(self, caplog):
        with caplog.at_level("WARNING", logger="registry.tables"):
            tables.data_writes(self._history())
        assert "SOME FUTURE OPERATION" in caplog.text
        assert "version 4" in caplog.text
