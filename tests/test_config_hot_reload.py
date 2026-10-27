"""Config hot-reload tests — mtime-based fail-open reload of the models file.

The caretaker historically loaded ``models.local.settings.yaml`` exactly once
(in ``Caretaker.__init__``); edits required a daemon restart. These tests pin
the ``_maybe_reload_config`` contract:

1. ``switch_model``/``health()`` pick up file changes without a restart
   (models edited, added, removed; aliases added; ``health_polls`` knob).
2. Unchanged files cost a single stat (no reload, no parse).
3. Fail-open: a broken edit warns once, keeps the previous in-memory config,
   and never breaks ``switch_model`` — a later valid edit still reloads.
4. ``health()`` is defensive when the loaded model disappears from the
   config (``needs_reload: true`` instead of a KeyError raise).
5. One route-level pin: ``POST /ensure`` hot-loads a model added to the file
   after a first 404, through ``caretaker.server.init`` injection.
6. Embedded-MTP args build: ``draft_cache_type_k``/``draft_cache_type_v``
   are honored (``--cache-type-k-draft``/``--cache-type-v-draft``) when
   explicitly configured and stay silent otherwise (dead-key fix).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from fastapi.testclient import TestClient

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from caretaker import config as config_mod
from caretaker.manager import DEFAULT_HEALTH_POLLS, Caretaker
from caretaker.server import app, init
from caretaker.vram import VramLimitExceededError

from test_phase_a import FakeServerProcess, _make_manager

AUTH = {"Authorization": "Bearer test-secret"}

MODEL_A = {"path": "/home/flip/models/minimal.gguf", "context": 4096}
MODEL_NEW = {"path": "/home/flip/models/newmodel.gguf", "context": 2048}


# --------------------------------------------------------------------------- helpers
def _set_models_file(
    tmp_path: Path,
    models: dict,
    aliases: dict | None = None,
    extra: dict | None = None,
) -> Path:
    """(Re)write the manager's models file — same fixed path ``_make_manager`` uses."""
    cfg = tmp_path / "models.local.settings.yaml"
    data: dict = {"models": models, "aliases": aliases or {}}
    if extra:
        data.update(extra)
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
    return cfg


def _bump_mtime(cfg: Path, tick: list[int]) -> None:
    """Force a strictly increasing ``st_mtime_ns`` so the reload check fires.

    Real-time writes have sub-second mtime granularity surprises; an explicit
    ``os.utime`` with a monotonically bumped ns value makes the change
    deterministic regardless of filesystem timestamp resolution.
    """
    tick[0] += 10_000_000  # +10 ms
    os.utime(cfg, ns=(tick[0], tick[0]))


@pytest.fixture
def patch_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Mirror of ``test_phase_a.patch_paths``: per-test tmp deployment paths."""
    monkeypatch.setenv("CARETAKER_LLAMA_SLOTS_DIR", "/usr/local/share/caretaker/fixture_slots")
    monkeypatch.setenv("CARETAKER_SERVER_URL", "http://127.0.0.1:11440")
    import caretaker.manager as manager_mod

    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_ARGS_FILE", tmp_path / "current_model.args")
    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_ENV_FILE", tmp_path / "current_model.env")
    monkeypatch.setattr(manager_mod, "CURRENT_MODEL_SIG_FILE", tmp_path / "current_model.sig")


@pytest.fixture
def injection_reset(isolated_api_lifespan):
    yield
    init(None)


# --------------------------------------------------------- 1. ctx change pickup
async def test_switch_model_picks_up_ctx_change_without_restart(
    tmp_path: Path, patch_paths: None
) -> None:
    """Load ctx A → edit the file (same model, ctx B) + bump mtime → switch
    again → the written args file contains ctx B. No daemon restart involved."""
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc, models={"minimal": dict(MODEL_A)})
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]

    await mgr.switch_model("minimal")
    args_after_a = (tmp_path / "current_model.args").read_text()
    assert "-c 4096" in args_after_a

    _set_models_file(tmp_path, {"minimal": {"path": MODEL_A["path"], "context": 8192}})
    _bump_mtime(cfg, tick)
    await mgr.switch_model("minimal")

    args_after_b = (tmp_path / "current_model.args").read_text()
    assert "-c 8192" in args_after_b, args_after_b
    assert mgr.current_model == "minimal"
    # A real reload happened (drift against the new config), not a no-op.
    assert proc.events.count("start") == 2


# --------------------------------------------------------- 2. unchanged → no reload
async def test_unchanged_file_skips_reload(
    tmp_path: Path, patch_paths: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unchanged mtime/size → the cheap one-stat path; load_models_config is
    NOT called again (no reparse, no dict swap)."""
    mgr = _make_manager(tmp_path, process=FakeServerProcess(), models={"minimal": dict(MODEL_A)})

    calls = {"n": 0}
    orig_load = config_mod.load_models_config

    def counting_load(path=None):
        calls["n"] += 1
        return orig_load(path)

    monkeypatch.setattr(config_mod, "load_models_config", counting_load)

    await mgr.switch_model("minimal")
    assert calls["n"] == 0, "unchanged file: switch must take the one-stat cheap path"

    await mgr.switch_model("minimal")
    assert calls["n"] == 0, "unchanged file must not re-parse the models config"

    mgr.health()
    assert calls["n"] == 0, "health() on an unchanged file must not re-parse either"

    args = (tmp_path / "current_model.args").read_text()
    assert "-c 4096" in args


