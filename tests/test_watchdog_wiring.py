"""Pins for the watchdog startup wiring (``server._lifespan``).

The watchdog handoff (docs/HANDOFF.md 2026-09-16) requires the crash watchdog
to actually run in production: these pins lock the lifespan contract — arm on
startup with env-configurable timing, stop on shutdown, fail open on a broken
manager, and skip (never crash on) a manager without the watchdog surface.
"""

from __future__ import annotations

import pytest
from caretaker import server as server_mod
from caretaker.config import ModelsConfigError
from caretaker.manager import (
    DEFAULT_WATCHDOG_INITIAL_BACKOFF,
    DEFAULT_WATCHDOG_INTERVAL,
    DEFAULT_WATCHDOG_MAX_BACKOFF,
)
from fastapi.testclient import TestClient

_WATCHDOG_ENV_VARS = (
    "CARETAKER_WATCHDOG_ENABLED",
    "CARETAKER_WATCHDOG_INTERVAL",
    "CARETAKER_WATCHDOG_INITIAL_BACKOFF",
    "CARETAKER_WATCHDOG_MAX_BACKOFF",
)


class RecordingManager:
    """Minimal manager double that records watchdog arming (no backend)."""

    def __init__(self) -> None:
        self.started: dict[str, float] | None = None
        self.stopped = 0

    def start_watchdog(self, **kwargs: float) -> None:
        self.started = kwargs

    def stop_watchdog(self) -> None:
        self.stopped += 1


@pytest.fixture()
def fake_manager(monkeypatch: pytest.MonkeyPatch) -> RecordingManager:
    """Inject a recording manager; reset the singleton and env afterwards."""
    for var in _WATCHDOG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    mgr = RecordingManager()
    server_mod.init(mgr)
    yield mgr
    server_mod.init(None)


def _run_lifespan() -> None:
    """Enter and exit the app lifespan (startup + shutdown both run)."""
    with TestClient(server_mod.app):
        pass


def test_lifespan_arms_watchdog_with_defaults_and_stops_on_shutdown(
    fake_manager: RecordingManager,
) -> None:
    _run_lifespan()
    assert fake_manager.started == {
        "interval": DEFAULT_WATCHDOG_INTERVAL,
        "initial_backoff": DEFAULT_WATCHDOG_INITIAL_BACKOFF,
        "max_backoff": DEFAULT_WATCHDOG_MAX_BACKOFF,
    }
    assert fake_manager.stopped == 1


def test_lifespan_skips_watchdog_when_disabled(
    fake_manager: RecordingManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CARETAKER_WATCHDOG_ENABLED", "0")
    _run_lifespan()
    assert fake_manager.started is None
    # Never armed → never stopped either (no spurious stop_watchdog call).
    assert fake_manager.stopped == 0


def test_lifespan_passes_env_overrides_through(
    fake_manager: RecordingManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CARETAKER_WATCHDOG_INTERVAL", "5.5")
    monkeypatch.setenv("CARETAKER_WATCHDOG_INITIAL_BACKOFF", "1.0")
    monkeypatch.setenv("CARETAKER_WATCHDOG_MAX_BACKOFF", "9.0")
    _run_lifespan()
    assert fake_manager.started == {
        "interval": 5.5,
        "initial_backoff": 1.0,
        "max_backoff": 9.0,
    }


def test_lifespan_falls_back_to_default_on_invalid_value(
    fake_manager: RecordingManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CARETAKER_WATCHDOG_INTERVAL", "banana")
    _run_lifespan()
    assert fake_manager.started is not None
    assert fake_manager.started["interval"] == DEFAULT_WATCHDOG_INTERVAL


def test_lifespan_survives_manager_build_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-open: a broken models config must not block the API boot."""

    def _boom(*args: object, **kwargs: object) -> None:
        raise ModelsConfigError("no models config")

    monkeypatch.setattr(server_mod, "Caretaker", _boom)
    server_mod.init(None)
    _run_lifespan()  # must not raise
    server_mod.init(None)


def test_lifespan_skips_arming_for_manager_without_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manager double without the watchdog surface is skipped, not crashed."""
    server_mod.init(object())  # type: ignore[arg-type]
    _run_lifespan()  # must not raise
    server_mod.init(None)


def test_lifespan_survives_raising_start_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-open covers arming itself: a raising start_watchdog must not
    block the API boot (review finding on PR #12)."""

    class ExplodingManager(RecordingManager):
        def start_watchdog(self, **kwargs: float) -> None:
            raise RuntimeError("cannot schedule watchdog")

    mgr = ExplodingManager()
    server_mod.init(mgr)
    _run_lifespan()  # must not raise
    # Never armed → never stopped either.
    assert mgr.started is None
    assert mgr.stopped == 0
    server_mod.init(None)
