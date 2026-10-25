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
- ``CARETAKER_COMFY_CONN_IDLE_TIMEOUT``  per-connection silence budget before
                                    the proxy closes it (default 300; 0 =
                                    never) — an abandoned client must not pin
                                    the idle release forever
- ``CARETAKER_COMFY_MAX_CONCURRENT``    proxied connection slots (default 1):
                                        clients wait for each other instead of
                                        piling onto the GPU
- ``CARETAKER_COMFY_SLOT_WAIT_TIMEOUT`` seconds a client may wait for a slot
                                        (default 300; 0 = wait indefinitely)
- ``CARETAKER_COMFY_VRAM_MIN_FREE_MB``  VRAM gate threshold (default 0 = gate
                                        off): connections proceed only when the
                                        GPU has at least this much free
- ``CARETAKER_COMFY_VRAM_POLICY``       ``wait`` (default: nudge Comfy's /free
                                        and re-probe) or ``fail`` (reject at
                                        once with a 503)
- ``CARETAKER_COMFY_VRAM_WAIT_SECONDS`` gate budget before rejecting (default 120)
- ``CARETAKER_COMFY_EVICT_MIN_TENURE_S`` anti-flap: minimum seconds between two
                                        LLM evictions under the ``evict``
                                        policy (default 180) — a queued comfy
                                        burst must not unload/reload the LLM
                                        on every request; the evicted LLM
                                        re-loads itself on its next request
                                        through the existing /ensure path

Mirrors the TTS/STT module contract: ``init()`` from server.py, a 30 s
exception-proof watcher loop, and a ``status()`` for the control API.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import httpx

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
# Connection-slot serialization (API/MCP clients wait on each other) + the
# VRAM gate state surfaced through astatus().
_slot_semaphore: asyncio.Semaphore | None = None
_slot_semaphore_size = 0
_slot_active = 0
_slot_waiting = 0
_last_free_mb: int | None = None
_vram_policy_warned = False
# Serializes the VRAM gate: even with MAX_CONCURRENT > 1 only one probe/nudge
# loop may run at a time, or N clients turn the /free nudge into a herd that
# can unload Comfy models mid-job.
_vram_gate_lock = asyncio.Lock()
# The evict-policy hook (cross-workload GPU arbiter, fase 2): when a proxied
# comfy connection cannot get VRAM because the LLM owns it, the gate evicts
# the llama backend via this hook and the next LLM request re-loads it
# through the existing /ensure repair path. server.py injects
# ``manager.unload`` at lifespan startup; the hook is a plain attribute so a
# missing/injected manager degrades to a warning instead of a broken proxy.
_llama_evict_hook: Callable[[], Awaitable[object]] | None = None
_last_evict_monotonic: float | None = None
_evict_count = 0


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
        "vram_gate_enabled": _vram_min_free_mb() > 0,
        "vram_policy": _vram_policy(),
        "evict_count": _evict_count,
        "last_evict_epoch": (
            round(time.time() - (time.monotonic() - _last_evict_monotonic), 1)
            if _last_evict_monotonic is not None
            else None
        ),
        "evict_hook_set": _llama_evict_hook is not None,
        "max_concurrent": _max_concurrent(),
        "slot_active": _slot_active,
        "slot_waiting": _slot_waiting,
        "last_free_mb": _last_free_mb,
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
    """Pump bytes one direction; give up after connection silence.

    A peer that vanishes without a FIN (dropped websocket, hung tab) would
    otherwise pin ``_active_connections`` up forever and defeat the idle
    release — so a connection silent for
    ``CARETAKER_COMFY_CONN_IDLE_TIMEOUT`` seconds (default 300; 0 = never) is
    closed from our side. Comfy's own ws client reconnects transparently."""
    conn_idle = _env_int("CARETAKER_COMFY_CONN_IDLE_TIMEOUT", 300)
    try:
        while True:
            if conn_idle > 0:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=conn_idle)
            else:
                chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except TimeoutError:
        logger.info("Comfy proxy: connection silent for %ss — releasing", conn_idle)
    except Exception:  # A broken pipe on either side is normal.
        logger.debug("Comfy proxy pipe closed after an error", exc_info=True)
    finally:
        try:
            writer.close()
        except Exception:
            logger.debug("Comfy proxy writer close failed", exc_info=True)


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


