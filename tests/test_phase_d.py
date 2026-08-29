"""Phase D tests — idle-unload contract + ensure-recovery (PLAN.md §5).

The idle *decision* stays in the gateway (it sees the queue); caretaker owns
the *execution* surface: ``/unload`` (Phase C) + ``/status`` with
``idle_since``/``loaded_at``. These tests pin the contract the gateway
consumes:

1. ``/status`` reports load-age proxies (``loaded_at``/``idle_since``) only
   while a model is loaded, and clears them on unload/stop.
2. The unload → ensure recovery loop is transparent: after a manual/automatic
   unload, ``POST /ensure`` reloads and ``/status`` flips back to loaded — the
   gateway's 503 → /ensure recovery path works against this surface.
3. The unload→ensure→unload lifecycle is idempotent and keeps VRAM accounting
   truthful (no double slots after repeated cycles).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from caretaker.server import app, init

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
    yield
    init(None)


def test_health_reports_loaded_at_and_idle_since_while_loaded(
    tmp_path: Path, isolated_paths: None
) -> None:
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc)
    assert mgr.health()["loaded_at"] is None
    assert mgr.health()["idle_since"] is None

    import asyncio

    asyncio.run(mgr.switch_model("minimal"))
    health = mgr.health()
    assert health["is_unloaded"] is False
    assert health["loaded_at"] is not None
    assert health["idle_since"] == health["loaded_at"]
    assert health["needs_reload"] is False


def test_health_clears_idle_fields_on_unload(tmp_path: Path, isolated_paths: None) -> None:
    import asyncio

    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc)
    asyncio.run(mgr.switch_model("minimal"))
    assert mgr.health()["loaded_at"] is not None

    asyncio.run(mgr.unload())
    health = mgr.health()
    assert health["is_unloaded"] is True
    assert health["loaded_at"] is None
    assert health["idle_since"] is None
    assert health["needs_reload"] is True


def test_status_idle_fields_via_api(tmp_path: Path, isolated_paths: None, injection_reset, monkeypatch) -> None:
    """The gateway reads the idle contract from ``GET /status`` (Bearer key)."""
    monkeypatch.setenv("CARETAKER_KEY", "test-secret")
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc)
    init(mgr)
    client = TestClient(app)

    import asyncio

    asyncio.run(mgr.switch_model("minimal"))

    r = client.get("/status", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["is_unloaded"] is False
    assert body["loaded_at"] is not None
    assert body["idle_since"] == body["loaded_at"]

    asyncio.run(mgr.unload())
    r2 = client.get("/status", headers=AUTH)
    assert r2.json()["loaded_at"] is None
    assert r2.json()["idle_since"] is None


def test_ensure_recovers_after_unload_api(
    tmp_path: Path, isolated_paths: None, injection_reset, monkeypatch
) -> None:
    """The gateway's 503 → /ensure recovery must work against this surface:

    after a manual/automatic ``POST /unload``, a subsequent ``POST /ensure
    {model}`` reloads the model and ``GET /status`` flips back to loaded.
    """
    monkeypatch.setenv("CARETAKER_KEY", "test-secret")
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc)
    init(mgr)
    client = TestClient(app)

    import asyncio

    asyncio.run(mgr.switch_model("minimal"))
    assert mgr.current_model == "minimal"

    # 1. unload (what the gateway's idle watcher calls when the queue is empty)
    r = client.post("/unload", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["is_unloaded"] is True
    assert client.get("/status", headers=AUTH).json()["is_unloaded"] is True

    # 2. ensure (what the gateway calls when a request arrives / a 503 occurs)
    r2 = client.post("/ensure", json={"model": "minimal"}, headers=AUTH)
    assert r2.status_code == 200, r2.text
    assert r2.json()["ok"] is True
    assert r2.json()["loaded_model"] == "minimal"

    # 3. status is loaded again; the ensure was a real (re)load, not a no-op
    assert client.get("/status", headers=AUTH).json()["is_unloaded"] is False
    assert proc.events.count("start") >= 2, "unload→ensure must actually restart"


def test_unload_ensure_unload_cycles_keep_vram_truthful(
    tmp_path: Path, isolated_paths: None
) -> None:
    """Repeated unload→ensure cycles must not leak VRAM slots (accounting stays
    truthful for the gateway's idle-unload cadence)."""
    import asyncio

    proc = FakeServerProcess()
    mgr = _make_manager(
        tmp_path,
        process=proc,
        vram_limit_mb=500,
        models={"minimal": {"path": "/home/flip/models/minimal.gguf", "size_mb": 300}},
    )
    for _ in range(3):
        asyncio.run(mgr.switch_model("minimal"))
        assert mgr.vram.active_counts.get("minimal", 0) == 1
        asyncio.run(mgr.unload())
        assert "minimal" not in mgr.vram.active_counts
        assert mgr._loaded_at is None
    assert not mgr._switch_in_progress
    assert mgr.crash_history == []  # no spurious crashes across cycles