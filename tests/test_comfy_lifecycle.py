"""Comfy lifecycle pins — idle-stop + wake proxy (the operator's VRAM spec:
GPU services stop after 5 min of non-use and the caretaker restarts them on
the next request)."""

import asyncio
import contextlib
import socket
import time

import httpx
import pytest
from caretaker import comfy as comfy_mod


@pytest.fixture(autouse=True)
def _fresh_state():
    comfy_mod._last_activity_monotonic = None
    comfy_mod._proxy_server = None
    comfy_mod._enabled = False
    comfy_mod._slot_semaphore = None
    comfy_mod._slot_semaphore_size = 0
    comfy_mod._slot_active = 0
    comfy_mod._slot_waiting = 0
    comfy_mod._last_free_mb = None
    comfy_mod._vram_policy_warned = False
    comfy_mod._vram_gate_lock = asyncio.Lock()
    comfy_mod._llama_evict_hook = None
    comfy_mod._last_evict_monotonic = None
    comfy_mod._evict_count = 0
    comfy_mod._evict_task = None
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


@pytest.fixture
def proxy_listener(monkeypatch, allow_test_listener):
    """Reserve an ephemeral socket; start_proxy's port=0 means disabled."""
    async def start_proxy():
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        real_start_server = asyncio.start_server

        async def start_server(handler, host, requested_port):
            assert (host, requested_port) == ("127.0.0.1", port)
            server = await real_start_server(handler, sock=sock)
            allow_test_listener(server)
            return server

        monkeypatch.setenv("CARETAKER_COMFY_PROXY_PORT", str(port))
        try:
            with monkeypatch.context() as patch:
                patch.setattr(asyncio, "start_server", start_server)
                await comfy_mod.start_proxy()
        except BaseException:
            sock.close()
            raise
        return port

    return start_proxy


# --- watcher tick ---


@pytest.mark.asyncio
async def test_idle_tick_stops_comfy_after_budget(monkeypatch):
    _env(monkeypatch, CARETAKER_COMFY_IDLE_SECONDS="5")
    async def snap():
        return {"queue_running": [], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)
    stops = []

    async def ok_stop():
        stops.append(1)
        return True

    monkeypatch.setattr(comfy_mod, "stop_comfy", ok_stop)
    comfy_mod._last_activity_monotonic = time.monotonic() - 10  # idle past the budget
    assert await comfy_mod._idle_watcher_tick() == "stopped"
    assert stops == [1]


@pytest.mark.asyncio
async def test_idle_tick_reports_failed_stop(monkeypatch):
    _env(monkeypatch, CARETAKER_COMFY_IDLE_SECONDS="5")
    async def snap():
        return {"queue_running": [], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)
    async def failed_stop():
        return False

    monkeypatch.setattr(comfy_mod, "stop_comfy", failed_stop)
    comfy_mod._last_activity_monotonic = time.monotonic() - 10
    assert await comfy_mod._idle_watcher_tick() == "stop_failed"  # never "stopped"


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
async def test_proxy_pumps_when_comfy_already_up(monkeypatch, proxy_listener, allow_test_listener):
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
    allow_test_listener(upstream)
    port_up = upstream.sockets[0].getsockname()[1]
    # comfy_url() prefers CARETAKER_COMFY_URL — point it at the fake upstream
    monkeypatch.setenv("CARETAKER_COMFY_URL", f"http://127.0.0.1:{port_up}")
    port = await proxy_listener()

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"ping")
    await writer.drain()
    assert await reader.read(100) == b"PING"
    writer.close()
    upstream.close()
    assert wake_calls == []  # comfy already up -> no wake needed


@pytest.mark.asyncio
async def test_proxy_wake_failure_closes_connection(monkeypatch, proxy_listener):
    _env(monkeypatch)

    async def snap():
        return None  # comfy down

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)

    async def fake_start():
        return False  # the start command failed

    monkeypatch.setattr(comfy_mod, "start_comfy", fake_start)
    port = await proxy_listener()

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET / HTTP/1.0\r\n\r\n")
    await writer.drain()
    # Contract: a failed wake drops the connection WITHOUT response data —
    # the client sees EOF or a reset, never a fake success.
    with pytest.raises((ConnectionResetError, ConnectionAbortedError)):
        await reader.read(100)
    assert reader.at_eof() or reader.exception() is not None


# --- status surface ---


@pytest.mark.asyncio
async def test_unbindable_port_degrades_instead_of_boot_failure(monkeypatch):
    """A taken/unbindable proxy port must never abort the caretaker boot —
    the feature degrades to proxy-off and init_async still completes."""
    _env(monkeypatch, CARETAKER_COMFY_PROXY_PORT="18127")

    async def boom(handler, host, port):
        raise OSError(98, "address already in use")

    real = comfy_mod.asyncio.start_server
    comfy_mod.asyncio.start_server = boom
    try:
        await comfy_mod.init_async()  # must NOT raise
    finally:
        comfy_mod.asyncio.start_server = real
    assert comfy_mod._proxy_server is None  # degraded, not bound
    assert comfy_mod._watcher_task is not None  # watcher still armed
    await comfy_mod.shutdown_async()


@pytest.mark.asyncio
async def test_silent_connection_times_out_and_releases(monkeypatch, allow_test_listener):
    """An abandoned connection (no bytes, no FIN) must not pin the idle
    release forever — the pipe closes after the silence budget."""
    _env(monkeypatch)
    monkeypatch.setenv("CARETAKER_COMFY_CONN_IDLE_TIMEOUT", "1")
    comfy_mod._active_connections = 1
    async def black_hole(reader, writer):
        await asyncio.sleep(60)  # holds the connection open, never sends

    server = await asyncio.start_server(black_hole, "127.0.0.1", 0)
    allow_test_listener(server)
    port = server.sockets[0].getsockname()[1]
    _client_reader, client_writer = await asyncio.open_connection("127.0.0.1", port)
    upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", port)

    async def dead_pipe():
        await comfy_mod._pipe(upstream_reader, upstream_writer)

    started = time.monotonic()
    await asyncio.wait_for(dead_pipe(), timeout=5)  # must END, not hang
    elapsed = time.monotonic() - started
    assert 0.9 <= elapsed < 4.5  # released after the ~1s silence budget
    assert upstream_writer.transport.is_closing()
    client_writer.close()
    server.close()
    upstream_writer.close()
    comfy_mod._active_connections = 0


