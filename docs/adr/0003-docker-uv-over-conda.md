# 0003 — Docker + uv for the local environment, not conda

**Status:** Accepted · **Date:** 2026-09-06
**Resolves:** open question 2 in the development plan (§7)

## Context

The development plan left the local environment open: a conda/mamba environment
pinning `pyspark`, `delta-spark` and a JDK, or a Docker image carrying all three.
Spark needs a JVM, and mismatched local Java versions are the single most common
way a "just run it locally" setup fails for a new contributor.

## Decision

Docker for the reproducible environment; `uv` for Python dependency management
inside and outside it.

- The image (JDK 17 + Python 3.11 + the pinned wheels) makes "no cluster needed"
  also mean "no local Java version conflicts".
- `uv` with a committed `uv.lock` gives fast, exactly reproducible resolution,
  and it works identically on a contributor's machine, in the image, and in CI —
  one dependency tool for all three contexts.
- Working without Docker stays fully supported (`make install && make test`) for
  anyone who already has a JDK; Docker is the guarantee, not a requirement.

## Consequences

- Contributors need Docker for the guaranteed-working path, or a local JDK 17/21
  for the native path.
- CI runs both: the native path for fast lint/test feedback, and an image build
  that executes the Phase 0 acceptance check inside the container, so the
  Dockerfile cannot rot unnoticed.
- conda is not used anywhere, so there is no environment.yml to keep in sync.

## Alternatives considered

**conda/mamba.** Solves the JDK problem by packaging a JDK, but it is a third
dependency-resolution system alongside pip metadata and the lockfile, and it does
not give the deployable artefact that Docker does.

**Neither — document a JDK install.** Cheapest to set up, most expensive per new
contributor, and it makes CI and local environments diverge.
