"""Phase B tests — launch-signature drift detection + control API (PLAN.md §3).

Covers:
1. ``check_drift`` reports reload-needed on args-change / vision-toggle /
   context-hint-change, and a no-op (identical) returns False.
2. ``check_drift`` raises ``ValueError`` for an unknown model, and True when no
   persisted signature exists.
3. ``health()`` reports ``needs_reload`` after unload and False after a clean
   load.
4. The FastAPI control API via ``TestClient``: ``/ensure`` (idempotent),
   ``/status``, auth 401s, unknown-model 404, load-fail 503 with crash details,
   and invalid-body 422 — all with a manager injected through ``init()`` so a
   real backend is never touched.

Fixtures/fakes are reused from `tests/test_phase_a.py` (the file is importable
because pytest's prepend import mode puts `tests/` on `sys.path`; the explicit
path insert below is a defensive fallback for direct execution).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

# Ensure tests/ is importable even when not running under pytest's prepend mode.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from caretaker.manager import Caretaker
from caretaker.server import app, init

from test_phase_a import (
    SLOTS,
    FakeServerProcess,
    _make_manager,
    _write_models_yaml,
)

AUTH_HEADER = {"Authorization": "Bearer test-secret"}


@pytest.fixture
def isolated_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate the manager's deployment files + env to a per-test tmp dir.

    Mirrors ``test_phase_a.patch_paths`` (same monkeypatched attrs) so the
    launch-signature and deployment-file writes never touch the real repo
    ``config/`` dir. Defined locally (rather than importing the ``patch_paths``
    fixture) to keep ruff clean — a fixture imported then used as a parameter
    would trigger F811 for shadowing the import.
    """
    monkeypatch.setenv("CARETAKER_LLAMA_SLOTS_DIR", SLOTS)
    monkeypatch.setenv("CARETAKER_SERVER_URL", "http://127.0.0.1:11440")
    import caretaker.manager as manager_mod

    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_ARGS_FILE", tmp_path / "current_model.args")
    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_ENV_FILE", tmp_path / "current_model.env")
    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_SIG_FILE", tmp_path / "current_model.sig")


def _start_count(process: FakeServerProcess) -> int:
    """Number of ``start`` calls recorded by a fake ServerProcess."""
    return process.events.count("start")


def _fast_manager(
    tmp_path: Path,
    *,
    models: dict | None = None,
    process: FakeServerProcess | None = None,
    **kwargs: Any,
) -> Caretaker:
    """Build a manager with the slow network/syscall helpers neutralized.

    The real switch_model path calls ``_free_gpu_memory`` (nvidia-smi +
    ComfyUI HTTP) and context save/load against a live llama-server; these are
    stubbed to no-ops so the drift/lifecycle tests run fast and touch nothing.
    """
    process = process or FakeServerProcess()
    kwargs.setdefault("health_polls", 3)
    kwargs.setdefault("health_interval", 0.0)
    mgr = _make_manager(tmp_path, process=process, models=models, **kwargs)

    async def _noop(*_a: Any, **_k: Any) -> None:
        pass

    mgr._save_context = _noop  # type: ignore[method-assign]
    mgr._load_context = _noop  # type: ignore[method-assign]
    mgr._free_gpu_memory = _noop  # type: ignore[method-assign]
    mgr._verify_backend_model = _noop  # type: ignore[method-assign]
    return mgr


