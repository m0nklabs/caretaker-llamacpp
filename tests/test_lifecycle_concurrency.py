"""Deterministic lifecycle races; all backend, GPU and file effects are in memory."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from caretaker import manager as manager_mod
from caretaker.manager import Caretaker, ModelLoadError


class MemoryProcess:
    """Track the backend's launched path independently of manager bookkeeping."""

    def __init__(self):
        self.events = []
        self.alive = False
        self.path = None
        self.next_path = None

    async def start(self):
        self.events.append(("start", self.next_path))
        self.path = self.next_path
        self.alive = True

    async def stop(self):
        self.events.append(("stop", self.path))
        self.alive = False

    async def health_ok(self, url):
        return self.alive

    async def restart_count(self):
        return 0

    async def is_failed(self):
        return False

    async def crash_error(self):
        return "mock crash"

    async def service_exit_code(self):
        return 1


@pytest.fixture
def rig(monkeypatch):
    """Keep real lifecycle/VRAM/crash/health/verification, forbid external I/O."""
    forbidden = Mock(side_effect=AssertionError("external I/O is forbidden"))
    monkeypatch.setattr(manager_mod.httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(manager_mod.asyncio, "create_subprocess_exec", forbidden)
    models = {
        name: {"path": f"/mock/{name}.gguf", "context": 4096, "ngl": 1, "size_mb": 300}
        for name in ("a", "b")
    }
    monkeypatch.setattr(
        manager_mod.config_mod, "load_models_config", lambda path: {"models": models}
    )
    process = MemoryProcess()
    manager = Caretaker(
        config_path="/mock/config.yaml", server_process=process,
        vram_limit_mb=500, health_polls=1,
    )
    monkeypatch.setattr(manager, "_config_drifted", Mock(return_value=False))
    monkeypatch.setattr(manager, "_compute_launch_signature", Mock(return_value=None))
    monkeypatch.setattr(manager, "_write_persisted_signature", forbidden)
    monkeypatch.setattr(
        manager, "_write_server_args",
        lambda config: setattr(process, "next_path", config["path"]),
    )
    for method in ("_save_context", "_load_context", "_free_gpu_memory"):
        monkeypatch.setattr(manager, method, AsyncMock())

    async def props():
        return {"model_path": process.path} if process.alive else None

    monkeypatch.setattr(manager, "_fetch_props", props)
    return SimpleNamespace(manager=manager, process=process)


class Gate:
    """Pause a specific awaited operation without wall-clock sleeps."""

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def wait(self, *args):
        self.entered.set()
        await self.release.wait()


async def wait_event(event):
    await asyncio.wait_for(event.wait(), timeout=2)


async def settle(task):
    return await asyncio.wait_for(task, timeout=2)


async def cleanup(*tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def foreground(manager, action):
    if action == "unload":
        await manager.unload()
    elif action == "stop":
        await manager.stop()
    elif action == "switch":
        assert await manager.switch_model("b") is True
    elif action == "reload":
        assert await manager.switch_model("a", force=True) is True
    else:
        assert await manager.switch_model("a") is False


def assert_state(rig, action):
    manager, process = rig.manager, rig.process
    if action in ("unload", "stop"):
        assert manager.current_model is None
        assert manager.is_unloaded
        assert not process.alive
        assert not manager.vram.active_counts
    else:
        model = "b" if action == "switch" else "a"
        assert manager.current_model == model
        assert not manager.is_unloaded
        assert process.alive
        assert process.path == f"/mock/{model}.gguf"
        assert dict(manager.vram.active_counts) == {model: 1}
    assert not manager._switch_in_progress


@pytest.mark.parametrize("action", ["unload", "stop", "switch", "reload"])
async def test_watchdog_discards_restart_after_foreground_during_backoff(rig, monkeypatch, action):
    manager, process = rig.manager, rig.process
    await manager.switch_model("a")
    process.alive = False
    gate = Gate()
    monkeypatch.setattr(manager_mod.asyncio, "sleep", gate.wait)
    tick = asyncio.create_task(manager._watchdog_tick())
    try:
        await wait_event(gate.entered)
        # Foreground completion must not wait for the watchdog backoff.
        await asyncio.wait_for(foreground(manager, action), timeout=2)
        events = list(process.events)
        gate.release.set()
        assert await settle(tick) is False
        assert process.events == events
        assert manager._watchdog_retry_model is None
        assert_state(rig, action)
    finally:
        await cleanup(tick)


@pytest.mark.parametrize("action", ["unload", "stop", "switch", "reload"])
async def test_watchdog_discards_stale_probe_before_crash_stop(rig, monkeypatch, action):
    manager, process = rig.manager, rig.process
    await manager.switch_model("a")
    gate = Gate()
    original_health = process.health_ok

    async def delayed_probe(url):
        # Only the watchdog's first observation is delayed/stale.
        monkeypatch.setattr(process, "health_ok", original_health)
        await gate.wait()
        return False

    monkeypatch.setattr(process, "health_ok", delayed_probe)
    manager._watchdog_backoff = 0
    tick = asyncio.create_task(manager._watchdog_tick())
    try:
        await wait_event(gate.entered)
        await foreground(manager, action)
        events = list(process.events)
        gate.release.set()
        assert await settle(tick) is False
        assert process.events == events
        assert not manager.crash_history, "stale probe must not record/stop a new lifecycle"
        assert_state(rig, action)
    finally:
        await cleanup(tick)


@pytest.mark.parametrize("action", ["unload", "stop", "switch", "noop"])
async def test_foreground_operations_wait_for_inflight_switch(rig, monkeypatch, action):
    manager, process = rig.manager, rig.process
    gate = Gate()
    original_start = process.start

    async def delayed_start():
        monkeypatch.setattr(process, "start", original_start)
        await gate.wait()
        await original_start()

    monkeypatch.setattr(process, "start", delayed_start)
    loading = asyncio.create_task(manager.switch_model("a"))
    competing = None
    try:
        await wait_event(gate.entered)
        events = list(process.events)
        attempted = asyncio.Event()

        async def compete():
            attempted.set()
            await foreground(manager, action)

        competing = asyncio.create_task(compete())
        await wait_event(attempted)
        assert not competing.done(), "foreground mutations must serialize"
        assert process.events == events
        gate.release.set()
        assert await settle(loading) is True
        await settle(competing)
        assert_state(rig, action)
    finally:
        await cleanup(loading, *([competing] if competing else []))


async def test_noop_verification_excludes_unload(rig, monkeypatch):
    manager, process = rig.manager, rig.process
    await manager.switch_model("a")
    gate = Gate()
    original_props = manager._fetch_props

    async def delayed_props():
        await gate.wait()
        return await original_props()

    monkeypatch.setattr(manager, "_fetch_props", delayed_props)
    noop = asyncio.create_task(manager.switch_model("a"))
    unloading = None
    try:
        await wait_event(gate.entered)
        attempted = asyncio.Event()

        async def unload():
            attempted.set()
            await manager.unload()

        unloading = asyncio.create_task(unload())
        await wait_event(attempted)
        assert not unloading.done()
        assert process.alive
        gate.release.set()
        assert await settle(noop) is False
        await settle(unloading)
        assert_state(rig, "unload")
    finally:
        await cleanup(noop, *([unloading] if unloading else []))


async def test_watchdog_crash_handling_excludes_foreground(rig, monkeypatch):
    manager, process = rig.manager, rig.process
    await manager.switch_model("a")
    process.alive = False
    crash_gate, backoff_gate = Gate(), Gate()

    async def crash_error():
        await crash_gate.wait()
        return "mock crash"

    monkeypatch.setattr(process, "crash_error", crash_error)
    monkeypatch.setattr(manager_mod.asyncio, "sleep", backoff_gate.wait)
    tick = asyncio.create_task(manager._watchdog_tick())
    switching = None
    try:
        await wait_event(crash_gate.entered)
        attempted = asyncio.Event()

        async def switch():
            attempted.set()
            await manager.switch_model("b")

        switching = asyncio.create_task(switch())
        await wait_event(attempted)
        assert not switching.done(), "crash introspection includes a destructive stop"
        crash_gate.release.set()
        await wait_event(backoff_gate.entered)
        await settle(switching)
        events = list(process.events)
        backoff_gate.release.set()
        assert await settle(tick) is False
        assert process.events == events
        assert_state(rig, "switch")
    finally:
        await cleanup(tick, *([switching] if switching else []))


async def test_cancelled_switch_releases_transition_and_vram(rig, monkeypatch):
    manager, process = rig.manager, rig.process
    gate = Gate()
    original_start = process.start
    monkeypatch.setattr(process, "start", gate.wait)
    task = asyncio.create_task(manager.switch_model("a"))
    try:
        await wait_event(gate.entered)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not manager._switch_in_progress
        assert not manager.vram.active_counts
        monkeypatch.setattr(process, "start", original_start)
        assert await asyncio.wait_for(manager.switch_model("b"), timeout=2) is True
        assert_state(rig, "switch")
    finally:
        await cleanup(task)


async def test_failed_watchdog_restarts_keep_retry_and_capped_backoff(rig, monkeypatch):
    manager, process = rig.manager, rig.process
    await manager.switch_model("a")
    process.alive = False
    original_start = process.start

    async def failing_start():
        process.events.append(("failed_start", process.next_path))

    monkeypatch.setattr(process, "start", failing_start)
    monkeypatch.setattr(manager_mod.asyncio, "sleep", AsyncMock())
    manager._watchdog_backoff = 1
    manager._watchdog_initial_backoff = 1
    manager._watchdog_max_backoff = 3
    for expected_backoff in (2, 3):
        with pytest.raises(ModelLoadError):
            await manager._watchdog_tick()
        assert manager.current_model is None
        assert manager._watchdog_retry_model == "a"
        assert manager._watchdog_backoff == expected_backoff
        assert not manager.vram.active_counts
        assert not manager._switch_in_progress
    monkeypatch.setattr(process, "start", original_start)
    assert await manager._watchdog_tick() is True
    assert manager._watchdog_retry_model is None
    assert manager._watchdog_backoff == 1
    assert_state(rig, "reload")


async def test_cancelled_lock_waiter_does_not_release_owners_lock(rig, monkeypatch):
    manager, process = rig.manager, rig.process
    gate = Gate()
    original_start = process.start

    async def delayed_start():
        await gate.wait()
        await original_start()

    monkeypatch.setattr(process, "start", delayed_start)
    owner = asyncio.create_task(manager.switch_model("a"))
    waiter = None
    try:
        await wait_event(gate.entered)
        attempted = asyncio.Event()

        async def unload():
            attempted.set()
            await manager.unload()

        waiter = asyncio.create_task(unload())
        await wait_event(attempted)
        assert not waiter.done()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert manager._switch_in_progress
        assert manager._lifecycle_lock.locked()
        gate.release.set()
        assert await settle(owner) is True
        await manager.unload()
        assert_state(rig, "unload")
    finally:
        await cleanup(owner, *([waiter] if waiter else []))


async def test_cancelled_watchdog_crash_handling_releases_lock(rig, monkeypatch):
    manager, process = rig.manager, rig.process
    await manager.switch_model("a")
    process.alive = False
    gate = Gate()
    monkeypatch.setattr(process, "crash_error", gate.wait)
    task = asyncio.create_task(manager._watchdog_tick())
    try:
        await wait_event(gate.entered)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not manager._switch_in_progress
        assert not manager._lifecycle_lock.locked()
        await asyncio.wait_for(manager.unload(), timeout=2)
        assert_state(rig, "unload")
    finally:
        await cleanup(task)


async def test_cancelled_watchdog_backoff_does_not_block_unload(rig, monkeypatch):
    manager, process = rig.manager, rig.process
    await manager.switch_model("a")
    process.alive = False
    gate = Gate()
    monkeypatch.setattr(manager_mod.asyncio, "sleep", gate.wait)
    task = asyncio.create_task(manager._watchdog_tick())
    try:
        await wait_event(gate.entered)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(manager.unload(), timeout=2)
        assert_state(rig, "unload")
    finally:
        await cleanup(task)