# --------------------------------------------------------- 3. broken YAML fail-open
async def test_broken_yaml_keeps_previous_config_and_warns_once(
    tmp_path: Path, patch_paths: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Broken YAML on a changed file: switch still succeeds with the PREVIOUS
    config, a warning is logged, no exception; a later valid edit reloads."""
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc, models={"minimal": dict(MODEL_A)})
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    await mgr.switch_model("minimal")

    cfg.write_text("models: [oops\n  broken", encoding="utf-8")  # invalid YAML
    _bump_mtime(cfg, tick)

    with caplog.at_level(logging.WARNING, logger="caretaker.manager"):
        # No exception: the previous in-memory config keeps the daemon alive.
        await mgr.switch_model("minimal")

    assert mgr.current_model == "minimal"
    assert mgr.models["minimal"]["context"] == 4096, "broken edit must keep the old config"
    assert "-c 4096" in (tmp_path / "current_model.args").read_text()
    assert any(
        "hot-reload failed" in record.getMessage() for record in caplog.records
    ), [record.getMessage() for record in caplog.records]

    # Subsequent VALID edit reloads normally (the broken sig was stored once).
    _set_models_file(tmp_path, {"minimal": {"path": MODEL_A["path"], "context": 8192}})
    _bump_mtime(cfg, tick)
    await mgr.switch_model("minimal")
    assert mgr.models["minimal"]["context"] == 8192
    assert "-c 8192" in (tmp_path / "current_model.args").read_text()


# --------------------------------------------------------- 4. added model loadable
async def test_added_model_becomes_loadable_without_restart(
    tmp_path: Path, patch_paths: None
) -> None:
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc, models={"minimal": dict(MODEL_A)})
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]

    with pytest.raises(ValueError):
        await mgr.switch_model("newmodel")

    _set_models_file(tmp_path, {"minimal": dict(MODEL_A), "newmodel": dict(MODEL_NEW)})
    _bump_mtime(cfg, tick)
    await mgr.switch_model("newmodel")

    assert mgr.current_model == "newmodel"
    assert "-c 2048" in (tmp_path / "current_model.args").read_text()


# --------------------------------------------------------- 5. removed model → needs_reload
async def test_health_reports_needs_reload_when_loaded_model_removed(
    tmp_path: Path, patch_paths: None
) -> None:
    """A reload that removes the LOADED model must not make ``health()`` raise
    (defensive branch — ``_compute_launch_signature`` would KeyError);
    ``needs_reload: true`` is reported instead."""
    mgr = _make_manager(tmp_path, process=FakeServerProcess(), models={"minimal": dict(MODEL_A)})
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]

    await mgr.switch_model("minimal")
    assert mgr.health()["needs_reload"] is False

    _set_models_file(tmp_path, {"other": {"path": "/home/flip/models/other.gguf"}})
    _bump_mtime(cfg, tick)

    health = mgr.health()  # must not raise
    assert health["loaded_model"] == "minimal"
    assert health["needs_reload"] is True


# --------------------------------------------------------- 6. added alias resolves
async def test_alias_added_by_file_change_resolves_in_switch(
    tmp_path: Path, patch_paths: None
) -> None:
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc, models={"minimal": dict(MODEL_A)})
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]

    with pytest.raises(ValueError):
        await mgr.switch_model("mini")

    _set_models_file(tmp_path, {"minimal": dict(MODEL_A)}, aliases={"mini": "minimal"})
    _bump_mtime(cfg, tick)
    await mgr.switch_model("mini")

    assert mgr.current_model == "minimal", "the reloaded alias map must resolve 'mini'"


# --------------------------------------------------------- 7. health_polls knob
async def test_health_polls_refreshed_from_file_on_reload(
    tmp_path: Path, patch_paths: None
) -> None:
    """Without an explicit constructor override, the file's ``health_polls``
    knob is applied on hot-reload (``health()`` is the lock-free call site)."""
    mgr = _make_manager(tmp_path, process=FakeServerProcess(), models={"minimal": dict(MODEL_A)})
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    assert mgr.health_polls == DEFAULT_HEALTH_POLLS

    _set_models_file(tmp_path, {"minimal": dict(MODEL_A)}, extra={"health_polls": 777})
    _bump_mtime(cfg, tick)
    mgr.health()

    assert mgr.health_polls == 777


async def test_explicit_health_polls_override_wins_over_file_reload(
    tmp_path: Path, patch_paths: None
) -> None:
    """An explicit constructor ``health_polls`` wins over the models-file knob,
    also after a hot reload."""
    mgr = _make_manager(
        tmp_path, process=FakeServerProcess(), models={"minimal": dict(MODEL_A)}, health_polls=99
    )
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    assert mgr.health_polls == 99

    _set_models_file(tmp_path, {"minimal": dict(MODEL_A)}, extra={"health_polls": 777})
    _bump_mtime(cfg, tick)
    mgr.health()

    assert mgr.health_polls == 99


# --------------------------------------------------------- 10. route-level ensure pin
def test_ensure_hot_loads_model_added_after_first_404(
    tmp_path: Path,
    patch_paths: None,
    injection_reset: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hot reload through the control API: ``POST /ensure`` for an unknown
    model → 404 ``model_not_found``; after the model is ADDED to the file,
    a second ``POST /ensure`` returns 200 — no daemon restart."""
    monkeypatch.setenv("CARETAKER_KEY", "test-secret")
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc, models={"other": {"path": "/home/flip/models/other.gguf"}})
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    init(mgr)
    client = TestClient(app)

    r1 = client.post("/ensure", json={"model": "minimal"}, headers=AUTH)
    assert r1.status_code == 404, r1.text
    assert r1.json()["error"] == "model_not_found"

    _set_models_file(
        tmp_path,
        {"other": {"path": "/home/flip/models/other.gguf"}, "minimal": dict(MODEL_A)},
    )
    _bump_mtime(cfg, tick)

    r2 = client.post("/ensure", json={"model": "minimal"}, headers=AUTH)
    assert r2.status_code == 200, r2.text
    assert r2.json()["ok"] is True
    assert r2.json()["loaded_model"] == "minimal"
    assert proc.events.count("start") >= 1


