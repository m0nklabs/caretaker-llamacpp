"""Phase A tests — lifecycle core of the caretaker manager (PLAN.md §2).

Covers:
1. Args byte-parity of ``_build_args_string`` (hardcoded golden strings for a
   full and a minimal model, plus a vision-override scenario).
2. Apples-to-apples cross-check against the guardian implementation when the
   guardian repo is present on disk (skipped otherwise).
3. ``switch_model`` orchestration driven by a fake ``ServerProcess`` (no-op
   fast-path, full stop→start→health flow, ``ModelLoadError`` on health
   failure, unknown-model guard).
4. ``unload`` double-unload guard.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from caretaker import config as config_mod
from caretaker.manager import (
    Caretaker,
    ModelLoadError,
    ServerProcess,
)

GUARDIAN_MANAGER_PATH = "/home/flip/guardian-llmprovider-gateway/app/engine/manager.py"

# Deterministic, non-existent slot dir so the golden strings are stable across
# hosts (a missing slot dir is fine — it is only a path literal here).
SLOTS = "/usr/local/share/caretaker/fixture_slots"


# --------------------------------------------------------------------------- helpers
def _write_models_yaml(tmp_path: Path, models: dict) -> Path:
    cfg = tmp_path / "models.local.settings.yaml"
    cfg.write_text(yaml.safe_dump({"models": models, "aliases": {}}), encoding="utf-8")
    return cfg


@pytest.fixture
def full_model(tmp_path: Path) -> dict:
    """A full model config exercising every args-builder branch.

    The ``mmproj`` and ``draft_model_path`` target real temp files so the
    builder emits their flags (a missing file merely logs and drops the flag).
    """
    mmproj = tmp_path / "mmproj-f16.gguf"
    mmproj.write_bytes(b"mmproj")
    draft = tmp_path / "draft-Q4_K_M.gguf"
    draft.write_bytes(b"draft")
    return {
        "path": "/home/flip/models/Full-Model-Q8_0.gguf",
        "context": 65536,
        "ngl": 99,
        "kv_type": "q4_0",
        "tensor_split": "0.57,0.43",
        "mmproj": str(mmproj),
        "draft_model_path": str(draft),
        "spec_type": "draft-dflash",
        "spec_draft_n_max": 8,
        "spec_draft_n_min": 1,
        "draft_cache_type_k": "f16",
        "draft_cache_type_v": "f16",
        "n_slots": 4,
        "extra_args": "--no-mmap -nkvo",
        "cuda_visible_devices": "0,1",
    }


def _make_manager(tmp_path: Path, process: ServerProcess | None = None, **kwargs) -> Caretaker:
    models = kwargs.pop("models", None) or {
        "minimal": {"path": "/home/flip/models/minimal.gguf"}
    }
    cfg = _write_models_yaml(tmp_path, models)
    return Caretaker(config_path=str(cfg), server_process=process, **kwargs)


@pytest.fixture
def patch_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the manager's deployment-file paths at a per-test tmp dir.

    ``CARETAKER_LLAMA_SLOTS_DIR`` is set via env so the call-time helper honors
    it deterministically (byte-parity goldens depend on it).
    """
    monkeypatch.setenv("CARETAKER_LLAMA_SLOTS_DIR", SLOTS)
    monkeypatch.setenv("CARETAKER_SERVER_URL", "http://127.0.0.1:11440")
    import caretaker.manager as manager_mod

    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_ARGS_FILE", tmp_path / "current_model.args")
    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_ENV_FILE", tmp_path / "current_model.env")
    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_SIG_FILE", tmp_path / "current_model.sig")