@pytest.mark.asyncio
async def test_open_connection_blocks_idle_stop(monkeypatch):
    """A client holding an open proxied session (web UI / websocket) counts as
    activity — the backend must not be stopped underneath it."""
    _env(monkeypatch, CARETAKER_COMFY_IDLE_SECONDS="5")
    async def snap():
        return {"queue_running": [], "queue_pending": []}  # queue empty

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)
    stops = []

    async def ok_stop():
        stops.append(1)
        return True

    monkeypatch.setattr(comfy_mod, "stop_comfy", ok_stop)
    comfy_mod._last_activity_monotonic = time.monotonic() - 999
    comfy_mod._active_connections = 1  # a client is connected
    assert await comfy_mod._idle_watcher_tick() == "busy"
    assert stops == []


def test_malformed_env_falls_back_to_defaults(monkeypatch):
    """A garbage env value must never abort boot or wedge the watcher —
    every integer env read falls back to its default."""
    _env(
        monkeypatch,
        CARETAKER_COMFY_IDLE_SECONDS="five minutes ",
        CARETAKER_COMFY_PROXY_PORT="8188 ",
        CARETAKER_COMFY_WAKE_TIMEOUT="soon",
    )
    assert comfy_mod._idle_seconds() == 300  # non-numeric -> default
    # a benign trailing space is stripped and parses
    assert comfy_mod._env_int("CARETAKER_COMFY_PROXY_PORT", 0) == 8188
    assert comfy_mod._env_int("CARETAKER_COMFY_WAKE_TIMEOUT", 90) == 90


@pytest.mark.asyncio
async def test_status_reports_shape(monkeypatch):
    _env(monkeypatch, CARETAKER_COMFY_PROXY_PORT="18125")

    requests = []

    def offline(request):
        requests.append(request)
        raise httpx.ConnectError("Synthetic offline backend", request=request)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        comfy_mod.httpx, "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(offline), **kwargs),
    )
    status = await comfy_mod.astatus()
    assert len(requests) == 1
    assert requests[0].url.path == "/queue"
    assert status["idle_seconds"] == 300
    assert status["proxy_port"] == 18125
    assert status["up"] is False
    # Commands never echo raw — only their configured-ness (info exposure).
    assert status["start_configured"] is True
    assert status["stop_configured"] is True
    assert "start_command" not in status and "stop_command" not in status


@pytest.mark.asyncio
async def test_proxy_binds_loopback_by_default(monkeypatch):
    """Safe-by-default: the wake proxy binds 127.0.0.1 unless the operator
    explicitly exposes it (a LAN-reachable wake is an unauthenticated GPU
    trigger)."""
    _env(monkeypatch, CARETAKER_COMFY_PROXY_PORT="18126")
    binds = []

    from unittest.mock import Mock

    real_start_server = comfy_mod.asyncio.start_server

    async def spy(handler, host, port):
        binds.append(host)
        return Mock()

    comfy_mod.asyncio.start_server = spy
    try:
        await comfy_mod.start_proxy()
    finally:
        comfy_mod.asyncio.start_server = real_start_server
    assert binds == ["127.0.0.1"]
    assert comfy_mod._proxy_server is not None
    comfy_mod._proxy_server.close()
    comfy_mod._proxy_server = None


def test_portless_url_falls_back_to_module_default(monkeypatch):
    """One internal-port default everywhere: a port-less URL falls back to
    8189 (the module/deployment default), never Comfy's stock 8188."""
    _env(monkeypatch)
    monkeypatch.setenv("CARETAKER_COMFY_URL", "http://comfy.internal")  # no port
    assert comfy_mod.upstream_target() == ("comfy.internal", 8189)


def test_probe_and_upstream_may_split(monkeypatch):
    """CARETAKER_COMFY_INTERNAL_URL is the real forward target; the probe URL
    only drives health/activity checks. Split configs forward to the internal
    URL, not to the probe endpoint."""
    _env(monkeypatch)
    monkeypatch.setenv("CARETAKER_COMFY_URL", "http://probe.internal:9000")
    monkeypatch.setenv("CARETAKER_COMFY_INTERNAL_URL", "http://127.0.0.1:8189")
    assert comfy_mod.upstream_target() == ("127.0.0.1", 8189)
    assert comfy_mod.comfy_url() == "http://probe.internal:9000"
    # default single-URL setup: the probe IS the upstream
    monkeypatch.delenv("CARETAKER_COMFY_INTERNAL_URL")
    assert comfy_mod.upstream_target() == ("probe.internal", 9000)


# --- VRAM gate + connection slots (the cross-workload GPU arbiter, fase 1) ---


@pytest.mark.asyncio
async def test_gate_off_by_default_never_probes(monkeypatch, proxy_listener, allow_test_listener):
    """Gate-off is zero-change behavior: no VRAM probing, pure wake+forward."""
    _env(monkeypatch)  # no VRAM keys -> gate off
    probes = []

    async def bomb():
        probes.append(1)
        return 999999

    monkeypatch.setattr(comfy_mod, "_probe_free_vram_mb", bomb)

    async def snap():
        return {"queue_running": [], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)

    async def echo(reader, writer):
        data = await reader.read(100)
        writer.write(data.upper())
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    allow_test_listener(upstream)
    monkeypatch.setenv("CARETAKER_COMFY_URL", f"http://127.0.0.1:{upstream.sockets[0].getsockname()[1]}")
    port = await proxy_listener()

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"ping")
    await writer.drain()
    assert await asyncio.wait_for(reader.read(100), timeout=3) == b"PING"
    writer.close()
    upstream.close()
    assert probes == []  # the gate never even looked at VRAM