# --------------------------------------------------- 8./9. embedded-MTP draft cache
MTP_PATH = "/home/flip/models/mtp-embedded.gguf"
MTP_SLOTS = "/usr/local/share/caretaker/fixture_slots"


def test_embedded_mtp_explicit_draft_cache_keys_emitted(
    tmp_path: Path, patch_paths: None
) -> None:
    """Embedded-MTP + explicit ``draft_cache_type_k/v`` → the draft-KV flags
    are emitted (previously dead letter on this branch, 2026-10-25 finding)."""
    mgr = _make_manager(
        tmp_path,
        process=FakeServerProcess(),
        models={
            "mtp": {
                "path": MTP_PATH,
                "context": 8192,
                "ngl": 99,
                "spec_type": "draft-mtp",
                "draft_cache_type_k": "turbo4",
                "draft_cache_type_v": "turbo4",
            }
        },
    )
    args_str, _ = mgr._build_args_string(mgr.build_runtime_config("mtp"))
    assert "--cache-type-k-draft turbo4" in args_str
    assert "--cache-type-v-draft turbo4" in args_str


def test_embedded_mtp_without_draft_cache_keys_stays_silent(
    tmp_path: Path, patch_paths: None
) -> None:
    """Embedded-MTP without the draft cache keys → NO draft-cache flags, args
    byte-identical to the pre-fix behavior (f16 default stays silent)."""
    mgr = _make_manager(
        tmp_path,
        process=FakeServerProcess(),
        models={"mtp": {"path": MTP_PATH, "context": 8192, "ngl": 99, "spec_type": "draft-mtp"}},
    )
    args_str, _ = mgr._build_args_string(mgr.build_runtime_config("mtp"))

    # Exact substring absence (the pin, word-level):
    assert "--cache-type-k-draft" not in args_str
    assert "--cache-type-v-draft" not in args_str
    # Byte-identical golden: only --spec-type, nothing else spec-related.
    golden = (
        f"-m {MTP_PATH} -c 8192 -ngl 99 -ctk q4_0 -ctv q4_0 "
        f"--host 127.0.0.1 --port 11440 --slot-save-path {MTP_SLOTS} --load-mode none "
        "--spec-type draft-mtp"
    )
    assert args_str == golden


