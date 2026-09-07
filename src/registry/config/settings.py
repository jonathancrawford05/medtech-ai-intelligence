"""Single source of truth for environment-driven configuration.

Every path, URL and feature flag the pipeline needs lives here. Transformation
code must never name a filesystem location directly -- it asks for a table by
logical name (``settings.table_ref("silver_devices")``) and this module decides
whether that resolves to a local Delta path or a Unity Catalog identifier.

That indirection is the entire migration story: moving to Databricks means
setting two environment variables, not editing pipeline code.
"""

from __future__ import annotations

import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# A Unity Catalog namespace is `catalog.schema` -- two dot-separated identifiers.
_CATALOG_NAMESPACE = re.compile(r"^[A-Za-z_][\w]*\.[A-Za-z_][\w]*$")


class StorageMode(StrEnum):
    """How a logical table name becomes something Spark can read.

    PATH
        Local / object-store Delta tables addressed by directory:
        ``spark.read.format("delta").load(ref)``.
    CATALOG
        Unity Catalog managed tables addressed by identifier:
        ``spark.read.table(ref)``. This is the Databricks target.
    """

    PATH = "path"
    CATALOG = "catalog"


class Settings(BaseSettings):
    """Environment-driven settings, prefixed ``REGISTRY_``."""

    model_config = SettingsConfigDict(
        env_prefix="REGISTRY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Lakehouse -------------------------------------------------------
    # Local: a directory ("./lakehouse"). Databricks: a namespace ("main.registry").
    lakehouse_root: str = "./lakehouse"
    storage_mode: StorageMode = StorageMode.PATH

    # ---- Spark -----------------------------------------------------------
    # Ignored on Databricks, where the session is provided by the runtime.
    spark_app_name: str = "medtech-ai-intelligence"
    spark_master: str = "local[*]"
    spark_driver_memory: str = "4g"
    spark_shuffle_partitions: int = 8  # local default; Databricks tunes its own
    spark_ui_enabled: bool = False

    # ---- Sources ---------------------------------------------------------
    # NOTE: verify this URL before the first live run -- the FDA has moved this
    # page before (see docs/adr/0005-fda-ai-list-acquisition.md).
    fda_ai_list_url: str = (
        "https://www.fda.gov/medical-devices/software-medical-device-samd/"
        "artificial-intelligence-enabled-medical-devices"
    )
    # The page's "Download a CSV File" link. Verified live 2026-09-06 (ADR 0009);
    # the ingester scrapes the page for this link if the known URL stops working,
    # so a media-id change degrades to discovery rather than breaking.
    fda_ai_list_csv_url: str = "https://www.fda.gov/media/178541/download?attachment"
    openfda_base_url: str = "https://api.fda.gov"
    openfda_api_key: str | None = None

    # ---- HTTP behaviour --------------------------------------------------
    http_timeout_seconds: float = 30.0
    http_max_retries: int = 4
    http_backoff_seconds: float = 2.0
    http_user_agent: str = "medtech-ai-intelligence/0.1 (research; contact via repo)"

    # ---- Curated data files ---------------------------------------------
    # Repo-root `config/` holds hand-maintained lookups (panel taxonomy, company
    # aliases). Overridable so a container or job can mount them elsewhere.
    config_dir: Path = Path(__file__).resolve().parents[3] / "config"

    # ---- Pipeline behaviour ---------------------------------------------
    refresh_cadence_days: int = Field(default=7, ge=1)
    # Secondary-source reconciliation is deferred until the primary pipeline is
    # solid (development plan section 7). Off by default.
    source_cross_check_enabled: bool = False

    @model_validator(mode="after")
    def _validate_root_matches_mode(self) -> Settings:
        if self.storage_mode is StorageMode.CATALOG and not _CATALOG_NAMESPACE.match(
            self.lakehouse_root
        ):
            raise ValueError(
                f"storage_mode=catalog requires lakehouse_root to be a "
                f"catalog.schema namespace (e.g. 'main.registry'), got {self.lakehouse_root!r}"
            )
        return self

    @property
    def specialty_taxonomy_path(self) -> Path:
        return self.config_dir / "specialty_taxonomy.yaml"

    @property
    def is_catalog_mode(self) -> bool:
        return self.storage_mode is StorageMode.CATALOG

    def table_ref(self, name: str) -> str:
        """Resolve a logical table name to a Spark-addressable reference.

        Returns a directory path in PATH mode and a fully-qualified Unity
        Catalog identifier in CATALOG mode. Callers should pair this with
        :func:`registry.tables.read_table` / ``write_table`` rather than
        branching on the mode themselves.
        """
        if not name or not name.strip():
            raise ValueError("table name must be a non-empty string")
        name = name.strip()
        if self.is_catalog_mode:
            return f"{self.lakehouse_root}.{name}"
        return f"{self.lakehouse_root.rstrip('/')}/{name}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so config is read once. Tests that manipulate the environment should
    construct ``Settings()`` directly, or call ``get_settings.cache_clear()``.
    """
    return Settings()
