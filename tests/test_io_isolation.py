"""Regression evidence for fail-fast I/O protection and explicit lifecycle fakes."""

import asyncio
import json
import socket
import subprocess

import httpx
import pytest
from caretaker.manager import Caretaker

from io_guard import UnsafeIOError
from test_phase_a import FakeServerProcess, _make_manager


@pytest.fixture
def probe_guard(io_guard):
    """Only guard regression tests deliberately provoke and assert one denial."""
    yield io_guard
    assert len(io_guard.violations) == 1
    io_guard.violations.clear()


@pytest.fixture
def unused_address():
    # Reserve but do not listen: never depend on any host service or fixed port.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        yield sock.getsockname()


def test_unintended_http_connection_fails(probe_guard, unused_address):
    host, port = unused_address
    with (
        httpx.Client(trust_env=False) as client,
        pytest.raises(UnsafeIOError, match="Unmocked socket connection"),
    ):
        client.get(f"http://{host}:{port}/health")
    assert len(probe_guard.violations) == 1


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_socket_connections_fail_before_os_call(probe_guard, unused_address, method):
    with socket.socket() as sock, pytest.raises(UnsafeIOError, match="socket connection"):
        getattr(sock, method)(unused_address)
    assert len(probe_guard.violations) == 1


def test_swallowed_violation_still_fails_teardown_check(probe_guard, unused_address):
    from contextlib import suppress

    # Deliberately reproduce a caller that discards even BaseException signals.
    with socket.socket() as sock, suppress(BaseException):
        sock.connect(unused_address)
    with pytest.raises(AssertionError, match="Unmocked I/O attempted"):
        probe_guard.assert_clean()


def test_dns_lookup_fails(probe_guard):
    with pytest.raises(UnsafeIOError, match="DNS lookup"):
        socket.getaddrinfo("backend.invalid", None)
    assert len(probe_guard.violations) == 1


def test_subprocess_fails_before_launch(probe_guard):
    with pytest.raises(UnsafeIOError, match="process I/O"):
        subprocess.run(["must-not-execute"], check=True)
    assert len(probe_guard.violations) == 1


async def test_async_process_fails_before_launch(probe_guard):
    with pytest.raises(UnsafeIOError, match="process I/O"):
        await asyncio.create_subprocess_exec("must-not-execute")
    assert len(probe_guard.violations) == 1


async def test_only_registered_live_listener_is_allowed(probe_guard):
    async def echo(reader, writer):
        writer.write(await reader.read(4))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    address = server.sockets[0].getsockname()
    try:
        with probe_guard.allow_listener(server):
            assert probe_guard.permits(address)
            reader, writer = await asyncio.open_connection(*address)
            writer.write(b"ping")
            await writer.drain()
            assert await reader.read(4) == b"ping"
            writer.close()
            await writer.wait_closed()
        assert not probe_guard.permits(address)
        with socket.socket() as sock, pytest.raises(UnsafeIOError):
            sock.connect(address)
    finally:
        server.close()
        await server.wait_closed()
    assert len(probe_guard.violations) == 1


async def test_closed_listener_permission_is_not_reusable(io_guard):
    server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    address = server.sockets[0].getsockname()
    with io_guard.allow_listener(server):
        assert io_guard.permits(address)
        server.close()
        await server.wait_closed()
        assert not io_guard.permits(address)


async def test_context_save_cannot_swallow_blocked_http(
    tmp_path, monkeypatch, probe_guard, unused_address
):
    host, port = unused_address
    mgr = _make_manager(tmp_path, process=FakeServerProcess(), server_url=f"http://{host}:{port}")
    # Use the actual helper (not the fixture mock) and actual HTTP transport.
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda: real_client(trust_env=False))
    with pytest.raises(BaseExceptionGroup) as caught:
        await Caretaker._save_context(mgr, "test-only")
    assert caught.value.subgroup(UnsafeIOError) is not None
    assert len(probe_guard.violations) == 1


async def test_context_helpers_use_fake_http_transport(tmp_path, monkeypatch, io_guard):
    requests = []

    def handle(request):
        requests.append((request.method, request.url.path, request.url.query, json.loads(request.content)))
        return httpx.Response(200)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda: real_client(transport=httpx.MockTransport(handle), trust_env=False),
    )
    mgr = _make_manager(tmp_path, process=FakeServerProcess(), server_url="http://backend.invalid")
    await Caretaker._save_context(mgr, "test-context")
    await Caretaker._load_context(mgr, "test-context")
    assert requests == [
        ("POST", "/slots/0", b"action=save", {"filename": "test-context"}),
        ("POST", "/slots/0", b"action=restore", {"filename": "test-context"}),
    ]
    assert io_guard.violations == []


async def test_lifecycle_fixture_uses_explicit_mocks(tmp_path, monkeypatch, io_guard):
    import caretaker.manager as manager_mod

    for name in ("ARGS", "ENV", "SIG"):
        monkeypatch.setattr(manager_mod, f"CURRENT_MODEL_{name}_FILE", tmp_path / name)
    proc = FakeServerProcess()
    mgr = _make_manager(
        tmp_path, process=proc,
        models={"first": {"path": "first.gguf"}, "second": {"path": "second.gguf"}},
    )
    await mgr.switch_model("first")
    mgr._save_context.assert_not_awaited()
    mgr._load_context.assert_awaited_once_with("auto_save_first")
    mgr._load_context.reset_mock()
    await mgr.switch_model("second")
    mgr._save_context.assert_awaited_once_with("auto_save_first")
    mgr._load_context.assert_awaited_once_with("auto_save_second")
    assert mgr._free_gpu_memory.await_count == 2
    assert proc.events == ["stop", "start", "stop", "start"]
    assert io_guard.violations == []