@pytest.mark.asyncio
async def test_slot_serializes_concurrent_connections(monkeypatch, proxy_listener, allow_test_listener):
    """MAX_CONCURRENT=1: the second client waits for the slot until the first
    connection closes — requests wait on each other by design."""
    _env(monkeypatch, CARETAKER_COMFY_MAX_CONCURRENT="1")

    async def snap():
        return {"queue_running": [], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)
    hold = asyncio.Event()

    async def hold_echo(reader, writer):
        data = await reader.read(100)
        await hold.wait()
        writer.write(data.upper())
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(hold_echo, "127.0.0.1", 0)
    allow_test_listener(upstream)
    monkeypatch.setenv("CARETAKER_COMFY_URL", f"http://127.0.0.1:{upstream.sockets[0].getsockname()[1]}")
    port = await proxy_listener()

    a_reader, a_writer = await asyncio.open_connection("127.0.0.1", port)
    a_writer.write(b"aa")
    await a_writer.drain()
    await asyncio.sleep(0.3)  # let A take the slot and reach the held upstream

    b_reader, b_writer = await asyncio.open_connection("127.0.0.1", port)
    b_writer.write(b"bb")
    await b_writer.drain()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(b_reader.read(100), timeout=0.5)  # B is queued, not served

    hold.set()  # upstream releases A
    assert await asyncio.wait_for(a_reader.read(100), timeout=3) == b"AA"
    a_writer.close()
    assert await asyncio.wait_for(b_reader.read(100), timeout=5) == b"BB"  # B proceeds after release
    b_writer.close()
    upstream.close()


@pytest.mark.asyncio
async def test_slot_timeout_closes_with_503(monkeypatch, proxy_listener, allow_test_listener):
    """A client that waits past CARETAKER_COMFY_SLOT_WAIT_TIMEOUT gets a
    synthetic 503 with a JSON error body, never a hang or fake success."""
    _env(monkeypatch, CARETAKER_COMFY_MAX_CONCURRENT="1", CARETAKER_COMFY_SLOT_WAIT_TIMEOUT="1")

    async def snap():
        return {"queue_running": [], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)

    async def black_hole(reader, writer):
        await asyncio.sleep(60)

    upstream = await asyncio.start_server(black_hole, "127.0.0.1", 0)
    allow_test_listener(upstream)
    monkeypatch.setenv("CARETAKER_COMFY_URL", f"http://127.0.0.1:{upstream.sockets[0].getsockname()[1]}")
    port = await proxy_listener()

    _a_reader, a_writer = await asyncio.open_connection("127.0.0.1", port)
    a_writer.write(b"aa")
    await a_writer.drain()
    await asyncio.sleep(0.3)  # A owns the only slot, upstream never answers

    b_reader, b_writer = await asyncio.open_connection("127.0.0.1", port)
    body = await asyncio.wait_for(b_reader.read(4096), timeout=6)
    assert b"503 Service Unavailable" in body
    assert b"comfy_slot_timeout" in body
    b_writer.close()
    a_writer.close()
    upstream.close()


@pytest.mark.asyncio
async def test_vram_gate_waits_frees_then_forwards(monkeypatch, proxy_listener, allow_test_listener):
    """wait-policy: below-threshold probe -> /free nudge -> re-probe passes ->
    connection forwarded. Both probes and the nudge are asserted."""
    _env(monkeypatch, CARETAKER_COMFY_VRAM_MIN_FREE_MB="1000", CARETAKER_COMFY_VRAM_WAIT_SECONDS="30")

    async def snap():
        return {"queue_running": [], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)

    probe_results = [500, 5000]  # first below threshold, then sufficient

    async def fake_probe():
        return probe_results.pop(0) if probe_results else 5000

    monkeypatch.setattr(comfy_mod, "_probe_free_vram_mb", fake_probe)

    free_posts = []

    def transport_handler(request):
        free_posts.append(request.url.path)
        return httpx.Response(200, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        comfy_mod.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(transport_handler), **kwargs),
    )
    async def echo(reader, writer):
        data = await reader.read(100)
        writer.write(data.upper())
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    allow_test_listener(upstream)
    monkeypatch.setenv("CARETAKER_COMFY_URL", f"http://127.0.0.1:{upstream.sockets[0].getsockname()[1]}")
    port = await proxy_listener()

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"ping")
    await writer.drain()
    assert await asyncio.wait_for(reader.read(100), timeout=10) == b"PING"
    writer.close()
    upstream.close()
    assert free_posts == ["/free"]  # exactly one nudge before the re-probe passed
    assert len(probe_results) == 0  # both probes ran


@pytest.mark.asyncio
async def test_vram_gate_fail_policy_rejects_without_free(monkeypatch, proxy_listener, allow_test_listener):
    """fail-policy: below-threshold probe rejects immediately with the VRAM 503
    body; Comfy's /free is never nudged."""
    _env(monkeypatch, CARETAKER_COMFY_VRAM_MIN_FREE_MB="1000", CARETAKER_COMFY_VRAM_POLICY="fail")

    async def snap():
        return {"queue_running": [], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)

    async def fake_probe():
        return 100

    monkeypatch.setattr(comfy_mod, "_probe_free_vram_mb", fake_probe)

    free_posts = []

    def transport_handler(request):
        free_posts.append(request.url.path)
        return httpx.Response(200, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        comfy_mod.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(transport_handler), **kwargs),
    )
    port = await proxy_listener()

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    body = await asyncio.wait_for(reader.read(4096), timeout=5)
    assert b"comfy_vram_unavailable" in body
    assert b'"free_mb": 100' in body
    assert b'"required_mb": 1000' in body
    writer.close()
    assert free_posts == []  # fail-policy never nudges


