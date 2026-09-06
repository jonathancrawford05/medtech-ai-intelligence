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
    JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 \
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

# ---------------------------------------------------------------------------
FROM deps AS dev

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --all-groups

# Stage the Delta JARs onto Spark's classpath at build time, so container
# startup needs no Maven access and no Ivy resolution. This must run AFTER the
# final `uv sync` -- a later sync can reinstall pyspark and discard them.
RUN python scripts/warm_delta_jars.py \
    && python -c "import pathlib,pyspark; \
assert list((pathlib.Path(pyspark.__file__).parent/'jars').glob('delta-spark*.jar')), \
'Delta JARs missing from the Spark classpath'"

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

# As above: stage the JARs after the final sync, and fail the build if they
# are not actually on the classpath.
RUN python scripts/warm_delta_jars.py \
    && python -c "import pathlib,pyspark; \
assert list((pathlib.Path(pyspark.__file__).parent/'jars').glob('delta-spark*.jar')), \
'Delta JARs missing from the Spark classpath'"

RUN useradd --create-home --uid 1000 spark \
    && mkdir -p /app/lakehouse \
    && chown -R spark:spark /app /opt/venv
USER spark
ENV HOME=/home/spark

ENTRYPOINT ["registry"]
CMD ["--help"]