# ============================== PR-#15 review round: validation + defer ==============================
class _GatedHealthServerProcess(FakeServerProcess):
    """FakeServerProcess whose health_ok parks on an asyncio.Event.

    Lets a test hold a switch mid-flight (inside ``_wait_for_health``, lock
    held) while editing the models file — the exact race the defer-guard pins.
    """

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.gate_hits = 0

    async def health_ok(self, url: str = "") -> bool:
        self.gate_hits += 1
        await self.gate.wait()
        return True


# --------------------------------------------------- validation before publish
async def test_hot_reload_rejects_null_model_entry(
    tmp_path: Path, patch_paths: None, caplog: pytest.LogCaptureFixture
) -> None:
    """``models: {minimal: null}`` is shallow-valid but would crash later
    ensures: hot-reload must REJECT it (fail-open, previous config kept)."""
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc, models={"minimal": dict(MODEL_A)})
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    await mgr.switch_model("minimal")

    cfg.write_text(yaml.safe_dump({"models": {"minimal": None}, "aliases": {}}), encoding="utf-8")
    _bump_mtime(cfg, tick)
    with caplog.at_level(logging.WARNING, logger="caretaker.manager"):
        await mgr.switch_model("minimal")  # daemon still works, no exception

    assert mgr.models["minimal"] == MODEL_A, "broken edit must keep the previous config"
    assert any(
        "hot-reload rejected" in record.getMessage() for record in caplog.records
    ), [record.getMessage() for record in caplog.records]

    # The rejected edit consumed its signature (warn once); a later VALID
    # edit still reloads.
    _set_models_file(tmp_path, {"minimal": {"path": MODEL_A["path"], "context": 8192}})
    _bump_mtime(cfg, tick)
    await mgr.switch_model("minimal")
    assert mgr.models["minimal"]["context"] == 8192


async def test_hot_reload_rejects_entry_missing_path(
    tmp_path: Path, patch_paths: None, caplog: pytest.LogCaptureFixture
) -> None:
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc, models={"minimal": dict(MODEL_A)})
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    await mgr.switch_model("minimal")

    _set_models_file(tmp_path, {"minimal": {"context": 100}})
    _bump_mtime(cfg, tick)
    with caplog.at_level(logging.WARNING, logger="caretaker.manager"):
        await mgr.switch_model("minimal")

    assert mgr.models["minimal"] == MODEL_A
    assert any("hot-reload rejected" in record.getMessage() for record in caplog.records)


async def test_hot_reload_rejects_non_string_alias_target(
    tmp_path: Path, patch_paths: None, caplog: pytest.LogCaptureFixture
) -> None:
    proc = FakeServerProcess()
    mgr = _make_manager(tmp_path, process=proc, models={"minimal": dict(MODEL_A)})
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    await mgr.switch_model("minimal")

    _set_models_file(tmp_path, {"minimal": dict(MODEL_A)}, aliases={"mini": 5})
    _bump_mtime(cfg, tick)
    with caplog.at_level(logging.WARNING, logger="caretaker.manager"):
        await mgr.switch_model("minimal")

    assert mgr.aliases == {}, "a non-string alias target must be rejected wholesale"
    assert mgr.models["minimal"] == MODEL_A
    assert any("hot-reload rejected" in record.getMessage() for record in caplog.records)


# --------------------------------------------------- defer during locked switches
async def test_health_defers_reload_while_lifecycle_lock_held(
    tmp_path: Path, patch_paths: None
) -> None:
    """A status call while the lifecycle lock is held must NOT swap the config
    out from under an in-flight switch; after release, health() reloads."""
    mgr = _make_manager(tmp_path, process=FakeServerProcess(), models={"minimal": dict(MODEL_A)})
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    await mgr.switch_model("minimal")

    _set_models_file(tmp_path, {"minimal": dict(MODEL_A), "late": dict(MODEL_NEW)})
    _bump_mtime(cfg, tick)

    async with mgr._lifecycle_lock:
        mgr.health()
        assert "late" not in mgr.models, "defer: no reload while the lock is held"

    mgr.health()
    assert "late" in mgr.models, "after release, health() reloads normally"