@pytest.mark.asyncio
async def test_vram_probe_failure_fails_open(monkeypatch, proxy_listener, allow_test_listener):
    """A broken nvidia-smi must never brick the proxy: probe raises -> the
    connection passes (fail-open) with a warning."""
    _env(monkeypatch, CARETAKER_COMFY_VRAM_MIN_FREE_MB="1000")

    async def snap():
        return {"queue_running": [], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)
    probes = []

    async def broken_probe():
        probes.append(1)
        raise RuntimeError("nvidia-smi exploded")

    monkeypatch.setattr(comfy_mod, "_probe_free_vram_mb", broken_probe)

    async def echo(reader, writer):
        data = await reader.read(100)
        writer.write(data.upper())
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    allow_test_listener(upstream)
    monkeypatch.setenv("CARETAKER_COMFY_URL", f"http://127.0.0.1:{upstream.sockets[0].getsockname()[1]}")
    port = await proxy_listener()

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"ping")
    await writer.drain()
    assert await asyncio.wait_for(reader.read(100), timeout=3) == b"PING"
    writer.close()
    upstream.close()
    assert probes == [1]  # probed exactly once, then failed open


def test_invalid_concurrency_and_budgets_clamp(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("CARETAKER_COMFY_MAX_CONCURRENT", "0")
    monkeypatch.setenv("CARETAKER_COMFY_SLOT_WAIT_TIMEOUT", "soon")
    monkeypatch.setenv("CARETAKER_COMFY_VRAM_MIN_FREE_MB", "-5")
    monkeypatch.setenv("CARETAKER_COMFY_VRAM_WAIT_SECONDS", "later")
    assert comfy_mod._max_concurrent() == 1  # 0 clamps to 1
    assert comfy_mod._slot_wait_timeout() == 300  # garbage -> default
    assert comfy_mod._vram_min_free_mb() == -5  # negative reads as gate-off (<= 0)
    assert comfy_mod._vram_wait_seconds() == 120


@pytest.mark.asyncio
async def test_status_reports_gate_keys(monkeypatch):
    _env(monkeypatch, CARETAKER_COMFY_VRAM_MIN_FREE_MB="2500", CARETAKER_COMFY_VRAM_POLICY="fail",
         CARETAKER_COMFY_MAX_CONCURRENT="2")

    def offline(request):
        raise httpx.ConnectError("offline", request=request)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        comfy_mod.httpx, "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(offline), **kwargs),
    )
    status = await comfy_mod.astatus()
    assert status["vram_gate_enabled"] is True
    assert status["vram_policy"] == "fail"
    assert status["max_concurrent"] == 2
    assert status["slot_active"] == 0
    assert status["slot_waiting"] == 0
    assert status["last_free_mb"] is None


@pytest.mark.asyncio
async def test_gate_runs_inside_slot_not_per_waiter(monkeypatch, proxy_listener, allow_test_listener):
    """The gate probes only after a connection HOLDS its slot: a queued client
    must not spin probe/nudge loops while waiting (finding: the /free herd)."""
    _env(monkeypatch, CARETAKER_COMFY_MAX_CONCURRENT="1", CARETAKER_COMFY_VRAM_MIN_FREE_MB="1000")

    async def snap():
        return {"queue_running": [], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)

    async def fake_probe():
        return 5000  # sufficient — the gate passes immediately when it runs

    monkeypatch.setattr(comfy_mod, "_probe_free_vram_mb", fake_probe)

    hold = asyncio.Event()

    async def hold_echo(reader, writer):
        data = await reader.read(100)
        await hold.wait()
        writer.write(data.upper())
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(hold_echo, "127.0.0.1", 0)
    allow_test_listener(upstream)
    monkeypatch.setenv("CARETAKER_COMFY_URL", f"http://127.0.0.1:{upstream.sockets[0].getsockname()[1]}")
    port = await proxy_listener()

    a_reader, a_writer = await asyncio.open_connection("127.0.0.1", port)
    a_writer.write(b"aa")
    await a_writer.drain()
    await asyncio.sleep(0.3)  # A owns the slot and reaches the held upstream

    b_reader, b_writer = await asyncio.open_connection("127.0.0.1", port)
    b_writer.write(b"bb")
    await b_writer.drain()
    await asyncio.sleep(0.3)
    assert comfy_mod._slot_waiting == 1  # B is queued on the slot...

    hold.set()
    assert await asyncio.wait_for(a_reader.read(100), timeout=3) == b"AA"
    a_writer.close()
    # Only after A releases may B enter the gate at all.
    assert await asyncio.wait_for(b_reader.read(100), timeout=5) == b"BB"
    b_writer.close()
    upstream.close()
    await asyncio.sleep(0.1)  # let the handler's finally run before asserting
    assert comfy_mod._slot_active == 0  # released, no permit leak


@pytest.mark.asyncio
async def test_gate_lock_serializes_probe_loops(monkeypatch, proxy_listener, allow_test_listener):
    """Two concurrent gate entrants share ONE serialized probe sequence: the
    second waits for the lock instead of running its own loop (3 probes, not
    4 — the exact /free-herd shape PR-Piet flagged)."""
    _env(
        monkeypatch,
        CARETAKER_COMFY_MAX_CONCURRENT="2",
        CARETAKER_COMFY_VRAM_MIN_FREE_MB="1000",
        CARETAKER_COMFY_VRAM_WAIT_SECONDS="30",
    )

    async def snap():
        return {"queue_running": [], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)

    # Serialized with the gate lock: conn A consumes [500 (fail), 5000 (pass)],
    # conn B then gets 5000 (pass). Without the lock both would interleave and
    # drain 4 probes.
    probe_results = [500, 5000, 5000]
    probe_calls = []

    async def fake_probe():
        probe_calls.append(1)
        return probe_results.pop(0) if probe_results else 5000

    monkeypatch.setattr(comfy_mod, "_probe_free_vram_mb", fake_probe)

    free_posts = []

    def transport_handler(request):
        free_posts.append(request.url.path)
        return httpx.Response(200, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        comfy_mod.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(transport_handler), **kwargs),
    )
    async def echo(reader, writer):
        data = await reader.read(100)
        writer.write(data.upper())
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    allow_test_listener(upstream)
    monkeypatch.setenv("CARETAKER_COMFY_URL", f"http://127.0.0.1:{upstream.sockets[0].getsockname()[1]}")
    port = await proxy_listener()

    readers = []
    for _ in range(2):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        readers.append((r, w))
        w.write(b"ping")
        await w.drain()
    for r, w in readers:
        assert await asyncio.wait_for(r.read(100), timeout=10) == b"PING"
        w.close()
    upstream.close()
    assert len(probe_calls) == 3  # serialized sequence, no interleave inflation
    assert free_posts == ["/free"]  # exactly one nudge


