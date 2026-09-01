"""Caretaker lifecycle manager (Phase A — lifecycle core).

Phase A (PLAN.md §2) relocates the local llama-server lifecycle core out of
``guardian-llmprovider-gateway/app/engine/manager.py`` into this module,
**behaviour-neutrally**: args building is byte-identical, and every
deployment literal is re-routed through ``caretaker.paths`` / config instead
of being hardcoded.

The gateway keeps the registry/choice/pinning/switch-allowlist (F4); the
caretaker only executes: spawn, stop, reload (drift-aware), health, unload.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from . import config as config_mod
from .paths import (
    CURRENT_MODEL_ARGS_FILE,
    CURRENT_MODEL_ENV_FILE,
    CURRENT_MODEL_SIG_FILE,
    LLAMA_SERVER_BIN,
    SERVER_URL,
    SYSTEMD_SERVICE,
    llama_slots_dir,
    server_url,
)
from .vram import VramScheduler, get_model_size_mb

logger = logging.getLogger("caretaker.manager")

MAX_CRASH_HISTORY = 50  # Keep last N crash records

# Health-probe defaults mirroring the guardian's ``_wait_for_health`` (120 polls,
# 1s interval). They are constructor-injectable so tests/fakes can run fast.
DEFAULT_HEALTH_POLLS = 120
DEFAULT_HEALTH_INTERVAL = 1.0
DEFAULT_HEALTH_TIMEOUT = 5.0

# Short timeout for the strict GET /props model verification. Callers poll
# /health first (bounded ``_wait_for_health`` after a start; a single
# ``health_ok`` on the no-op path), so by the time /props is queried the
# backend answers requests — a single short-timeout call must suffice and
# keeps total /ensure latency bounded.
DEFAULT_PROPS_TIMEOUT = 5.0

# Watchdog defaults (Phase C). The operator starts the watchdog explicitly via
# ``manager.start_watchdog()``; interval/backoff are constructor-free here so
# tests can exercise ``_watchdog_loop`` with short values directly.
DEFAULT_WATCHDOG_INTERVAL = 15.0
DEFAULT_WATCHDOG_INITIAL_BACKOFF = 5.0
DEFAULT_WATCHDOG_MAX_BACKOFF = 60.0


@dataclass
class CrashRecord:
    """Record of a llama-server crash event."""

    timestamp: str
    model: str
    error_message: str
    exit_code: int | None = None
    config_snapshot: dict | None = None

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "model": self.model,
            "error_message": self.error_message,
            "exit_code": self.exit_code,
            "config_snapshot": self.config_snapshot,
        }


class ModelLoadError(Exception):
    """Raised when llama-server fails to load a model."""

    def __init__(self, message: str, crash_record: CrashRecord | None = None):
        super().__init__(message)
        self.crash_record = crash_record


class ModelMismatchError(Exception):
    """Raised when the backend does not provably serve the requested model.

    Strict post-load verification (GET /props) failed: either the backend
    reports a different model path than configured, or /props could not be
    consulted at all (verification must be proven, never assumed — the
    2026-09-01 incident let /ensure confirm success while llama-server was
    still serving the previous model). Carries the machine-readable fields
    the control API maps into its 503 ``model_mismatch`` body.
    """

    def __init__(
        self,
        model: str,
        expected: str | None,
        actual: str | None,
        message: str | None = None,
    ) -> None:
        self.model = model
        self.expected = expected
        self.actual = actual
        super().__init__(
            message
            or f"backend serves {actual!r}, expected {expected!r} for model '{model}'"
        )


class ServerProcess(ABC):
    """Abstracts how caretaker starts/stops a llama-server backend.

    Phase A ships two implementations: :class:`SystemdServerProcess` (the
    default on Linux, controlling the ``llama-server`` systemd unit) and
    :class:`DirectServerProcess` (spawns ``LLAMA_SERVER_BIN`` directly as a
    subprocess — used for tests and as a fallback; the Windows/NSSM impl in
    Phase E). The manager also routes crash-detection introspection through
    this interface so the lifecycle logic stays generic.
    """

    @abstractmethod
    async def start(self) -> None:
        """Start llama-server."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop llama-server."""

    async def health_ok(self, url: str = SERVER_URL) -> bool:
        """Return True when the managed llama-server at ``url`` accepts requests."""
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_HEALTH_TIMEOUT) as client:
                resp = await client.get(f"{url}/health")
            return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def restart_count(self) -> int:
        """Return the service's restart counter (systemd NRestarts)."""
        return 0

    async def is_failed(self) -> bool:
        """Return True when the managed service entered a failed state."""
        return False

    async def crash_error(self) -> str:
        """Return a human-readable crash/reason string from the last run."""
        return "Unknown error (no crash-log source available)"

    async def service_exit_code(self) -> int | None:
        """Return the last run's exit code, if known."""
        return None


