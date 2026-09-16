"""On-demand TTS engine lifecycle (Windows): start/stop the qwen3tts-http
engine service so its VRAM is only held while the audio route is in use.

Design (2026-09-16, guardian F6 follow-up — operator request "ondemand, zodat
mijn VRAM niet gebruikt wordt als de audio route niet gebruikt wordt"):

- The gateway calls ``POST /tts/ensure`` before every engine forward; the call
  is idempotent (health-check first) and refreshes the idle timer.
- A background watcher stops the service after ``CARETAKER_TTS_IDLE_SECONDS``
  without an ensure (0 disables the watcher).
- The engine truth is its ``/health`` endpoint, not the SCM state: a service
  can be "running" while the engine is still loading.
- Non-Windows hosts are inert: the routes report ``disabled`` so this module
  ships unchanged with the Linux caretaker.

Configuration (env, read at call time):
- ``CARETAKER_TTS_ENABLED``      (default "1"; also forced off off-Windows)
- ``CARETAKER_TTS_SERVICE``      service name (default "qwen3tts-http")
- ``CARETAKER_TTS_URL``          engine base URL (default http://127.0.0.1:11450)
- ``CARETAKER_TTS_IDLE_SECONDS`` idle timer (default 600; 0 = never stop)
- ``CARETAKER_TTS_START_TIMEOUT`` cold-start health wait (default 90 s)

Service control uses ``sc start/stop`` (LocalSystem has SCM rights; NSSM
performs the actual process supervision — a service stop is a clean stop and
does NOT trigger the NSSM restart-on-exit path).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

import httpx

logger = logging.getLogger("caretaker.tts")

_last_use_monotonic: float | None = None
_watcher_task: asyncio.Task | None = None


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def tts_enabled() -> bool:
    """TTS lifecycle is a Windows feature; off-Windows hosts report disabled."""
    return sys.platform == "win32" and _env("CARETAKER_TTS_ENABLED", "1") == "1"


def tts_status() -> dict:
    """Report the TTS lifecycle state (for GET /tts/status and GET /status)."""
    idle_seconds = int(_env("CARETAKER_TTS_IDLE_SECONDS", "600"))
    running: bool | None = None
    if tts_enabled() and _last_use_monotonic is not None:
        running = None  # unknown until probed; status stays cheap and sync
    return {
        "enabled": tts_enabled(),
        "service": _env("CARETAKER_TTS_SERVICE", "qwen3tts-http"),
        "url": _env("CARETAKER_TTS_URL", "http://127.0.0.1:11450"),
        "idle_seconds": idle_seconds,
        "last_use_epoch": (
            round(time.time() - (time.monotonic() - _last_use_monotonic), 1)
            if _last_use_monotonic is not None
            else None
        ),
        "running": running,
    }


async def _engine_healthy(timeout_s: float = 3.0) -> bool:
    url = _env("CARETAKER_TTS_URL", "http://127.0.0.1:11450").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(f"{url}/health")
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def _service_control(action: str) -> tuple[int, str]:
    """Run ``sc <action> <service>``; returns (returncode, stdout)."""
    service = _env("CARETAKER_TTS_SERVICE", "qwen3tts-http")
    proc = await asyncio.create_subprocess_exec(
        "sc", action, service,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace").strip()


def _mark_used() -> None:
    global _last_use_monotonic
    _last_use_monotonic = time.monotonic()


async def ensure_tts() -> dict:
    """Idempotent: make sure the engine answers /health, refresh the idle timer.

    Returns ``{"ok": bool, "already_running": bool, ...}``; never raises —
    the gateway treats ok=False as a failed attempt.
    """
    global _watcher_task
    if not tts_enabled():
        return {"ok": False, "reason": "tts lifecycle disabled (off-Windows or CARETAKER_TTS_ENABLED=0)"}
    if _watcher_task is None:
        _watcher_task = asyncio.create_task(_idle_watcher_loop())

    if await _engine_healthy():
        _mark_used()
        return {"ok": True, "already_running": True}

    start_timeout = float(_env("CARETAKER_TTS_START_TIMEOUT", "90"))
    rc, out = await _service_control("start")
    # A non-zero rc may mean "already starting"; the health poll decides.
    if rc != 0 and "1056" not in out and "already running" not in out.lower():
        logger.warning("TTS service start failed (rc=%s): %s", rc, out[:200])
        return {"ok": False, "reason": f"service start failed (rc={rc})", "detail": out[:200]}

    deadline = time.monotonic() + start_timeout
    while time.monotonic() < deadline:
        if await _engine_healthy():
            _mark_used()
            logger.info("TTS engine started and healthy (cold start <= %.0fs)", start_timeout)
            return {"ok": True, "already_running": False, "cold_start": True}
        await asyncio.sleep(2)
    logger.warning("TTS engine not healthy within %.0fs after service start", start_timeout)
    return {"ok": False, "reason": f"engine not healthy within {start_timeout:.0f}s"}


async def release_tts() -> dict:
    """Stop the engine service (VRAM freed); idempotent."""
    if not tts_enabled():
        return {"ok": False, "reason": "tts lifecycle disabled"}
    if not await _engine_healthy():
        return {"ok": True, "already_stopped": True}
    rc, out = await _service_control("stop")
    if rc != 0 and "1062" not in out and "not started" not in out.lower():
        logger.warning("TTS service stop failed (rc=%s): %s", rc, out[:200])
        return {"ok": False, "reason": f"service stop failed (rc={rc})", "detail": out[:200]}
    # Give the engine a moment to actually drop (health goes stale-fast).
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if not await _engine_healthy():
            logger.info("TTS engine stopped (VRAM freed)")
            return {"ok": True, "stopped": True}
        await asyncio.sleep(2)
    return {"ok": False, "reason": "engine still healthy 30s after stop"}


async def _idle_watcher_loop() -> None:
    """Stop the engine after CARETAKER_TTS_IDLE_SECONDS without an ensure."""
    while True:
        try:
            idle_seconds = int(_env("CARETAKER_TTS_IDLE_SECONDS", "600"))
            if (
                tts_enabled()
                and idle_seconds > 0
                and _last_use_monotonic is not None
                and (time.monotonic() - _last_use_monotonic) > idle_seconds
                and await _engine_healthy()
            ):
                logger.info(
                    "TTS engine idle for %.0fs (limit %ds) — releasing",
                    time.monotonic() - _last_use_monotonic, idle_seconds,
                )
                await release_tts()
        except Exception as exc:  # noqa: BLE001 — the watcher must never die
            logger.warning("TTS idle watcher error: %r", exc)
        await asyncio.sleep(30)
