"""Text-evidenced crash classification and additive control-API serialization."""

from unittest.mock import AsyncMock

import pytest
from caretaker import manager as manager_mod
from caretaker import server
from caretaker.manager import CrashRecord
from fastapi.testclient import TestClient

from test_phase_a import FakeServerProcess, _make_manager


@pytest.fixture
def isolated_paths(monkeypatch, tmp_path):
    for name in ("ARGS", "ENV", "SIG"):
        monkeypatch.setattr(manager_mod, f"CURRENT_MODEL_{name}_FILE", tmp_path / name)


@pytest.fixture
def injection_reset(isolated_api_lifespan, monkeypatch):
    monkeypatch.setenv("CARETAKER_KEY", "control-test-key")
    server.init(None)
    yield
    server.init(None)


@pytest.mark.parametrize(("text", "expected"), [
    ("CUDA out of memory", (True, "cuda")),
    ("cudaMalloc failed", (True, "cuda")),
    ("failed to fit params to free device memory", (True, "cuda")),
    ("CUDA error: failed to allocate compute buffers", (True, "cuda")),
    ("out of memory", (True, "kernel")),
    ("failed to allocate host buffer", (True, "kernel")),
    ("cannot meet free memory targets", (True, "kernel")),
    ("segmentation fault", (False, None)),
    ("Unknown error", (False, None)),
    ("CUDA error: invalid device ordinal", (False, None)),
    ("", (False, None)),
])
def test_classify_oom_text(text, expected):
    assert manager_mod.classify_oom(text) == expected


@pytest.mark.parametrize(("text", "exit_code", "expected"), [
    ("CUDA out of memory", 1, (True, "cuda")),
    ("failed to allocate host buffer", None, (True, "kernel")),
    ("Killed", 137, (False, None)),
    ("segmentation fault", 139, (False, None)),
    ("Unknown error", None, (False, None)),
])
async def test_detect_crash_classifies_text_not_exit_code(tmp_path, monkeypatch, text, exit_code, expected):
    process = FakeServerProcess()
    monkeypatch.setattr(process, "crash_error", AsyncMock(return_value=text))
    monkeypatch.setattr(process, "service_exit_code", AsyncMock(return_value=exit_code))
    manager = _make_manager(tmp_path, process=process)
    async with manager._lifecycle_lock:
        crash = await manager._detect_crash("minimal")
    assert (crash.oom, crash.oom_source) == expected
    assert crash.exit_code == exit_code
    assert manager.last_crash is crash
    assert manager.crash_history == [crash]
    assert process.events == ["stop"]


def test_crash_record_serialization_is_additive():
    original = {
        "timestamp": "2026-10-03T00:00:00+00:00",
        "model": "example",
        "error_message": "failure",
        "exit_code": 137,
        "config_snapshot": {"context": 4096},
    }
    assert CrashRecord(**original).to_dict() == {**original, "oom": False, "oom_source": None}
    assert CrashRecord(**original, oom=True, oom_source="cuda").to_dict() == {
        **original, "oom": True, "oom_source": "cuda",
    }


def test_ensure_503_passes_classified_crash_details(
    tmp_path, monkeypatch, isolated_paths, injection_reset
):
    process = FakeServerProcess(health_ok=False)
    monkeypatch.setattr(process, "crash_error", AsyncMock(return_value="cudaMalloc failed"))
    monkeypatch.setattr(process, "service_exit_code", AsyncMock(return_value=1))
    manager = _make_manager(tmp_path, process=process, health_polls=0)
    server.init(manager)
    with TestClient(server.app) as client:
        response = client.post(
            "/ensure", json={"model": "minimal"},
            headers={"Authorization": "Bearer control-test-key"},
        )
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "model_load_failed"
    assert body["crash_details"] == manager.last_crash.to_dict()
    assert body["crash_details"]["oom"] is True
    assert body["crash_details"]["oom_source"] == "cuda"
    assert body["crash_details"]["model"] == "minimal"
    assert body["crash_details"]["error_message"] == "cudaMalloc failed"
