"""Comfy lifecycle pins — idle-stop + wake proxy (the operator's VRAM spec:
GPU services stop after 5 min of non-use and the caretaker restarts them on
the next request)."""

import asyncio
import time

import pytest

from caretaker import comfy as comfy_mod


@pytest.fixture(autouse=True)
def _fresh_state():
    comfy_mod._last_activity_monotonic = None
    comfy_mod._proxy_server = None
    comfy_mod._enabled = False
    yield
    if comfy_mod._proxy_server is not None:
        comfy_mod._proxy_server.close()
        comfy_mod._proxy_server = None


def _env(monkeypatch, **overrides):
    base = {
        "CARETAKER_COMFY_IDLE_SECONDS": "300",
        "CARETAKER_COMFY_URL": "http://127.0.0.1:8189",
        "CARETAKER_COMFY_START_COMMAND": "echo start",
        "CARETAKER_COMFY_STOP_COMMAND": "echo stop",
        "CARETAKER_COMFY_PROXY_PORT": "0",
    }
    base.update(overrides)
    for key, value in base.items():
        monkeypatch.setenv(key, value)


# --- watcher tick ---


@pytest.mark.asyncio
async def test_idle_tick_stops_comfy_after_budget(monkeypatch):
    _env(monkeypatch, CARETAKER_COMFY_IDLE_SECONDS="5")
    async def snap():
        return {"queue_running": [], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)
    stops = []
    monkeypatch.setattr(comfy_mod, "stop_comfy", lambda: stops.append(1) or asyncio.sleep(0))
    comfy_mod._last_activity_monotonic = time.monotonic() - 10  # idle past the budget
    assert await comfy_mod._idle_watcher_tick() == "stopped"
    assert stops == [1]


@pytest.mark.asyncio
async def test_busy_queue_resets_clock(monkeypatch):
    _env(monkeypatch, CARETAKER_COMFY_IDLE_SECONDS="5")
    async def snap():
        return {"queue_running": [1], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)
    stops = []
    monkeypatch.setattr(comfy_mod, "stop_comfy", lambda: stops.append(1) or asyncio.sleep(0))
    comfy_mod._last_activity_monotonic = time.monotonic() - 999  # way past
    assert await comfy_mod._idle_watcher_tick() == "busy"
    assert stops == []  # a running job is never stopped
    assert time.monotonic() - comfy_mod._last_activity_monotonic < 5


@pytest.mark.asyncio
async def test_unreachable_comfy_parks_clock(monkeypatch):
    _env(monkeypatch)
    async def snap():
        return None

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)
    assert await comfy_mod._idle_watcher_tick() == "down"
    assert comfy_mod._last_activity_monotonic is None


@pytest.mark.asyncio
async def test_zero_idle_budget_disables_watch(monkeypatch):
    _env(monkeypatch, CARETAKER_COMFY_IDLE_SECONDS="0")
    assert await comfy_mod._idle_watcher_tick() == "off"


# --- wake proxy ---


@pytest.mark.asyncio
async def test_proxy_pumps_when_comfy_already_up(monkeypatch):
    _env(monkeypatch)
    wake_calls = []

    async def snap():
        return {"queue_running": [], "queue_pending": []}  # comfy already up

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)

    async def fake_start():
        wake_calls.append(1)
        return True

    monkeypatch.setattr(comfy_mod, "start_comfy", fake_start)

    async def echo(reader, writer):
        data = await reader.read(100)
        writer.write(data.upper())
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    port_up = upstream.sockets[0].getsockname()[1]
    # comfy_url() prefers CARETAKER_COMFY_URL — point it at the fake upstream
    monkeypatch.setenv("CARETAKER_COMFY_URL", f"http://127.0.0.1:{port_up}")
    monkeypatch.setenv("CARETAKER_COMFY_PROXY_PORT", "18123")
    await comfy_mod.start_proxy()

    reader, writer = await asyncio.open_connection("127.0.0.1", 18123)
    writer.write(b"ping")
    await writer.drain()
    assert await reader.read(100) == b"PING"
    writer.close()
    upstream.close()
    assert wake_calls == []  # comfy already up -> no wake needed


@pytest.mark.asyncio
async def test_proxy_wake_failure_closes_connection(monkeypatch):
    _env(monkeypatch)

    async def snap():
        return None  # comfy down

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)

    async def fake_start():
        return False  # the start command failed

    monkeypatch.setattr(comfy_mod, "start_comfy", fake_start)
    monkeypatch.setenv("CARETAKER_COMFY_PROXY_PORT", "18124")
    monkeypatch.setenv("CARETAKER_COMFY_URL", "http://127.0.0.1:8189")
    await comfy_mod.start_proxy()

    reader, writer = await asyncio.open_connection("127.0.0.1", 18124)
    writer.write(b"GET / HTTP/1.0\r\n\r\n")
    await writer.drain()
    # Contract: a failed wake drops the connection WITHOUT response data —
    # the client sees EOF or a reset, never a fake success.
    with pytest.raises((ConnectionResetError, ConnectionAbortedError)):
        await reader.read(100)
    assert reader.at_eof() or reader.exception() is not None


# --- status surface ---


def test_status_reports_shape(monkeypatch):
    _env(monkeypatch, CARETAKER_COMFY_PROXY_PORT="18125")
    status = comfy_mod.status()
    assert status["idle_seconds"] == 300
    assert status["proxy_port"] == 18125
    assert status["up"] is None or isinstance(status["up"], bool)
    assert "start_command" in status and "stop_command" in status
