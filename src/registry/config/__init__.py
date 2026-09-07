"""Configuration package.

The Python settings module is namespaced under ``registry.config`` rather than a
top-level ``config`` package (as the development plan sketched) so it installs
cleanly into a wheel without claiming the very generic top-level name ``config``.
The repo-root ``config/`` directory is retained for *data* configuration --
curated YAML lookups such as the specialty taxonomy.
See docs/adr/0006-config-package-layout.md.
"""

from registry.config.settings import Settings, StorageMode, get_settings

__all__ = ["Settings", "StorageMode", "get_settings"]
