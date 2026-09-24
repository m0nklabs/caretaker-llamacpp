"""ComfyUI lifecycle for caretaker: idle-stop + on-demand wake proxy.

The operator's spec (2026-09-23): GPU services shut down after 5 minutes of
non-use and are restarted by the caretaker on the next request — uniform
across hosts. ComfyUI on Windows runs as its own scheduled task, completely
outside the caretaker, holding VRAM around the clock.

Two mechanisms live here:

1. **Idle watcher** — poll ``{comfy_url}/queue`` every 30 s; when Comfy is up
   and both ``queue_running`` and ``queue_pending`` stay empty for
   ``CARETAKER_COMFY_IDLE_SECONDS`` (default 300; 0 = never stop), run
   ``CARETAKER_COMFY_STOP_COMMAND`` (e.g. ``schtasks /End /tn ComfyUIServer``).
   Any queued/executing work resets the timer.

2. **Wake proxy** — Comfy clients point at ``CARETAKER_COMFY_PROXY_PORT``
   (the public Comfy port); Comfy itself moves to the internal port. On an
   incoming connection the proxy runs ``CARETAKER_COMFY_START_COMMAND``
   (e.g. ``schtasks /Run /tn ComfyUIServer``), waits until the internal
   ``/queue`` answers (up to ``CARETAKER_COMFY_WAKE_TIMEOUT``, default 90 s —
   Comfy's python startup is slow), then pumps bytes both ways transparently
   (HTTP and the ``/ws`` websocket flow through the raw TCP pipe).

Config (env, mirrors the TTS/STT module idiom):

- ``CARETAKER_COMFY_IDLE_SECONDS``  queue-empty budget (default 300; 0 = never stop)
- ``CARETAKER_COMFY_URL``           internal probe URL (default from the
                                    settings yaml ``services.comfyui_url``)
- ``CARETAKER_COMFY_START_COMMAND`` shell command that starts Comfy
- ``CARETAKER_COMFY_STOP_COMMAND``  shell command that stops Comfy
- ``CARETAKER_COMFY_PROXY_PORT``    public listen port (default 0 = proxy off)
- ``CARETAKER_COMFY_PROXY_BIND``    listen interface (default 127.0.0.1 — the
                                    on-demand wake is local-only unless the
                                    operator explicitly exposes it; binding
                                    0.0.0.0 makes GPU-wake reachable LAN-wide
                                    without credentials, mirroring Comfy's own
                                    unauthenticated surface)
- ``CARETAKER_COMFY_INTERNAL_URL``  the real Comfy URL behind the proxy
- ``CARETAKER_COMFY_WAKE_TIMEOUT``  max seconds to wait for a cold start

Mirrors the TTS/STT module contract: ``init()`` from server.py, a 30 s
exception-proof watcher loop, and a ``status()`` for the control API.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx
from urllib.parse import urlparse

from . import config as config_mod

logger = logging.getLogger("caretaker.comfy")

_last_activity_monotonic: float | None = None
_watcher_task: asyncio.Task | None = None
_proxy_server: asyncio.AbstractServer | None = None
_start_lock = asyncio.Lock()
_enabled = False
# Open proxied connections count as activity: a client holding a session
# (Comfy web UI, a long-lived websocket) must not have its backend stopped
# underneath it just because the queue happens to be empty.
_active_connections = 0


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    """Parse an integer env value; fall back to the default on garbage so a
    misconfiguration can never abort the caretaker boot or wedge the watcher
    (the TTS/STT modules accept the same laxity via their own env reads)."""
    raw = _env(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        logger.warning("⚠️ %s=%r is not an integer — using %s", name, raw, default)
        return default


def comfy_url() -> str:
    """Internal probe URL: env override, else the settings-yaml comfyui_url."""
    override = _env("CARETAKER_COMFY_URL", "").strip()
    if override:
        return override.rstrip("/")
    try:
        return config_mod.comfyui_url().rstrip("/")
    except Exception:  # noqa: BLE001 — settings yaml absent/corrupt
        return _env("CARETAKER_COMFY_INTERNAL_URL", "http://127.0.0.1:8189").rstrip("/")


def comfy_enabled() -> bool:
    """Comfy lifecycle is available on every host; the commands decide whether
    stop/start can actually run (they are no-ops when unset)."""
    return _env("CARETAKER_COMFY_IDLE_SECONDS", "300") != "0" or _env(
        "CARETAKER_COMFY_PROXY_PORT", "0"
    ) not in ("", "0")


def _idle_seconds() -> int:
    return _env_int("CARETAKER_COMFY_IDLE_SECONDS", 300)


async def _queue_snapshot(timeout_s: float = 5.0) -> dict[str, Any] | None:
    """Return the /queue body, or None when Comfy is unreachable."""
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(f"{comfy_url()}/queue")
        if resp.status_code != 200:
            return None
        body = resp.json()
        if not isinstance(body, dict):
            return None
        return body
    except Exception:  # noqa: BLE001 — unreachable = not running
        return None


async def _shell(command: str, *, label: str) -> bool:
    """Run a lifecycle shell command; returns True on exit code 0."""
    if not command.strip():
        logger.warning("Comfy %s: no command configured", label)
        return False
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()
    if proc.returncode == 0:
        logger.info("✅ Comfy %s: %s", label, command)
        return True
    logger.warning(
        "⚠️ Comfy %s failed (rc=%s): %s", label, proc.returncode, stderr.decode(errors="replace")[:200]
    )
    return False


async def start_comfy() -> bool:
    """Start Comfy via the configured command; wait until /queue answers."""
    async with _start_lock:
        if await _queue_snapshot(timeout_s=3.0) is not None:
            return True  # already up (race with another waker)
        started = await _shell(_env("CARETAKER_COMFY_START_COMMAND", ""), label="start")
        if not started:
            return False
        deadline = time.monotonic() + float(_env_int("CARETAKER_COMFY_WAKE_TIMEOUT", 90))
        while time.monotonic() < deadline:
            if await _queue_snapshot(timeout_s=3.0) is not None:
                logger.info("✅ Comfy up after wake — serving")
                return True
            await asyncio.sleep(2.0)
        logger.warning("⚠️ Comfy did not answer within the wake timeout")
        return False


async def stop_comfy() -> bool:
    """Stop Comfy via the configured command (VRAM release)."""
    stopped = await _shell(_env("CARETAKER_COMFY_STOP_COMMAND", ""), label="stop")
    if stopped:
        _reset_idle_clock()
    return stopped


def _reset_idle_clock() -> None:
    global _last_activity_monotonic
    _last_activity_monotonic = time.monotonic()


def mark_used() -> None:
    """External activity marker (a proxied connection counts as use)."""
    global _last_activity_monotonic
    _last_activity_monotonic = time.monotonic()


async def astatus() -> dict[str, Any]:
    """Control-API status surface, mirroring the TTS/STT status shapes.

    Fully async: the queue probe must never block the event loop (the wake
    proxy and the idle watcher share it)."""
    up = False
    queue: dict[str, Any] | None = None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{comfy_url()}/queue")
        up = resp.status_code == 200
        if up and isinstance(resp.json(), dict):
            queue = resp.json()
    except Exception:  # noqa: BLE001 — down is a valid status
        up = False
    running = len((queue or {}).get("queue_running", []))
    pending = len((queue or {}).get("queue_pending", []))
    return {
        "enabled": comfy_enabled(),
        "url": comfy_url(),
        "up": up,
        "queue_running": running,
        "queue_pending": pending,
        "idle_seconds": _idle_seconds(),
        "idle_since_epoch": (
            round(time.time() - (time.monotonic() - _last_activity_monotonic), 1)
            if _last_activity_monotonic is not None
            else None
        ),
        "proxy_port": _env_int("CARETAKER_COMFY_PROXY_PORT", 0),
        "proxy_bind": _env("CARETAKER_COMFY_PROXY_BIND", "127.0.0.1"),
        # Booleans only — the raw command lines may embed hosts, task names,
        # tokens or internal paths and must not leak through the status API.
        "start_configured": bool(_env("CARETAKER_COMFY_START_COMMAND", "").strip()),
        "stop_configured": bool(_env("CARETAKER_COMFY_STOP_COMMAND", "").strip()),
    }


async def _idle_watcher_tick() -> str:
    """One watcher pass: returns "stopped" | "busy" | "idle" | "down" | "off".

    Kept as its own awaitable so pins can drive a single tick without the
    30 s loop."""
    global _last_activity_monotonic
    idle_seconds = _idle_seconds()
    if not comfy_enabled() or idle_seconds <= 0:
        return "off"
    snapshot = await _queue_snapshot()
    if snapshot is None:
        # Comfy down — nothing to release; keep the clock parked.
        _last_activity_monotonic = None
        return "down"
    busy = bool(
        snapshot.get("queue_running")
        or snapshot.get("queue_pending")
        or _active_connections > 0
    )
    if busy:
        _last_activity_monotonic = time.monotonic()
        return "busy"
    if _last_activity_monotonic is None:
        _last_activity_monotonic = time.monotonic()
        return "idle"
    if (time.monotonic() - _last_activity_monotonic) > idle_seconds:
        logger.info(
            "Comfy idle for %.0fs (limit %ds) — releasing",
            time.monotonic() - _last_activity_monotonic,
            idle_seconds,
        )
        stopped = await stop_comfy()
        return "stopped" if stopped else "stop_failed"
    return "idle"


async def _idle_watcher_loop() -> None:
    """Stop Comfy when the queue stays empty past the idle budget."""
    while True:
        try:
            await _idle_watcher_tick()
        except Exception as exc:  # noqa: BLE001 — the watcher must never die
            logger.warning("Comfy idle watcher error: %r", exc)
        await asyncio.sleep(30)


# ------------------------------------------------------------------ wake proxy


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    except Exception:  # noqa: BLE001 — a broken pipe on either side is normal
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


def upstream_target() -> tuple[str, int]:
    """The TCP forward target for the wake proxy, as (host, port).

    ``CARETAKER_COMFY_INTERNAL_URL`` (the documented "real Comfy URL behind
    the proxy") wins when set; otherwise the probe URL is the upstream (the
    default single-URL setup, where probe and upstream coincide). A port-less
    URL falls back to the module default 8189 — never Comfy's stock 8188,
    because this deployment moves Comfy off the public port."""
    raw = _env("CARETAKER_COMFY_INTERNAL_URL", "").strip() or comfy_url()
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    return (parsed.hostname or "127.0.0.1", parsed.port or 8189)


async def _handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:
    """Wake Comfy on an incoming connection, then pump bytes both ways."""
    global _active_connections
    mark_used()
    _active_connections += 1
    try:
        await _handle_client_inner(client_reader, client_writer)
    finally:
        _active_connections -= 1


async def _handle_client_inner(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:
    internal_host, internal_port = upstream_target()
    if await _queue_snapshot() is None:
        logger.info("Comfy wake proxy: connection on the public port — starting Comfy")
        if not await start_comfy():
            client_writer.close()
            return
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(
            internal_host, int(internal_port)
        )
    except Exception as exc:  # noqa: BLE001 — comfy refusing after a "successful" start
        logger.warning("Comfy wake proxy: upstream connect failed: %r", exc)
        client_writer.close()
        return
    to_upstream = asyncio.create_task(_pipe(client_reader, upstream_writer))
    to_client = asyncio.create_task(_pipe(upstream_reader, client_writer))
    await asyncio.gather(to_upstream, to_client, return_exceptions=True)


async def start_proxy() -> None:
    """Bind the public Comfy port (idempotent)."""
    global _proxy_server
    port = _env_int("CARETAKER_COMFY_PROXY_PORT", 0)
    if not port or _proxy_server is not None:
        return
    # Safe-by-default bind: 127.0.0.1 unless the operator explicitly exposes
    # the wake surface (a LAN-reachable proxy would let any host trigger the
    # start command without credentials).
    bind = _env("CARETAKER_COMFY_PROXY_BIND", "127.0.0.1")
    _proxy_server = await asyncio.start_server(_handle_client, bind, port)
    logger.info("🎧 Comfy wake proxy listening on %s:%s -> %s", bind, port, comfy_url())


async def init_async() -> None:
    """Start the watcher + wake proxy (called from the FastAPI startup)."""
    global _watcher_task, _enabled
    if _enabled:
        return
    _enabled = True
    _watcher_task = asyncio.create_task(_idle_watcher_loop())
    await start_proxy()
    logger.info("Comfy lifecycle armed (idle %ss, proxy %s)", _idle_seconds(), _env("CARETAKER_COMFY_PROXY_PORT", "0"))


async def shutdown_async() -> None:
    """Cancel the watcher and release the proxy socket (lifespan shutdown)."""
    global _watcher_task, _proxy_server, _enabled
    if _watcher_task is not None:
        _watcher_task.cancel()
        try:
            await _watcher_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutdown best-effort
            pass
        _watcher_task = None
    if _proxy_server is not None:
        _proxy_server.close()
        try:
            await _proxy_server.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        _proxy_server = None
    _enabled = False
    logger.info("Comfy lifecycle disarmed")


def init() -> None:
    """Module init hook for server.py import-time wiring (mirrors tts.init)."""
    # The actual tasks start on the FastAPI startup event (they need a loop).
    return None
