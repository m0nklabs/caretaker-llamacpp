"""On-demand TTS engine lifecycle (caretaker/tts.py) — platform-agnostic.

The caretaker spawns/stops the engine process from CARETAKER_TTS_COMMAND on
every host (Linux, Windows, cloud GPU boxes) — no service-manager coupling,
no platform gate (operator principle: providers behave identically everywhere).
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from caretaker import tts as tts_mod


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(tts_mod, "_last_use_monotonic", None)
    monkeypatch.setattr(tts_mod, "_watcher_task", None)
    monkeypatch.setattr(tts_mod, "_process", None)
    yield


def _patch_env(monkeypatch, **values):
    base = {
        "CARETAKER_TTS_ENABLED": "1",
        "CARETAKER_TTS_URL": "http://127.0.0.1:11450",
        "CARETAKER_TTS_COMMAND": ".venv/bin/python tts_http_wrapper.py",
        "CARETAKER_TTS_CWD": "/home/flip/Qwen3-TTS-GGUF",
        "CARETAKER_TTS_IDLE_SECONDS": "600",
        "CARETAKER_TTS_START_TIMEOUT": "90",
    }
    base.update(values)
    monkeypatch.setattr(tts_mod, "_env", lambda name, default: base.get(name, default))
    monkeypatch.setattr(tts_mod, "_gpu_free_mb", AsyncMock(return_value=8000))


class _FakeProcess:
    def __init__(self, pid=4242, exit_during_startup=False):
        self.pid = pid
        self.returncode = None
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


def _patch_health(monkeypatch, states):
    it = iter(states)

    async def _healthy(timeout_s=3.0):
        try:
            return next(it)
        except StopIteration:
            return states[-1]

    monkeypatch.setattr(tts_mod, "_engine_healthy", _healthy)


def _patch_spawn(monkeypatch, process):
    spawned = []

    async def _spawn(*cmd, cwd=None, stdout=None, stderr=None, env=None):
        spawned.append({"cmd": cmd, "cwd": cwd, "env": env})
        return process

    monkeypatch.setattr(tts_mod.asyncio, "create_subprocess_exec", _spawn)
    return spawned


async def test_disabled_reports_reason(monkeypatch):
    _patch_env(monkeypatch, CARETAKER_TTS_ENABLED="0")
    ensure = await tts_mod.ensure_tts()
    release = await tts_mod.release_tts()
    assert ensure["ok"] is False and "ENABLED" in ensure["reason"]
    assert release["ok"] is False and "ENABLED" in release["reason"]


async def test_ensure_already_running_refreshes_timer(monkeypatch):
    _patch_env(monkeypatch)
    _patch_health(monkeypatch, [True])
    spawned = _patch_spawn(monkeypatch, _FakeProcess())
    result = await tts_mod.ensure_tts()
    assert result == {"ok": True, "already_running": True}
    assert spawned == []  # healthy fast-path: no spawn
    assert tts_mod._last_use_monotonic is not None


async def test_ensure_cold_start_spawns_command_and_waits_health(monkeypatch):
    _patch_env(monkeypatch)
    _patch_health(monkeypatch, [False, False, True])  # down, starting, healthy
    spawned = _patch_spawn(monkeypatch, _FakeProcess(pid=777))
    result = await tts_mod.ensure_tts()
    assert result["ok"] is True
    assert result["cold_start"] is True
    assert result["pid"] == 777
    assert spawned[0]["cmd"][0] == ".venv/bin/python"
    assert spawned[0]["cwd"] == "/home/flip/Qwen3-TTS-GGUF"
    assert tts_mod._last_use_monotonic is not None


async def test_ensure_without_command_reports_not_configured(monkeypatch):
    _patch_env(monkeypatch, CARETAKER_TTS_COMMAND="")
    _patch_health(monkeypatch, [False])
    result = await tts_mod.ensure_tts()
    assert result["ok"] is False
    assert "not configured" in result["reason"]


async def test_ensure_engine_exit_during_startup_reports_failure(monkeypatch):
    _patch_env(monkeypatch)
    _patch_health(monkeypatch, [False])
    proc = _FakeProcess()
    _patch_spawn(monkeypatch, proc)

    async def _flip_later():
        await asyncio.sleep(0.2)
        proc.returncode = 1  # the engine dies right after spawn

    flip_task = asyncio.create_task(_flip_later())
    result = await tts_mod.ensure_tts()
    await flip_task
    assert result["ok"] is False
    assert "exited during startup" in result["reason"]


async def test_ensure_start_timeout_reports_failure(monkeypatch):
    _patch_env(monkeypatch, CARETAKER_TTS_START_TIMEOUT="0")
    _patch_health(monkeypatch, [False])
    _patch_spawn(monkeypatch, _FakeProcess())
    result = await tts_mod.ensure_tts()
    assert result["ok"] is False
    assert "not healthy" in result["reason"]


async def test_release_stops_own_child(monkeypatch):
    _patch_env(monkeypatch)
    _patch_health(monkeypatch, [True, False])  # healthy now, gone after stop
    proc = _FakeProcess()
    monkeypatch.setattr(tts_mod, "_process", proc)
    result = await tts_mod.release_tts()
    assert result == {"ok": True, "stopped": True}
    assert proc.terminated is True


async def test_release_adopted_engine_cannot_stop(monkeypatch):
    """A healthy engine without a caretaker child handle (started manually or
    adopted after a caretaker restart) is honestly reported, not lied about."""
    _patch_env(monkeypatch)
    _patch_health(monkeypatch, [True])
    monkeypatch.setattr(tts_mod, "_process", None)
    result = await tts_mod.release_tts()
    assert result["ok"] is False
    assert "not spawned by this caretaker" in result["reason"]


async def test_release_already_stopped_is_idempotent(monkeypatch):
    _patch_env(monkeypatch)
    _patch_health(monkeypatch, [False])
    result = await tts_mod.release_tts()
    assert result == {"ok": True, "already_stopped": True}


async def test_status_shape(monkeypatch):
    _patch_env(monkeypatch)
    status = tts_mod.tts_status()
    assert status["enabled"] is True
    assert status["command_configured"] is True
    assert status["idle_seconds"] == 600
    assert status["child_pid"] is None
    assert status["last_use_epoch"] is None


async def test_watcher_releases_after_idle(monkeypatch):
    released = []

    async def fake_release():
        released.append(True)
        return {"ok": True}

    _patch_env(monkeypatch)
    monkeypatch.setattr(tts_mod, "release_tts", fake_release)
    monkeypatch.setattr(tts_mod, "_engine_healthy", AsyncMock(return_value=True))
    import time as _time

    monkeypatch.setattr(tts_mod, "_last_use_monotonic", _time.monotonic() - 9999)

    async def _sleep(seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(tts_mod.asyncio, "sleep", _sleep)
    task = tts_mod.asyncio.create_task(tts_mod._idle_watcher_loop())
    try:
        await task
    except asyncio.CancelledError:
        pass  # the patched sleep cancels after exactly one watcher body
    assert released == [True]


async def test_shlex_splits_windows_command_with_backslashes(monkeypatch):
    """On Windows the command keeps backslashes (posix=False shlex split)."""
    monkeypatch.setattr(tts_mod.os, "name", "nt")
    monkeypatch.setattr(
        tts_mod, "_env",
        lambda name, default: "J:\\Qwen3-TTS-GGUF\\.venv\\Scripts\\python.exe tts_http_wrapper.py"
        if name == "CARETAKER_TTS_COMMAND" else default,
    )
    parts = tts_mod._spawn_command()
    assert parts == ["J:\\Qwen3-TTS-GGUF\\.venv\\Scripts\\python.exe", "tts_http_wrapper.py"]


async def test_vram_gate_spawns_when_enough_free(monkeypatch):
    _patch_env(monkeypatch, CARETAKER_TTS_MIN_FREE_MB="2500")
    monkeypatch.setattr(tts_mod, "_gpu_free_mb", AsyncMock(return_value=8000))
    _patch_health(monkeypatch, [False, False, True])
    _patch_spawn(monkeypatch, _FakeProcess())
    result = await tts_mod.ensure_tts()
    assert result["ok"] is True and result["cold_start"] is True


async def test_tts_first_unloads_then_spawns(monkeypatch):
    """STOP_LLAMA=1 unloads the caretaker's own llama BEFORE the VRAM check
    (deterministic TTS-first); with the GPU freed the check passes and the
    engine spawns."""
    _patch_env(monkeypatch, CARETAKER_TTS_MIN_FREE_MB="2500", CARETAKER_TTS_STOP_LLAMA="1")
    monkeypatch.setattr(tts_mod, "_gpu_free_mb", AsyncMock(return_value=9500))
    _patch_health(monkeypatch, [False, False, True])
    _patch_spawn(monkeypatch, _FakeProcess())
    unloaded = []

    class _Mgr:
        async def unload(self):
            unloaded.append(True)

    tts_mod.init(lambda: _Mgr())
    result = await tts_mod.ensure_tts()
    assert result["ok"] is True
    assert unloaded == [1]


async def test_vram_gate_fails_honestly_when_llama_stop_disabled(monkeypatch):
    _patch_env(monkeypatch, CARETAKER_TTS_MIN_FREE_MB="2500", CARETAKER_TTS_STOP_LLAMA="0")
    monkeypatch.setattr(tts_mod, "_gpu_free_mb", AsyncMock(return_value=800))
    _patch_health(monkeypatch, [False])
    result = await tts_mod.ensure_tts()
    assert result["ok"] is False
    assert "insufficient VRAM" in result["reason"]


async def test_spawn_forces_utf8_io_env(monkeypatch):
    """Engine prints carry emoji; a Windows file redirect defaults to cp1252
    and a UnicodeEncodeError there kills the model load mid-flight.  The
    caretaker therefore always spawns the engine with UTF-8 IO env vars —
    a no-op on Linux, life-saving on Windows, uniform everywhere."""
    _patch_env(monkeypatch)
    _patch_health(monkeypatch, [False, False, True])
    spawned = _patch_spawn(monkeypatch, _FakeProcess())
    result = await tts_mod.ensure_tts()
    assert result["ok"] is True
    env = spawned[0]["env"]
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONUTF8"] == "1"


async def test_vram_wait_frees_up_and_spawns(monkeypatch):
    """Tight VRAM + WAIT>0: poll until the memory frees, then spawn."""
    _patch_env(monkeypatch, CARETAKER_TTS_VRAM_WAIT_SECONDS="10", CARETAKER_TTS_VRAM_POLL_SECONDS="2")
    free_states = iter([800, 800, 9000])

    async def _free():
        return next(free_states)

    monkeypatch.setattr(tts_mod, "_gpu_free_mb", _free)
    _patch_health(monkeypatch, [False, False, False, False, True])
    _patch_spawn(monkeypatch, _FakeProcess())
    result = await tts_mod.ensure_tts()
    assert result["ok"] is True and result["cold_start"] is True


async def test_vram_wait_times_out_and_gives_up(monkeypatch):
    _patch_env(monkeypatch, CARETAKER_TTS_VRAM_WAIT_SECONDS="4", CARETAKER_TTS_VRAM_POLL_SECONDS="2")
    monkeypatch.setattr(tts_mod, "_gpu_free_mb", AsyncMock(return_value=800))
    _patch_health(monkeypatch, [False])
    result = await tts_mod.ensure_tts()
    assert result["ok"] is False
    assert "after 4s wait" in result["reason"]


async def test_concurrent_ensures_spawn_once(monkeypatch):
    """A second request during a cold start waits on the ensure lock and takes
    the healthy fast-path — never a second engine on the same GPU."""
    _patch_env(monkeypatch)
    healthy = {"flag": False}

    async def _healthy(timeout_s=3.0):
        return healthy["flag"]

    monkeypatch.setattr(tts_mod, "_engine_healthy", _healthy)

    async def _spawn(*cmd, cwd=None, stdout=None, stderr=None, env=None):
        await asyncio.sleep(0.3)  # cold start in flight
        healthy["flag"] = True
        return _FakeProcess()

    monkeypatch.setattr(tts_mod.asyncio, "create_subprocess_exec", _spawn)
    one, two = await asyncio.gather(tts_mod.ensure_tts(), tts_mod.ensure_tts())
    assert one["ok"] is True and two["ok"] is True
    assert one.get("cold_start") is True and two.get("already_running") is True


async def test_tts_first_host_yields_llama_even_with_vram_plenty(monkeypatch):
    """STOP_LLAMA=1 means deterministic TTS-first: the caretaker's llama-server
    unloads whenever the engine must start — not only when VRAM happens to be
    tight.  A borderline 2.3GB-free host must never squeeze the TTS next to a
    9GB llama (fragile DML init)."""
    _patch_env(monkeypatch, CARETAKER_TTS_STOP_LLAMA="1")
    monkeypatch.setattr(tts_mod, "_gpu_free_mb", AsyncMock(return_value=9000))
    _patch_health(monkeypatch, [False, False, True])
    _patch_spawn(monkeypatch, _FakeProcess())
    unloaded = []

    class _Mgr:
        async def unload(self):
            unloaded.append(True)

    tts_mod.init(lambda: _Mgr())
    result = await tts_mod.ensure_tts()
    assert result["ok"] is True, result
    assert unloaded == [1]
