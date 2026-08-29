"""Phase C tests — watchdog, VRAM slot, /unload (PLAN.md §4).

Covers:
1. ``VramScheduler`` acquire/release semantics (fits / blocks / notifies).
2. ``get_model_size_mb`` precedence (config size_mb > heuristics > 0).
3. ``switch_model`` VRAM integration: acquire on real load, release on
   failure/unload, no double-account on no-op.
4. Watchdog ``_watchdog_tick``: healthy no-op, crash → record + restart,
   backoff doubling on load failure, skip while a transition is in flight.
5. Control API ``POST /unload`` (idempotent; auth-gated).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from caretaker.manager import ModelLoadError
from caretaker.server import app, init
from caretaker.vram import VramScheduler, get_model_size_mb

from test_phase_a import FakeServerProcess, _make_manager

AUTH = {"Authorization": "Bearer test-secret"}


@pytest.fixture
def isolated_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate deployment files + env to a per-test tmp dir (mirror of
    ``test_phase_a.patch_paths``)."""
    monkeypatch.setenv("CARETAKER_LLAMA_SLOTS_DIR", "/usr/local/share/caretaker/fixture_slots")
    monkeypatch.setenv("CARETAKER_SERVER_URL", "http://127.0.0.1:11440")
    import caretaker.manager as manager_mod

    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_ARGS_FILE", tmp_path / "current_model.args")
    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_ENV_FILE", tmp_path / "current_model.env")
    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_SIG_FILE", tmp_path / "current_model.sig")


@pytest.fixture
def injection_reset():
    """Guarantee the server singleton is reset even if a test fails mid-way."""
    yield
    init(None)


# ---------------------------------------------------------------------------
# VramScheduler
# ---------------------------------------------------------------------------


class _NoRecordFake(FakeServerProcess):
    """FakeServerProcess with a mutable health flag (watchdog needs toggle)."""

    def __init__(self, health_ok: bool = True) -> None:
        super().__init__(health_ok=health_ok)
        self.health_flag = health_ok

    async def health_ok(self, url: str = "") -> bool:
        return self.health_flag

    async def stop(self) -> None:
        await super().stop()
        self.health_flag = False  # a stopped backend is dead

    async def start(self) -> None:
        await super().start()
        self.health_flag = True  # a freshly started backend is healthy


class _AlwaysDownFake(_NoRecordFake):
    """A backend that never becomes healthy — even after start() — so a
    restart attempt fails (watchdog backoff path)."""

    async def start(self) -> None:
        self.events.append("start")  # log the attempt; health stays down


async def test_vram_acquire_fits_and_release_notifies() -> None:
    sched = VramScheduler(limit_mb=1000)
    await sched.acquire("a", 400)
    await sched.acquire("a", 400)  # same model, no extra size
    assert sched.active_counts["a"] == 2
    await sched.release("a")
    assert sched.active_counts["a"] == 1
    await sched.release("a")
    assert "a" not in sched.active_counts


async def test_vram_acquire_blocks_until_space() -> None:
    sched = VramScheduler(limit_mb=1000)
    await sched.acquire("big", 900)
    # second acquire of a different model must block
    task = asyncio.create_task(sched.acquire("other", 900))
    await asyncio.sleep(0.05)
    assert not task.done(), "acquire should block while VRAM is full"
    await sched.release("big")
    await asyncio.wait_for(task, timeout=1)
    assert "other" in sched.active_counts


async def test_vram_zero_size_acquires_immediately() -> None:
    sched = VramScheduler(limit_mb=10)
    await sched.acquire("unknown-size", 0)  # no warning-level block
    assert "unknown-size" in sched.active_counts


def test_get_model_size_precedence() -> None:
    assert get_model_size_mb("whatever", {"size_mb": 123}) == 123
    assert get_model_size_mb("some-glm-4-model", {}) == 26000
    assert get_model_size_mb("qwen3-30b", {}) == 20000
    assert get_model_size_mb("deepseek-r1-32b", {}) == 22000
    assert get_model_size_mb("totally-unknown", {}) == 0
    assert get_model_size_mb("", {}) == 0


# ---------------------------------------------------------------------------
# switch_model VRAM integration
# ---------------------------------------------------------------------------


async def test_switch_model_acquires_and_releases_on_unload(
    tmp_path: Path, isolated_paths: None
) -> None:
    proc = FakeServerProcess()
    mgr = _make_manager(
        tmp_path,
        process=proc,
        vram_limit_mb=500,
        models={"minimal": {"path": "/home/flip/models/minimal.gguf", "size_mb": 300}},
    )
    await mgr.switch_model("minimal")
    assert mgr.current_model == "minimal"
    assert mgr.vram is not None
    assert mgr.vram.active_counts.get("minimal", 0) == 1

    await mgr.unload()
    assert "minimal" not in mgr.vram.active_counts
    assert mgr.is_unloaded


