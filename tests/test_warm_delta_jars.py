"""Tests for the Delta-JAR staging script.

This is build tooling rather than pipeline code, but the Docker image depends on
its exit codes: the `deps` layer resolves once with ``--stage-to`` and each final
stage restores with ``--from-dir`` after its own ``uv sync``. If the guard stops
working, a broken image ships and the failure surfaces at container start as a
Maven fetch or a missing class, far from the cause.

Everything here is monkeypatched away from the real Spark install and Maven, so
these are fast and need neither a JVM nor a network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import warm_delta_jars as mod


@pytest.fixture
def classpath(tmp_path: Path, monkeypatch) -> Path:
    """A throwaway stand-in for pyspark/jars, so tests never touch the real venv."""
    target = tmp_path / "pyspark-jars"
    target.mkdir()
    monkeypatch.setattr(mod, "spark_jars_dir", lambda: target)
    return target


@pytest.fixture
def no_maven(monkeypatch):
    """Fail loudly if a code path reaches for Maven when it should not."""

    def _boom() -> list[Path]:
        raise AssertionError("resolve_delta_jars() was called; this path must not need Maven")

    monkeypatch.setattr(mod, "resolve_delta_jars", _boom)


def _make_jar(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    jar = directory / name
    jar.write_bytes(b"not really a jar")
    return jar


class TestFromDir:
    def test_copies_staged_jars_onto_the_classpath(self, tmp_path, classpath, no_maven):
        source = tmp_path / "staged"
        _make_jar(source, "delta-spark_2.13-4.0.1.jar")
        _make_jar(source, "delta-storage-4.0.1.jar")

        assert mod.main(["--from-dir", str(source)]) == 0
        assert sorted(p.name for p in classpath.glob("*.jar")) == [
            "delta-spark_2.13-4.0.1.jar",
            "delta-storage-4.0.1.jar",
        ]

    def test_needs_no_jvm_and_no_network(self, tmp_path, classpath, no_maven):
        """The whole point of the two-step staging: the post-sync step is a copy.

        `no_maven` raises if resolution is attempted, so this passing means the
        Docker `dev`/`runtime` stages genuinely do not reach Maven.
        """
        source = tmp_path / "staged"
        _make_jar(source, "delta-spark_2.13-4.0.1.jar")

        assert mod.main(["--from-dir", str(source)]) == 0

    def test_strips_the_ivy_filename_prefix(self, tmp_path, classpath, no_maven):
        source = tmp_path / "staged"
        _make_jar(source, "io.delta_delta-spark_2.13-4.0.1.jar")

        assert mod.main(["--from-dir", str(source)]) == 0
        assert (classpath / "delta-spark_2.13-4.0.1.jar").exists()

    def test_is_idempotent(self, tmp_path, classpath, no_maven):
        source = tmp_path / "staged"
        _make_jar(source, "delta-spark_2.13-4.0.1.jar")

        assert mod.main(["--from-dir", str(source)]) == 0
        assert mod.main(["--from-dir", str(source)]) == 0
        assert len(list(classpath.glob("*.jar"))) == 1

    def test_empty_directory_exits_non_zero(self, tmp_path, classpath, no_maven, capsys):
        source = tmp_path / "staged"
        source.mkdir()

        assert mod.main(["--from-dir", str(source)]) == 1
        assert "no JARs found" in capsys.readouterr().err

    def test_missing_directory_exits_non_zero(self, tmp_path, classpath, no_maven):
        assert mod.main(["--from-dir", str(tmp_path / "nope")]) == 1


class TestStageTo:
    def test_keeps_a_copy_and_populates_the_classpath(self, tmp_path, classpath, monkeypatch):
        resolved = tmp_path / "ivy"
        jars = [
            _make_jar(resolved, "io.delta_delta-spark_2.13-4.0.1.jar"),
            _make_jar(resolved, "io.delta_delta-storage-4.0.1.jar"),
        ]
        monkeypatch.setattr(mod, "resolve_delta_jars", lambda: jars)
        stage = tmp_path / "opt" / "delta-jars"

        assert mod.main(["--stage-to", str(stage)]) == 0
        # The kept copy is what a later --from-dir reads, so its names must
        # already be the classpath names.
        assert sorted(p.name for p in stage.glob("*.jar")) == [
            "delta-spark_2.13-4.0.1.jar",
            "delta-storage-4.0.1.jar",
        ]
        assert sorted(p.name for p in classpath.glob("*.jar")) == [
            "delta-spark_2.13-4.0.1.jar",
            "delta-storage-4.0.1.jar",
        ]

    def test_round_trips_into_from_dir(self, tmp_path, classpath, monkeypatch):
        """--stage-to then --from-dir is exactly the Dockerfile's two-step flow."""
        resolved = tmp_path / "ivy"
        jars = [_make_jar(resolved, "io.delta_delta-spark_2.13-4.0.1.jar")]
        monkeypatch.setattr(mod, "resolve_delta_jars", lambda: jars)
        stage = tmp_path / "opt" / "delta-jars"

        assert mod.main(["--stage-to", str(stage)]) == 0
        # Simulate a later `uv sync` reinstalling pyspark and discarding them.
        for jar in classpath.glob("*.jar"):
            jar.unlink()

        monkeypatch.setattr(mod, "resolve_delta_jars", lambda: pytest.fail("must not re-resolve"))
        assert mod.main(["--from-dir", str(stage)]) == 0
        assert (classpath / "delta-spark_2.13-4.0.1.jar").exists()

    def test_exits_non_zero_when_resolution_finds_nothing(self, classpath, monkeypatch, capsys):
        monkeypatch.setattr(mod, "resolve_delta_jars", list)

        assert mod.main([]) == 1
        assert "Maven Central" in capsys.readouterr().err


class TestClasspathGuard:
    def test_exits_non_zero_when_nothing_delta_lands_on_the_classpath(
        self, tmp_path, classpath, no_maven, capsys
    ):
        """The guard the Dockerfile relies on to fail the build loudly."""
        source = tmp_path / "staged"
        _make_jar(source, "something-else.jar")

        assert mod.main(["--from-dir", str(source)]) == 1
        assert "not on the Spark classpath" in capsys.readouterr().err


def test_modes_are_mutually_exclusive(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        mod.main(["--stage-to", str(tmp_path / "a"), "--from-dir", str(tmp_path / "b")])
    assert excinfo.value.code == 2