@pytest.mark.asyncio
async def test_semaphore_not_rebuilt_while_waiters_pending(monkeypatch):
    """Rebuild waits for BOTH zero active and zero waiting: queued clients
    must never be stranded on a stale semaphore instance."""
    _env(monkeypatch, CARETAKER_COMFY_MAX_CONCURRENT="2")
    sem = comfy_mod._get_slot_semaphore()
    comfy_mod._slot_active = 1
    comfy_mod._slot_waiting = 1
    monkeypatch.setenv("CARETAKER_COMFY_MAX_CONCURRENT", "5")
    assert comfy_mod._get_slot_semaphore() is sem  # unchanged while occupied
    comfy_mod._slot_active = 0
    assert comfy_mod._get_slot_semaphore() is sem  # still: waiter pending
    comfy_mod._slot_waiting = 0
    assert comfy_mod._get_slot_semaphore() is not sem  # idle now: rebuilt at 5


@pytest.mark.asyncio
async def test_gate_never_frees_while_job_running(monkeypatch, proxy_listener, allow_test_listener):
    """The /free nudge is skipped while Comfy reports a running job: /free can
    unload models mid-generation (PR-Piet ronde 3). The gate just keeps
    waiting/re-probing until the budget runs out instead."""
    _env(monkeypatch, CARETAKER_COMFY_VRAM_MIN_FREE_MB="1000", CARETAKER_COMFY_VRAM_WAIT_SECONDS="30")

    async def busy_snap():
        return {"queue_running": [1], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", busy_snap)

    probe_results = [500, 5000]  # low -> (guard skips nudge) -> re-probe passes

    async def fake_probe():
        return probe_results.pop(0) if probe_results else 5000

    monkeypatch.setattr(comfy_mod, "_probe_free_vram_mb", fake_probe)

    free_posts = []

    def transport_handler(request):
        free_posts.append(request.url.path)
        return httpx.Response(200, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        comfy_mod.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(transport_handler), **kwargs),
    )
    async def echo(reader, writer):
        data = await reader.read(100)
        writer.write(data.upper())
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    allow_test_listener(upstream)
    monkeypatch.setenv("CARETAKER_COMFY_URL", f"http://127.0.0.1:{upstream.sockets[0].getsockname()[1]}")
    port = await proxy_listener()

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"ping")
    await writer.drain()
    assert await asyncio.wait_for(reader.read(100), timeout=3) == b"PING"
    writer.close()
    upstream.close()
    assert free_posts == []  # busy queue -> the nudge never fires


@pytest.mark.asyncio
async def test_503_delivered_despite_pipelined_request(monkeypatch, proxy_listener, allow_test_listener):
    """A client that pipelines its HTTP request right after connect must still
    receive the 503: the proxy drains inbound bytes before closing, otherwise
    the TCP RST discards the response (PR-Piet ronde 4)."""
    _env(monkeypatch, CARETAKER_COMFY_VRAM_MIN_FREE_MB="1000", CARETAKER_COMFY_VRAM_POLICY="fail")

    async def snap():
        return {"queue_running": [], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)

    async def fake_probe():
        return 100

    monkeypatch.setattr(comfy_mod, "_probe_free_vram_mb", fake_probe)
    port = await proxy_listener()

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"POST /prompt HTTP/1.1\r\nHost: proxy\r\nContent-Length: 2\r\n\r\n{}")
    await writer.drain()
    body = await asyncio.wait_for(reader.read(4096), timeout=6)
    assert b"503 Service Unavailable" in body
    assert b"comfy_vram_unavailable" in body
    writer.close()


@pytest.mark.asyncio
async def test_queued_connection_not_closed_by_conn_idle(monkeypatch, proxy_listener, allow_test_listener):
    """The per-connection silence budget lives in _pipe, which exists only
    after the permit: a QUEUED connection is untouched by conn_idle, so with
    SLOT_WAIT=0 (indefinite) and CONN_IDLE=1 — the racing config PR-Piet
    named — the queued client is neither stranded nor silently closed; it is
    served when a permit frees (refutation pinned with code)."""
    _env(
        monkeypatch,
        CARETAKER_COMFY_MAX_CONCURRENT="1",
        CARETAKER_COMFY_SLOT_WAIT_TIMEOUT="0",
        CARETAKER_COMFY_CONN_IDLE_TIMEOUT="1",
    )

    async def snap():
        return {"queue_running": [], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)
    hold = asyncio.Event()

    async def hold_echo(reader, writer):
        data = await reader.read(100)
        # Like a live Comfy job: stream progress bytes while the work runs,
        # or conn_idle would rightly close the (half-)silent connection — the
        # interaction under test is B's queued window, not A's.
        while not hold.is_set():
            writer.write(b".")
            await writer.drain()
            await asyncio.sleep(0.4)
        writer.write(data.upper())
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(hold_echo, "127.0.0.1", 0)
    allow_test_listener(upstream)
    monkeypatch.setenv("CARETAKER_COMFY_URL", f"http://127.0.0.1:{upstream.sockets[0].getsockname()[1]}")
    port = await proxy_listener()

    a_reader, a_writer = await asyncio.open_connection("127.0.0.1", port)
    a_writer.write(b"aa")
    await a_writer.drain()
    await asyncio.sleep(0.3)  # A owns the slot, upstream held

    # A behaves like a live websocket job: it keeps sending input bytes,
    # otherwise conn_idle would rightly close ITS half-silent connection
    # (and free the slot) — under test is B's queued window, not A's.
    stop = asyncio.Event()

    async def keepalive(writer: asyncio.StreamWriter):
        while not stop.is_set():
            try:
                writer.write(b"x")
                await writer.drain()
            except Exception:  # noqa: BLE001 — any transport error ends the job
                return
            await asyncio.sleep(0.4)

    ka = asyncio.create_task(keepalive(a_writer))

    b_reader, b_writer = await asyncio.open_connection("127.0.0.1", port)
    b_writer.write(b"bb")
    await b_writer.drain()
    await asyncio.sleep(2.5)  # 2.5x conn_idle — the named silent close window
    assert comfy_mod._slot_waiting == 1  # B is STILL queued, not silently closed
    assert not b_writer.is_closing()

    stop.set()
    await asyncio.gather(ka, return_exceptions=True)
    hold.set()
    a_buf = b""
    while b"AA" not in a_buf:  # progress dots may arrive before the reply
        a_buf += await asyncio.wait_for(a_reader.read(100), timeout=3)
    a_writer.close()
    await asyncio.sleep(0.1)  # let A's handler finally release the permit
    assert await asyncio.wait_for(b_reader.read(100), timeout=5) == b"BB"
    b_writer.close()
    upstream.close()