@pytest.fixture
def injection_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the control key + reset the server singleton after each API test."""
    monkeypatch.setenv("CARETAKER_KEY", "test-secret")
    yield
    init(None)


# ------------------------------------------------------------- check_drift


def test_drift_on_args_change(tmp_path: Path, isolated_paths: None) -> None:
    """Changing models.yaml (context 4096 → 8192) makes check_drift report drift.

    A persisted signature written by an earlier manager must be detected stale
    by a NEW manager reading the same sig file + the updated config."""
    models = {"X": {"path": "/home/flip/models/X.gguf", "context": 4096}}
    cfg = _write_models_yaml(tmp_path, models)

    import asyncio

    async def go() -> None:
        # Manager 1 loads X (context 4096) and persists its launch signature.
        await _fast_manager(tmp_path, models=models, process=FakeServerProcess()).switch_model("X")
        # Rewrite the shared config: context is now 8192.
        _write_models_yaml(tmp_path, {"X": {"path": "/home/flip/models/X.gguf", "context": 8192}})
        # Manager 2 reads the updated config + the same persisted sig file.
        m2 = Caretaker(config_path=str(cfg))
        assert m2.check_drift("X") is True

    asyncio.run(go())


def test_drift_on_vision_toggle(tmp_path: Path, isolated_paths: None) -> None:
    """Switching vision on after a text load is a real reload (drift), not a no-op."""
    mmproj = tmp_path / "mmproj.gguf"
    mmproj.write_bytes(b"mmproj")
    models = {
        "X": {
            "path": "/home/flip/models/X.gguf",
            "context": 8192,
            "mmproj": str(mmproj),
            "vision_context": 16384,
        }
    }
    process = FakeServerProcess()
    mgr = _fast_manager(tmp_path, models=models, process=process)
    mgr.current_model = None

    import asyncio

    async def go() -> None:
        await mgr.switch_model("X")
        starts_after_text = _start_count(process)
        assert starts_after_text == 1
        await mgr.switch_model("X", enable_vision=True)
        assert _start_count(process) == 2  # vision toggle reloaded, not no-op'd

    asyncio.run(go())


def test_drift_on_context_hint_change(tmp_path: Path, isolated_paths: None) -> None:
    """Changing the client context_hint (4096 → 8192) is a drift → reload."""
    models = {"X": {"path": "/home/flip/models/X.gguf", "context": 16384}}
    process = FakeServerProcess()
    mgr = _fast_manager(tmp_path, models=models, process=process)
    mgr.current_model = None

    import asyncio

    async def go() -> None:
        await mgr.switch_model("X", context_hint=4096)
        assert _start_count(process) == 1
        await mgr.switch_model("X", context_hint=8192)
        assert _start_count(process) == 2  # hint change reloaded

    asyncio.run(go())


def test_noop_identical_single_start(tmp_path: Path, isolated_paths: None) -> None:
    """Same model + vision + no drift → exactly one start total."""
    process = FakeServerProcess()
    mgr = _fast_manager(tmp_path, models={"X": {"path": "/home/flip/models/X.gguf"}}, process=process)
    mgr.current_model = None

    import asyncio

    async def go() -> None:
        await mgr.switch_model("X")
        await mgr.switch_model("X")  # identical → idempotent no-op
        assert _start_count(process) == 1
        assert mgr.check_drift("X") is False

    asyncio.run(go())


def test_check_drift_unknown_model_raises(tmp_path: Path, isolated_paths: None) -> None:
    """check_drift on an unconfigured model raises ValueError (switch_model contract)."""
    mgr = _fast_manager(tmp_path, models={"X": {"path": "/home/flip/models/X.gguf"}})
    with pytest.raises(ValueError):
        mgr.check_drift("nope")


def test_check_drift_true_when_no_persisted_signature(tmp_path: Path, isolated_paths: None) -> None:
    """No persisted sig → drift (a fresh server has no surviving launch to match)."""
    mgr = _fast_manager(tmp_path, models={"X": {"path": "/home/flip/models/X.gguf"}})
    assert mgr.check_drift("X") is True


# ---------------------------------------------------------------- health()


def test_health_needs_reload_true_after_unload(tmp_path: Path, isolated_paths: None) -> None:
    """After unload there is no backend: /ensure would be required → needs_reload True."""
    process = FakeServerProcess()
    mgr = _fast_manager(tmp_path, process=process, models={"X": {"path": "/home/flip/models/X.gguf"}})
    mgr.current_model = None

    import asyncio

    async def go() -> None:
        await mgr.switch_model("X")
        await mgr.unload()
        health = mgr.health()
        assert health["is_unloaded"] is True
        assert health["needs_reload"] is True

    asyncio.run(go())


def test_health_needs_reload_false_after_clean_load(tmp_path: Path, isolated_paths: None) -> None:
    """A clean load persists a matching signature → no reload needed."""
    process = FakeServerProcess()
    mgr = _fast_manager(tmp_path, process=process, models={"X": {"path": "/home/flip/models/X.gguf"}})
    mgr.current_model = None

    import asyncio

    async def go() -> None:
        await mgr.switch_model("X")
        health = mgr.health()
        assert health["loaded_model"] == "X"
        assert health["is_unloaded"] is False
        assert health["needs_reload"] is False

    asyncio.run(go())


# ------------------------------------------------------------- control API


def _client() -> TestClient:
    return TestClient(app)


def test_api_ensure_via_injected_manager(tmp_path: Path, isolated_paths: None, injection_reset: None) -> None:
    """POST /ensure loads a model through an injected (fake-backed) manager."""
    process = FakeServerProcess()
    mgr = _fast_manager(tmp_path, process=process, models={"X": {"path": "/home/flip/models/X.gguf"}})
    mgr.current_model = None
    init(mgr)
    with _client() as client:
        resp = client.post("/ensure", json={"model": "X"}, headers=AUTH_HEADER)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["loaded_model"] == "X"


def test_api_ensure_idempotent_single_start(tmp_path: Path, isolated_paths: None, injection_reset: None) -> None:
    """Two identical /ensure calls start the backend exactly once."""
    process = FakeServerProcess()
    mgr = _fast_manager(tmp_path, process=process, models={"X": {"path": "/home/flip/models/X.gguf"}})
    mgr.current_model = None
    init(mgr)
    with _client() as client:
        client.post("/ensure", json={"model": "X"}, headers=AUTH_HEADER)
        client.post("/ensure", json={"model": "X"}, headers=AUTH_HEADER)
    assert _start_count(process) == 1


def test_api_ensure_unknown_model_404(tmp_path: Path, isolated_paths: None, injection_reset: None) -> None:
    """Unknown model → 404 model_not_found."""
    mgr = _fast_manager(tmp_path, models={"X": {"path": "/home/flip/models/X.gguf"}})
    init(mgr)
    with _client() as client:
        resp = client.post("/ensure", json={"model": "nope"}, headers=AUTH_HEADER)
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["error"] == "model_not_found"


def test_api_ensure_load_fail_503_crash_details(
    tmp_path: Path, isolated_paths: None, injection_reset: None
) -> None:
    """Backend never becomes healthy → 503 model_load_failed with crash details."""
    mgr = _fast_manager(tmp_path, process=FakeServerProcess(health_ok=False))
    init(mgr)
    with _client() as client:
        resp = client.post("/ensure", json={"model": "minimal"}, headers=AUTH_HEADER)
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["error"] == "model_load_failed"
    assert body["crash_details"] is not None
    assert body["crash_details"]["model"] == "minimal"


def test_api_ensure_invalid_body_422(tmp_path: Path, isolated_paths: None, injection_reset: None) -> None:
    """Missing/malformed body → 422 invalid_request."""
    mgr = _fast_manager(tmp_path, models={"X": {"path": "/home/flip/models/X.gguf"}})
    init(mgr)
    with _client() as client:
        for payload in (None, {}, {"model": 42}):
            resp = client.post("/ensure", json=payload, headers=AUTH_HEADER)
            assert resp.status_code == 422, resp.text
            assert resp.json()["error"] == "invalid_request"


def test_api_status_injected_manager(tmp_path: Path, isolated_paths: None, injection_reset: None) -> None:
    """GET /status returns the injected manager's health dict."""
    mgr = _fast_manager(tmp_path, models={"X": {"path": "/home/flip/models/X.gguf"}})
    mgr.is_unloaded = True
    init(mgr)
    with _client() as client:
        resp = client.get("/status", headers=AUTH_HEADER)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_unloaded"] is True
    assert body["needs_reload"] is True