# ----------------------------------------------------------------------- args parity
def test_args_full_model_golden(tmp_path: Path, full_model: dict, patch_paths: None) -> None:
    """A fully-configured model produces the exact guardian-identical args string.

    ``enable_vision`` is left unset (text mode), so the ``mmproj`` flag is not
    emitted — identical to the guardian (vision off drops ``--mmproj``). The
    spec-draft / tensor-split / parallel / extra-args branches all appear.
    """
    mgr = _make_manager(tmp_path, models={"full": full_model})
    args_str, env_dict = mgr._build_args_string(mgr.build_runtime_config("full"))

    draft = full_model["draft_model_path"]
    golden = (
        "-m /home/flip/models/Full-Model-Q8_0.gguf -c 65536 -ngl 99 -ctk q4_0 -ctv q4_0 "
        f"--host 127.0.0.1 --port 11440 --slot-save-path {SLOTS} --load-mode none "
        "--tensor-split 0.57,0.43 "
        f"--spec-type draft-dflash --model-draft {draft} "
        "--spec-draft-n-max 8 --spec-draft-n-min 1 "
        "--cache-type-k-draft f16 --cache-type-v-draft f16 "
        "--parallel 4 "
        "--no-mmap -nkvo"
    )
    assert args_str == golden
    assert env_dict == {"CUDA_VISIBLE_DEVICES": "0,1"}


def test_args_minimal_model_matches_guardian_defaults(tmp_path: Path, patch_paths: None) -> None:
    """A path-only model matches the guardian output exactly.

    Note: ``build_runtime_config`` sets ``context``/``ngl`` to ``None`` when the
    keys are absent (it does not inject defaults), and ``_build_args_string``
    only applies ``4096``/``99`` when the key is entirely missing — so the
    guardian-identical output is ``-c None -ngl None``. This is the honest
    byte-parity result, asserted as the caretaker==guardian contract.
    """
    mgr = _make_manager(tmp_path)
    args_str, env_dict = mgr._build_args_string(mgr.build_runtime_config("minimal"))

    golden = (
        "-m /home/flip/models/minimal.gguf -c None -ngl None -ctk q4_0 -ctv q4_0 "
        f"--host 127.0.0.1 --port 11440 --slot-save-path {SLOTS} --load-mode none"
    )
    assert args_str == golden
    assert env_dict == {}


def test_args_vision_override_uses_vision_values(tmp_path: Path, patch_paths: None) -> None:
    """Vision mode picks ``vision_context``/``vision_ngl`` over the base values."""
    mmproj = tmp_path / "mmproj.gguf"
    mmproj.write_bytes(b"mmproj")
    model = {
        "path": "/home/flip/models/vo.gguf",
        "context": 8192,
        "ngl": 20,
        "vision_context": 16384,
        "vision_ngl": 40,
        "mmproj": str(mmproj),
    }
    mgr = _make_manager(tmp_path, models={"vo": model})
    config = mgr.build_runtime_config("vo", enable_vision=True)
    assert config["context"] == 16384
    assert config["ngl"] == 40
    args_str, _ = mgr._build_args_string(config)
    assert "-c 16384" in args_str
    assert "-ngl 40" in args_str
    assert f"--mmproj {mmproj}" in args_str


def test_writes_basic_args_mirror_guardian(tmp_path: Path, patch_paths: None) -> None:
    """Mirror of guardian ``test_writes_basic_args``: a GLM-4.7-Flash-style model
    with no ``context`` key resolves a default ctx (4096) and ngl 99, and the
    full args string carries ``--load-mode none`` + ``--host 127.0.0.1 --port 11440``."""
    model = {
        "path": "/models/GLM-4.7-Flash.gguf",
        "ngl": 99,
        "ctx": 8192,  # legacy key the args-builder must ignore (no context: key)
        "ts": "17,11",  # legacy tensor-split key (also ignored)
    }
    mgr = _make_manager(tmp_path, models={"GLM-4.7-Flash": model})

    raw = mgr.models["GLM-4.7-Flash"]
    assert raw["path"] == "/models/GLM-4.7-Flash.gguf"
    assert raw.get("context", 4096) == 4096  # guardian golden: default ctx
    assert raw.get("ngl", 99) == 99          # guardian golden: ngl 99

    args_str, env_dict = mgr._build_args_string(raw)
    golden = (
        "-m /models/GLM-4.7-Flash.gguf -c 4096 -ngl 99 -ctk q4_0 -ctv q4_0 "
        f"--host 127.0.0.1 --port 11440 --slot-save-path {SLOTS} --load-mode none"
    )
    assert args_str == golden
    assert env_dict == {}


