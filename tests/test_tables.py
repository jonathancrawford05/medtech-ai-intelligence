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
            "SOME FUTURE OPERATION",
        ],
    )
    def test_skips_everything_else(self, operation):
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
