"""Backend credentials are call-time and restricted to protected llama endpoints."""

import httpx
import pytest
from caretaker.manager import Caretaker, ServerProcess

from test_phase_a import FakeServerProcess, _make_manager


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.delenv("CARETAKER_BACKEND_KEY", raising=False)
    monkeypatch.setenv("CARETAKER_KEY", "control-key-must-not-reach-backend")
    manager = _make_manager(tmp_path, process=FakeServerProcess(), server_url="http://backend.invalid")
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"model_path": "/models/test.gguf"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(handle), trust_env=False, **kwargs
        ),
    )
    return manager, requests


async def protected_calls(manager):
    # Invoke real helpers rather than the lifecycle fixture's explicit mocks.
    assert await Caretaker._fetch_props(manager) == {"model_path": "/models/test.gguf"}
    await Caretaker._save_context(manager, "session")
    await Caretaker._load_context(manager, "session")


@pytest.mark.parametrize("key", [None, "", "backend-only-secret"])
async def test_backend_auth_header_on_protected_requests(backend, monkeypatch, key):
    manager, requests = backend
    if key is not None:
        monkeypatch.setenv("CARETAKER_BACKEND_KEY", key)
    await protected_calls(manager)
    assert [(r.method, r.url.path, r.url.query) for r in requests] == [
        ("GET", "/props", b""),
        ("POST", "/slots/0", b"action=save"),
        ("POST", "/slots/0", b"action=restore"),
    ]
    for request in requests:
        assert request.headers.get_list("authorization") == ([f"Bearer {key}"] if key else [])
    assert requests[1].content == requests[2].content == b'{"filename":"session"}'


async def test_key_changes_apply_after_manager_construction(backend, monkeypatch):
    manager, requests = backend
    for key in (None, "first-key", "rotated-key", None):
        if key is None:
            monkeypatch.delenv("CARETAKER_BACKEND_KEY", raising=False)
        else:
            monkeypatch.setenv("CARETAKER_BACKEND_KEY", key)
        await protected_calls(manager)
        assert [r.headers.get("authorization") for r in requests[-3:]] == [
            f"Bearer {key}" if key else None
        ] * 3


async def test_health_probe_does_not_receive_backend_key(backend, monkeypatch):
    manager, requests = backend
    monkeypatch.setenv("CARETAKER_BACKEND_KEY", "protected-endpoints-only")
    assert await ServerProcess.health_ok(manager.server_process, manager.server_url)
    assert requests[0].url.path == "/health"
    assert "authorization" not in requests[0].headers