def _max_concurrent() -> int:
    return max(1, _env_int("CARETAKER_COMFY_MAX_CONCURRENT", 1))


def _slot_wait_timeout() -> int:
    return _env_int("CARETAKER_COMFY_SLOT_WAIT_TIMEOUT", 300)


def _vram_min_free_mb() -> int:
    return _env_int("CARETAKER_COMFY_VRAM_MIN_FREE_MB", 0)


def _vram_policy() -> str:
    return _env("CARETAKER_COMFY_VRAM_POLICY", "wait").strip().lower()


def _vram_wait_seconds() -> int:
    return _env_int("CARETAKER_COMFY_VRAM_WAIT_SECONDS", 120)


def _evict_min_tenure_s() -> int:
    """Minimum seconds between two LLM evictions (anti-flap: a queued comfy
    burst must not unload/reload the LLM every request)."""
    return max(0, _env_int("CARETAKER_COMFY_EVICT_MIN_TENURE_S", 180))


def set_llama_evict_hook(fn: Callable[[], Awaitable[object]] | None) -> None:
    """Install the LLM-evict hook (server.py wires ``manager.unload`` at
    lifespan startup). Passing None disarms evict (the gate then behaves as
    the ``wait`` policy)."""
    global _llama_evict_hook
    _llama_evict_hook = fn


def _get_slot_semaphore() -> asyncio.Semaphore:
    """Loop-created, shared across proxied connections; rebuilt only while NO
    slots are held or waited on (env is read per call, tests monkeypatch it),
    so a permit is never credited to a semaphore a live connection did not
    consume and queued waiters are never stranded on a stale instance."""
    global _slot_semaphore, _slot_semaphore_size
    want = _max_concurrent()
    if _slot_semaphore is None or (
        _slot_semaphore_size != want and _slot_active == 0 and _slot_waiting == 0
    ):
        _slot_semaphore = asyncio.Semaphore(want)
        _slot_semaphore_size = want
    return _slot_semaphore


async def _probe_free_vram_mb() -> int | None:
    """Minimum free VRAM across GPUs (nvidia-smi), or None on any failure.

    Fail-open: a broken probe must never brick the proxy — the gate logs and
    lets the connection through."""
    global _last_free_mb
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=memory.free",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except BaseException:
            # A cancelled communicate() leaves the child running — kill and
            # reap it, or a wedged nvidia-smi leaks a zombie on every probe.
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raise
    except Exception:
        logger.warning("⚠️ Comfy VRAM probe failed — gate fails open", exc_info=True)
        return None
    try:
        values = [int(line.strip()) for line in out.decode().splitlines() if line.strip()]
    except ValueError:
        logger.warning("⚠️ Comfy VRAM probe returned unparsable output — gate fails open")
        return None
    if not values:
        return None
    _last_free_mb = min(values)
    return _last_free_mb