async def test_health_defers_reload_during_in_flight_switch(
    tmp_path: Path, patch_paths: None
) -> None:
    """Mid-switch defer pin: a switch parked in its health-wait holds the
    lock; health() must not swap self.models. The switch then persists the
    launch signature computed from the snapshot it STARTED with, so the
    file's fresh ctx value is NOT hidden by a mid-switch signature."""
    proc = _GatedHealthServerProcess()
    mgr = _make_manager(tmp_path, process=proc, models={"minimal": dict(MODEL_A)})
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]

    task = asyncio.create_task(mgr.switch_model("minimal"))
    for _ in range(500):
        if proc.gate_hits > 0:
            break
        await asyncio.sleep(0)
    assert proc.gate_hits > 0, "switch must be parked in the gated health probe"
    assert mgr._lifecycle_lock.locked()

    # The operator edits the file while the switch is in flight.
    _set_models_file(tmp_path, {"minimal": {"path": MODEL_A["path"], "context": 8192}})
    _bump_mtime(cfg, tick)

    mgr.health()  # lock-free, but must defer the reload
    assert mgr.models["minimal"]["context"] == 4096, "no mid-switch config swap"

    proc.gate.set()
    await task  # switch lands against its 4096 snapshot
    assert mgr.current_model == "minimal"

    # Follow-up health() reloads (lock free now) and reports HONEST drift:
    # the persisted signature came from the 4096 snapshot, so the file's
    # 8192 is not hidden (a mid-switch swap would have made this False).
    health = mgr.health()
    assert mgr.models["minimal"]["context"] == 8192
    assert health["needs_reload"] is True


# --------------------------------------------------- health_polls default restore
async def test_health_polls_restored_to_default_when_file_key_removed(
    tmp_path: Path, patch_paths: None
) -> None:
    """Removing the ``health_polls`` key from the file must restore the
    built-in default on the next reload (no sticky last-override)."""
    mgr = _make_manager(tmp_path, process=FakeServerProcess(), models={"minimal": dict(MODEL_A)})
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    assert mgr.health_polls == DEFAULT_HEALTH_POLLS

    _set_models_file(tmp_path, {"minimal": dict(MODEL_A)}, extra={"health_polls": 400})
    _bump_mtime(cfg, tick)
    mgr.health()
    assert mgr.health_polls == 400

    _set_models_file(tmp_path, {"minimal": dict(MODEL_A)})  # key removed
    _bump_mtime(cfg, tick)
    mgr.health()
    assert mgr.health_polls == DEFAULT_HEALTH_POLLS


# ===================== PR-#15 review round: VRAM same-name re-size =====================
class _StopFailingServerProcess(FakeServerProcess):
    """FakeServerProcess whose Nth stop() raises (failure mid-switch teardown).

    The cold load's stop must succeed; ``fail_on=2`` fails the switch's stop.
    """

    def __init__(self, fail_on: int = 2, **kwargs) -> None:
        super().__init__(**kwargs)
        self.fail_on = fail_on
        self.stop_attempts = 0

    async def stop(self) -> None:
        self.stop_attempts += 1
        if self.stop_attempts == self.fail_on:
            raise RuntimeError("stop boom")


def _sized_model(size_mb: int) -> dict:
    return {"path": MODEL_A["path"], "context": 4096, "size_mb": size_mb}


async def test_same_name_size_growth_beyond_budget_fails_before_stop(
    tmp_path: Path, patch_paths: None
) -> None:
    """A hot-reloaded ``size_mb`` (300→600) under a 500MB budget must fail the
    same-name switch with VramLimitExceededError BEFORE any backend teardown;
    the stale-but-valid reservation stays and the old server keeps running."""
    proc = FakeServerProcess()
    mgr = _make_manager(
        tmp_path,
        process=proc,
        models={"minimal": _sized_model(300)},
        vram_limit_mb=500,
    )
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    await mgr.switch_model("minimal")
    assert mgr.vram is not None
    assert mgr.vram.active_sizes["minimal"] == 300
    stops_after_load = proc.stop_calls

    _set_models_file(tmp_path, {"minimal": _sized_model(600)})
    _bump_mtime(cfg, tick)
    # force=True: a pure size_mb edit produces no args-drift (the launch
    # signature hashes args/env only), so the no-op fast-path would skip the
    # VRAM block otherwise — force routes the switch through the real path
    # where the re-size guard lives (same mechanism the watchdog uses).
    with pytest.raises(VramLimitExceededError):
        await mgr.switch_model("minimal", force=True)

    assert proc.stop_calls == stops_after_load, (
        "the switch must fail BEFORE touching the backend (no extra stop)"
    )
    assert mgr.vram.active_sizes["minimal"] == 300, "reservation unchanged on failure"
    assert mgr.current_model == "minimal", "old server keeps running"
    assert mgr._switch_in_progress is False, "no stuck flag after the failure"

    # Shrinking to a fitting size (400 <= 500): the switch proceeds and the
    # reservation is re-booked.
    _set_models_file(tmp_path, {"minimal": _sized_model(400)})
    _bump_mtime(cfg, tick)
    await mgr.switch_model("minimal", force=True)
    assert mgr.vram.active_sizes["minimal"] == 400
    assert mgr.current_model == "minimal"


