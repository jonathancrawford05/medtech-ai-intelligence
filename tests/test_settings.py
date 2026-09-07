"""Settings + table-resolution tests.

The whole migration story rests on these: if transformation code never names a
filesystem path directly, pointing the pipeline at Databricks is a config change.
"""

import pytest

from registry.config.settings import Settings, StorageMode


def test_defaults_are_local_path_mode():
    s = Settings()
    assert s.storage_mode is StorageMode.PATH
    assert s.lakehouse_root == "./lakehouse"
    assert s.source_cross_check_enabled is False


def test_env_overrides_are_picked_up(monkeypatch):
    monkeypatch.setenv("REGISTRY_LAKEHOUSE_ROOT", "/mnt/lake")
    monkeypatch.setenv("REGISTRY_SOURCE_CROSS_CHECK_ENABLED", "true")
    s = Settings()
    assert s.lakehouse_root == "/mnt/lake"
    assert s.source_cross_check_enabled is True


def test_path_mode_resolves_to_a_delta_path():
    s = Settings(lakehouse_root="./lakehouse", storage_mode=StorageMode.PATH)
    assert s.table_ref("bronze_fda_ai_list") == "./lakehouse/bronze_fda_ai_list"


def test_catalog_mode_resolves_to_unity_catalog_identifier():
    """On Databricks, LAKEHOUSE_ROOT becomes a catalog.schema namespace."""
    s = Settings(lakehouse_root="main.registry", storage_mode=StorageMode.CATALOG)
    assert s.table_ref("bronze_fda_ai_list") == "main.registry.bronze_fda_ai_list"


def test_catalog_mode_requires_catalog_dot_schema():
    with pytest.raises(ValueError, match=r"catalog\.schema"):
        Settings(lakehouse_root="./lakehouse", storage_mode=StorageMode.CATALOG)


def test_is_catalog_mode_flag():
    assert Settings(storage_mode=StorageMode.PATH).is_catalog_mode is False
    assert (
        Settings(lakehouse_root="main.registry", storage_mode=StorageMode.CATALOG).is_catalog_mode
        is True
    )


def test_table_ref_rejects_empty_name():
    with pytest.raises(ValueError):
        Settings().table_ref("")
