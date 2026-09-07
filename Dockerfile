# syntax=docker/dockerfile:1.7
#
# Spark needs a JVM even in local mode, so this image exists mainly to make
# "no cluster needed" also mean "no local Java version conflicts".
#
# Two targets:
#   dev      -- dev dependencies + tests, the default for `make test` / compose
#   runtime  -- application dependencies only, for scheduled jobs
#
# JDK 17 is chosen to match Databricks Runtime 17.x; Spark 4.0 also supports 21.
# See docs/adr/0002-spark-delta-version-pinning.md.

FROM python:3.11-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# procps supplies `ps`, which Spark's launcher scripts shell out to.
RUN apt-get update && apt-get install -y --no-install-recommends \
        openjdk-17-jdk-headless \
        procps \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Derive JAVA_HOME from the JDK apt actually installed, so the image builds and
# runs on both amd64 (CI / Databricks parity) and arm64 (Apple Silicon dev)
# without a hard-coded, arch-specific path. See CONTINUATION.md §5.
RUN ln -sf "$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")" /opt/java
ENV JAVA_HOME=/opt/java

RUN pip install --no-cache-dir uv==0.8.17

WORKDIR /app

# ---------------------------------------------------------------------------
# Dependency layer: cached until the lockfile changes.
# ---------------------------------------------------------------------------
FROM base AS deps
COPY pyproject.toml uv.lock README.md ./
# `--no-install-project` resolves third-party deps without needing our source,
# so editing application code does not invalidate this layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --all-groups

# Resolve the Delta JARs from Maven ONCE, here in the cached dependency layer,
# and keep a copy in /opt/delta-jars. Only the script itself invalidates this,
# so editing application code never triggers another Maven fetch.
COPY scripts/warm_delta_jars.py ./scripts/
RUN python scripts/warm_delta_jars.py --stage-to /opt/delta-jars

# ---------------------------------------------------------------------------
FROM deps AS dev

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --all-groups

# Put the JARs back on the classpath AFTER the final sync, which can reinstall
# pyspark and discard them. This is a file copy from the layer above -- no
# network and no JVM -- so a source change costs milliseconds, not a Maven
# round trip. The script exits non-zero if they are not on the classpath.
RUN python scripts/warm_delta_jars.py --from-dir /opt/delta-jars

# Spark writes scratch data; give it a home a non-root user owns.
RUN useradd --create-home --uid 1000 spark \
    && mkdir -p /app/lakehouse /tmp/spark-events \
    && chown -R spark:spark /app /opt/venv /tmp/spark-events
USER spark
ENV HOME=/home/spark

CMD ["registry", "--help"]

# ---------------------------------------------------------------------------
FROM deps AS runtime

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY config ./config
COPY scripts ./scripts
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# As above: restore the JARs after the final sync, from the cached layer.
RUN python scripts/warm_delta_jars.py --from-dir /opt/delta-jars

RUN useradd --create-home --uid 1000 spark \
    && mkdir -p /app/lakehouse \
    && chown -R spark:spark /app /opt/venv
USER spark
ENV HOME=/home/spark

ENTRYPOINT ["registry"]
CMD ["--help"]
