"""On-demand STT engine lifecycle — platform-agnostic.

The caretaker OWNS the STT engine process on every host the same way it owns
llama-server: it spawns it from ``CARETAKER_STT_COMMAND`` (working dir
``CARETAKER_STT_CWD``), health-checks ``CARETAKER_STT_URL/health``, and stops
it when the route goes idle. There is deliberately NO platform gate: the same
code runs on Linux, Windows, or a cloud GPU box (RunPod et al.) — the
caretaker is a provider, and providers behave identically everywhere
(operator principle, 2026-09-16).

Contract (identical on every host):
- ``POST /tts/ensure``  — idempotent: health-check first; spawn when down;
  refresh the idle timer. Never raises; returns ``{"ok": bool, ...}``.
- ``POST /tts/release`` — stop the engine process (frees its VRAM); idempotent.
- ``GET  /tts/status``  — lifecycle state for the gateway/operator.

An idle watcher stops the engine after ``CARETAKER_STT_IDLE_SECONDS`` without
an ensure (0 disables). The engine is the truth: a healthy ``/health`` beats
any bookkeeping state. When the caretaker itself restarts, its child dies with
it (same semantics as llama-server) and the next ensure re-spawns.

Configuration (env, read at call time):
- ``CARETAKER_STT_ENABLED``       default "1"
- ``CARETAKER_STT_URL``           engine base URL (default http://127.0.0.1:11451)
- ``CARETAKER_STT_COMMAND``       command line to spawn the engine (REQUIRED
                                  for cold start; without it ensure reports
                                  "not configured" — the engine was meant to
                                  be managed elsewhere)
- ``CARETAKER_STT_CWD``           working directory for the spawned command
- ``CARETAKER_STT_IDLE_SECONDS``  idle budget (default 600; 0 = never stop)
- ``CARETAKER_STT_START_TIMEOUT`` cold-start health wait (default 90 s)
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
import time

import httpx

logger = logging.getLogger("caretaker.stt")

_last_use_monotonic: float | None = None
_process: asyncio.subprocess.Process | None = None
_watcher_task: asyncio.Task | None = None
_manager_getter = None  # set by init(): lazy access to the Caretaker singleton


def init(manager_getter) -> None:
    """Inject the lazy manager accessor so the STT ensure can coordinate VRAM
    with the caretaker's own llama-server (stop it when the engine needs the
    memory).  Called once from server.py with ``lambda: _manager()``."""
    global _manager_getter
    _manager_getter = manager_getter


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _engine_url() -> str:
    return _env("CARETAKER_STT_URL", "http://127.0.0.1:11451").rstrip("/")


def stt_enabled() -> bool:
    """STT lifecycle is a plain provider capability: enabled by default on
    every host (the command decides whether cold-start is possible)."""
    return _env("CARETAKER_STT_ENABLED", "1") == "1"


def stt_status() -> dict:
    """Report the STT lifecycle state (for GET /tts/status and GET /status)."""
    return {
        "enabled": stt_enabled(),
        "url": _engine_url(),
        "command_configured": bool(_env("CARETAKER_STT_COMMAND", "").strip()),
        "idle_seconds": int(_env("CARETAKER_STT_IDLE_SECONDS", "600")),
        "last_use_epoch": (
            round(time.time() - (time.monotonic() - _last_use_monotonic), 1)
            if _last_use_monotonic is not None
            else None
        ),
        "child_pid": _process.pid if _process is not None and _process.returncode is None else None,
    }


async def _gpu_free_mb() -> int | None:
    """Free VRAM (MiB) on the engine's target GPU (CARETAKER_STT_CUDA_DEVICE,
    else device 0).  None when nvidia-smi is unavailable (non-NVIDIA hosts)."""
    idx = _env("CARETAKER_STT_CUDA_DEVICE", "0")
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi", "--id", idx, "--query-gpu=memory.free",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return int(out.decode(errors="replace").strip().splitlines()[0].strip())
    except Exception:  # noqa: BLE001 — best-effort; the gate degrades to off
        return None


async def _engine_healthy(timeout_s: float = 3.0) -> bool:
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(f"{_engine_url()}/health")
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


def _spawn_command() -> list[str] | None:
    cmd = _env("CARETAKER_STT_COMMAND", "").strip()
    if not cmd:
        return None
    return shlex.split(cmd, posix=os.name != "nt")


async def _start_engine() -> dict:
    """Spawn the engine process and wait for /health. Returns the result dict."""
    global _process
    cmd = _spawn_command()
    if cmd is None:
        return {"ok": False, "reason": "CARETAKER_STT_COMMAND not configured"}

    # A previous child may still be winding down; reap it so the port frees.
    if _process is not None and _process.returncode is None:
        try:
            _process.terminate()
            await asyncio.wait_for(_process.wait(), timeout=10)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass

    cwd = _env("CARETAKER_STT_CWD", "") or None
    log_path = _env("CARETAKER_STT_LOG", "")
    log_fh = open(log_path, "ab") if log_path else subprocess.DEVNULL
    # Force UTF-8 IO on the engine child: engine prints carry emoji, and on
    # Windows a redirected file defaults to cp1252 — the print then raises
    # UnicodeEncodeError and kills the model load mid-flight.  Linux defaults
    # to UTF-8, so this is a no-op there; the same env works on every host.
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        _process = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, stdout=log_fh, stderr=subprocess.STDOUT, env=env
        )
    except (OSError, ValueError) as exc:
        logger.warning("STT engine spawn failed (%s): %r", cmd, exc)
        return {"ok": False, "reason": f"spawn failed: {exc}"}

    start_timeout = float(_env("CARETAKER_STT_START_TIMEOUT", "90"))
    deadline = time.monotonic() + start_timeout
    while time.monotonic() < deadline:
        if _process.returncode is not None:
            logger.warning(
                "STT engine exited during startup (rc=%s) — see CARETAKER_STT_LOG",
                _process.returncode,
            )
            return {"ok": False, "reason": f"engine exited during startup (rc={_process.returncode})"}
        if await _engine_healthy():
            logger.info("STT engine started and healthy (pid %s)", _process.pid)
            return {"ok": True, "already_running": False, "cold_start": True, "pid": _process.pid}
        await asyncio.sleep(2)
    logger.warning("STT engine not healthy within %.0fs after spawn", start_timeout)
    return {"ok": False, "reason": f"engine not healthy within {start_timeout:.0f}s"}


def _mark_used() -> None:
    global _last_use_monotonic
    _last_use_monotonic = time.monotonic()


async def ensure_stt() -> dict:
    """Idempotent: make sure the engine answers /health, refresh the idle timer."""
    global _watcher_task
    if not stt_enabled():
        return {"ok": False, "reason": "CARETAKER_STT_ENABLED=0"}
    if _watcher_task is None:
        _watcher_task = asyncio.create_task(_idle_watcher_loop())

    # Serialize concurrent ensures: a request arriving while a cold start is
    # in flight WAITS on this lock and then takes the healthy fast-path.  Two
    # engines must never spawn next to each other on the same GPU.
    async with _ensure_lock:
        return await _ensure_locked()


_ensure_lock = asyncio.Lock()


async def _ensure_locked() -> dict:
    if await _engine_healthy():
        # Could be our child, an engine started elsewhere, or an adopt-after-
        # restart — all fine: the contract is the health endpoint, not the pid.
        _mark_used()
        return {"ok": True, "already_running": True}

    # VRAM gate: the engine needs ~2 GB.  On hosts where the caretaker also
    # runs llama-server, both processes compete for the same GPU.
    # CARETAKER_STT_STOP_LLAMA=1 means STT-first, DETERMINISTICALLY: whenever
    # the engine must start, the caretaker's own llama-server yields (unload
    # is idempotent) — chat on such a host falls back via the failover group.
    # With the coordination disabled the VRAM budget below still applies and
    # the ensure fails with an honest reason instead of a silent GPU fight.
    min_free = int(_env("CARETAKER_STT_MIN_FREE_MB", "2500"))
    if _env("CARETAKER_STT_STOP_LLAMA", "0") == "1" and _manager_getter is not None:
        logger.info("STT ensure: STT-first host — unloading the caretaker's llama-server")
        try:
            await _manager_getter().unload()
        except Exception as exc:  # noqa: BLE001 — fall through to the VRAM check
            logger.warning("STT ensure: llama unload failed: %r", exc)
        await asyncio.sleep(4)  # let the driver reclaim the memory
    free = await _gpu_free_mb()
    if free is not None and free < min_free:
        if free is not None and free < min_free:
            # All VRAM busy: WAIT for it to free up (CARETAKER_STT_VRAM_WAIT_
            # SECONDS, 0 = give up immediately) or fail with an honest reason.
            # On a busy host the memory frees when the big model idle-unloads
            # or another engine stops; the guardian's ensure_timeout_seconds
            # must cover wait + cold start (see global.settings.yaml).
            wait_s = int(_env("CARETAKER_STT_VRAM_WAIT_SECONDS", "0"))
            poll_s = max(2, int(_env("CARETAKER_STT_VRAM_POLL_SECONDS", "5")))
            deadline = time.monotonic() + wait_s
            while free is not None and free < min_free and time.monotonic() < deadline:
                logger.info(
                    "STT ensure: VRAM busy (%s/%s MB free) — waiting up to %ss",
                    free, min_free, wait_s,
                )
                await asyncio.sleep(min(poll_s, max(1.0, deadline - time.monotonic())))
                if await _engine_healthy():
                    _mark_used()
                    return {"ok": True, "already_running": True}
                free = await _gpu_free_mb()
            if free is not None and free < min_free:
                waited = f" after {wait_s}s wait" if wait_s > 0 else ""
                return {
                    "ok": False,
                    "reason": (
                        f"insufficient VRAM free ({free} MB < {min_free} MB){waited} — "
                        "configure CARETAKER_STT_VRAM_WAIT_SECONDS to wait longer or rely on failover"
                    ),
                }

    result = await _start_engine()
    if result.get("ok"):
        _mark_used()
    return result


async def release_stt() -> dict:
    """Stop the engine process (frees its VRAM); idempotent."""
    global _process
    if not stt_enabled():
        return {"ok": False, "reason": "CARETAKER_STT_ENABLED=0"}
    if not await _engine_healthy():
        return {"ok": True, "already_stopped": True}

    # An engine we did not spawn (e.g. started manually): still stop the
    # process tree the same way — on this host the caretaker owns the port.
    if _process is not None and _process.returncode is None:
        try:
            _process.terminate()
            await asyncio.wait_for(_process.wait(), timeout=15)
        except Exception:  # noqa: BLE001 — escalate to kill
            try:
                _process.kill()
            except ProcessLookupError:
                pass
    else:
        # Not our child (adopted): best-effort kill by port owner is out of
        # scope — report what happened so the caller knows the engine may
        # still be up under someone else's supervision.
        logger.warning("TTS release: engine healthy but not a caretaker child — cannot stop")
        return {"ok": False, "reason": "engine not spawned by this caretaker (no child handle)"}

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not await _engine_healthy():
            logger.info("STT engine stopped (VRAM freed)")
            return {"ok": True, "stopped": True}
        await asyncio.sleep(1)
    return {"ok": False, "reason": "engine still healthy 15s after stop"}


async def _idle_watcher_loop() -> None:
    """Stop the engine after CARETAKER_STT_IDLE_SECONDS without an ensure."""
    while True:
        try:
            idle_seconds = int(_env("CARETAKER_STT_IDLE_SECONDS", "600"))
            if (
                stt_enabled()
                and idle_seconds > 0
                and _last_use_monotonic is not None
                and (time.monotonic() - _last_use_monotonic) > idle_seconds
                and await _engine_healthy()
            ):
                logger.info(
                    "STT engine idle for %.0fs (limit %ds) — releasing",
                    time.monotonic() - _last_use_monotonic, idle_seconds,
                )
                await release_stt()
        except Exception as exc:  # noqa: BLE001 — the watcher must never die
            logger.warning("TTS idle watcher error: %r", exc)
        await asyncio.sleep(30)
