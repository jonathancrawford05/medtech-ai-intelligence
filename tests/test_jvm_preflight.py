"""Preflight check for a usable JVM.

Spark 4.0 needs JDK 17+, and a machine without one fails deep inside py4j with
`JAVA_GATEWAY_EXITED` -- a ~60-line traceback that names neither the cause nor a
remedy. This actually happened on a first live ingest (findings/0005), so the
guard converts it into one actionable sentence.

No JVM is started here: every probe is monkeypatched.
"""

from __future__ import annotations

import pytest

from registry import spark_session as mod


class TestJavaVersionParsing:
    @pytest.mark.parametrize(
        ("output", "expected"),
        [
            ('openjdk version "17.0.20.1" 2026-08-18', 17),
            ('openjdk version "21.0.10" 2026-01-20', 21),
            ('java version "1.8.0_401"', 8),
            ('openjdk version "11.0.22" 2024-01-16', 11),
            ("openjdk 17.0.8 2023-07-18", 17),
        ],
    )
    def test_extracts_the_major_version(self, output, expected):
        assert mod._parse_java_major(output) == expected

    @pytest.mark.parametrize(
        "output",
        [
            "",
            "The operation couldn't be completed. Unable to locate a Java Runtime.",
            "some unrelated text",
        ],
    )
    def test_returns_none_when_no_version_is_present(self, output):
        assert mod._parse_java_major(output) is None


class TestPreflight:
    def test_passes_when_a_supported_jdk_is_present(self, monkeypatch):
        monkeypatch.setattr(mod, "_probe_java_version", lambda: (17, "openjdk 17"))
        mod._require_jvm()  # must not raise

    def test_passes_on_a_newer_jdk(self, monkeypatch):
        monkeypatch.setattr(mod, "_probe_java_version", lambda: (21, "openjdk 21"))
        mod._require_jvm()

    def test_raises_when_no_java_is_found(self, monkeypatch):
        monkeypatch.setattr(
            mod, "_probe_java_version", lambda: (None, "Unable to locate a Java Runtime.")
        )
        with pytest.raises(mod.JavaRuntimeError) as excinfo:
            mod._require_jvm()
        message = str(excinfo.value)
        assert "JDK 17" in message
        # The remedies must be in the message: that is the whole point.
        assert "docker compose" in message
        assert "temurin" in message.lower()

    def test_raises_when_java_is_too_old(self, monkeypatch):
        monkeypatch.setattr(mod, "_probe_java_version", lambda: (11, 'openjdk version "11.0.22"'))
        with pytest.raises(mod.JavaRuntimeError) as excinfo:
            mod._require_jvm()
        assert "found Java 11" in str(excinfo.value)

    def test_error_names_the_scheduled_workflow_as_an_option(self, monkeypatch):
        monkeypatch.setattr(mod, "_probe_java_version", lambda: (None, ""))
        with pytest.raises(mod.JavaRuntimeError, match="Scheduled FDA ingest"):
            mod._require_jvm()

    def test_is_skipped_on_databricks(self, monkeypatch):
        """The runtime provides its own JVM, so get_spark must not probe for one.

        This drives `get_spark()` itself rather than asserting on `_on_databricks()`:
        the behaviour under test is that the preflight is *bypassed*, and only
        calling the real entry point can show that.
        """
        monkeypatch.setenv("DATABRICKS_RUNTIME_VERSION", "17.3")
        monkeypatch.setattr(mod, "_session", None)

        def _boom():
            raise AssertionError("must not probe for java on Databricks")

        monkeypatch.setattr(mod, "_probe_java_version", _boom)

        # Stand in for the runtime-provided session so no JVM is started here.
        sentinel = object()

        class _FakeBuilder:
            def getOrCreate(self):
                return sentinel

        class _FakeSparkSession:
            builder = _FakeBuilder()

        monkeypatch.setattr(mod, "SparkSession", _FakeSparkSession)

        assert mod.get_spark() is sentinel

    def test_preflight_runs_when_not_on_databricks(self, monkeypatch):
        """The counterpart: off Databricks, get_spark must consult the preflight."""
        monkeypatch.delenv("DATABRICKS_RUNTIME_VERSION", raising=False)
        monkeypatch.setattr(mod, "_session", None)
        monkeypatch.setattr(
            mod, "_probe_java_version", lambda: (None, "Unable to locate a Java Runtime.")
        )

        with pytest.raises(mod.JavaRuntimeError):
            mod.get_spark()


@pytest.mark.spark
def test_real_environment_passes_preflight():
    """Wherever the Spark tests can run, the preflight must agree."""
    mod._require_jvm()
