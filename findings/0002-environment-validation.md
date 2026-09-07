# 0002 — Environment validation: what has actually been executed, and where

**Date:** 2026-09-07 · **Status:** Open (one claim unverified) · **Component:** Phase 0 substrate

**Partly closed 2026-09-07:** the arm64 claim is now verified in CI — see below.

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
| **The image builds and Delta works on arm64** | CI `docker (ubuntu-24.04-arm)`, native runner, run 34129148007 |

## Assumed, not verified

**1. arm64 / Apple Silicon — CLOSED 2026-09-07.** The `docker` job now runs as a
matrix over `ubuntu-latest` and the native `ubuntu-24.04-arm` runner, so every
push builds and smoke-tests the image on both architectures. Native runners
rather than QEMU: emulating a JVM suite is 10-40x slower and prone to emulation
bugs, so a red job would say nothing about arm64 correctness.

Evidence from CI run 34129148007, job `docker (ubuntu-24.04-arm)`:

```text
"platform": "linux/arm64"
container arch: aarch64
JAVA_HOME=/opt/java -> /usr/lib/jvm/java-17-openjdk-arm64
openjdk version "17.0.20.1" 2026-08-18
Spark 4.0.1 up.
Delta round-trip OK (1 row).
```

The symlink resolves to `java-17-openjdk-**arm64**`, which is the point: the
original hard-coded `JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64` would have
failed on this runner. The arch-derivation is not a tidiness fix, it is load-bearing.

Docker Desktop on Apple Silicon runs `linux/arm64` containers, so this exercises the
same container architecture a Mac developer gets, and it stays closed on every push
rather than for one afternoon. It does **not** cover Docker Desktop's own bind-mount
semantics for the `./lakehouse` and `./config` volumes in `docker-compose.yml`, nor
a developer running with Rosetta amd64 emulation enabled. If `docker compose run`
ever misbehaves on a Mac specifically, look there and not at the JDK.

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
make docker-build               # both architectures are covered by CI on every push
docker compose run --rm test
```