async def _synthetic_503(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, body: dict[str, Any]
) -> None:
    """Answer a not-yet-piped connection with a minimal HTTP 503 + JSON body.

    The proxy speaks raw bytes; this is only ever sent BEFORE forwarding
    starts, so no HTTP parsing is involved. Inbound bytes are drained first
    (bounded): closing a socket with unread client data makes the TCP stack
    emit RST, which can discard the 503 before the client ever reads it."""
    payload = json.dumps(body).encode()
    head = (
        "HTTP/1.1 503 Service Unavailable\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    try:
        drain_deadline = time.monotonic() + 2.0
        while time.monotonic() < drain_deadline:
            try:
                if not await asyncio.wait_for(reader.read(65536), timeout=0.25):
                    break  # EOF — the client is already gone
            except TimeoutError:
                break  # nothing more inbound within the read budget
        writer.write(head + payload)
        await writer.drain()
    except Exception:
        logger.debug("Comfy proxy: 503 write failed", exc_info=True)
    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()


async def _vram_gate() -> dict[str, Any] | None:
    """VRAM gate: return None to proceed, or a 503 body dict to reject.

    ``wait`` policy nudges Comfy's /free (release cached models) and re-probes
    within the wait budget; ``fail`` rejects on the first below-threshold
    probe; ``evict`` additionally evicts the LLM backend via the injected
    hook (once per ``EVICT_MIN_TENURE``) so a queued comfy job can claim the
    GPU — the LLM re-loads on its next request through the /ensure path.
    Probe failure always passes (fail-open). Serialized by
    ``_vram_gate_lock``: concurrent connections must not each spawn their own
    probe/nudge loop — the /free herd can unload models mid-job."""
    global _vram_policy_warned, _last_evict_monotonic, _evict_count
    min_free = _vram_min_free_mb()
    if min_free <= 0:
        return None
    async with _vram_gate_lock:
        policy = _vram_policy()
        if policy not in ("wait", "fail", "evict"):
            if not _vram_policy_warned:
                logger.warning("⚠️ Unknown CARETAKER_COMFY_VRAM_POLICY %r — treating as wait", policy)
                _vram_policy_warned = True
            policy = "wait"
        deadline = time.monotonic() + _vram_wait_seconds()
        nudged = False
        while True:
            try:
                free = await _probe_free_vram_mb()
            except Exception:
                logger.warning("⚠️ Comfy VRAM probe raised — gate fails open", exc_info=True)
                return None
            if free is None:
                return None
            if free >= min_free:
                return None
            if policy == "fail":
                return {
                    "error": "comfy_vram_unavailable",
                    "message": "free VRAM below the comfy gate threshold",
                    "free_mb": free,
                    "required_mb": min_free,
                }
            if time.monotonic() >= deadline:
                return {
                    "error": "comfy_vram_unavailable",
                    "message": "free VRAM stayed below threshold within the wait budget",
                    "free_mb": free,
                    "required_mb": min_free,
                }
            snap = await _queue_snapshot()
            if not snap or snap.get("queue_running"):
                # Busy (or unreachable): a job owns the VRAM and /free can
                # unload models mid-generation — never nudge, never evict;
                # just keep the re-probe rhythm until the budget runs out.
                await asyncio.sleep(2.0)
                continue
            if policy == "evict" and nudged:
                # Comfy's own cache was already released and VRAM is STILL
                # short — escalate to the LLM hook so the queued comfy job
                # can claim the GPU; the LLM re-loads via /ensure on demand.
                if _llama_evict_hook is None:
                    logger.warning("⚠️ Comfy evict policy without an LLM hook — behaving as wait")
                    policy = "wait"
                    await asyncio.sleep(2.0)
                    continue
                now = time.monotonic()
                if (
                    _last_evict_monotonic is not None
                    and (now - _last_evict_monotonic) < _evict_min_tenure_s()
                ):
                    # Inside the anti-flap tenure: the LLM stays; the job
                    # waits exactly like under the wait policy.
                    await asyncio.sleep(2.0)
                    continue
                logger.info(
                    "Comfy VRAM gate: evicting LLM backend (free %s MB < %s MB) for a queued comfy job",
                    free,
                    min_free,
                )
                try:
                    await _llama_evict_hook()
                except Exception:
                    logger.warning("⚠️ Comfy evict hook failed — behaving as wait", exc_info=True)
                    policy = "wait"
                    await asyncio.sleep(2.0)
                    continue
                _last_evict_monotonic = now
                _evict_count += 1
                await asyncio.sleep(2.0)
                continue
            # Nudge Comfy to release cached models, then re-probe. The wait
            # policy nudges on every idle pass; the evict policy nudges once
            # and then escalates (the flag above).
            host, port = upstream_target()
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(f"http://{host}:{port}/free", json={"unload_models": True})
            except Exception:
                logger.debug("Comfy /free nudge failed during VRAM gate", exc_info=True)
            nudged = True
            await asyncio.sleep(2.0)


async def _acquire_slot(timeout_s: int) -> tuple[bool, asyncio.Semaphore | None]:
    """Acquire a connection slot; ``(False, None)`` on timeout. Tracks
    active/waiting. Returns the acquired semaphore so the release credits the
    exact instance — a mid-connection rebuild must not miscredit a permit."""
    global _slot_active, _slot_waiting
    sem = _get_slot_semaphore()
    started = time.monotonic()
    _slot_waiting += 1
    try:
        if timeout_s <= 0:
            await sem.acquire()
        else:
            try:
                await asyncio.wait_for(sem.acquire(), timeout=timeout_s)
            except TimeoutError:
                return False, None
    finally:
        _slot_waiting -= 1
    _slot_active += 1
    elapsed = time.monotonic() - started
    if elapsed > 0.05:
        logger.info("Comfy proxy: connection waited %.1fs for a slot", elapsed)
    return True, sem


def _release_slot(sem: asyncio.Semaphore) -> None:
    """Release the semaphore this connection actually acquired."""
    global _slot_active, _slot_semaphore
    _slot_active = max(0, _slot_active - 1)
    sem.release()
    # asyncio.Semaphore never raises on over-release (unbounded, unlike
    # threading.BoundedSemaphore), so a lost-permit race cannot be caught as
    # ValueError. Instead the permit accounting is verified at the idle
    # boundary — with no slots held AND no waiters (a waiting client receives
    # the permit directly: value stays 0) every permit must be free, anything
    # else is drift (phantom consumption on pre-3.12.4 asyncio, or a double
    # release) and the semaphore is dropped for a clean rebuild. `_value` is
    # private asyncio state; if a future CPython removes it, the check
    # degrades to disabled instead of crashing the release path.
    value = getattr(sem, "_value", None)
    if value is not None and _slot_active == 0 and _slot_waiting == 0 and value != _slot_semaphore_size:
        logger.warning(
            "⚠️ Comfy slot accounting drift (value=%s size=%s) — rebuilding semaphore",
            value,
            _slot_semaphore_size,
        )
        _slot_semaphore = None


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
    ok, sem = await _acquire_slot(_slot_wait_timeout())
    if not ok:
        await _synthetic_503(
            client_reader,
            client_writer,
            {"error": "comfy_slot_timeout", "message": "Comfy proxy slot wait timed out"},
        )
        return
    try:
        # The gate runs INSIDE the slot (and is lock-serialized) so probing
        # and /free nudges stay bounded by max_concurrent instead of fanning
        # out per waiting client.
        gate_error = await _vram_gate()
        if gate_error is not None:
            await _synthetic_503(client_reader, client_writer, gate_error)
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
    finally:
        _release_slot(sem)


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
    try:
        _proxy_server = await asyncio.start_server(_handle_client, bind, port)
    except OSError as exc:
        # Optional feature: an unbindable port (already taken, permissions)
        # degrades to proxy-off — it must never abort the caretaker boot.
        logger.warning("⚠️ Comfy wake proxy not started (%s:%s): %r", bind, port, exc)
        return
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
        except (asyncio.CancelledError, Exception):  # Shutdown is best-effort.
            logger.debug("Comfy idle watcher stopped during shutdown", exc_info=True)
        _watcher_task = None
    if _proxy_server is not None:
        _proxy_server.close()
        try:
            await _proxy_server.wait_closed()
        except Exception:
            logger.debug("Comfy proxy wait_closed failed during shutdown", exc_info=True)
        _proxy_server = None
    _enabled = False
    logger.info("Comfy lifecycle disarmed")


def init() -> None:
    """Module init hook for server.py import-time wiring (mirrors tts.init)."""
    # The actual tasks start on the FastAPI startup event (they need a loop).