@pytest.mark.asyncio
async def test_idle_permit_drift_rebuilds_semaphore(monkeypatch):
    """Permit accounting is verified at the idle boundary: a normal release
    keeps the semaphore, but free-permit drift at zero holders (phantom
    consumption on pre-3.12.4 asyncio, or an over-release) drops it for a
    clean rebuild. asyncio.Semaphore never raises on over-release, so this
    check — not a ValueError guard — is what can actually detect the race."""
    _env(monkeypatch, CARETAKER_COMFY_MAX_CONCURRENT="1")
    sem = comfy_mod._get_slot_semaphore()
    await sem.acquire()  # a real holder, so the release below is a normal one
    comfy_mod._slot_active = 1
    comfy_mod._release_slot(sem)  # normal release: value == size, kept
    assert comfy_mod._slot_active == 0
    assert comfy_mod._slot_semaphore is sem
    sem.release()  # simulate the phantom-permit shape (value inflates)
    comfy_mod._slot_active = 1
    comfy_mod._release_slot(sem)  # idle boundary: value != size
    assert comfy_mod._slot_semaphore is None  # drift -> dropped for rebuild
    fresh = comfy_mod._get_slot_semaphore()
    assert fresh is not sem


@pytest.mark.asyncio
async def test_drift_check_degrades_without_private_value(monkeypatch):
    """`sem._value` is private asyncio state: if a future CPython drops it,
    the drift check degrades to disabled — the release path must never crash
    on a missing private attribute."""
    _env(monkeypatch, CARETAKER_COMFY_MAX_CONCURRENT="1")

    class ValuelessSemaphore:
        def __init__(self):
            self._permits = 1

        async def acquire(self):
            assert self._permits > 0
            self._permits -= 1

        def release(self):
            self._permits += 1

    comfy_mod._slot_semaphore_size = 1
    sem = ValuelessSemaphore()
    comfy_mod._slot_semaphore = sem  # installed as the live instance
    await sem.acquire()
    comfy_mod._slot_active = 1
    comfy_mod._release_slot(sem)  # no _value attribute -> no crash, no rebuild
    assert comfy_mod._slot_active == 0
    assert comfy_mod._slot_semaphore is sem  # kept as-is (check degraded off)


# --- evict policy (fase 2: the cross-workload GPU arbiter) ---


@pytest.mark.asyncio
async def test_evict_policy_unloads_llm_after_nudge_fails(monkeypatch, proxy_listener, allow_test_listener):
    """evict-policy: VRAM short -> one /free nudge (comfy can release its own
    cache) -> VRAM STILL short -> the LLM hook fires and the connection
    proceeds once the probe passes. Hook and nudge both asserted."""
    _env(monkeypatch, CARETAKER_COMFY_VRAM_MIN_FREE_MB="1000", CARETAKER_COMFY_VRAM_POLICY="evict")
    monkeypatch.delenv("CARETAKER_COMFY_EVICT_MIN_TENURE_S", raising=False)

    async def snap():
        return {"queue_running": [], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)

    # probe sequence: low (nudge path) -> low (evict path) -> sufficient
    probe_results = [500, 500, 5000]

    async def fake_probe():
        return probe_results.pop(0) if probe_results else 5000

    monkeypatch.setattr(comfy_mod, "_probe_free_vram_mb", fake_probe)

    evictions = []

    async def fake_evict():
        evictions.append(1)

    comfy_mod.set_llama_evict_hook(fake_evict)
    free_posts = []

    def transport_handler(request):
        free_posts.append(request.url.path)
        return httpx.Response(200, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        comfy_mod.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(transport_handler), **kwargs),
    )
    try:
        async def echo(reader, writer):
            data = await reader.read(100)
            writer.write(data.upper())
            await writer.drain()
            writer.close()

        upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        allow_test_listener(upstream)
        monkeypatch.setenv("CARETAKER_COMFY_URL", f"http://127.0.0.1:{upstream.sockets[0].getsockname()[1]}")
        port = await proxy_listener()

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"ping")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(100), timeout=10) == b"PING"
        writer.close()
        upstream.close()
    finally:
        comfy_mod.set_llama_evict_hook(None)
    assert free_posts == ["/free"]  # nudge first...
    assert evictions == [1]  # ...then the hook, exactly once
    assert comfy_mod._evict_count == 1


@pytest.mark.asyncio
async def test_evict_tenure_blocks_repeat_unloads(monkeypatch):
    """Anti-flap: within CARETAKER_COMFY_EVICT_MIN_TENURE_S the hook must not
    fire again — the LLM stays, the job keeps waiting (wait rhythm)."""
    _env(
        monkeypatch,
        CARETAKER_COMFY_VRAM_MIN_FREE_MB="1000",
        CARETAKER_COMFY_VRAM_POLICY="evict",
        CARETAKER_COMFY_EVICT_MIN_TENURE_S="600",
        CARETAKER_COMFY_VRAM_WAIT_SECONDS="5",
    )

    async def snap():
        return {"queue_running": [], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)

    async def low_probe():
        return 500  # always short

    monkeypatch.setattr(comfy_mod, "_probe_free_vram_mb", low_probe)

    free_posts = []

    def transport_handler(request):
        free_posts.append(request.url.path)
        return httpx.Response(200, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        comfy_mod.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(transport_handler), **kwargs),
    )
    evictions = []

    async def ok_evict():
        evictions.append(1)

    comfy_mod.set_llama_evict_hook(ok_evict)
    try:
        comfy_mod._last_evict_monotonic = time.monotonic()  # evicted 0 s ago
        assert await comfy_mod._vram_gate() is not None  # budget burns out waiting
        assert evictions == []  # tenure blocked the second unload
    finally:
        comfy_mod.set_llama_evict_hook(None)
        comfy_mod._last_evict_monotonic = None