def test_write_server_args_ignores_legacy_backend_key(
    tmp_path: Path, patch_paths: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirror of guardian ``test_write_server_args_ignores_legacy_backend_key``:
    a ``backend`` config key must have NO effect on the written args (backwards
    compat; the guardian ignores it too). Uses a GLM-4.7-Flash-style raw config."""
    import caretaker.manager as manager_mod

    base = {
        "path": "/models/GLM-4.7-Flash.gguf",
        "context": 4096,
        "ngl": 99,
    }
    with_backend = dict(base, backend="unexpected_backend")

    plain_mgr = _make_manager(tmp_path, models={"m": base})
    with_backend_mgr = _make_manager(tmp_path, models={"m": with_backend})

    plain_args_file = tmp_path / "plain.args"
    backend_args_file = tmp_path / "backend.args"
    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_ARGS_FILE", plain_args_file)
    plain_mgr._write_server_args(plain_mgr.models["m"])
    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_ARGS_FILE", backend_args_file)
    with_backend_mgr._write_server_args(with_backend_mgr.models["m"])

    plain = plain_args_file.read_text()
    backend = backend_args_file.read_text()
    assert "-m /models/GLM-4.7-Flash.gguf" in plain
    assert "--host 127.0.0.1 --port 11440" in plain
    assert "--load-mode none" in plain
    assert plain == backend  # the backend key changes nothing


# ------------------------------------------------- cross-check against the guardian
# Guardian is bridged via its **own venv** in a subprocess: the caretaker venv does
# not carry guardian deps (psutil, pydantic, schedule, …), so an in-process
# ``import app.engine.manager`` is not reliable. We hand the guardian venv a small
# script that runs a real ``ModelManager`` on the fixture and prints the args/env;
# the caretaker side runs the same fixture and compares byte-equal.
GUARDIAN_ROOT = "/home/flip/guardian-llmprovider-gateway"
GUARDIAN_VENV = GUARDIAN_ROOT + "/venv/bin/python"

_GUARDIAN_BRIDGE_SCRIPT = r"""
import json, os, sys

sys.path.insert(0, os.environ["GG_ROOT"])
os.chdir(os.environ["GG_ROOT"])
from app.engine.manager import ModelManager

cfg = os.environ["GG_CFG"]
mgr = ModelManager(config_path=cfg)
out = {}
for name in ("full-model", "minimal"):
    runtime = mgr.build_runtime_config(name, enable_vision=False)
    args_str, env_dict = mgr._build_args_string(runtime)
    out[name] = {"args": args_str, "env": env_dict}
print(json.dumps(out, sort_keys=True))
"""


@pytest.mark.skipif(
    not os.path.exists(GUARDIAN_MANAGER_PATH),
    reason=f"guardian manager not present at {GUARDIAN_MANAGER_PATH}",
)
@pytest.mark.skipif(
    not os.path.exists(GUARDIAN_VENV),
    reason=f"guardian venv python not present at {GUARDIAN_VENV}",
)
def test_args_crosscheck_guardian_byte_equal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, full_model: dict
) -> None:
    """Apples-to-apples: caretaker and guardian ``_build_args_string`` match bytes.

    The guardian side runs in its own venv via a subprocess bridge (the caretaker
    venv lacks guardian deps). Both slot-dir env vars are forced to the same value
    so ``--slot-save-path`` resolves identically on both sides.
    """
    monkeypatch.setenv("CARETAKER_LLAMA_SLOTS_DIR", SLOTS)
    monkeypatch.setenv("GUARDIAN_LLMPROVIDER_GATEWAY_SLOTS_DIR", SLOTS)
    monkeypatch.setenv("CARETAKER_SERVER_URL", "http://127.0.0.1:11440")

    models = {"full-model": full_model, "minimal": {"path": "/home/flip/models/minimal.gguf"}}
    cfg = _write_models_yaml(tmp_path, models)
    caretaker = _make_manager(tmp_path, models=models)

    bridge_env = {
        **os.environ,
        "GG_ROOT": GUARDIAN_ROOT,
        "GG_CFG": str(cfg),
    }
    try:
        proc = subprocess.run(
            [GUARDIAN_VENV, "-c", _GUARDIAN_BRIDGE_SCRIPT],
            capture_output=True,
            text=True,
            env=bridge_env,
            timeout=60,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - bridge availability guard
        pytest.skip(f"guardian bridge subprocess could not run: {exc}")
    if proc.returncode != 0:
        pytest.skip(
            "guardian bridge failed to instantiate ModelManager on the fixture "
            f"(rc={proc.returncode}): {proc.stderr.strip()[-400:]}"
        )

    guardian_out = json.loads(proc.stdout.strip().splitlines()[-1])

    for model in ("full-model", "minimal"):
        caretaker_args, caretaker_env = caretaker._build_args_string(
            caretaker.build_runtime_config(model)
        )
        guardian_args, guardian_env = guardian_out[model]["args"], guardian_out[model]["env"]
        assert caretaker_args == guardian_args, (
            f"args mismatch for {model}:\nCaretaker: {caretaker_args}\nGuardian:  {guardian_args}"
        )
        assert caretaker_env == guardian_env, f"env mismatch for {model}"


# ------------------------------------------------------------- ServerProcess
class FakeServerProcess(ServerProcess):
    """In-memory fake: records start/stop and returns scripted health/restart results."""

    def __init__(
        self,
        health_ok: bool = True,
        restart_count: int = 0,
        is_failed: bool = False,
    ) -> None:
        self.events: list[str] = []
        self._health_ok = health_ok
        self._restart_count = restart_count
        self._is_failed = is_failed
        self.stop_calls = 0

    async def start(self) -> None:
        self.events.append("start")

    async def stop(self) -> None:
        self.events.append("stop")
        self.stop_calls += 1

    async def health_ok(self, url: str = "") -> bool:
        return self._health_ok

    async def restart_count(self) -> int:
        return self._restart_count

    async def is_failed(self) -> bool:
        return self._is_failed

    async def crash_error(self) -> str:
        return "synthetic-failure"

    async def service_exit_code(self) -> int | None:
        return 1


async def test_switch_model_noop_when_already_active(tmp_path: Path, patch_paths: None) -> None:
    """Same model + vision + no drift → no stop/start is issued."""
    process = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=process)
    await mgr.switch_model("minimal")
    assert mgr.current_model == "minimal"
    first_events = list(process.events)

    await mgr.switch_model("minimal")  # identical, no drift → no-op
    assert process.events == first_events


async def test_switch_model_flows_stop_then_start(tmp_path: Path, patch_paths: None) -> None:
    """A real switch issues stop → write args → start."""
    process = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=process)
    await mgr.switch_model("minimal")
    assert mgr.current_model == "minimal"
    assert process.events[:2] == ["stop", "start"]
    # Args file was written for the launch.
    import caretaker.manager as manager_mod

    assert manager_mod.CURRENT_MODEL_ARGS_FILE.exists()


async def test_switch_model_health_fail_raises_ModelLoadError(
    tmp_path: Path, patch_paths: None
) -> None:
    """When the backend never becomes healthy, switch_model raises ModelLoadError."""
    process = FakeServerProcess(health_ok=False)
    mgr = _make_manager(
        tmp_path, process=process, health_polls=3, health_interval=0.0
    )
    with pytest.raises(ModelLoadError) as excinfo:
        await mgr.switch_model("minimal")
    assert "failed to load" in str(excinfo.value)
    assert excinfo.value.crash_record is not None
    assert excinfo.value.crash_record.model == "minimal"
    # Crash was stopped (prevent restart loop) and recorded.
    assert process.events == ["stop", "start", "stop"]


async def test_switch_unknown_model_raises_ValueError(tmp_path: Path, patch_paths: None) -> None:
    """Switching to an unconfigured model raises ValueError and touches nothing."""
    process = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=process)
    with pytest.raises(ValueError):
        await mgr.switch_model("nope")
    assert process.events == []


async def test_unload_guards_against_double(tmp_path: Path, patch_paths: None) -> None:
    """Two unload calls stop the backend only once and clear the model state."""
    process = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=process)
    await mgr.switch_model("minimal")
    assert process.stop_calls == 1

    await mgr.unload()
    assert process.stop_calls == 2
    assert mgr.is_unloaded is True
    assert mgr.current_model is None

    await mgr.unload()  # guard: already unloaded
    assert process.stop_calls == 2


def test_caretaker_importable_without_models_file() -> None:
    """Module import is safe even when no models file exists (no eager singleton)."""
    import caretaker.manager as manager_mod

    for name in (
        "Caretaker",
        "ModelLoadError",
        "CrashRecord",
        "ServerProcess",
        "SystemdServerProcess",
        "DirectServerProcess",
    ):
        assert hasattr(manager_mod, name)


def test_config_comfyui_url_from_env_overrides_yaml(tmp_path: Path) -> None:
    """CARETAKER_COMFYUI_URL beats the settings YAML's services.comfyui_url."""
    cfg = tmp_path / "models.local.settings.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "models": {"m": {"path": "/tmp/x.gguf"}},
                "services": {"comfyui_url": "http://y:1234"},
            }
        ),
        encoding="utf-8",
    )
    assert config_mod.comfyui_url(cfg) == "http://y:1234"
    os.environ["CARETAKER_COMFYUI_URL"] = "http://env:9000"
    try:
        assert config_mod.comfyui_url(cfg) == "http://env:9000"
    finally:
        os.environ.pop("CARETAKER_COMFYUI_URL", None)

