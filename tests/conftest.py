"""Shared pytest fixtures.

The SparkSession is session-scoped: starting a JVM costs seconds, and every
Spark test can share one local session safely.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from registry.config.settings import Settings, StorageMode


@pytest.fixture(scope="session")
def spark():
    """A local Spark session with Delta enabled, shared across the test session."""
    from registry.spark_session import get_spark, stop_spark

    session = get_spark(
        Settings(
            spark_app_name="pytest-registry",
            spark_master="local[2]",
            spark_shuffle_partitions=2,
        )
    )
    yield session
    stop_spark()


@pytest.fixture
def lakehouse(tmp_path: Path) -> Settings:
    """Settings pointed at a throwaway lakehouse directory."""
    return Settings(lakehouse_root=str(tmp_path / "lakehouse"), storage_mode=StorageMode.PATH)


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch) -> Iterator[None]:
    """Stop a developer's real REGISTRY_* env vars from leaking into tests."""
    for key in list(os.environ):
        if key.startswith("REGISTRY_"):
            monkeypatch.delenv(key, raising=False)
    yield