@pytest.mark.asyncio
async def test_evict_without_hook_degrades_to_wait(monkeypatch):
    """No hook installed (broken models config, test double): the evict policy
    must degrade to plain wait — never crash the proxy path."""
    _env(monkeypatch, CARETAKER_COMFY_VRAM_MIN_FREE_MB="1000", CARETAKER_COMFY_VRAM_POLICY="evict",
         CARETAKER_COMFY_VRAM_WAIT_SECONDS="5")
    comfy_mod.set_llama_evict_hook(None)

    async def snap():
        return {"queue_running": [], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)

    async def low_probe():
        return 500

    monkeypatch.setattr(comfy_mod, "_probe_free_vram_mb", low_probe)
    free_posts = []

    def transport_handler(request):
        free_posts.append(request.url.path)
        return httpx.Response(200, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        comfy_mod.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(transport_handler), **kwargs),
    )
    assert await comfy_mod._vram_gate() is not None  # wait-shaped budget timeout


@pytest.mark.asyncio
async def test_evict_hook_failure_degrades_to_wait(monkeypatch):
    """A raising hook must not crash the gate: it degrades to wait for the
    rest of this connection's gate pass."""
    _env(monkeypatch, CARETAKER_COMFY_VRAM_MIN_FREE_MB="1000", CARETAKER_COMFY_VRAM_POLICY="evict",
         CARETAKER_COMFY_VRAM_WAIT_SECONDS="5")
    monkeypatch.delenv("CARETAKER_COMFY_EVICT_MIN_TENURE_S", raising=False)

    async def snap():
        return {"queue_running": [], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)

    async def low_probe():
        return 500

    monkeypatch.setattr(comfy_mod, "_probe_free_vram_mb", low_probe)
    free_posts = []

    def transport_handler(request):
        free_posts.append(request.url.path)
        return httpx.Response(200, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        comfy_mod.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(transport_handler), **kwargs),
    )

    async def bomb():
        raise RuntimeError("manager exploded")

    comfy_mod.set_llama_evict_hook(bomb)
    try:
        assert await comfy_mod._vram_gate() is not None  # waited out, no crash
    finally:
        comfy_mod.set_llama_evict_hook(None)
    assert comfy_mod._evict_count == 0


@pytest.mark.asyncio
async def test_failed_free_nudge_never_escalates_to_evict(monkeypatch):
    """A /free that fails released nothing, so evict-policy must retry the cheap
    path instead of unloading the LLM on a nudge that never happened
    (PR-Piet finding, comfy.py:591)."""
    _env(monkeypatch, CARETAKER_COMFY_VRAM_MIN_FREE_MB="1000", CARETAKER_COMFY_VRAM_POLICY="evict",
         CARETAKER_COMFY_VRAM_WAIT_SECONDS="5")
    monkeypatch.delenv("CARETAKER_COMFY_EVICT_MIN_TENURE_S", raising=False)

    async def snap():
        return {"queue_running": [], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)

    async def low_probe():
        return 500

    monkeypatch.setattr(comfy_mod, "_probe_free_vram_mb", low_probe)

    free_posts = []

    def failing_handler(request):
        free_posts.append(request.url.path)
        return httpx.Response(500, json={"error": "nudge failed"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        comfy_mod.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(failing_handler), **kwargs),
    )

    evictions = []

    async def fake_evict():
        evictions.append(1)

    comfy_mod.set_llama_evict_hook(fake_evict)
    try:
        assert await comfy_mod._vram_gate() is not None  # budget burns out -> 503
    finally:
        comfy_mod.set_llama_evict_hook(None)
    assert evictions == []  # the LLM was never unloaded on a failed nudge
    assert comfy_mod._evict_count == 0
    assert len(free_posts) >= 2  # the nudge was retried, not treated as done


@pytest.mark.asyncio
async def test_busy_job_rearms_the_nudge_before_evict(monkeypatch):
    """A comfy job that ran during the wait may have (re)loaded models and
    undone the earlier /free, so the cheap nudge must run again before the LLM
    is evicted (PR-Piet finding, comfy.py:547)."""
    _env(monkeypatch, CARETAKER_COMFY_VRAM_MIN_FREE_MB="1000", CARETAKER_COMFY_VRAM_POLICY="evict",
         CARETAKER_COMFY_VRAM_WAIT_SECONDS="7")
    monkeypatch.delenv("CARETAKER_COMFY_EVICT_MIN_TENURE_S", raising=False)

    # idle (nudge) -> a job starts -> idle again (must nudge before evicting)
    snapshots = [
        {"queue_running": [], "queue_pending": []},
        {"queue_running": [{"id": 1}], "queue_pending": []},
    ]

    async def snap():
        return snapshots.pop(0) if snapshots else {"queue_running": [], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)

    async def low_probe():
        return 500

    monkeypatch.setattr(comfy_mod, "_probe_free_vram_mb", low_probe)

    events = []

    def transport_handler(request):
        events.append("free")
        return httpx.Response(200, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        comfy_mod.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(transport_handler), **kwargs),
    )

    async def fake_evict():
        events.append("evict")

    comfy_mod.set_llama_evict_hook(fake_evict)
    try:
        assert await comfy_mod._vram_gate() is not None  # budget burns out -> 503
    finally:
        comfy_mod.set_llama_evict_hook(None)
    assert events == ["free", "free", "evict"]  # the post-busy pass nudged again


# --- evict-hook deadline bounding (PR #16 review: unbounded eviction) ---


