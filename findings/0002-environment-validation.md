# 0002 — Environment validation: what has actually been executed, and where

**Date:** 2026-09-07 · **Status:** Open (two claims unverified) · **Component:** Phase 0 substrate

Records which environment claims in `README.md` and `CLAUDE.md` rest on an actual
execution and which rest on reasoning. Both are legitimate; conflating them is not.

## Verified by execution

| Claim | Evidence |
|-------|----------|
| Spark 4.0.1 + Delta 4.0.1 start and work in local mode | Delta write, read-back, append accumulation and `versionAsOf` time travel all pass (`tests/test_spark_session.py`) |
| The stack runs on **JDK 21** | Full suite green on OpenJDK 21.0.10 in the agent container, 2026-09-06 |
| The stack runs on **JDK 17** | CI `test` job, Temurin 17, run 34123539363 |
| The Docker image builds and Delta works inside it | CI `docker` job builds `--target dev` and runs `registry smoke` in-container; passed in ~5 min, run 34123539363 |
| Suite state | 66 passed, 1 `live_network` deselected, 85.81% coverage against the 70% floor |
| Markdown gates | markdownlint 0 errors; all 27 relative links resolve |

## Assumed, not verified

**1. arm64 / Apple Silicon.** `CLAUDE.md` states the image works "natively on arm64
and amd64" because `JAVA_HOME` is arch-derived. The derivation is real — the
Dockerfile symlinks `$(dirname $(dirname $(readlink -f $(command -v java))))` to
`/opt/java`, which is genuinely architecture-independent — but **no arm64 build has
ever run.** Every `runs-on:` in `.github/workflows/ci.yml` is `ubuntu-latest`,
which is amd64; there is no `matrix`, no `platforms:` and no `--platform` anywhere.

The reasoning is sound and the claim is probably true. It is still untested, and
`CLAUDE.md` states it without qualification. Either someone runs
`make docker-build && docker compose run --rm test` on an Apple Silicon machine and
records it here, or the claim should be softened to "should work on arm64".

**2. Databricks `catalog` mode.** `Settings.table_ref()` and `registry.tables` are
unit-tested in `catalog` mode, but have never addressed a real Unity Catalog. The
Phase 5 acceptance criterion — that migration touches only `spark_session.py` and
settings — is therefore argued, not demonstrated. `CONTINUATION.md` §1 already says
so; recorded here because it is the project's central architectural bet and the one
most costly to be wrong about.

## Diagnostics worth searching for

Two traps cost real time. The literal error text is recorded so the next person
finds this by searching rather than rediscovering it.

**Spark driver binding in containers.**

```text
java.io.IOException: Failed to connect to /192.0.2.2:46883
Caused by: io.netty.channel.AbstractChannel$AnnotatedConnectException: Connection refused
```

The container hostname resolved to `192.0.2.2` — a TEST-NET-1 documentation
address. `spark.driver.host` is what the driver *advertises*; `spark.driver.bindAddress`
is what it *listens on*. Setting only one makes the driver advertise an address it
is not listening on, and every Spark test dies. `spark_session.py` pins both to
`127.0.0.1` whenever the master is `local*`. **Do not set one without the other.**

**Delta JARs are not in the `delta-spark` wheel.**

```bash
find .venv -iname "*delta*.jar"        # -> nothing
find ~/.ivy2* -iname "*delta*.jar"     # -> delta-spark_2.13-4.0.1.jar, delta-storage-4.0.1.jar
```

`delta-spark` ships Python only; the Scala JARs are Ivy-resolved from Maven on
first session start. That makes startup slow and fails outright on a restricted
network — which is why `scripts/warm_delta_jars.py` exists and why
`_delta_jars_on_classpath()` skips Maven when they are already staged. Run
`make install`, not a bare `uv sync`, or the first Spark call reaches for Maven.

## Network egress in agent environments

`fda.gov` and `api.fda.gov` return `403` to `CONNECT` from the agent proxy
(confirmed 2026-09-06). `pypi.org`, `files.pythonhosted.org` and Maven Central are
reachable. This is an environment policy, not a code problem — see
[finding 0001](0001-phase-1-live-ingestion-gap.md) for what it blocks.

## How to re-check

```bash
make lint && make test          # substrate, needs JDK 17+
make docker-build               # on an arm64 host, to close claim 1 above
docker compose run --rm test
```
