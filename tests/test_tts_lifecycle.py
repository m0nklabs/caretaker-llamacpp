"""On-demand TTS engine lifecycle (see caretaker/tts.py).

Pins the contract the gateway relies on: ensure is idempotent (health-first),
cold start goes through the SCM service control, release stops the service,
and the whole module is inert off-Windows (the same code ships with the Linux
caretaker).
"""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from caretaker import tts as tts_mod


@pytest.fixture(autouse=True)
def _win32(monkeypatch):
    """Tests exercise the Windows path; restore the real platform after."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(tts_mod, "_last_use_monotonic", None)
    monkeypatch.setattr(tts_mod, "_watcher_task", None)
    yield
    monkeypatch.setattr(sys, "platform", sys.platform)


def _patch_health(monkeypatch, states):
    """states: list of health results consumed in order (True = healthy)."""
    it = iter(states)

    async def _healthy(timeout_s=3.0):
        try:
            return next(it)
        except StopIteration:
            return states[-1]

    monkeypatch.setattr(tts_mod, "_engine_healthy", _healthy)


def _patch_sc(monkeypatch, rc=0, out=""):
    calls = []

    async def _control(action):
        calls.append(action)
        return rc, out

    monkeypatch.setattr(tts_mod, "_service_control", _control)
    return calls


async def test_off_windows_is_inert(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    ensure = await tts_mod.ensure_tts()
    release = await tts_mod.release_tts()
    assert ensure["ok"] is False and "disabled" in ensure["reason"]
    assert release["ok"] is False and "disabled" in release["reason"]


async def test_ensure_already_running_refreshes_timer(monkeypatch):
    _patch_health(monkeypatch, [True])
    calls = _patch_sc(monkeypatch)
    result = await tts_mod.ensure_tts()
    assert result == {"ok": True, "already_running": True}
    assert calls == []  # no service control on the healthy fast-path
    assert tts_mod._last_use_monotonic is not None


async def test_ensure_cold_start_starts_service_and_waits_health(monkeypatch):
    _patch_health(monkeypatch, [False, False, True])  # down, starting, healthy
    calls = _patch_sc(monkeypatch, rc=0)
    result = await tts_mod.ensure_tts()
    assert result["ok"] is True
    assert result["cold_start"] is True
    assert calls == ["start"]


async def test_ensure_start_failure_reports_ok_false(monkeypatch):
    _patch_health(monkeypatch, [False])
    _patch_sc(monkeypatch, rc=1060, out="service does not exist")
    result = await tts_mod.ensure_tts()
    assert result["ok"] is False
    assert "service start failed" in result["reason"]


async def test_ensure_start_timeout_reports_ok_false(monkeypatch):
    _patch_health(monkeypatch, [False])  # never becomes healthy
    monkeypatch.setattr(tts_mod, "_env", lambda name, default: "0" if name == "CARETAKER_TTS_START_TIMEOUT" else default)
    _patch_sc(monkeypatch, rc=0)
    result = await tts_mod.ensure_tts()
    assert result["ok"] is False
    assert "not healthy" in result["reason"]


async def test_release_stops_healthy_engine(monkeypatch):
    _patch_health(monkeypatch, [True, False])  # healthy now, gone after stop
    calls = _patch_sc(monkeypatch, rc=0)
    result = await tts_mod.release_tts()
    assert result == {"ok": True, "stopped": True}
    assert calls == ["stop"]


async def test_release_already_stopped_is_idempotent(monkeypatch):
    _patch_health(monkeypatch, [False])
    calls = _patch_sc(monkeypatch)
    result = await tts_mod.release_tts()
    assert result == {"ok": True, "already_stopped": True}
    assert calls == []


async def test_status_shape(monkeypatch):
    monkeypatch.setattr(
        tts_mod, "_env",
        lambda name, default: {
            "CARETAKER_TTS_SERVICE": "qwen3tts-http",
            "CARETAKER_TTS_URL": "http://127.0.0.1:11450",
            "CARETAKER_TTS_IDLE_SECONDS": "600",
        }.get(name, default),
    )
    status = tts_mod.tts_status()
    assert status["enabled"] is True
    assert status["service"] == "qwen3tts-http"
    assert status["idle_seconds"] == 600
    assert status["last_use_epoch"] is None


async def test_watcher_releases_after_idle(monkeypatch):
    """The idle watcher must call release_tts when the idle budget elapses."""
    released = []

    async def fake_release():
        released.append(True)
        return {"ok": True}

    monkeypatch.setattr(tts_mod, "tts_enabled", lambda: True)
    monkeypatch.setattr(tts_mod, "release_tts", fake_release)
    monkeypatch.setattr(tts_mod, "_engine_healthy", AsyncMock(return_value=True))
    import time as _time

    monkeypatch.setattr(tts_mod, "_last_use_monotonic", _time.monotonic() - 9999)
    # run exactly one watcher iteration (the loop sleeps 30s between passes —
    # break out after the first body by cancelling the task we start manually)
    task = asyncio_create_single_iteration(monkeypatch)
    try:
        await task
    except asyncio.CancelledError:
        pass  # the patched sleep cancels after exactly one watcher body
    assert released == [True]


def asyncio_create_single_iteration(monkeypatch):
    """Run one watcher body by patching the inter-iteration sleep to block."""

    async def _sleep(seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(tts_mod.asyncio, "sleep", _sleep)
    return tts_mod.asyncio.create_task(tts_mod._idle_watcher_loop())