class SystemdServerProcess(ServerProcess):
    """Linux systemd backend: ``sudo systemctl start|stop llama-server``.

    Uses ``asyncio.create_subprocess_exec`` (no shell, no f-string injection)
    exactly like the guardian's ``_stop_server``/``_start_server``. The unit
    name is configurable (``CARETAKER_SYSTEMD_SERVICE``, default
    ``llama-server``).
    """

    def __init__(self, service: str = SYSTEMD_SERVICE) -> None:
        self.service = service

    async def _run_systemctl(self, action: str) -> None:
        """Run ``sudo systemctl <action> <service>`` and surface failures.

        A non-zero exit (unit missing, sudo denied, …) must not be swallowed
        silently: the caller (switch_model / unload / stop) needs to know the
        backend did not actually start or stop.
        """
        proc = await asyncio.create_subprocess_exec(
            "sudo", "systemctl", action, self.service,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip() or "(no stderr)"
            raise RuntimeError(
                f"systemctl {action} {self.service} failed "
                f"(rc={proc.returncode}): {detail}"
            )

    async def start(self) -> None:
        await self._run_systemctl("start")

    async def stop(self) -> None:
        await self._run_systemctl("stop")

    async def restart_count(self) -> int:
        """Read ``NRestarts`` from systemd for the llama-server unit."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "show", self.service, "--property=NRestarts", "--no-pager",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            # Output like: NRestarts=16
            val = stdout.decode().strip().split("=")[-1]
            return int(val)
        except Exception:  # noqa: BLE001
            return 0

    async def is_failed(self) -> bool:
        """Return True when the systemd unit is in a failed state."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "is-failed", self.service,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return stdout.decode().strip() == "failed"
        except Exception:  # noqa: BLE001
            return False

    async def crash_error(self) -> str:
        """Extract relevant error lines from journalctl for the last run."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "journalctl", "-u", self.service, "-n", "120", "--no-pager", "-o", "cat",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            lines = stdout.decode().strip().splitlines()
            return self._extract_crash_error_from_lines(lines)
        except Exception as e:  # noqa: BLE001
            return f"Failed to read crash logs: {e}"

    async def service_exit_code(self) -> int | None:
        """Read ``ExecMainStatus`` from systemd (last run's exit code)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "show", self.service, "--property=ExecMainStatus", "--no-pager",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            # Output like: ExecMainStatus=1
            val = stdout.decode().strip().split("=")[-1]
            return int(val)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _extract_crash_error_from_lines(lines: list[str]) -> str:
        """Summarize the most relevant llama-server crash lines from logs."""
        error_keywords = [
            "cudamalloc failed",
            "cuda error",
            "out of memory",
            "failed to load model",
            "failed to allocate",
            "failed to fit params to free device memory",
            "cannot meet free memory targets",
            "failed to initialize the context",
            "failed to allocate compute pp buffers",
            "error loading model",
            "unknown model architecture",
            "alloc_tensor_range: failed",
            "graph_reserve: failed",
            "segmentation fault",
            "core dumped",
            "exiting due to",
        ]

        error_lines: list[str] = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            lower = line.lower()
            if any(keyword in lower for keyword in error_keywords) and (
                not error_lines or error_lines[-1] != line
            ):
                error_lines.append(line)

        if error_lines:
            return " | ".join(error_lines[-6:])
        return "Unknown error (no recognizable error pattern in logs)"


class DirectServerProcess(ServerProcess):
    """Spawn ``LLAMA_SERVER_BIN`` with the args caretaker just wrote.

    Reads ``CURRENT_MODEL_ARGS_FILE`` (written by ``_write_server_args``) and
    splits it into an argv array for ``create_subprocess_exec`` (no shell).
    The process runs in its own session/group so ``stop`` can terminate the
    whole group (covering worker children too). Used for tests and as a
    non-systemd fallback; the Windows impl lands in Phase E.

    Crash introspection is unavailable (no systemd/journalctl) → the abstract
    defaults (0/False/"Unknown") apply.
    """

    def __init__(self, binary: str = str(LLAMA_SERVER_BIN)) -> None:
        self.binary = binary
        self.process: asyncio.subprocess.SubprocessTransport | None = None
        self._proc: Any | None = None

    async def start(self) -> None:
        args_text = CURRENT_MODEL_ARGS_FILE.read_text().strip()
        argv = [self.binary, *shlex.split(args_text)]
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )

    async def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.returncode is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception:  # noqa: BLE001
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except Exception:  # noqa: BLE001
                    proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except TimeoutError:
                    pass
        self._proc = None


def _default_server_process() -> ServerProcess:
    """Return the default ServerProcess on this platform.

    systemd is the default on Linux (production). Windows (F6, Phase E) gets
    :class:`WindowsDirectServerProcess` (spawns llama-server.exe directly,
    tree-killed via taskkill — no killpg/systemd there).
    ``DirectServerProcess`` remains the non-systemd Linux fallback and the
    test double.
    """
    if sys.platform == "win32":
        from .windows_process import WindowsDirectServerProcess

        return WindowsDirectServerProcess()
    return SystemdServerProcess()


def _parse_server_url(url: str = "") -> tuple[str, int | None]:
    """Split a server URL into (host, port) for ``--host``/``--port``.

    e.g. ``http://127.0.0.1:11440`` → ``("127.0.0.1", 11440)``. A missing port
    yields None (in which case `--port` is omitted). Defaults to the current
    ``SERVER_URL`` value (env honored at call time).
    """
    from urllib.parse import urlparse

    parsed = urlparse(url or server_url())
    host = parsed.hostname or "127.0.0.1"
    return host, parsed.port


def _yaml_vram_limit_mb(config_path: str | None) -> int | None:
    """Read an optional ``vram_limit_mb`` from the models YAML root.

    A caretaker may declare its VRAM budget at the top of its own
    ``models.local.settings.yaml`` (the same document that holds ``models``).
    ``None`` (no field / unreadable / non-numeric) means "not configured" →
    the caretaker enforces no VRAM slot. Best-effort: a parse error never
    blocks construction.
    """
    import yaml as _yaml

    try:
        path = Path(config_path) if config_path else config_mod.models_file_path()
        with path.open(encoding="utf-8") as fh:
            data = _yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            return None
        raw = data.get("vram_limit_mb")
    except Exception:  # noqa: BLE001 - best-effort config read
        return None
    if raw is None:
        return None
    try:
        val = int(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring non-integer vram_limit_mb=%r in %s", raw, path)
        return None
    return val


class Caretaker:
    """Owns the local llama-server lifecycle behind the control API.

    Instantiated by ``caretaker.server`` once the phases wire it to the
    ``/status`` / ``/ensure`` / ``/unload`` routes. ``config_path`` is
    optional (mirroring guardian ``ModelManager.__init__(config_path)``);
    when omitted the models file resolves via ``CARETAKER_MODELS_FILE``.
    ``server_process`` is injectable (default ``SystemdServerProcess``).
    """

    def __init__(
        self,
        config_path: str | None = None,
        server_process: ServerProcess | None = None,
        *,
        health_polls: int = DEFAULT_HEALTH_POLLS,
        health_interval: float = DEFAULT_HEALTH_INTERVAL,
        server_url: str = SERVER_URL,
        vram_limit_mb: int | None = None,
        check_vram: bool = True,
    ) -> None:
        self.config_path = Path(config_path) if config_path else config_mod.models_file_path()
        loaded = config_mod.load_models_config(self.config_path)
        self.models: dict[str, dict] = dict(loaded["models"])
        self.server_process = server_process or _default_server_process()
        self.server_url = server_url
        self.health_polls = health_polls
        self.health_interval = health_interval

        # VRAM slot (Phase C): a single-per-caretaker scheduler serializes model
        # loads within a configurable budget. ``vram_limit_mb`` is None (no field
        # in the settings YAML) or enforced is off → no VRAM slot (None), so
        # switch_model skips acquire/release entirely.
        if check_vram and vram_limit_mb is None:
            vram_limit_mb = _yaml_vram_limit_mb(config_path)
        self.vram: VramScheduler | None = (
            VramScheduler(vram_limit_mb) if check_vram and vram_limit_mb else None
        )

        self.current_model: str | None = None
        self.current_vision_enabled: bool = False
        self.is_unloaded: bool = False
        # Unix-epoch of the last successful model load (None when unloaded /
        # never loaded). The caretaker sees no request traffic, so this is a
        # load-age proxy: the gateway's own ``last_request_time`` is the
        # authoritative idle signal (Phase D contract — decision in gateway).
        self._loaded_at: float | None = None
        self.crash_history: list[CrashRecord] = []
        self.last_crash: CrashRecord | None = None

        # Watchdog bookkeeping (Phase C). The watchdog is not started here; the
        # operator starts it via ``start_watchdog()`` (F5-deploy wiring).
        self._watchdog_task: asyncio.Task | None = None
        self._watchdog_interval: float = DEFAULT_WATCHDOG_INTERVAL
        self._watchdog_initial_backoff: float = DEFAULT_WATCHDOG_INITIAL_BACKOFF
        self._watchdog_max_backoff: float = DEFAULT_WATCHDOG_MAX_BACKOFF
        self._watchdog_backoff: float = DEFAULT_WATCHDOG_INITIAL_BACKOFF
        self._watchdog_running: bool = False
        # Model the watchdog keeps retrying after a failed forced restart
        # (switch_model clears ``current_model`` on failure; this remembers the
        # target so a crash-looping model is retried with backoff instead of
        # being abandoned after one attempt).
        self._watchdog_retry_model: str | None = None
        # True while a switch/load/unload/stop is running, so the watchdog does
        # not race an in-flight lifecycle transition.
        self._switch_in_progress: bool = False

        # Simple per-model vision-validation reset map (the authoritative vision
        # cache stays in the gateway; caretaker only needs a no-op/bookkeeping
        # reset surface for Phase A).
        self._vision_validations: dict[str, str] = {}

    # ---------------------------------------------------------------- helpers
    def _resolve_vision_mmproj(self, config: dict[str, Any]) -> str | None:
        """Return the mmproj path used for vision runtime, if any."""
        mmproj = str(config.get("vision_mmproj") or config.get("mmproj") or "").strip()
        return mmproj or None

    def _resolve_runtime_value(self, config: dict[str, Any], key: str, *, enable_vision: bool) -> Any:
        """Return the effective runtime value for text or vision mode."""
        override_key = f"vision_{key}" if enable_vision else f"text_{key}"
        override_value = config.get(override_key)
        if override_value not in (None, ""):
            return override_value
        return config.get(key)

    def _resolve_runtime_vision_flag(self, model_name: str, enable_vision: bool | None) -> bool:
        """Resolve whether a load/switch should start the model with mmproj."""
        config = self.models.get(model_name, {})
        if not self._resolve_vision_mmproj(config):
            return False
        if enable_vision is None:
            if model_name == self.current_model:
                return self.current_vision_enabled
            return False
        return bool(enable_vision)

    def build_runtime_config(
        self,
        model_name: str,
        *,
        enable_vision: bool | None = None,
        context_hint: int | None = None,
    ) -> dict[str, Any]:
        """Build the effective runtime config for text or vision mode.

        Identical to the gateway's ``ModelRegistry.build_runtime_config``:
        deepcopy of the model config, vision flag, context/ngl via
        ``_resolve_runtime_value``, context-hint clamp, ``tensor_split`` ''
        pop, mmproj set/pop per vision.
        """
        if model_name not in self.models:
            raise ValueError(f"Model {model_name} not found in configuration")

        runtime_config = copy.deepcopy(self.models[model_name])
        vision_enabled = self._resolve_runtime_vision_flag(model_name, enable_vision)

        runtime_config["context"] = self._resolve_runtime_value(runtime_config, "context", enable_vision=vision_enabled)
        runtime_config["ngl"] = self._resolve_runtime_value(runtime_config, "ngl", enable_vision=vision_enabled)

        # Client context hint: clamp to a safe range. Always cap at the configured
        # context (never enlarge beyond config — clients can't grow the model's KV).
        # Floor at 4096 (llama-server requires a sane minimum).
        if context_hint is not None:
            cfg_ctx = runtime_config.get("context") or 4096
            hinted = max(4096, min(int(context_hint), int(cfg_ctx)))
            runtime_config["context"] = hinted

        tensor_split = self._resolve_runtime_value(runtime_config, "tensor_split", enable_vision=vision_enabled)
        if tensor_split not in (None, ""):
            runtime_config["tensor_split"] = tensor_split
        else:
            runtime_config.pop("tensor_split", None)

        if vision_enabled:
            mmproj = self._resolve_vision_mmproj(runtime_config)
            if mmproj:
                runtime_config["mmproj"] = mmproj
        else:
            runtime_config.pop("mmproj", None)

        return runtime_config

    def _build_crash_config_snapshot(
        self,
        model_name: str,
        *,
        runtime_config: dict[str, Any] | None = None,
        vision_enabled: bool | None = None,
    ) -> dict[str, Any]:
        """Capture both the configured profile and resolved runtime shape for crash reports."""
        snapshot = copy.deepcopy(self.models.get(model_name, {}))
        if vision_enabled is not None:
            snapshot["runtime_mode"] = "vision" if vision_enabled else "text"
        if runtime_config is not None:
            snapshot["effective_runtime_config"] = copy.deepcopy(runtime_config)
        return snapshot

    # ------------------------------------------------------------- args build
    def _build_args_string(self, config: dict[str, Any]) -> tuple[str, dict[str, str]]:
        """Build the llama-server CLI args string + env vars from a runtime config.

        MUST be byte-identical to the guardian's ``_build_args_string`` (the single
        source of truth for both launch and signature). The ``--host``/``--port``
        literals derive from ``SERVER_URL`` (default ``127.0.0.1:11440``) and the
        slot path from ``LLAMA_SLOTS_DIR``; the flags themselves stay flags.
        """
        path = config["path"]
        ctx = config.get("context", 4096)
        ngl = config.get("ngl", 99)
        kv_type = config.get("kv_type", "q4_0")
        tensor_split = config.get("tensor_split", "")
        mmproj = config.get("mmproj", "")
        extra_args = config.get("extra_args", "")
        cuda_visible_devices = config.get("cuda_visible_devices", "")
        # DFlash / speculative-decoding draft model (llama-server b2111+)
        draft_model_path = str(config.get("draft_model_path", "")).strip()
        spec_type = str(config.get("spec_type", "draft-dflash")).strip()
        spec_draft_n_max = config.get("spec_draft_n_max", 8)
        spec_draft_n_min = config.get("spec_draft_n_min", 1)
        draft_cache_type_k = str(config.get("draft_cache_type_k", "f16")).strip()
        draft_cache_type_v = str(config.get("draft_cache_type_v", "f16")).strip()

        host, port = _parse_server_url(self.server_url)
        logger.info(f"Using official llama.cpp binary: {LLAMA_SERVER_BIN}")

        # Build args string
        slots_dir = llama_slots_dir()
        port_arg = f" --port {port}" if port is not None else ""
        args_content = (
            f"-m {path} -c {ctx} -ngl {ngl} -ctk {kv_type} -ctv {kv_type} "
            f"--host {host}{port_arg} --slot-save-path {slots_dir} --load-mode none"
        )

        # Multi-GPU weight distribution (e.g. "0.55,0.45" for 2 GPUs)
        if tensor_split:
            args_content += f" --tensor-split {tensor_split}"
            logger.info(f"Tensor split: {tensor_split}")

        # Vision-language projector (required for VL/multimodal models)
        if mmproj:
            mmproj_path = Path(mmproj)
            if not mmproj_path.exists():
                logger.error(f"❌ mmproj file not found: {mmproj} — vision input will NOT work!")
            else:
                args_content += f" --mmproj {mmproj}"
                logger.info(f"🖼️  mmproj: {mmproj}")

        # DFlash / speculative-decoding draft model (llama.cpp b2111+).
        # If draft_model_path is set and exists, llama-server will use --spec-type
        # with --model-draft to draft N tokens at a time before main-model verification.
        if draft_model_path:
            draft_path = Path(draft_model_path)
            if not draft_path.exists():
                logger.warning(
                    f"⚠️  draft_model_path set but file missing: {draft_model_path} — "
                    "speculative decoding will be DISABLED"
                )
            else:
                args_content += (
                    f" --spec-type {spec_type}"
                    f" --model-draft {draft_model_path}"
                    f" --spec-draft-n-max {spec_draft_n_max}"
                    f" --spec-draft-n-min {spec_draft_n_min}"
                    f" --cache-type-k-draft {draft_cache_type_k}"
                    f" --cache-type-v-draft {draft_cache_type_v}"
                )
                logger.info(
                    f"⚡ Speculative decoding enabled: spec_type={spec_type}, "
                    f"draft={draft_path.name}, n_max={spec_draft_n_max}, n_min={spec_draft_n_min}"
                )
        elif spec_type not in ("", "none", "draft-dflash"):
            # Speculative decoding WITHOUT an external draft model: either native
            # MTP layers (draft-mtp) or n-gram lookup (ngram-simple/ngram-map-k/
            # ngram-mod/ngram-cache). Emit only --spec-type.
            args_content += f" --spec-type {spec_type}"
            logger.info(f"⚡ Speculative decoding enabled (no draft): spec_type={spec_type}")
        elif spec_type == "draft-dflash":
            # draft-dflash requires an external draft model; without one it cannot
            # launch — treat as a config error and emit nothing. Note: the spec_type
            # default IS "draft-dflash", so only warn when it was explicitly set.
            if "spec_type" in config:
                logger.warning(
                    "⚠️  spec_type=draft-dflash without draft_model_path — "
                    "speculative decoding DISABLED (draft-dflash needs --model-draft)"
                )

        # Optional per-model parallel slot count (--parallel). Higher slot counts
        # pair naturally with client-hinted smaller contexts. Part of args_content,
        # so a n_slots change also triggers launch-signature drift.
        n_slots = config.get("n_slots")
        if n_slots and int(n_slots) > 1:
            args_content += f" --parallel {int(n_slots)}"
            logger.info(f"Parallel slots: {n_slots}")

        # Pass-through for any extra flags not covered above
        if extra_args:
            args_content += f" {extra_args}"
            logger.info(f"Extra args: {extra_args}")

        # Optional per-model GPU pinning for the systemd launch wrapper.
        # scripts/start_llama.sh sources current_model.env before launching llama-server.
        env_dict: dict[str, str] = {}
        cuda_visible_devices = str(cuda_visible_devices).strip()
        if cuda_visible_devices:
            env_dict["CUDA_VISIBLE_DEVICES"] = cuda_visible_devices
        return args_content, env_dict

    def _write_server_args(self, config: dict) -> None:
        """Build llama-server CLI arguments from model config and write to args file.

        Supported config keys (from models.yaml): path, context, ngl, kv_type,
        tensor_split, mmproj, extra_args, cuda_visible_devices, draft_model_path,
        spec_type, spec_draft_n_max, spec_draft_n_min, draft_cache_type_k,
        draft_cache_type_v.
        """
        args_file = CURRENT_MODEL_ARGS_FILE
        env_file = CURRENT_MODEL_ENV_FILE

        args_content, env_dict = self._build_args_string(config)

        args_file.parent.mkdir(parents=True, exist_ok=True)
        with open(args_file, "w") as f:
            f.write(args_content)

        # Optional per-model GPU pinning for the systemd launch wrapper.
        # scripts/start_llama.sh sources current_model.env before launching llama-server.
        if env_dict:
            with open(env_file, "w") as f:
                f.write(f"export CUDA_VISIBLE_DEVICES={env_dict['CUDA_VISIBLE_DEVICES']}\n")
            logger.info(f"CUDA_VISIBLE_DEVICES={env_dict['CUDA_VISIBLE_DEVICES']}")
        elif env_file.exists():
            env_file.unlink()
            logger.info("Cleared model environment file (no CUDA_VISIBLE_DEVICES override)")

    # --------------------------------------------------- context save/restore
    async def _save_context(self, filename: str) -> None:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.server_url}/slots/0?action=save",
                    json={"filename": filename},
                    timeout=30.0,
                )
                if resp.status_code == 200:
                    logger.info(f"Auto-saved context to {filename}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to auto-save context: {e}")

    async def _load_context(self, filename: str) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.server_url}/slots/0?action=restore",
                json={"filename": filename},
                timeout=60.0,
            )
            if resp.status_code == 200:
                logger.info(f"Auto-restored context from {filename}")
            else:
                raise RuntimeError("Restore failed")

    # ------------------------------------------------------- GPU memory / etc.
    async def _free_gpu_memory(self) -> None:
        """Ask coexisting GPU services to release VRAM before loading a model.

        Instead of killing processes, this asks services politely via their APIs:
        - ComfyUI: POST /free {"unload_models": true, "free_memory": true}
        - Frigate: NEVER touched (cameras are sacred)
        Any unknown GPU processes are logged but left alone.
        """
        logger.info("🧹 Requesting GPU memory release from coexisting services...")

        await self._request_comfyui_free()

        # Log remaining GPU consumers for visibility. Kept as a brief blocking call