def _short_vram_env(monkeypatch, wait_seconds: int = 3) -> None:
    """VRAM short + ``evict`` policy + a tight gate budget, no sockets."""
    _env(
        monkeypatch,
        CARETAKER_COMFY_VRAM_MIN_FREE_MB="1000",
        CARETAKER_COMFY_VRAM_POLICY="evict",
        CARETAKER_COMFY_VRAM_WAIT_SECONDS=str(wait_seconds),
    )
    monkeypatch.delenv("CARETAKER_COMFY_EVICT_MIN_TENURE_S", raising=False)

    async def snap():
        return {"queue_running": [], "queue_pending": []}

    monkeypatch.setattr(comfy_mod, "_queue_snapshot", snap)

    async def low_probe():
        return 500  # VRAM never recovers on its own

    monkeypatch.setattr(comfy_mod, "_probe_free_vram_mb", low_probe)

    def transport_handler(request):
        return httpx.Response(200, json={})  # the /free nudge "succeeds"

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        comfy_mod.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(transport_handler), **kwargs),
    )


def _install_stalled_evict_hook() -> tuple[asyncio.Event, asyncio.Event]:
    """Install a hook that never returns on its own (a wedged unload)."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def stalled_evict():
        started.set()
        await release.wait()

    comfy_mod.set_llama_evict_hook(stalled_evict)
    return started, release


async def _drop_stalled_evict_hook() -> None:
    """Release/cancel the test hook and clear the module's in-flight ref."""
    comfy_mod.set_llama_evict_hook(None)
    task = comfy_mod._evict_task
    comfy_mod._evict_task = None
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


@pytest.mark.asyncio
async def test_stalled_evict_hook_is_bounded_by_the_wait_budget(monkeypatch):
    """A hook that never returns must not hold the gate past its budget.

    Regression for the PR #16 review finding (comfy.py:577): the hook await was
    unbounded, so a stalled unload kept ``_vram_gate_lock`` forever and no
    client ever got the configured 503.
    """
    budget = 3
    _short_vram_env(monkeypatch, budget)
    started, _release = _install_stalled_evict_hook()
    try:
        t0 = time.monotonic()
        gate = await asyncio.wait_for(comfy_mod._vram_gate(), timeout=budget + 5)
        elapsed = time.monotonic() - t0
        assert started.is_set()  # the hook really was awaited, not skipped
        assert gate is not None
        assert gate["error"] == "comfy_vram_unavailable"
        assert elapsed < budget + 3, f"gate answered after {elapsed:.1f}s (budget {budget}s)"
        # The eviction never completed: bookkeeping must not claim one.
        assert comfy_mod._evict_count == 0
        assert comfy_mod._last_evict_monotonic is None
    finally:
        await _drop_stalled_evict_hook()


@pytest.mark.asyncio
async def test_stalled_evict_hook_does_not_block_the_next_connection(monkeypatch):
    """A second gate call behind a stalled eviction must still answer.

    On the unfixed head the first caller blocks inside the hook forever, so the
    second caller blocks on ``_vram_gate_lock`` forever as well.
    """
    budget = 3
    _short_vram_env(monkeypatch, budget)
    started, _release = _install_stalled_evict_hook()
    second: asyncio.Task | None = None
    try:
        t0 = time.monotonic()
        first = asyncio.create_task(comfy_mod._vram_gate())
        await asyncio.wait_for(started.wait(), timeout=budget + 5)  # hook in flight
        second = asyncio.create_task(comfy_mod._vram_gate())
        first_gate = await asyncio.wait_for(first, timeout=budget + 5)
        first_elapsed = time.monotonic() - t0
        second_gate = await asyncio.wait_for(second, timeout=budget + 10)
        second_elapsed = time.monotonic() - t0
        assert first_gate is not None and second_gate is not None
        assert first_gate["error"] == "comfy_vram_unavailable"
        assert second_gate["error"] == "comfy_vram_unavailable"
        assert first_elapsed < budget + 3, f"first gate {first_elapsed:.1f}s (budget {budget}s)"
        # The second caller queues behind the first one's budget, never behind
        # the stalled hook: at most ~2 budgets, not "forever".
        assert second_elapsed < (2 * budget) + 6, f"second gate {second_elapsed:.1f}s"
        assert not _release.is_set()  # the hook never finished during the test
        assert comfy_mod._evict_count == 0
        assert comfy_mod._last_evict_monotonic is None
        # The abandoned hook is detached (still pending), never stacked twice.
        assert comfy_mod._evict_task is not None
        assert not comfy_mod._evict_task.done()
    finally:
        if second is not None and not second.done():
            second.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await second
        await _drop_stalled_evict_hook()


@pytest.mark.asyncio
async def test_eviction_completing_after_the_budget_is_recorded_once(monkeypatch):
    """A late unload is recorded when it *lands*, never when it was requested.

    The gate answers its 503 at the budget and leaves the detached hook to
    finish; ``evict_count``/``last_evict_epoch`` only move on completion, so the
    anti-flap tenure starts from the real completion and no failed/abandoned
    attempt is ever reported as an eviction.
    """
    budget = 3
    _short_vram_env(monkeypatch, budget)
    unloaded = asyncio.Event()

    async def slow_evict():
        await asyncio.sleep(2.5)  # outlives the remaining gate budget
        unloaded.set()

    comfy_mod.set_llama_evict_hook(slow_evict)
    try:
        t0 = time.monotonic()
        gate = await asyncio.wait_for(comfy_mod._vram_gate(), timeout=budget + 5)
        elapsed = time.monotonic() - t0
        assert gate is not None and gate["error"] == "comfy_vram_unavailable"
        assert elapsed < budget + 3, f"gate answered after {elapsed:.1f}s (budget {budget}s)"
        assert comfy_mod._evict_count == 0  # not claimed while still unloading
        task = comfy_mod._evict_task
        assert task is not None and not task.done()
        await asyncio.wait_for(task, timeout=5)
        assert unloaded.is_set()
        assert comfy_mod._evict_count == 1  # recorded exactly once, on completion
        assert comfy_mod._last_evict_monotonic is not None
    finally:
        await _drop_stalled_evict_hook()