async def test_same_name_size_update_reverted_on_pre_stop_failure(
    tmp_path: Path, patch_paths: None
) -> None:
    """Failure BEFORE the stop (backend still runs the OLD footprint): the
    published size update is reverted to the previous size; the original
    exception propagates."""
    proc = FakeServerProcess()
    mgr = _make_manager(
        tmp_path,
        process=proc,
        models={"minimal": _sized_model(300)},
        vram_limit_mb=500,
    )
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    await mgr.switch_model("minimal")

    _set_models_file(tmp_path, {"minimal": _sized_model(400)})
    _bump_mtime(cfg, tick)
    mgr._save_context = AsyncMock(side_effect=RuntimeError("save boom"))
    with pytest.raises(RuntimeError, match="save boom"):
        await mgr.switch_model("minimal", force=True)

    assert mgr.vram.active_sizes["minimal"] == 300, "size update reverted"
    assert mgr.current_model == "minimal", "backend untouched, still the old model"


async def test_same_name_size_update_released_when_stop_fails(
    tmp_path: Path, patch_paths: None
) -> None:
    """Failure AT the stop (current_model already cleared): the except-path
    must NOT double-handle the size update — the existing same-name failure
    contract releases the slot entirely, and the original stop-raise
    propagates untouched."""
    proc = _StopFailingServerProcess()
    mgr = _make_manager(
        tmp_path,
        process=proc,
        models={"minimal": _sized_model(300)},
        vram_limit_mb=500,
    )
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    await mgr.switch_model("minimal")
    assert mgr.vram is not None

    _set_models_file(tmp_path, {"minimal": _sized_model(400)})
    _bump_mtime(cfg, tick)
    with pytest.raises(RuntimeError, match="stop boom"):
        await mgr.switch_model("minimal", force=True)

    assert mgr.current_model is None
    # Slot fully released (pre-existing same-name failure contract): the
    # server is presumed dead, holding a phantom reservation would block
    # others. No double handling: no revert, no extra release, no stuck flag.
    assert "minimal" not in mgr.vram.active_sizes
    assert "minimal" not in mgr.vram.active_counts
    assert mgr._switch_in_progress is False


# ================= warn-only re-book on the no-op fast-path (lead decision) =================
async def test_noop_path_rebooks_changed_size_warn_only_fits(
    tmp_path: Path, patch_paths: None
) -> None:
    """A pure size_mb edit produces no args-drift, so the next ensure takes the
    no-op fast-path — which re-books the held slot in place (300→400, budget
    500): fresh_load False, backend untouched, active_sizes tracks the config."""
    proc = FakeServerProcess()
    mgr = _make_manager(
        tmp_path,
        process=proc,
        models={"minimal": _sized_model(300)},
        vram_limit_mb=500,
    )
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    await mgr.switch_model("minimal")
    stops_after_load = proc.stop_calls

    _set_models_file(tmp_path, {"minimal": _sized_model(400)})
    _bump_mtime(cfg, tick)
    fresh_load = await mgr.switch_model("minimal")  # no force: no-op fast-path

    assert fresh_load is False, "no drift → no-op fast-path"
    assert proc.stop_calls == stops_after_load, "no-op must not touch the backend"
    assert mgr.vram.active_sizes["minimal"] == 400, "no-op re-booked the slot"
    assert mgr.current_model == "minimal"