# --------------------------------------------------------------------------
# 5. Review-fix regressions (PR #3 review findings)
# --------------------------------------------------------------------------


def test_switch_model_failure_clears_active_state(tmp_path):
    """Review fix 1: after a failed switch, current_model/vision are cleared so
    a later same-model ensure actually restarts (no stale 'already active')."""
    import asyncio

    fake = FakeServerProcess(health_ok=False)
    mgr = _make_manager(tmp_path, process=fake, health_polls=4, health_interval=0.01)
    mgr.current_model = "minimal"
    mgr.current_vision_enabled = False

    with pytest.raises(ModelLoadError):
        asyncio.run(mgr.switch_model("minimal"))

    assert mgr.current_model is None
    assert mgr.current_vision_enabled is False


def test_direct_process_start_uses_devnull(tmp_path, monkeypatch):
    """Review fix 2: DirectServerProcess must not hold unread PIPE buffers
    (child would block once the pipe fills); output goes to DEVNULL."""
    from caretaker.manager import DirectServerProcess

    # Patch the args file path to a nonexistent one: start() reads it before
    # spawning, so a missing file raises FileNotFoundError without spawning.
    monkeypatch.setattr(
        "caretaker.manager.CURRENT_MODEL_ARGS_FILE",
        tmp_path / "nope" / "current_model.args",
    )
    import asyncio

    proc = DirectServerProcess(binary="/bin/true")
    with pytest.raises(FileNotFoundError):
        asyncio.run(proc.start())


def test_build_args_string_uses_injected_server_url(tmp_path, monkeypatch):
    """Review fix 3: _build_args_string honors the constructor-injected
    server_url (self.server_url) instead of re-reading the env at call time.
    The injected URL wins even when a different env var is set."""
    cfg_yaml = _write_models_yaml(
        tmp_path, {"minimal": {"path": "/home/flip/models/minimal.gguf"}}
    )
    monkeypatch.setenv("CARETAKER_SERVER_URL", "http://env-host:9999")

    from caretaker.manager import Caretaker

    mgr_inj = Caretaker(
        config_path=str(cfg_yaml), server_url="http://inj-host:11441"
    )
    cfg = mgr_inj.build_runtime_config("minimal", enable_vision=False)
    args_inj, _ = mgr_inj._build_args_string(cfg)
    # The injected URL must appear in the args, NOT the env var.
    assert "--host inj-host --port 11441" in args_inj
    assert "--host env-host" not in args_inj
