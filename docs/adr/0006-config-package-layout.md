# 0006 — Settings live at `registry.config`, not a top-level `config` package

**Status:** Accepted · **Date:** 2026-09-06
**Deviates from:** development plan §2 repository structure

## Context

The plan's repo layout put `settings.py` in a top-level `config/` package,
imported as `from config.settings import ...`. That works when everything runs
from the repo root, but it broke as soon as the project was installed as a
package and exposed a `registry` console script: `config` was not part of the
wheel, so the CLI failed with `ModuleNotFoundError: No module named 'config'`.

Packaging a top-level module named `config` would fix the import and create a
worse problem — `config` is about as generic a name as exists in
`site-packages`, and claiming it invites collisions with anything else installed
in the same environment.

## Decision

Split the two things the plan's `config/` was doing:

- **Python settings** move to `src/registry/config/settings.py`, imported as
  `from registry.config.settings import Settings`. Properly namespaced,
  installable, no collision risk.
- **Curated data files** stay in the repo-root `config/` directory —
  `specialty_taxonomy.yaml` today, company alias lookups later. These are
  hand-maintained YAML, read at runtime via `Settings.config_dir`, and mounted
  into the container so editing them needs no rebuild.

## Consequences

- The `registry` console script works from an installed wheel.
- Curated lookups stay where a human would look for them, editable without
  touching Python.
- `Settings.config_dir` is itself configurable, so a container or scheduled job
  can mount the lookups elsewhere.
- This is a documented deviation from the plan's §2 tree; anyone diffing the
  repo against the plan should read this rather than "correcting" it.
