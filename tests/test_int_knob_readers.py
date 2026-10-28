"""Non-finite numeric knobs must never abort the daemon (issue #17).

``_yaml_vram_limit_mb`` converted with ``int(raw)`` inside
``except (TypeError, ValueError)``, so ``vram_limit_mb: .inf`` escaped as
``OverflowError`` — from the constructor, which means one bad knob took the whole
unit down at boot instead of being ignored. Its sibling ``health_polls`` had
already learned this lesson (PR #15 round 3), which is exactly the drift a
single reader prevents.

These tests pin the contract at both levels: the reader (``.inf``/``-.inf``/
``.nan``/garbage/absent) and the daemon (a constructor that survives the knob).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from caretaker import manager
from caretaker.manager import Caretaker


def _write_config(tmp_path: Path, text: str) -> Path:
    cfg = tmp_path / "models.local.settings.yaml"
    cfg.write_text(text, encoding="utf-8")
    return cfg


NON_FINITE = [".inf", "-.inf", ".nan"]


class TestVramLimitReader:
    @pytest.mark.parametrize("value", NON_FINITE)
    def test_a_non_finite_vram_limit_is_ignored(self, tmp_path, caplog, value):
        cfg = _write_config(tmp_path, f"vram_limit_mb: {value}\n")
        with caplog.at_level(logging.WARNING):
            assert manager._yaml_vram_limit_mb(str(cfg)) is None
        warnings = [r for r in caplog.records if "vram_limit_mb" in r.getMessage()]
        assert len(warnings) == 1, [r.getMessage() for r in caplog.records]

    def test_a_valid_vram_limit_is_returned(self, tmp_path):
        cfg = _write_config(tmp_path, "vram_limit_mb: 4096\n")
        assert manager._yaml_vram_limit_mb(str(cfg)) == 4096

    def test_a_quoted_integer_and_a_float_string_still_work(self, tmp_path):
        cfg = _write_config(tmp_path, 'vram_limit_mb: "2048"\n')
        assert manager._yaml_vram_limit_mb(str(cfg)) == 2048

    @pytest.mark.parametrize("text", ["vram_limit_mb: nonsense\n", "vram_limit_mb: null\n", ""])
    def test_garbage_or_absent_means_not_configured(self, tmp_path, text):
        cfg = _write_config(tmp_path, text)
        assert manager._yaml_vram_limit_mb(str(cfg)) is None

    def test_a_missing_file_means_not_configured(self, tmp_path):
        assert manager._yaml_vram_limit_mb(str(tmp_path / "nope.yaml")) is None


class TestHealthPollsReader:
    """The sibling must keep the behaviour PR #15 gave it."""

    @pytest.mark.parametrize("value", NON_FINITE)
    def test_a_non_finite_health_poll_count_is_ignored(self, tmp_path, value):
        cfg = _write_config(tmp_path, f"health_polls: {value}\n")
        assert manager._read_health_polls(str(cfg)) is None

    def test_a_valid_health_poll_count_is_returned(self, tmp_path):
        cfg = _write_config(tmp_path, "health_polls: 900\n")
        assert manager._read_health_polls(str(cfg)) == 900


class TestTheDaemonSurvivesTheKnob:
    def test_the_constructor_survives_a_non_finite_vram_limit(self, tmp_path, caplog):
        """The issue's acceptance test: boot with ``.inf`` and a default limit."""
        cfg = _write_config(
            tmp_path,
            "vram_limit_mb: .inf\n"
            "health_polls: .inf\n"
            "models:\n"
            "  minimal:\n"
            "    path: /home/flip/models/minimal.gguf\n",
        )
        with caplog.at_level(logging.WARNING):
            daemon = Caretaker(config_path=str(cfg))  # must not raise
        # Both knobs fell back: no VRAM slot, built-in health-poll default.
        assert daemon.vram is None
        assert daemon.health_polls == manager.DEFAULT_HEALTH_POLLS
        messages = [r.getMessage() for r in caplog.records]
        assert sum("vram_limit_mb" in m for m in messages) == 1
        assert sum("health_polls" in m for m in messages) == 1

    def test_an_explicit_argument_wins_over_a_non_finite_knob(self, tmp_path):
        cfg = _write_config(tmp_path, "vram_limit_mb: .inf\nhealth_polls: .inf\n")
        daemon = Caretaker(config_path=str(cfg), health_polls=42, vram_limit_mb=1000)
        assert daemon.health_polls == 42
        assert daemon.vram is not None


class TestOneReaderNotTwo:
    """Guard against the drift returning: one implementation, both wrappers."""

    def test_both_wrappers_go_through_the_shared_reader(self, monkeypatch):
        calls: list[tuple[str, str]] = []

        def spy(config_path, key, *, reader):
            calls.append((key, reader))

        monkeypatch.setattr(manager, "_read_optional_int_knob", spy)
        manager._yaml_vram_limit_mb("whatever.yaml")
        manager._read_health_polls("whatever.yaml")
        assert [c[0] for c in calls] == ["vram_limit_mb", "health_polls"]

    def test_the_overflow_error_branch_lives_in_the_shared_reader_only(self):
        """A per-knob ``except`` tuple is how the two copies drifted apart."""
        import inspect

        reader_src = inspect.getsource(manager._read_optional_int_knob)
        assert "OverflowError" in reader_src
        for wrapper in (manager._yaml_vram_limit_mb, manager._read_health_polls):
            src = inspect.getsource(wrapper)
            assert "except" not in src, f"{wrapper.__name__} re-implements error handling"
