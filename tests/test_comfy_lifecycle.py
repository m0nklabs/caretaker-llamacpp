"""Comfy lifecycle pins — idle-stop + wake proxy (the operator's VRAM spec:
GPU services stop after 5 min of non-use and the caretaker restarts them on
the next request)."""

import asyncio
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
    real_sleep = asyncio.sleep

    async def instant_sleep(delay):
        return None

    comfy_mod.asyncio.sleep = instant_sleep
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
        assert await asyncio.wait_for(reader.read(100), timeout=3) == b"PING"
        writer.close()
        upstream.close()
    finally:
        comfy_mod.asyncio.sleep = real_sleep
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
    real_sleep = asyncio.sleep

    async def instant_sleep(delay):
        return None

    comfy_mod.asyncio.sleep = instant_sleep
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

        readers = []
        for _ in range(2):
            r, w = await asyncio.open_connection("127.0.0.1", port)
            readers.append((r, w))
            w.write(b"ping")
            await w.drain()
        for r, w in readers:
            assert await asyncio.wait_for(r.read(100), timeout=5) == b"PING"
            w.close()
        upstream.close()
    finally:
        comfy_mod.asyncio.sleep = real_sleep
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
    real_sleep = asyncio.sleep

    async def instant_sleep(delay):
        return None

    comfy_mod.asyncio.sleep = instant_sleep
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
        assert await asyncio.wait_for(reader.read(100), timeout=3) == b"PING"
        writer.close()
        upstream.close()
    finally:
        comfy_mod.asyncio.sleep = real_sleep
    assert free_posts == []  # busy queue -> the nudge never fires