async def test_noop_path_over_budget_size_keeps_booking_and_warns(
    tmp_path: Path, patch_paths: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Over-budget size edit (300→600, budget 500) on the no-op path: a
    healthy backend is NEVER failed on a bookkeeping estimate — the no-op
    still succeeds, the old booking stays, a warning is logged."""
    proc = FakeServerProcess()
    mgr = _make_manager(
        tmp_path,
        process=proc,
        models={"minimal": _sized_model(300)},
        vram_limit_mb=500,
    )
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    await mgr.switch_model("minimal")
    stops_after_load = proc.stop_calls

    _set_models_file(tmp_path, {"minimal": _sized_model(600)})
    _bump_mtime(cfg, tick)
    with caplog.at_level(logging.WARNING, logger="caretaker.manager"):
        fresh_load = await mgr.switch_model("minimal")  # no force: no-op fast-path

    assert fresh_load is False, "the no-op still succeeds (warn-only, never raises)"
    assert proc.stop_calls == stops_after_load, "backend untouched"
    assert mgr.vram.active_sizes["minimal"] == 300, "old booking kept"
    assert mgr.current_model == "minimal"
    assert any(
        "keeps running with its previous booking" in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


# ============ review round 3: pre-stop rollback restores the HELD reservation ============
async def test_swap_failure_restores_exact_held_size_not_reloaded_estimate(
    tmp_path: Path, patch_paths: None
) -> None:
    """A→B swap where the target alone is over budget: beta's acquire fails
    AFTER alpha's slot was released, while alpha's backend keeps running.

    The rollback must restore alpha's EXACT previous reservation (300MB) — not
    a recomputation from the hot-reloaded config (600MB), which no longer fits
    under the 500MB budget: that would leave the still-running alpha
    unreserved (later acquires could overcommit) and mask the original budget
    error with a second one from the rollback itself.
    """
    proc = FakeServerProcess()
    mgr = _make_manager(
        tmp_path,
        process=proc,
        models={"alpha": _sized_model(300), "beta": _sized_model(600)},
        vram_limit_mb=500,
    )
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    await mgr.switch_model("alpha")
    assert mgr.vram is not None
    assert mgr.vram.active_sizes["alpha"] == 300
    stops_after_load = proc.stop_calls

    # The operator hot-edits alpha's estimate beyond the budget, then asks for
    # beta (itself bigger than the whole budget): the swap releases alpha's
    # slot first, so beta's acquire fails with alpha already unbooked.
    _set_models_file(tmp_path, {"alpha": _sized_model(600), "beta": _sized_model(600)})
    _bump_mtime(cfg, tick)
    with pytest.raises(VramLimitExceededError) as excinfo:
        await mgr.switch_model("beta")

    assert mgr.vram.active_sizes.get("alpha") == 300, (
        "the running backend's exact held reservation is restored, not the "
        f"reloaded estimate: {dict(mgr.vram.active_sizes)}"
    )
    assert "beta" in str(excinfo.value), (
        f"the target's budget failure must not be masked by its rollback: {excinfo.value}"
    )
    assert proc.stop_calls == stops_after_load, (
        "the failure is pre-stop: alpha's backend keeps running"
    )
    assert mgr.current_model == "alpha"
    assert "beta" not in mgr.vram.active_counts, "no phantom slot for the failed target"
    assert mgr._vram_released_held is None, "the rollback token is consumed"
    assert mgr._switch_in_progress is False


async def test_post_start_failure_keeps_new_size_booking(
    tmp_path: Path, patch_paths: None
) -> None:
    """Same-name reload 300→400 (fits): a failure AFTER the new backend is up
    (context restore cancelled) must NOT revert the booking to 300.

    The revert window ends at teardown: once llama-server is stopped, the old
    footprint no longer exists and the freshly booked 400MB describes the
    process that is now running — reverting understates ``active_sizes`` and
    lets a later acquire overcommit, i.e. exactly the bug the size-tracking
    fix is meant to prevent.
    """
    proc = FakeServerProcess()
    mgr = _make_manager(
        tmp_path,
        process=proc,
        models={"minimal": _sized_model(300)},
        vram_limit_mb=500,
    )
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    await mgr.switch_model("minimal")
    assert mgr.vram is not None

    _set_models_file(tmp_path, {"minimal": _sized_model(400)})
    _bump_mtime(cfg, tick)
    mgr._load_context = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await mgr.switch_model("minimal", force=True)

    assert mgr.current_model == "minimal", "the new backend is up and verified"
    assert mgr.vram.active_sizes["minimal"] == 400, (
        "the new backend's booking survives a post-start failure: "
        f"{dict(mgr.vram.active_sizes)}"
    )
    assert mgr._vram_size_update is None, "no rollback token survives the switch"


async def test_post_start_swap_failure_does_not_restore_released_old_slot(
    tmp_path: Path, patch_paths: None
) -> None:
    """Guard for the swap rollback token: alpha→beta (300→400, budget 500) with
    a post-start failure must NOT resurrect alpha's released 300MB slot — alpha
    is stopped, only beta's 400MB booking may remain."""
    proc = FakeServerProcess()
    mgr = _make_manager(
        tmp_path,
        process=proc,
        models={"alpha": _sized_model(300), "beta": _sized_model(400)},
        vram_limit_mb=500,
    )
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    await mgr.switch_model("alpha")
    assert mgr.vram is not None

    _set_models_file(tmp_path, {"alpha": _sized_model(300), "beta": _sized_model(400)})
    _bump_mtime(cfg, tick)
    mgr._load_context = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await mgr.switch_model("beta", force=True)

    assert mgr.current_model == "beta"
    assert dict(mgr.vram.active_sizes) == {"beta": 400}, (
        f"only the running model may hold a slot: {dict(mgr.vram.active_sizes)}"
    )
    assert mgr._vram_released_held is None


async def test_swap_rollback_releases_unused_target_slot(
    tmp_path: Path, patch_paths: None
) -> None:
    """alpha(300)→beta(400), budget 500: a pre-stop failure AFTER beta's slot
    was acquired (auto-save cancelled) must release beta's unused reservation
    and restore alpha's exact held size.

    A leftover ``beta`` slot would be a phantom: beta never started, yet the
    booked 400MB + alpha's real footprint exceed the budget, so every later
    acquire (including a retry of beta itself) would block on space no release
    can free.
    """
    proc = FakeServerProcess()
    mgr = _make_manager(
        tmp_path,
        process=proc,
        models={"alpha": _sized_model(300), "beta": _sized_model(400)},
        vram_limit_mb=500,
    )
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    await mgr.switch_model("alpha")
    assert mgr.vram is not None

    _set_models_file(tmp_path, {"alpha": _sized_model(300), "beta": _sized_model(400)})
    _bump_mtime(cfg, tick)
    mgr._save_context = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await mgr.switch_model("beta", force=True)

    assert mgr.current_model == "alpha", "pre-stop failure: alpha keeps running"
    assert dict(mgr.vram.active_sizes) == {"alpha": 300}, (
        f"only the running model may hold a slot: {dict(mgr.vram.active_sizes)}"
    )
    assert mgr.vram.active_counts.get("beta", 0) == 0, "no phantom target slot"
    assert mgr._vram_released_held is None


# ------------------------------- startup signature capture + non-finite polls
async def test_startup_edit_between_parse_and_signature_capture_is_not_lost(
    tmp_path: Path, patch_paths: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write landing between the constructor's parse and its signature
    capture must not be recorded as "already loaded".

    Pre-fix ordering (signature taken AFTER the parse) paired the OLD
    in-memory snapshot with the NEW file signature, so every later reload
    check saw an unchanged file and the edit stayed unapplied until an
    unrelated write changed mtime again. The signature is captured BEFORE the
    parse now, so the stale signature makes the next check re-read the file.
    """
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    real_load = config_mod.load_models_config
    raced: list[bool] = []

    def racing_load(path=None):
        """Simulate the operator's write in the parse→stat window."""
        loaded = real_load(path)  # the snapshot the constructor will publish
        if not raced:
            raced.append(True)
            _set_models_file(
                tmp_path, {"minimal": dict(MODEL_A), "added": dict(MODEL_NEW)}
            )
            _bump_mtime(cfg, tick)
        return loaded

    monkeypatch.setattr(config_mod, "load_models_config", racing_load)
    mgr = _make_manager(
        tmp_path, process=FakeServerProcess(), models={"minimal": dict(MODEL_A)}
    )

    assert raced, "fixture sanity: the constructor must have parsed the config"
    assert "added" not in mgr.models, "the published snapshot is the pre-edit one"

    assert mgr._maybe_reload_config() is True, (
        "the pre-edit signature must not mask the file change"
    )
    assert "added" in mgr.models, "the update must still be applied on the next check"


def test_non_finite_health_polls_edit_does_not_break_status(
    tmp_path: Path, patch_paths: None
) -> None:
    """``health_polls: .inf`` must be ignored, not raised out of every entry point.

    ``int(float('inf'))`` raises ``OverflowError``, which the pre-fix handler
    (``TypeError``/``ValueError`` only) did not catch. Because the reload call
    sat AFTER the config and signature were published, the first
    status/switch/unload call after that edit failed while the file was
    already recorded as loaded — the same "silently unapplied" trap.
    """
    mgr = _make_manager(
        tmp_path, process=FakeServerProcess(), models={"minimal": dict(MODEL_A)}
    )
    cfg = tmp_path / "models.local.settings.yaml"
    tick = [time.time_ns()]
    assert mgr.health_polls == DEFAULT_HEALTH_POLLS

    _set_models_file(
        tmp_path, {"minimal": dict(MODEL_A)}, extra={"health_polls": float("inf")}
    )
    assert ".inf" in cfg.read_text(encoding="utf-8")
    _bump_mtime(cfg, tick)

    payload = mgr.health()  # pre-fix: OverflowError escapes here

    assert payload["loaded_model"] is None
    assert mgr.health_polls == DEFAULT_HEALTH_POLLS, "the bad value must not stick"

    # The reload still went through: a later valid edit applies normally.
    _set_models_file(
        tmp_path, {"minimal": dict(MODEL_A)}, extra={"health_polls": 555}
    )
    _bump_mtime(cfg, tick)
    mgr.health()
    assert mgr.health_polls == 555


def test_non_finite_health_polls_at_boot_falls_back_to_default(
    tmp_path: Path,
) -> None:
    """Same ignore path at boot: ``.inf`` must not abort the daemon constructor.

    ``__init__`` reads the knob too, so a non-finite value used to take the
    whole caretaker down at startup instead of just losing the override.
    """
    cfg = _set_models_file(
        tmp_path, {"minimal": dict(MODEL_A)}, extra={"health_polls": float("inf")}
    )
    mgr = Caretaker(config_path=str(cfg), server_process=FakeServerProcess())
    assert mgr.health_polls == DEFAULT_HEALTH_POLLS