# (mirrors the guardian); the event loop stall is a negligible best-effort
# report, so ASYNC221 is waived for this one call.
        try:
            result = subprocess.run(  # noqa: ASYNC221
                ["nvidia-smi", "--query-compute-apps=pid,process_name,used_gpu_memory",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                for line in result.stdout.strip().splitlines():
                    logger.info(f"📊 GPU process: {line.strip()}")
        except Exception:  # noqa: BLE001
            logger.debug("nvidia-smi not available or failed — skipping GPU-consumer report")

    def _get_comfyui_url(self) -> str:
        """Read the ComfyUI URL from the caretaker's config/env, else default."""
        return config_mod.comfyui_url(self.config_path)

    async def _request_comfyui_free(self) -> None:
        """Ask ComfyUI to unload all models and free GPU memory via its API."""
        comfyui_url = self._get_comfyui_url()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{comfyui_url}/free",
                    json={"unload_models": True, "free_memory": True},
                )
                if resp.status_code == 200:
                    logger.info("✅ ComfyUI released GPU memory (models unloaded)")
                    # Give CUDA a moment to actually release the memory
                    await asyncio.sleep(1)
                else:
                    logger.warning(f"⚠️ ComfyUI /free returned HTTP {resp.status_code}")
        except httpx.ConnectError:
            logger.info("ℹ️ ComfyUI not running — no memory to free")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"⚠️ Failed to request ComfyUI memory free: {e}")

    # ------------------------------------------------------------ signatures
    def _compute_launch_signature(
        self,
        model_name: str,
        *,
        enable_vision: bool | None,
        context_hint: int | None = None,
        config_vision: bool | None = None,
    ) -> dict | None:
        """Compute the launch signature for a model+vision-mode from current models.yaml.

        Returns None if the model is unknown. Uses build_runtime_config (so
        vision/text overrides and the client context hint resolve correctly).

        ``config_vision`` (internal): force the vision flag to the model's
        config *default* without the loaded-runtime shortcut — pass
        ``False``/``True`` when the caller wants drift against the config
        default (health/needs_reload), not the currently loaded mode."""
        if model_name not in self.models:
            return None
        # config_vision=True → the model's config *default* vision (mmproj
        # present), independent of the loaded runtime mode: pass enable_vision
        # through (None → default) but force vision from config; for the args
        # builder use None when no mmproj (no vision) or None with the forced
        # True only when mmproj exists.
        if config_vision is True:
            vision = bool(self._resolve_vision_mmproj(self.models.get(model_name, {})))
        elif config_vision is False:
            vision = False
        else:
            vision = bool(self._resolve_runtime_vision_flag(model_name, enable_vision))
        runtime_config = self.build_runtime_config(
            model_name,
            enable_vision=None if config_vision is None else (True if vision else None),
            context_hint=context_hint,
        )
        args_str, env_dict = self._build_args_string(runtime_config)
        return {
            "model": model_name,
            "vision": vision,
            "args_sha256": hashlib.sha256(args_str.encode("utf-8")).hexdigest(),
            "env_sha256": hashlib.sha256(json.dumps(env_dict, sort_keys=True).encode("utf-8")).hexdigest(),
        }

    def _read_persisted_signature(self) -> dict | None:
        try:
            text = CURRENT_MODEL_SIG_FILE.read_text()
            return json.loads(text)
        except Exception:  # noqa: BLE001
            return None

    def _write_persisted_signature(self, sig: dict) -> None:
        try:
            CURRENT_MODEL_SIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CURRENT_MODEL_SIG_FILE.write_text(json.dumps(sig, sort_keys=True))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to persist launch signature: {e}")

    def _config_drifted(
        self,
        model_name: str,
        *,
        enable_vision: bool | None,
        context_hint: int | None = None,
        config_vision: bool | None = None,
    ) -> bool:
        """True if the model must be reloaded to apply current models.yaml settings.

        Drift = persisted sig missing, OR model/vision differ, OR args/env hash differ.
        context_hint is folded into the computed signature, so a client-hinted
        context different from the persisted one counts as drift (triggers reload)."""
        persisted = self._read_persisted_signature()
        if not persisted:
            return True
        current = self._compute_launch_signature(
            model_name,
            enable_vision=enable_vision,
            context_hint=context_hint,
            config_vision=config_vision,
        )
        if not current:
            return True
        return persisted != current

    def check_drift(
        self,
        model_name: str,
        *,
        enable_vision: bool | None = None,
        context_hint: int | None = None,
    ) -> bool:
        """Report whether ``model_name`` must be reloaded to apply current config.

        Pure reporting wrapper around :meth:`_config_drifted` (no side-effects):
        ``True`` when the persisted launch signature is missing, or the model /
        vision / args / env (incl. the client ``context_hint``) no longer match
        the current config; ``False`` when identical. ``enable_vision`` semantics
        match :meth:`switch_model` (None resolves the running/default mode).

        Raises :class:`ValueError` when ``model_name`` is not configured, the
        same contract as :meth:`switch_model`.
        """
        if model_name not in self.models:
            raise ValueError(f"Model {model_name} not found in configuration")
        return self._config_drifted(
            model_name, enable_vision=enable_vision, context_hint=context_hint
        )

    def _config_vision_default(self, model_name: str) -> bool:
        """The model's configured vision default (mmproj present → True)."""
        return bool(self._resolve_vision_mmproj(self.models.get(model_name, {})))

    # ------------------------------------------------------------ health/crash
    async def _wait_for_health(self, model_name: str = "") -> bool:
        """Poll llama-server health endpoint. True if healthy, False if crashed.

        Detects crashes by monitoring the ServerProcess restart counter; if it
        climbs past a threshold the service is crash-looping.
        """
        initial_restarts = await self.server_process.restart_count()
        max_crash_restarts = 3  # If service restarts 3+ times, it's definitely broken

        for i in range(self.health_polls):
            if await self.server_process.health_ok(self.server_url):
                logger.info(f"✅ Server healthy after {i}s (model: {model_name})")
                return True

            # Every 5 seconds, check if the service is crash-looping
            if i > 3 and i % 5 == 0:
                current_restarts = await self.server_process.restart_count()
                restart_delta = current_restarts - initial_restarts
                if restart_delta >= max_crash_restarts:
                    logger.error(
                        f"❌ llama-server crash-looping ({restart_delta} restarts) "
                        f"while loading '{model_name}'"
                    )
                    return False

                # Also check if service entered failed state (Restart=on-failure with limit)
                if await self.server_process.is_failed():
                    logger.error(f"❌ llama-server service failed while loading '{model_name}'")
                    return False

            await asyncio.sleep(self.health_interval)

        logger.error(f"❌ Server health timeout after {self.health_polls}s for '{model_name}'")
        return False

    async def _detect_crash(
        self, model_name: str, config_snapshot: dict[str, Any] | None = None
    ) -> CrashRecord:
        """Extract error details from the ServerProcess's crash log source and record the crash."""
        error_msg = await self.server_process.crash_error()
        config_snap = copy.deepcopy(config_snapshot) if config_snapshot is not None else self.models.get(model_name, {}).copy()

        crash = CrashRecord(
            timestamp=datetime.now(UTC).isoformat(),
            model=model_name,
            error_message=error_msg,
            exit_code=await self.server_process.service_exit_code(),
            config_snapshot=config_snap,
        )

        self.last_crash = crash
        self.crash_history.append(crash)
        if len(self.crash_history) > MAX_CRASH_HISTORY:
            self.crash_history = self.crash_history[-MAX_CRASH_HISTORY:]

        runtime_mode = config_snap.get("runtime_mode") if isinstance(config_snap, dict) else None
        effective = config_snap.get("effective_runtime_config") if isinstance(config_snap, dict) else None
        logger.error(
            "💥 Crash recorded: model=%s runtime_mode=%s effective_runtime=%s error=%s",
            model_name,
            runtime_mode or "unknown",
            effective or {},
            error_msg,
        )

        # Stop the service to prevent restart loops (best-effort: a failing
        # stop during crash handling must not swallow the original crash).
        try:
            await self.server_process.stop()
        except Exception:  # best-effort on the crash path
            logger.debug("Failed to stop service during crash handling", exc_info=True)

        return crash

    async def _fetch_props(self) -> dict[str, Any] | None:
        """Fetch llama-server ``GET /props`` with a short timeout.

        Returns the parsed JSON object, or ``None`` when the backend is
        unreachable, answers non-200, or returns an unparseable/non-object
        body. The caller decides whether that is fatal — under strict
        verification it is: success must be proven, never assumed.
        """
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_PROPS_TIMEOUT) as client:
                resp = await client.get(f"{self.server_url}/props")
        except Exception:  # noqa: BLE001 - any transport failure means "unverified"
            return None
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 - unparseable body means "unverified"
            return None
        return data if isinstance(data, dict) else None

    async def _verify_loaded_model(self, model_name: str) -> str:
        """Strict post-load verification: /props must report the expected model path.

        Compares the backend-reported model path (``model_path`` field, with
        ``model`` as legacy fallback) against the configured path for
        ``model_name`` and returns the actually-served path on success.

        Raises :class:`ModelMismatchError` when the backend provably serves a
        different model, or when /props cannot be consulted at all — a
        fail-open here is exactly what let the 2026-09-01 incident confirm
        ``/ensure`` success while llama-server still served the previous
        model. Callers poll /health first (bounded), so a single
        short-timeout /props call suffices once the backend answers.
        """
        expected = str(self.models.get(model_name, {}).get("path") or "")
        if not expected:
            raise ModelMismatchError(
                model_name,
                None,
                None,
                message=f"no configured model path to verify against for '{model_name}'",
            )
        data = await self._fetch_props()
        if data is None:
            raise ModelMismatchError(
                model_name,
                expected,
                None,
                message=f"backend verification unavailable: GET {self.server_url}/props failed",
            )
        actual = data.get("model_path") or data.get("model") or None
        if actual is None:
            raise ModelMismatchError(
                model_name,
                expected,
                None,
                message="backend /props did not expose a model path",
            )
        actual = str(actual)
        if self._model_paths_diverge(actual, expected):
            raise ModelMismatchError(model_name, expected, actual)
        logger.info(f"✅ Backend model verified: {model_name} ({Path(actual).name})")
        return actual

    @staticmethod
    def _model_paths_diverge(actual: str, expected: str) -> bool:
        """True when the /props model path and the configured path differ.

        Compared on :func:`os.path.realpath` (after ``~``-expansion) rather
        than strict string equality: llama-server may echo the launched path
        in resolved form while the configured path uses a symlink, relative
        segment or trailing-slash variant — the same file must verify OK,
        otherwise a correctly loaded model is reported as model_mismatch and
        /ensure burns a pointless stop/start retry before 503-ing. Only a
        genuinely different file counts as a mismatch. Falls back to raw
        string equality if the filesystem cannot resolve a path.
        """
        try:
            actual_resolved = os.path.realpath(os.path.expanduser(actual))
            expected_resolved = os.path.realpath(os.path.expanduser(expected))
        except Exception:
            return actual != expected
        return actual_resolved != expected_resolved

    async def _retry_verified_start(
        self,
        model_name: str,
        *,
        desired_vision: bool,
        target_config: dict[str, Any],
    ) -> None:
        """One bounded stop/start cycle + wait + verify after a failed verification.

        The launch args for ``model_name`` are already written (the launcher
        re-reads them on start), so the retry is exactly stop → start → wait
        for health → verify — the bounded single retry for a launcher race
        that left the old model running after the first start.

        Raises :class:`ModelLoadError` (crash-recorded, active-model
        bookkeeping cleared) when the retried backend never becomes healthy,
        and :class:`ModelMismatchError` (bookkeeping cleared — nothing is
        verified-loaded) when the second verification also fails.
        """
        await self.server_process.stop()
        await self.server_process.start()
        if not await self._wait_for_health(model_name):
            # The old model is no longer running: clear the "active model"
            # bookkeeping so a later same-model ensure/switch actually
            # restarts llama-server instead of short-circuiting as
            # "already active" (same contract as the primary load path).
            self.current_model = None
            self.current_vision_enabled = False
            crash = await self._detect_crash(
                model_name,
                config_snapshot=self._build_crash_config_snapshot(
                    model_name,
                    runtime_config=target_config,
                    vision_enabled=desired_vision,
                ),
            )
            raise ModelLoadError(
                f"Model '{model_name}' failed to load: {crash.error_message}",
                crash_record=crash,
            )
        try:
            await self._verify_loaded_model(model_name)
        except ModelMismatchError:
            # Truthful final state: the backend does not provably serve the
            # target model even after the bounded retry. Clear the active-model
            # bookkeeping so /status reports needs_reload instead of a model
            # the backend is not actually serving.
            self.current_model = None
            self.current_vision_enabled = False
            raise

    # ------------------------------------------------------------- orchestrator
    async def switch_model(
        self,
        model_name: str,
        *,
        enable_vision: bool | None = None,
        context_hint: int | None = None,
        force: bool = False,
    ) -> bool:
        """Swap the running llama-server to ``model_name``.

        The gateway keeps registry/choice/pinning/switch-allowlist; here we only
        execute: no-op when the same model+vision is already active, not
        drifted, and /props verifies the backend really serves it; else
        auto-save context → stop → write args → free GPU memory → start →
        wait for health (crash-aware) → persist signature → verify /props
        (strict; one bounded stop/start retry on mismatch) → restore context.

        Returns ``fresh_load``: ``True`` when this call actually (re)started
        llama-server (the backend is fresh — any prior in-memory session state
        is gone and the gateway may restore the saved context), ``False`` when
        the request was a no-op fast-path (the live session is authoritative —
        nothing was restarted, so restoring a saved context would clobber the
        live one).

        Raises :class:`ModelLoadError` when the backend fails to become
        healthy, and :class:`ModelMismatchError` when GET /props cannot prove
        the backend serves ``model_name`` (after the bounded retry) — the
        caller must never report success in that case (2026-09-01 incident:
        /ensure confirmed a model while llama-server still served the previous
        one).
        """
        if model_name not in self.models:
            raise ValueError(f"Model {model_name} not found in configuration")

        desired_vision = self._resolve_runtime_vision_flag(model_name, enable_vision)
        drifted = self._config_drifted(model_name, enable_vision=desired_vision, context_hint=context_hint)
        if (
            not force
            and model_name == self.current_model
            and desired_vision == self.current_vision_enabled
            and not drifted
        ):
            # The no-op is only truthful when the backend actually lives: a
            # crash between watchdog ticks (or a KillMode=control-group stop)
            # can leave ``current_model`` set with a dead llama-server.
            # Report "already active" then would lie to the gateway
            # (``ok: true, fresh_load: false`` on a dead backend) — refuse the
            # no-op and fall through to the real (re)load path so the ensure
            # heals the backend instead.  Uses ``self.server_url`` (call-time
            # resolved, same as _wait_for_health and the watchdog tick) — NOT
            # the import-time SERVER_URL default — so the gate probes the same
            # endpoint the rest of the flow probes.
            if await self.server_process.health_ok(self.server_url):
                # The no-op is only truthful when the backend actually lives AND
                # provably serves the requested model: a crash between watchdog
                # ticks (or a KillMode=control-group stop) can leave
                # ``current_model`` set with a dead llama-server, and a launcher
                # race can leave it alive but serving the WRONG model (the
                # 2026-09-01 incident: bookkeeping said qwen, /props showed
                # gemma). Either way the no-op would lie to the gateway
                # (``ok: true, fresh_load: false`` on a wrong backend) — refuse
                # it and fall through to the real (re)load path so the ensure
                # heals the backend instead.  Uses ``self.server_url`` (call-time
                # resolved, same as _wait_for_health and the watchdog tick) — NOT
                # the import-time SERVER_URL default — so the gate probes the same
                # endpoint the rest of the flow probes.
                try:
                    await self._verify_loaded_model(model_name)
                except ModelMismatchError as exc:
                    logger.warning(
                        f"Model {model_name} is marked active but the backend "
                        f"serves a different model ({exc}) — reloading to heal "
                        "(no-op refused)"
                    )
                else:
                    self.is_unloaded = False  # a live server is active; not unloaded
                    logger.info(f"Model {model_name} is already active")
                    return False
            else:
                logger.warning(
                    f"Model {model_name} is marked active but the backend does not "
                    "answer health — reloading to heal (no-op refused)"
                )
        if drifted:
            logger.info(
                f"🔄 Config drift detected for '{model_name}' "
                "(settings changed in models.yaml) — reloading to apply new settings"
            )

        # Mark a lifecycle transition in flight so the watchdog does not race
        # this load. The no-op fast-path above returns early, so this only runs
        # on a real (re)load. ``_switch_in_progress`` and the config build live
        # INSIDE the try so a raise in either (e.g. a malformed model entry in
        # build_runtime_config) still clears the flag via the finally — the
        # watchdog must never be permanently disabled by a stuck transition.
        try:
            self._switch_in_progress = True
            target_config = self.build_runtime_config(
                model_name, enable_vision=desired_vision, context_hint=context_hint
            )
            # VRAM slot: a swap A→B frees A's slot *before* acquiring B — this
            # switch stops the old model itself, so waiting for space only this
            # switch could free would deadlock forever. A same-model
            # reload/restart (drift, force) keeps the slot it already holds, so
            # it never double-acquires. Inside the try so a fail-fast (model
            # bigger than the whole budget) can never leave
            # ``_switch_in_progress`` stuck.
            held_model = self.current_model if self.vram is not None else None
            if self.vram is not None:
                if held_model is not None and held_model != model_name:
                    await self.vram.release(held_model)
                if held_model != model_name:
                    await self.vram.acquire(
                        model_name,
                        get_model_size_mb(model_name, self.models.get(model_name)),
                    )

            logger.info(
                "Switching from %s [%s] to %s [%s]",
                self.current_model,
                "vision" if self.current_vision_enabled else "text",
                model_name,
                "vision" if desired_vision else "text",
            )
            # 1. Auto-save current context (only if a model is actually loaded —
            #    on the first switch current_model is None, so nothing to save).
            if self.current_model is not None:
                await self._save_context(f"auto_save_{self.current_model}")

            # 2. Stop llama-server. The old model is no longer running: clear the
            #    active-model bookkeeping NOW, so a later exception (args write,
            #    GPU free, start) cannot leave a stale "old model active" state.
            self.current_model = None
            self.current_vision_enabled = False
            self.is_unloaded = False
            await self.server_process.stop()

            # 3. Write new model args
            logger.info(
                "Runtime config for %s [%s]: context=%s ngl=%s split=%s mmproj=%s",
                model_name,
                "vision" if desired_vision else "text",
                target_config.get("context"),
                target_config.get("ngl"),
                target_config.get("tensor_split") or "auto",
                target_config.get("mmproj") or "none",
            )
            self._write_server_args(target_config)

            # 4. Free GPU memory
            await self._free_gpu_memory()

            # 5. Start llama-server
            await self.server_process.start()

            # 6. Wait for health with crash detection
            healthy = await self._wait_for_health(model_name)

            if not healthy:
                # The old model is no longer running: clear the "active model"
                # bookkeeping so a later same-model ensure/switch actually
                # restarts llama-server instead of short-circuiting as
                # "already active".
                self.current_model = None
                self.current_vision_enabled = False
                crash = await self._detect_crash(
                    model_name,
                    config_snapshot=self._build_crash_config_snapshot(
                        model_name,
                        runtime_config=target_config,
                        vision_enabled=desired_vision,
                    ),
                )
                raise ModelLoadError(
                    f"Model '{model_name}' failed to load: {crash.error_message}",
                    crash_record=crash,
                )

            self.current_model = model_name
            self.current_vision_enabled = desired_vision
            self.is_unloaded = False
            self._loaded_at = time.time()
            # Persist the launch signature so future same-model requests can
            # detect config drift and reload instead of skipping.
            launch_sig = self._compute_launch_signature(
                model_name, enable_vision=desired_vision, context_hint=context_hint
            )
            if launch_sig is not None:
                self._write_persisted_signature(launch_sig)
            self.reset_vision_validation(model_name)
            logger.info(f"✅ Model '{model_name}' loaded successfully")

            # Post-switch verification — STRICT since the 2026-09-01 incident:
            # /ensure may only report success when GET /props proves the
            # backend serves the requested model (a warn-only check here let a
            # launcher race confirm the old model as "loaded"). On a mismatch,
            # retry ONCE — bounded: one more stop/start cycle + wait + verify —
            # before failing the request.
            try:
                await self._verify_loaded_model(model_name)
            except ModelMismatchError as first:
                logger.warning(
                    f"⚠️ Post-load verification failed for '{model_name}' ({first}) "
                    "— retrying once (stop/start + wait + verify)"
                )
                await self._retry_verified_start(
                    model_name,
                    desired_vision=desired_vision,
                    target_config=target_config,
                )

            # 7. Restore context if exists.
            try:
                await self._load_context(f"auto_save_{model_name}")
            except Exception:  # noqa: BLE001
                logger.info(f"No auto-save found for {model_name}, starting fresh.")
        except BaseException:
            # Restore truthful VRAM accounting for whatever the failure state is:
            # - current_model is still set (failure before the stop, e.g. a
            #   failed acquire on the new model or a build error): the old
            #   server keeps running, so its slot must be restored if it was
            #   released for a swap — otherwise the scheduler under-counts and
            #   a later load can overcommit.
            # - current_model is None (failure after the stop, or cold start):
            #   the new model is not running, so release its slot — a swap
            #   frees the just-acquired slot, a same-model reload frees the
            #   slot it kept (its server is dead; holding it would
            #   phantom-block others), a cold-start failure is a no-op decrement.
            if self.vram is not None:
                if self.current_model is not None:
                    if self.vram.active_counts.get(self.current_model, 0) == 0:
                        await self.vram.acquire(
                            self.current_model,
                            get_model_size_mb(
                                self.current_model,
                                self.models.get(self.current_model),
                            ),
                        )
                else:
                    await self.vram.release(model_name)
            raise
        finally:
            self._switch_in_progress = False

        # Only reached on the successful (re)load path: the backend was freshly
        # started, so in-memory session state is gone and the gateway may
        # restore the target model's saved context.
        return True

    def reset_vision_validation(self, model_name: str) -> None:
        """Reset caretaker-local vision-validation bookkeeping after a load/switch."""
        self._vision_validations.pop(model_name, None)

    async def unload(self) -> None:
        """Stop llama-server to free all VRAM. Guard against double-unload."""
        if self.is_unloaded:
            logger.info("⚡ Already unloaded — nothing to do")
            return
        self._switch_in_progress = True
        previous_model = self.current_model
        try:
            logger.info(f"🔌 Unloading model '{previous_model}' to free VRAM...")
            await self.server_process.stop()
            self.is_unloaded = True
            self.current_model = None
            self.current_vision_enabled = False
            self._loaded_at = None
            # Release the VRAM slot held by the just-stopped model (skip when
            # there was no active model, e.g. none was ever loaded).
            if self.vram is not None and previous_model is not None:
                await self.vram.release(previous_model)
            logger.info("✅ llama-server stopped — VRAM is free")
        finally:
            self._switch_in_progress = False

    # ------------------------------------------- public skeleton surface (Phase A)
    async def spawn(self, model: str) -> None:
        """Start llama-server for ``model`` (Phase A: ``switch_model``)."""
        await self.switch_model(model)

    async def stop(self) -> None:
        """Stop the running llama-server and clear active-model bookkeeping.

        Mirrors unload()'s state transition so a caller using stop() (e.g. a
        Phase C health-check route) never sees a stale 'model active' state
        while the backend is stopped.
        """
        self._switch_in_progress = True
        previous_model = self.current_model
        try:
            await self.server_process.stop()
            # Release the VRAM slot held by the just-stopped model, like unload().
            if self.vram is not None and previous_model is not None:
                await self.vram.release(previous_model)
            self.current_model = None
            self.current_vision_enabled = False
            self.is_unloaded = True
            self._loaded_at = None
        finally:
            self._switch_in_progress = False

    async def reload(self, model: str) -> None:
        """Reload ``model`` (Phase A+B: idempotent, drift-aware swap)."""
        await self.switch_model(model)

    def health(self) -> dict[str, object]:
        """Return a status dict consumed by ``GET /status``.

        ``needs_reload`` is True when the backend cannot serve the current
        config as-is and a ``/ensure`` would be needed first: always when
        unloaded, and when the loaded model's launch signature is drifted
        (see :meth:`check_drift`)."""
        if self.is_unloaded:
            return {
                "loaded_model": None,
                "vision_enabled": False,
                "is_unloaded": True,
                "needs_reload": True,
                "loaded_at": None,
                "idle_since": None,
            }
        return {
            "loaded_model": self.current_model,
            "vision_enabled": self.current_vision_enabled,
            "is_unloaded": False,
            # load-age proxy: the caretaker sees no request traffic; the
            # gateway combines ``idle_since`` with its own ``last_request_time``
            # to decide when it is safe to call ``POST /unload`` (Phase D).
            "loaded_at": self._loaded_at,
            "idle_since": self._loaded_at,
            # Drift against the model's config *default* vision (config_vision
            # forces it), NOT the loaded vision mode: folding in the loaded
            # mode would hide a config change that adds/removes mmproj and the
            # gateway would never issue the needed /ensure reload.
            "needs_reload": self._config_drifted(
                self.current_model, enable_vision=None, config_vision=True
            )
            if self.current_model is not None
            else True,
        }

    # ------------------------------------------------------------- watchdog
    def start_watchdog(
        self,
        *,
        interval: float = DEFAULT_WATCHDOG_INTERVAL,
        max_backoff: float = DEFAULT_WATCHDOG_MAX_BACKOFF,
        initial_backoff: float = DEFAULT_WATCHDOG_INITIAL_BACKOFF,
    ) -> None:
        """Start the background health/crash watchdog (idempotent).

        The watchdog is **not** started automatically: tests and the F5-deploy
        wiring start it explicitly. A second call while running only updates the
        timing parameters — it never spawns a duplicate background task.
        """
        self._watchdog_interval = interval
        self._watchdog_initial_backoff = initial_backoff
        self._watchdog_max_backoff = max_backoff
        self._watchdog_backoff = initial_backoff
        self._watchdog_running = True
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    def stop_watchdog(self) -> None:
        """Stop the background watchdog (idempotent).

        Clear the running flag (the loop exits at its next cadence) and cancel
        any in-flight task so a long sleep is interrupted immediately.
        """
        self._watchdog_running = False
        task = self._watchdog_task
        if task is not None and not task.done():
            task.cancel()
        self._watchdog_task = None

    async def _watchdog_tick(self) -> bool:
        """One deterministic watchdog pass: probe health and restart on a crash.

        Returns ``True`` when a restart was attempted; ``False`` (no action) when
        there is no active model, caretaker is unloaded, or a lifecycle
        transition is in flight — so a tick never races a switch/unload/stop.

        Backoff: ``_watchdog_backoff`` starts at ``initial_backoff`` and is
        slept *before* the restart. A successful ``switch_model`` resets it to
        ``initial_backoff``; a failed load (``ModelLoadError``) doubles it
        (capped at ``_watchdog_max_backoff``) so a crash-looping model is not
        thrashed.

        This is the testable core: tests call it directly with a small/zero
        backoff instead of running the infinite :meth:`_watchdog_loop`.
        """
        if self.is_unloaded or self._switch_in_progress:
            return False
        if self.current_model is not None:
            model = self.current_model
        elif self._watchdog_retry_model is not None:
            # A previous restart attempt failed and switch_model cleared
            # current_model — keep retrying the same target with backoff
            # instead of abandoning a crash-looping model after one attempt.
            model = self._watchdog_retry_model
        else:
            return False
        if await self.server_process.health_ok(self.server_url):
            # Healthy — a fresh crash later starts again from the initial backoff.
            self._watchdog_backoff = self._watchdog_initial_backoff
            self._watchdog_retry_model = None
            return False

        logger.warning("Watchdog: backend unhealthy for '%s' — recording crash", model)
        await self._detect_crash(model)
        logger.warning(
            "Watchdog: restarting '%s' after %.1fs backoff",
            model,
            self._watchdog_backoff,
        )
        if self._watchdog_backoff > 0:
            await asyncio.sleep(self._watchdog_backoff)
        try:
            self._watchdog_retry_model = model
            # force=True: the no-op fast-path in switch_model (same model +
            # no drift) must NOT skip the restart — the backend is known dead.
            await self.switch_model(model, force=True)
        except ModelLoadError:
            self._watchdog_backoff = min(
                self._watchdog_backoff * 2, self._watchdog_max_backoff
            )
            raise
        self._watchdog_backoff = self._watchdog_initial_backoff
        self._watchdog_retry_model = None
        return True

    async def _watchdog_loop(self) -> None:
        """The watchdog background loop: poll health every ``interval``.

        Each pass calls :meth:`_watchdog_tick`; a failed restart (backed off by
        the tick) does not abort the loop, so a later recovery is picked up.
        Cancellation and ``_watchdog_running = False`` both stop the loop cleanly.
        """
        while self._watchdog_running:
            try:
                await self._watchdog_tick()
            except ModelLoadError:
                # ModelLoadError is already backoff-doubled inside the tick; the
                # loop keeps polling so a crash-looping model's recovery or a
                # different model is still acted on.
                pass
            except asyncio.CancelledError:
                raise
            except Exception:  # best-effort: a failing tick never kills the loop
                logger.exception("Watchdog tick failed")
            await asyncio.sleep(self._watchdog_interval)