@pytest.mark.parametrize("route,method", [("/status", "get"), ("/ensure", "post"), ("/unload", "post")])
def test_api_auth_401_all_routes(
    route: str, method: str, tmp_path: Path, isolated_paths: None, injection_reset: None
) -> None:
    """With a configured key, a missing/wrong key is rejected 401 on every route."""
    mgr = _fast_manager(tmp_path, models={"X": {"path": "/home/flip/models/X.gguf"}})
    init(mgr)

    with _client() as client:
        # Missing key → 401.
        missing = getattr(client, method)(route)
        assert missing.status_code == 401, missing.text

        # Wrong key → 401.
        wrong = getattr(client, method)(route, headers={"Authorization": "Bearer wrong-key"})
        assert wrong.status_code == 401, wrong.text


def test_api_auth_configured_key_required(tmp_path: Path, isolated_paths: None, monkeypatch, injection_reset: None) -> None:
    """Without CARETAKER_KEY set, every route is 503 'key not configured'."""
    monkeypatch.delenv("CARETAKER_KEY")
    mgr = _fast_manager(tmp_path, models={"X": {"path": "/home/flip/models/X.gguf"}})
    init(mgr)
    with _client() as client:
        resp = client.get("/status")
        assert resp.status_code == 503, resp.text
        assert "not configured" in resp.json()["detail"]