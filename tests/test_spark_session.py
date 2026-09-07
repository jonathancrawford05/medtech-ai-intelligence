"""Phase 0 acceptance: Delta write-and-read-back works locally."""

from __future__ import annotations

import pytest

from registry import tables


@pytest.mark.spark
def test_spark_session_has_delta_extensions(spark):
    assert "io.delta.sql.DeltaSparkSessionExtension" in spark.conf.get("spark.sql.extensions")
    assert spark.version.startswith("4.")


@pytest.mark.spark
def test_get_spark_is_idempotent(spark):
    from registry.spark_session import get_spark

    assert get_spark() is spark


@pytest.mark.spark
def test_write_and_read_back_a_delta_table(spark, lakehouse):
    """The Phase 0 smoke test from the development plan."""
    df = spark.createDataFrame([(1, "alpha"), (2, "beta")], "id int, name string")

    tables.write_table(df, lakehouse, "smoke_test", mode="overwrite")
    result = tables.read_table(spark, lakehouse, "smoke_test")

    assert result.count() == 2
    assert set(result.columns) == {"id", "name"}
    assert tables.table_exists(spark, lakehouse, "smoke_test")


@pytest.mark.spark
def test_append_preserves_history(spark, lakehouse):
    """Bronze tables are append-only, so appends must accumulate, not replace."""
    schema = "id int, name string"
    tables.write_table(spark.createDataFrame([(1, "a")], schema), lakehouse, "bronze_x")
    tables.write_table(spark.createDataFrame([(2, "b")], schema), lakehouse, "bronze_x")

    assert tables.read_table(spark, lakehouse, "bronze_x").count() == 2


@pytest.mark.spark
def test_table_exists_is_false_for_unwritten_table(spark, lakehouse):
    assert tables.table_exists(spark, lakehouse, "never_written") is False


@pytest.mark.spark
def test_delta_time_travel_is_available(spark, lakehouse):
    """Time travel is a headline reason for choosing Delta over plain parquet."""
    schema = "id int, name string"
    tables.write_table(spark.createDataFrame([(1, "a")], schema), lakehouse, "bronze_tt")
    tables.write_table(spark.createDataFrame([(2, "b")], schema), lakehouse, "bronze_tt")

    v0 = tables.read_table(spark, lakehouse, "bronze_tt", version=0)
    assert v0.count() == 1