async def test_switch_model_vram_block_then_unload_frees(
    tmp_path: Path, isolated_paths: None
) -> None:
    proc = FakeServerProcess()
    models = {
        "first": {"path": "/home/flip/models/first.gguf", "size_mb": 400},
        "second": {"path": "/home/flip/models/second.gguf", "size_mb": 400},
    }
    mgr = _make_manager(tmp_path, process=proc, vram_limit_mb=500, models=models)
    await mgr.switch_model("first")
    task = asyncio.create_task(mgr.switch_model("second"))
    await asyncio.sleep(0.05)
    assert not task.done(), "switch to second must block: VRAM full"
    await mgr.unload()  # frees 'first'
    await asyncio.wait_for(task, timeout=2)
    assert mgr.current_model == "second"
    assert mgr.vram.active_counts.get("second", 0) == 1


async def test_switch_model_releases_vram_on_load_failure(
    tmp_path: Path, isolated_paths: None
) -> None:
    proc = FakeServerProcess(health_ok=False)
    mgr = _make_manager(
        tmp_path,
        process=proc,
        vram_limit_mb=500,
        health_polls=3,
        health_interval=0.01,
        models={"minimal": {"path": "/home/flip/models/minimal.gguf", "size_mb": 300}},
    )
    with pytest.raises(ModelLoadError):
        await mgr.switch_model("minimal")
    assert mgr.current_model is None
    assert mgr.vram.active_counts.get("minimal", 0) == 0, "failed load releases slot"


async def test_switch_model_noop_does_not_double_acquire(
    tmp_path: Path, isolated_paths: None
) -> None:
    proc = FakeServerProcess()
    mgr = _make_manager(
        tmp_path,
        process=proc,
        vram_limit_mb=500,
        models={"minimal": {"path": "/home/flip/models/minimal.gguf", "size_mb": 300}},
    )
    await mgr.switch_model("minimal")
    first_events = list(proc.events)
    await mgr.switch_model("minimal")  # no-op
    assert proc.events == first_events
    assert mgr.vram.active_counts.get("minimal", 0) == 1, "no double acquire"


async def test_check_vram_false_disables_slot(tmp_path: Path, isolated_paths: None) -> None:
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc, check_vram=False)
    assert mgr.vram is None
    await mgr.switch_model("minimal")
    assert mgr.current_model == "minimal"


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------


async def test_watchdog_tick_healthy_no_action(tmp_path: Path, isolated_paths: None) -> None:
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc)
    await mgr.switch_model("minimal")
    events_before = list(proc.events)
    restarted = await mgr._watchdog_tick()
    assert restarted is False
    assert proc.events == events_before


async def test_watchdog_tick_crash_restarts(tmp_path: Path, isolated_paths: None) -> None:
    proc = _NoRecordFake(health_ok=True)
    mgr = _make_manager(
        tmp_path, process=proc, health_polls=3, health_interval=0.01
    )
    mgr._watchdog_backoff = 0  # no real sleep in the test
    await mgr.switch_model("minimal")  # load succeeds (backend healthy)
    assert mgr.current_model == "minimal"
    proc.health_flag = False  # backend dies *after* the load
    starts_before = proc.events.count("start")
    restarted = await mgr._watchdog_tick()
    assert restarted is True
    assert proc.events.count("start") > starts_before, "must restart the crashed backend"
    assert mgr.crash_history, "crash must be recorded"


async def test_watchdog_tick_skips_unloaded(tmp_path: Path, isolated_paths: None) -> None:
    proc = _NoRecordFake(health_ok=False)
    mgr = _make_manager(tmp_path, process=proc)
    mgr.is_unloaded = True
    assert await mgr._watchdog_tick() is False


async def test_watchdog_tick_backoff_doubles_on_load_failure(
    tmp_path: Path, isolated_paths: None
) -> None:
    proc = _AlwaysDownFake(health_ok=False)  # health stays false → switch fails
    mgr = _make_manager(tmp_path, process=proc, health_polls=2, health_interval=0.01)
    mgr._watchdog_backoff = 0.25  # short sleep; doubling is what we assert
    mgr._watchdog_max_backoff = 2.0
    mgr.current_model = "minimal"  # pretend a model is loaded
    with pytest.raises(ModelLoadError):
        await mgr._watchdog_tick()
    assert mgr._watchdog_backoff == 0.5, "backoff doubles on failed restart"


async def test_watchdog_loop_stops_cleanly(tmp_path: Path, isolated_paths: None) -> None:
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc)
    mgr.start_watchdog(interval=0.01, initial_backoff=0, max_backoff=0)
    await asyncio.sleep(0.05)  # a few ticks run
    mgr.stop_watchdog()
    assert mgr._watchdog_task is None or mgr._watchdog_task.done()


# ---------------------------------------------------------------------------
# Control API /unload
# ---------------------------------------------------------------------------


def test_api_unload_200_and_idempotent(tmp_path: Path, isolated_paths: None, injection_reset, monkeypatch) -> None:
    monkeypatch.setenv("CARETAKER_KEY", "test-secret")
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc)
    init(mgr)
    client = TestClient(app)

    r = client.post("/unload", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "is_unloaded": True}
    assert mgr.is_unloaded

    r2 = client.post("/unload", headers=AUTH)
    assert r2.status_code == 200, r2.text  # idempotent


def test_api_unload_401_without_key(tmp_path: Path, isolated_paths: None, injection_reset, monkeypatch) -> None:
    monkeypatch.setenv("CARETAKER_KEY", "test-secret")
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc)
    init(mgr)
    client = TestClient(app)

    assert client.post("/unload").status_code == 401
    assert (
        client.post("/unload", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )